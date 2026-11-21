"""Calibrating the judges against people (docs/v0.14-plan.md H48).

The judges of the benchmark are DSH sub-agents (``bench.judging``).  A rubric's verdicts only count for the acceptance report when they agree with a
person's: Cohen's kappa on the rubric's primary field must reach 0.6 (``bench.stats.KAPPA_THRESHOLD``) over enough items.  Two commands
(``python -m bench ...``) and one page make that possible - nothing here calls a model:

* ``calibration-set``  ``judge_items.jsonl`` [+ ``judgments.jsonl``] -> ``set.jsonl``: for every rubric a *stratified* and *blinded* sample of the items
  for a person to label.  A row holds what a judge's prompt holds - the rubric's title and schema, the blind view of the item (``bench.judging.blind_view_of``:
  no bot, no run, no sample, no situation, no truth) - and never a verdict of a judge.  The order is shuffled (fixed seed).
* ``bench/label/index.html`` (the labelling page, offline): the person fills in the same schema a judge fills in; the page exports ``labels.jsonl``.
* ``calibrate``  ``labels.jsonl`` + ``judgments.jsonl`` -> ``calibration.json`` and a table: the labels are paired with the verdicts by ``blind_id``; per
  rubric the number of pairs, kappa of the primary field (with its bootstrap interval and the confusion matrix), the agreement of the other fields, and
  whether the rubric ``counts_for_acceptance`` (kappa >= threshold and n >= min_n).  ``bench.report`` reads that file.

The formulas are the ones of ``bench.stats`` (``cohens_kappa`` / ``kappa_ci`` / ``confusion`` / ``agreement``): nothing is computed twice.  ``user guide``:
``bench/judges/calibration/README.md``.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bench.judging import (
    JUDGES_DIR,
    JUDGMENT_FORMAT,
    JudgingError,
    Rubric,
    RubricError,
    blind_view_of,
    load_rubrics,
    read_jsonl,
    validate_verdict,
)
from bench.stats import DEFAULT_SEED, KAPPA_THRESHOLD, agreement, cohens_kappa, confusion, derive_seed, kappa_ci

LABEL_ITEM_FORMAT = "arena-bench-label-item"
LABEL_FORMAT = "arena-bench-label"
CALIBRATION_FORMAT = "arena-bench-calibration"
#: How many items per rubric a calibration set draws, and how many pairs a rubric needs before its kappa counts (the plan: "30 条起", at least 20 paired).
DEFAULT_N = 30
MIN_N = 20
THRESHOLD = KAPPA_THRESHOLD
#: Words shorter than this are not looked for in a blind view (a run called "t" would be everywhere).
LEAK_MIN_CHARS = 4
CONFUSION_AXES = "行 = 人工标注，列 = 裁判"


class CalibrationError(ValueError):
    """Items, labels or judgments that cannot be calibrated; the message says what to fix."""


# ---------------------------------------------------------------------------------------------------------------------- small helpers
def category_of(value: Any, *, kind: str = "") -> str:
    """A value of a verdict / label as the category kappa compares: strings are trimmed (a ``match`` rubric also upper-cases: "c" is card C), booleans are
    "true" / "false", anything else is its JSON."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        text = value.strip()
        return text.upper() if kind == "match" else text
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _round(value: Any, digits: int = 4) -> float | None:
    """``value`` rounded for the report; ``None`` for nan / inf / not a number (the file must be valid JSON: no NaN)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else round(number, digits)


def _fmt(value: float | None) -> str:
    """A number for a sentence: up to 4 decimals, no trailing zeros (0.72, 1, -0.3333); "—" for none."""
    if value is None:
        return "—"
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _print(text: str = "") -> None:
    print(text)


def _write_lines(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    gap = " " * max(0, width - _width(text))
    return gap + text if right else text + gap


def guide_of(rubric: Rubric) -> str:
    """What a person needs of the rubric text: how to judge (up to the worked examples), without the title line (the page shows it anyway)."""
    body = rubric.body
    cut = re.search(r"^## 示例", body, re.M)
    text = body[: cut.start()] if cut else body
    return re.sub(r"\A# [^\n]*\n+", "", text.strip()).strip()


def label_schema(rubric: Rubric, view: Mapping[str, Any]) -> dict[str, Any]:
    """The schema a person fills in for ONE item: the rubric's own, with what the item itself fixes - an S2 item has a few style cards, so ``match`` is one of their
    ids and ``runner_up`` one of them or ``none`` (the page then offers buttons instead of a text box).  A judge's verdict is checked against the rubric's schema
    (which is wider); every label of this schema fits that too."""
    schema = copy.deepcopy(rubric.schema)
    context = view.get("context") if isinstance(view.get("context"), Mapping) else {}
    cards = context.get("cards") if isinstance(context, Mapping) else None
    if rubric.kind != "match" or not isinstance(cards, list):
        return schema
    ids = [str(card["id"]) for card in cards if isinstance(card, Mapping) and card.get("id") is not None]
    properties = schema.get("properties") or {}
    if ids and isinstance(properties.get(rubric.primary), dict) and "enum" not in properties[rubric.primary]:
        properties[rubric.primary]["enum"] = ids
    if ids and isinstance(properties.get("runner_up"), dict) and "enum" not in properties["runner_up"]:
        properties["runner_up"]["enum"] = [*ids, "none"]
    return schema


# ---------------------------------------------------------------------------------------------------------------------- the sample
def _interleave(groups: Sequence[Sequence[Any]]) -> list[Any]:
    """One of each group in turn (the first of every group, then the second of every group, ...): a group that runs out drops out, the others go on."""
    out: list[Any] = []
    for rank in range(max((len(group) for group in groups), default=0)):
        out.extend(group[rank] for group in groups if rank < len(group))
    return out


def draw_order(items: Sequence[Any], keys: Sequence[Callable[[Any], str]], rng: random.Random) -> list[Any]:
    """The items in the order they are drawn: stratified by ``keys[0]``, inside a stratum by ``keys[1]``, and so on; the strata (and the items of the last
    level) in an order of their own for ``rng``.  Taking the first ``n`` gives every stratum the same number of items (or all it has, when it has fewer: the
    others make up for it), and inside a stratum the same number of every sub-stratum."""
    if not keys:
        order = list(items)
        rng.shuffle(order)
        return order
    groups: dict[str, list[Any]] = {}
    for item in items:
        groups.setdefault(keys[0](item), []).append(item)
    names = sorted(groups)
    rng.shuffle(names)
    return _interleave([draw_order(groups[name], keys[1:], rng) for name in names])


def _shared_words(items: Sequence[Mapping[str, Any]]) -> list[str]:
    """What must not be in a blind view and is the same for many items: the bots, the brains, the runs, the situations."""
    words: set[str] = set()
    for item in items:
        for key in ("bot", "bot_label", "brain", "run", "situation"):
            value = item.get(key)
            if isinstance(value, str) and len(value) >= LEAK_MIN_CHARS:
                words.add(value)
    return sorted(words)


def _current_verdict(rubric: Rubric, judgment: Mapping[str, Any] | None) -> tuple[str | None, bool]:
    """``(category of the primary field, stale)`` of a judgment row: ``None`` when it is no usable verdict of the CURRENT rubric; ``stale`` when it was made
    with another version of the rubric."""
    if not isinstance(judgment, Mapping) or judgment.get("rubric") != rubric.id:
        return None, False
    if judgment.get("rubric_version") != rubric.version:
        return None, True
    verdict = judgment.get("verdict")
    if validate_verdict(rubric.schema, verdict) or rubric.primary not in verdict:
        return None, False
    return category_of(verdict[rubric.primary], kind=rubric.kind), False


@dataclass
class CalibrationSet:
    """The result of :func:`build_set`: the rows of ``set.jsonl``, the hints for the user (short pools, dropped items ...) and a summary per rubric."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    summary: dict[str, dict[str, Any]] = field(default_factory=dict)


def build_set(
    items: Sequence[Mapping[str, Any]],
    judgments: Sequence[Mapping[str, Any]] | None = None,
    *,
    n: int = DEFAULT_N,
    rubrics: Mapping[str, Rubric] | None = None,
    only: Sequence[str] = (),
    seed: int | str = 1,
) -> CalibrationSet:
    """A blinded, stratified sample of ``n`` items per rubric for a person to label.

    * With ``judgments``: only items a judge has judged (with the CURRENT version of the rubric) can be drawn - an item without a verdict can never be paired -
      and the strata are the judge's values of the rubric's primary field, so every value the judge gives is labelled by the person too; inside a value the
      bots and the situations take turns.  Without: the strata are the bots, and inside a bot the situations.
    * The same ``seed`` and the same items give the same set, whatever the order of the file; a rubric's sample does not change when another rubric is added.
    * An item whose blind view names a bot, a run or a situation is dropped (a person must not learn who spoke).
    * Fewer than ``n`` items: all of them, and a note says so.
    """
    if n < 1:
        raise CalibrationError("--n 要 ≥ 1")
    known = dict(rubrics if rubrics is not None else load_rubrics())
    wanted = [r.strip() for r in only if r and r.strip()]
    unknown = [r for r in wanted if r not in known]
    if unknown:
        raise CalibrationError(f"没有这些 rubric 的评分标准（bench/judges/<名字>.md）：{', '.join(unknown)}")
    by_rubric: dict[str, list[Mapping[str, Any]]] = {}
    seen: set[str] = set()
    for item in items:
        blind_id, rubric_id = item.get("blind_id"), item.get("rubric")
        if not isinstance(blind_id, str) or not blind_id:
            raise CalibrationError(f"有一条裁判条目没有 blind_id：{str(item)[:120]}")
        if rubric_id not in known:
            raise CalibrationError(f"裁判条目 {blind_id} 的 rubric {rubric_id!r} 没有对应的 bench/judges/{rubric_id}.md")
        if (wanted and rubric_id not in wanted) or blind_id in seen:
            continue
        seen.add(blind_id)
        by_rubric.setdefault(rubric_id, []).append(item)

    judged: dict[str, Mapping[str, Any]] = {}
    for row in judgments or []:
        if isinstance(row, Mapping) and row.get("blind_id") is not None and row.get("format", JUDGMENT_FORMAT) == JUDGMENT_FORMAT:
            judged[str(row["blind_id"])] = row  # (a later judgment of the same item replaces the earlier)

    shared = _shared_words(items)
    result = CalibrationSet()
    for rubric_id in sorted(by_rubric):
        rubric = known[rubric_id]
        pool = sorted(by_rubric[rubric_id], key=lambda it: str(it["blind_id"]))
        category: dict[str, str] = {}
        if judgments is not None:
            usable: list[Mapping[str, Any]] = []
            unjudged = stale = 0
            for item in pool:
                value, old = _current_verdict(rubric, judged.get(str(item["blind_id"])))
                if value is None:
                    stale += 1 if old else 0
                    unjudged += 0 if old else 1
                    continue
                category[str(item["blind_id"])] = value
                usable.append(item)
            if unjudged:
                result.notes.append(f"{rubric_id}：{unjudged} 条没有裁判的判定，没有放进抽样（没有判定的条目配不成对）")
            if stale:
                result.notes.append(f"{rubric_id}：{stale} 条的判定不是现行 rubric 版本（v{rubric.version}）做的，没有放进抽样：请重新裁判")
            if not usable:
                result.notes.append(f"{rubric_id}：没有一条有现行版本的判定，没有抽样（先跑裁判；或者不给 --judgments，就按 bot × 题抽样）")
                continue
            pool = usable
        views = {str(item["blind_id"]): blind_view_of(item, rubric) for item in pool}
        clean: list[Mapping[str, Any]] = []
        leaked = 0
        for item in pool:
            blob = json.dumps(views[str(item["blind_id"])], ensure_ascii=False)
            own = item.get("item_id")
            hit = any(word in blob for word in shared) or (isinstance(own, str) and len(own) >= LEAK_MIN_CHARS and own in blob)
            leaked += 1 if hit else 0
            if not hit:
                clean.append(item)
        if leaked:
            result.notes.append(f"{rubric_id}：{leaked} 条的盲化内容里出现了 bot / 运行 / 题目的名字，没有放进抽样（检查题目的 title / brief）")
        if not clean:
            result.notes.append(f"{rubric_id}：没有可抽的条目")
            continue
        bot_key = lambda it: str(it.get("bot") or "")  # noqa: E731
        situation_key = lambda it: str(it.get("situation") or "")  # noqa: E731
        if judgments is not None:
            keys: list[Callable[[Any], str]] = [lambda it: category[str(it["blind_id"])], bot_key, situation_key]
        else:
            keys = [bot_key, situation_key]
        picked = draw_order(clean, keys, random.Random(f"{seed}/{rubric_id}"))[:n]
        strata = Counter(keys[0](item) for item in picked)
        result.summary[rubric_id] = {
            "version": rubric.version,
            "pool": len(clean),
            "picked": len(picked),
            "stratified_by": "裁判的取值" if judgments is not None else "bot",
            "strata": dict(sorted(strata.items())),
        }
        if len(picked) < n:
            result.notes.append(
                f"{rubric_id}：只有 {len(clean)} 条可抽，不够 {n} 条：全取了（再多跑一些样本后重新抽；校准每个 rubric 至少要 {MIN_N} 对，建议 30 条起）"
            )
        for item in picked:
            view = views[str(item["blind_id"])]
            result.rows.append(
                {
                    "format": LABEL_ITEM_FORMAT,
                    "version": 1,
                    "blind_id": item["blind_id"],
                    "rubric": rubric.id,
                    "rubric_version": rubric.version,
                    "title": rubric.title,
                    "kind": rubric.kind,
                    "primary": rubric.primary,
                    "schema": label_schema(rubric, view),
                    "guide": guide_of(rubric),
                    "view": view,
                }
            )
    random.Random(f"{seed}/order").shuffle(result.rows)  # (the order must not tell which stratum an item is from)
    return result


# ---------------------------------------------------------------------------------------------------------------------- the calibration
def _latest(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Mapping[str, Any]], int]:
    """``({blind_id: row}, how many rows were replaced)``: of several rows for one item the one with the latest ``ts`` stays (a tie: the later line)."""
    kept: dict[str, Mapping[str, Any]] = {}
    replaced = 0
    for row in rows:
        blind_id = str(row.get("blind_id"))
        old = kept.get(blind_id)
        if old is not None:
            replaced += 1
            if str(row.get("ts") or "") < str(old.get("ts") or ""):
                continue
        kept[blind_id] = row
    return kept, replaced


def _field_stats(rubric: Rubric, pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Agreement of the boolean and enum fields other than the primary one (a free text cannot be compared)."""
    out: dict[str, dict[str, Any]] = {}
    for name, sub in (rubric.schema.get("properties") or {}).items():
        if name == rubric.primary or not isinstance(sub, Mapping):
            continue
        kind = "boolean" if sub.get("type") == "boolean" else "enum" if "enum" in sub else None
        if kind is None:
            continue
        a = [category_of(label[name]) for label, verdict in pairs if name in label and name in verdict]
        b = [category_of(verdict[name]) for label, verdict in pairs if name in label and name in verdict]
        out[name] = {
            "type": kind,
            "n": len(a),
            "agreement": _round(agreement(a, b)) if a else None,
            "kappa": _round(cohens_kappa(a, b)) if a else None,
        }
    return out


def _reason(n: int, kappa: float | None, min_n: int, threshold: float) -> str:
    if n == 0:
        return "没有配上的样本（n = 0）"
    problems = []
    if n < min_n:
        problems.append(f"n 太少（{n} < {min_n}）")
    if kappa is None:
        problems.append("κ 无定义（人工和裁判都只用了一种取值，这批样本分不出好裁判和固执的裁判）")
    elif kappa < threshold:
        problems.append(f"κ 太低（{_fmt(kappa)} < {_fmt(threshold)}）")
    return "；".join(problems) if problems else f"通过（κ {_fmt(kappa)} ≥ {_fmt(threshold)}，n {n} ≥ {min_n}）"


def calibrate_rubric(
    rubric: Rubric,
    labels: Mapping[str, Mapping[str, Any]],
    judgments: Mapping[str, Mapping[str, Any]],
    *,
    min_n: int = MIN_N,
    threshold: float = THRESHOLD,
    duplicate_labels: int = 0,
) -> tuple[dict[str, Any], list[str]]:
    """The entry of one rubric in ``calibration.json`` and the problems found on the way.  ``labels`` / ``judgments``: the (latest) rows of this rubric by
    ``blind_id``.  A pair counts only when the person's label and the judge's verdict were made with the same version of the rubric AND that is the current one."""
    problems: list[str] = []
    human: list[str] = []
    judge: list[str] = []
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    unpaired_labels = stale_pairs = invalid_labels = invalid_judgments = 0
    for blind_id, row in labels.items():
        label = row.get("label")
        bad = validate_verdict(rubric.schema, label, "label")
        if bad or rubric.primary not in label:
            invalid_labels += 1
            problems.append(f"{rubric.id} {blind_id}：人工标注不符合 {rubric.id} 的格式，没有计入：" + "；".join(bad or [f"缺主指标 {rubric.primary}"]))
            continue
        judgment = judgments.get(blind_id)
        if judgment is None:
            unpaired_labels += 1
            continue
        if not (row.get("rubric_version") == judgment.get("rubric_version") == rubric.version):
            stale_pairs += 1
            continue
        verdict = judgment.get("verdict")
        bad = validate_verdict(rubric.schema, verdict, "verdict")
        if bad or rubric.primary not in verdict:
            invalid_judgments += 1
            problems.append(f"{rubric.id} {blind_id}：裁判的判定不符合 {rubric.id} 的格式，没有计入：" + "；".join(bad or [f"缺主指标 {rubric.primary}"]))
            continue
        human.append(category_of(label[rubric.primary], kind=rubric.kind))
        judge.append(category_of(verdict[rubric.primary], kind=rubric.kind))
        pairs.append((label, verdict))
    unpaired_judgments = sum(1 for blind_id in judgments if blind_id not in labels)
    if stale_pairs:
        problems.append(f"{rubric.id}：{stale_pairs} 对的 rubric 版本不一致（人工、裁判、现行 v{rubric.version} 三者要相同），没有计入：改了 rubric 之后要重新裁判、重新标")

    n = len(pairs)
    if n:
        point, low, high = kappa_ci(human, judge, seed=derive_seed(DEFAULT_SEED, "calibrate", rubric.id))
        kappa, ci = _round(point), [_round(low), _round(high)]
        observed = _round(agreement(human, judge))
        matrix: dict[str, dict[str, int]] = confusion(human, judge)
    else:
        kappa, ci, observed, matrix = None, [None, None], None, {}
    counts = kappa is not None and kappa >= threshold and n >= min_n
    entry = {
        "rubric_version": rubric.version,
        "n": n,
        "unpaired_labels": unpaired_labels,
        "unpaired_judgments": unpaired_judgments,
        "stale_pairs": stale_pairs,
        "invalid_labels": invalid_labels,
        "invalid_judgments": invalid_judgments,
        "duplicate_labels": duplicate_labels,
        "primary": rubric.primary,
        "kappa": kappa,
        "ci": ci,
        "agreement": observed,
        "confusion": matrix,
        "fields": _field_stats(rubric, pairs) if n else {},
        "counts_for_acceptance": bool(counts),
        "reason": _reason(n, kappa, min_n, threshold),
    }
    return entry, problems


def build_report(
    labels: Sequence[Mapping[str, Any]],
    judgments: Sequence[Mapping[str, Any]],
    *,
    rubrics: Mapping[str, Rubric] | None = None,
    min_n: int = MIN_N,
    threshold: float = THRESHOLD,
) -> dict[str, Any]:
    """``calibration.json``: the labels (rows of ``arena-bench-label``) paired with the judgments (rows of ``arena-bench-judgment``) by ``blind_id``, per rubric.
    Only rubrics somebody labelled are in it; a rubric that is not in it is not calibrated."""
    if min_n < 1:
        raise CalibrationError("--min-n 要 ≥ 1")
    if not -1.0 < threshold <= 1.0:
        raise CalibrationError("--threshold 要在 (-1, 1] 之内")
    known = dict(rubrics if rubrics is not None else load_rubrics())
    problems: list[str] = []
    by_rubric_labels: dict[str, list[Mapping[str, Any]]] = {}
    skipped = 0
    for row in labels:
        if not isinstance(row, Mapping) or row.get("format") != LABEL_FORMAT or not isinstance(row.get("blind_id"), str) or not isinstance(row.get("label"), Mapping):
            skipped += 1
            continue
        by_rubric_labels.setdefault(str(row.get("rubric")), []).append(row)
    if skipped:
        problems.append(f"labels 里有 {skipped} 行不是 {LABEL_FORMAT}（页面导出的 labels.jsonl），已跳过")
    by_rubric_judgments: dict[str, list[Mapping[str, Any]]] = {}
    foreign = 0
    for row in judgments:
        if not isinstance(row, Mapping) or row.get("format") != JUDGMENT_FORMAT or not isinstance(row.get("verdict"), Mapping) or row.get("blind_id") is None:
            foreign += 1
            continue
        by_rubric_judgments.setdefault(str(row.get("rubric")), []).append(row)
    if foreign:
        problems.append(f"judgments 里有 {foreign} 行不是 {JUDGMENT_FORMAT}（judge-merge 写的 judgments.jsonl），已跳过")

    out: dict[str, Any] = {}
    owner: dict[str, str] = {}  # blind_id -> the rubric the judgments say it belongs to
    for rubric_id, rows in by_rubric_judgments.items():
        for row in rows:
            owner[str(row["blind_id"])] = rubric_id
    for rubric_id in sorted(by_rubric_labels):
        rubric = known.get(rubric_id)
        if rubric is None:
            problems.append(f"{rubric_id}：没有这个 rubric 的评分标准（bench/judges/{rubric_id}.md），{len(by_rubric_labels[rubric_id])} 条人工标注没有计入")
            continue
        mine, replaced = _latest(by_rubric_labels[rubric_id])
        theirs, _ = _latest(by_rubric_judgments.get(rubric_id, []))
        for blind_id in list(mine):
            other = owner.get(blind_id)
            if other is not None and other != rubric_id:
                problems.append(f"{rubric_id} {blind_id}：人工标注说它属于 {rubric_id}，裁判的判定说它属于 {other}：没有配对")
                theirs.pop(blind_id, None)
        entry, found = calibrate_rubric(rubric, mine, theirs, min_n=min_n, threshold=threshold, duplicate_labels=replaced)
        out[rubric_id] = entry
        problems += found
    return {
        "format": CALIBRATION_FORMAT,
        "version": 1,
        "threshold": threshold,
        "min_n": min_n,
        "confusion_axes": CONFUSION_AXES,
        "rubrics": out,
        "problems": problems,
    }


def _level(kappa: float | None, threshold: float) -> str:
    """Landis-Koch-ish words for the table: 弱 (< 0.4), 中 (0.4 up to the threshold), 够 (the threshold or more)."""
    if kappa is None:
        return "—"
    return "弱" if kappa < 0.4 else ("中" if kappa < threshold else "够")


def _matrix_lines(matrix: Mapping[str, Mapping[str, int]]) -> list[str]:
    names = list(matrix)
    first = max((_width(name) for name in names), default=0)
    widths = [max(_width(name), 2) for name in names]
    lines = [" " * (first + 2) + "  ".join(_pad(name, width, right=True) for name, width in zip(names, widths))]
    for row in names:
        lines.append(_pad(row, first) + "  " + "  ".join(_pad(str(matrix[row].get(col, 0)), width, right=True) for col, width in zip(names, widths)))
    return lines


def format_report(report: Mapping[str, Any]) -> str:
    """The Chinese table of the terminal (and, below it, the confusion matrix of every rubric that does not count)."""
    threshold, min_n = report["threshold"], report["min_n"]
    columns = ["rubric", "版本", "配对 n", "κ", "95% 区间", "一致率", "解读", "计入验收", "说明"]
    rows: list[list[str]] = []
    for rubric_id, entry in report["rubrics"].items():
        low, high = entry["ci"]
        interval = "—" if low is None or high is None else f"[{_fmt(low)}, {_fmt(high)}]"
        percent = "—" if entry["agreement"] is None else f"{entry['agreement'] * 100:.0f}%"
        rows.append(
            [rubric_id, f"v{entry['rubric_version']}", str(entry["n"]), _fmt(entry["kappa"]), interval, percent, _level(entry["kappa"], threshold),
             "是" if entry["counts_for_acceptance"] else "否", entry["reason"]]
        )
    lines = [f"校准：人工标注 ↔ 裁判（κ ≥ {_fmt(threshold)} 且至少 {min_n} 对才计入验收）"]
    if rows:
        widths = [max(_width(columns[i]), *(_width(row[i]) for row in rows)) for i in range(len(columns) - 1)]
        lines.append("  ".join(_pad(columns[i], widths[i]) for i in range(len(columns) - 1)) + "  " + columns[-1])
        for row in rows:
            lines.append("  ".join(_pad(row[i], widths[i], right=i in (2, 3, 5)) for i in range(len(columns) - 1)) + "  " + row[-1])
    else:
        lines.append("（没有任何 rubric 配上了样本）")
    for rubric_id, entry in report["rubrics"].items():
        extra = []
        for key, text in (("unpaired_labels", "人工标了、裁判没判"), ("unpaired_judgments", "裁判判了、人工没标"), ("stale_pairs", "版本不一致"),
                          ("invalid_labels", "人工标注不合格式"), ("invalid_judgments", "判定不合格式"), ("duplicate_labels", "重复的标注（取最新）")):
            if entry.get(key):
                extra.append(f"{text} {entry[key]}")
        if extra:
            lines.append(f"  {rubric_id}：" + "，".join(extra))
    for rubric_id, entry in report["rubrics"].items():
        if not entry["counts_for_acceptance"] and entry["confusion"]:
            lines += ["", f"{rubric_id} 的混淆矩阵（{report.get('confusion_axes', CONFUSION_AXES)}）："]
            lines += ["  " + line for line in _matrix_lines(entry["confusion"])]
            for name, stats in entry["fields"].items():
                if stats["agreement"] is not None:
                    lines.append(f"  字段 {name}：一致率 {stats['agreement'] * 100:.0f}%（{stats['n']} 对）")
    for problem in report.get("problems", [])[:12]:
        lines.append(f"问题：{problem}")
    if len(report.get("problems", [])) > 12:
        lines.append(f"……还有 {len(report['problems']) - 12} 条问题，见 calibration.json 的 problems")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------------- the command line
def cmd_calibration_set(args: argparse.Namespace) -> int:
    try:
        items = read_jsonl(args.items)
        judgments = [row for path in args.judgments for row in read_jsonl(path)] if args.judgments else None
        rubrics = load_rubrics(args.judges)
        result = build_set(items, judgments, n=args.n, rubrics=rubrics, only=[r for r in (args.rubrics or "").split(",") if r.strip()], seed=args.seed)
    except (CalibrationError, JudgingError, RubricError, OSError) as exc:
        _print(f"抽样失败：{exc}")
        return 1
    for rubric_id, info in result.summary.items():
        spread = " · ".join(f"{name or '（空）'} {count}" for name, count in info["strata"].items())
        _print(f"{rubric_id}  v{info['version']}  取了 {info['picked']} / {info['pool']} 条  分层（{info['stratified_by']}）：{spread}")
    for note in result.notes:
        _print(f"提示：{note}")
    if not result.rows:
        _print("没有抽到任何条目。")
        return 1
    if args.n < MIN_N:
        _print(f"提示：每个 rubric 只抽 {args.n} 条，低于校准默认要的 {MIN_N} 对（calibrate --min-n）：建议 30 条起。")
    _write_lines(args.out, result.rows)
    _print(f"写到 {args.out}（共 {len(result.rows)} 条，顺序已打乱，种子 {args.seed}）。下一步：用浏览器打开 bench/label/index.html，选择这个文件开始标注。")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    try:
        labels = [row for path in args.labels for row in read_jsonl(path)]
        judgments = [row for path in args.judgments for row in read_jsonl(path)]
        rubrics = load_rubrics(args.judges)
        report = build_report(labels, judgments, rubrics=rubrics, min_n=args.min_n, threshold=args.threshold)
        text = json.dumps(report, ensure_ascii=False, indent=1, allow_nan=False) + "\n"
    except (CalibrationError, JudgingError, RubricError, OSError) as exc:
        _print(f"校准失败：{exc}")
        return 1
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    _print(format_report(report))
    if args.out:
        _print(f"\n写到 {args.out}")
    if not any(entry["n"] for entry in report["rubrics"].values()):
        _print("一对样本也没有配上：人工标注和裁判的判定要来自同一批条目（blind_id 相同）、同一个 rubric 版本。")
        return 1
    return 0


def add_commands(sub: Any) -> None:
    make = sub.add_parser("calibration-set", help="judge_items.jsonl (+ judgments.jsonl) -> set.jsonl: a blinded, stratified sample per rubric for a person to label (bench/label/index.html)")
    make.add_argument("--items", required=True, type=Path, help="judge_items.jsonl of a run")
    make.add_argument("--judgments", nargs="+", type=Path, help="judgments.jsonl: with it the sample is stratified by the judge's values of the primary field")
    make.add_argument("--n", type=int, default=DEFAULT_N, help=f"items per rubric (default {DEFAULT_N})")
    make.add_argument("--rubrics", help="only these rubrics (comma separated)")
    make.add_argument("--seed", type=int, default=1, help="the seed of the draw and of the order (default 1)")
    make.add_argument("--out", required=True, type=Path, help="the set file the labelling page opens")
    make.add_argument("--judges", type=Path, default=JUDGES_DIR, help="the directory of the rubric files")
    cal = sub.add_parser("calibrate", help="labels.jsonl + judgments.jsonl -> calibration.json: agreement (Cohen's kappa) of the judges with the labels, per rubric")
    cal.add_argument("--labels", required=True, nargs="+", type=Path, help="labels.jsonl exported by the labelling page")
    cal.add_argument("--judgments", required=True, nargs="+", type=Path, help="judgments.jsonl written by judge-merge")
    cal.add_argument("--min-n", type=int, default=MIN_N, help=f"pairs a rubric needs to count (default {MIN_N})")
    cal.add_argument("--threshold", type=float, default=THRESHOLD, help=f"kappa a rubric needs to count (default {THRESHOLD})")
    cal.add_argument("--out", type=Path, help="calibration.json (read by bench.report)")
    cal.add_argument("--judges", type=Path, default=JUDGES_DIR, help="the directory of the rubric files")


HANDLERS = {"calibration-set": cmd_calibration_set, "calibrate": cmd_calibrate}


def main(argv: Sequence[str] | None = None) -> int:  # (python -m bench.calibrate ... for a quick look; the real entry is python -m bench)
    parser = argparse.ArgumentParser(prog="python -m bench.calibrate")
    sub = parser.add_subparsers(dest="command", required=True)
    add_commands(sub)
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    return HANDLERS[args.command](args)


__all__ = [
    "CALIBRATION_FORMAT",
    "CalibrationError",
    "CalibrationSet",
    "HANDLERS",
    "LABEL_FORMAT",
    "LABEL_ITEM_FORMAT",
    "add_commands",
    "build_report",
    "build_set",
    "calibrate_rubric",
    "category_of",
    "draw_order",
    "format_report",
    "guide_of",
    "label_schema",
]

if __name__ == "__main__":
    sys.exit(main())
