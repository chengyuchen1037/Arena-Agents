"""Suite definitions (``bench/suites/<suite>.yml``): which situations a suite is made of, which bots it compares, how many samples a *tier* takes, which
judges and gates belong to it - and what a run of it costs.

A suite is a name for "a set of questions + the bots to put them to + how to score the answers".  The tiers (docs/v0.14-plan.md §6.5: three price classes)::

    smoke   CI / a first look: a few situations, 0 calls to a real model (the fake one)
    quick   what one runs after changing a prompt or a memory: every situation, one sample, the baseline and one v0.14 bot; all suites together ~300 calls
    full    what a release is judged on: every situation, k samples, more bots

``python -m bench suites`` lists them with the number of situations and the calls each tier would make; ``python -m bench run --suite L1 --tier quick`` runs one
(the real-model rules of ``run`` are unchanged: ``--real`` and ``--max-calls`` or nothing).  Suites that are not situations (E1 learning curve, E2 style from
people, G league) are defined too - what they need and how they are done - but ``run`` does not run them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bench.situation import Situation, SituationError, load_situation

BENCH_DIR = Path(__file__).resolve().parent
SUITES_DIR = BENCH_DIR / "suites"
JUDGES_DIR = BENCH_DIR / "judges"
GATES_FILE = BENCH_DIR / "gates.yml"
SUITE_FORMAT = "arena-bench-suite"
SUITE_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{0,3}$")
TIERS = ("smoke", "quick", "full")
_TOP_KEYS = {
    "format", "version", "id", "title", "purpose", "runnable", "acceptance", "fixtures", "pairs", "uses", "decision", "judges", "metrics", "gates", "tiers",
    "calls_per_sample", "baseline", "candidate", "sources", "requires", "manual", "notes", "ablations", "seats",
}
_TIER_KEYS = {"samples", "bots", "situations", "calls_per_sample", "note"}
DEFAULT_CALLS_PER_SAMPLE = 1.3  # a decision is one call, plus the rewrite of a refused speech now and then


class SuiteError(ValueError):
    """A suite definition that cannot be used; the message names the file and the key."""


@dataclass(frozen=True)
class Tier:
    name: str
    samples: int
    bots: tuple[str, ...]
    situations: int | None = None  # at most this many situations (the first ones by id); None = all
    calls_per_sample: float | None = None
    note: str = ""


@dataclass(frozen=True)
class Suite:
    id: str
    title: str
    purpose: str = ""
    runnable: bool = True
    acceptance: bool = True  # counted in the acceptance report (its gates), or reference only
    fixtures: tuple[str, ...] = ()  # globs below bench/
    pairs: str = ""  # glob of pair files below bench/ (L1)
    uses: str = ""  # run the fixtures of this other suite (S1 and S3 are numbers over what S2 collects)
    decision: str = ""  # vote | speech | quiz | ... (what the bot is asked to do)
    judges: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    gates: tuple[str, ...] = ()
    tiers: Mapping[str, Tier] = field(default_factory=dict)
    calls_per_sample: float = DEFAULT_CALLS_PER_SAMPLE
    baseline: tuple[str, ...] = ("legacy",)
    candidate: tuple[str, ...] = ("bot:Alice",)
    sources: tuple[str, ...] = ()  # numbers over the rows of these suites (R, S1, S3)
    requires: str = ""
    manual: str = ""  # how a suite that ``run`` does not run is done
    notes: str = ""
    ablations: tuple[Mapping[str, Any], ...] = ()  # M1: {layer, option, suite, metric, situations?}: the candidate with and without one layer, on the fixtures of that suite
    path: str = ""


# ---------------------------------------------------------------------------------------------------------------------- reading
def _fail(source: str, message: str) -> SuiteError:
    return SuiteError(f"{source}：{message}")


def _strings(value: Any, source: str, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise _fail(source, f"{what} 必须是字符串列表。")
    return tuple(v.strip() for v in value)


def _ablations(raw: Any, source: str) -> tuple[Mapping[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise _fail(source, "ablations 必须是一个列表。")
    out = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping) or set(item) - {"layer", "option", "suite", "metric", "situations", "note"}:
            raise _fail(source, f"ablations[{index}] 只能有 layer、option、suite、metric、situations、note。")
        for key in ("layer", "option", "suite", "metric"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise _fail(source, f"ablations[{index}].{key} 必须是非空字符串。")
        limit = item.get("situations")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise _fail(source, f"ablations[{index}].situations 必须是正整数。")
        out.append(dict(item))
    return tuple(out)


def parse_suite(data: Any, *, source: str = "") -> Suite:
    """A suite from the loaded YAML.  Raises :class:`SuiteError`."""
    if not isinstance(data, Mapping):
        raise _fail(source, "文件的顶层必须是一个字典。")
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise _fail(source, f"不认识的键：{'、'.join(map(str, unknown))}。")
    if data.get("format") != SUITE_FORMAT or data.get("version") != 1:
        raise _fail(source, f"format 必须是 {SUITE_FORMAT}，version 必须是 1。")
    suite_id = data.get("id")
    if not isinstance(suite_id, str) or not SUITE_ID_RE.match(suite_id):
        raise _fail(source, "id 必须是套件名（大写字母开头，例如 L1、S2、M3、G）。")
    runnable = data.get("runnable", True)
    acceptance = data.get("acceptance", True)
    if not isinstance(runnable, bool) or not isinstance(acceptance, bool):
        raise _fail(source, "runnable 和 acceptance 必须是 true / false。")
    tiers: dict[str, Tier] = {}
    raw_tiers = data.get("tiers") or {}
    if not isinstance(raw_tiers, Mapping):
        raise _fail(source, "tiers 必须是一个字典（smoke / quick / full）。")
    for name, body in raw_tiers.items():
        if name not in TIERS:
            raise _fail(source, f"tiers 里不认识的档位 {name!r}（只有 {'、'.join(TIERS)}）。")
        if not isinstance(body, Mapping) or set(body) - _TIER_KEYS:
            raise _fail(source, f"tiers.{name} 只能有：{'、'.join(sorted(_TIER_KEYS))}。")
        samples = body.get("samples")
        if isinstance(samples, bool) or not isinstance(samples, int) or samples < 1:
            raise _fail(source, f"tiers.{name}.samples 必须是正整数。")
        limit = body.get("situations")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise _fail(source, f"tiers.{name}.situations 必须是正整数（或不写：全部）。")
        cps = body.get("calls_per_sample")
        if cps is not None and (isinstance(cps, bool) or not isinstance(cps, (int, float)) or cps <= 0):
            raise _fail(source, f"tiers.{name}.calls_per_sample 必须是正数。")
        bots = _strings(body.get("bots"), source, f"tiers.{name}.bots")
        if not bots:
            raise _fail(source, f"tiers.{name}.bots 不能是空的。")
        tiers[name] = Tier(name, samples, bots, limit, float(cps) if cps is not None else None, str(body.get("note") or ""))
    ablations = _ablations(data.get("ablations"), source)
    if runnable and not data.get("uses") and not data.get("fixtures") and not ablations:
        raise _fail(source, "能跑的套件要写 fixtures（题目的 glob）、uses（借用另一个套件的题）或 ablations（逐层消融）。")
    if runnable and set(tiers) != set(TIERS):
        raise _fail(source, f"能跑的套件要写全三档：{'、'.join(TIERS)}。")
    if not runnable and not data.get("manual"):
        raise _fail(source, "不能用 run 跑的套件要写 manual：它是怎么做的、需要什么。")
    cps_suite = data.get("calls_per_sample", DEFAULT_CALLS_PER_SAMPLE)
    if isinstance(cps_suite, bool) or not isinstance(cps_suite, (int, float)) or cps_suite <= 0:
        raise _fail(source, "calls_per_sample 必须是正数。")
    return Suite(
        id=suite_id, title=str(data.get("title") or suite_id), purpose=str(data.get("purpose") or "").strip(), runnable=runnable, acceptance=acceptance,
        fixtures=_strings(data.get("fixtures"), source, "fixtures"), pairs=str(data.get("pairs") or ""), uses=str(data.get("uses") or ""),
        decision=str(data.get("decision") or ""), judges=_strings(data.get("judges"), source, "judges"), metrics=_strings(data.get("metrics"), source, "metrics"),
        gates=_strings(data.get("gates"), source, "gates"), tiers=tiers, calls_per_sample=float(cps_suite),
        baseline=_strings(data.get("baseline"), source, "baseline") or ("legacy",), candidate=_strings(data.get("candidate"), source, "candidate") or ("bot:Alice",),
        sources=_strings(data.get("sources"), source, "sources"), requires=str(data.get("requires") or "").strip(), manual=str(data.get("manual") or "").strip(),
        notes=str(data.get("notes") or "").strip(), ablations=ablations, path=source,
    )


def load_suite(path: str | Path) -> Suite:
    target = Path(path)
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise SuiteError(f"{target.name}：读不了这个文件（{exc}）。") from exc
    except yaml.YAMLError as exc:
        raise SuiteError(f"{target.name}：不是合法的 YAML。") from exc
    suite = parse_suite(data, source=target.name)
    if suite.id != target.stem.upper() and suite.id != target.stem:
        raise SuiteError(f"{target.name}：文件名要和 id 一致（{suite.id}.yml）。")
    return Suite(**{**suite.__dict__, "path": str(target)})


def load_suites(directory: str | Path = SUITES_DIR) -> dict[str, Suite]:
    """Every suite below ``directory`` by id (in the order of the plan: R S1 S2 S3 L1 L2 L3 L4 M1 M2 M3 E1 E2 G)."""
    found: dict[str, Suite] = {}
    for path in sorted(Path(directory).glob("*.yml")):
        suite = load_suite(path)
        if suite.id in found:
            raise SuiteError(f"{path.name}：套件 {suite.id} 重复了。")
        found[suite.id] = suite
    for suite in found.values():
        if suite.uses and suite.uses not in found:
            raise SuiteError(f"{Path(suite.path).name}：uses 指向的套件 {suite.uses!r} 不存在。")
    order = ["R", "S1", "S2", "S3", "L1", "L2", "L3", "L4", "M1", "M2", "M3", "E1", "E2", "G"]
    return {sid: found[sid] for sid in sorted(found, key=lambda s: (order.index(s) if s in order else len(order), s))}


# ---------------------------------------------------------------------------------------------------------------------- resolving
def fixture_files(suite: Suite, suites: Mapping[str, Suite] | None = None, *, root: Path = BENCH_DIR) -> list[Path]:
    """The situation files of a suite (``fixtures`` globs below ``root``, or those of the suite it ``uses``), sorted by file name."""
    if suite.uses:
        pool = suites if suites is not None else load_suites()
        return fixture_files(pool[suite.uses], pool, root=root)
    files: list[Path] = []
    for pattern in suite.fixtures:
        files.extend(sorted(p for p in root.glob(pattern) if p.is_file()))
    return list(dict.fromkeys(files))


def suite_situations(suite: Suite, suites: Mapping[str, Suite] | None = None, *, root: Path = BENCH_DIR) -> list[Situation]:
    """The situations of a suite, loaded (sorted by id).  Raises :class:`~bench.situation.SituationError` for a file that cannot be read."""
    situations = [load_situation(path) for path in fixture_files(suite, suites, root=root)]
    ids = [s.id for s in situations]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise SituationError(f"套件 {suite.id}：题目 id 重复：{'、'.join(duplicated)}。")
    return sorted(situations, key=lambda s: s.id)


def tier_situations(suite: Suite, tier: str, suites: Mapping[str, Suite] | None = None, *, root: Path = BENCH_DIR) -> list[Situation]:
    """The situations a tier runs (``situations: N`` keeps the first N by id)."""
    spec = suite.tiers[tier]
    situations = suite_situations(suite, suites, root=root)
    return situations[: spec.situations] if spec.situations else situations


def with_option(spec: str, option: str) -> str:
    """``bot:Alice`` + ``no-people`` -> ``bot:Alice@no-people``;  ``bot:Alice@nomem`` + ``no-people`` -> ``bot:Alice@nomem,no-people``."""
    return f"{spec},{option}" if "@" in spec else f"{spec}@{option}"


@dataclass(frozen=True)
class Group:
    """Some situations put to some bots (a suite is one group; M1 is one group per ablation)."""

    situations: tuple[Situation, ...]
    bots: tuple[str, ...]
    layer: str = ""


@dataclass(frozen=True)
class Plan:
    """What ``run --suite X --tier T`` runs: the groups and the number of samples of each (bot, situation, seat)."""

    suite: str
    tier: str
    samples: int
    groups: tuple[Group, ...]

    def specs(self) -> list[str]:
        """Every bot spec of the plan, once, in order of appearance."""
        return list(dict.fromkeys(spec for group in self.groups for spec in group.bots))

    def situations(self) -> list[Situation]:
        """Every situation of the plan, once, in order of appearance."""
        seen: dict[str, Situation] = {}
        for group in self.groups:
            for situation in group.situations:
                seen.setdefault(situation.id, situation)
        return list(seen.values())


def plan(suite: Suite, tier: str, suites: Mapping[str, Suite] | None = None, *, root: Path = BENCH_DIR) -> Plan:
    """The plan of a tier of a runnable suite.  Raises :class:`SuiteError` for a suite that ``run`` does not run."""
    if not suite.runnable:
        raise SuiteError(f"套件 {suite.id} 不是靠 run 跑的：{suite.manual}")
    spec = suite.tiers[tier]
    pool = suites if suites is not None else load_suites()
    if not suite.ablations:
        return Plan(suite.id, tier, spec.samples, (Group(tuple(tier_situations(suite, tier, pool, root=root)), spec.bots),))
    groups = []
    for ablation in suite.ablations:
        target = pool.get(ablation["suite"])
        if target is None:
            raise SuiteError(f"套件 {suite.id}：ablations 指向的套件 {ablation['suite']!r} 不存在。")
        situations = suite_situations(target, pool, root=root)
        limits = [n for n in (ablation.get("situations"), spec.situations) if n]  # (the ablation's own number, and the tier's cap)
        if limits:
            situations = situations[: min(limits)]
        bots = tuple(dict.fromkeys(spec_ for candidate in spec.bots for spec_ in (candidate, with_option(candidate, ablation["option"]))))
        groups.append(Group(tuple(situations), bots, ablation["layer"]))
    return Plan(suite.id, tier, spec.samples, tuple(groups))


def decision_seat_count(situation: Situation) -> int:
    """How many seats a situation asks a decision of."""
    return len(situation.decision.seats)


@dataclass(frozen=True)
class Estimate:
    suite: str
    tier: str
    situations: int
    pairs: int
    bots: int
    samples: int
    jobs: int  # samples that would be started
    calls: int  # model calls they would make (an estimate: jobs × calls per sample)


def _bot_count(specs: Sequence[str]) -> int:
    """How many bots a list of specs stands for (``default12`` is twelve; a library that is not there counts the groups once)."""
    from bench.bots import BotSpecError, expand_bot_specs

    try:
        return len(expand_bot_specs(list(specs)))
    except BotSpecError:
        return len(specs)


def estimate(suite: Suite, tier: str, suites: Mapping[str, Suite] | None = None, *, root: Path = BENCH_DIR) -> Estimate:
    """What a tier of a suite costs: the situations it takes × the bots × the samples, and the calls those make (an estimate: ``calls_per_sample`` of the
    tier, else of the suite).  A situation that pins its own bots (``bots:``) counts those, not the tier's."""
    spec = suite.tiers[tier]
    chosen = plan(suite, tier, suites, root=root)
    jobs = 0
    for group in chosen.groups:
        for situation in group.situations:
            for seat in situation.decision.seats:
                jobs += (1 if seat in situation.bots else _bot_count(group.bots)) * chosen.samples
    cps = spec.calls_per_sample or suite.calls_per_sample
    pair_count = len(list(root.glob(suite.pairs))) if suite.pairs else 0
    return Estimate(suite.id, tier, len(chosen.situations()), pair_count, _bot_count(chosen.specs()), chosen.samples, jobs, round(jobs * cps))


def estimates(suites: Mapping[str, Suite] | None = None, *, root: Path = BENCH_DIR) -> dict[str, dict[str, Estimate]]:
    """``{suite id: {tier: Estimate}}`` for every runnable suite."""
    pool = suites if suites is not None else load_suites()
    return {sid: {tier: estimate(suite, tier, pool, root=root) for tier in TIERS} for sid, suite in pool.items() if suite.runnable}


def gate_ids(path: str | Path = GATES_FILE) -> set[str]:
    """The ids of the gates of ``gates.yml``."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set()
    return {str(g.get("id")) for g in data.get("gates") or [] if isinstance(g, Mapping) and g.get("id")}


def check_suites(suites: Mapping[str, Suite], *, judges_dir: Path = JUDGES_DIR, gates_file: Path = GATES_FILE, root: Path = BENCH_DIR) -> list[str]:
    """Everything a suite definition promises that the repository must back: files for its globs, rubrics for its judges, gates for its gates."""
    problems: list[str] = []
    known_gates = gate_ids(gates_file)
    for suite in suites.values():
        for ablation in suite.ablations:
            if ablation["suite"] not in suites:
                problems.append(f"{suite.id}：ablations 指向的套件 {ablation['suite']} 不存在。")
        if suite.runnable and not suite.ablations and not fixture_files(suite, suites, root=root):
            problems.append(f"{suite.id}：fixtures 没有匹配到任何文件（{', '.join(suite.fixtures) or 'uses ' + suite.uses}）。")
        for rubric in suite.judges:
            if not (judges_dir / f"{rubric}.md").is_file():
                problems.append(f"{suite.id}：judges 里的 {rubric} 没有 {judges_dir.name}/{rubric}.md。")
        for gate in suite.gates:
            if gate not in known_gates:
                problems.append(f"{suite.id}：gates 里的 {gate} 不在 gates.yml 里。")
        if suite.pairs and not list(root.glob(suite.pairs)):
            problems.append(f"{suite.id}：pairs 没有匹配到任何文件（{suite.pairs}）。")
    return problems


def describe_runs(suite_ids: Iterable[str], suites: Mapping[str, Suite]) -> list[str]:
    """For the command line: the suites that cannot be run by ``run``, with what they need (one line each)."""
    lines = []
    for sid in suite_ids:
        suite = suites[sid]
        if not suite.runnable:
            lines.append(f"{sid} 不是靠 `run` 跑的：{suite.manual}")
    return lines


__all__ = [
    "BENCH_DIR",
    "DEFAULT_CALLS_PER_SAMPLE",
    "Estimate",
    "Group",
    "Plan",
    "SUITES_DIR",
    "Suite",
    "SuiteError",
    "TIERS",
    "Tier",
    "check_suites",
    "decision_seat_count",
    "describe_runs",
    "estimate",
    "estimates",
    "fixture_files",
    "gate_ids",
    "load_suite",
    "load_suites",
    "parse_suite",
    "plan",
    "with_option",
    "suite_situations",
    "tier_situations",
]
