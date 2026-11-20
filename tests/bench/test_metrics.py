"""bench.metrics / bench.loaders / bench.__main__ on small hand-made data (no model, no data directory)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bench.__main__ import audit_game, baseline, main
from bench.loaders import Speech, load_game, read_trace, speeches_of
from bench.metrics import cliche_counts, form_metrics, homogeneity, inline_enumeration, length_band, runtime_health


def speech(text: str, seat: str = "N1", name: str = "Alice", **extra: Any) -> Speech:
    return Speech(seq=extra.pop("seq", 1), seat=seat, name=name, channel="public", text=text, **extra)


# ------------------------------------------------------------------------------------------------------- inline enumerations
@pytest.mark.parametrize(
    "text",
    ["1）看票 2）看发言", "一，他没解释。二，他投了票。", "首先他跳了，其次他没验", "第一点，他的票；第二点，他的话", "1. 先 2. 后"],
)
def test_an_enumeration_inside_a_speech_is_found(text: str) -> None:
    assert inline_enumeration(text)


@pytest.mark.parametrize(
    "text",
    ["我觉得 N5、N7 都有问题", "这个口径要统一，所以我不投", "涨了 2.5 倍，不算多", "第2天我就投了他", "最后我想说一句：我投 N4", "他首先跳了，我不信"],
)
def test_ordinary_talk_is_not_an_enumeration(text: str) -> None:
    assert not inline_enumeration(text)


def test_the_length_band_of_a_voice_is_read_from_its_line() -> None:
    assert length_band("你说话短。\n篇幅：40–110 字") == (40, 110)
    assert length_band("篇幅: 110-40") == (40, 110)  # either order, either dash
    assert length_band("没有这一行") is None


# ------------------------------------------------------------------------------------------------------------------- S1 form
def test_form_metrics_count_what_reads_like_a_report() -> None:
    items = [
        speech("我先说结论，票给 N3。\n他昨天那句话前后对不上，我不信。"),
        speech("1. 他没验人\n2. 他票型怪\n所以票 N3"),  # a list
        speech("**结论**：N3 是狼"),  # bold
        speech("N1：我觉得他不对"),  # prefix
        speech("一，他没说清；二，他票型怪。"),  # inline enumeration only
        speech("就这样吧。", seq=6, truncated=12),
    ]
    m = form_metrics(items)
    assert m["n"] == 6
    assert m["list_rate"] == round(1 / 6, 4)
    assert m["markdown_rate"] == round(1 / 6, 4)
    assert m["self_prefix_rate"] == round(1 / 6, 4)
    assert m["inline_enum_rate"] == round(2 / 6, 4)  # the list also has "1." and "2."
    assert m["report_rate"] == round(4 / 6, 4)  # list, bold, prefix, enumeration; the plain two paragraphs are not a "report"
    assert m["multi_paragraph_rate"] == round(2 / 6, 4)
    assert m["truncated_rate"] == round(1 / 6, 4)


def test_a_voice_that_allows_spoken_lists_is_not_counted_for_its_lists() -> None:
    text = "- 他没验\n- 他票怪"
    assert form_metrics([speech(text, seat="N1")])["list_rate"] == 1.0
    assert form_metrics([speech(text, seat="N1")], allow_lists={"N1": True})["list_rate"] == 0.0


def test_the_length_band_is_checked_per_seat() -> None:
    items = [speech("a" * 50, seat="N1"), speech("a" * 200, seat="N1"), speech("a" * 500, seat="N2")]
    m = form_metrics(items, bands={"N1": (40, 110)})
    assert m["in_band_rate"] == 0.5  # the seat without a band is not counted
    assert form_metrics(items)["in_band_rate"] is None
    assert m["chars_max"] == 500 and m["chars_median"] == 200


def test_an_empty_input_gives_zeros_not_errors() -> None:
    m = form_metrics([])
    assert m["n"] == 0 and m["report_rate"] == 0.0 and m["chars_median"] == 0.0


# ------------------------------------------------------------------------------------------------------------ S3 homogeneity
def test_text_that_many_seats_share_is_found_and_distinct_talk_is_not() -> None:
    shared = "这条理由是可以验证的，请先把验人的结果交出来再谈站边"
    same = [speech(shared + f"，我是{n}号", seat=f"N{n}", name=f"P{n}") for n in range(1, 6)]
    assert homogeneity(same)["shared_occurrence_share"] > 0.5
    distinct = [
        speech("昨天夜里很安静，我一直在想谁最先开口", seat="N1"),
        speech("票型出来之后我才发现自己投错了人", seat="N2"),
        speech("你们先吵，我只看谁的手最稳", seat="N3"),
        speech("天亮前后他的态度变了一次又变回来", seat="N4"),
    ]
    assert homogeneity(distinct)["shared_occurrence_share"] == 0.0


def test_seat_ids_and_names_are_blurred_before_comparing() -> None:
    items = [speech(f"N{n}说他不能信，{name}这票我不跟", seat=f"N{n}", name=name) for n, name in enumerate(["甲", "乙", "丙", "丁"], start=1)]
    assert homogeneity(items, names=["甲", "乙", "丙", "丁"])["shared_occurrence_share"] > 0.9


def test_stock_phrases_are_counted_per_speech_and_per_seat() -> None:
    items = [
        speech("这条可验证吗", seat="N1"),
        speech("请对账，先兑现再说", seat="N2"),
        speech("可验证的东西才算", seat="N3"),
        speech("我记一笔，可验证", seat="N4"),
        speech("随便聊聊", seat="N5"),
    ]
    c = cliche_counts(items)
    assert c["phrases"]["可验证"] == {"speeches": 3, "seats": 3}
    assert c["phrases"]["对账"]["speeches"] == 1
    assert c["hits_per_speech"] == round((3 + 1 + 1 + 1) / 5, 3)
    assert cliche_counts(items, lexicon=["随便"])["phrases"]["随便"]["seats"] == 1


# --------------------------------------------------------------------------------------------------------- R runtime health
def receipt(agent: str, ok: bool, code: str | None = None, name: str = "speak") -> dict[str, Any]:
    return {"event": "receipt", "agent_id": agent, "name": name, "ok": ok, "code": code}


def test_runtime_health_counts_refusals_and_whether_a_refused_speech_was_said_again() -> None:
    trace = [
        receipt("N1", False, "TOO_LONG"), receipt("N1", True),  # sent back, said again, went through
        receipt("N2", False, "FORMAT"), receipt("N2", False, "TOO_LONG"), receipt("N2", True),  # two refusals in a row, then through
        receipt("N3", False, "NO_FLOOR"),  # not a rewrite case
        receipt("N4", False, "TOO_LONG"),  # never said again
        receipt("M", True, name="open_floor"),
        {"event": "tool_call", "agent_id": "N1", "name": "speak"},
        {"event": "llm_call", "agent_id": "N1", "tool_calls": ["speak"], "elapsed_ms": 4000, "prompt_tokens": 100, "cache_read_tokens": 900, "completion_tokens": 50},
        {"event": "llm_call", "agent_id": "N2", "tool_calls": ["speak"], "elapsed_ms": 8000, "prompt_tokens": 100, "cache_read_tokens": 0, "completion_tokens": 70},
        {"event": "llm_call", "agent_id": "M", "tool_calls": ["sleep"], "elapsed_ms": 1000, "prompt_tokens": 10, "completion_tokens": 5},
    ]
    ledger = [{"type": "floor.token_ended", "data": {"reason": r}} for r in ("used", "used", "used", "timeout")]
    r = runtime_health(ledger, trace, speeches=[speech("a"), speech("b", truncated=40)])
    assert (r["agent_speeches"], r["truncated_speeches"], r["truncation_rate"]) == (2, 1, 0.5)
    assert r["floor_ends"] == {"used": 3, "timeout": 1} and r["floor_timeout_rate"] == 0.25
    assert r["refusals"] == {"TOO_LONG": 3, "FORMAT": 1, "NO_FLOOR": 1}
    assert r["speak"]["attempts"] == 7 and r["speak"]["accepted"] == 2
    assert (r["speak"]["too_long"], r["speak"]["format"], r["speak"]["no_floor"]) == (3, 1, 1)
    assert r["speak"]["rewrite_success_rate"] == round(2 / 3, 4)  # N1: yes; N2's FORMAT -> refused again: no; N2's TOO_LONG -> yes
    assert r["speak_turn_ms"]["n"] == 2 and r["speak_turn_ms"]["median"] in (4000.0, 8000.0)
    assert r["tokens"] == {"calls": 3, "prompt": 210, "cache_read": 900, "completion": 125}


def test_without_a_trace_the_trace_numbers_are_unknown_not_zero() -> None:
    r = runtime_health([], [], speeches=[speech("a")])
    assert r["refusals"] is None and r["speak"] is None and r["tokens"] is None
    assert r["floor_timeout_rate"] is None and r["truncation_rate"] == 0.0


def test_a_person_who_played_a_seat_is_not_an_agent() -> None:
    r = runtime_health([], [], speeches=[speech("a", human=True, truncated=9), speech("b")])
    assert r["agent_speeches"] == 1 and r["truncated_speeches"] == 0


# ----------------------------------------------------------------------------------------- loaders and the command line
def make_game(extra_speeches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    def message(seq: int, actor: str, text: str, channel: str = "public", **data: Any) -> dict[str, Any]:
        return {"seq": seq, "type": "message", "actor_id": actor, "channel_id": channel, "data": {"content": text, "kind": "speech", **data}}

    ledger = [
        message(1, "moderator", "天亮了"),
        message(2, "N1", "我是好人，这票给 N3"),
        message(3, "N2", "1）他没验 2）他票怪", truncated={"dropped_chars": 12, "kept_chars": 300}),
        message(4, "N3", "狼队里说句话", channel="wolf"),
        message(5, "N1", "人类代打的话，不算 agent"),
        {"seq": 6, "type": "floor.token_ended", "actor_id": "engine", "data": {"reason": "used"}},
        *(extra_speeches or []),
    ]
    return {
        "version": "v0.13.1",
        "participants": [{"id": f"N{i}", "name": n} for i, n in enumerate(["Alice", "Bob", "Carol"], start=1)],
        "human_spans": [{"seat": "N1", "from_seq": 5, "to_seq": None}],
        "world": {"state": {"channels": [{"id": "public", "type": "public"}, {"id": "wolf", "type": "group"}]}, "ledger": ledger},
    }


def test_speeches_of_reads_the_ledger_and_marks_the_person() -> None:
    game = make_game()
    public = speeches_of(game)
    assert [(s.seat, s.human) for s in public] == [("N1", False), ("N2", False), ("N1", True)]
    assert public[1].truncated == 12 and public[0].name == "Alice"
    assert [s.channel for s in speeches_of(game, scope="all")] == ["public", "public", "wolf", "public"]
    assert [s.seat for s in speeches_of(game, scope="wolf")] == ["N3"]


def test_audit_game_counts_agents_only(tmp_path: Path) -> None:
    game = make_game()
    report = audit_game(game, [receipt("N2", False, "TOO_LONG")])
    assert (report["agent_speeches"], report["human_speeches"]) == (2, 1)
    assert report["form"]["n"] == 2 and report["form"]["inline_enum_rate"] == 0.5
    assert report["runtime"]["truncated_speeches"] == 1 and report["runtime"]["refusals"] == {"TOO_LONG": 1}


def test_baseline_pools_the_games_of_a_data_directory_and_skips_silent_ones(tmp_path: Path) -> None:
    data = tmp_path / "data"
    for session, game in (("aaaa", make_game()), ("bbbb", {"participants": [], "world": {"ledger": []}})):
        (data / "games" / session).mkdir(parents=True)
        (data / "games" / session / "game.json").write_text(json.dumps(game, ensure_ascii=False), encoding="utf-8")
    (data / "logs" / "aaaa").mkdir(parents=True)
    (data / "logs" / "aaaa" / "trace-0001.jsonl").write_text(json.dumps(receipt("N1", True)) + "\n{half a line", encoding="utf-8")
    report = baseline([data])
    assert [g["id"] for g in report["games"]] == ["aaaa"]  # the silent game has no agent speech
    assert report["pooled"]["games"] == 1 and report["pooled"]["agent_speeches"] == 2
    assert report["games"][0]["runtime"]["speak"]["accepted"] == 1  # the half-written last line is skipped


def test_the_command_line_writes_a_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "game.json"
    path.write_text(json.dumps(make_game(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "report.json"
    assert main(["audit", "--game", str(path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["agent_speeches"] == 2
    data = tmp_path / "data"
    (data / "games" / "cccc").mkdir(parents=True)
    (data / "games" / "cccc" / "game.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["baseline", "--data-dir", str(data), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["pooled"]["games"] == 1


def test_a_damaged_game_file_is_a_value_error(tmp_path: Path) -> None:
    broken = tmp_path / "g.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_game(broken)
    broken.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_game(broken)


def test_a_trace_is_read_line_by_line_and_a_half_written_line_is_skipped(tmp_path: Path) -> None:
    trace = tmp_path / "trace-0001.jsonl"
    trace.write_text('{"event": "wake"}\n\n{"event": "sleep"}\n{"event": "tool_ca', encoding="utf-8")
    assert [row["event"] for row in read_trace(trace)] == ["wake", "sleep"]
