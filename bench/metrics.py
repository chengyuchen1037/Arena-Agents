"""Pure metrics over speeches, ledgers and traces (suites R, S1 and S3 of docs/v0.14-plan.md §6.5).

Every function takes plain data and returns a plain ``dict`` of numbers (JSON-ready); none of them calls a model or reads a file.

* **S1 form** (:func:`form_metrics`): how much a speech reads like a report - list lines, inline enumerations ("1）…2）…", "一，…二，…",
  "首先…其次…"), markdown, a self-introducing prefix, several paragraphs - and how long it is (against the length band a voice declares).
  The line-level rules are the ones of the output gate itself (``app.agents.gates``): what the gate sends back is what this counts.
* **S3 homogeneity** (:func:`homogeneity`, :func:`cliche_counts`): how much of what the seats say is the same text (character n-grams that
  several seats share) and how often the stock phrases of the v0.13.1 games come back.
* **R runtime health** (:func:`runtime_health`): truncated speeches, floors that timed out, refusals by code (``TOO_LONG`` / ``FORMAT`` /
  ``NO_FLOOR`` ...), how often a refused speech was said again and went through, how long a speaking turn took, tokens.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.agents.gates import check_speech
from bench.loaders import Speech, by_seat

# ---------------------------------------------------------------------------------------------------------------- S1: form
_INLINE_ENUM_RE = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9])[1-9][)）.、](?![0-9])"  # 1) 1） 1. 1、   (not "N5、N7", not 2.5)
    r"|(?<![\u4e00-\u9fff])[一二三四五六七八九十][、，,.）)]"  # 一、 二，   (not "统一，")
    r"|第[一二三四五六七八九十][点条，,、：:]"  # 第一点 第二条 第一，
    r"|首先|其次|再次|最后|其一|其二|其三"
    r")"
)
_RANGE_RE = re.compile(r"(\d{1,4})\s*[-–—~～到至]\s*(\d{1,4})")


def inline_enumeration(text: str) -> bool:
    """Two or more *different* enumerators anywhere in the text ("1）… 2）…", "一，… 二，…", "首先… 其次…")."""
    return len({m.group(0) for m in _INLINE_ENUM_RE.finditer(text or "")}) >= 2


def length_band(voice_text: str) -> tuple[int, int] | None:
    """The ``篇幅：40–110 字`` range a voice declares (the first one), or ``None``."""
    for line in (voice_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("篇幅：", "篇幅:")):
            match = _RANGE_RE.search(stripped)
            if match:
                low, high = int(match.group(1)), int(match.group(2))
                return (min(low, high), max(low, high))
    return None


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return float(ordered[index])


def _rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def form_metrics(
    speeches: Iterable[Speech],
    *,
    bands: Mapping[str, tuple[int, int]] | None = None,
    allow_lists: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """The S1 numbers of ``speeches`` (any mix of seats).  ``bands`` maps a seat to its declared length band (characters);
    ``allow_lists`` marks the seats whose voice allows spoken enumerations (the gate's ``list`` rule is then not counted for them)."""
    items = list(speeches)
    total = len(items)
    bands = bands or {}
    allow_lists = allow_lists or {}
    counts: Counter[str] = Counter()
    lengths = [len(s.text.strip()) for s in items]
    in_band = out_band = 0
    for speech in items:
        text = speech.text
        problems = {p.rule for p in check_speech(text, ids=[speech.seat], names=[speech.name], allow_lists=allow_lists.get(speech.seat, False))}
        enum = inline_enumeration(text)
        counts["list"] += "list" in problems
        counts["markdown"] += "heading" in problems
        counts["prefix"] += "prefix" in problems
        counts["inline_enum"] += enum
        counts["report"] += bool(problems) or enum
        counts["paragraphs"] += sum(1 for line in text.splitlines() if line.strip()) >= 2
        counts["truncated"] += speech.truncated > 0
        band = bands.get(speech.seat)
        if band:
            if band[0] <= len(text.strip()) <= band[1]:
                in_band += 1
            else:
                out_band += 1
    return {
        "n": total,
        "chars_mean": round(statistics.fmean(lengths), 1) if lengths else 0.0,
        "chars_median": _quantile(lengths, 0.5),
        "chars_p90": _quantile(lengths, 0.9),
        "chars_max": max(lengths) if lengths else 0,
        "list_rate": _rate(counts["list"], total),
        "inline_enum_rate": _rate(counts["inline_enum"], total),
        "markdown_rate": _rate(counts["markdown"], total),
        "self_prefix_rate": _rate(counts["prefix"], total),
        "multi_paragraph_rate": _rate(counts["paragraphs"], total),
        "report_rate": _rate(counts["report"], total),  # any of the above except "paragraphs"
        "truncated_rate": _rate(counts["truncated"], total),
        "in_band_rate": _rate(in_band, in_band + out_band) if (in_band + out_band) else None,
    }


# --------------------------------------------------------------------------------------------------------- S3: homogeneity
_KEEP_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")
_SEAT_RE = re.compile(r"N\d{1,2}")

#: The stock phrases of the v0.13.1 games (docs/v0.14-plan.md §0.1): the audit of 249 public speeches found them in almost every seat.
DEFAULT_CLICHES = (
    "可验证", "顺位", "对账", "兑现", "记一笔", "我记着", "不接", "这点不让", "别只", "硬证据", "站边", "警徽流",
    "归票", "自证", "打脸", "闭环", "口径", "硬点", "死磕", "交作业",
)


def _normalise(text: str, names: Iterable[str] = ()) -> str:
    cleaned = _SEAT_RE.sub("N", text or "")
    for name in names:
        if name:
            cleaned = cleaned.replace(name, "名")
    return _KEEP_RE.sub("", cleaned)


def homogeneity(
    speeches: Iterable[Speech], *, n: int = 6, min_seats: int = 4, names: Iterable[str] = (), top: int = 10
) -> dict[str, Any]:
    """How much of what the seats say is the same text: the share of all character ``n``-gram occurrences that belong to an n-gram
    used by at least ``min_seats`` different seats (seat ids and ``names`` are blurred first, punctuation is dropped)."""
    names = list(names)
    seats_of: dict[str, set[str]] = defaultdict(set)
    occurrences: Counter[str] = Counter()
    seats = set()
    for seat, group in by_seat(speeches).items():
        seats.add(seat)
        for speech in group:
            text = _normalise(speech.text, names)
            for index in range(len(text) - n + 1):
                gram = text[index:index + n]
                if len(set(gram)) <= 1 or gram.replace("N", "").isdigit():
                    continue
                seats_of[gram].add(seat)
                occurrences[gram] += 1
    total = sum(occurrences.values())
    shared = {gram: len(owners) for gram, owners in seats_of.items() if len(owners) >= min_seats}
    shared_total = sum(occurrences[gram] for gram in shared)
    ranked = sorted(shared, key=lambda gram: (-shared[gram], -occurrences[gram], gram))[:top]
    return {
        "n": n,
        "min_seats": min_seats,
        "seats": len(seats),
        "ngram_occurrences": total,
        "shared_occurrence_share": _rate(shared_total, total),
        "shared_distinct": len(shared),
        "top_shared": [{"ngram": gram, "seats": shared[gram], "count": occurrences[gram]} for gram in ranked],
    }


def cliche_counts(speeches: Iterable[Speech], lexicon: Sequence[str] = DEFAULT_CLICHES) -> dict[str, Any]:
    """For every stock phrase: in how many speeches and from how many seats it appears; plus the mean number of hits per speech."""
    items = list(speeches)
    per_phrase: dict[str, dict[str, int]] = {}
    hits = 0
    for phrase in lexicon:
        owners = {s.seat for s in items if phrase in s.text}
        count = sum(1 for s in items if phrase in s.text)
        hits += count
        per_phrase[phrase] = {"speeches": count, "seats": len(owners)}
    return {
        "speeches": len(items),
        "hits_per_speech": round(hits / len(items), 3) if items else 0.0,
        "phrases": per_phrase,
        "phrases_in_4_or_more_seats": sorted(p for p, v in per_phrase.items() if v["seats"] >= 4),
    }


# ----------------------------------------------------------------------------------------------------- R: runtime health
def _ledger_floor_ends(ledger: Iterable[Mapping[str, Any]]) -> Counter[str]:
    ends: Counter[str] = Counter()
    for entry in ledger:
        if entry.get("type") == "floor.token_ended":
            ends[str((entry.get("data") or {}).get("reason") or "?")] += 1
    return ends


def runtime_health(
    ledger: Iterable[Mapping[str, Any]] = (),
    trace: Iterable[Mapping[str, Any]] = (),
    *,
    speeches: Iterable[Speech] = (),
) -> dict[str, Any]:
    """The R numbers of one game: ``ledger`` (the saved world's entries), ``trace`` (the events of its trace file) and the speeches of
    its players (``bench.loaders.speeches_of``).  Any of the three may be empty: the numbers that need it are then ``None``."""
    result: dict[str, Any] = {}
    items = [s for s in speeches if not s.human]
    cut = [s for s in items if s.truncated > 0]
    result["agent_speeches"] = len(items)
    result["truncated_speeches"] = len(cut)
    result["truncation_rate"] = _rate(len(cut), len(items)) if items else None
    result["dropped_chars_median"] = _quantile([s.truncated for s in cut], 0.5) if cut else 0.0

    ledger = list(ledger)
    ends = _ledger_floor_ends(ledger) if ledger else Counter()
    result["floor_ends"] = dict(ends)
    result["floor_timeout_rate"] = _rate(ends.get("timeout", 0), sum(ends.values())) if ends else None

    rows = list(trace)
    if not rows:
        result.update(tool_calls=None, refusals=None, refusal_rate=None, speak=None, speak_turn_ms=None, tokens=None)
        return result
    calls: Counter[str] = Counter()
    refusals: Counter[str] = Counter()
    receipts = 0
    speak_outcomes: dict[str, list[tuple[bool, str | None]]] = defaultdict(list)
    for row in rows:
        event = row.get("event")
        if event == "tool_call":
            calls[str(row.get("name") or "?")] += 1
        elif event == "receipt":
            receipts += 1
            if not row.get("ok", True):
                refusals[str(row.get("code") or "?")] += 1
            if row.get("name") == "speak":
                speak_outcomes[str(row.get("agent_id") or "?")].append((bool(row.get("ok")), row.get("code")))
    attempts = sum(len(v) for v in speak_outcomes.values())
    accepted = sum(1 for v in speak_outcomes.values() for ok, _code in v if ok)
    retried = recovered = 0
    for outcomes in speak_outcomes.values():
        for index, (ok, code) in enumerate(outcomes):
            if not ok and code in ("TOO_LONG", "FORMAT") and index + 1 < len(outcomes):
                retried += 1
                recovered += bool(outcomes[index + 1][0])
    result["tool_calls"] = dict(calls)
    result["refusals"] = dict(refusals)
    result["refusal_rate"] = _rate(sum(refusals.values()), receipts)
    result["speak"] = {
        "attempts": attempts,
        "accepted": accepted,
        "too_long": refusals.get("TOO_LONG", 0),
        "format": refusals.get("FORMAT", 0),
        "no_floor": refusals.get("NO_FLOOR", 0),
        "rewrite_success_rate": _rate(recovered, retried) if retried else None,
    }
    turn_ms = [float(r["elapsed_ms"]) for r in rows if r.get("event") == "llm_call" and "speak" in (r.get("tool_calls") or []) and r.get("elapsed_ms") is not None]
    result["speak_turn_ms"] = {"n": len(turn_ms), "median": _quantile(turn_ms, 0.5), "p90": _quantile(turn_ms, 0.9), "max": max(turn_ms) if turn_ms else 0.0}
    llm = [r for r in rows if r.get("event") == "llm_call"]
    result["tokens"] = {
        "calls": len(llm),
        "prompt": sum(int(r.get("prompt_tokens") or 0) for r in llm),
        "cache_read": sum(int(r.get("cache_read_tokens") or 0) for r in llm),
        "completion": sum(int(r.get("completion_tokens") or 0) for r in llm),
    }
    return result


__all__ = [
    "DEFAULT_CLICHES",
    "cliche_counts",
    "form_metrics",
    "homogeneity",
    "inline_enumeration",
    "length_band",
    "runtime_health",
]
