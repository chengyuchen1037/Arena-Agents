"""Numbers over the rows of a ``results.jsonl``: per bot and for the whole run (suites R, S1, S3 and the objective score of the choice situations).

The rows come from ``bench.runner`` (one per bot × situation × seat × sample).  This module only *reads* them: it calls no model and changes nothing.
The judge-based suites (S2, L1-L3, E2) need ``judge_items.jsonl`` and the verdicts of a judge; their numbers come from ``judgments.jsonl``, not from here.

* **R** (runtime health): how many samples ended with a decision, how many refused speeches (``FORMAT`` / ``TOO_LONG``) the bot rewrote and how many
  of those rewrites went through at the next try, how often it got it right at the first try, model calls and tokens;
* **S1** (form): ``bench.metrics.form_metrics`` over the decision speeches of a bot (lists, markdown, a self-introducing prefix, length against the
  band its voice declares);
* **S3** (homogeneity): the share of 6-grams that several *bots* use in the same situations, and the stock phrases - across the bots of the run;
* **score**: for ``choice`` situations (``score:`` in the script) the share of correct decisions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from bench.loaders import Speech
from bench.metrics import cliche_counts, form_metrics, homogeneity

REWRITE_CODES = ("FORMAT", "TOO_LONG")


def _rate(count: int, total: int) -> float | None:
    return round(count / total, 4) if total else None


def rewrite_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Of the refused speeches (``FORMAT`` / ``TOO_LONG``) that were followed by another try, how many of those tries went through."""
    retried = recovered = refused = 0
    for row in rows:
        attempts = [a for a in row.get("attempts") or [] if a.get("tool") in ("speak", "speak_template")]
        for index, attempt in enumerate(attempts):
            if not attempt.get("ok") and attempt.get("code") in REWRITE_CODES:
                refused += 1
                if index + 1 < len(attempts):
                    retried += 1
                    recovered += bool(attempts[index + 1].get("ok"))
    return {"refused": refused, "retried": retried, "recovered": recovered, "rewrite_success_rate": _rate(recovered, retried)}


def _speeches(rows: Sequence[Mapping[str, Any]], *, by_bot: bool = False) -> list[Speech]:
    out = []
    for index, row in enumerate(rows):
        decision = row.get("decision") or {}
        text = decision.get("text")
        if row.get("status") != "ok" or not text:
            continue
        out.append(Speech(
            seq=index, seat=str(row["bot"] if by_bot else row["seat"]), name=str(row.get("name") or row["seat"]), channel=str(decision.get("channel_id") or ""),
            text=str(text), kind="speech", truncated=0,
        ))
    return out


def bot_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Everything about the rows of ONE bot."""
    status = Counter(str(row.get("status")) for row in rows)
    ok = [row for row in rows if row.get("status") == "ok"]
    bands = {str(r["seat"]): tuple(r["speech"]["band"]) for r in ok if (r.get("speech") or {}).get("band")}
    speeches = _speeches(rows)
    scored = [r for r in rows if r.get("score")]
    refusals: Counter[str] = Counter()
    for row in rows:
        refusals.update(row.get("refusals") or {})
    usage: Counter[str] = Counter()
    for row in rows:
        usage.update({k: v for k, v in (row.get("usage") or {}).items() if isinstance(v, int)})
    first_try = [r for r in ok if (r.get("decision") or {}).get("attempt") == 0]
    verbatim = [r["integrity"]["speech_verbatim"] for r in rows if (r.get("integrity") or {}).get("speech_verbatim") is not None]
    return {
        "samples": len(rows),
        "status": dict(status),
        "decision_rate": _rate(len(ok), len(rows)),
        "first_try_rate": _rate(len(first_try), len(ok)),
        "refusals": dict(refusals),
        "rewrite": rewrite_stats(rows),
        "form": form_metrics(speeches, bands=bands),
        "score": {"n": len(scored), "correct": sum(1 for r in scored if r["score"].get("correct")), "accuracy": _rate(sum(1 for r in scored if r["score"].get("correct")), len(scored))},
        "speech_verbatim": all(verbatim) if verbatim else None,
        "usage": dict(usage),
        "elapsed_ms_mean": round(sum(r.get("elapsed_ms", 0) for r in rows) / len(rows)) if rows else 0,
    }


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per bot (``bots``) and across the run (``overall``: S3 over the bots, the totals)."""
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["bot"])].append(row)
    bots = {label: bot_summary(items) for label, items in grouped.items()}
    by_bot = _speeches(rows, by_bot=True)
    names = sorted({str(r.get("name") or "") for r in rows} - {""})
    seats = max(2, min(4, len(grouped)))
    overall = {
        "rows": len(rows),
        "bots": len(grouped),
        "situations": len({r["situation"] for r in rows}),
        "status": dict(Counter(str(r.get("status")) for r in rows)),
        "homogeneity": homogeneity(by_bot, n=6, min_seats=seats, names=names),
        "cliches": cliche_counts(by_bot),
        "speech_verbatim": all(b["speech_verbatim"] is not False for b in bots.values()),
    }
    return {"format": "arena-bench-summary", "version": 1, "overall": overall, "bots": bots}


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def format_summary(summary: Mapping[str, Any]) -> str:
    """A small table for the terminal (Chinese)."""
    overall = summary["overall"]
    lines = [
        f"共 {overall['rows']} 个样本，{overall['bots']} 个 bot，{overall['situations']} 道题；状态：{overall['status']}",
        "bot | 样本 | 有决定 | 一次过 | 退回 FORMAT/TOO_LONG | 重写成功 | 编号/markdown/自报 | 篇幅内 | 正确率 | 调用 | 发言逐字一致",
    ]
    for label, bot in summary["bots"].items():
        form, refused, rewrite = bot["form"], bot["refusals"], bot["rewrite"]
        lines.append(
            f"{label} | {bot['samples']} | {_pct(bot['decision_rate'])} | {_pct(bot['first_try_rate'])} | "
            f"{refused.get('FORMAT', 0)}/{refused.get('TOO_LONG', 0)} | {_pct(rewrite['rewrite_success_rate'])} | "
            f"{_pct(form['list_rate'])}/{_pct(form['markdown_rate'])}/{_pct(form['self_prefix_rate'])} | {_pct(form['in_band_rate'])} | "
            f"{_pct(bot['score']['accuracy'])} | {bot['usage'].get('llm_calls', 0)} | {bot['speech_verbatim']}"
        )
    homo = overall["homogeneity"]
    lines.append(f"跨 bot 共用的 6 字片段占比：{_pct(homo['shared_occurrence_share'])}；套话每条命中 {overall['cliches']['hits_per_speech']}")
    return "\n".join(lines)


__all__ = ["bot_summary", "format_summary", "rewrite_stats", "summarize"]
