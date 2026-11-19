"""Reading a trace from its end (v0.13.1, G24; contract docs/v0.12-contracts.md 12.4): the lines newest first, in blocks, a
half-written last line skipped; pages with cursors; the thinking record of one agent.  Nothing here loads a file whole."""

from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.runtime import trace_log as trace_module
from app.runtime.trace_log import (
    DEFAULT_SCAN_BYTES,
    MAX_SHORT_CHARS,
    MAX_TEXT_CHARS,
    ScanStats,
    TraceLog,
    finite_number,
    format_cursor,
    iter_lines_backwards,
    parse_cursor,
    read_thoughts,
    scan_events_backwards,
    thought_item,
    trace_dir,
    trace_files,
    utc_iso,
)


def forward(path: Path) -> list[tuple[int, bytes]]:
    """The complete, non-blank lines of ``path`` with the offset of their first byte, read the plain way."""
    data = path.read_bytes()
    found: list[tuple[int, bytes]] = []
    offset = 0
    for raw in data.split(b"\n")[:-1]:  # (the part after the last newline is not a complete line)
        if raw:
            found.append((offset, raw))
        offset += len(raw) + 1
    return found


# ---------------------------------------------------------------------------------- iter_lines_backwards
@pytest.mark.parametrize("block_size", [16, 17, 31, 64, 1000, 1 << 20])
def test_lines_come_back_last_first_with_the_offset_of_their_first_byte(tmp_path, block_size):
    path = tmp_path / "t.jsonl"
    lines = [("x" * n).encode() for n in (1, 5, 0, 40, 3, 200, 17, 2, 64, 33)]
    lines.append("多字节的行 ✓ ünïcode".encode("utf-8"))  # (a multi-byte character may straddle a block boundary)
    path.write_bytes(b"\n".join(lines) + b"\n")
    expected = forward(path)
    assert len(expected) == 10  # the blank line is not one
    assert list(iter_lines_backwards(path, block_size=block_size)) == expected[::-1]


@pytest.mark.parametrize("block_size", [16, 64, 4096])
def test_a_last_line_without_its_newline_is_not_a_line_yet(tmp_path, block_size):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b'{"a":1}\n{"b":2}\n{"c":3,"half":"wri')  # the writer is in the middle of it
    assert [line for _offset, line in iter_lines_backwards(path, block_size=block_size)] == [b'{"b":2}', b'{"a":1}']


@pytest.mark.parametrize("content", [b"", b"no newline at all", b"\n", b"\n\n\n"])
def test_a_file_without_a_complete_non_blank_line_has_no_lines(tmp_path, content):
    path = tmp_path / "t.jsonl"
    path.write_bytes(content)
    assert list(iter_lines_backwards(path, block_size=16)) == []


def test_a_missing_file_or_directory_is_just_empty(tmp_path):
    assert list(iter_lines_backwards(tmp_path / "nowhere.jsonl")) == []
    assert list(iter_lines_backwards(tmp_path)) == []  # (a directory cannot be read: OSError, not a crash)


def test_end_limits_the_file_to_its_first_bytes_and_drops_the_line_it_cuts(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"aaaa\nbbbb\ncccc\ndddd\n")
    assert [o for o, _ in iter_lines_backwards(path, end=10, block_size=16)] == [5, 0]  # 10 = the start of "cccc"
    assert [line for _, line in iter_lines_backwards(path, end=13, block_size=16)] == [b"bbbb", b"aaaa"]  # cut "cccc": gone
    assert [line for _, line in iter_lines_backwards(path, end=14, block_size=16)] == [b"bbbb", b"aaaa"]
    assert [line for _, line in iter_lines_backwards(path, end=15, block_size=16)] == [b"cccc", b"bbbb", b"aaaa"]
    assert list(iter_lines_backwards(path, end=0)) == [] and list(iter_lines_backwards(path, end=3)) == []
    assert len(list(iter_lines_backwards(path, end=10**9))) == 4  # beyond the end: the whole file


def test_a_line_longer_than_many_blocks_is_stitched_together(tmp_path):
    path = tmp_path / "t.jsonl"
    long_line = b"L" * 5000
    path.write_bytes(b"first\n" + long_line + b"\nlast\n")
    assert list(iter_lines_backwards(path, block_size=16)) == [(5007, b"last"), (6, long_line), (0, b"first")]


def test_windows_line_endings_leave_the_carriage_return_to_the_json_parser(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b'{"event":"thought","agent_id":"a1","text":"hi"}\r\n{"event":"x"}\r\n')
    lines = [line for _o, line in iter_lines_backwards(path)]
    assert lines[1].endswith(b"\r") and json.loads(lines[1])["text"] == "hi"


def test_a_file_that_shrinks_while_it_is_read_ends_the_reading_quietly(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"x" * 100 + b"\n" + b"y" * 100 + b"\n")
    reader = iter_lines_backwards(path, block_size=32)
    assert next(reader) == (101, b"y" * 100)
    path.write_bytes(b"short\n")  # replaced by something smaller
    assert list(reader) == []


# ------------------------------------------------------- a line that is many blocks long (review: it was read in quadratic time)
def naive_backwards(data: bytes, end: int | None = None) -> list[tuple[int, bytes]]:
    """The reference: the complete non-blank lines of ``data[:end]``, last first, with the offset of their first byte."""
    if end is not None:
        data = data[: max(0, min(end, len(data)))]
    found: list[tuple[int, bytes]] = []
    offset = 0
    for piece in data.split(b"\n")[:-1]:  # (what follows the last newline is no line yet)
        if piece:
            found.append((offset, piece))
        offset += len(piece) + 1
    return found[::-1]


def test_the_backwards_reader_agrees_with_the_plain_way_on_random_files(tmp_path):
    rng = random.Random(20261118)
    alphabet = [b"a", b"b", b"\n", b"\r\n", "é".encode(), "多".encode(), "😀".encode(), b"\n\n", b"x" * 40, b"y" * 300]
    path = tmp_path / "fuzz.bin"
    for _ in range(250):
        data = b"".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        path.write_bytes(data)
        for block in (16, 17, 23, 64, 1000):
            for end in (None, rng.randint(0, len(data) + 3), 0, len(data)):
                assert list(iter_lines_backwards(path, end=end, block_size=block)) == naive_backwards(data, end), (data, block, end)


@pytest.mark.parametrize("block_size", [16, 100, 4096])
@pytest.mark.parametrize("where", ["only", "first", "middle", "last", "several"])
def test_a_line_spanning_many_blocks_comes_back_whole_and_in_place(tmp_path, block_size, where):
    long = b"L" * 2500 + "多".encode() * 100 + b"M" * 1700  # (multi-byte characters inside: they straddle block borders)
    lines = {
        "only": [long],
        "first": [long, b"b", b"c"],
        "middle": [b"a", long, b"c"],
        "last": [b"a", b"b", long],
        "several": [long, b"b", long + b"2", long + b"3", b"e"],
    }[where]
    data = b"\n".join(lines) + b"\n"
    path = tmp_path / "t.jsonl"
    path.write_bytes(data)
    assert list(iter_lines_backwards(path, block_size=block_size)) == naive_backwards(data)
    unfinished = data + long[:3001]  # a long last line the writer has not finished: it is no line yet
    path.write_bytes(unfinished)
    assert list(iter_lines_backwards(path, block_size=block_size)) == naive_backwards(data)
    for end in (len(data) // 2, len(data) - 1, 6):  # a cursor that cuts a long line: that line is gone, the others stay
        assert list(iter_lines_backwards(path, end=end, block_size=block_size)) == naive_backwards(unfinished, end)


def test_a_single_very_long_line_costs_no_more_than_the_same_bytes_in_many_short_lines(tmp_path):
    """Review: a 40 MB single line took 7 s, because every block of it was copied again and the whole buffer was searched
    again for newlines (quadratic).  No fixed time limit here: the same number of bytes as ONE line must not cost much more
    than as many short lines.  (Measured: one line 45 ms, many lines 77 ms; the old way took 10.8 s for the one line - 240
    times as long - so the comparison has room for a slow, noisy machine and still tells the two apart.)"""
    size = 2 * 1024 * 1024
    unit = b'{"event":"x","pad":"' + b"y" * 40 + b'"}\n'
    one, many = tmp_path / "one.jsonl", tmp_path / "many.jsonl"
    one.write_bytes(b'{"event":"x","pad":"' + b"y" * size + b'"}\n')
    many.write_bytes(unit * (size // len(unit)))
    started = time.perf_counter()
    long_lines = list(iter_lines_backwards(one, block_size=256))  # ~8000 blocks of ONE line
    t_one = time.perf_counter() - started
    started = time.perf_counter()
    short_lines = sum(1 for _ in iter_lines_backwards(many, block_size=256))
    t_many = time.perf_counter() - started
    assert len(long_lines) == 1 and long_lines[0][0] == 0 and len(long_lines[0][1]) == size + 22
    assert long_lines[0][1].startswith(b'{"event":"x"') and long_lines[0][1].endswith(b'yy"}')
    assert short_lines == size // len(unit)
    assert t_one < 20 * t_many + 2.0, (t_one, t_many)


# ----------------------------------------------------------------------------------- scan_events_backwards
def write_events(trace: TraceLog, *events: tuple[str, str, dict[str, Any]]) -> None:
    for event, agent, fields in events:
        trace.write(event, agent, **fields)
    trace.close()


def test_records_come_newest_first_across_rolled_files_with_their_positions(tmp_path):
    trace = TraceLog(tmp_path, "s", max_bytes=400)
    for n in range(40):
        trace.write("thought", "a1", n=n, text="t" * 30)
    trace.close()
    files = trace_files(trace.directory)
    assert len(files) > 3 and [i for i, _ in files] == list(range(1, len(files) + 1))
    found = list(scan_events_backwards(trace.directory, ["thought"]))
    assert [r["n"] for _i, _o, r in found] == list(range(39, -1, -1))
    indexes = [i for i, _o, _r in found]
    assert indexes == sorted(indexes, reverse=True)
    for index, offset, record in found:  # the position really is where the line starts
        path = dict(files)[index]
        assert json.loads(path.read_bytes()[offset:].split(b"\n", 1)[0])["n"] == record["n"]


def test_only_the_wanted_events_of_the_wanted_agent_are_returned(tmp_path):
    trace = TraceLog(tmp_path, "s")
    write_events(
        trace,
        ("thought", "a1", {"text": "mine"}),
        ("thought", "a2", {"text": "somebody else's"}),
        ("tool_call", "a1", {"name": "speak", "args": {"content": '"event":"thought","agent_id":"a1"'}}),  # a look-alike
        ("content", "a1", {"text": "monologue"}),
        ("llm_call", "a1", {"model": "m"}),
        ("thought", "a10", {"text": "a1 with a longer id"}),
    )
    found = list(scan_events_backwards(trace.directory, ["thought", "content"], agent_id="a1"))
    assert [r["text"] for _i, _o, r in found] == ["monologue", "mine"]
    assert [r["text"] for _i, _o, r in scan_events_backwards(trace.directory, ["thought"])] == [
        "a1 with a longer id", "somebody else's", "mine",
    ]
    assert list(scan_events_backwards(trace.directory, [])) == []


def test_garbage_lines_and_non_objects_are_ignored(tmp_path):
    directory = tmp_path / "s"
    directory.mkdir()
    (directory / "trace-0001.jsonl").write_bytes(
        b'{"event":"thought","agent_id":"a1","text":"one"}\n'
        b'{"event":"thought","agent_id":"a1","text":"broken\n'
        b'["event","thought"]\n'
        b'\xff\xfe{"event":"thought","agent_id":"a1"}\n'  # not valid UTF-8
        b'{"event":"thought","agent_id":"a1","text":"two"}\n'
    )
    assert [r["text"] for _i, _o, r in scan_events_backwards(directory, ["thought"], agent_id="a1")] == ["two", "one"]


def test_before_means_strictly_in_front_of_that_line_across_files(tmp_path):
    trace = TraceLog(tmp_path, "s", max_bytes=300)
    for n in range(20):
        trace.write("thought", "a1", n=n, text="t" * 40)
    trace.close()
    everything = list(scan_events_backwards(trace.directory, ["thought"]))
    for cut in (0, 7, 19):
        index, offset, record = everything[cut]
        older = list(scan_events_backwards(trace.directory, ["thought"], before=(index, offset)))
        assert [r["n"] for _i, _o, r in older] == [r["n"] for _i, _o, r in everything[cut + 1 :]]
    assert list(scan_events_backwards(trace.directory, ["thought"], before=(1, 0))) == []  # nothing in front of the first line
    beyond = list(scan_events_backwards(trace.directory, ["thought"], before=(99, 0)))
    assert len(beyond) == 20  # a cursor past the newest file: everything is in front of it


def test_the_scan_stops_at_its_byte_budget_and_says_where(tmp_path):
    trace = TraceLog(tmp_path, "s")
    for n in range(100):
        trace.write("thought", "a1", n=n, text="t" * 100)
    trace.close()
    stats = ScanStats()
    found = list(scan_events_backwards(trace.directory, ["thought"], max_bytes=1000, stats=stats))
    assert 0 < len(found) < 100 and stats.truncated is True and stats.scanned >= 1000
    index, offset = stats.position  # type: ignore[misc]
    rest = list(scan_events_backwards(trace.directory, ["thought"], before=(index, offset)))
    assert len(found) + len(rest) <= 100 and rest and rest[0][2]["n"] < found[-1][2]["n"]
    whole = ScanStats()
    assert len(list(scan_events_backwards(trace.directory, ["thought"], stats=whole))) == 100 and whole.truncated is False
    assert DEFAULT_SCAN_BYTES > 20 * 1024 * 1024  # (a whole trace file fits)


def test_a_directory_without_trace_files_is_empty(tmp_path):
    assert list(scan_events_backwards(tmp_path / "nowhere", ["thought"])) == []
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "notes.txt").write_text("x", encoding="utf-8")
    assert list(scan_events_backwards(tmp_path / "empty", ["thought"])) == []


# ------------------------------------------------------------------------------------------ the thinking record
def build_game_trace(tmp_path, **kwargs: Any) -> TraceLog:
    """A trace the way agents write it: wake-ups, calls, thoughts, monologues, tool calls - for two agents."""
    trace = TraceLog(tmp_path, "game", **kwargs)
    n = 0
    for gen in (1, 2, 3):
        for agent in ("a1", "a2"):
            trace.write("wake", agent, gen=gen)
            for rnd in (0, 1):
                n += 1
                trace.write("llm_call", agent, gen=gen, round=rnd, model="m1", tool_calls=["speak"], gt=float(n))
                trace.write("thought", agent, gen=gen, round=rnd, text=f"{agent} thought {n}", gt=float(n), model="m1")
                trace.write("content", agent, gen=gen, round=rnd, text=f"{agent} said {n} next to a tool", gt=float(n), model="m1", with_tools=True)
                trace.write("tool_call", agent, gen=gen, name="speak", args={"content": f"public words {n}"}, result="ok")
    trace.write("llm_call", "a1", gen=4, round=0, model="m1", tool_calls=[], gt=99.5)
    trace.write("content", "a1", gen=4, round=0, text="a1 monologue without tools", gt=99.5, model="m1", with_tools=False)
    trace.close()
    return trace


def test_only_thoughts_and_the_monologue_of_a_call_without_tools_are_read_never_tool_calls(tmp_path):
    trace = build_game_trace(tmp_path)
    page = read_thoughts(trace.directory, "a1", limit=200)
    texts = [item["text"] for item in page["items"]]
    assert texts == ["a1 thought 1", "a1 thought 2", "a1 thought 5", "a1 thought 6", "a1 thought 9", "a1 thought 10", "a1 monologue without tools"]
    assert page["has_more"] is False and page["next_before"] is None
    assert [item["kind"] for item in page["items"]] == ["thought"] * 6 + ["content"]
    everything = json.dumps(page, ensure_ascii=False)
    assert "public words" not in everything and "tool" not in everything.replace("without tools", "")  # nothing of the tool calls
    assert all(item["text"].startswith("a1") for item in read_thoughts(trace.directory, "a1", limit=200)["items"])
    assert [i["text"] for i in read_thoughts(trace.directory, "a2", limit=200)["items"]] == [f"a2 thought {n}" for n in (3, 4, 7, 8, 11, 12)]
    assert read_thoughts(trace.directory, "moderator", limit=200) == {"items": [], "next_before": None, "has_more": False}


def test_an_item_carries_its_time_generation_round_and_model(tmp_path):
    trace = build_game_trace(tmp_path)
    items = read_thoughts(trace.directory, "a1", limit=200)["items"]
    first, last = items[0], items[-1]
    assert set(first) == {"id", "ts", "gt", "gen", "round", "kind", "text", "model"}
    assert (first["gt"], first["gen"], first["round"], first["model"]) == (1.0, 1, 1, "m1")  # round counts from 1 here
    assert items[1]["round"] == 2 and items[2]["gen"] == 2 and (last["gen"], last["round"], last["gt"]) == (4, 1, 99.5)
    assert first["ts"].endswith("Z") and "T" in first["ts"]
    index, offset = (int(part) for part in first["id"].split(":"))
    assert index == 1 and json.loads(trace.files()[0].read_bytes()[offset:].split(b"\n", 1)[0])["text"] == "a1 thought 1"
    assert len({item["id"] for item in items}) == len(items)  # unique: a DOM key


def test_pages_run_from_the_newest_back_and_together_cover_everything_once(tmp_path):
    trace = TraceLog(tmp_path, "paged", max_bytes=1500)  # rolled: the pages cross files
    for n in range(23):
        trace.write("thought", "a1", gen=n, round=0, text=f"thought {n}", gt=float(n))
        trace.write("thought", "a2", gen=n, round=0, text=f"other {n}")
    trace.close()
    assert len(trace.files()) > 2
    pages: list[dict[str, Any]] = []
    before = None
    while True:
        page = read_thoughts(trace.directory, "a1", before=before, limit=5)
        pages.append(page)
        if not page["has_more"]:
            break
        before = page["next_before"]
        assert before == page["items"][0]["id"]  # (the cursor is the oldest item of the page)
    assert [len(p["items"]) for p in pages] == [5, 5, 5, 5, 3]
    assert pages[0]["items"][-1]["text"] == "thought 22"  # the newest page first ...
    assert [i["text"] for i in pages[0]["items"]] == [f"thought {n}" for n in range(18, 23)]  # ... each page oldest to newest
    gathered = [i["text"] for p in reversed(pages) for i in p["items"]]
    assert gathered == [f"thought {n}" for n in range(23)]
    assert pages[-1]["next_before"] is None


def test_a_page_that_exactly_exhausts_the_record_has_nothing_more(tmp_path):
    trace = TraceLog(tmp_path, "s")
    for n in range(5):
        trace.write("thought", "a1", text=f"t{n}")
    trace.close()
    exact = read_thoughts(trace.directory, "a1", limit=5)
    assert len(exact["items"]) == 5 and exact["has_more"] is False and exact["next_before"] is None
    short = read_thoughts(trace.directory, "a1", limit=4)
    assert len(short["items"]) == 4 and short["has_more"] is True
    older = read_thoughts(trace.directory, "a1", before=short["next_before"], limit=4)
    assert [i["text"] for i in older["items"]] == ["t0"] and older["has_more"] is False


def test_a_blank_thought_is_not_an_entry_and_the_limit_is_at_least_one(tmp_path):
    trace = TraceLog(tmp_path, "s")
    trace.write("thought", "a1", text="  \n ")
    trace.write("thought", "a1", text="real")
    trace.write("thought", "a1")  # (no text at all)
    trace.close()
    page = read_thoughts(trace.directory, "a1", limit=0)
    assert [i["text"] for i in page["items"]] == ["real"] and page["has_more"] is False


def test_an_old_trace_without_gt_gen_or_model_still_reads(tmp_path):
    trace = TraceLog(tmp_path, "s")
    trace.write("thought", "a1", text="old style")
    trace.close()
    (item,) = read_thoughts(trace.directory, "a1")["items"]
    assert (item["gt"], item["gen"], item["round"], item["model"]) == (None, None, None, None) and item["text"] == "old style"


def test_a_bad_cursor_is_refused_and_a_cursor_past_everything_is_empty(tmp_path):
    trace = build_game_trace(tmp_path)
    for bad in ("nonsense", "1", "1:2:3", "-1:5", "a:b"):
        with pytest.raises(ValueError):
            read_thoughts(trace.directory, "a1", before=bad)
    assert read_thoughts(trace.directory, "a1", before="1:0")["items"] == []  # in front of the very first line
    assert len(read_thoughts(trace.directory, "a1", before="999:0", limit=200)["items"]) == 7


def test_the_texts_keep_the_scrubbing_the_trace_gave_them(tmp_path):
    trace = TraceLog(tmp_path, "s")
    trace.write("thought", "a1", text="the key is sk-abcdef1234567890 ok")
    trace.close()
    assert "sk-abcdef" not in read_thoughts(trace.directory, "a1")["items"][0]["text"]


def test_a_cut_short_scan_says_there_is_more_and_where_to_go_on(tmp_path):
    trace = TraceLog(tmp_path, "s")
    for n in range(200):
        trace.write("tool_call", "a1", name="speak", args={"pad": "x" * 200})  # a long stretch without a thought
    trace.write("thought", "a1", text="long ago")
    for n in range(200):
        trace.write("tool_call", "a1", name="speak", args={"pad": "x" * 200})
    trace.close()
    first = read_thoughts(trace.directory, "a1", max_bytes=20_000)
    assert first["items"] == [] and first["has_more"] is True and parse_cursor(first["next_before"]) is not None
    again = first
    for _ in range(40):  # (each request looks at a little more of the file)
        if again["items"] or not again["has_more"]:
            break
        again = read_thoughts(trace.directory, "a1", before=again["next_before"], max_bytes=20_000)
    assert [i["text"] for i in again["items"]] == ["long ago"]


def test_the_newest_page_of_a_large_trace_reads_a_few_blocks_not_the_file(tmp_path, monkeypatch):
    trace = TraceLog(tmp_path, "big")
    for n in range(1500):
        trace.write("thought", "a1", gen=n, round=0, text=f"thought {n} " + "x" * 800)
        trace.write("tool_call", "a1", name="speak", args={"pad": "y" * 800}, result="z" * 800)
    trace.close()
    size = sum(path.stat().st_size for _i, path in trace_files(trace.directory))
    assert size > 3_000_000
    read = {"bytes": 0}
    real_open = open

    class Counting:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def __enter__(self) -> "Counting":
            self._handle.__enter__()
            return self

        def __exit__(self, *exc: Any) -> Any:
            return self._handle.__exit__(*exc)

        def read(self, n: int = -1) -> bytes:
            data = self._handle.read(n)
            read["bytes"] += len(data)
            return data

        def __getattr__(self, name: str) -> Any:
            return getattr(self._handle, name)

    monkeypatch.setattr(trace_module, "open", lambda path, mode="r": Counting(real_open(path, mode)), raising=False)
    page = read_thoughts(trace.directory, "a1", limit=10, block_size=64 * 1024)
    assert [i["text"][:12] for i in page["items"]][-1] == "thought 1499" and len(page["items"]) == 10
    assert read["bytes"] < 400_000 < size  # a few blocks of a 3+ MB trace
    assert page["has_more"] is True


def test_reading_while_the_trace_is_being_written_never_sees_a_torn_line(tmp_path):
    trace = TraceLog(tmp_path, "live", max_bytes=200_000)
    stop = threading.Event()
    failures: list[BaseException] = []

    def write() -> None:
        n = 0
        while not stop.is_set():
            trace.write("thought", "a1", gen=n, round=0, text=f"thought {n} " + "w" * 300)
            n += 1

    writer = threading.Thread(target=write)
    writer.start()
    try:
        for _ in range(60):
            try:
                page = read_thoughts(trace.directory, "a1", limit=50)
                gens = [item["gen"] for item in page["items"]]
                assert gens == sorted(gens) and len(set(gens)) == len(gens)
                assert all(item["text"].startswith(f"thought {item['gen']} ") for item in page["items"])
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)
                break
    finally:
        stop.set()
        writer.join()
        trace.close()
    assert not failures, failures


# ------------------------------------------------------------------------------------------------ small things
def test_cursors_round_trip_and_reject_everything_else():
    assert format_cursor(2, 1048576) == "2:1048576" and parse_cursor("2:1048576") == (2, 1048576)
    assert parse_cursor(" 3:4 ") == (3, 4)
    for bad in (None, "", "1", "1:", ":1", "a:1", "1:b", "1:2:3", "-1:2", "1:-2", "1.5:2", "1234567890:1", "1:1234567890123456"):
        assert parse_cursor(bad) is None, bad


def test_utc_iso_turns_a_local_time_with_an_offset_into_utc_with_a_z():
    assert utc_iso("2026-11-16T23:30:18.123-08:00") == "2026-11-17T07:30:18.123Z"
    assert utc_iso("2026-11-18T16:00:00+00:00") == "2026-11-18T16:00:00.000Z"
    assert utc_iso("2026-11-18T08:00:00").endswith("Z")  # (no offset: it was local time)
    for bad in (None, "", "yesterday", 5, ["2026"]):
        assert utc_iso(bad) is None


def test_thought_item_is_tolerant_of_odd_values():
    item = thought_item(3, 77, {"event": "content", "ts": "x", "gt": True, "gen": "7", "round": 2.5, "text": None, "model": ""})
    assert item == {"id": "3:77", "ts": None, "gt": None, "gen": None, "round": None, "kind": "content", "text": "", "model": None}


def test_trace_dir_makes_a_session_id_safe_and_trace_files_lists_the_files_in_order(tmp_path):
    assert trace_dir(tmp_path, "abc123") == tmp_path / "abc123"
    assert trace_dir(tmp_path, "../../evil/..\\x").resolve().is_relative_to(tmp_path.resolve())
    assert trace_dir(tmp_path, "") == tmp_path / "session"
    directory = tmp_path / "s"
    directory.mkdir()
    for name in ("trace-0002.jsonl", "trace-0001.jsonl", "trace-10000.jsonl", "trace-0099.jsonl", "trace-1.jsonl", "notes.txt", "trace-0003.jsonl.bak"):
        (directory / name).write_text("{}\n", encoding="utf-8")
    assert [(i, p.name) for i, p in trace_files(directory)] == [
        (1, "trace-0001.jsonl"), (2, "trace-0002.jsonl"), (99, "trace-0099.jsonl"), (10000, "trace-10000.jsonl"),
    ]
    assert trace_files(tmp_path / "nowhere") == []
    assert TraceLog(tmp_path, "abc123").directory == trace_dir(tmp_path, "abc123")


def test_a_trace_log_can_scrub_a_text_with_the_secrets_it_knows(tmp_path):
    trace = TraceLog(tmp_path, "s", secrets=["registered-secret-1"])
    trace.add_secret("another-secret-22")
    assert trace.redact("a registered-secret-1 b another-secret-22 c sk-abcdef1234567890") == "a *** b *** c sk-***"
    assert trace.redact("") == ""


# ================================================================================ after the review (v0.13.1)
def test_a_trace_log_says_whether_it_holds_its_file_open(tmp_path):
    trace = TraceLog(tmp_path, "s")
    assert trace.is_open is False  # (a log is only opened by its first write)
    trace.write("wake", "a1")
    assert trace.is_open is True
    trace.close()
    assert trace.is_open is False
    trace.write("wake", "a1")  # a later write reopens it
    assert trace.is_open is True
    with TraceLog(tmp_path, "s2") as other:
        other.write("wake", "a1")
        assert other.is_open
    assert other.is_open is False
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x", encoding="utf-8")
    broken = TraceLog(blocker, "s")  # (cannot be opened: still not "open")
    broken.write("wake", "a1")
    assert broken.is_open is False
    trace.close()


def test_cursors_take_ascii_digits_only():
    # ``\d`` would take the digits of any script (and ``int()`` would read them): a cursor is what this module made
    for bad in ("٣:٤", "１:２", "1:２", "1:2\x00", "0x1:2", "1_0:5", "+1:2", " ", "1: 2", "1 :2"):
        assert parse_cursor(bad) is None, bad
    assert parse_cursor(" 3:4\n") == (3, 4)  # (surrounding white space is ignored, as before)


def test_a_trace_file_name_needs_ascii_digits_too(tmp_path):
    directory = tmp_path / "s"
    directory.mkdir()
    for name in ("trace-0001.jsonl", "trace-٠٠٠٢.jsonl"):  # the second is "trace-0002" in Arabic-Indic digits
        (directory / name).write_text("{}\n", encoding="utf-8")
    assert [(i, p.name) for i, p in trace_files(directory)] == [(1, "trace-0001.jsonl")]
    assert [p.name for p in TraceLog(tmp_path, "s").files()] == ["trace-0001.jsonl"]


def test_a_line_nested_too_deeply_for_the_parser_is_skipped_not_a_server_error(tmp_path):
    directory = tmp_path / "s"
    directory.mkdir()
    deep = b'{"event":"thought","agent_id":"a1","text":"deep","x":' + b"[" * 100_000 + b"]" * 100_000 + b"}\n"
    (directory / "trace-0001.jsonl").write_bytes(
        b'{"event":"thought","agent_id":"a1","text":"before"}\n' + deep + b'{"event":"thought","agent_id":"a1","text":"after"}\n'
    )
    assert [i["text"] for i in read_thoughts(directory, "a1")["items"]] == ["before", "after"]
    assert [r["text"] for _i, _o, r in scan_events_backwards(directory, ["thought"])] == ["after", "before"]


def test_a_repeated_key_with_a_value_of_another_type_is_skipped_not_a_server_error(tmp_path):
    directory = tmp_path / "s"
    directory.mkdir()
    (directory / "trace-0001.jsonl").write_bytes(
        b'{"event":"thought","agent_id":"a1","text":"one"}\n'
        b'{"event":"thought","event":["thought"],"agent_id":"a1","text":"the last event key wins: a list"}\n'
        b'{"event":"thought","event":{"x":1},"agent_id":"a1","text":"a dict"}\n'
        b'{"event":"thought","agent_id":"a1","agent_id":{"x":1},"text":"somebody else"}\n'
        b'{"event":"thought","agent_id":"a1","text":"two"}\n'
    )
    assert [i["text"] for i in read_thoughts(directory, "a1")["items"]] == ["one", "two"]


def test_values_the_api_cannot_send_become_null_and_the_page_can_be_sent_as_json(tmp_path):
    directory = tmp_path / "s"
    directory.mkdir()
    (directory / "trace-0001.jsonl").write_bytes(
        b'{"event":"thought","agent_id":"a1","text":"nan time","gt":NaN,"gen":1,"round":0,"model":"m"}\n'
        b'{"event":"thought","agent_id":"a1","text":"inf time","gt":Infinity,"gen":2,"round":0}\n'
        b'{"event":"thought","agent_id":"a1","text":"year one","gt":-Infinity,"ts":"0001-01-01T00:00:00+05:00","gen":3}\n'
        b'{"event":"thought","agent_id":"a1","text":"year 9999","ts":"9999-12-31T23:59:59-12:00","gen":4}\n'
    )
    page = read_thoughts(directory, "a1")
    assert [i["gt"] for i in page["items"]] == [None, None, None, None]
    assert page["items"][2]["ts"] is None and page["items"][3]["ts"] is None  # (not convertible to UTC: unknown, not a crash)
    json.dumps(page, allow_nan=False)  # what the API does with the page: no NaN, no Infinity anywhere


def test_finite_number_is_a_number_the_api_can_send():
    assert finite_number(3) == 3 and finite_number(2.5) == 2.5 and finite_number(10**30) == 10**30 and finite_number(-0.0) == 0
    for bad in (True, False, None, "1", [1], float("nan"), float("inf"), float("-inf")):
        assert finite_number(bad) is None, bad
    assert utc_iso("0001-01-01T00:00:00+05:00") is None and utc_iso("9999-12-31T23:59:59-12:00") is None  # (no OverflowError)


def test_what_an_imported_trace_puts_into_a_thought_is_cut_at_what_the_trace_log_itself_keeps(tmp_path):
    item = thought_item(
        1, 0, {"event": "thought", "text": "t" * 500_000, "model": "m" * 10_000, "gen": 10**40, "round": 3, "gt": 1e300}
    )
    assert len(item["text"]) == MAX_TEXT_CHARS and item["text"].endswith("…")
    assert len(item["model"]) == MAX_SHORT_CHARS == 120 and item["model"].endswith("…")
    assert item["gen"] == 10**40 and item["round"] == 4 and item["gt"] == 1e300  # (numbers are numbers)
    assert thought_item(1, 0, {"event": "thought", "text": ["a", "b"], "model": {"id": "m"}})["text"] == "['a', 'b']"
    assert thought_item(1, 0, {"event": "thought", "text": "x", "model": 0})["model"] is None  # (falsy: none)

    class NoText:
        def __str__(self) -> str:
            raise RecursionError("maximum recursion depth exceeded while getting the str of an object")

    assert thought_item(1, 0, {"event": "thought", "text": NoText(), "model": NoText()})["text"] == ""  # (no text to be had)
    # what the log itself writes is never cut: a field is at most ``max_field_chars`` plus its note
    trace = TraceLog(tmp_path, "s")
    trace.write("thought", "a1", gen=1, round=0, text="é" * 80_000, model="m")
    trace.close()
    (kept,) = read_thoughts(trace.directory, "a1")["items"]
    assert kept["text"].startswith("é" * 50_000) and kept["text"].endswith("字]") and len(kept["text"]) < MAX_TEXT_CHARS


def test_pages_over_thoughts_much_longer_than_a_block_cover_everything_once(tmp_path):
    trace = TraceLog(tmp_path, "long", max_bytes=200_000)
    for n in range(12):
        trace.write("thought", "a1", gen=n, round=0, text=f"{n}:" + "多" * (5000 + 777 * n) + f":{n}", gt=float(n))
        trace.write("tool_call", "a1", gen=n, name="speak", args={"pad": "p" * 3000})
    trace.close()
    assert len(trace.files()) > 1
    pages: list[dict[str, Any]] = []
    before = None
    while True:
        page = read_thoughts(trace.directory, "a1", before=before, limit=2, block_size=512)
        pages.append(page)
        if not page["has_more"]:
            break
        before = page["next_before"]
    gathered = [item for page in reversed(pages) for item in page["items"]]
    assert [item["gen"] for item in gathered] == list(range(12))
    assert all(item["text"].startswith(f"{item['gen']}:") and item["text"].endswith(f":{item['gen']}") for item in gathered)
    assert [len(p["items"]) for p in pages] == [2] * 6


def test_stopping_early_lets_go_of_the_trace_files_at_once_so_the_folder_can_be_moved(tmp_path, monkeypatch):
    trace = TraceLog(tmp_path, "s", max_bytes=2000)
    for n in range(40):
        trace.write("thought", "a1", gen=n, round=0, text=f"t{n}" + "x" * 50)
    trace.close()
    assert len(trace.files()) > 3
    handles: list[Any] = []
    kept: list[Any] = []  # (the test keeps every generator alive: only an explicit close lets go of a file then)
    real_open, real_lines, real_scan = open, trace_module.iter_lines_backwards, trace_module.scan_events_backwards

    def tracking_open(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(path, mode, *args, **kwargs)
        handles.append(handle)
        return handle

    def keeping_lines(*args: Any, **kwargs: Any) -> Any:
        kept.append(real_lines(*args, **kwargs))
        return kept[-1]

    def keeping_scan(*args: Any, **kwargs: Any) -> Any:
        kept.append(real_scan(*args, **kwargs))
        return kept[-1]

    monkeypatch.setattr(trace_module, "open", tracking_open, raising=False)
    monkeypatch.setattr(trace_module, "iter_lines_backwards", keeping_lines)
    monkeypatch.setattr(trace_module, "scan_events_backwards", keeping_scan)
    page = read_thoughts(trace.directory, "a1", limit=3)  # one page, and a lot of the trace is left unread
    assert len(page["items"]) == 3 and page["has_more"] is True
    assert handles and all(handle.closed for handle in handles)
    # a budget that ends the scan lets go as well
    opened = len(handles)
    stats = ScanStats()
    list(trace_module.scan_events_backwards(trace.directory, ["thought"], max_bytes=300, stats=stats))
    assert stats.truncated is True and len(handles) > opened and all(handle.closed for handle in handles)
    moved = tmp_path / "moved"
    os.replace(trace.directory, moved)  # (on Windows an open file would make this fail)
    assert len(list(moved.glob("trace-*.jsonl"))) > 3
