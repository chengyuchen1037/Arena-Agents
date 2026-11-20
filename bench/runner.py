"""The runner: bot × situation × sample -> the REAL ``Agent`` / worker loop -> ``results.jsonl`` and ``judge_items.jsonl`` (H18, H47).

For every job the runner plays the situation (``bench.play``: the real engine, the world stops at a decision point), gives the seat under test
the brain of the bot being tried (``BenchBot.factory``: ``LegacyBrain`` for the v0.13.1 baseline, ``BotBrain`` for v0.14) and starts the real
``app.runtime.agent.Agent`` for that seat with the envelopes that wake it.  The agent does what an agent does: opens its conversation with the
brain's FULL message, calls the model, runs the tools it asks for through the brain and the commit gate, sends refusals (``FORMAT`` /
``TOO_LONG``) back for it to rewrite.  The run ends when the bot itself makes its move - an accepted ``speak`` / ``pass`` / ``respond`` ... -
or stops (``sleep``, a plain answer), or a limit is hit.  **Nothing in this module writes, rewrites, cuts or asks for a speech**: the model's
own tool call is the only source of what the bot says, and every row carries a check that what reached the ledger is what the model wrote
(``integrity.speech_verbatim``).  The other seats and the moderator are not run: the situation is frozen at the decision point.

The model is a ``LlmPort`` given by ``llm_factory(job, scene)``: ``bench.fakes`` (scripted, CI) or ``bench.realllm`` (a real model, on request
only).  Every call is counted against a :class:`Budget`; a call the budget does not allow is never made.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agents.gates import check_speech
from app.runtime.agent import Agent, AgentConfig
from app.runtime.conversation import ConversationBook
from bench.bots import BenchBot
from bench.metrics import inline_enumeration, length_band, runtime_health
from bench.play import PlayError, Scene, build_scene
from bench.safety import scrub
from bench.situation import Situation, SituationError

RESULT_FORMAT = "arena-bench-result"
JUDGE_FORMAT = "arena-judge-item"
RUN_FORMAT = "arena-bench-run"
ROW_VERSION = 1
#: The tools whose calls are listed as *attempts* of the bot's move (a refused one is there too: the rewrite after it is what R2 counts).
MOVE_TOOLS = ("speak", "speak_template", "pass", "respond", "use_ability", "challenge_moderator")
REWRITE_CODES = ("FORMAT", "TOO_LONG")
RESULT_CAP = 600  # characters of a tool result kept in a row
THOUGHT_CAP = 6000  # characters of the bot's thinking kept in a row

#: How a run of the agent ended, by the reason the ``Agent`` traces in ``run_end``.
_RUN_END = {"ends_run": "slept", "no_tool": "no_tool", "nothing": "nothing", "stale": "stale"}
STOP_INCOMPLETE = ("max_rounds", "timeout", "budget")
STOP_NO_DECISION = ("slept", "no_tool", "nothing", "stale")


# ---------------------------------------------------------------------------------------------------------------------- jobs & limits
@dataclass(frozen=True)
class Job:
    """One sample: a bot plays one seat of one situation."""

    situation: Situation
    bot: BenchBot
    seat: str
    sample: int

    @property
    def key(self) -> tuple[str, str, str, int]:
        return (self.situation.id, self.seat, self.bot.label, self.sample)


@dataclass(frozen=True)
class RunConfig:
    samples: int = 1
    max_rounds: int = 12  # model calls per sample (a refused speech and its rewrite are two of them)
    timeout_s: float = 300.0  # wall clock per sample
    concurrency: int = 1
    max_calls: int | None = None  # the whole run's budget of model calls (None = unlimited: only for the fake model)
    mode: str = "fake"  # fake | real
    model: str = "fake"  # the model's name, for the rows
    run_id: str = ""
    context_window: int = 0  # tokens; 0 = unknown (no compaction reminders)
    agent_retries: int = 1  # the agent's own re-attempts of a failed run
    thought_chars: int = THOUGHT_CAP
    dump_prompts: bool = False  # keep the whole conversation of every sample (out/prompts/)


class Budget:
    """The calls a run may still make.  ``take()`` is asked before every model call; ``False`` = the call must not be made."""

    def __init__(self, max_calls: int | None = None) -> None:
        self.max_calls = max_calls
        self.calls = 0

    @property
    def exhausted(self) -> bool:
        return self.max_calls is not None and self.calls >= self.max_calls

    def take(self) -> bool:
        if self.exhausted:
            return False
        self.calls += 1
        return True


class SampleTrace:
    """The ``TraceSink`` of one sample: keeps every row the agent writes and says when the sample is over - the bot's move was accepted
    (``decision``), the run ended without one (``slept`` / ``no_tool``), the agent gave up (``error``) or a limit was hit."""

    def __init__(self, expect: Iterable[str]) -> None:
        self.rows: list[dict[str, Any]] = []
        self.expect = frozenset(expect)
        self.done = asyncio.Event()
        self.stop = ""
        self._t0 = time.monotonic()

    def write(self, event: str, agent_id: str, **fields: Any) -> None:
        self.rows.append({"event": event, "agent_id": agent_id, "t": round(time.monotonic() - self._t0, 3), **fields})
        if self.done.is_set():
            return
        if event == "receipt" and fields.get("ok") and fields.get("name") in self.expect:
            self.finish("decision")
        elif event == "run_end":
            self.finish(_RUN_END.get(str(fields.get("reason")), str(fields.get("reason") or "run_end")))
        elif event == "give_up":
            self.finish("error")

    def finish(self, reason: str) -> None:
        if not self.done.is_set():
            self.stop = reason
            self.done.set()


class NullSink:
    """The ``EventSink`` of a sample: nobody watches."""

    def publish(self, event: Any) -> None:
        return None


class GuardedLlm:
    """The model port the agent talks to: the real or fake port behind a stop sign.  Once the sample is over, or the rounds / the budget are
    used up, a call is *parked* (it never reaches the model and never returns) until the runner stops the agent - so nothing is spent after
    the bot's move, and a limit never raises into the agent (which would retry)."""

    def __init__(self, inner: Any, trace: SampleTrace, *, max_rounds: int, budget: Budget) -> None:
        self.inner = inner
        self.trace = trace
        self.max_rounds = max_rounds
        self.budget = budget
        self.calls = 0

    async def complete(
        self, agent_id: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, *, max_tokens: int | None = None
    ) -> Any:
        if self.trace.done.is_set():
            await self._park()
        if self.calls >= self.max_rounds:
            self.trace.finish("max_rounds")
            await self._park()
        if not self.budget.take():
            self.trace.finish("budget")
            await self._park()
        self.calls += 1
        return await self.inner.complete(agent_id, messages, tools, max_tokens=max_tokens)

    @staticmethod
    async def _park() -> None:
        await asyncio.Event().wait()  # until the runner cancels the worker


LlmFactory = Callable[[Job, Scene], Any]


# ---------------------------------------------------------------------------------------------------------------------- reading the trace
def _json(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _cap(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def extract_calls(rows: Iterable[Mapping[str, Any]], secrets: Sequence[str] = ()) -> list[dict[str, Any]]:
    """The tool calls of a sample in order, each with its receipt: ``{i, round, name, args, ok, code, message, seq, data, receipt, result}``
    (``receipt`` = it went through the commit gate; a note, ``sleep`` or ``read_contract`` has none and its ``ok`` is what the tool said)."""
    calls: list[dict[str, Any]] = []
    round_no = -1
    last: dict[str, Any] | None = None
    for row in rows:
        event = row.get("event")
        if event == "llm_call":
            round_no = int(row.get("round") or 0)
        elif event == "tool_call":
            body = _json(row.get("result"))
            last = {
                "i": len(calls), "round": round_no, "name": str(row.get("name") or ""), "args": row.get("args") if isinstance(row.get("args"), dict) else {},
                "ok": body.get("ok") if isinstance(body.get("ok"), bool) else None, "code": body.get("code"), "message": body.get("message") or body.get("error"),
                "seq": body.get("seq"), "data": body.get("data") if isinstance(body.get("data"), dict) else {}, "receipt": False,
                "result": scrub(_cap(row.get("result"), RESULT_CAP), secrets), "ends_run": bool(row.get("ends_run")),
            }
            calls.append(last)
        elif event == "receipt" and last is not None and not last["receipt"] and last["name"] == row.get("name"):
            last.update(ok=bool(row.get("ok")), code=row.get("code"), message=row.get("message") or last["message"], seq=row.get("seq"), receipt=True)
    return calls


def attempts_of(calls: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The bot's moves in order, refused ones included: ``{i, round, tool, ok, code, message, channel_id, text | value, chars, rules}``."""
    out: list[dict[str, Any]] = []
    for call in calls:
        if call["name"] not in MOVE_TOOLS:
            continue
        args = call["args"]
        item: dict[str, Any] = {"i": call["i"], "round": call["round"], "tool": call["name"], "ok": bool(call["ok"]) if call["receipt"] else False,
                                "code": call["code"], "message": _cap(call["message"], 300)}
        if call["name"] in ("speak", "challenge_moderator"):
            text = str(args.get("content") or "")
            item.update(channel_id=args.get("channel_id"), text=text, chars=len(text))
        elif call["name"] == "respond":
            item.update(request_id=args.get("request_id"), value=args.get("value"), reason=args.get("reason"))
        elif call["name"] == "speak_template":
            item.update(channel_id=args.get("channel_id"), template_id=args.get("template_id"), params=args.get("params"))
        elif call["name"] == "pass":
            item.update(channel_id=args.get("channel_id"))
        elif call["name"] == "use_ability":
            item.update(ability_id=args.get("ability_id"), params=args.get("params"))
        rules = call["data"].get("rules") if isinstance(call.get("data"), dict) else None
        if rules:
            item["rules"] = list(rules)
        out.append(item)
    return out


def decision_of(calls: Sequence[Mapping[str, Any]], expect: Iterable[str], scene: Scene) -> dict[str, Any] | None:
    """The first accepted move of an expected tool - what the bot decided - with what reached the ledger (``text``)."""
    wanted = set(expect)
    refused_before = 0
    rewrote = False
    for call in calls:
        if call["name"] not in wanted:
            continue
        if not (call["receipt"] and call["ok"]):
            refused_before += 1
            rewrote = rewrote or call["code"] in REWRITE_CODES
            continue
        args = call["args"]
        seq = call["seq"] if isinstance(call["seq"], int) else None
        entry = scene.world.ledger[seq - 1] if seq and 0 < seq <= len(scene.world.ledger) else None
        text = None
        if entry is not None and entry.type == "message":
            text = str(entry.data.get("content") or "")
        elif call["name"] == "speak":
            text = str(args.get("content") or "")
        return {
            "tool": call["name"], "i": call["i"], "round": call["round"], "seq": seq, "channel_id": args.get("channel_id"),
            "request_id": args.get("request_id"), "ability_id": args.get("ability_id"), "text": text, "value": args.get("value"),
            "reason": args.get("reason"), "template_id": args.get("template_id"), "params": args.get("params"),
            "attempt": refused_before, "rewrote": rewrote,
        }
    return None


def score_of(decision: Mapping[str, Any] | None, scene: Scene) -> dict[str, Any] | None:
    """The objective score of a ``respond`` decision (the situation's ``score`` list: ``choice`` = the value is one of the ``correct`` options)."""
    if decision is None:
        return None
    for rule in scene.situation.score:
        if rule.get("kind") != "choice" or decision["tool"] != "respond":
            continue
        request = str(rule["request"])
        if request.startswith("@"):
            label = request[1:].split(".", 1)[0]
            request = str(scene.runner.labels.get(label, {}).get("request_id") or "")
        if decision.get("request_id") != request:
            continue
        value = str(decision.get("value"))
        return {"kind": "choice", "request": request, "value": value, "correct": value in rule["correct"], "wrong": value in rule.get("wrong", [])}
    return None


def speech_metrics(text: str | None, bot: BenchBot, seat: str, name: str) -> dict[str, Any] | None:
    """The S1 facts of one speech: the gate's rules it breaks, an inline enumeration, how long it is against the bot's declared band."""
    if not text:
        return None
    problems = [p.rule for p in check_speech(text, ids=[seat], names=[name], allow_lists=bot.oral_lists)]
    band = length_band(bot.voice)
    chars = len(text.strip())
    return {
        "chars": chars, "problems": problems, "inline_enum": inline_enumeration(text),
        "paragraphs": sum(1 for line in text.splitlines() if line.strip()),
        "band": list(band) if band else None, "in_band": (band[0] <= chars <= band[1]) if band else None,
    }


def usage_of(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    llm = [r for r in rows if r.get("event") == "llm_call"]
    return {
        "llm_calls": len(llm),
        "prompt": sum(int(r.get("prompt_tokens") or 0) for r in llm),
        "cache_read": sum(int(r.get("cache_read_tokens") or 0) for r in llm),
        "completion": sum(int(r.get("completion_tokens") or 0) for r in llm),
    }


def _sha(text: Any) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------------------------------------------------- one sample
def _base_row(job: Job, cfg: RunConfig) -> dict[str, Any]:
    return {
        "format": RESULT_FORMAT, "version": ROW_VERSION, "run": cfg.run_id, "bot": job.bot.label, "brain": job.bot.brain,
        "situation": job.situation.id, "seat": job.seat, "sample": job.sample, "scenario": job.situation.scenario,
        "seed": job.situation.seed, "mode": cfg.mode, "model": cfg.model, "tags": list(job.situation.tags), "meta": dict(job.situation.meta),
    }


def _failure(job: Job, cfg: RunConfig, stop: str, error: str, started: float, secrets: Sequence[str]) -> dict[str, Any]:
    return {
        **_base_row(job, cfg), "status": "error", "stop": stop, "error": scrub(_cap(error, 500), secrets), "decision": None, "attempts": [],
        "tool_calls": [], "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


async def run_sample(
    job: Job, cfg: RunConfig, llm_factory: LlmFactory, budget: Budget, *, secrets: Sequence[str] = (), prompts_dir: Path | None = None
) -> dict[str, Any]:
    """Play the situation, run the agent of ``job.bot`` on the decision seat until it moves (or stops), and describe what happened as one
    ``results.jsonl`` row.  Never raises for a failing sample: the row says what failed (``status: error``)."""
    started = time.monotonic()
    try:
        scene = build_scene(job.situation, seat=job.seat, seat_name=job.bot.name or None)
    except (SituationError, PlayError) as exc:
        return _failure(job, cfg, "scene", str(exc), started, secrets)
    try:
        brain = job.bot.make_brain(scene.seat, scene.world, scene.spec)
        llm = llm_factory(job, scene)
    except Exception as exc:  # noqa: BLE001 - a bot that cannot be made fails its own samples, not the run
        return _failure(job, cfg, "bot", f"{type(exc).__name__}: {exc}", started, secrets)
    trace = SampleTrace(job.situation.decision.expect)
    guarded = GuardedLlm(llm, trace, max_rounds=cfg.max_rounds, budget=budget)
    book = ConversationBook()
    window = cfg.context_window
    agent = Agent(
        scene.seat, "player", scene.world, brain, guarded, None, trace, NullSink(),
        AgentConfig(llm_retries=cfg.agent_retries, retry_backoff_s=0.2, retry_backoff_max_s=1.0, cancel_grace_s=2.0),
        book=book, context_window=(lambda: window) if window > 0 else None,
    )
    agent.activate()
    try:
        for envelope in scene.wake:
            agent.deliver(envelope)
        try:
            await asyncio.wait_for(trace.done.wait(), timeout=cfg.timeout_s)
        except asyncio.TimeoutError:
            trace.finish("timeout")
    finally:
        await agent.stop()
    row = build_row(job, cfg, scene, trace, book, started, secrets)
    if cfg.dump_prompts and prompts_dir is not None:
        _dump_conversation(prompts_dir, job, book, scene.seat, secrets)
    return row


def build_row(job: Job, cfg: RunConfig, scene: Scene, trace: SampleTrace, book: ConversationBook, started: float, secrets: Sequence[str] = ()) -> dict[str, Any]:
    rows = trace.rows
    calls = extract_calls(rows, secrets)
    attempts = attempts_of(calls)
    decision = decision_of(calls, scene.situation.decision.expect, scene)
    stop = "decision" if decision is not None else trace.stop or "error"
    if decision is not None:
        status = "ok"
    elif stop in STOP_NO_DECISION:
        status = "no_decision"
    elif stop in STOP_INCOMPLETE:
        status = "incomplete"
    else:
        status = "error"
    thoughts = [scrub(str(r.get("text") or ""), secrets) for r in rows if r.get("event") == "thought"]
    monologues = [scrub(str(r.get("text") or ""), secrets) for r in rows if r.get("event") == "content"]
    errors = [
        {"event": r["event"], "message": scrub(_cap(r.get("message"), 300), secrets), "error_class": r.get("error_class")}
        for r in rows if r.get("event") in ("error", "give_up")
    ]
    refusals = Counter(str(c["code"]) for c in calls if c["receipt"] and not c["ok"])
    conv = book.get(scene.seat)
    first = ""
    tool_names: list[str] = []
    system = ""
    if conv is not None:
        system = conv.system
        first = str(conv.messages[0].get("content") or "") if conv.messages else ""
        tool_names = [t["function"]["name"] for t in conv.tools]
    text = decision.get("text") if decision else None
    verbatim: bool | None = None
    if decision is not None and decision["tool"] == "speak":
        wrote = next((c["args"].get("content") for c in calls if c["i"] == decision["i"]), None)
        verbatim = text == wrote  # H18: what reached the ledger is exactly what the model wrote
    return {
        **_base_row(job, cfg),
        "status": status,
        "stop": stop,
        "decision": decision,
        "correct": (score_of(decision, scene) or {}).get("correct"),
        "score": score_of(decision, scene),
        "attempts": attempts,
        "tool_calls": [
            {k: c[k] for k in ("i", "round", "name", "args", "ok", "code", "receipt", "result")} for c in calls
        ],
        "refusals": dict(refusals),
        "speech": speech_metrics(text, job.bot, scene.seat, scene.seat_name),
        "integrity": {"speech_verbatim": verbatim},
        "runtime": runtime_health([], rows, speeches=[]),
        "usage": usage_of(rows),
        "rounds": sum(1 for r in rows if r.get("event") == "llm_call"),
        "thoughts": _cap_list(thoughts, cfg.thought_chars),
        "monologues": _cap_list(monologues, cfg.thought_chars),
        "errors": errors,
        "mounts": [{k: m.get(k) for k in ("id", "layer", "chars", "sha")} for r in rows if r.get("event") == "mounts" for m in r.get("blocks") or []][:64],
        "hints": [{"tags": r.get("tags") or [], "entries": r.get("entries") or [], **({"kind": r["kind"]} if "kind" in r else {})} for r in rows if r.get("event") == "hint"],
        "prompt": {
            "system_sha": _sha(system), "system_chars": len(system), "first_sha": _sha(first), "first_chars": len(first),
            "tools": tool_names, "tools_sha": _sha(json.dumps(tool_names)),
        },
        "wake": [e.payload.get("event") for e in scene.wake],
        "scene": scene.fingerprint,
        "name": scene.seat_name,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def _cap_list(items: Sequence[str], limit: int) -> list[str]:
    """``items`` as they are, until their total is over ``limit`` characters (the last one is cut)."""
    out: list[str] = []
    used = 0
    for item in items:
        if used >= limit:
            break
        out.append(_cap(item, limit - used))
        used += len(item)
    return out


def _dump_conversation(directory: Path, job: Job, book: ConversationBook, seat: str, secrets: Sequence[str]) -> None:
    conv = book.get(seat)
    if conv is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{job.situation.id}__{job.seat}__{job.bot.label}__{job.sample}".replace(":", "-").replace("@", "+").replace("/", "-")
    parts = []
    for message in conv.request_messages():
        body = message.get("content")
        if message.get("tool_calls"):
            body = (body or "") + "\n" + json.dumps(message["tool_calls"], ensure_ascii=False)
        parts.append(f"## {message.get('role')}\n\n{scrub(body or '', secrets)}\n")
    (directory / f"{name}.md").write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------------- judge items
def thought_text(row: Mapping[str, Any]) -> str:
    return "\n\n".join(row.get("thoughts") or [])


def judge_items(row: Mapping[str, Any], situation: Situation, bots: Sequence[BenchBot]) -> list[dict[str, Any]]:
    """The items of ``row`` for the rubrics the situation asks for (``judge:``): everything a judge may see - the situation's brief and
    facts, what the bot did (its speech or choice, what it thought, its refused attempts) - and nothing else.  ``S2`` gets the style cards of
    every bot of the run and the truth (which bot spoke).  A row without a decision has no items."""
    decision = row.get("decision")
    if not decision or not situation.judge:
        return []
    if decision.get("text"):
        kind = "speech"
    elif decision["tool"] == "respond":
        kind = "choice"
    else:
        kind = decision["tool"]
    subject = {
        "kind": kind, "tool": decision["tool"], "text": decision.get("text"), "value": decision.get("value"), "reason": decision.get("reason"),
        "thought": _cap(thought_text(row), THOUGHT_CAP), "attempts": row.get("attempts") or [],
    }
    items = []
    for rubric, facts in situation.judge.items():
        context: dict[str, Any] = {**facts}
        if rubric == "S2":
            context["cards"] = {bot.label: bot.card for bot in bots}
            context["truth"] = {"bot": row["bot"]}
        items.append({
            "format": JUDGE_FORMAT, "version": ROW_VERSION, "item_id": f"{row['run']}/{row['situation']}/{row['seat']}/{row['bot']}/{row['sample']}/{rubric}",
            "rubric": rubric, "run": row["run"], "situation": row["situation"], "seat": row["seat"], "bot": row["bot"], "sample": row["sample"],
            "title": situation.title, "brief": situation.brief, "tags": list(situation.tags), "context": context, "subject": subject,
            "meta": dict(situation.meta),
        })
    return items


# ---------------------------------------------------------------------------------------------------------------------- the whole run
def jobs_for(situations: Sequence[Situation], bots: Sequence[BenchBot], samples: int) -> list[Job]:
    """Every sample of the run: situation (and each seat the situation asks for) × bot × sample number."""
    return [Job(situation, bot, seat, sample) for situation in situations for seat in situation.decision.seats for bot in bots for sample in range(samples)]


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


@dataclass
class RunReport:
    run_id: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0  # samples that were not started (the budget was used up, or they were done already)
    calls: int = 0
    elapsed_s: float = 0.0

    def by_status(self) -> dict[str, int]:
        return dict(Counter(row["status"] for row in self.rows))

    @property
    def scene_errors(self) -> int:
        return sum(1 for row in self.rows if row.get("stop") in ("scene", "bot"))


def _line(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class ResultWriter:
    """Appends rows and judge items to ``<out>/results.jsonl`` and ``<out>/judge_items.jsonl`` as they come (a run that dies keeps what it had)."""

    def __init__(self, out_dir: Path) -> None:
        self.dir = out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        self._results = (out_dir / "results.jsonl").open("a", encoding="utf-8", newline="\n")
        self._items = (out_dir / "judge_items.jsonl").open("a", encoding="utf-8", newline="\n")

    def add(self, row: Mapping[str, Any], items: Sequence[Mapping[str, Any]]) -> None:
        self._results.write(_line(row) + "\n")
        self._results.flush()
        for item in items:
            self._items.write(_line(item) + "\n")
        self._items.flush()

    def close(self) -> None:
        self._results.close()
        self._items.close()


def read_results(path: str | Path) -> list[dict[str, Any]]:
    """The rows of a ``results.jsonl`` (a damaged line is skipped); when a key (situation, seat, bot, sample) occurs more than once - a resumed
    run - the last row wins."""
    rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    target = Path(path)
    if not target.is_file():
        return []
    for line in target.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("format") == RESULT_FORMAT:
            rows[(row.get("situation"), row.get("seat"), row.get("bot"), row.get("sample"))] = row
    return list(rows.values())


async def run_suite(
    situations: Sequence[Situation],
    bots: Sequence[BenchBot],
    cfg: RunConfig,
    llm_factory: LlmFactory,
    *,
    out_dir: Path | None = None,
    budget: Budget | None = None,
    secrets: Sequence[str] = (),
    resume: bool = False,
    on_row: Callable[[dict[str, Any]], None] | None = None,
) -> RunReport:
    """Run every sample (situation × seat × bot × ``cfg.samples``), at most ``cfg.concurrency`` at a time, within ``budget``.  Rows (and judge
    items) are written to ``out_dir`` as they finish.  A sample the budget cannot pay for is not started (it is counted in ``skipped``; with
    ``resume`` the next run picks it up)."""
    run_id = cfg.run_id or new_run_id()
    cfg = RunConfig(**{**cfg.__dict__, "run_id": run_id})
    budget = budget or Budget(cfg.max_calls)
    jobs = jobs_for(situations, bots, cfg.samples)
    done: set[tuple[Any, ...]] = set()
    if resume and out_dir is not None:
        done = {
            (r["situation"], r["seat"], r["bot"], r["sample"]) for r in read_results(out_dir / "results.jsonl") if r.get("status") in ("ok", "no_decision")
        }
    report = RunReport(run_id=run_id)
    writer = ResultWriter(out_dir) if out_dir is not None else None
    by_id = {s.id: s for s in situations}
    semaphore = asyncio.Semaphore(max(1, cfg.concurrency))
    started = time.monotonic()

    async def one(job: Job) -> None:
        if job.key in done:
            report.skipped += 1
            return
        async with semaphore:
            if budget.exhausted:
                report.skipped += 1
                return
            row = await run_sample(job, cfg, llm_factory, budget, secrets=secrets, prompts_dir=(out_dir / "prompts") if out_dir is not None else None)
        items = judge_items(row, by_id[job.situation.id], bots)
        report.rows.append(row)
        report.items.extend(items)
        if writer is not None:
            writer.add(row, items)
        if on_row is not None:
            on_row(row)

    try:
        await asyncio.gather(*(one(job) for job in jobs))
    finally:
        if writer is not None:
            writer.close()
    report.calls = budget.calls
    report.elapsed_s = round(time.monotonic() - started, 2)
    return report


def run_info(
    report: RunReport, cfg: RunConfig, situations: Sequence[Situation], bots: Sequence[BenchBot], *, real: Mapping[str, Any] | None = None,
    max_calls: int | None = None,
) -> dict[str, Any]:
    """``run.json``: what was run, with what, and what it cost (no secrets, no texts of the bots' memory)."""
    usage = Counter()
    for row in report.rows:
        usage.update({k: v for k, v in (row.get("usage") or {}).items() if isinstance(v, int)})
    files = []
    for situation in situations:
        path = Path(situation.path) if situation.path else None
        files.append({"id": situation.id, "file": path.name if path else "", "sha": _sha(path.read_bytes().decode("utf-8", "replace")) if path and path.is_file() else ""})
    return {
        "format": RUN_FORMAT, "version": ROW_VERSION, "run": report.run_id, "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": cfg.mode, "model": cfg.model, "real": dict(real) if real else None,
        "config": {
            "samples": cfg.samples, "max_rounds": cfg.max_rounds, "timeout_s": cfg.timeout_s, "concurrency": cfg.concurrency,
            "max_calls": max_calls if max_calls is not None else cfg.max_calls, "context_window": cfg.context_window,
        },
        "situations": files, "bots": [bot.describe() for bot in bots],
        "rows": len(report.rows), "by_status": report.by_status(), "skipped": report.skipped, "model_calls": report.calls,
        "usage": dict(usage), "elapsed_s": report.elapsed_s,
    }


__all__ = [
    "JUDGE_FORMAT",
    "MOVE_TOOLS",
    "RESULT_FORMAT",
    "RUN_FORMAT",
    "Budget",
    "GuardedLlm",
    "Job",
    "LlmFactory",
    "NullSink",
    "ResultWriter",
    "RunConfig",
    "RunReport",
    "SampleTrace",
    "attempts_of",
    "build_row",
    "decision_of",
    "extract_calls",
    "judge_items",
    "jobs_for",
    "new_run_id",
    "read_results",
    "run_info",
    "run_sample",
    "run_suite",
    "score_of",
    "speech_metrics",
    "usage_of",
]
