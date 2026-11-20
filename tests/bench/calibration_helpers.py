"""Builders shared by the calibration tests (``test_calibrate.py``, ``test_label_page.py``): judge items, judgments, labels - every one made of the shapes
the real files have (``bench.runner.judge_items``, ``bench.judging.merge_verdicts``, the labelling page's export)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bench.judging import JUDGMENT_FORMAT

VALUES = ["decisive_fact", "trivia", "expression", "intuition", "unclear"]
BOT_NAMES = ["bot:Alice@no-people", "bot:Bob", "legacy"]
TS = "2026-11-20T10:00:00.000Z"


def make_item(n: int, rubric: str = "L3", *, bot: str | None = None, situation: str | None = None, **over: Any) -> dict[str, Any]:
    """One row of ``judge_items.jsonl`` (it names the bot, the run and the situation; ``truth`` is the answer): ``blind_id`` = ``{n:012x}``."""
    item = {
        "format": "arena-judge-item", "version": 1, "blind_id": f"{n:012x}", "item_id": f"20261120-aaaa/sit-{n}/N5/{bot or 'bot:Alice@no-people'}/0/{rubric}",
        "rubric": rubric, "run": "20261120-aaaa", "situation": situation or f"sit-{n % 5}", "seat": "N5", "bot": bot or BOT_NAMES[n % 3], "brain": "BotBrain",
        "sample": 0, "tags": [rubric], "title": "一道题", "brief": "被测座位是 N5。",
        "context": {"decisive": "N4 自称猎人，公告里开枪的猎人是 N2。", "trap": "N4 说得很有条理。"},
        "truth": {"answer": ["N4"], "wrong": ["N6"], "correct": True},
        "subject": {"kind": "choice", "tool": "respond", "value": "N4", "reason": f"N4 自称猎人，公告对不上（{n}）。", "thought": "", "text": None, "attempts": [{"secret": "x"}]},
    }
    item.update(over)
    return item


def make_s2(n: int, cards: tuple[str, ...] = ("A", "B", "C"), **over: Any) -> dict[str, Any]:
    return make_item(
        n, "S2",
        context={"cards": [{"id": c, "text": f"卡 {c}：你说话短，爱反问。\n篇幅：40–110 字"} for c in cards]},
        subject={"kind": "speech", "tool": "speak", "text": f"你急什么？这票你自己信吗？（{n}）", "value": None, "reason": None, "thought": "秘密思考", "attempts": [{"secret": "x"}]},
        truth={"bot": "bot:Alice@no-people", "cards": {c: "bot:Alice@no-people" for c in cards}}, **over,
    )


def make_e2(n: int, **over: Any) -> dict[str, Any]:
    return make_item(
        n, "E2", context={"reference": ["我昨天就说了，N5 不对。", "又在绕。投谁，说一句。"], "situation": "第 2 天，白天发言，没有新的信息。"},
        subject={"kind": "pair", "x": f"我先把今天的情况分三点说明（{n}）", "y": "N5 还是不对。我今天还投他。"}, **over,
    )


def make_m2(n: int, **over: Any) -> dict[str, Any]:
    return make_item(
        n, "M2", context={"question": "第 1 夜被狼人刀死的是谁？", "expected": "N7"},
        subject={"kind": "speech", "tool": "speak", "text": f"第 1 夜出局的是 N7，公告里写了。（{n}）", "value": None, "reason": None, "thought": "", "attempts": [{"secret": "x"}]}, **over,
    )


def verdict_l3(basis: str = "trivia", cites: bool = True, reason: str = "理由") -> dict[str, Any]:
    return {"basis": basis, "cites_decisive": cites, "reason": reason}


def judgment_of(item: dict[str, Any] | str, verdict: dict[str, Any], *, rubric: str = "L3", version: int = 1) -> dict[str, Any]:
    """One row of ``judgments.jsonl`` (what ``judge-merge`` writes)."""
    blind_id = item if isinstance(item, str) else item["blind_id"]
    return {
        "format": JUDGMENT_FORMAT, "version": 1, "blind_id": blind_id, "item_id": f"x/{blind_id}", "rubric": rubric, "rubric_version": version,
        "verdict": verdict, "judge": {"source": "dsh-subagent", "attempts": 1},
    }


def label_row(blind_id: str, label: dict[str, Any], *, rubric: str = "L3", version: int = 1, ts: str = TS) -> dict[str, Any]:
    """One row of ``labels.jsonl`` (what the labelling page exports)."""
    return {"format": "arena-bench-label", "version": 1, "blind_id": blind_id, "rubric": rubric, "rubric_version": version, "label": label, "labeler": "user", "ts": ts}


def from_matrix(counts: dict[tuple[str, str], int], *, rubric: str = "L3", start: int = 1) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Labels and judgments whose confusion matrix (person, judge) is ``counts`` - the primary field is ``basis`` (``cites_decisive`` always agrees)."""
    labels, judgments = [], []
    n = start
    for (human, judge), how_many in counts.items():
        for _ in range(how_many):
            blind_id = f"{n:012x}"
            labels.append(label_row(blind_id, verdict_l3(human), rubric=rubric))
            judgments.append(judgment_of(blind_id, verdict_l3(judge), rubric=rubric))
            n += 1
    return labels, judgments


def judged_items(counts: dict[str, int], *, start: int = 1) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Items and judgments: ``counts`` items per value of ``basis`` (bots and situations take turns)."""
    items, judgments = [], []
    n = start
    for value, how_many in counts.items():
        for _ in range(how_many):
            item = make_item(n)
            items.append(item)
            judgments.append(judgment_of(item, verdict_l3(value)))
            n += 1
    return items, judgments


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8", newline="\n")
    return path


def read_jsonl_file(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
