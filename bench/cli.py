"""The commands of ``python -m bench`` that run things: ``situations``, ``run``, ``summarize`` and ``replay`` (``audit`` / ``baseline`` are in
``bench.__main__``).

``run`` is the runner: it plays situations on the real engine and runs the real agent loop of each bot on them (see ``bench.runner``).  Its default
model is the FAKE one (scripted, no network, no key).  A real model needs ``--real`` AND ``--max-calls`` (the brake), ``--model``, ``--base-url`` and a
key in an environment variable (``--api-key-env``): the benchmark never reads the app's provider table, and the whole run happens with
``ARENA_DATA_DIR`` pointed at a temporary directory - the real ``data/`` is neither read nor written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bench import fakes
from bench.bots import BotSpecError, make_bots
from bench.loaders import load_game
from bench.play import PlayError, build_scene
from bench.realllm import DEFAULT_KEY_ENV, PROTOCOLS, REASONING, RealConfig, RealModeError, build_real_port
from bench.replay import (
    decision_points,
    replay_game,
    report_text,
    situation_from_game,
    situation_to_yaml,
)
from bench.runner import Budget, RunConfig, jobs_for, new_run_id, read_results, run_info, run_suite
from bench.safety import UnsafeDataDir, isolated_data_dir, refuse_real_data_dir
from bench.situation import SituationError, load_situations
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
    run.add_argument("--bots", action="append", help="bot specs: legacy, legacy:Name, bot:Alice, bot:<dir>, bot:Alice@nomem,no-playbook, default12, legacy12")
    run.add_argument("--library", type=Path, help="the bot library (default: bots/)")
    run.add_argument("--samples", type=int, default=1, help="samples per bot × situation × seat")
    run.add_argument("--out", type=Path, required=True, help="the directory the results go to (never the app's data directory)")
    run.add_argument("--run-id")
    run.add_argument("--resume", action="store_true", help="skip the samples that are in <out>/results.jsonl already (ok / no_decision)")
    run.add_argument("--max-rounds", type=int, default=12, help="model calls per sample")
    run.add_argument("--timeout", type=float, default=300.0, help="wall clock seconds per sample")
    run.add_argument("--concurrency", type=int, help="samples at a time (default 1 for the fake model, 4 for a real one)")
    run.add_argument("--dump-prompts", action="store_true", help="write the whole conversation of every sample to <out>/prompts/")
    run.add_argument("--fake", default="prose", help=f"the fake model's policy ({', '.join(fakes.POLICIES)}); the default mode")
    run.add_argument("--real", action="store_true", help="use a real model (needs --max-calls, --model, --base-url, a key in --api-key-env)")
    run.add_argument("--max-calls", type=int, help="the whole run's budget of model calls (required with --real)")
    run.add_argument("--model", help=f"the model id (or ${MODEL_ENV})")
    run.add_argument("--base-url", help=f"the model service's address (or ${BASE_URL_ENV})")
    run.add_argument("--protocol", default="openai_chat", choices=PROTOCOLS)
    run.add_argument("--api-key-env", default=DEFAULT_KEY_ENV, help="the environment variable that holds the key")
    run.add_argument("--reasoning", default="high", choices=REASONING)
    run.add_argument("--context-window", type=int, default=1_000_000)
    run.add_argument("--price-in", type=float, default=0.0, help="USD per million input tokens (for the cost estimate)")
    run.add_argument("--price-out", type=float, default=0.0, help="USD per million output tokens (for the cost estimate)")

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
    return RealConfig(
        base_url=args.base_url or os.environ.get(BASE_URL_ENV, ""), model=args.model or os.environ.get(MODEL_ENV, ""), protocol=args.protocol,
        api_key_env=args.api_key_env, reasoning=args.reasoning, context_window=args.context_window,
        input_price_per_m=args.price_in, output_price_per_m=args.price_out,
    )


def cmd_run(args: argparse.Namespace) -> int:
    out: Path = args.out
    try:
        refuse_real_data_dir(out)
    except UnsafeDataDir as exc:
        _print(str(exc))
        return 2
    if args.real and (args.max_calls is None or args.max_calls <= 0):
        _print("真实模型模式必须同时给 --max-calls N（整个运行的模型调用上限，大于 0）：基准不会做没有计入预算的调用。")
        return 2
    if args.samples < 1 or args.max_rounds < 1:
        _print("--samples 和 --max-rounds 必须至少是 1。")
        return 2
    with isolated_data_dir():
        try:
            situations = load_situations(args.situations or [DEFAULT_SITUATIONS], include_examples=args.include_examples,
                                         ids=_split(args.ids), tags=_split(args.tags))
        except SituationError as exc:
            _print(f"题目文件有问题：{exc}")
            return 2
        if not situations:
            _print("没有要跑的题目（examples 默认不跑：加 --include-examples，或用 --situations 指定文件）。")
            return 2
        try:
            scenarios = {s.scenario for s in situations}
            bots = make_bots(_split(args.bots) or ["legacy", "bot:Alice"], scenario_id=next(iter(scenarios)) if len(scenarios) == 1 else "", library=args.library)
        except BotSpecError as exc:
            _print(f"bot 有问题：{exc}")
            return 2
        concurrency = args.concurrency or (4 if args.real else 1)
        real_port = None
        secrets: tuple[str, ...] = ()
        real_info = None
        try:
            if args.real:
                config = _real_config(args)
                real_port = build_real_port(config, max_concurrent=concurrency)
                secrets = real_port.secrets
                real_info = config.describe()
                model, mode = config.model, "real"
            else:
                if args.fake not in fakes.POLICIES:
                    _print(f"不认识的假模型策略 {args.fake!r}。可用的：{'、'.join(fakes.POLICIES)}。")
                    return 2
                model, mode = "fake", "fake"
        except RealModeError as exc:
            _print(str(exc))
            return 2
        cfg = RunConfig(
            samples=args.samples, max_rounds=args.max_rounds, timeout_s=args.timeout, concurrency=concurrency, max_calls=args.max_calls, mode=mode,
            model=model, run_id=args.run_id or new_run_id(), context_window=args.context_window if args.real else 0, dump_prompts=args.dump_prompts,
        )
        jobs = jobs_for(situations, bots, cfg.samples)
        _print(
            f"运行 {cfg.run_id}：{len(situations)} 道题 × {len(bots)} 个 bot × {cfg.samples} 次 = {len(jobs)} 个样本；模式 {mode}"
            + (f"（{model} @ {urlparse(real_info['base_url']).netloc}，调用上限 {args.max_calls}，最坏情况 {len(jobs) * cfg.max_rounds} 次）" if args.real else f"（假模型 {args.fake}，不调用任何模型）")
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
            what = decision.get("tool") or "-"
            _print(f"  {row['situation']} {row['seat']} {row['bot']} #{row['sample']}：{row['status']}（{row['stop']}，{what}，{row['rounds'] if 'rounds' in row else 0} 次调用）")

        async def go() -> Any:
            try:
                return await run_suite(situations, bots, cfg, llm_factory, out_dir=out, budget=budget, secrets=secrets, resume=args.resume, on_row=on_row)
            finally:
                if real_port is not None:
                    await real_port.aclose()

        report = asyncio.run(go())
        info = run_info(report, cfg, situations, bots, real=real_info, max_calls=args.max_calls)
        out.mkdir(parents=True, exist_ok=True)
        (out / "run.json").write_text(json.dumps(info, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        rows = read_results(out / "results.jsonl")
        summary = summarize(rows)
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        _print(format_summary(summary))
        _print(f"完成：{report.by_status()}，跳过 {report.skipped}，模型调用 {report.calls}，用时 {report.elapsed_s} 秒。结果在 {out}")
        return 1 if report.scene_errors else 0


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
    code = 0
    for path in paths:
        try:
            game = load_game(path)
        except ValueError as exc:
            _print(f"{path.parent.name}：读不了（{exc}）")
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
        if len(paths) > 1 and not args.upto:
            led = result.ledger_report
            _print(
                f"{game_id} {game.get('version')} 账本 {led['saved']} 条：一致 {led['matched']}、仅截断空白 {led['artifacts']}、不同 {len(led['mismatches'])}；"
                f"终局状态 {'一致' if result.state_report and result.state_report['equal'] else '不同'}；引擎拒绝 {len(result.refused)} 步；转不回的条目 {len(result.conversion.gaps)} 个"
            )
        else:
            _print(report_text(result, game_id=game_id))
        code = code or (0 if result.exact else 1)
        if args.emit:
            if not (args.seat and args.situation_id and args.upto):
                _print("--emit 需要 --upto（决策点的记录序号）、--seat 和 --id。")
                return 2
            situation = situation_from_game(game, upto_seq=args.upto, seat=args.seat, situation_id=args.situation_id, title=args.title)
            args.emit.parent.mkdir(parents=True, exist_ok=True)
            args.emit.write_text(
                situation_to_yaml(situation, header="由存档重放得到（bench replay --emit）。存档是用户自己的数据：提交前检查名字、人类发言和个人信息。"),
                encoding="utf-8",
            )
            _print(f"written: {args.emit}")
    return code


__all__ = ["add_commands", "cmd_replay", "cmd_run", "cmd_situations", "cmd_summarize"]
