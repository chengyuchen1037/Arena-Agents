"""Real-model smoke test for the engine (costs API credits - it has a hard call budget).

It plays a *small* game with the models that are bound in the saved default bindings of a data directory (``--from``) and prints a compact
report: tool usage, rejection codes (TOO_LONG / FORMAT among them), the four kinds of tokens, the prompt-cache hit rate, how many activations
each agent had, what was mounted from the bots' memory.  Nothing is shown from the key store; the provider registry is only used by the engine
to make the calls.  How to use it, what it costs and how to read the result: docs/real-model-runs.md.

What it does (v0.14: a game only starts when every bot is initialized):
  1. makes a private scratch data directory and COPIES providers.json, secrets.v1.enc and (Windows) master.v1.dpapi into it from --from -
     the live data directory is only read, never opened by a store or modified; the scratch is deleted at the end (--keep keeps it);
  2. starts the application on it: the table of bots is made from the default library (bots/): the first --players bots, with their L0 and
     nothing else; the default model bindings of --from (saved by seat in v0.13, by bot id in v0.14 - both are read) become the bindings of
     those seats and of the moderator;
  3. initializes the bots: --init fixed (default) commits the fixed initialization of the demo - valid files, NO model call - only so that
     the start check passes; --init real runs the real initialization (every bot asks its own model, in parallel, at most 120 s each) and
     prints how each bot did (time, calls, tokens, files and their size, why it failed);
  4. plays the game, paused when the call budget or the time limit is reached, and prints the report;
  5. optionally: --resume-calls (does the provider's prompt cache survive a pause?), --oneshots (the moderator's "complete the core", the
     real post-game review).

The table has --players seats (1-12) filled by the first bots of the default library, and the scenario must be one for exactly that many
players (scenarios/minimal_test.md: 4, scenarios/werewolf_12p.md: 12).

ONE hard budget (--max-calls) covers every model call of the run: the initialization (--init real needs about 6 calls per bot), the game and
the post-game calls of --oneshots (those add 2 + 8 per reviewed bot on top).  A call over the budget is never sent.

Exit code: 0 the run is over (the report was printed); 2 a model binding is missing or unusable, or the scenario does not fit --players; 3 the
game could not start (a bot is not initialized, or the start check refused; the problems are printed).

Examples::

    python tools/real_smoke.py --from "C:/Users/me/Arena-Agent/data" --players 4 --max-calls 60
    python tools/real_smoke.py --from ... --init real --players 4 --max-calls 120
    python tools/real_smoke.py --from ... --scenario scenarios/werewolf_12p.md --players 12 --max-calls 200 --oneshots 2
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools/ (devlib), whatever the working directory is

import devlib  # noqa: E402

#: Sample characters: the first twelve bots of the default library (``--core-from-sample`` writes them into the L0 of the bots at the seats).
SEATS = [
    ("Alice", "逻辑严密，善于从发言细节中推敲破绽。"),
    ("Bob", "稳健谨慎，倾向于整合多方信息后再站边。"),
    ("Carol", "直觉敏锐，关注言行一致性与情绪变化。"),
    ("David", "擅长伪装，喜欢制造信息迷雾。"),
    ("Eve", "大局观强，善于梳理时间线。"),
    ("Frank", "作风凌厉，进攻性强。"),
    ("Grace", "外柔内刚，关键时刻敢于亮明态度。"),
    ("Henry", "领袖气质，喜欢主动控场。"),
    ("Ivy", "细致入微，擅长复盘前后矛盾。"),
    ("Jack", "务实果断，倾向于收益最大化。"),
    ("Kate", "善于倾听，常能识破套路。"),
    ("Leo", "思维跳跃，擅长奇袭。"),
]

#: Seconds between two looks at the running game (a test makes it short).
POLL_S = 1.0
#: How many entries of the floor timeline the report prints.
TIMELINE_LINES = 60
#: What ``--oneshots N`` adds to the budget: two "complete the core" calls, and per reviewed bot a review of a few rounds.
REFINE_CALLS = 2
REVIEW_CALLS_PER_BOT = 8
REFINE_MAX_TOKENS = 6000
MODERATOR = "moderator"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="real_smoke.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--from", dest="source", required=True, help="a data directory (the data/ of an installation) holding providers.json and secrets.v1.enc; only read"
    )
    parser.add_argument("--scenario", default="scenarios/minimal_test.md", help="the scenario file (default: the 4-player test scenario)")
    parser.add_argument(
        "--players", type=int, default=4, help="seats at the table, 1-12 (the first bots of the default library); the scenario must fit (default 4)"
    )
    parser.add_argument(
        "--init",
        choices=("fixed", "real"),
        default="fixed",
        help="how the bots get the initialization the start check asks for: fixed = the demo's fixed one, no model call (default); "
        "real = the real learning run, every bot asks its own model (costs credits, counted in --max-calls)",
    )
    parser.add_argument(
        "--core-from-sample",
        action="store_true",
        help="write the sample characters of this script (SEATS) into the L0 of the bots, instead of the default library's",
    )
    parser.add_argument("--max-calls", type=int, default=60, help="hard budget of model calls for the whole run (default 60); the game is paused when it is reached")
    parser.add_argument("--timeout", type=float, default=300.0, help="wall-clock limit in seconds, for the initialization and for the game")
    parser.add_argument("--concurrency", type=int, default=0, help="limit of concurrent model calls (0 = unlimited, the engine default)")
    parser.add_argument(
        "--price-source",
        action="store_true",
        help="download the LiteLLM price table and price the bound models with it (in the scratch copy only), so the "
        "report shows what the provider really bills; also a live test of the price-source feature",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=0,
        metavar="TOKENS",
        help="pretend every bound model has only this context window (e.g. 16000), so the compaction reminder "
        "(at compact_at of the window), the model's compact_context call and the emergency trim happen within a short game",
    )
    parser.add_argument(
        "--resume-calls",
        type=int,
        default=0,
        metavar="K",
        help="after the call budget is reached: pause, resume the same game and let it run K more calls; reports whether "
        "the provider's prompt cache kept hitting across the pause (the conversations and system prompts must survive it)",
    )
    parser.add_argument(
        "--oneshots",
        type=int,
        default=0,
        metavar="N",
        help="after the game, the two kinds of call outside the game loop with the real models: the moderator's \"complete the core\" "
        "(a draft of an L0 from a few notes; printed, never saved) and the real post-game review of the first N bots (the paused game is "
        "treated as finished, so the reviews read what was really played; prints what each wrote).  Adds 2 + 8 per bot to the budget",
    )
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory (it holds an encrypted copy of the providers)")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.players <= len(SEATS):
        parser.error(f"--players must be between 1 and {len(SEATS)}")
    if args.max_calls < 1:
        parser.error("--max-calls must be at least 1")
    if args.oneshots < 0 or args.oneshots > args.players:
        parser.error("--oneshots must be between 0 and --players")
    return args


# ============================================================================================================== scratch
def prepare_scratch(source: Path, players: list[str], window: int = 0, *, scratch_root: Path | None = None) -> Path:
    """A private scratch data directory below ``.demo-data`` (``scratch_root``) with COPIES of the provider registry, the encrypted keys and
    the master-key wrapper of ``source`` and the default model bindings of ``source`` for the seats ``players`` and the moderator - nothing
    else; ``source`` is only read.  (The run does the same in two steps, inside :func:`devlib.scratch_environment`.)"""
    scratch = devlib.new_scratch(scratch_root if scratch_root is not None else ROOT / ".demo-data", "arena-real-smoke-")
    try:
        populate_scratch(Path(source), scratch, players, window)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return scratch


def populate_scratch(source: Path, scratch: Path, seats: list[str], window: int = 0) -> None:
    devlib.copy_provider_files(source, scratch)
    devlib.write_bindings(scratch, devlib.source_bindings(source, seats, window=window))


def scenario_path(text: str) -> str:
    """The scenario file as an absolute path: as given, else relative to the working directory, else to the program directory."""
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path if (Path.cwd() / path).is_file() else ROOT / path
    return str(path)


def smoke_config(args: argparse.Namespace, seats: list[str]) -> dict[str, Any]:
    """``config.yml`` for this run: the scenario asked for, the seats of the table (the table of bots says who sits there), the model-call limit."""
    from app.config import load_config

    cfg = load_config()
    cfg["scenario"] = {"path": scenario_path(args.scenario)}
    cfg["participants"] = [{"id": pid, "seat": str(index), "name": SEATS[index - 1][0]} for index, pid in enumerate(seats, start=1)]
    runtime = dict(cfg.get("runtime") or {})
    runtime["llm_concurrency"] = int(args.concurrency)
    runtime["llm_concurrency_per_provider"] = 0
    cfg["runtime"] = runtime
    return cfg


def sample_core(name: str, persona: str) -> Any:
    """The L0 of a bot written from one of the sample characters: who it is, how it talks, how it thinks (valid, and about the persona only)."""
    from app.bots.types import CoreFiles

    return CoreFiles(
        identity=f"你是{name}。{persona}",
        voice=(
            f"你说话自然，不拿腔拿调，口吻和你的性格一致：{persona}一次只讲清楚一两个要点，不背稿子。"
            "「我先说说我的看法。」「这一点我还不太确定，先听听大家怎么说。」「我倾向这样想，不过随时可以改。」\n篇幅：30–120 字"
        ),
        mind=f"你的分析方式贴合你的性格：{persona}你先看已经确定的事实，再看别人的话前后是否一致；看错了就承认，换个角度重新想。",
    )


# ================================================================================================================ run
async def run(
    args: argparse.Namespace,
    *,
    port_factory: Callable[[Any], Any] | None = None,
    scratch_root: Path | None = None,
) -> int:
    """The whole smoke run.  ``port_factory`` (a test, the demo): a fake model port for the agents instead of the real models - then no key is
    needed to run and nothing is sent anywhere (the budget still counts and caps the calls)."""
    source = Path(args.source).expanduser().resolve()
    seats = [f"N{i}" for i in range(1, args.players + 1)]
    scratch = devlib.new_scratch(scratch_root if scratch_root is not None else ROOT / ".demo-data", "arena-real-smoke-")
    try:
        with devlib.scratch_environment(scratch):  # (the data directory is the scratch BEFORE anything of the application is imported)
            populate_scratch(source, scratch, seats, args.window)
            devlib.adopt_master_key(scratch)
            with devlib.patched_config(smoke_config(args, seats)):
                return await smoke(args, scratch, port_factory)
    finally:
        if args.keep:
            print("scratch kept at", scratch)
        else:
            shutil.rmtree(scratch, ignore_errors=True)


async def smoke(args: argparse.Namespace, scratch: Path, port_factory: Callable[[Any], Any] | None) -> int:
    from app.game.orchestrator import GameOrchestrator, NotReadyError
    from app.storage import reset_stores_for_tests

    devlib.require_scratch(scratch)
    reset_stores_for_tests()  # (the stores are made for the scratch directory now)
    meter = devlib.Meter(args.max_calls)
    try:
        orch = GameOrchestrator()
    except ValueError as exc:  # the engine's own check: the scenario's identity pool against the number of seats
        print("THE TABLE DOES NOT FIT THE SCENARIO:", exc)
        print(f"(--players {args.players} against --scenario {args.scenario}: the scenario must be one for exactly that many players)")
        return 2
    if port_factory is not None:
        orch.llm_port_factory = lambda o: meter.port(port_factory(o))
    else:  # every real call - the game's, the learning runs', the one-shots' - passes the client
        orch.llm = meter.client(orch.llm)
    started = time.monotonic()
    try:
        await orch.startup()
        issues = orch.refresh_binding_issues()
        if issues:
            print("BINDING ISSUES:", [(i.actor_id, i.message) for i in issues][:5])
            return 2
        bots = orch._bots().lineup()
        print(
            f"scenario={orch.spec.id} players={len(bots)} budget={args.max_calls} calls / {args.timeout:.0f}s concurrency={args.concurrency or 'unlimited'}"
            f" init={args.init}"
        )
        models = {f"{b.provider_id[:6]}…/{b.model_id}" for b in orch.session.model_bindings.values()}
        print("models:", ", ".join(sorted(models)))
        if args.core_from_sample:
            apply_core_samples(orch)
        if args.price_source:
            await apply_price_source(orch)
        if not await initialize(orch, args, meter):
            return 3
        game_started = time.monotonic()  # (the time limit of the game counts from here: the initialization had its own)
        try:
            await orch.start_or_resume()
        except NotReadyError as exc:
            print("THE GAME DID NOT START:", exc)
            for problem in exc.problems:
                print("  -", problem.get("name") or problem.get("scope"), [item["message"] for item in problem["items"]])
            return 3
        await watch(orch, meter, game_started, args.timeout)
        await asyncio.sleep(0.5)
        if args.resume_calls > 0 and orch.session.status == "paused":
            await resume_phase(orch, meter, args.resume_calls, game_started, args.timeout, scratch)
        report(orch, meter, time.monotonic() - started, scratch)
        if args.oneshots > 0:
            await exercise_oneshots(orch, args.oneshots, meter)
        return 0
    finally:
        try:
            await orch.shutdown()
        except Exception as exc:  # noqa: BLE001
            print("shutdown error:", type(exc).__name__)
        reset_stores_for_tests()


# ======================================================================================================== the bots
def apply_core_samples(orch: Any) -> None:
    """``--core-from-sample``: the L0 of the bot at seat k is written from the sample character k (before any initialization)."""
    store = orch._bots()
    written = []
    for index, bot_id in enumerate(store.lineup()):
        name = store.name_of(bot_id)
        persona = SEATS[index][1] if index < len(SEATS) else SEATS[-1][1]
        store.set_core(bot_id, sample_core(name, persona))
        written.append(name)
    print("L0 written from the sample characters:", ", ".join(written))


async def initialize(orch: Any, args: argparse.Namespace, meter: devlib.Meter) -> bool:
    """Give every bot the initialization the start check asks for.  ``True`` when all of them are ``ready`` afterwards."""
    store = orch._bots()
    ids = store.lineup()
    if args.init == "fixed":
        from app.game.demo import prepare_demo

        prepared = prepare_demo(orch)
        print(f"initialization: fixed (no model call): {len(prepared)} of {len(ids)} bots committed")
    else:
        from app.learning.jobs import LearningRefused

        try:
            job = await orch.start_init()
        except LearningRefused as exc:
            print("INITIALIZATION REFUSED:", exc)
            for problem in exc.problems:
                print("  -", problem.get("name") or problem.get("scope"), [item["message"] for item in problem["items"]])
            return False
        print(f"initialization: real - job {job['job_id']}: {len(job['ids'])} bots at the same time, at most {job['deadline_s']}s each (budget {meter.max_calls} calls)")
        deadline = time.monotonic() + args.timeout
        while orch.init_job_view() is not None:
            if time.monotonic() > deadline:
                print("time limit reached; cancelling the initialization")
                await orch.cancel_init()
                break
            await asyncio.sleep(min(POLL_S, 0.5))
        await asyncio.sleep(0.2)
        print("\n=== initialization (real) ===")
        devlib.init_report(orch)
    statuses = orch.init_statuses()
    not_ready = {bot_id: status for bot_id, status in statuses.items() if status != "ready"}
    if not_ready:
        print("BOTS THAT ARE NOT INITIALIZED (the game cannot start):", {store.name_of(b): s for b, s in not_ready.items()})
        return False
    return True


# ================================================================================================================ game
async def watch(orch: Any, meter: devlib.Meter, started: float, timeout: float) -> None:
    """Let the game run; pause it when the call budget or the time limit is reached (or leave when it stops by itself)."""
    last_print = 0.0
    while True:
        await asyncio.sleep(POLL_S)
        elapsed = time.monotonic() - started
        status = orch.session.status
        if elapsed - last_print >= 10:
            last_print = elapsed
            deadline = orch.world.next_deadline()
            busy = [a for a, ag in (orch._supervisor.agents.items() if orch._supervisor else []) if ag.status != "sleeping"]
            print(
                f"[{elapsed:5.0f}s] status={status} calls={meter.calls} ledger={orch.world.seq} clock={orch.clock.now():.0f}"
                f" next_deadline={'-' if deadline is None else f'{deadline:.0f}'} busy={busy} phase={orch.world.state.phase!r}"
            )
        if status != "running":
            print(f"game left 'running': status={status} reason={orch.session.pause_reason or '-'} {orch.session.hold_reason}")
            return
        if meter.calls >= meter.max_calls:
            print(f"call budget reached ({meter.calls}); pausing")
            await orch.pause("user")
            return
        if elapsed >= timeout:
            print("time limit reached; pausing")
            await orch.pause("user")
            return


def turn_rows(rows: list[dict]) -> list[dict]:
    """The rows of the trace that are model calls which answered a turn."""
    return [row for row in rows if row.get("event") == "llm_call" and is_successful_turn(row)]


async def resume_phase(orch: Any, meter: devlib.Meter, more: int, started: float, timeout: float, scratch: Path) -> None:
    """Pause -> resume the SAME game and run ``more`` further calls; did the provider's prompt cache survive?"""
    mark = meter.calls
    before = len(turn_rows(devlib.trace_rows(scratch, orch.session.session_id)))
    sessions_before = {a: len(c.messages) for a in orch._book.agents() if (c := orch._book.get(a)) is not None}
    prompts_before = {a: c.system for a in orch._book.agents() if (c := orch._book.get(a)) is not None}
    print(f"\n=== resume: the game is paused at call {mark}; resuming the same game for {more} more calls ===")
    meter.extend(more)  # (the budget of this phase)
    await orch.start_or_resume()
    while True:
        await asyncio.sleep(POLL_S)
        if orch.session.status != "running":
            print(f"game left 'running' after the resume: status={orch.session.status} reason={orch.session.pause_reason or '-'}")
            break
        if meter.calls >= mark + more or time.monotonic() - started >= timeout:
            await orch.pause("user")
            break
    after = turn_rows(devlib.trace_rows(scratch, orch.session.session_id))[before:]
    kept = sum(1 for a, c in prompts_before.items() if (now := orch._book.get(a)) is not None and now.system == c)
    grew = sum(1 for a, n in sessions_before.items() if (now := orch._book.get(a)) is not None and len(now.messages) >= n)
    hits = [row for row in after if int(row.get("cache_read_tokens") or 0) > 0]
    print(
        f"after the resume: {len(after)} calls, {len(hits)} reported cached input ({len(hits) / max(1, len(after)):.0%}); "
        f"cache read {sum(int(r.get('cache_read_tokens') or 0) for r in after)} vs uncached {sum(int(r.get('prompt_tokens') or 0) for r in after)} tokens"
    )
    print(f"conversations kept across the pause: {grew}/{len(sessions_before)} still start with what they had; system prompts byte-identical: {kept}/{len(prompts_before)}")
    firsts: dict[str, int] = {}
    for row in after:
        firsts.setdefault(str(row.get("agent_id")), int(row.get("cache_read_tokens") or 0))
    cold = [agent for agent, cached in firsts.items() if cached == 0]
    print("first call of each agent after the resume reported no cache hit for:", cold or "nobody")


# =========================================================================================================== one-shots
async def exercise_oneshots(orch: Any, count: int, meter: devlib.Meter) -> None:
    """Real-model check of the calls outside the game loop: the moderator's "complete the core" (``core:refine``) and the post-game review."""
    meter.extend(REFINE_CALLS + REVIEW_CALLS_PER_BOT * count)
    print("\n=== one-shot calls (core:refine / review) ===")
    store = orch._bots()
    ids = store.lineup()
    for bot_id, notes in zip(ids[:REFINE_CALLS], ("爱说冷笑话，遇事不慌", "上一局被人骗过，所以更相信事实；说话直来直去")):
        await refine_one(orch, bot_id, notes)
    # pretend the paused game has ended: the reviews read the real history of what was played
    orch.session.status = "finished"
    orch.world.state.status = "finished"
    orch.world.state.winner = orch.world.state.winner or "未分胜负"  # (a game that was paused has no winner: the reviews say the result is unknown)
    seats = [p.id for p in orch.session.participants][:count]
    mark = orch.bus.last_id
    try:
        review = await orch.start_review(seats)
    except Exception as exc:  # noqa: BLE001 - this is a probe: report it
        print(f"review REFUSED: {type(exc).__name__}: {exc}")
        return
    deadline = time.monotonic() + 300
    while orch.review_running_ids() and time.monotonic() < deadline:
        await asyncio.sleep(min(POLL_S, 0.5))
    await asyncio.sleep(0.2)
    print(f"review started for {review['started']}; done: {sorted(orch.session.review_done)}")
    replay, queue = orch.bus.subscribe(mark)
    orch.bus.unsubscribe(queue)
    final: dict[str, dict] = {}
    for event in replay or []:
        if event.type == "review.progress" and event.data.get("state") in ("done", "timeout", "error"):
            final[str(event.data["id"])] = event.data
    for seat in review["started"]:
        data = final.get(seat)
        if data is None:
            print(f"--- {seat}: no result (the review did not finish)")
            continue
        print(f"--- {seat} {data['name']}: {data['state']} - {data['message']}")
        changes = data.get("changes") or {"files": [], "proposals": 0}
        for item in changes["files"]:
            before, after = str(item["before"]), str(item["after"])
            print(f"    {item['file']}: {len(before)} -> {len(after)} chars: {after[:300]!r}")
        if changes["proposals"]:
            print(f"    {changes['proposals']} proposal(s) for the L0 (pending for the user)")
    usage = orch.usage
    print(f"one-shot calls so far: {meter.calls} (budget {meter.max_calls}); game usage total {usage.total_tokens} tokens")


async def refine_one(orch: Any, bot_id: str, notes: str) -> None:
    """One real "complete the core" call, as ``POST /api/bots/{id}/core:refine`` makes it; the draft is printed and never saved."""
    from app.bots.refine import build_core_refine_messages, clean_answer
    from app.storage import botfiles

    store = orch._bots()
    name = store.name_of(bot_id)
    try:
        messages = build_core_refine_messages(name, notes, store.core_markdown(bot_id))
        answer = await orch.complete_oneshot(MODERATOR, messages, max_tokens=REFINE_MAX_TOKENS, purpose="refine")
        text = clean_answer(answer)
        parsed = botfiles.parse_core_markdown(text, require_complete=False, lenient_limits=True)
    except Exception as exc:  # noqa: BLE001 - this is a probe: report, go on
        print(f"refine core of {name} FAILED: {type(exc).__name__}: {exc}")
        return
    sizes = {key: len(getattr(parsed.core, key)) for key in ("identity", "voice", "mind")}
    print(f"refine core of {name}: {len(text)} chars, sections {sizes}, extras {len(parsed.core.extras)}, missing {list(parsed.core.missing) or 'none'}")
    for warning in parsed.warnings:
        print(f"    warning: {warning}")
    print(f"    {text[:260]!r}")


async def apply_price_source(orch: Any) -> None:
    """Price the bound models with the LiteLLM table (in the scratch copy only; see ``devlib``)."""
    for line in await devlib.apply_price_source(orch.session.model_bindings.values()):
        print(line)


# ============================================================================================================== report
def is_successful_turn(row: dict) -> bool:
    """Is this ``llm_call`` row of the trace a model call that ANSWERED a player's or the moderator's turn?

    Since v0.13.1 the trace also holds rows for the attempts that failed or were cancelled (``status``: ``retry`` /
    ``timeout`` / ``error`` / ``cancelled``) and for the one-shot calls outside the game loop (``purpose``: ``review`` /
    ``refine``).  The rows an agent writes for its own successful calls carry neither field (older traces never do).
    Only those say anything about whether the provider's prompt cache keeps hitting across the turns of a conversation:
    a failed call reports no tokens at all, and a one-shot call starts a conversation of its own."""
    return row.get("status") in (None, "ok") and row.get("purpose") in (None, "turn")


def report(orch: Any, meter: devlib.Meter, seconds: float, scratch: Path | None = None) -> None:
    """The report of the game: ``meter`` is the call budget of the run (:class:`devlib.Meter`: how many calls, how many were refused, what the
    calls that were sent raised); ``scratch`` the data directory the game ran in (default: this process's ``ARENA_DATA_DIR`` - the run's
    scratch directory - where its trace is)."""
    scratch = Path(scratch) if scratch is not None else Path(os.environ["ARENA_DATA_DIR"])
    world = orch.world
    usage = orch.usage
    print("\n=== report ===")
    print(
        f"wall time {seconds:.0f}s | LLM calls {meter.calls} (refused over the budget: {meter.refused}) | call errors {dict(meter.errors)} | ledger entries {world.seq}"
    )
    print(
        f"tokens: uncached input {usage.prompt_tokens} | cache read {usage.cache_read_tokens} | cache write {usage.cache_write_tokens}"
        f" | output {usage.completion_tokens} | total {usage.total_tokens} | cache hit {usage.cache_hit_rate:.1%} | cost ${usage.total_cost_usd:.4f}"
    )
    print("status:", orch.session.status, "| winner:", world.state.winner, "| phase:", world.state.phase)
    types = collections.Counter(e.type for e in world.ledger)
    print("ledger:", dict(types.most_common(14)))
    for agent_id, actor in sorted(usage.by_actor.items()):
        print(
            f"  {agent_id:10s} act={actor.activations:3d} calls={actor.calls:3d} tools={actor.tool_calls:3d} think={actor.thinking_rounds:3d}"
            f" rej={actor.rejections:2d} in={actor.prompt_tokens:7d} cached={actor.cache_read_tokens:7d} ({actor.cache_hit_rate:5.1%}) out={actor.completion_tokens:5d}"
        )
    rows = devlib.trace_rows(scratch, orch.session.session_id)
    rejections: collections.Counter[str] = collections.Counter()
    rejection_text: dict[str, str] = {}
    codes: collections.Counter[str] = collections.Counter()
    tool_names: collections.Counter[str] = collections.Counter()
    errors: list[str] = []
    injections: collections.Counter[str] = collections.Counter()
    merged_reasons = 0
    lifecycle: collections.Counter[str] = collections.Counter()  # compaction events (see --window)
    later_calls = later_cached = 0  # calls after an agent's first one: they must hit the prompt cache
    seen_first: set[str] = set()
    mounts: dict[str, list[dict]] = {}  # what each agent started with (the first mount of each)
    hints: collections.Counter[str] = collections.Counter()
    for row in rows:
        event = row.get("event")
        if event == "tool_call":
            tool_names[f"{row.get('agent_id')}:{row.get('name')}" if row.get("agent_id") == "moderator" else str(row.get("name"))] += 1
        elif event == "receipt":
            if row.get("ok") is False:
                code = str(row.get("code"))
                key = f"{row.get('name')} -> {code}"
                codes[code] += 1
                rejections[key] += 1
                rejection_text.setdefault(key, str(row.get("message") or "")[:140])
        elif event == "inject":
            injections[str(row.get("kind"))] += 1
            if row.get("kind") == "merge":
                merged_reasons += len(row.get("reasons") or [])
        elif event == "llm_call":
            if not is_successful_turn(row):
                continue  # (a failed / cancelled attempt, a review or a "complete it": not a turn of a conversation)
            agent = str(row.get("agent_id"))
            if agent in seen_first:
                later_calls += 1
                later_cached += 1 if int(row.get("cache_read_tokens") or 0) > 0 else 0
            seen_first.add(agent)
        elif event == "mounts":
            mounts.setdefault(str(row.get("agent_id")), [b for b in (row.get("blocks") or []) if isinstance(b, dict)])
        elif event == "hint":
            hints[str(row.get("kind") or "hint")] += 1
        elif event in ("compact_nudge", "rebuild", "trim", "trim_failed", "discard", "long_run"):
            lifecycle[str(event)] += 1
        elif event in ("error", "give_up", "retry"):
            errors.append(f"{event}: {str(row.get('message') or row.get('error') or row)[:200]}")
    print("tool usage:", dict(tool_names.most_common(25)))
    merges = injections.get("merge", 0)
    print(
        f"activations {injections.get('activation', 0)} | in-flight merges {merges}"
        f" (avg {merged_reasons / merges:.1f} messages per merge)" if merges else f"activations {injections.get('activation', 0)} | in-flight merges 0"
    )
    if mounts:
        per_layer: collections.Counter[str] = collections.Counter()
        for blocks in mounts.values():
            for block in blocks:
                per_layer[str(block.get("layer"))] += int(block.get("chars") or 0)
        print(
            "mounted at the start of a conversation (average per agent, chars): "
            + ", ".join(f"{layer} {chars // len(mounts)}" for layer, chars in sorted(per_layer.items()))
        )
    if hints:
        print("hints shown with the messages:", dict(hints))
    if lifecycle:
        print("conversation lifecycle:", dict(lifecycle))
    if later_calls:
        print(f"prompt cache: {later_cached}/{later_calls} calls after an agent's first one reported cached input ({later_cached / later_calls:.0%})")
    print(
        "rejections by the engine (receipts):",
        f"TOO_LONG x{codes.get('TOO_LONG', 0)}, FORMAT x{codes.get('FORMAT', 0)}"
        + (f", others {dict((c, n) for c, n in codes.items() if c not in ('TOO_LONG', 'FORMAT'))}" if set(codes) - {"TOO_LONG", "FORMAT"} else ""),
    )
    if rejections:
        for text, count in rejections.most_common(12):
            print(f"  x{count} {text}: {rejection_text.get(text, '')}")
    if errors:
        print("runtime errors:", errors[:6])
    timeline = [e for e in world.ledger if e.type.startswith("floor.") or e.type in ("request.opened", "request.closed", "game.status")]
    print(f"floor timeline (game seconds; the first {min(len(timeline), TIMELINE_LINES)} of {len(timeline)} entries):")
    for entry in timeline[:TIMELINE_LINES]:
        d = entry.data
        print(f"  #{entry.seq:3d} t={entry.gt:6.1f} {entry.type:20s} {d.get('holder', '')} {d.get('reason', '')} {d.get('request_id', '')}")
    print("last public messages:")
    for entry in [e for e in world.ledger if e.type == "message"][-8:]:
        print(f"  #{entry.seq} [{entry.channel_id}] {world.display_name(entry.actor_id)}: {str(entry.data.get('content', ''))[:90]}")


def main(argv: list[str] | None = None) -> int:
    stdout = getattr(sys.stdout, "reconfigure", None)
    if callable(stdout):  # (a Windows console may not be UTF-8; the report has Chinese in it)
        stdout(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="LOG %(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
