"""The commands of ``python -m bench`` that run things: ``situations``, ``run``, ``summarize`` and ``replay`` (``audit`` / ``baseline`` are in
``bench.__main__``).

``run`` is the runner: it plays situations on the real engine and runs the real agent loop of each bot on them (see ``bench.runner``).  Its default
model is the FAKE one (scripted, no network, no key).  A real model needs ``--real`` AND ``--max-calls`` (the brake), ``--model``, ``--base-url`` and a
key in an environment variable (``--api-key-env``): the benchmark never reads the app's provider table, and while a run goes on ``ARENA_DATA_DIR``
points at a scratch directory inside the run's output directory - the real ``data/`` is neither read nor written.  A run that is refused (a missing
flag, a missing key, an unsafe output directory) is refused before anything is made: no directory, no connection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from bench import fakes
from bench.bots import BotSpecError, make_bots
from bench.loaders import load_game
from bench.play import PlayError, build_scene
from bench.realllm import DEFAULT_KEY_ENV, PROTOCOLS, REASONING, RealConfig, RealModeError, build_real_port, load_provider_entry
from bench.replay import (
    decision_points,
    replay_game,
    report_text,
    situation_from_game,
    situation_to_yaml,
)
from bench.runner import Budget, RunConfig, jobs_for, new_run_id, read_results, run_info, run_suite
from bench.safety import UnsafeDataDir, default_out_dir, isolated_data_dir, refuse_real_data_dir
from bench.bots import expand_bot_specs
from bench.situation import Situation, SituationError, load_situations, parse_situation
from bench.suite import TIERS, Group, Plan, SuiteError, check_suites, estimates, load_suites, plan
from bench.summary import format_summary, summarize

HERE = Path(__file__).resolve().parent
DEFAULT_SITUATIONS = HERE / "fixtures" / "situations"
BASE_URL_ENV = "ARENA_BENCH_BASE_URL"
MODEL_ENV = "ARENA_BENCH_MODEL"


def _print(text: str = "") -> None:
    print(text, flush=True)


def _split(values: Sequence[str] | None) -> list[str]:
    return [item.strip() for raw in values or [] for item in str(raw).split(",") if item.strip()]


# ---------------------------------------------------------------------------------------------------------------------- registration
def add_commands(sub: Any) -> None:
    situations = sub.add_parser("situations", help="list the situation scripts (--check plays each one on the real engine)")
    situations.add_argument("--situations", action="append", type=Path, help="a file or a directory (default: bench/fixtures/situations)")
    situations.add_argument("--include-examples", action="store_true", help="also the files that start with _")
    situations.add_argument("--check", action="store_true", help="build every situation's world and say what wakes the seat")

    run = sub.add_parser("run", help="bot × situation × sample on the real agent loop -> results.jsonl, judge_items.jsonl, run.json")
    run.add_argument("--situations", action="append", type=Path, help="a file or a directory (default: bench/fixtures/situations)")
    run.add_argument("--include-examples", action="store_true")
    run.add_argument("--ids", action="append", help="only these situation ids (comma separated)")
    run.add_argument("--tags", action="append", help="only situations with one of these tags")
    run.add_argument("--bots", action="append", help="bot specs: legacy, legacy:Name, bot:Alice, bot:<dir>, bot:Alice@nomem,no-playbook, default12, legacy12 "
                     "(default: legacy,bot:Alice; a seat the situation file puts a bot on (bots:) is played by that bot alone)")
    run.add_argument("--library", type=Path, help="the bot library (default: bots/)")
    run.add_argument("--suite", help="run a suite (bench/suites/<id>.yml: its situations, bots and samples for --tier) instead of --situations; python -m bench suites lists them")
    run.add_argument("--tier", choices=TIERS, default="smoke", help="with --suite: smoke (default) | quick | full")
    run.add_argument("--samples", type=int, default=None, help="samples per bot × situation × seat (default 1; with --suite: the tier's)")
    run.add_argument("--out", type=Path, help="the directory the results go to (default: .pytest_tmp/bench-runs/<run id>/; never the app's data directory)")
    run.add_argument("--run-id")
    run.add_argument("--resume", action="store_true", help="skip the samples that are in <out>/results.jsonl already (ok / no_decision)")
    run.add_argument("--max-rounds", type=int, default=12, help="model calls per sample")
    run.add_argument("--timeout", type=float, default=300.0, help="wall clock seconds per sample")
    run.add_argument("--concurrency", type=int, help="samples at a time (default 1 for the fake model, 4 for a real one)")
    run.add_argument("--dump-prompts", action="store_true", help="write the whole conversation of every sample to <out>/prompts/")
    run.add_argument("--fake", default="prose", help=f"the fake model's policy ({', '.join(fakes.POLICIES)}); the default mode")
    run.add_argument("--real", action="store_true", help="use a real model (needs --max-calls, --model, --base-url, a key in --api-key-env)")
    run.add_argument("--max-calls", type=int, help="the whole run's budget of model calls (required with --real)")
    run.add_argument("--model", help=f"the model id (or ${MODEL_ENV}; with --providers-file: default the provider's first model)")
    run.add_argument("--base-url", help=f"the model service's address (or ${BASE_URL_ENV})")
    run.add_argument("--providers-file", type=Path, help="reuse a provider the app knows: a providers.json (read only; the key is NEVER read from it) - needs --provider")
    run.add_argument("--provider", help="the id of the provider in --providers-file: its address, protocol, model, context window and prices are used (flags override)")
    run.add_argument("--protocol", choices=PROTOCOLS, help="(default: the provider's, else openai_chat)")
    run.add_argument("--api-key-env", default=DEFAULT_KEY_ENV, help="the NAME of the environment variable that holds the key (never the key itself)")
    run.add_argument("--reasoning", default="high", choices=REASONING)
    run.add_argument("--context-window", type=int, help="tokens (default: the provider's, else 1000000)")
    run.add_argument("--call-timeout", type=float, default=180.0, help="seconds one model call may take (real model)")
    run.add_argument("--attempts", type=int, default=3, help="HTTP requests per counted call: the client re-sends a request the service answered with 429 / 5xx (real model)")
    run.add_argument("--price-in", type=float, help="USD per million input tokens, for the cost estimate (default: the provider's, else 0)")
    run.add_argument("--price-out", type=float, help="USD per million output tokens, for the cost estimate (default: the provider's, else 0)")

    suites_cmd = sub.add_parser("suites", help="list the suites: situations, bots, and the model calls each tier would make")
    suites_cmd.add_argument("--check", action="store_true", help="also check that every suite's fixtures, judges and gates exist (exit 1 if not)")

    fixtures = sub.add_parser("fixtures", help="write the question bank from bench/cases (or --check that the committed files are what the code writes)")
    fixtures.add_argument("--check", action="store_true", help="only compare: exit 1 when a committed file differs from what the code writes")
    fixtures.add_argument("--root", type=Path, help="write below this directory instead of bench/fixtures (for trying a change)")

    summary = sub.add_parser("summarize", help="the R / S1 / S3 / score numbers of a results.jsonl")
    summary.add_argument("--results", required=True, type=Path)
    summary.add_argument("--out", type=Path)

    replay = sub.add_parser("replay", help="can a saved game's decision point be rebuilt on the real engine? (feasibility, scripts from ledgers)")
    replay.add_argument("--game", type=Path, help="a game.json (read only)")
    replay.add_argument("--data-dir", action="append", type=Path, help="check every game below <dir>/games (read only); repeatable")
    replay.add_argument("--upto", type=int, help="replay up to this ledger seq (the decision point), default: the whole game")
    replay.add_argument("--spec", default="saved", choices=("saved", "current"), help="the scenario the game was played with (saved) or as it is now")
    replay.add_argument("--deal", default="seed", choices=("seed", "explicit"))
    replay.add_argument("--list-decisions", action="store_true", help="list where a seat was asked to act (floor.granted / request.opened)")
    replay.add_argument("--emit", type=Path, help="write a situation script of the game up to --upto (needs --seat and --id)")
    replay.add_argument("--seat")
    replay.add_argument("--id", dest="situation_id")
    replay.add_argument("--title", default="")


# ---------------------------------------------------------------------------------------------------------------------- situations
def cmd_situations(args: argparse.Namespace) -> int:
    where = args.situations or [DEFAULT_SITUATIONS]
    try:
        found = load_situations(where, include_examples=args.include_examples)
    except SituationError as exc:
        _print(f"题目文件有问题：{exc}")
        return 1
    failed = 0
    for situation in found:
        line = f"{situation.id}  [{', '.join(situation.tags)}]  {situation.scenario}  座位 {','.join(situation.decision.seats)}  步骤 {len(situation.steps)}  {situation.title}"
        if situation.bots:
            line += "  自带 bot：" + "、".join(f"{seat}={d['spec'] if 'spec' in d else 'bot:' + d['name']}" for seat, d in situation.bots.items())
        if args.check:
            for seat in situation.decision.seats:
                try:
                    scene = build_scene(situation, seat=seat)
                    wake = ",".join(str(e.payload.get("event")) for e in scene.wake)
                    _print(f"{line}\n    ✔ {seat}：账本 {len(scene.world.ledger)} 条，叫醒它的是 {wake}，指纹 {scene.fingerprint}")
                except (SituationError, PlayError) as exc:
                    failed += 1
                    _print(f"{line}\n    ✖ {seat}：{exc}")
        else:
            _print(line)
    if not found:
        _print("没有找到题目。")
    return 1 if failed else 0


# ---------------------------------------------------------------------------------------------------------------------- run
def _real_config(args: argparse.Namespace) -> RealConfig:
    """What a flag says wins over the provider file, which wins over the environment and the defaults.  Raises ``RealModeError``."""
    entry: dict[str, Any] = {}
    if args.providers_file is not None:
        if not args.provider:
            raise RealModeError("--providers-file 要同时给 --provider（用文件里的哪一个 provider）。")
        entry = load_provider_entry(args.providers_file, args.provider, args.model or "")
    elif args.provider:
        raise RealModeError("--provider 要和 --providers-file 一起用。")

    def pick(flag: Any, key: str, default: Any) -> Any:
        return flag if flag is not None else entry.get(key, default)

    return RealConfig(
        base_url=args.base_url or entry.get("base_url") or os.environ.get(BASE_URL_ENV, ""),
        model=args.model or entry.get("model") or os.environ.get(MODEL_ENV, ""),
        protocol=pick(args.protocol, "protocol", "openai_chat"), api_key_env=args.api_key_env, reasoning=args.reasoning,
        context_window=pick(args.context_window, "context_window", 1_000_000), timeout_s=args.call_timeout, max_attempts=args.attempts,
        input_price_per_m=pick(args.price_in, "input_price_per_m", 0.0), output_price_per_m=pick(args.price_out, "output_price_per_m", 0.0),
        auto_append_path=bool(entry.get("auto_append_path", True)), provider=args.provider or "",
    )


class _Refused(Exception):
    """A run that cannot be made, with the reason in Chinese (printed, exit 2)."""


def _select(args: argparse.Namespace) -> tuple[list[Situation], list[Any], list[Any], int]:
    """What ``run`` runs: the situations, the bots, every sample (job) and the number of samples - from ``--suite`` / ``--tier``, or from ``--situations`` /
    ``--bots`` / ``--samples``.  Raises :class:`_Refused` before anything is made."""
    ids, tags = set(_split(args.ids)), set(_split(args.tags))
    plan_: Plan | None = None
    suite = None
    if args.suite:
        if args.situations:
            raise _Refused("--suite 和 --situations 不能同时给（套件自己带着题目）。")
        try:
            pool = load_suites()
        except SuiteError as exc:
            raise _Refused(f"套件定义有问题：{exc}") from None
        suite = pool.get(args.suite)
        if suite is None:
            raise _Refused(f"没有套件 {args.suite!r}。有：{'、'.join(pool)}。")
        if not suite.runnable:
            raise _Refused(f"套件 {suite.id} 不是靠 run 跑的：{suite.manual}")
        try:
            plan_ = plan(suite, args.tier, pool)
        except (SuiteError, SituationError) as exc:
            raise _Refused(f"套件 {suite.id} 的题目有问题：{exc}") from None
        situations = [s for s in plan_.situations() if (not ids or s.id in ids) and (not tags or tags & set(s.tags))]
        if ids and ids - {s.id for s in plan_.situations()}:
            raise _Refused(f"套件 {suite.id} 里找不到这些题目：{'、'.join(sorted(ids - {s.id for s in plan_.situations()}))}。")
    else:
        try:
            situations = load_situations(args.situations or [DEFAULT_SITUATIONS], include_examples=args.include_examples, ids=sorted(ids), tags=sorted(tags))
        except SituationError as exc:
            raise _Refused(f"题目文件有问题：{exc}") from None
    if not situations:
        raise _Refused("没有要跑的题目（examples 默认不跑：加 --include-examples，或用 --situations / --suite 指定）。")
    samples = args.samples or (plan_.samples if plan_ else 1)
    try:
        scenarios = {s.scenario for s in situations}
        scenario = next(iter(scenarios)) if len(scenarios) == 1 else ""
        if plan_ is None:
            everywhere_pinned = all(seat in s.bots for s in situations for seat in s.decision.seats)  # (the situation files choose every bot: --bots is not needed)
            bots = make_bots(args.bots or ([] if everywhere_pinned else ["legacy,bot:Alice"]), scenario_id=scenario, library=args.library)  # (raw: ``@a,b`` options)
            jobs = jobs_for(situations, bots, samples, library=args.library)
            return situations, bots, jobs, samples
        groups = plan_.groups
        if args.bots and not (suite and suite.ablations):
            groups = (Group(tuple(situations), tuple(expand_bot_specs(args.bots, library=args.library))),)
        expanded = [list(expand_bot_specs(list(g.bots), library=args.library)) for g in groups]
        bots = make_bots(list(dict.fromkeys(spec for specs in expanded for spec in specs)), scenario_id=scenario, library=args.library)
        by_label = {bot.label: bot for bot in bots}
        keep = {s.id for s in situations}
        jobs = []
        for group, specs in zip(groups, expanded):
            jobs.extend(jobs_for([s for s in group.situations if s.id in keep], [by_label[spec] for spec in specs], samples, library=args.library))
        return situations, bots, list({job.key: job for job in jobs}.values()), samples
    except BotSpecError as exc:
        raise _Refused(f"bot 有问题：{exc}") from None


def cmd_run(args: argparse.Namespace) -> int:
    """Everything that can be refused is refused *before* anything is made (no directory, no data directory, no connection); then the run."""
    run_id = args.run_id or new_run_id()
    out: Path = args.out if args.out is not None else default_out_dir(run_id)
    try:
        refuse_real_data_dir(out)
    except UnsafeDataDir as exc:
        _print(str(exc))
        return 2
    if args.real and (args.max_calls is None or args.max_calls <= 0):
        _print("真实模型模式必须同时给 --max-calls N（整个运行的模型调用上限，大于 0）：基准不会做没有计入预算的调用。")
        return 2
    if args.max_calls is not None and args.max_calls < 0:
        _print("--max-calls 不能是负数。")
        return 2
    if (args.samples is not None and args.samples < 1) or args.max_rounds < 1:
        _print("--samples 和 --max-rounds 必须至少是 1。")
        return 2
    if not args.real and args.fake not in fakes.POLICIES:
        _print(f"不认识的假模型策略 {args.fake!r}。可用的：{'、'.join(fakes.POLICIES)}。")
        return 2
    try:
        situations, bots, jobs, samples = _select(args)
    except _Refused as exc:
        _print(str(exc))
        return 2
    concurrency = args.concurrency or (4 if args.real else 1)
    real_port = None
    secrets: tuple[str, ...] = ()
    real_info: dict[str, Any] | None = None
    model, mode, window = "fake", "fake", 0
    if args.real:
        try:
            config = _real_config(args)
            real_port = build_real_port(config, max_concurrent=concurrency)  # (the key is read from the environment here; nothing is sent)
        except RealModeError as exc:
            _print(str(exc))
            return 2
        secrets, real_info, model, mode, window = real_port.secrets, config.describe(), config.model, "real", config.context_window
    elif args.providers_file is not None or args.provider:
        _print("--providers-file / --provider 只在 --real 时有用（假模型不需要）。")
        return 2
    cfg = RunConfig(
        samples=samples, max_rounds=args.max_rounds, timeout_s=args.timeout, concurrency=concurrency, max_calls=args.max_calls, mode=mode,
        model=model, run_id=run_id, context_window=window, dump_prompts=args.dump_prompts,
    )
    pinned = sum(1 for s in situations for seat in s.decision.seats if seat in s.bots)
    _print(
        f"运行 {cfg.run_id}：{len(situations)} 道题 × {len(bots)} 个 bot × {cfg.samples} 次 = {len(jobs)} 个样本；模式 {mode}"
        + (f"（{model} @ {urlsplit(real_info['base_url']).netloc}，调用上限 {args.max_calls}，最坏情况 {len(jobs) * cfg.max_rounds} 次）" if real_info else f"（假模型 {args.fake}，不调用任何模型）")
        + (f"；{pinned} 个座位由题目自带的 bot 来答（样本数按它们算）" if pinned else "")
        + f"；结果写到 {out}"
    )
    if real_port is not None:
        port = real_port.port

        def llm_factory(job: Any, scene: Any) -> Any:
            return port
    else:
        policy = fakes.policy_named(args.fake)

        def llm_factory(job: Any, scene: Any) -> Any:
            return fakes.FakeLlm(policy, world=scene.world, seat=scene.seat, bot=job.bot.label, situation=job.situation.id, sample=job.sample)

    budget = Budget(args.max_calls)

    def on_row(row: dict[str, Any]) -> None:
        decision = row.get("decision") or {}
        _print(f"  {row['situation']} {row['seat']} {row['bot']} #{row['sample']}：{row['status']}（{row['stop']}，{decision.get('tool') or '-'}，{row.get('rounds', 0)} 次调用）")

    async def go() -> Any:
        try:
            return await run_suite(situations, bots, cfg, llm_factory, out_dir=out, budget=budget, secrets=secrets, resume=args.resume, on_row=on_row, jobs=jobs)
        finally:
            if real_port is not None:
                await real_port.aclose()

    # Everything the run makes lives in ``out`` - the isolated data directory too: nothing is left in the system's temporary directory, and the
    # app's own data (``ARENA_DATA_DIR`` / ``data/``) is never the one the process works with while a bot runs.
    with isolated_data_dir(out):
        report = asyncio.run(go())
        info = run_info(report, cfg, situations, real=real_info, max_calls=args.max_calls)
        (out / "run.json").write_text(json.dumps(info, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        rows = read_results(out / "results.jsonl")
        summary = summarize(rows)
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    _print(format_summary(summary))
    _print(f"完成：{report.by_status()}，跳过 {report.skipped}，模型调用 {report.calls}，用时 {report.elapsed_s} 秒。结果在 {out}")
    return 1 if report.scene_errors else 0


# ---------------------------------------------------------------------------------------------------------------------- suites
def cmd_suites(args: argparse.Namespace) -> int:
    try:
        pool = load_suites()
    except SuiteError as exc:
        _print(f"套件定义有问题：{exc}")
        return 1
    sizes = estimates(pool)
    _print("套件 | 题数(对) | smoke 样本/调用 | quick 样本/调用 | full 样本/调用 | 进验收 | 题目 / 做法")
    totals = {tier: [0, 0] for tier in TIERS}
    for sid, suite in pool.items():
        if not suite.runnable:
            _print(f"{sid} | — | — | — | — | {'是' if suite.acceptance else '否'} | 不由 run 运行：{suite.manual[:60]}…")
            continue
        row = sizes[sid]
        for tier in TIERS:
            totals[tier][0] += row[tier].jobs
            totals[tier][1] += row[tier].calls
        pairs = f"({row['quick'].pairs}对)" if row["quick"].pairs else ""
        source = f"借用 {suite.uses} 的题" if suite.uses else ("逐层消融：" + "、".join(a["layer"] for a in suite.ablations) if suite.ablations else suite.title)
        _print(f"{sid} | {row['full'].situations}{pairs} | " + " | ".join(f"{row[t].jobs}/{row[t].calls}" for t in TIERS) + f" | {'是' if suite.acceptance else '否'} | {source}")
    _print("合计（有的套件借用同一批样本，S1 / S3 借用 S2 的，所以这里的合计会重复算它们） | | " + " | ".join(f"{totals[t][0]}/{totals[t][1]}" for t in TIERS) + " | |")
    if args.check:
        problems = check_suites(pool)
        for problem in problems:
            _print(problem)
        _print("套件定义和仓库一致。" if not problems else f"有 {len(problems)} 处不一致。")
        return 1 if problems else 0
    return 0


# ---------------------------------------------------------------------------------------------------------------------- fixtures
def cmd_fixtures(args: argparse.Namespace) -> int:
    from bench.cases import render

    root = args.root or render.FIXTURES
    if args.check:
        problems = render.check(root)
        for problem in problems:
            _print(problem)
        _print("题库和生成器一致。" if not problems else f"有 {len(problems)} 处不一致。")
        return 1 if problems else 0
    written = render.write(root)
    _print(f"写了 {len(written)} 个文件到 {root}。" if written else "没有变化：题库已经是生成器写出的样子。")
    return 0


# ---------------------------------------------------------------------------------------------------------------------- summarize
def cmd_summarize(args: argparse.Namespace) -> int:
    rows = read_results(args.results)
    if not rows:
        _print(f"{args.results} 里没有结果。")
        return 1
    summary = summarize(rows)
    _print(format_summary(summary))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        _print(f"written: {args.out}")
    return 0


# ---------------------------------------------------------------------------------------------------------------------- replay
def _games_below(data_dirs: Sequence[Path]) -> list[Path]:
    paths: list[Path] = []
    for data_dir in data_dirs:
        paths.extend(sorted((data_dir / "games").glob("*/game.json")))
    return paths


def cmd_replay(args: argparse.Namespace) -> int:
    paths = [args.game] if args.game else []
    paths += _games_below(args.data_dir or [])
    if not paths:
        _print("要给 --game 或 --data-dir。")
        return 2
    if args.emit and not (args.game and not args.data_dir and args.seat and args.situation_id and args.upto):
        _print("--emit 只能用于一局（--game），并且需要 --upto（决策点的记录序号）、--seat 和 --id。")
        return 2
    code = 0
    for path in paths:
        try:
            game = load_game(path)
        except ValueError as exc:
            _print(f"{path.parent.name}：读不了（{exc}）")
            code = 1
            continue
        game_id = path.parent.name
        if args.list_decisions:
            for point in decision_points(game):
                _print(f"#{point['seq']:<5} gt={point['gt']!s:<9} {point['phase'][:22]:<22} {point['kind']:<8} → {','.join(str(t) for t in point['to'])[:60]}  {point.get('question', '')}")
            continue
        if not (game.get("world") or {}).get("ledger"):
            _print(f"{game_id}：账本是空的，跳过。")
            continue
        try:
            result = replay_game(game, upto_seq=args.upto, spec_source=args.spec, deal=args.deal)
        except Exception as exc:  # noqa: BLE001 - a game that cannot be replayed is a finding, not a crash
            _print(f"{game_id}：重放失败：{type(exc).__name__}: {exc}")
            code = 1
            continue
        if args.data_dir and not args.game and not args.upto:  # (a survey of a data directory: one line per game; one game: the whole report)
            led = result.ledger_report
            _print(
                f"{game_id} {game.get('version')} 账本 {led['saved']} 条：一致 {led['matched']}、仅截断空白 {led['artifacts']}、不同 {len(led['mismatches'])}；"
                f"终局状态 {'一致' if result.state_report and result.state_report['equal'] else '不同'}；引擎拒绝 {len(result.refused)} 步；转不回的条目 {len(result.conversion.gaps)} 个"
            )
        else:
            _print(report_text(result, game_id=game_id))
        code = code or (0 if result.exact else 1)
        if args.emit:
            if not result.exact:
                _print("这一局到该决策点的重放和存档不一致，没有写出题目（一个对不上存档的题目会让 bot 面对一个不是那一局的局面）。")
                return 1
            try:
                situation = situation_from_game(game, upto_seq=args.upto, seat=args.seat, situation_id=args.situation_id, title=args.title)
            except ValueError as exc:
                _print(str(exc))
                return 2
            text = situation_to_yaml(situation, header="由存档重放得到（bench replay --emit）。存档是用户自己的数据：提交前检查名字、人类发言和个人信息。")
            try:
                parse_situation(yaml.safe_load(text), source=str(args.emit))  # (what is written must be a script the runner accepts)
            except SituationError as exc:
                _print(f"写不出合格的题目：{exc}")
                return 2
            args.emit.parent.mkdir(parents=True, exist_ok=True)
            args.emit.write_text(text, encoding="utf-8")
            _print(f"written: {args.emit}")
    return code


__all__ = ["add_commands", "cmd_replay", "cmd_run", "cmd_situations", "cmd_summarize"]
