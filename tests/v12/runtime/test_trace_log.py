from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.runtime.ports import TraceSink
from app.runtime.trace_log import TraceLog, redact_text


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def all_records(log: TraceLog) -> list[dict]:
    return [rec for path in log.files() for rec in read_lines(path)]


def test_writes_one_json_object_per_line_in_the_session_directory(tmp_path):
    log = TraceLog(tmp_path / "logs", "sess1")
    sink: TraceSink = log
    sink.write("tool_call", "a1", name="speak", args={"content": "你好，世界"}, ok=True)
    sink.write("thought", "moderator", text="想一想")
    log.close()

    files = log.files()
    assert [p.name for p in files] == ["trace-0001.jsonl"]
    assert files[0].parent == tmp_path / "logs" / "sess1"
    raw = files[0].read_text(encoding="utf-8")
    assert "你好，世界" in raw  # plaintext, not \u-escaped (D11)
    first, second = read_lines(files[0])
    assert first["event"] == "tool_call" and first["agent_id"] == "a1" and first["name"] == "speak"
    assert first["args"] == {"content": "你好，世界"} and first["ok"] is True
    assert "ts" in first and first["ts"][:4].isdigit()
    assert second["event"] == "thought" and second["agent_id"] == "moderator"
    assert log.written == 2 and log.dropped == 0


def test_reserved_field_names_cannot_overwrite_the_envelope_of_a_record(tmp_path):
    log = TraceLog(tmp_path, "s")
    log.write("x", "a1", ts="mine", agent_id_note=1)
    (record,) = all_records(log)
    assert record["ts"] != "mine" and record["field_ts"] == "mine"


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdef1234567890",
        "sk-ant-api03-AbCdEf_123456-xyz",
        "sk-proj-1234567890abcdefghij",
    ],
)
def test_sk_style_keys_are_scrubbed(tmp_path, secret):
    log = TraceLog(tmp_path, "s")
    log.write("error", "a1", message=f"request failed for key {secret} (401)")
    raw = log.current_path.read_text(encoding="utf-8")
    assert secret not in raw and "sk-***" in raw


def test_bearer_tokens_and_key_assignments_are_scrubbed():
    text = "Authorization: Bearer abc.def-123_456/xyz== and api_key=\"hunter2hunter2\" then x-api-key: 99887766aa"
    out = redact_text(text)
    assert "abc.def-123" not in out and "hunter2" not in out and "99887766aa" not in out
    assert "Bearer ***" in out and "api_key" in out
    # ordinary text is left alone
    assert redact_text("max_tokens: 2048, risk-management, task-3, ask politely") == (
        "max_tokens: 2048, risk-management, task-3, ask politely"
    )


def test_sensitive_dictionary_keys_are_scrubbed_at_any_depth(tmp_path):
    log = TraceLog(tmp_path, "s")
    log.write("wake", "a1", payload={"config": {"api_key": "plainsecretvalue", "Authorization": "Basic Zm9v"}, "n": 1})
    (record,) = all_records(log)
    assert record["payload"]["config"] == {"api_key": "***", "Authorization": "***"}
    assert record["payload"]["n"] == 1
    assert "plainsecretvalue" not in log.current_path.read_text(encoding="utf-8")


def test_registered_secrets_are_scrubbed_wherever_they_appear(tmp_path):
    log = TraceLog(tmp_path, "s", secrets=["my-provider-key-77"])
    log.add_secret("another/odd\"key")
    log.write("error", "a1", message="boom my-provider-key-77", nested=["x", {"k": 'with another/odd"key inside'}])
    raw = log.current_path.read_text(encoding="utf-8")
    assert "my-provider-key-77" not in raw and "odd" not in raw.replace("***", "")
    (record,) = all_records(log)
    assert record["message"] == "boom ***"
    assert record["nested"][1]["k"] == "with *** inside"


def test_non_json_values_are_converted_not_fatal(tmp_path):
    class Model(BaseModel):
        a: int = 1

    class Weird:
        def __str__(self) -> str:
            return "weird!"

    log = TraceLog(tmp_path, "s")
    log.write("x", "a1", model=Model(), obj=Weird(), data=b"bytes", items={1, 2}, tup=(1, "2"), none=None)
    (record,) = all_records(log)
    assert record["model"] == {"a": 1}
    assert record["obj"] == "weird!" and record["data"] == "bytes"
    assert sorted(record["items"]) == [1, 2] and record["tup"] == [1, "2"] and record["none"] is None


def test_overlong_strings_and_cycles_are_bounded(tmp_path):
    log = TraceLog(tmp_path, "s", max_field_chars=100)
    cyc: dict = {}
    cyc["self"] = cyc
    log.write("x", "a1", big="y" * 5000, cyc=cyc)
    (record,) = all_records(log)
    assert len(record["big"]) < 200 and "已截断" in record["big"]
    assert log.dropped == 0


def test_files_roll_over_at_the_size_limit_without_splitting_lines(tmp_path):
    log = TraceLog(tmp_path, "roll", max_bytes=400)
    for i in range(30):
        log.write("tool_call", "a1", n=i, pad="x" * 40)
    log.close()
    files = log.files()
    assert len(files) > 3
    assert [p.name for p in files] == [f"trace-{i:04d}.jsonl" for i in range(1, len(files) + 1)]
    for path in files:
        assert path.stat().st_size <= 400 + 120  # at most one line over (a single line is never split)
        for record in read_lines(path):  # every line is complete, valid JSON
            assert record["event"] == "tool_call"
    assert [r["n"] for r in all_records(log)] == list(range(30))


def test_reopening_a_session_continues_with_the_newest_file(tmp_path):
    first = TraceLog(tmp_path, "resume", max_bytes=400)
    for i in range(10):
        first.write("x", "a1", n=i, pad="x" * 40)
    first.close()
    count = len(first.files())
    second = TraceLog(tmp_path, "resume", max_bytes=400)
    second.write("x", "a1", n=99, pad="z")
    second.close()
    assert len(second.files()) in (count, count + 1)  # appended to the last file (or rolled if it was full)
    assert all_records(second)[-1]["n"] == 99
    assert [r["n"] for r in all_records(second)][:10] == list(range(10))


def test_a_single_huge_event_is_written_whole_into_its_own_file(tmp_path):
    log = TraceLog(tmp_path, "big", max_bytes=200, max_field_chars=10_000)
    log.write("a", "a1", pad="p" * 50)
    log.write("b", "a1", pad="q" * 1000)
    log.write("c", "a1", pad="r" * 10)
    log.close()
    assert [r["event"] for r in all_records(log)] == ["a", "b", "c"]
    assert len(log.files()) == 3


def test_session_ids_cannot_escape_the_log_directory(tmp_path):
    log = TraceLog(tmp_path / "logs", "../../evil/..\\x")
    log.write("x", "a1")
    assert log.current_path.resolve().is_relative_to((tmp_path / "logs").resolve())


def test_write_never_raises_even_when_the_directory_cannot_be_created(tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x", encoding="utf-8")
    log = TraceLog(blocker, "s")  # <file>/s cannot be created
    log.write("x", "a1", a=1)
    log.write("y", "a1", b=2)
    assert log.dropped == 2 and log.written == 0


def test_close_then_write_reopens_the_file(tmp_path):
    with TraceLog(tmp_path, "s") as log:
        log.write("a", "a1")
    log.write("b", "a1")
    log.close()
    assert [r["event"] for r in all_records(log)] == ["a", "b"]


def test_concurrent_threads_never_interleave_lines(tmp_path):
    log = TraceLog(tmp_path, "threads", max_bytes=2000)

    def work(tag: int) -> None:
        for i in range(60):
            log.write("x", f"a{tag}", i=i, pad="y" * 30)

    threads = [threading.Thread(target=work, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()
    records = all_records(log)  # json.loads would raise on a torn line
    assert len(records) == 240
