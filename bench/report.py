"""The acceptance report (docs/v0.14-plan.md §6.5, H48, H51): the numbers of every suite, per bot - each with an interval and a sample size - and the
gates of ``bench/gates.yml`` checked against them.

    python -m bench report --results <run dir>... [--calibration calibration.json] [--gates bench/gates.yml] [--baseline legacy] --out bench/reports/<version>/

**Reads** the run directories of ``python -m bench run`` (``results.jsonl``; ``judge_items.jsonl``; ``judgments.jsonl`` once ``judge-merge`` has made it) and,
when given, ``calibration.json`` (``python -m bench calibrate``: ``{"rubrics": {"L3": {"n", "kappa", "ci", "counts_for_acceptance", ...}}}``).  A sample
(situation, seat, bot, sample number) that is in several run directories counts once - the copy the judges worked on, else the one of the run given last (M1 runs
the same bots on the same questions as the suites it borrows from); a sample of a shipped example, of a failed run or of a situation that has no suite tag
counts for nothing, and the report says how many there were.  Which suite a sample belongs to is read from the tags of its situation (``[L1, ...]``).

**Writes** ``report.md`` (Chinese: the gates, every suite's numbers, the judges), ``summary.json`` (every number), ``gates.json`` (every gate, per bot) and
``raw/`` (a verbatim copy of what the numbers were made of: H51).  An output directory that exists is refused without ``--force``.  Everything is
reproducible: the bootstrap seeds are fixed and the files hold no clock (the date in ``report.md`` is the only thing that changes between two runs).

**Numbers.**  A *rate* is a ratio of sums over observations (one per speech, vote or judgment) that come in clusters (the samples of one question are
not independent of each other): the interval is the two-level bootstrap of :mod:`bench.stats` - questions first, then the samples inside each chosen
question - 95 %, a seed of its own for every number.  When every observation is the same (all right, all wrong) that interval is a single point; the
report then gives the Wilson interval for as many observations as there are questions (conservative) and says so.  ``n`` is what the rate is over
(speeches, votes, judgments), ``题`` the number of questions behind it.

* **R**: ``form.truncated_rate`` = speeches the engine cut / speeches the bot's moves made (the ledger numbers of every row); ``runtime.speak.rewrite_success_rate``
  = refused speeches (FORMAT / TOO_LONG) that the next try got through / refused speeches that were tried again; ``runtime.floor_timeout_rate`` = floors that
  ended by the clock / floors that ended.
* **S1**: per speech, from the checks the runner stored (``row.speech``: the output gate's rules with the bot's own allowances): list, markdown (a heading, a code
  fence, bold), a self-introducing prefix, "report-like" (any of those or an inline enumeration), inside the length band the bot's voice declares.
* **S2**: the judge (rubric S2) is given a speech and the style cards of all bots; top-1 = the card it names is the one of the bot that spoke (chance = 1 / cards).
* **S3**: ``cliches.hits_per_speech`` = stock phrases per speech; ``homogeneity.shared_occurrence_share`` = the share of 6-grams that at least ``min_seats``
  different bots use (a property of the whole group of bots: needs 4 or more to be comparable with the 0.011 of the 12-seat baseline).
* **L1**: a pair of situations has the same facts; in A the wolf talks well and the good player bluntly, in B the other way round (``truth.sides``).  A vote for the
  blunt player = trusting the fluent one.  ``expression_bias`` = P(trust the fluent one) - 0.5, over the pairs whose A and B (same bot, same sample number) were
  both answered with a side taken: a bot that follows the facts votes for the wolf in both and scores 0, one that always trusts the fluent side scores +0.5.
  Abstentions, votes for a third player and missing answers are counted and left out, together with the other half of their pair.  ``accuracy`` is over the
  votes (abstentions left out).  The gate compares the absolute bias.
* **L2 / L3** (accuracy = right votes / questions asked: no answer, an abstention and an error of the bot count as wrong; a failure of the run - an error, a
  spent budget - is left out): the judge (rubric L2 / L3) says what the decision mainly rested on.  ``trivia_hit_rate`` = ``basis == trivia``;
  ``decisive_hit_rate`` = ``cites_decisive`` OR ``basis == decisive_fact`` (the bot names the decisive fact, even if it is not its main reason);
  ``decisive_main_rate`` = ``basis == decisive_fact`` alone.
* **L4 / M2 / M3**: accuracy as above, from ``truth.answer``.
* **M1**: for each ablation of ``bench/suites/M1.yml`` the bot against itself without that layer (``bot@option``) on the layer's own metric, paired by question: the
  difference, its interval, and the one-sided p-value of "the full bot is better"; the gate asks for p < 0.05 on every layer.
* **E1 / E2 / G**: not made by ``run``: "待数据".

**What counts for acceptance.**  A number made from a judge's verdicts counts only when its rubric is calibrated (H48): ``counts_for_acceptance`` in the
calibration file (and κ >= 0.6).  Without verdicts it is "待裁判", without a calibration "待校准", calibrated but below 0.6 "仅供参考".  A suite with
``acceptance: false`` is "仅供参考" too.  Gate states: 通过 / 未通过 (by the point estimate; ⚠ marks an interval that crosses the line; ``--strict`` turns such a
pass into a fail), 待基线 (relative to a baseline that is not in the results), 待校准, 待裁判, 仅供参考, 未跑 (no data), 待数据 (not run by ``run``).  A gate is
checked for every candidate bot and is as good as the worst of them; ``scope: pooled`` gates (S2, S3) are checked on all candidates together.

Gate values are a number or one of ``<k> * baseline``, ``baseline + <x>``, ``baseline - <x>`` - nothing else is read, nothing is evaluated.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import shutil
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from bench import stats
from bench.bots import ABLATIONS
from bench.loaders import Speech
from bench.metrics import DEFAULT_CLICHES, homogeneity
from bench.safety import UnsafeDataDir, refuse_real_data_dir
from bench.suite import GATES_FILE, Suite, SuiteError, load_suites, with_option
from bench.summary import rewrite_stats

SUMMARY_FORMAT = "arena-bench-report-summary"
GATES_FORMAT = "arena-bench-report-gates"
GATES_FILE_FORMAT = "arena-bench-gates"
RESULT_FORMAT = "arena-bench-result"  # (bench.runner.RESULT_FORMAT; a test keeps the two equal)
JUDGMENT_FORMAT = "arena-bench-judgment"  # (bench.judging.JUDGMENT_FORMAT)
FILE_VERSION = 1
RAW_FILES = ("results.jsonl", "judge_items.jsonl", "judgments.jsonl", "run.json")

# The states of a gate (and of a number that goes into one).
PASS, FAIL = "通过", "未通过"
NO_BASELINE, UNCALIBRATED, UNJUDGED, REFERENCE = "待基线", "待校准", "待裁判", "仅供参考"
NOT_RUN, AWAITING = "未跑", "待数据"
STATUSES = (PASS, FAIL, NO_BASELINE, UNCALIBRATED, UNJUDGED, REFERENCE, NOT_RUN, AWAITING)
#: Which state speaks for several (the bots of a gate, the layers of M1): a failure first - nothing else can undo it - then what is missing, a pass last.
_ROLLUP = (FAIL, NOT_RUN, AWAITING, UNJUDGED, UNCALIBRATED, NO_BASELINE, REFERENCE, PASS)

POOLED = "候选合计"
BASE_POOLED = "基线合计"
ABSTAIN_VALUES = frozenset({"abstain", "none", "skip", "pass", ""})
SPECIAL_OPS = ("no_regression_and_one_significant_gain",)
COMPARE_OPS = ("==", "<=", ">=", "<", ">")
GATE_KEYS = {"id", "suite", "metric", "op", "value", "baseline", "status", "note", "abs", "scope"}
HOMOGENEITY_MIN_BOTS = 4  # (fewer bots share fewer seats: the 0.011 of the 12-seat baseline is not comparable)
HOMOGENEITY_RESAMPLES = 100  # (every replicate recounts the n-grams of the whole run: about 50 ms per 300 speeches)
M1_ALPHA = 0.05


class ReportError(ValueError):
    """An input the report cannot use; the message (Chinese) names the file and what is wrong."""


# ---------------------------------------------------------------------------------------------------------------------- bot labels
def split_label(label: str) -> tuple[str, tuple[str, ...]]:
    """``bot:Alice@no-people,history=6000`` -> ``("bot:Alice", ("no-people", "history=6000"))``."""
    base, _, rest = label.partition("@")
    return base, tuple(o.strip() for o in rest.split(",") if o.strip())


def ablations_of(label: str) -> tuple[str, ...]:
    """The layers a bot spec takes away (M1)."""
    return tuple(o for o in split_label(label)[1] if o in ABLATIONS)


def plain_options(label: str) -> tuple[str, ...]:
    """The options of a spec that are not ablations (``history=6000``): bots that differ in them are not one another's baseline."""
    return tuple(sorted(o for o in split_label(label)[1] if o not in ABLATIONS))


def is_baseline_label(label: str, baseline: str) -> bool:
    """``legacy`` is the baseline of ``legacy``, ``legacy:Alice``, ``legacy@history=6000`` ...; a baseline spelled with options (``legacy@history=6000``) is
    only that one.  A bot with a layer taken away is never a baseline."""
    if ablations_of(label):
        return False
    base, want = split_label(label)[0], split_label(baseline)
    if want[1] and plain_options(label) != plain_options(baseline):
        return False
    return base == want[0] or (":" not in want[0] and base.startswith(want[0] + ":"))


def classify(labels: Iterable[str], baseline: str) -> dict[str, list[str]]:
    """``{"baseline": [...], "candidates": [...], "ablated": [...]}`` (each sorted)."""
    out: dict[str, list[str]] = {"baseline": [], "candidates": [], "ablated": []}
    for label in sorted(set(labels)):
        if ablations_of(label):
            out["ablated"].append(label)
        elif is_baseline_label(label, baseline):
            out["baseline"].append(label)
        else:
            out["candidates"].append(label)
    return out


# ---------------------------------------------------------------------------------------------------------------------- gates
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_UNSIGNED = r"(?:\d+(?:\.\d*)?|\.\d+)"
_CONST_RE = re.compile(rf"^{_NUMBER}$")
_SCALE_RE = re.compile(rf"^(?:({_NUMBER})\s*\*\s*baseline|baseline\s*\*\s*({_NUMBER}))$")
_OFFSET_RE = re.compile(rf"^(?:baseline\s*([+-])\s*({_UNSIGNED})|({_NUMBER})\s*\+\s*baseline)$")


@dataclass(frozen=True)
class Threshold:
    """What a gate compares with: a ``const`` (``0.9``), a ``scale`` of the baseline (``0.5 * baseline``) or an ``offset`` from it (``baseline + 0.20``)."""

    kind: str
    amount: float
    text: str = ""

    @property
    def needs_baseline(self) -> bool:
        return self.kind != "const"

    def resolve(self, baseline: float | None) -> float | None:
        """The line to compare with (``None``: it depends on a baseline that is not there)."""
        if self.kind == "const":
            return self.amount
        if baseline is None or math.isnan(baseline):
            return None
        return self.amount * baseline if self.kind == "scale" else baseline + self.amount


def parse_threshold(value: Any, *, where: str = "门槛") -> Threshold:
    """The ``value`` of a gate: a number, ``"<k> * baseline"``, ``"baseline * <k>"``, ``"baseline + <x>"``, ``"<x> + baseline"``, ``"baseline - <x>"`` or
    ``"baseline"``.  Anything else raises :class:`ReportError` (the text is never evaluated)."""
    if isinstance(value, bool) or value is None:
        raise ReportError(f"{where}：value 要写成数字，或 <k> * baseline、baseline + <x>、baseline - <x>（现在是 {value!r}）。")
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ReportError(f"{where}：value 不是有限的数字（{value!r}）。")
        return Threshold("const", float(value), str(value))
    text = str(value).strip()
    if _CONST_RE.match(text):
        return Threshold("const", float(text), text)
    if text == "baseline":
        return Threshold("scale", 1.0, text)
    scale = _SCALE_RE.match(text)
    if scale:
        return Threshold("scale", float(scale.group(1) or scale.group(2)), text)
    offset = _OFFSET_RE.match(text)
    if offset:
        if offset.group(3) is not None:
            return Threshold("offset", float(offset.group(3)), text)
        amount = float(offset.group(2))
        return Threshold("offset", amount if offset.group(1) == "+" else -amount, text)
    raise ReportError(f"{where}：value {text!r} 看不懂。只认：数字、<k> * baseline、baseline + <x>、baseline - <x>。")


@dataclass(frozen=True)
class Gate:
    """One line of ``bench/gates.yml``.  ``abs``: compare the absolute value (of the bot and of the baseline); ``scope``: ``bot`` (every candidate on its own,
    the default) or ``pooled`` (all candidates together)."""

    id: str
    suite: str
    metric: str
    op: str
    threshold: Threshold | None
    baseline: float | None = None
    status: str = ""
    note: str = ""
    abs: bool = False
    scope: str = "bot"


def parse_gates(data: Any, *, source: str = "gates.yml") -> list[Gate]:
    """The gates of a loaded ``gates.yml``.  Raises :class:`ReportError` naming the gate that is wrong."""
    if not isinstance(data, Mapping):
        raise ReportError(f"{source}：顶层要是一个字典。")
    if data.get("format") not in (None, GATES_FILE_FORMAT):
        raise ReportError(f"{source}：format 要是 {GATES_FILE_FORMAT}。")
    raw = data.get("gates")
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise ReportError(f"{source}：要有 gates 列表。")
    gates: list[Gate] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise ReportError(f"{source}：gates[{index}] 要是一个字典。")
        name = str(entry.get("id") or f"gates[{index}]")
        where = f"{source}：门槛 {name}"
        unknown = sorted(set(entry) - GATE_KEYS)
        if unknown:
            raise ReportError(f"{where}：不认识的键 {'、'.join(map(str, unknown))}。")
        for key in ("id", "suite", "metric", "op"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise ReportError(f"{where}：缺 {key}。")
        if name in seen:
            raise ReportError(f"{where}：id 重复了。")
        seen.add(name)
        op = entry["op"].strip()
        if op not in COMPARE_OPS and op not in SPECIAL_OPS:
            raise ReportError(f"{where}：op {op!r} 不认识（{'、'.join(COMPARE_OPS + SPECIAL_OPS)}）。")
        threshold = None if op in SPECIAL_OPS else parse_threshold(entry.get("value"), where=where)
        baseline = entry.get("baseline")
        if baseline is not None and (isinstance(baseline, bool) or not isinstance(baseline, (int, float))):
            raise ReportError(f"{where}：baseline 要是数字或 null。")
        scope = str(entry.get("scope") or "bot")
        if scope not in ("bot", "pooled"):
            raise ReportError(f"{where}：scope 要是 bot 或 pooled。")
        if not isinstance(entry.get("abs", False), bool):
            raise ReportError(f"{where}：abs 要是 true / false。")
        gates.append(Gate(
            id=name, suite=entry["suite"].strip(), metric=entry["metric"].strip(), op=op, threshold=threshold,
            baseline=None if baseline is None else float(baseline), status=str(entry.get("status") or ""), note=str(entry.get("note") or "").strip(),
            abs=bool(entry.get("abs", False)), scope=scope,
        ))
    return gates


def load_gates(path: str | Path = GATES_FILE) -> list[Gate]:
    target = Path(path)
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ReportError(f"读不了门槛文件 {target}：{exc}") from exc
    except yaml.YAMLError as exc:
        raise ReportError(f"{target.name} 不是合法的 YAML。") from exc
    return parse_gates(data, source=target.name)


def _compare(op: str, value: float, limit: float) -> bool:
    close = math.isclose(value, limit, rel_tol=0.0, abs_tol=1e-9)
    if op == "==":
        return close
    if op == "<=":
        return value <= limit or close
    if op == ">=":
        return value >= limit or close
    if op == "<":
        return value < limit and not close
    return value > limit and not close


# ---------------------------------------------------------------------------------------------------------------------- calibration
@dataclass(frozen=True)
class Calibration:
    """What ``calibrate`` found for one rubric: the agreement of the judge with the people who labelled the calibration set."""

    rubric: str
    n: int | None = None
    kappa: float | None = None
    lo: float | None = None
    hi: float | None = None
    flagged: bool | None = None  # counts_for_acceptance as the file says
    version: int | None = None  # the rubric version it was calibrated for (when the file says)
    reason: str = ""  # why it does (not) count, in the words of the calibration file

    @property
    def counts(self) -> bool:
        """The verdicts of this rubric may go into the acceptance: kappa is 0.6 or more (H48) and the file does not say otherwise."""
        if self.kappa is not None and self.kappa < stats.KAPPA_THRESHOLD:
            return False
        if self.flagged is not None:
            return self.flagged
        return self.kappa is not None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def parse_calibration(data: Any, *, source: str = "calibration.json") -> dict[str, Calibration]:
    if not isinstance(data, Mapping) or not isinstance(data.get("rubrics"), Mapping):
        raise ReportError(f"{source}：里面要有 rubrics（每个 rubric 一项：n、kappa、ci、counts_for_acceptance）。")
    out: dict[str, Calibration] = {}
    for name, entry in data["rubrics"].items():
        if not isinstance(entry, Mapping):
            raise ReportError(f"{source}：rubrics.{name} 要是一个字典。")
        ci = entry.get("ci")
        lo = hi = None
        if isinstance(ci, Sequence) and not isinstance(ci, str) and len(ci) == 2:
            lo, hi = _finite(ci[0]), _finite(ci[1])
        elif isinstance(ci, Mapping):
            lo, hi = _finite(ci.get("lo")), _finite(ci.get("hi"))
        flag = entry.get("counts_for_acceptance")
        version = entry.get("rubric_version", entry.get("version"))
        n = entry.get("n")
        out[str(name)] = Calibration(
            rubric=str(name), n=int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else None, kappa=_finite(entry.get("kappa")), lo=lo, hi=hi,
            flagged=flag if isinstance(flag, bool) else None, version=int(version) if isinstance(version, (int, float)) and not isinstance(version, bool) else None,
            reason=str(entry.get("reason") or "").strip(),
        )
    return out


def load_calibration(path: str | Path) -> dict[str, Calibration]:
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ReportError(f"读不了校准文件 {target}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReportError(f"{target.name} 不是合法的 JSON：{exc}") from exc
    return parse_calibration(data, source=target.name)


# ---------------------------------------------------------------------------------------------------------------------- the data
@dataclass
class RunInfo:
    """One run directory."""

    name: str
    path: Path
    rows: int = 0
    items: int = 0
    judgments: int = 0
    damaged: int = 0
    modes: tuple[str, ...] = ()
    models: tuple[str, ...] = ()


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """The objects of a jsonl file and the number of lines that were not (a damaged line is skipped, not fatal)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReportError(f"读不了 {path}：{exc}") from exc
    rows: list[dict[str, Any]] = []
    damaged = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            damaged += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            damaged += 1
    return rows, damaged


def suite_of(tags: Iterable[Any] | None, situation: Any, suite_ids: Iterable[str]) -> str | None:
    """The suite a situation belongs to: its first tag that names a suite (``[L1, dead_voter, ...]``), else the prefix of its id (``l3-dead-voter``)."""
    ids = set(suite_ids)
    for tag in tags or ():
        if isinstance(tag, str) and tag in ids:
            return tag
    head = str(situation or "").split("-", 1)[0].upper()
    return head if head in ids else None


class Data:
    """Everything the run directories hold, de-duplicated and indexed.  A sample (situation, seat, bot, sample number) that is in two runs is counted once
    (``M1`` runs the bot on the same situations as the suite itself): the copy the judges have worked on if there is one, else the run given last.  Items and
    judgments follow their rows (an item of the copy that was not kept is dropped)."""

    def __init__(self) -> None:
        self.runs: list[RunInfo] = []
        self.rows: list[dict[str, Any]] = []
        self.suite_rows: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.verdicts: dict[str, dict[str, Any]] = {}
        self.rubric_versions: dict[str, Counter[int]] = {}
        self.counts: Counter[str] = Counter()
        self._index: dict[tuple[str, str | None, str], list[tuple[dict[str, Any], dict[str, Any] | None]]] = {}

    # -- access
    def labels(self, suites: Iterable[str]) -> list[str]:
        """The bots that have rows in ``suites``."""
        found: set[str] = set()
        for suite in suites:
            found.update(label for label, rows in self.suite_rows.get(suite, {}).items() if rows)
        return sorted(found)

    def rows_of(self, suites: Iterable[str], labels: Iterable[str]) -> list[dict[str, Any]]:
        wanted = list(labels)
        return [row for suite in suites for label in wanted for row in self.suite_rows.get(suite, {}).get(label, ())]

    def asked(self, rubric: str, suite: str, labels: Iterable[str]) -> list[dict[str, Any]]:
        """The items of ``rubric`` about the speeches / votes of ``labels`` in ``suite`` (judged or not)."""
        return [item for label in labels for item, _verdict in self._index.get((rubric, suite, label), ())]

    def judged(self, rubric: str, suite: str, labels: Iterable[str]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """``(item, verdict)`` for those of :meth:`asked` that have a judgment."""
        return [(item, verdict) for label in labels for item, verdict in self._index.get((rubric, suite, label), ()) if verdict is not None]

    def rubric_totals(self, rubric: str) -> tuple[int, int]:
        """``(items, judged)`` of a rubric over the whole run."""
        pairs = [pair for (name, _suite, _label), group in self._index.items() if name == rubric for pair in group]
        return len(pairs), sum(1 for _item, verdict in pairs if verdict is not None)

    # -- loading
    def add_run(self, entry: str | Path, taken: Counter[str], copies: dict[tuple[Any, ...], list[dict[str, Any]]], raw_items: dict[str, dict[str, Any]],
                raw_verdicts: dict[str, dict[str, Any]]) -> None:
        path = Path(entry)
        if path.is_file() and path.name == "results.jsonl":
            path = path.parent
        if not path.is_dir():
            raise ReportError(f"找不到运行目录：{path}")
        results = path / "results.jsonl"
        if not results.is_file():
            raise ReportError(f"{path} 里没有 results.jsonl（要给 `python -m bench run` 的输出目录）。")
        name = path.resolve().name or str(path)
        taken[name] += 1
        if taken[name] > 1:
            name = f"{name}-{taken[name]}"
        info = RunInfo(name=name, path=path)
        rows, bad = read_jsonl(results)
        info.damaged += bad
        modes: set[str] = set()
        models: set[str] = set()
        local: dict[tuple[Any, ...], dict[str, Any]] = {}  # (a resumed run writes a sample again: inside one file the last row wins)
        for row in rows:
            if row.get("format") not in (None, RESULT_FORMAT) or not isinstance(row.get("bot"), str) or not isinstance(row.get("situation"), str):
                continue
            info.rows += 1
            modes.add(str(row.get("mode") or "?"))
            if row.get("mode") != "fake" and row.get("model"):
                models.add(str(row["model"]))
            local[(row["situation"], row.get("seat"), row["bot"], row.get("sample"))] = row
        for key, row in local.items():
            copies.setdefault(key, []).append(row)
        for file, bucket, attr in (("judge_items.jsonl", raw_items, "items"), ("judgments.jsonl", raw_verdicts, "judgments")):
            target = path / file
            if not target.is_file():
                continue
            entries, bad = read_jsonl(target)
            info.damaged += bad
            for entry_row in entries:
                blind = entry_row.get("blind_id")
                if isinstance(blind, str) and blind:
                    bucket[blind] = entry_row
                    setattr(info, attr, getattr(info, attr) + 1)
        info.modes, info.models = tuple(sorted(modes)), tuple(sorted(models))
        self.runs.append(info)
        self.counts["damaged"] += info.damaged

    def finish(self, copies: dict[tuple[Any, ...], list[dict[str, Any]]], raw_items: dict[str, dict[str, Any]], raw_verdicts: dict[str, dict[str, Any]],
               suite_ids: Iterable[str]) -> None:
        ids = set(suite_ids)
        judged_copies: set[tuple[Any, ...]] = set()  # (sample, run) of the copies whose items have a judgment
        for blind, entry in raw_verdicts.items():
            item = raw_items.get(blind)
            if item is not None and isinstance(entry.get("verdict"), Mapping):
                judged_copies.add(((item.get("situation"), item.get("seat"), item.get("bot"), item.get("sample")), item.get("run")))
        keyed: dict[tuple[Any, ...], dict[str, Any]] = {}
        for key, found in copies.items():
            chosen = found[-1]
            if len(found) > 1:
                self.counts["duplicates"] += len(found) - 1
                judged = [row for row in found if (key, row.get("run")) in judged_copies]
                chosen = judged[-1] if judged else chosen
            keyed[key] = chosen
        alive: dict[tuple[Any, ...], Any] = {}
        for key, row in keyed.items():
            if "example" in (row.get("tags") or []):
                self.counts["examples"] += 1
                continue
            alive[key] = row.get("run")
            self.rows.append(row)
            if row.get("mode") == "fake":
                self.counts["fake"] += 1
            if not usable(row):
                self.counts["unusable"] += 1
            suite = suite_of(row.get("tags"), row["situation"], ids)
            if suite is None:
                self.counts["unassigned"] += 1
                continue
            self.suite_rows.setdefault(suite, {}).setdefault(row["bot"], []).append(row)
        for blind, item in raw_items.items():
            key = (item.get("situation"), item.get("seat"), item.get("bot"), item.get("sample"))
            if key not in alive:
                self.counts["orphan_items"] += 1
            elif item.get("run") != alive[key]:
                self.counts["superseded_items"] += 1
            elif isinstance(item.get("rubric"), str) and isinstance(item.get("bot"), str):
                self.items[blind] = item
        for blind, entry in raw_verdicts.items():
            item = self.items.get(blind)
            verdict = entry.get("verdict")
            if item is None or entry.get("rubric") not in (None, item["rubric"]):
                self.counts["orphan_judgments"] += 1
            elif not isinstance(verdict, Mapping):
                self.counts["bad_judgments"] += 1
            else:
                self.verdicts[blind] = entry
                version = entry.get("rubric_version")
                if isinstance(version, int) and not isinstance(version, bool):
                    self.rubric_versions.setdefault(item["rubric"], Counter())[version] += 1
        for blind, item in self.items.items():
            suite = suite_of(item.get("tags"), item.get("situation"), ids)
            entry = self.verdicts.get(blind)
            self._index.setdefault((item["rubric"], suite, item["bot"]), []).append((item, dict(entry["verdict"]) if entry else None))


def load_runs(dirs: Sequence[str | Path], suite_ids: Iterable[str]) -> Data:
    """Read the run directories (in the order given).  A sample that is in several is kept once: the copy that has judgments, else the one of the run given last."""
    data = Data()
    taken: Counter[str] = Counter()
    copies: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    raw_items: dict[str, dict[str, Any]] = {}
    raw_verdicts: dict[str, dict[str, Any]] = {}
    for entry in dirs:
        data.add_run(entry, taken, copies, raw_items, raw_verdicts)
    data.finish(copies, raw_items, raw_verdicts, suite_ids)
    return data


# ---------------------------------------------------------------------------------------------------------------------- reading a row
def usable(row: Mapping[str, Any]) -> bool:
    """A sample that says something about the bot: not an error of the run (a scene that cannot be built, a model that did not answer) and not a sample the budget
    ended before the bot had its say."""
    status = row.get("status")
    return not (status == "error" or (status == "incomplete" and row.get("stop") == "budget"))


def _decision(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    decision = row.get("decision")
    return decision if isinstance(decision, Mapping) else None


def _truth(row: Mapping[str, Any]) -> Mapping[str, Any]:
    truth = row.get("truth")
    return truth if isinstance(truth, Mapping) else {}


def answer_state(row: Mapping[str, Any]) -> str:
    """``correct`` / ``wrong`` (a vote or an answer, scored against ``truth``), ``abstain`` (a ``respond`` that votes for nobody), ``none`` (the bot did not answer
    at all) or ``skip`` (a failure of the run, or a situation with nothing to score against)."""
    if not usable(row):
        return "skip"
    score = row.get("score")
    if not _truth(row).get("answer") and not isinstance(score, Mapping):
        return "skip"
    decision = _decision(row)
    if decision is None or decision.get("tool") != "respond":
        return "none"
    if str(decision.get("value") if decision.get("value") is not None else "").strip() in ABSTAIN_VALUES:
        return "abstain"
    if isinstance(score, Mapping) and "correct" in score:
        return "correct" if score["correct"] else "wrong"
    if row.get("correct") is not None:
        return "correct" if row["correct"] else "wrong"
    return "correct" if str(decision.get("value")) in [str(a) for a in _truth(row).get("answer") or ()] else "wrong"


def pair_of(row: Mapping[str, Any]) -> str | None:
    """The pair an L1 situation belongs to (``meta.pair``, the ``pair:<id>`` tag, or the situation id without its ``-a`` / ``-b``)."""
    pair = (row.get("meta") or {}).get("pair") if isinstance(row.get("meta"), Mapping) else None
    if isinstance(pair, str) and pair:
        return pair
    for tag in row.get("tags") or ():
        if isinstance(tag, str) and tag.startswith("pair:") and len(tag) > 5:
            return tag[5:]
    match = re.match(r"^(.+)-[ab]$", str(row.get("situation") or ""))
    return match.group(1) if match else None


def l1_side(row: Mapping[str, Any]) -> str:
    """What an L1 vote did about the two people the pair is about: ``trust`` (voted the blunt one: believes the fluent one), ``distrust`` (voted the fluent one),
    ``other`` (voted somebody else), ``abstain`` (no vote, or no decision) or ``skip`` (a failure of the run)."""
    if not usable(row):
        return "skip"
    sides = _truth(row).get("sides")
    decision = _decision(row)
    if not isinstance(sides, Mapping) or decision is None or decision.get("tool") != "respond":
        return "abstain"
    value = str(decision.get("value") if decision.get("value") is not None else "").strip()
    if value in ABSTAIN_VALUES:
        return "abstain"
    if value == sides.get("blunt"):
        return "trust"
    if value == sides.get("fluent"):
        return "distrust"
    return "other"


# ---------------------------------------------------------------------------------------------------------------------- the numbers
Cluster = list[tuple[float, float]]


@dataclass
class Built:
    """The observations of one number: per question (a cluster) the ``(numerator, denominator)`` of each speech / vote / judgment, plus what is worth saying
    about them."""

    clusters: dict[str, Cluster] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def add(self, item: str, num: float, den: float) -> None:
        self.clusters.setdefault(item, []).append((float(num), float(den)))


Builder = Callable[[Data, Sequence[str], Sequence[str]], Built]


def _rows(data: Data, srcs: Sequence[str], labels: Sequence[str]) -> list[dict[str, Any]]:
    return data.rows_of(srcs, labels)


def build_truncated(data: Data, labels: Sequence[str], srcs: Sequence[str]) -> Built:
    out = Built()
    for row in _rows(data, srcs, labels):
        if not usable(row):
            continue
        runtime = row.get("runtime") if isinstance(row.get("runtime"), Mapping) else {}
        if isinstance(runtime.get("agent_speeches"), int):  # (the ledger's own count: what the engine did to the speeches the bot's moves made)
            out.add(row["situation"], float(runtime.get("truncated_speeches") or 0), float(runtime["agent_speeches"]))
        elif (_decision(row) or {}).get("text"):
            verbatim = (row.get("integrity") or {}).get("speech_verbatim") if isinstance(row.get("integrity"), Mapping) else None
            out.add(row["situation"], 1.0 if verbatim is False else 0.0, 1.0)
    return out


def build_rewrite(data: Data, labels: Sequence[str], srcs: Sequence[str]) -> Built:
    out = Built()
    refused = 0
    for row in _rows(data, srcs, labels):
        if not usable(row):
            continue
        counted = rewrite_stats([row])
        refused += counted["refused"]
        if counted["retried"]:
            out.add(row["situation"], counted["recovered"], counted["retried"])
    out.extra["refused"] = refused
    out.notes.append(f"被退回 {refused} 次" + ("" if refused else "，没有重写可看"))
    return out


def build_floor_timeout(data: Data, labels: Sequence[str], srcs: Sequence[str]) -> Built:
    out = Built()
    for row in _rows(data, srcs, labels):
        ends = (row.get("runtime") or {}).get("floor_ends") if isinstance(row.get("runtime"), Mapping) else None
        if usable(row) and isinstance(ends, Mapping):
            total = sum(v for v in ends.values() if isinstance(v, (int, float)) and not isinstance(v, bool))
            if total:
                out.add(row["situation"], float(ends.get("timeout") or 0), float(total))
    return out


def build_form(rule: str) -> Builder:
    """Per speech (``row.speech``: what the runner checked when the speech was accepted)."""

    def build(data: Data, labels: Sequence[str], srcs: Sequence[str]) -> Built:
        out = Built()
        for row in _rows(data, srcs, labels):
            speech = row.get("speech")
            if not usable(row) or not isinstance(speech, Mapping):
                continue
            problems = speech.get("problems") or []
            if rule == "list":
                hit = "list" in problems
            elif rule == "markdown":
                hit = "heading" in problems
            elif rule == "prefix":
                hit = "prefix" in problems
            elif rule == "report":
                hit = bool(problems) or bool(speech.get("inline_enum"))
            else:  # in_band: only for a bot whose voice declares a band
                if speech.get("in_band") is None:
                    continue
                hit = bool(speech["in_band"])
            out.add(row["situation"], 1.0 if hit else 0.0, 1.0)
        return out

    return build


def build_cliches(data: Data, labels: Sequence[str], srcs: Sequence[str]) -> Built:
    out = Built()
    for row in _rows(data, srcs, labels):
        text = (_decision(row) or {}).get("text")
        if usable(row) and text:
            out.add(row["situation"], float(sum(1 for phrase in DEFAULT_CLICHES if phrase in str(text))), 1.0)
    return out


def _judged_note(out: Built, asked: int, judged: int) -> None:
    out.extra.update(items=asked, judged=judged)
    if asked > judged:
        out.notes.append(f"还有 {asked - judged} 条没有判定")


def build_style(top: int) -> Builder:
    """S2: the judge names a card; it is right when that card belongs to the bot that spoke."""

    def build(data: Data, labels: Sequence[str], srcs: Sequence[str]) -> Built:
        out = Built()
        chances: list[float] = []
        for item, verdict in data.judged("S2", "S2", labels):
            truth = item.get("truth") if isinstance(item.get("truth"), Mapping) else {}
            cards = truth.get("cards") if isinstance(truth.get("cards"), Mapping) else {}
            speaker = truth.get("bot") or item.get("bot")
            guesses = [verdict.get("match")] + ([verdict.get("runner_up")] if top >= 2 else [])
            hit = any(g is not None and cards.get(str(g)) == speaker for g in guesses)
            out.add(item["situation"], 1.0 if hit else 0.0, 1.0)
            if cards:
                chances.append(min(1.0, top / len(cards)))
        if chances:
            out.extra["chance"] = sum(chances) / len(chances)
            out.notes.append(f"随机水平 {out.extra['chance'] * 100:.1f}%")
        _judged_note(out, len(data.asked("S2", "S2", labels)), len(data.judged("S2", "S2", labels)))
        return out

    return build


def build_basis(rubric: str, suite: str, hit: Callable[[Mapping[str, Any]], bool]) -> Builder:
    """A judge's verdict about the basis of a decision, counted when ``hit`` says so."""

    def build(data: Data, labels: Sequence[str], srcs: Sequence[str]) -> Built:
        out = Built()
        judged = data.judged(rubric, suite, labels)
        for item, verdict in judged:
            out.add(item["situation"], 1.0 if hit(verdict) else 0.0, 1.0)
        _judged_note(out, len(data.asked(rubric, suite, labels)), len(judged))
        return out

    return build


def _decisive_hit(verdict: Mapping[str, Any]) -> bool:
    return verdict.get("cites_decisive") is True or verdict.get("basis") == "decisive_fact"


def build_accuracy(data: Data, labels: Sequence[str], srcs: Sequence[str]) -> Built:
    """Right answers / questions asked.  No answer, an abstention and a wrong answer all count as wrong; a failure of the run is left out."""
    out = Built()
    counts: Counter[str] = Counter()
    for row in _rows(data, srcs, labels):
        state = answer_state(row)
        counts[state] += 1
        if state != "skip":
            out.add(row["situation"], 1.0 if state == "correct" else 0.0, 1.0)
    out.extra.update(no_answer=counts["none"] + counts["abstain"], left_out=counts["skip"])
    if counts["none"] + counts["abstain"]:
        out.notes.append(f"没作答 / 弃票 {counts['none'] + counts['abstain']}（按错算）")
    if counts["skip"]:
        out.notes.append(f"{counts['skip']} 个样本出错或预算耗尽，不计")
    return out


def _bias(p: float) -> float:
    return p - 0.5


def variant_of(row: Mapping[str, Any]) -> str | None:
    """Which version of its pair an L1 situation is: ``A`` or ``B`` (``meta.variant``, a tag, or the ``-a`` / ``-b`` ending of the situation id)."""
    meta = row.get("meta") if isinstance(row.get("meta"), Mapping) else {}
    value = meta.get("variant")
    if isinstance(value, str) and value.upper() in ("A", "B"):
        return value.upper()
    for tag in row.get("tags") or ():
        if tag in ("A", "B"):
            return str(tag)
    match = re.search(r"-([ab])$", str(row.get("situation") or ""))
    return match.group(1).upper() if match else None


def build_l1_bias(data: Data, labels: Sequence[str], srcs: Sequence[str]) -> Built:
    """P(trusts the fluent one) over the complete pairs - an A and a B version of the same pair, same bot, same sample number, both with a side taken."""
    out = Built()
    groups: dict[tuple[Any, ...], list[tuple[str | None, str]]] = {}
    unpaired = other = abstain = 0
    for row in _rows(data, srcs, labels):
        side = l1_side(row)
        if side == "skip":
            continue
        pair = pair_of(row)
        if pair is None or not isinstance(_truth(row).get("sides"), Mapping):
            unpaired += 1
            continue
        other += side == "other"
        abstain += side == "abstain"
        groups.setdefault((pair, row.get("seat"), row["bot"], row.get("sample")), []).append((variant_of(row), side))
    complete = incomplete = 0
    trusted: Counter[str] = Counter()
    asked: Counter[str] = Counter()
    for (pair, _seat, _bot, _sample), found in groups.items():
        taken = [(v, s) for v, s in found if s in ("trust", "distrust")]
        if len(found) == 2 and len(taken) == 2:
            complete += 1
            out.add(pair, float(sum(1 for _v, s in taken if s == "trust")), 2.0)
            if {v for v, _s in taken} == {"A", "B"}:
                for variant, side in taken:
                    asked[str(variant)] += 1
                    trusted[str(variant)] += side == "trust"
        else:
            incomplete += 1
    out.extra.update(complete=complete, incomplete=incomplete, abstain=abstain, other=other, unpaired=unpaired, votes=2 * complete)
    if complete and sum(asked.values()) == 2 * complete:
        out.extra["versions"] = {v: [trusted[v], asked[v]] for v in sorted(asked)}  # (trusted, asked) per version: for the interval when nothing varies
    out.notes.append(f"成对 {complete}（{2 * complete} 票）；没站边 {abstain} 行、投给第三人 {other} 行（连同它的另一版不计，共 {incomplete} 对）")
    if unpaired:
        out.notes.append(f"{unpaired} 行认不出是哪一对")
    return out


def _bias_fallback(built: Built, items: int) -> tuple[float, float] | None:
    """The interval of the bias when the bootstrap has nothing to vary (every pair came out the same way): bias = (P(trust | A) + P(trust | B)) / 2 - 0.5, and each
    of the two proportions gets its Wilson interval for as many observations as there are pairs (conservative; the ends add up)."""
    versions = built.extra.get("versions")
    if not isinstance(versions, Mapping) or len(versions) != 2:
        return None
    lo = hi = 0.0
    for trusted, asked in versions.values():
        wl, wh = stats.wilson_interval(round(trusted / asked * items), items)
        lo, hi = lo + wl / 2, hi + wh / 2
    return lo - 0.5, hi - 0.5


def build_l1_accuracy(data: Data, labels: Sequence[str], srcs: Sequence[str]) -> Built:
    """The right votes among the votes that were cast (no vote, no decision: left out, and counted)."""
    out = Built()
    left = 0
    for row in _rows(data, srcs, labels):
        state = answer_state(row)
        if state in ("correct", "wrong"):
            out.add(row["situation"], 1.0 if state == "correct" else 0.0, 1.0)
        elif state in ("none", "abstain"):
            left += 1
    out.extra["abstain"] = left
    out.notes.append(f"没投票 {left} 行不计")
    return out


def build_l1_abstain(data: Data, labels: Sequence[str], srcs: Sequence[str]) -> Built:
    out = Built()
    for row in _rows(data, srcs, labels):
        state = answer_state(row)
        if state != "skip":
            out.add(row["situation"], 1.0 if state in ("none", "abstain") else 0.0, 1.0)
    return out


@dataclass(frozen=True)
class MetricSpec:
    """A number the report knows how to make: the suite it belongs to, its name there (the one in ``bench/suites/<suite>.yml`` and in ``gates.yml``), and how."""

    suite: str
    name: str
    title: str
    build: Builder | None
    fmt: str = "pct"  # pct | num | signed
    better: str = "higher"  # higher | lower | zero
    rubric: str = ""  # a judge-based number names its rubric
    binary: bool = True  # every observation is yes / no: a bootstrap interval that is a single point gets a Wilson interval
    transform: Callable[[float], float] | None = None
    group: bool = False  # a number of a whole group of bots (S3 homogeneity), not of one bot
    fallback: Callable[[Built, int], tuple[float, float] | None] | None = None  # the interval when the bootstrap's is a single point (instead of Wilson)


def _specs() -> list[MetricSpec]:
    return [
        MetricSpec("R", "form.truncated_rate", "发言被截断的比例", build_truncated, better="lower"),
        MetricSpec("R", "runtime.speak.rewrite_success_rate", "被退回后重写成功的比例", build_rewrite),
        MetricSpec("R", "runtime.floor_timeout_rate", "发言权因超时结束的比例", build_floor_timeout, better="lower"),
        MetricSpec("S1", "form.list_rate", "分条列项的比例", build_form("list"), better="lower"),
        MetricSpec("S1", "form.markdown_rate", "用 markdown 的比例", build_form("markdown"), better="lower"),
        MetricSpec("S1", "form.self_prefix_rate", "开头自报座位 / 名字的比例", build_form("prefix"), better="lower"),
        MetricSpec("S1", "form.report_rate", "“像报告”的比例", build_form("report"), better="lower"),
        MetricSpec("S1", "form.in_band_rate", "落在自己声明篇幅里的比例", build_form("in_band")),
        MetricSpec("S2", "style.top1_accuracy", "风格识别 top-1 正确率", build_style(1), rubric="S2"),
        MetricSpec("S2", "style.top2_accuracy", "风格识别 top-2 正确率", build_style(2), rubric="S2"),
        MetricSpec("S3", "homogeneity.shared_occurrence_share", "跨 bot 共用的 6 字片段占比", None, better="lower", binary=False, group=True),
        MetricSpec("S3", "cliches.hits_per_speech", "每条发言的套话命中数", build_cliches, fmt="num", better="lower", binary=False),
        MetricSpec("L1", "expression_bias", "表达偏差", build_l1_bias, fmt="signed", better="zero", transform=_bias, fallback=_bias_fallback),
        MetricSpec("L1", "accuracy", "站边准确率", build_l1_accuracy),
        MetricSpec("L1", "abstain_rate", "没站边的比例", build_l1_abstain, better="lower"),
        MetricSpec("L1", "expression_basis_rate", "裁判判“主要依据是说话方式”的比例", build_basis("L1", "L1", lambda v: v.get("basis") == "expression"), better="lower", rubric="L1"),
        MetricSpec("L2", "trivia_hit_rate", "被琐碎点带偏的比例", build_basis("L2", "L2", lambda v: v.get("basis") == "trivia"), better="lower", rubric="L2"),
        MetricSpec("L2", "accuracy", "投对的比例", build_accuracy),
        MetricSpec("L3", "accuracy", "投对的比例", build_accuracy),
        MetricSpec("L3", "decisive_hit_rate", "抓住决定性事实的比例", build_basis("L3", "L3", _decisive_hit), rubric="L3"),
        MetricSpec("L3", "decisive_main_rate", "以决定性事实为主要依据的比例", build_basis("L3", "L3", lambda v: v.get("basis") == "decisive_fact"), rubric="L3"),
        MetricSpec("L4", "side_accuracy", "站边 / 投票准确率", build_accuracy),
        MetricSpec("M2", "recall.accuracy", "前期事实的召回正确率", build_accuracy),
        MetricSpec("M3", "rules_quiz.accuracy", "规则测验正确率", build_accuracy),
    ]


#: The numbers the report can make, by ``(suite, name)``.  Every metric a suite file declares and every metric a gate names must be here (a test checks).
METRICS: dict[tuple[str, str], MetricSpec] = {(spec.suite, spec.name): spec for spec in _specs()}
#: The suites ``run`` does not run: the report only says "待数据".
AWAITING_SUITES = ("E1", "E2", "G")


@dataclass(frozen=True)
class Estimate:
    """A number with its interval.  ``n`` = what it is over (speeches, votes, judgments), ``items`` = how many questions that is spread over."""

    value: float | None = None
    lo: float | None = None
    hi: float | None = None
    n: float = 0.0
    items: int = 0
    method: str = "none"  # bootstrap | wilson-items | point | none
    extra: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Instrument:
    """The judge behind a rubric's numbers: are there verdicts, and may they count for acceptance (H48)."""

    rubric: str
    items: int
    judged: int
    version: int | None
    state: str  # counts | reference | uncalibrated | unjudged
    calibration: Calibration | None
    note: str


@dataclass(frozen=True)
class Measured:
    """An estimate of one metric for one arm (a bot, or a group of bots), and the state of its judge (``None``: no judge is involved)."""

    spec: MetricSpec
    labels: tuple[str, ...]
    estimate: Estimate
    clusters: Mapping[str, Sequence[tuple[float, float]]]
    state: str | None = None
    comparable: bool = True  # False: the number is not comparable with the threshold (S3 homogeneity of fewer than 4 bots)


def _num(value: Any) -> float | None:
    """A number for the JSON files: ``None`` for nan / missing, else rounded (so that two machines write the same digits)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else round(number, 6)


class _Calc:
    """Makes and remembers the estimates and the state of every rubric."""

    def __init__(self, data: Data, suites: Mapping[str, Suite], calibration: Mapping[str, Calibration], seed: int, resamples: int) -> None:
        self.data, self.suites, self.calibration, self.seed, self.resamples = data, suites, calibration, seed, resamples
        self._instruments: dict[str, Instrument] = {}
        self._measured: dict[tuple[Any, ...], Measured] = {}

    def srcs(self, suite_id: str) -> tuple[str, ...]:
        """The suites whose rows a suite's numbers are made of (S1 / S3 use the speeches of S2)."""
        suite = self.suites.get(suite_id)
        return tuple(suite.sources) if suite is not None and suite.sources else (suite_id,)

    # -- the judges
    def instrument(self, rubric: str) -> Instrument:
        if rubric in self._instruments:
            return self._instruments[rubric]
        items, judged = self.data.rubric_totals(rubric)
        versions = self.data.rubric_versions.get(rubric)
        version = versions.most_common(1)[0][0] if versions else None
        cal = self.calibration.get(rubric)
        if judged == 0:
            state, note = "unjudged", ("还没有判定（judgments.jsonl）" if items else "没有这个 rubric 的裁判条目")
        elif cal is None:
            state, note = "uncalibrated", ("没有校准结果（calibration.json 里没有 " + rubric + "）") if self.calibration else "没有校准结果（没有给 calibration.json）"
        elif cal.version is not None and version is not None and cal.version != version:
            state, note = "uncalibrated", f"校准的是 {rubric} v{cal.version}，判定用的是 v{version}"
        elif cal.counts:
            state, note = "counts", "已校准，计入验收"
        elif cal.kappa is not None and cal.kappa < stats.KAPPA_THRESHOLD:
            state, note = "reference", f"κ={cal.kappa:.2f} 低于 {stats.KAPPA_THRESHOLD}"
        else:
            state, note = "reference", cal.reason or "校准结果标了不计入验收"  # (kappa is enough: what the file says is why it still does not count, e.g. too few pairs)
        out = Instrument(rubric, items, judged, version, state, cal, note)
        self._instruments[rubric] = out
        return out

    # -- the numbers
    def measure(self, spec: MetricSpec, labels: Sequence[str]) -> Measured:
        key = (spec.suite, spec.name, tuple(labels))
        if key not in self._measured:
            self._measured[key] = self._group(spec, labels) if spec.group else self._rate(spec, labels)
        return self._measured[key]

    def _rate(self, spec: MetricSpec, labels: Sequence[str]) -> Measured:
        assert spec.build is not None
        built = spec.build(self.data, labels, self.srcs(spec.suite))
        estimate = self.estimate(spec, built, labels)
        state = self.instrument(spec.rubric).state if spec.rubric else None
        return Measured(spec, tuple(labels), estimate, built.clusters, state)

    def estimate(self, spec: MetricSpec, built: Built, labels: Sequence[str]) -> Estimate:
        clusters = {item: obs for item, obs in built.clusters.items() if obs}
        total = sum(den for obs in clusters.values() for _num_, den in obs)
        notes = tuple(built.notes)
        if total <= 0:
            return Estimate(extra=built.extra, notes=notes)
        successes = sum(num for obs in clusters.values() for num, _den in obs)
        items = sum(1 for obs in clusters.values() if sum(den for _n, den in obs) > 0)
        seed = stats.derive_seed(self.seed, "estimate", spec.suite, spec.name, *labels)
        interval = stats.cluster_ratio_ci(clusters, n=self.resamples, seed=seed, transform=spec.transform)
        value, lo, hi, method = _num(interval.point), _num(interval.lo), _num(interval.hi), "bootstrap"
        raw = successes / total
        if lo == hi and items >= 1:
            # (everything came out the same: the resampling has nothing to vary - say how little that proves, counting the questions rather than the samples)
            bounds: tuple[float, float] | None = None
            if spec.fallback is not None:
                bounds = spec.fallback(built, items)
            elif spec.binary and raw in (0.0, 1.0):
                shape = spec.transform or (lambda v: v)
                wl, wh = stats.wilson_interval(items if raw == 1.0 else 0, items)
                bounds = (shape(wl), shape(wh))
            if bounds is not None:
                lo, hi, method = _num(bounds[0]), _num(bounds[1]), "wilson-items"
        return Estimate(value, lo, hi, total, items, method, built.extra, notes)

    def _group(self, spec: MetricSpec, labels: Sequence[str]) -> Measured:
        """S3 homogeneity: the share of 6-grams that several bots use, over the speeches of the bots of ``labels`` in the same situations."""
        srcs = self.srcs(spec.suite)
        speeches: list[tuple[str, Speech]] = []
        names: set[str] = set()
        for index, row in enumerate(self.data.rows_of(srcs, labels)):
            text = (_decision(row) or {}).get("text")
            if usable(row) and text:
                names.add(str(row.get("name") or ""))
                speeches.append((row["situation"], Speech(
                    seq=index, seat=row["bot"], name=str(row.get("name") or row.get("seat") or ""), channel="", text=str(text), kind="speech", truncated=0,
                )))
        bots = len({s.seat for _sit, s in speeches})
        if bots < 2:
            note = f"跨 bot 的数字，至少要 2 个 bot 的发言（现在 {bots} 个）"
            return Measured(spec, tuple(labels), Estimate(notes=(note,), extra={"bots": bots}), {}, comparable=False)
        min_seats = max(2, min(HOMOGENEITY_MIN_BOTS, bots))
        blur = sorted(names - {""})

        def share(sample: Sequence[Speech]) -> float:
            return float(homogeneity(sample, n=6, min_seats=min_seats, names=blur)["shared_occurrence_share"])

        by_situation: dict[str, list[Speech]] = {}
        for situation, speech in speeches:
            by_situation.setdefault(situation, []).append(speech)
        point = share([s for _sit, s in speeches])
        rng = random.Random(stats.derive_seed(self.seed, "homogeneity", *labels))  # (this estimate recounts the n-grams of every replicate: it has its own loop)
        keys = sorted(by_situation)
        replicates = [share([s for k in rng.choices(keys, k=len(keys)) for s in by_situation[k]]) for _ in range(min(self.resamples, HOMOGENEITY_RESAMPLES))]
        lo, hi = stats.percentile_interval(replicates, stats.DEFAULT_ALPHA)
        comparable = bots >= HOMOGENEITY_MIN_BOTS
        notes = (f"{bots} 个 bot，共用要 ≥ {min_seats} 个 bot 都用",) + (() if comparable else (f"bot 不到 {HOMOGENEITY_MIN_BOTS} 个，和 12 座位的基线不可比",))
        estimate = Estimate(_num(point), _num(lo), _num(hi), float(len(speeches)), len(by_situation), "bootstrap", {"bots": bots, "min_seats": min_seats}, notes)
        return Measured(spec, tuple(labels), estimate, {}, comparable=comparable)


# ---------------------------------------------------------------------------------------------------------------------- gates: results
@dataclass
class ArmResult:
    """A gate for one arm (a candidate bot, or all candidates together)."""

    arm: str
    labels: tuple[str, ...]
    status: str = NOT_RUN
    value: float | None = None
    lo: float | None = None
    hi: float | None = None
    n: float = 0.0
    items: int = 0
    method: str = "none"
    baseline_value: float | None = None
    baseline_n: float = 0.0
    baseline_lo: float | None = None
    baseline_hi: float | None = None
    limit: float | None = None
    margin_lo: float | None = None
    margin_hi: float | None = None
    p_value: float | None = None
    crosses: bool | None = None
    would_be: str | None = None
    reason: str = ""
    layers: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GateResult:
    gate: Gate
    title: str
    fmt: str = "pct"
    status: str = NOT_RUN
    arms: list[ArmResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def rollup(states: Iterable[str]) -> str:
    """The one state that speaks for several (see ``_ROLLUP``); no state at all is "未跑"."""
    found = set(states)
    for state in _ROLLUP:
        if state in found:
            return state
    return NOT_RUN


@dataclass
class Verdict:
    ok: bool | None
    limit: float | None
    margin_lo: float | None = None
    margin_hi: float | None = None
    p_value: float | None = None
    crosses: bool | None = None
    note: str = ""


@dataclass
class MetricRow:
    arm: str
    measured: Measured
    kind: str = "bot"  # baseline | bot | ablated | pooled


@dataclass
class MetricResult:
    spec: MetricSpec
    declared: bool
    rows: list[MetricRow] = field(default_factory=list)
    instrument: Instrument | None = None


@dataclass
class SuiteResult:
    id: str
    title: str
    purpose: str
    status: str  # "有数据" | 未跑 | 待数据
    acceptance: bool
    rows: int = 0
    sources: tuple[str, ...] = ()
    manual: str = ""
    metrics: list[MetricResult] = field(default_factory=list)


@dataclass
class AblationResult:
    """One layer of M1 for one bot: the bot against itself without the layer."""

    bot: str
    layer: str
    option: str
    suite: str
    metric: str
    title: str
    fmt: str
    status: str = NOT_RUN
    full: Estimate | None = None
    ablated: Estimate | None = None
    diff: float | None = None
    lo: float | None = None
    hi: float | None = None
    p_value: float | None = None
    items: int = 0
    reason: str = ""
    variant: str = ""  # the label of the bot without the layer


def _without_layer(label: str) -> str:
    """``bot:Alice@no-digest`` -> ``bot:Alice``; ``bot:Alice@history=6000,no-digest`` -> ``bot:Alice@history=6000``."""
    base, options = split_label(label)
    rest = [o for o in options if o not in ABLATIONS]
    return base + ("@" + ",".join(rest) if rest else "")


@dataclass
class Report:
    label: str
    baseline: str
    seed: int
    resamples: int
    strict: bool
    generated: str
    runs: list[RunInfo]
    counts: Mapping[str, int]
    warnings: list[str]
    bots: dict[str, list[str]]
    health: dict[str, Counter[str]]
    suites: list[SuiteResult]
    ablations: list[AblationResult]
    gates: list[GateResult]
    instruments: dict[str, Instrument]
    calibration_file: str | None
    gates_file: str
    gate_counts: dict[str, int] = field(default_factory=dict)
    verdict: str = ""
    models: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------------------------------------------------- the builder
class _Builder:
    def __init__(self, data: Data, suites: Mapping[str, Suite], gates: Sequence[Gate], calibration: Mapping[str, Calibration], *, baseline: str, seed: int,
                 resamples: int, strict: bool) -> None:
        self.data, self.suites, self.gates, self.baseline, self.strict = data, suites, list(gates), baseline, strict
        self.calc = _Calc(data, suites, calibration, seed, resamples)
        every = sorted({row["bot"] for row in data.rows})
        self.bots = classify(every, baseline)
        self.problems: list[str] = []
        self.seed, self.resamples = seed, resamples
        self._ablations: list[AblationResult] | None = None

    # -- which bots a gate is about
    def _present(self, labels: Iterable[str], srcs: Sequence[str]) -> list[str]:
        return [label for label in labels if any(self.data.suite_rows.get(s, {}).get(label) for s in srcs)]

    def _baseline_for(self, label: str | None, srcs: Sequence[str]) -> list[str]:
        """The baseline bots a candidate is compared with: those that were put the same questions the same way (the same options besides the ablations)."""
        pool = self._present(self.bots["baseline"], srcs)
        return pool if label is None else [b for b in pool if plain_options(b) == plain_options(label)]

    def _arms(self, gate: Gate, srcs: Sequence[str]) -> list[tuple[str, list[str], list[str]]]:
        present = self._present(self.bots["candidates"], srcs)
        if not present:
            return []
        if gate.scope == "pooled":
            return [(POOLED, present, self._baseline_for(None, srcs))]
        return [(label, [label], self._baseline_for(label, srcs)) for label in present]

    # -- one gate
    def evaluate(self, gate: Gate) -> GateResult:
        suite = self.suites.get(gate.suite)
        spec = METRICS.get((gate.suite, gate.metric))
        result = GateResult(gate=gate, title=spec.title if spec else gate.metric, fmt=spec.fmt if spec else "num")
        if gate.op in SPECIAL_OPS or gate.suite in AWAITING_SUITES or (suite is not None and not suite.runnable):
            result.status = AWAITING
            result.notes.append("这个套件不由 run 运行（需要整局对局 / 真人语料），本版报告不聚合它的数据。")
            return result
        if suite is None:
            result.status = AWAITING
            result.notes.append(f"套件 {gate.suite} 没有定义（bench/suites/{gate.suite}.yml）。")
            self.problems.append(f"门槛 {gate.id} 指向的套件 {gate.suite} 没有定义。")
            return result
        if gate.suite == "M1":
            return self._m1_gate(gate, result)
        if spec is None:
            result.status = AWAITING
            result.notes.append(f"报告还不会算 {gate.suite} 的 {gate.metric}。")
            self.problems.append(f"门槛 {gate.id} 的指标 {gate.suite}.{gate.metric} 报告不会算。")
            return result
        srcs = self.calc.srcs(gate.suite)
        arms = self._arms(gate, srcs)
        if not arms:
            result.status = NOT_RUN
            has_baseline = bool(self._present(self.bots["baseline"], srcs))
            result.notes.append("只有基线的数据，没有候选 bot 的。" if has_baseline else "没有这个套件的数据。")
            return result
        for name, labels, base_labels in arms:
            result.arms.append(self._arm(gate, spec, suite, name, labels, base_labels))
        result.status = rollup(arm.status for arm in result.arms)
        return result

    def _arm(self, gate: Gate, spec: MetricSpec, suite: Suite, name: str, labels: list[str], base_labels: list[str]) -> ArmResult:
        arm = ArmResult(arm=name, labels=tuple(labels))
        measured = self.calc.measure(spec, labels)
        est = measured.estimate
        arm.value, arm.lo, arm.hi, arm.n, arm.items, arm.method = est.value, est.lo, est.hi, est.n, est.items, est.method
        base = self.calc.measure(spec, base_labels) if base_labels else None
        if base is not None and base.estimate.value is not None:
            arm.baseline_value, arm.baseline_n = base.estimate.value, base.estimate.n
            arm.baseline_lo, arm.baseline_hi = base.estimate.lo, base.estimate.hi
        if est.value is None:
            detail = "；".join(est.notes)
            arm.status, arm.reason = NOT_RUN, "没有可算的数据（n=0）" + (f"：{detail}" if detail else "")
            if measured.state == "unjudged":
                arm.status, arm.reason = UNJUDGED, self.calc.instrument(spec.rubric).note
            return arm
        verdict = self._verdict(gate, spec, measured, base if base is not None and base.estimate.value is not None else None)
        arm.limit, arm.margin_lo, arm.margin_hi, arm.p_value, arm.crosses = verdict.limit, verdict.margin_lo, verdict.margin_hi, verdict.p_value, verdict.crosses
        would = None if verdict.ok is None else (PASS if verdict.ok else FAIL)
        if measured.state == "unjudged":
            arm.status, arm.reason = UNJUDGED, self.calc.instrument(spec.rubric).note
        elif measured.state == "uncalibrated":
            arm.status, arm.would_be, arm.reason = UNCALIBRATED, would, self.calc.instrument(spec.rubric).note
        elif measured.state == "reference":
            arm.status, arm.would_be, arm.reason = REFERENCE, would, self.calc.instrument(spec.rubric).note
        elif not suite.acceptance:
            arm.status, arm.would_be, arm.reason = REFERENCE, would, "这个套件不进验收（acceptance: false）"
        elif not measured.comparable:
            arm.status, arm.would_be, arm.reason = REFERENCE, would, "；".join(est.notes)
        elif verdict.ok is None:
            arm.status, arm.reason = NO_BASELINE, verdict.note or "结果里没有基线 bot 的数据"
        else:
            arm.status = PASS if verdict.ok else FAIL
            if verdict.ok and verdict.crosses and self.strict:
                arm.status, arm.reason = FAIL, "按点估计达标，但区间跨过了门槛线（--strict）"
            elif verdict.crosses:
                arm.reason = "区间跨过了门槛线：样本不够下结论"
        return arm

    def _verdict(self, gate: Gate, spec: MetricSpec, cand: Measured, base: Measured | None) -> Verdict:
        assert gate.threshold is not None
        absolute = abs if gate.abs else (lambda v: v)
        value = absolute(cand.estimate.value)  # type: ignore[arg-type]
        base_value = None if base is None else absolute(base.estimate.value)  # type: ignore[arg-type]
        limit = gate.threshold.resolve(base_value)
        if limit is None:
            return Verdict(None, None, note="结果里没有基线 bot 的数据")
        verdict = Verdict(_compare(gate.op, value, limit), limit)
        if gate.op == "==":
            return verdict
        below = gate.op in ("<=", "<")  # (the margin is positive when the gate is met)
        if gate.threshold.needs_baseline and base is not None and cand.clusters and base.clusters:
            shape = spec.transform or (lambda v: v)

            def margin(a: float, b: float) -> float:
                av, bv = absolute(shape(a)), absolute(shape(b))
                line = gate.threshold.resolve(bv)  # type: ignore[union-attr]
                assert line is not None
                return (line - av) if below else (av - line)

            seed = stats.derive_seed(self.seed, "paired", gate.id, *cand.labels, "vs", *base.labels)
            paired = stats.paired_ratio_bootstrap(cand.clusters, base.clusters, margin, n=self.resamples, seed=seed)
            if paired.n_items and not math.isnan(paired.lo):
                verdict.margin_lo, verdict.margin_hi, verdict.p_value = _num(paired.lo), _num(paired.hi), _num(paired.p_value)
        elif cand.estimate.lo is not None and cand.estimate.hi is not None and not gate.abs:
            lo, hi = cand.estimate.lo, cand.estimate.hi
            verdict.margin_lo, verdict.margin_hi = (_num(limit - hi), _num(limit - lo)) if below else (_num(lo - limit), _num(hi - limit))
        if verdict.margin_lo is not None and verdict.margin_hi is not None:
            verdict.crosses = verdict.margin_lo <= 0 <= verdict.margin_hi
        return verdict

    # -- M1
    def _m1_rule(self) -> tuple[str, float]:
        """How the M1 gate tests the p-value of a layer (``p < 0.05`` unless the gate says otherwise)."""
        for gate in self.gates:
            if gate.suite == "M1" and gate.threshold is not None and gate.op in COMPARE_OPS and not gate.threshold.needs_baseline:
                return gate.op, gate.threshold.amount
        return "<", M1_ALPHA

    def _m1_gate(self, gate: Gate, result: GateResult) -> GateResult:
        layers = self.ablations()
        if not layers:
            result.status = NOT_RUN
            result.notes.append("没有消融变体（bot@no-… 之类）的结果。")
            return result
        op, amount = self._m1_rule()
        for bot in sorted({a.bot for a in layers}):
            mine = [a for a in layers if a.bot == bot]
            arm = ArmResult(arm=bot, labels=(bot,))
            arm.layers = [
                {
                    "layer": a.layer, "option": a.option, "variant": a.variant, "suite": a.suite, "metric": a.metric, "status": a.status, "full": _num(a.full.value) if a.full else None,
                    "ablated": _num(a.ablated.value) if a.ablated else None, "diff": _num(a.diff), "lo": _num(a.lo), "hi": _num(a.hi), "p_value": _num(a.p_value),
                    "items": a.items, "reason": a.reason,
                }
                for a in mine
            ]
            arm.status = rollup(a.status for a in mine)
            worst = [a.layer for a in mine if a.status != PASS]
            arm.reason = ("不通过或没有结论的层：" + "、".join(worst)) if worst else f"{len(mine)} 层都显著（p {op} {amount:g}）"
            result.arms.append(arm)
        result.status = rollup(arm.status for arm in result.arms)
        return result

    def _ablated_label(self, base: str, option: str) -> str | None:
        """The label of ``base`` with exactly the layer ``option`` taken away, as it is in the results (``bot:Alice`` + ``no-digest`` -> ``bot:Alice@no-digest``)."""
        for label in self.bots["ablated"]:
            if ablations_of(label) == (option,) and split_label(label)[0] == split_label(base)[0] and plain_options(label) == plain_options(base):
                return label
        return None

    def ablations(self) -> list[AblationResult]:
        """Every layer of ``M1.yml`` for every bot that has a version with a layer taken away (the layers that were not run are in the list too: "未跑")."""
        if self._ablations is not None:
            return self._ablations
        out: list[AblationResult] = []
        suite = self.suites.get("M1")
        entries = list(suite.ablations) if suite is not None else []
        for entry in entries:
            if (entry["suite"], entry["metric"]) not in METRICS:
                self.problems.append(f"M1 的消融 {entry['layer']} 指向的指标 {entry['suite']}.{entry['metric']} 报告不会算。")
        bases = sorted({_without_layer(label) for label in self.bots["ablated"] if len(ablations_of(label)) == 1})
        for base in bases:
            for entry in entries:
                spec = METRICS.get((entry["suite"], entry["metric"]))
                if spec is not None:
                    out.append(self._ablation(entry, spec, base, self._ablated_label(base, entry["option"])))
        self._ablations = out
        return out

    def _ablation(self, entry: Mapping[str, Any], spec: MetricSpec, full_label: str, ablated_label: str | None) -> AblationResult:
        result = AblationResult(
            bot=full_label, layer=str(entry["layer"]), option=str(entry["option"]), suite=spec.suite, metric=spec.name, title=spec.title, fmt=spec.fmt,
            variant=ablated_label or with_option(full_label, str(entry["option"])),
        )
        if ablated_label is None:
            result.reason = f"结果里没有拿掉这一层的版本（{result.variant}）"
            return result
        full, cut = self.calc.measure(spec, [full_label]), self.calc.measure(spec, [ablated_label])
        result.full, result.ablated = full.estimate, cut.estimate
        if full.estimate.value is None or cut.estimate.value is None:
            result.reason = "完整版或拿掉这一层的版本在这个指标上没有可算的数据"
            if "unjudged" in (full.state, cut.state):
                result.status, result.reason = UNJUDGED, self.calc.instrument(spec.rubric).note
            return result
        better_high = spec.better != "lower"
        seed = stats.derive_seed(self.seed, "m1", spec.suite, spec.name, full_label, ablated_label)
        paired = stats.paired_ratio_bootstrap(
            full.clusters, cut.clusters, (lambda a, b: a - b) if better_high else (lambda a, b: b - a), n=self.resamples, seed=seed,
        )
        result.items = paired.n_items
        if not paired.n_items or math.isnan(paired.p_value):
            result.reason = "完整版和拿掉这一层的版本没有共同的题"
            return result
        result.diff, result.lo, result.hi, result.p_value = _num(paired.diff), _num(paired.lo), _num(paired.hi), _num(paired.p_value)
        op, amount = self._m1_rule()
        result.status = PASS if (result.p_value is not None and _compare(op, result.p_value, amount) and (result.diff or 0) > 0) else FAIL
        if "unjudged" in (full.state, cut.state):
            result.status, result.reason = UNJUDGED, self.calc.instrument(spec.rubric).note
        elif "uncalibrated" in (full.state, cut.state):
            result.status, result.reason = UNCALIBRATED, self.calc.instrument(spec.rubric).note
        elif "reference" in (full.state, cut.state):
            result.status, result.reason = REFERENCE, self.calc.instrument(spec.rubric).note
        elif result.status == FAIL:
            result.reason = "不显著" if (result.diff or 0) > 0 else "拿掉这一层没有变差"
        return result

    # -- the suites
    def suite_results(self, gate_metrics: Iterable[tuple[str, str]]) -> list[SuiteResult]:
        pooled_metrics = {(g.suite, g.metric) for g in self.gates if g.scope == "pooled"}
        results: list[SuiteResult] = []
        for sid, suite in self.suites.items():
            srcs = self.calc.srcs(sid)
            labels = self.data.labels(srcs)
            rows = sum(len(self.data.suite_rows.get(s, {}).get(label, ())) for s in srcs for label in labels) if sid not in AWAITING_SUITES else 0
            out = SuiteResult(
                id=sid, title=suite.title, purpose=suite.purpose, status=AWAITING if not suite.runnable else ("有数据" if rows else NOT_RUN),
                acceptance=suite.acceptance, rows=rows, sources=srcs, manual=suite.manual,
            )
            results.append(out)
            if sid == "M1" or not suite.runnable or not rows:
                if sid == "M1":
                    out.rows = len(self.ablations())
                    out.status = "有数据" if self.ablations() else NOT_RUN
                continue
            declared = list(suite.metrics)
            names = declared + [name for (s, name) in METRICS if s == sid and name not in declared]
            for name in names:
                spec = METRICS.get((sid, name))
                if spec is None:
                    self.problems.append(f"套件 {sid} 声明的指标 {name} 报告不会算。")
                    continue
                metric = MetricResult(spec=spec, declared=name in declared, instrument=self.calc.instrument(spec.rubric) if spec.rubric else None)
                out.metrics.append(metric)
                self._fill(metric, srcs, pooled=(sid, name) in pooled_metrics or spec.group)
        return results

    def _fill(self, metric: MetricResult, srcs: Sequence[str], *, pooled: bool) -> None:
        spec = metric.spec
        base = self._present(self.bots["baseline"], srcs)
        cands = self._present(self.bots["candidates"], srcs)
        ablated = self._present(self.bots["ablated"], srcs)
        if spec.group:
            if base:
                metric.rows.append(MetricRow(BASE_POOLED, self.calc.measure(spec, base), "baseline"))
            if cands:
                metric.rows.append(MetricRow(POOLED, self.calc.measure(spec, cands), "pooled"))
            return
        for kind, labels in (("baseline", base), ("bot", cands), ("ablated", ablated)):
            for label in labels:
                metric.rows.append(MetricRow(label, self.calc.measure(spec, [label]), kind))
        if pooled and len(cands) > 1:  # (one candidate is its own pool)
            metric.rows.append(MetricRow(POOLED, self.calc.measure(spec, cands), "pooled"))


def build_report(
    run_dirs: Sequence[str | Path],
    *,
    calibration: str | Path | Mapping[str, Any] | None = None,
    gates: str | Path | Sequence[Gate] | None = None,
    baseline: str = "legacy",
    suites: Mapping[str, Suite] | None = None,
    seed: int = stats.DEFAULT_SEED,
    resamples: int = stats.DEFAULT_RESAMPLES,
    strict: bool = False,
    label: str = "",
    generated: str | None = None,
) -> Report:
    """Everything the report says, as data.  ``run_dirs``: the output directories of ``python -m bench run`` (the last one wins for a sample that is in several).
    ``calibration``: a ``calibration.json`` (or its content as a dict); ``gates``: a ``gates.yml`` (default: the one in ``bench/``) or ready-made :class:`Gate` objects.
    Raises :class:`ReportError` for an input that cannot be used."""
    try:
        pool = dict(suites) if suites is not None else load_suites()
    except SuiteError as exc:
        raise ReportError(f"套件定义有问题：{exc}") from exc
    if gates is None or isinstance(gates, (str, Path)):
        gate_list = load_gates(GATES_FILE if gates is None else gates)
        gates_file = Path(GATES_FILE if gates is None else gates).name
    else:
        gate_list, gates_file = list(gates), "(给定的门槛)"
    if calibration is None:
        cal: dict[str, Calibration] = {}
        cal_file: str | None = None
    elif isinstance(calibration, Mapping):
        cal, cal_file = parse_calibration(calibration), "(给定的校准)"
    else:
        cal, cal_file = load_calibration(calibration), Path(calibration).name
    data = load_runs(run_dirs, list(pool))
    builder = _Builder(data, pool, gate_list, cal, baseline=baseline, seed=seed, resamples=resamples, strict=strict)
    gate_results = [builder.evaluate(g) for g in gate_list]
    suite_results = builder.suite_results({(g.suite, g.metric) for g in gate_list})
    ablations = builder.ablations()
    health: dict[str, Counter[str]] = {}
    for row in data.rows:
        health.setdefault(row["bot"], Counter())[str(row.get("status") or "?")] += 1
    rubrics = sorted({item["rubric"] for item in data.items.values()} | set(cal))
    instruments = {rubric: builder.calc.instrument(rubric) for rubric in rubrics}
    counts = Counter(g.status for g in gate_results)
    verdict = FAIL if counts[FAIL] else (PASS if gate_results and counts[PASS] == len(gate_results) else "未完成")
    models = tuple(sorted({m for run in data.runs for m in run.models}))
    modes = tuple(sorted({m for run in data.runs for m in run.modes}))
    report = Report(
        label=label, baseline=baseline, seed=seed, resamples=resamples, strict=strict, generated=generated if generated is not None else date.today().isoformat(),
        runs=data.runs, counts=dict(data.counts), warnings=[], bots=builder.bots, health=health, suites=suite_results, ablations=ablations, gates=gate_results,
        instruments=instruments, calibration_file=cal_file, gates_file=gates_file, gate_counts={s: counts[s] for s in STATUSES if counts[s]}, verdict=verdict,
        models=models, modes=modes,
    )
    report.warnings = _warnings(report, data, builder, cal)
    return report


def _warnings(report: Report, data: Data, builder: _Builder, cal: Mapping[str, Calibration]) -> list[str]:
    out: list[str] = []
    counts = report.counts
    if not data.rows:
        out.append("结果里一个样本也没有：所有套件都是“未跑”。")
    if counts.get("fake"):
        out.append(f"有 {counts['fake']} 个样本来自假模型（mode=fake）：它们只说明流水线通，什么都没有测，不能用来验收；验收请用真实模型的运行。")
    if len(report.models) > 1:
        out.append(f"结果来自 {len(report.models)} 个不同的模型（{'、'.join(report.models)}）：不同模型的数字混在一起没有意义，建议分开出报告。")
    if counts.get("duplicates"):
        out.append(f"{counts['duplicates']} 个样本在多个运行里重复出现（M1 和它借用的套件跑同一批题时会这样），每个只算一次：有判定的那一份，没有就取 --results 里排在后面的。")
    if counts.get("unusable"):
        out.append(f"{counts['unusable']} 个样本出错（status=error）或预算耗尽，不计入任何数字（在“运行情况”里能看到）。")
    lost = counts.get("orphan_items", 0) + counts.get("superseded_items", 0) + counts.get("orphan_judgments", 0) + counts.get("bad_judgments", 0)
    if lost:
        out.append(f"{lost} 条裁判条目 / 判定没有计入：找不到对应的结果行、被后来的运行取代、或格式不对。")
    if counts.get("damaged"):
        out.append(f"{counts['damaged']} 行不是合法的 JSON，已跳过。")
    if counts.get("unassigned"):
        out.append(f"{counts['unassigned']} 个样本的题目没有套件标签（tags 里没有 R / S2 / L1 … ），不进任何套件的数字。")
    if data.rows and not builder.bots["baseline"]:
        out.append(f"基线 bot {report.baseline!r} 不在结果里：相对基线的门槛都是“待基线”。")
    if data.rows and not builder.bots["candidates"]:
        out.append("结果里只有基线 bot（没有候选）：所有门槛都是“未跑”。")
    judged_any = any(inst.judged for inst in report.instruments.values())
    if judged_any and not cal:
        out.append("有裁判的判定，但没有校准结果（--calibration）：这些数字都是“待校准”，不计入验收。")
    for rubric, inst in report.instruments.items():
        if inst.judged and inst.judged < inst.items:
            out.append(f"裁判 {rubric}：{inst.items} 条条目里只有 {inst.judged} 条有判定。")
        versions = data.rubric_versions.get(rubric)
        if versions and len(versions) > 1:
            out.append(f"裁判 {rubric} 的判定来自多个 rubric 版本（{'、'.join(f'v{v}' for v in sorted(versions))}）：请重新打包再判。")
    for rubric, entry in cal.items():
        if entry.flagged is True and entry.kappa is not None and entry.kappa < stats.KAPPA_THRESHOLD:
            out.append(f"校准结果里 {rubric} 标了“计入验收”，但 κ={entry.kappa:.2f} < {stats.KAPPA_THRESHOLD}：按不计入处理。")
    out.extend(dict.fromkeys(builder.problems))
    return out


# ---------------------------------------------------------------------------------------------------------------------- JSON
def _estimate_json(est: Estimate) -> dict[str, Any]:
    return {
        "value": _num(est.value), "lo": _num(est.lo), "hi": _num(est.hi), "n": _num(est.n), "items": est.items, "method": est.method,
        "extra": {k: (_num(v) if isinstance(v, float) else v) for k, v in est.extra.items()}, "notes": list(est.notes),
    }


def summary_json(report: Report) -> dict[str, Any]:
    """``summary.json``: every number of the report (no clock, no paths: the same inputs give the same file)."""
    suites: dict[str, Any] = {}
    for suite in report.suites:
        metrics: dict[str, Any] = {}
        for metric in suite.metrics:
            spec = metric.spec
            metrics[spec.name] = {
                "title": spec.title, "format": spec.fmt, "better": spec.better, "declared": metric.declared, "judge": spec.rubric or None,
                "judge_state": metric.instrument.state if metric.instrument else None,
                "bots": {
                    row.arm: {**_estimate_json(row.measured.estimate), "kind": row.kind, "state": row.measured.state, "comparable": row.measured.comparable}
                    for row in metric.rows
                },
            }
        suites[suite.id] = {
            "title": suite.title, "status": suite.status, "acceptance": suite.acceptance, "rows": suite.rows, "sources": list(suite.sources), "metrics": metrics,
        }
    return {
        "format": SUMMARY_FORMAT, "version": FILE_VERSION, "label": report.label, "baseline": report.baseline, "seed": report.seed, "resamples": report.resamples,
        "confidence": 1 - stats.DEFAULT_ALPHA,
        "inputs": {
            "runs": [
                {"name": r.name, "rows": r.rows, "items": r.items, "judgments": r.judgments, "modes": list(r.modes), "models": list(r.models)} for r in report.runs
            ],
            "calibration": report.calibration_file, "gates": report.gates_file, "counts": dict(sorted(report.counts.items())),
        },
        "models": list(report.models), "modes": list(report.modes), "warnings": list(report.warnings), "bots": report.bots,
        "health": {bot: dict(counter) for bot, counter in report.health.items()},
        "suites": suites,
        "m1": [
            {
                "bot": a.bot, "layer": a.layer, "option": a.option, "variant": a.variant, "suite": a.suite, "metric": a.metric, "status": a.status,
                "full": _estimate_json(a.full) if a.full else None, "ablated": _estimate_json(a.ablated) if a.ablated else None,
                "diff": _num(a.diff), "lo": _num(a.lo), "hi": _num(a.hi), "p_value": _num(a.p_value), "items": a.items, "reason": a.reason,
            }
            for a in report.ablations
        ],
        "judges": {
            rubric: {
                "items": inst.items, "judged": inst.judged, "version": inst.version, "state": inst.state, "note": inst.note,
                "calibration": None if inst.calibration is None else {
                    "n": inst.calibration.n, "kappa": _num(inst.calibration.kappa), "ci": [_num(inst.calibration.lo), _num(inst.calibration.hi)],
                    "counts_for_acceptance": inst.calibration.counts, "rubric_version": inst.calibration.version,
                },
            }
            for rubric, inst in report.instruments.items()
        },
    }


def gates_json(report: Report) -> dict[str, Any]:
    """``gates.json``: every gate, with its state and, for every bot, the number, the line it was held to and where the baseline stood."""
    gates = []
    for result in report.gates:
        gate = result.gate
        gates.append({
            "id": gate.id, "suite": gate.suite, "metric": gate.metric, "title": result.title, "op": gate.op, "value": gate.threshold.text if gate.threshold else None,
            "abs": gate.abs, "scope": gate.scope, "baseline": gate.baseline, "gate_status": gate.status, "note": gate.note, "status": result.status, "notes": list(result.notes),
            "bots": [
                {
                    "bot": arm.arm, "labels": list(arm.labels), "status": arm.status, "value": _num(arm.value), "lo": _num(arm.lo), "hi": _num(arm.hi), "n": _num(arm.n),
                    "items": arm.items, "method": arm.method, "baseline_value": _num(arm.baseline_value), "baseline_n": _num(arm.baseline_n), "limit": _num(arm.limit),
                    "margin": {"lo": _num(arm.margin_lo), "hi": _num(arm.margin_hi), "p_value": _num(arm.p_value)}, "crosses": arm.crosses, "would_be": arm.would_be,
                    "reason": arm.reason, **({"layers": arm.layers} if arm.layers else {}),
                }
                for arm in result.arms
            ],
        })
    return {
        "format": GATES_FORMAT, "version": FILE_VERSION, "label": report.label, "baseline": report.baseline, "strict": report.strict, "verdict": report.verdict,
        "counts": report.gate_counts, "gates": gates,
    }


# ---------------------------------------------------------------------------------------------------------------------- Markdown
def fmt_value(fmt: str, value: float | None) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    if fmt == "pct":
        return f"{value * 100:.1f}%"
    if fmt == "signed":
        return f"{value:+.3f}"
    if fmt == "p":
        return "<0.001" if value < 0.001 else f"{value:.3f}"
    return f"{value:.3f}"


def fmt_interval(fmt: str, lo: float | None, hi: float | None) -> str:
    if lo is None or hi is None:
        return "—"
    return f"[{fmt_value(fmt, lo)}, {fmt_value(fmt, hi)}]"


def _fmt_n(value: float) -> str:
    return str(int(round(value))) if abs(value - round(value)) < 1e-9 else f"{value:.1f}"


def _delta(fmt: str, amount: float) -> str:
    return f"{abs(amount) * 100:g} 个百分点" if fmt == "pct" else f"{abs(amount):g}"


def requirement(gate: Gate, title: str, fmt: str) -> str:
    """The gate in words: ``表达偏差 ≤ 0.5 × 基线``."""
    sign = {"==": "=", "<=": "≤", ">=": "≥", "<": "<", ">": ">"}.get(gate.op, gate.op)
    if gate.threshold is None:
        return f"{title}（{gate.op}）"
    t = gate.threshold
    name = f"|{title}|" if gate.abs else title
    if t.kind == "const":
        rhs = fmt_value(fmt, t.amount) if fmt != "signed" else f"{t.amount:g}"
    else:
        base = "|基线|" if gate.abs else "基线"
        if t.kind == "scale":
            rhs = base if t.amount == 1 else f"{t.amount:g} × {base}"
        else:
            rhs = f"{base} {'+' if t.amount >= 0 else '−'} {_delta(fmt, t.amount)}"
    return f"{name} {sign} {rhs}"


def _esc(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(_esc(c) for c in row) + " |" for row in rows)
    return lines


def _instrument_text(inst: Instrument | None) -> str:
    if inst is None:
        return ""
    if inst.state == "counts":
        kappa = f"κ={inst.calibration.kappa:.2f}" if inst.calibration and inst.calibration.kappa is not None else ""
        return f"裁判 {inst.rubric}：已校准{('（' + kappa + '）') if kappa else ''}，计入验收"
    if inst.state == "reference":
        return f"裁判 {inst.rubric}：{inst.note}，仅供参考"
    if inst.state == "uncalibrated":
        return f"裁判 {inst.rubric}：{inst.note}，待校准，不计入验收"
    return f"裁判 {inst.rubric}：{inst.note}"


def _bot_kind(kind: str) -> str:
    return {"baseline": "（基线）", "ablated": "（消融）", "pooled": ""}.get(kind, "")


def _estimate_notes(est: Estimate, *, small: bool = True) -> list[str]:
    notes = list(est.notes)
    if est.method == "wilson-items":
        notes.append("† 全对 / 全错，区间按题数算 Wilson")
    if small and est.value is not None and 0 < est.items < stats.SMALL_SAMPLE:
        notes.append(f"⚠ 只有 {est.items} 道题，区间仅供参考")
    return notes


def render_markdown(report: Report) -> str:
    """``report.md``."""
    lines: list[str] = []
    out = lines.append
    out(f"# Arena 基准验收报告{' · ' + report.label if report.label else ''}")
    out("")
    out(f"- 生成日期：{report.generated}")
    names = "、".join(r.name for r in report.runs) or "—"
    total_rows = sum(r.rows for r in report.runs)
    out(f"- 数据：{len(report.runs)} 个运行目录（{names}），{total_rows} 个样本，{sum(r.items for r in report.runs)} 条裁判条目，{sum(r.judgments for r in report.runs)} 条判定")
    out(f"- 模型：{'、'.join(report.models) or '—'}；模式：{'、'.join(report.modes) or '—'}")
    out(f"- 基线 bot：{'、'.join(report.bots['baseline']) or '（结果里没有）'}（--baseline {report.baseline}）；候选：{'、'.join(report.bots['candidates']) or '—'}；消融变体：{'、'.join(report.bots['ablated']) or '—'}")
    out(f"- 区间：95%，两级 bootstrap {report.resamples} 次（先重采样题，再重采样每题里的样本），种子 {report.seed}；门槛判定：{'严格（区间要全在线内）' if report.strict else '按点估计，区间跨线的标 ⚠'}")
    out(f"- 校准结果：{report.calibration_file or '没有给'}；门槛：{report.gates_file}")
    out("")
    if report.warnings:
        out("## 需要注意")
        out("")
        lines.extend(f"- ⚠ {w}" for w in report.warnings)
        out("")
    out("## 一、结论")
    out("")
    tally = "　".join(f"{status} {report.gate_counts[status]}" for status in STATUSES if status in report.gate_counts) or "没有门槛"
    out(f"门槛 {len(report.gates)} 条：{tally}。**验收结论：{report.verdict}**" + ("（有门槛没过）" if report.verdict == FAIL else "（有门槛还没有结论）" if report.verdict == "未完成" else ""))
    out("")
    lines.extend(_table(
        ["门槛", "套件", "要求", "结果", "各 bot"],
        [[g.gate.id, g.gate.suite, requirement(g.gate, g.title, g.fmt), g.status, _arm_summary(g)] for g in report.gates],
    ))
    out("")
    described = [g for g in report.gates if _gate_note(g)]
    if described:
        lines.extend(f"- **{g.gate.id}**：{_gate_note(g)}" for g in described)
        out("")
    out("### 门槛明细（每个候选 bot）")
    out("")
    rows = [_arm_row(g, arm) for g in report.gates for arm in g.arms]
    lines.extend(_table(["门槛", "bot", "值", "95% 区间", "n（题）", "基线", "门槛线", "结果", "备注"], rows) if rows else ["没有哪条门槛有候选 bot 的数据。"])
    out("")
    if any(arm.layers for g in report.gates for arm in g.arms):
        out("M1 每一层的情况见下面的“三、M1 分层消融”。")
        out("")
    out("## 二、各套件的数字")
    out("")
    out("每个数字后面是 95% 区间；n 是这个数字是在多少条发言 / 票 / 判定上算的，题是它们分布在多少道题上。")
    out("")
    for suite in report.suites:
        lines.extend(_suite_section(report, suite))
    out("## 三、M1 分层消融")
    out("")
    lines.extend(_m1_section(report))
    out("")
    out("## 四、运行情况")
    out("")
    health_rows = []
    for bot in report.bots["baseline"] + report.bots["candidates"] + report.bots["ablated"]:
        c = report.health.get(bot, Counter())
        total = sum(c.values())
        health_rows.append([bot, total, c["ok"], c["no_decision"], c["incomplete"], c["error"], f"{c['ok'] / total * 100:.0f}%" if total else "—"])
    lines.extend(_table(["bot", "样本", "有决定", "没决定", "没做完", "出错", "有决定的比例"], health_rows) if health_rows else ["没有样本。"])
    out("")
    out("## 五、裁判与校准")
    out("")
    if report.instruments:
        judge_rows = []
        for rubric, inst in report.instruments.items():
            cal = inst.calibration
            kappa = "—" if cal is None or cal.kappa is None else f"{cal.kappa:.2f} {fmt_interval('num', cal.lo, cal.hi)}"
            judge_rows.append([
                rubric, f"v{inst.version}" if inst.version is not None else "—", f"{inst.judged}/{inst.items}", kappa, "—" if cal is None or cal.n is None else cal.n,
                {"counts": "已校准，计入验收", "reference": "仅供参考", "uncalibrated": "待校准", "unjudged": "待裁判"}[inst.state], inst.note,
            ])
        lines.extend(_table(["rubric", "版本", "判定 / 条目", "κ 及区间", "校准集 n", "状态", "说明"], judge_rows))
    else:
        out("这次的结果里没有裁判条目。")
    out("")
    out("裁判的结果只有在这个 rubric 校准通过（κ ≥ 0.6，H48）时才计入验收；没有判定是“待裁判”，没有校准是“待校准”，校准了但没到 0.6 是“仅供参考”。")
    out("")
    out("## 六、口径")
    out("")
    lines.extend(_notes())
    out("")
    return "\n".join(lines)


def _gate_note(result: GateResult) -> str:
    """What the gate's own line in ``gates.yml`` says about it (and why it has no verdict, when it has none)."""
    gate = result.gate
    parts = []
    if gate.note:
        parts.append(gate.note)
        if gate.status == "provisional":
            parts.append("（门槛初拟）")
        elif gate.status == "pending_baseline":
            parts.append("（门槛待基线定稿）")
    parts.extend(result.notes)
    return " ".join(parts)


def _arm_summary(result: GateResult) -> str:
    """One cell: the state of the gate for each bot (``bot:Alice 通过；bot:Bob 未通过``)."""
    if not result.arms:
        return ""
    parts = []
    for arm in result.arms:
        text = f"{arm.arm} {arm.status}" + (" ⚠" if arm.crosses and arm.status in (PASS, FAIL) else "")
        if arm.would_be:
            text += f"（若计入：{arm.would_be}）"
        parts.append(text)
    return "；".join(parts)


def _limit_text(g: GateResult, arm: ArmResult) -> str:
    if arm.limit is None:
        return "—"
    sign = {"==": "=", "<=": "≤", ">=": "≥", "<": "<", ">": ">"}.get(g.gate.op, g.gate.op)
    return f"{'|值| ' if g.gate.abs else ''}{sign} {fmt_value(g.fmt if g.fmt != 'signed' or not g.gate.abs else 'num', arm.limit)}"


def _arm_row(g: GateResult, arm: ArmResult) -> list[Any]:
    fmt = g.fmt
    status = arm.status + (" ⚠" if arm.crosses and arm.status in (PASS, FAIL) else "")
    if arm.layers:
        return [g.gate.id, arm.arm, "—", "—", "—", "—", "—", status, arm.reason]
    base = fmt_value(fmt, arm.baseline_value) if arm.baseline_value is not None else (f"存档 {fmt_value(fmt, g.gate.baseline)}" if g.gate.baseline is not None else "—")
    notes = []
    if arm.would_be:
        notes.append(f"若计入：{arm.would_be}")
    if arm.reason:
        notes.append(arm.reason)
    if arm.p_value is not None:
        notes.append(f"p={fmt_value('p', arm.p_value)}")
    n = f"{_fmt_n(arm.n)}（{arm.items}）" if arm.value is not None else "—"
    return [g.gate.id, arm.arm, fmt_value(fmt, arm.value), fmt_interval(fmt, arm.lo, arm.hi) + ("†" if arm.method == "wilson-items" else ""), n, base, _limit_text(g, arm), status, "；".join(notes)]


def _suite_section(report: Report, suite: SuiteResult) -> list[str]:
    lines = [f"### {suite.id} {suite.title}" + ("" if suite.acceptance else "（不进验收，仅供参考）"), ""]
    if suite.purpose:
        lines += [suite.purpose, ""]
    if suite.status == AWAITING:
        lines += [f"**{AWAITING}**：这个套件不由 `run` 运行，本版报告不聚合它的数据。" + (f"做法：{suite.manual}" if suite.manual else ""), ""]
        return lines
    if suite.status == NOT_RUN:
        lines += [f"**{NOT_RUN}**：结果里没有这个套件的样本（不是 0）。", ""]
        return lines
    if suite.id == "M1":
        lines += ["见“三、M1 分层消融”。", ""]
        return lines
    if suite.sources and suite.sources != (suite.id,):
        lines += [f"（数字取自 {'、'.join(suite.sources)} 的样本。）", ""]
    rows = []
    for metric in suite.metrics:
        spec = metric.spec
        label = spec.title + ("" if metric.declared else "（补充）")
        if not metric.rows:
            rows.append([label, "—", "—", "—", "—", "—", "没有数据"])
            continue
        for index, row in enumerate(metric.rows):
            est = row.measured.estimate
            notes = _estimate_notes(est)
            if est.value is None and not notes:
                notes.append("没有数据")
            if spec.rubric and index == 0:
                notes.insert(0, _instrument_text(metric.instrument))  # (what the judge's verdicts are worth is said once per number)
            if not row.measured.comparable and est.value is not None:
                notes.append("不可比")
            rows.append([
                label if index == 0 else "", row.arm + _bot_kind(row.kind), fmt_value(spec.fmt, est.value),
                fmt_interval(spec.fmt, est.lo, est.hi) + ("†" if est.method == "wilson-items" else ""),
                _fmt_n(est.n) if est.value is not None else "—", est.items if est.value is not None else "—", "；".join(n for n in notes if n),
            ])
    lines += _table(["指标", "bot", "值", "95% 区间", "n", "题", "备注"], rows)
    lines.append("")
    return lines


def _m1_section(report: Report) -> list[str]:
    suite = next((s for s in report.suites if s.id == "M1"), None)
    if suite is not None and suite.status == NOT_RUN and not report.ablations:
        return [f"**{NOT_RUN}**：结果里没有消融变体（bot@no-… 之类）的样本。"]
    rows = []
    for a in report.ablations:
        rows.append([
            a.bot, a.layer, a.variant, f"{a.suite} · {a.title}", fmt_value(a.fmt, a.full.value if a.full else None), fmt_value(a.fmt, a.ablated.value if a.ablated else None),
            _fmt_diff(a), "—" if a.p_value is None else fmt_value("p", a.p_value), a.items or "—", a.status, a.reason,
        ])
    return _table(["bot", "拿掉的层", "拿掉后的版本", "看的指标", "完整", "拿掉后", "差（完整 − 拿掉）", "p（完整更好）", "题", "结果", "备注"], rows) + [
        "",
        "p 是单侧的：完整的 bot 比拿掉这一层的更好，p < 0.05 才算这一层“起作用”（门槛的写法见 gates.yml）。“其他指标不变差”这一半本报告不检查。",
    ]


def _fmt_diff(a: AblationResult) -> str:
    """The difference of an ablation with its interval: percentage points for a rate."""
    if a.diff is None:
        return "—"
    if a.fmt == "pct":
        return f"{a.diff * 100:+.1f} [{(a.lo or 0) * 100:+.1f}, {(a.hi or 0) * 100:+.1f}] 个百分点"
    return f"{a.diff:+.3f} [{(a.lo or 0):+.3f}, {(a.hi or 0):+.3f}]"


def _notes() -> list[str]:
    return [
        "- **状态**：通过 / 未通过按点估计与门槛线比较，区间跨过门槛线的标 ⚠（样本不够下结论；`--strict` 把这样的“通过”算成未通过）；"
        "待基线 = 门槛是相对基线的，结果里没有基线 bot 的数据；待裁判 = 这个数字要裁判的判定，还没有 judgments.jsonl；待校准 = 有判定，但这个 rubric 还没有校准结果；"
        "仅供参考 = rubric 校准了但 κ < 0.6，或者套件本身不进验收；未跑 = 没有数据（不是 0）；待数据 = 套件不由 run 运行（E1 / E2 / G）。",
        "- **一个门槛对每个候选 bot 各判一次，取最差的**（失败 > 未跑 > 待数据 > 待裁判 > 待校准 > 待基线 > 仅供参考 > 通过）；S2、S3 这类整体的指标按“候选合计”判。"
        "门槛本身的状态（gates.yml 的 status）：初拟 = 等真实数据出来再收紧；待基线定稿 = 数字要等基线 bot 在同一批题上的结果出来再定。",
        "- **区间**：95%，两级 bootstrap（先重采样题，再重采样每题里的样本；同一道题的几次采样不是独立的）。全对 / 全错时 bootstrap 没有可变的东西，区间改用 Wilson（按题数算，保守），标 †。",
        "- **正确率**（L2 / L3 / L4 / M2 / M3）= 答对的 / 被问到的：没作答、弃票都按错算；样本出错（status=error）或预算耗尽的不计。",
        "- **L1 表达偏差** = P(信“会说的”一方) − 0.5：同一对题的 A、B 两版（同一个 bot、同一个采样号）都站了边才算；投给“不会说的”一方 = 信了会说的一方。"
        "只看事实投票的 bot 两版都投对，偏差 0；永远信会说的一方是 +0.5。弃票、投给第三人、缺一版的整对不计，数量在备注里。门槛按绝对值比（偏向“不会说的”一方同样是偏差）。",
        "- **L2 被琐碎点带偏** = 裁判判 basis = trivia 的比例。**L3 抓住决定性事实** = cites_decisive 为真，或 basis = decisive_fact（bot 明确说出了决定性事实，不一定是主要依据；"
        "只算主要依据的是“补充”里的 decisive_main_rate）。**S2** = 裁判点的风格卡是不是说话的那个 bot 的（随机水平 1 / 卡数）。",
        "- **S1** 用每条发言自带的检查结果（bot 自己声明允许口头列举时，不算分条）；**R** 的截断数来自账本，重写成功率 = 被退回后下一次就发出去的比例；S1 / S3 的样本取自 S2 的题。",
        "- **S3 共用片段占比**是整组 bot 的数字，要 4 个以上的 bot 才和 12 座位的基线（0.011）可比；不到的标“不可比”。",
        "- 同一个样本（题、座位、bot、采样号）出现在几个运行里只算一次：有裁判判定的那一份，没有就取 --results 里排在后面的。原始文件在 raw/ 里（H51）。",
    ]


# ---------------------------------------------------------------------------------------------------------------------- writing
def write_report(report: Report, out_dir: str | Path, *, run_dirs: Sequence[str | Path], calibration: str | Path | None = None, gates: str | Path | None = None,
                 force: bool = False) -> list[Path]:
    """Write ``report.md``, ``summary.json``, ``gates.json`` and ``raw/`` into ``out_dir`` (made if need be).  An existing ``out_dir`` raises
    :class:`ReportError` unless ``force``: then the three files are replaced and ``raw/`` is made anew (other files in the directory are left alone)."""
    out = Path(out_dir)
    if out.exists() and not force:
        raise ReportError(f"输出目录 {out} 已经有了：加 --force 才会覆盖（报告要提交的，不要悄悄改掉旧的）。")
    if out.exists() and not out.is_dir():
        raise ReportError(f"输出路径 {out} 是个文件，不是目录。")
    markdown = render_markdown(report)
    summary = json.dumps(summary_json(report), ensure_ascii=False, indent=1) + "\n"
    gate_text = json.dumps(gates_json(report), ensure_ascii=False, indent=1) + "\n"
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in (("report.md", markdown), ("summary.json", summary), ("gates.json", gate_text)):
        target = out / name
        target.write_text(text, encoding="utf-8", newline="\n")
        written.append(target)
    raw = out / "raw"
    if raw.exists():
        shutil.rmtree(raw)
    raw.mkdir()
    for info in report.runs:
        target_dir = raw / info.name
        target_dir.mkdir()
        for name in RAW_FILES:
            source = info.path / name
            if source.is_file():
                shutil.copyfile(source, target_dir / name)
                written.append(target_dir / name)
    if calibration is not None and Path(calibration).is_file():
        shutil.copyfile(calibration, raw / "calibration.json")
        written.append(raw / "calibration.json")
    gate_source = Path(GATES_FILE if gates is None else gates)
    if gate_source.is_file():
        shutil.copyfile(gate_source, raw / "gates.yml")
        written.append(raw / "gates.yml")
    return written


# ---------------------------------------------------------------------------------------------------------------------- command line
def _print(text: str = "") -> None:
    print(text, flush=True)


def add_commands(sub: Any) -> None:
    report = sub.add_parser("report", help="the acceptance report: every suite's numbers with intervals, the gates of gates.yml checked (-> report.md, summary.json, gates.json, raw/)")
    report.add_argument("--results", required=True, action="extend", nargs="+", type=Path, help="run directories (the --out of `run`): results.jsonl, judge_items.jsonl, judgments.jsonl")
    report.add_argument("--calibration", type=Path, help="calibration.json (python -m bench calibrate): without it the judges' numbers are 待校准")
    report.add_argument("--gates", type=Path, help="the gates (default: bench/gates.yml)")
    report.add_argument("--baseline", default="legacy", help="the bot the others are compared with (default: legacy)")
    report.add_argument("--out", required=True, type=Path, help="where the report goes, e.g. bench/reports/v0.14.0/ (an existing directory needs --force)")
    report.add_argument("--force", action="store_true", help="overwrite report.md, summary.json, gates.json and raw/ of an existing --out")
    report.add_argument("--label", help="the name in the title (default: the name of --out)")
    report.add_argument("--seed", type=int, default=stats.DEFAULT_SEED, help="the bootstrap seed (the same data and seed give the same report)")
    report.add_argument("--resamples", type=int, default=stats.DEFAULT_RESAMPLES, help="bootstrap resamples per number (default 2000)")
    report.add_argument("--strict", action="store_true", help="a gate is passed only when its whole interval is on the right side of the line")


def cmd_report(args: argparse.Namespace) -> int:
    """Everything that can be refused is refused before anything is written (exit 2); an input that cannot be used is exit 1."""
    out: Path = args.out
    try:
        refuse_real_data_dir(out)
    except UnsafeDataDir as exc:
        _print(str(exc))
        return 2
    if args.resamples < 50:
        _print("--resamples 至少要 50。")
        return 2
    sources = {(Path(p).parent if Path(p).name == "results.jsonl" and Path(p).is_file() else Path(p)).resolve() for p in args.results}
    if out.resolve() in sources:
        _print("--out 不能就是 --results 里的运行目录（报告会把原始文件拷进 raw/，不能拷到自己身上）。")
        return 2
    if out.exists() and not out.is_dir():
        _print(f"输出路径 {out} 是个文件，不是目录。")
        return 2
    if out.exists() and not args.force:
        _print(f"输出目录 {out} 已经有了：加 --force 才会覆盖（报告要提交的，不要悄悄改掉旧的）。")
        return 2
    try:
        label = args.label or out.resolve().name
        report = build_report(
            args.results, calibration=args.calibration, gates=args.gates, baseline=args.baseline, seed=args.seed, resamples=args.resamples, strict=args.strict, label=label,
        )
        written = write_report(report, out, run_dirs=args.results, calibration=args.calibration, gates=args.gates, force=args.force)
    except ReportError as exc:
        _print(str(exc))
        return 1
    for warning in report.warnings:
        _print(f"⚠ {warning}")
    _print("门槛：" + ("　".join(f"{s} {report.gate_counts[s]}" for s in STATUSES if s in report.gate_counts) or "没有") + f"　验收结论：{report.verdict}")
    for result in report.gates:
        _print(f"  {result.gate.id:<4} {result.status:<5} {requirement(result.gate, result.title, result.fmt)}")
    _print(f"报告写到 {out}（{len(written)} 个文件，原始数据在 raw/）")
    return 0


HANDLERS = {"report": cmd_report}


__all__ = [
    "AWAITING",
    "BASE_POOLED",
    "FAIL",
    "HANDLERS",
    "METRICS",
    "NOT_RUN",
    "NO_BASELINE",
    "PASS",
    "POOLED",
    "REFERENCE",
    "STATUSES",
    "UNCALIBRATED",
    "UNJUDGED",
    "Calibration",
    "Data",
    "Estimate",
    "Gate",
    "MetricSpec",
    "Report",
    "ReportError",
    "Threshold",
    "add_commands",
    "answer_state",
    "build_report",
    "classify",
    "cmd_report",
    "gates_json",
    "is_baseline_label",
    "l1_side",
    "load_calibration",
    "load_gates",
    "load_runs",
    "pair_of",
    "parse_calibration",
    "parse_gates",
    "parse_threshold",
    "render_markdown",
    "rollup",
    "split_label",
    "summary_json",
    "suite_of",
    "write_report",
]
