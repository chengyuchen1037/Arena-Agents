"""``bench.summary``: the R / S1 / S3 / score numbers of a ``results.jsonl``, on hand-made rows."""

from __future__ import annotations

from typing import Any

from bench.summary import bot_summary, format_summary, rewrite_stats, summarize


def row(bot: str, situation: str = "s1", seat: str = "N3", sample: int = 0, *, text: str | None = None, attempts: list[dict[str, Any]] | None = None,
        status: str = "ok", tool: str = "speak", value: str | None = None, correct: bool | None = None, band: list[int] | None = None, attempt: int = 0,
        verbatim: bool | None = True, calls: int = 1) -> dict[str, Any]:
    decision = None
    if status == "ok":
        decision = {"tool": tool, "text": text, "value": value, "channel_id": "public", "attempt": attempt}
    scored = {"kind": "choice", "correct": correct} if correct is not None else None
    return {
        "bot": bot, "situation": situation, "seat": seat, "sample": sample, "status": status, "decision": decision, "name": bot.split(":")[-1] or "x",
        "attempts": attempts if attempts is not None else ([{"tool": tool, "ok": True, "code": None}] if status == "ok" else []),
        "refusals": {a["code"]: 1 for a in (attempts or []) if not a["ok"] and a.get("code")},
        "speech": {"band": band, "chars": len(text or "")} if band else None, "score": scored, "correct": correct,
        "integrity": {"speech_verbatim": verbatim if tool == "speak" and status == "ok" else None},
        "usage": {"llm_calls": calls, "prompt": 100 * calls, "completion": 10 * calls}, "elapsed_ms": 40,
    }


def refused(code: str) -> dict[str, Any]:
    return {"tool": "speak", "ok": False, "code": code}


OK = {"tool": "speak", "ok": True, "code": None}


def test_rewrite_stats_count_the_retries_that_went_through() -> None:
    rows = [
        row("a", attempts=[refused("FORMAT"), OK]),  # refused, said again, went through
        row("a", sample=1, attempts=[refused("TOO_LONG"), refused("TOO_LONG")], status="no_decision"),  # refused twice and gave up after the second: one retry, not recovered
        row("a", sample=2, attempts=[refused("FORMAT")], status="incomplete"),  # refused and never said again (the budget ended): not a retry
        row("a", sample=3, attempts=[OK]),  # fine at once
    ]
    assert rewrite_stats(rows) == {"refused": 4, "retried": 2, "recovered": 1, "rewrite_success_rate": 0.5}
    assert rewrite_stats([row("a")]) == {"refused": 0, "retried": 0, "recovered": 0, "rewrite_success_rate": None}
    assert rewrite_stats([row("a", attempts=[{"tool": "respond", "ok": False, "code": "REQUEST_CLOSED"}])])["refused"] == 0  # (only a speech is rewritten)


def test_one_bots_numbers() -> None:
    rows = [
        row("bot:A", text="我先听听。", attempt=0, band=[5, 20]),
        row("bot:A", sample=1, text="1. 一\n2. 二", attempt=0, band=[5, 20]),
        row("bot:A", sample=2, text="好的，我说完整的话，不分条，也不用任何编号，就这样说完。", attempt=1, attempts=[refused("FORMAT"), OK], band=[5, 20]),  # (over the band)
        row("bot:A", sample=3, status="no_decision"),
        row("bot:A", situation="s2", tool="respond", value="N1", correct=True),
        row("bot:A", situation="s2", sample=1, tool="respond", value="N4", correct=False),
    ]
    out = bot_summary(rows)
    assert out["samples"] == 6 and out["status"] == {"ok": 5, "no_decision": 1} and out["decision_rate"] == 0.8333
    assert out["first_try_rate"] == 0.8 and out["refusals"] == {"FORMAT": 1} and out["rewrite"]["rewrite_success_rate"] == 1.0
    assert out["score"] == {"n": 2, "correct": 1, "accuracy": 0.5} and out["speech_verbatim"] is True
    assert out["form"]["n"] == 3 and out["form"]["list_rate"] == 0.3333 and out["form"]["in_band_rate"] == 0.6667  # (the three speeches; the votes have no text)
    assert out["usage"] == {"llm_calls": 6, "prompt": 600, "completion": 60} and out["elapsed_ms_mean"] == 40


def test_a_speech_that_did_not_reach_the_world_as_written_is_flagged() -> None:
    assert bot_summary([row("bot:A", text="好", verbatim=False)])["speech_verbatim"] is False
    assert bot_summary([row("bot:A", tool="respond", value="N1")])["speech_verbatim"] is None  # (nothing to check without a speech)
    assert summarize([row("bot:A", text="好", verbatim=False), row("bot:B", text="好")])["overall"]["speech_verbatim"] is False


def test_the_whole_run_has_the_homogeneity_between_bots_and_the_stock_phrases() -> None:
    shared = "这一票必须归票给他因为他的发言不可验证"
    rows = [row(f"bot:{n}", text=shared + str(i)) for i, n in enumerate("ABCD")] + [row("bot:E", text="完全不同的一句话，说说我自己的想法。")]
    summary = summarize(rows)
    overall = summary["overall"]
    assert summary["format"] == "arena-bench-summary" and set(summary["bots"]) == {f"bot:{n}" for n in "ABCDE"}
    assert overall["rows"] == 5 and overall["bots"] == 5 and overall["situations"] == 1 and overall["homogeneity"]["shared_occurrence_share"] > 0.5
    assert overall["cliches"]["hits_per_speech"] > 0.5  # ("归票", "可验证")
    plain = summarize([row("bot:A", text="我觉得今天先听听大家的。"), row("bot:B", text="轮到我了，我想问问二号。")])
    assert plain["overall"]["homogeneity"]["shared_occurrence_share"] == 0.0 and plain["overall"]["cliches"]["hits_per_speech"] == 0.0


def test_the_table_for_the_terminal() -> None:
    summary = summarize([row("legacy", text="1. 一\n2. 二"), row("bot:A", text="我先听听大家怎么说。", attempts=[refused("FORMAT"), OK], attempt=1, band=[5, 30])])
    text = format_summary(summary)
    lines = text.splitlines()
    assert lines[0] == "共 2 个样本，2 个 bot，1 道题；状态：{'ok': 2}" and lines[1].startswith("bot | 样本 | 有决定 | 一次过")
    assert lines[2].startswith("legacy | 1 | 100% | 100% | 0/0 | — | 100%/0%/0% | — |")
    assert lines[3].startswith("bot:A | 1 | 100% | 0% | 1/0 | 100% | 0%/0%/0% | 100% |")
    assert lines[-1].startswith("跨 bot 共用的 6 字片段占比：")


def test_an_empty_run_has_empty_numbers_not_errors() -> None:
    assert summarize([])["bots"] == {} and summarize([])["overall"]["rows"] == 0
    only_sleepers = bot_summary([row("bot:A", status="no_decision"), row("bot:A", sample=1, status="incomplete")])
    assert only_sleepers["decision_rate"] == 0.0 and only_sleepers["first_try_rate"] is None and only_sleepers["form"]["n"] == 0 and only_sleepers["score"]["accuracy"] is None
