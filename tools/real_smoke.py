"""Real-model smoke test for the v0.12.1 engine (costs API credits — it has a hard call budget).

It plays a *small* game with the models that are bound in a workspace's saved default bindings, and
prints a compact report: tool usage, rejection codes, the four kinds of tokens, the prompt-cache hit rate,
how many activations each agent had and how many messages were merged into a running activation.
Nothing is shown from the key store; the provider registry is only used by the engine to make the calls.

Isolation: the encrypted provider registry and the DPAPI key wrapper are COPIED from ``--from`` into
a private scratch directory, so the live workspace (which another running instance may hold locked)
is never opened or modified; the copy is deleted at the end (``--keep`` keeps it).

Examples::

    python tools/real_smoke.py --from "C:/Users/me/Arena-Agents/Default/.arena" --players 4 --max-calls 60
    python tools/real_smoke.py --from ... --scenario scenarios/werewolf_12p.md --players 12 --max-calls 80
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import copy
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", required=True, help="a workspace's .arena directory (or a data dir) holding providers.v1.enc")
    parser.add_argument("--scenario", default="scenarios/minimal_test.md")
    parser.add_argument("--players", type=int, default=4)
    parser.add_argument("--max-calls", type=int, default=60, help="hard budget of LLM calls; the game is paused when reached")
    parser.add_argument("--timeout", type=float, default=300.0, help="wall-clock limit in seconds")
    parser.add_argument("--concurrency", type=int, default=0, help="limit of concurrent model calls (0 = unlimited, the engine default)")
    parser.add_argument("--persona", action="store_true", help="give the players the sample personas of this script (written to the scratch roster)")
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
        help="after the game: also try the two calls outside the game loop with the real models - the moderator's "
        "\"complete it\" (persona + memory drafts) and the post-game review of the first N players (the paused game is "
        "treated as finished, so the reviews read what was really played)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory (it holds an encrypted copy of the providers)")
    return parser.parse_args()


def prepare_scratch(source: Path, players: list[str], window: int = 0) -> Path:
    scratch = Path(tempfile.mkdtemp(prefix="arena-real-smoke-", dir=str(ROOT / ".demo-data")))
    for name in ("providers.v1.enc", "master.v1.dpapi"):
        src = source / name
        if not src.is_file():
            raise SystemExit(f"missing {name} in {source}")
        shutil.copy2(src, scratch / name)
    settings = json.loads((source / "settings.json").read_text(encoding="utf-8")) if (source / "settings.json").is_file() else {}
    bindings = settings.get("default_bindings") or {}
    wanted = {actor: bindings[actor] for actor in ["moderator", *players] if actor in bindings}
    missing = [actor for actor in ["moderator", *players] if actor not in wanted]
    if missing:
        # re-use the first available binding for seats that have none (ids only, no secrets)
        fallback = next(iter(bindings.values()), None)
        if fallback is None:
            raise SystemExit("no default model bindings found; bind models in the UI first")
        for actor in missing:
            wanted[actor] = fallback
    if window > 0:  # a short context window on purpose (see --window)
        wanted = {actor: {**binding, "context_window_tokens": window} for actor, binding in wanted.items()}
    # Only the model bindings (ids, no secrets); no last-session pointer, so nothing is restored.
    (scratch / "settings.json").write_text(
        json.dumps(
            {"schema_version": 1, "default_bindings": wanted, "last_session_id": None, "ui": {}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return scratch


async def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.WARNING, format="LOG %(levelname)s %(name)s: %(message)s")
    source = Path(args.source).expanduser().resolve()
    ids = [f"N{i}" for i in range(1, args.players + 1)]
    (ROOT / ".demo-data").mkdir(exist_ok=True)
    scratch = prepare_scratch(source, ids, args.window)
    os.environ["ARENA_DISABLE_WORKSPACE"] = "1"
    os.environ["ARENA_DISABLE_LEGACY_MIGRATION"] = "1"
    os.environ["ARENA_DATA_DIR"] = str(scratch)
    os.environ.pop("ARENA_MASTER_KEY", None)
    os.environ.pop("ARENA_MASTER_KEY_FILE", None)
    os.environ["PYTHONUTF8"] = "1"

    import app.game.orchestrator as orchestrator_module
    from app.config import load_config
    from app.game.orchestrator import GameOrchestrator
    from app.runtime.llm_gate import GatedEngineLlm, LlmGate
    from app.storage import reset_stores_for_tests

    reset_stores_for_tests()
    cfg = load_config()
    cfg["scenario"] = {"path": args.scenario}
    cfg["participants"] = [
        {"id": pid, "seat": str(index), "name": SEATS[index - 1][0]} for index, pid in enumerate(ids, start=1)
    ]
    orchestrator_module.load_config = lambda *a, **k: copy.deepcopy(cfg)
    if args.persona:  # personas live in the roster (plain files), exactly like a user's
        (scratch / "roster").mkdir(exist_ok=True)
        for index, pid in enumerate(ids, start=1):
            (scratch / "roster" / f"{pid}.persona.md").write_text(SEATS[index - 1][1] + "\n", encoding="utf-8")

    calls = {"n": 0, "errors": collections.Counter(), "log": []}  # log: (call number, agent, uncached, cache read)

    class Counting:
        def __init__(self, inner: GatedEngineLlm) -> None:
            self.inner = inner

        async def complete(self, agent_id, messages, tools, *, max_tokens=None):
            calls["n"] += 1
            number = calls["n"]
            try:
                resp = await self.inner.complete(agent_id, messages, tools, max_tokens=max_tokens)
                calls["log"].append((number, agent_id, int(resp.prompt_tokens or 0), int(getattr(resp, "cache_read_tokens", 0) or 0)))
                return resp
            except Exception as exc:  # noqa: BLE001 - reported, then re-raised for the runtime's own handling
                calls["errors"][type(exc).__name__] += 1
                raise

    orch = GameOrchestrator()
    orch.llm_port_factory = lambda o: Counting(
        GatedEngineLlm(o.llm, o.session.get_model_binding, LlmGate(max_concurrent=args.concurrency, per_provider=0), 120.0)
    )
    started = time.monotonic()
    try:
        await orch.startup()
        await orch.new_game()
        if args.price_source:
            await apply_price_source(orch)
        issues = orch.refresh_binding_issues()
        if issues:
            print("BINDING ISSUES:", [(i.actor_id, i.message) for i in issues][:5])
            return 2
        print(f"scenario={orch.spec.id} players={len(ids)} budget={args.max_calls} calls / {args.timeout:.0f}s concurrency={args.concurrency or 'unlimited'}")
        models = {f"{b.provider_id[:6]}…/{b.model_id}" for b in orch.session.model_bindings.values()}
        print("models:", ", ".join(sorted(models)))
        await orch.start_or_resume()
        last_print = 0.0
        while True:
            await asyncio.sleep(1.0)
            elapsed = time.monotonic() - started
            status = orch.session.status
            if elapsed - last_print >= 10:
                last_print = elapsed
                deadline = orch.world.next_deadline()
                busy = [a for a, ag in (orch._supervisor.agents.items() if orch._supervisor else []) if ag.status != "sleeping"]
                print(
                    f"[{elapsed:5.0f}s] status={status} calls={calls['n']} ledger={orch.world.seq} clock={orch.clock.now():.0f}"
                    f" next_deadline={'-' if deadline is None else f'{deadline:.0f}'} busy={busy} phase={orch.world.state.phase!r}"
                )
            if status != "running":
                print(f"game left 'running': status={status} reason={orch.session.pause_reason or '-'} {orch.session.hold_reason}")
                break
            if calls["n"] >= args.max_calls:
                print(f"call budget reached ({calls['n']}); pausing")
                await orch.pause("user")
                break
            if elapsed >= args.timeout:
                print("time limit reached; pausing")
                await orch.pause("user")
                break
        await asyncio.sleep(0.5)
        if args.resume_calls > 0 and orch.session.status == "paused":
            await resume_phase(orch, calls, args.resume_calls, started, args.timeout)
        report(orch, calls, time.monotonic() - started)
        if args.oneshots > 0:
            await exercise_oneshots(orch, args.oneshots)
        return 0
    finally:
        try:
            await orch.shutdown()
        except Exception as exc:  # noqa: BLE001
            print("shutdown error:", type(exc).__name__)
        reset_stores_for_tests()
        if args.keep:
            print("scratch kept at", scratch)
        else:
            shutil.rmtree(scratch, ignore_errors=True)


async def resume_phase(orch, calls, more: int, started: float, timeout: float) -> None:
    """Pause -> resume the SAME game and run ``more`` further calls; did the provider's prompt cache survive?"""
    mark = calls["n"]
    sessions_before = {a: len(c.messages) for a in orch._book.agents() if (c := orch._book.get(a)) is not None}
    prompts_before = {a: c.system for a in orch._book.agents() if (c := orch._book.get(a)) is not None}
    print(f"\n=== resume: the game is paused at call {mark}; resuming the same game for {more} more calls ===")
    await orch.start_or_resume()
    while True:
        await asyncio.sleep(1.0)
        if orch.session.status != "running":
            print(f"game left 'running' after the resume: status={orch.session.status} reason={orch.session.pause_reason or '-'}")
            break
        if calls["n"] >= mark + more or time.monotonic() - started >= timeout:
            await orch.pause("user")
            break
    after = [row for row in calls["log"] if row[0] >= mark]
    kept = sum(1 for a, c in prompts_before.items() if (now := orch._book.get(a)) is not None and now.system == c)
    grew = sum(1 for a, n in sessions_before.items() if (now := orch._book.get(a)) is not None and len(now.messages) >= n)
    hits = [row for row in after if row[3] > 0]
    print(
        f"after the resume: {len(after)} calls, {len(hits)} reported cached input ({len(hits) / max(1, len(after)):.0%}); "
        f"cache read {sum(r[3] for r in after)} vs uncached {sum(r[2] for r in after)} tokens"
    )
    print(f"conversations kept across the pause: {grew}/{len(sessions_before)} still start with what they had; system prompts byte-identical: {kept}/{len(prompts_before)}")
    firsts = {}
    for number, agent, uncached, cached in after:
        firsts.setdefault(agent, (uncached, cached))
    cold = [a for a, (u, c) in firsts.items() if c == 0]
    print("first call of each agent after the resume reported no cache hit for:", cold or "nobody")


async def exercise_oneshots(orch, count: int) -> None:
    """Real-model check of the calls outside the game loop: "complete it" and the post-game review."""
    print("\n=== one-shot calls (refine / review) ===")
    for agent_id, kind, notes in (("N1", "persona", "爱说冷笑话，遇事不慌"), ("N2", "memory", "上局被 N5 骗过；常因为太早跳身份被刀")):
        try:
            draft = await orch.refine_text(agent_id, kind, notes)
            print(f"refine {kind:7s} of {agent_id}: {len(draft)} chars: {draft[:260]!r}")
        except Exception as exc:  # noqa: BLE001 - this is a probe: report, go on
            print(f"refine {kind} of {agent_id} FAILED: {type(exc).__name__}: {exc}")
    # pretend the paused game has ended: the reviews read the real history of what was played
    orch.session.status = "finished"
    orch.world.state.status = "finished"
    orch.world.state.winner = "好人阵营"
    ids = [p.id for p in orch.session.participants][:count]
    started = await orch.start_review(ids)
    deadline = time.monotonic() + 300
    while orch._review_running and time.monotonic() < deadline:
        await asyncio.sleep(1.0)
    print(f"review started for {started}; done: {sorted(orch.session.review_done)}")
    for agent_id in started:
        memory = orch._roster().entry(agent_id)["memory"]
        print(f"--- {agent_id} memory ({len(memory)} chars) ---\n{memory[:900]}")
    usage = orch.usage.actor("roster")
    print(f"one-shot refine calls: {usage.calls}, tokens {usage.prompt_tokens}+{usage.cache_read_tokens}+{usage.completion_tokens}")


async def apply_price_source(orch) -> None:
    """Price the bound models with the LiteLLM table (in the scratch copy only; see ``devlib``)."""
    from devlib import apply_price_source as apply

    for line in await apply(orch.session.model_bindings.values()):
        print(line)


def report(orch, calls, seconds: float) -> None:
    world = orch.world
    usage = orch.usage
    print("\n=== report ===")
    print(f"wall time {seconds:.0f}s | LLM calls {calls['n']} | call errors {dict(calls['errors'])} | ledger entries {world.seq}")
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
    log_root = Path(os.environ["ARENA_DATA_DIR"]) / "logs" / orch.session.session_id
    files = sorted(log_root.glob("trace-*.jsonl")) if log_root.is_dir() else []
    rejections: collections.Counter[str] = collections.Counter()
    tool_names: collections.Counter[str] = collections.Counter()
    errors: list[str] = []
    injections: collections.Counter[str] = collections.Counter()
    merged_reasons = 0
    lifecycle: collections.Counter[str] = collections.Counter()  # compaction events (see --window)
    later_calls = later_cached = 0  # calls after an agent's first one: they must hit the prompt cache
    seen_first: set[str] = set()
    for file in files:
        for line in file.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("event") == "tool_call":
                tool_names[f"{row.get('agent_id')}:{row.get('name')}" if row.get("agent_id") == "moderator" else str(row.get("name"))] += 1
                result = str(row.get("result", ""))
                if '"ok":false' in result:
                    rejections[f"{row.get('name')} -> {result[:150]}"] += 1
            elif row.get("event") == "inject":
                injections[str(row.get("kind"))] += 1
                if row.get("kind") == "merge":
                    merged_reasons += len(row.get("reasons") or [])
            elif row.get("event") == "llm_call":
                agent = str(row.get("agent_id"))
                if agent in seen_first:
                    later_calls += 1
                    later_cached += 1 if int(row.get("cache_read_tokens") or 0) > 0 else 0
                seen_first.add(agent)
            elif row.get("event") in ("compact_nudge", "rebuild", "trim", "trim_failed", "discard", "long_run"):
                lifecycle[str(row.get("event"))] += 1
            elif row.get("event") in ("error", "give_up", "retry"):
                errors.append(f"{row.get('event')}: {str(row.get('message') or row.get('error') or row)[:200]}")
    print("tool usage:", dict(tool_names.most_common(25)))
    merges = injections.get("merge", 0)
    print(
        f"activations {injections.get('activation', 0)} | in-flight merges {merges}"
        f" (avg {merged_reasons / merges:.1f} messages per merge)" if merges else f"activations {injections.get('activation', 0)} | in-flight merges 0"
    )
    if lifecycle:
        print("conversation lifecycle:", dict(lifecycle))
    if later_calls:
        print(f"prompt cache: {later_cached}/{later_calls} calls after an agent's first one reported cached input ({later_cached / later_calls:.0%})")
    if rejections:
        print("rejected tool calls:")
        for text, count in rejections.most_common(12):
            print(f"  x{count} {text}")
    if errors:
        print("runtime errors:", errors[:6])
    print("floor timeline (game seconds):")
    for entry in world.ledger:
        if entry.type.startswith("floor.") or entry.type in ("request.opened", "request.closed", "game.status"):
            d = entry.data
            print(f"  #{entry.seq:3d} t={entry.gt:6.1f} {entry.type:20s} {d.get('holder', '')} {d.get('reason', '')} {d.get('request_id', '')}")
    print("last public messages:")
    for entry in [e for e in world.ledger if e.type == "message"][-8:]:
        print(f"  #{entry.seq} [{entry.channel_id}] {world.display_name(entry.actor_id)}: {str(entry.data.get('content', ''))[:90]}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
