"""The jobs of the learning runs: who is being initialized or reviewed, the events they announce, the clock, the cost (v0.14, H19-H24, H31-H33;
contract §13.6 - §13.8).

:class:`LearningHub` is what the orchestrator keeps (one per process).  It does not run a model itself - that is
:func:`app.learning.loop.run_learning` inside :func:`~app.learning.init_runner.run_init` / :func:`~app.learning.review_runner.run_review` - it
decides *when* (the preconditions: a game that has not started or is over, no other job in the way, a model for every bot), starts one task per
bot - all at the same time, under the concurrency gate of the model port -, tells the clients how each is doing, and keeps the books:

* **initialization** (``start_init`` / ``cancel_init``): at most ONE job at a time; every bot of it has 120 seconds of wall clock and its own model
  (its default binding, by bot id); the sources (contract, scenario, materials) are read once when the job starts and every result is stamped
  with their hashes of that moment; ``bot.init.progress`` (at most once a second per bot while it runs, at once when its state changes);
  cancelling gives every bot back as it was - nothing was written, a run only ever writes by one atomic commit at its very end;
* **review** (``start_review``): only for a finished v6 game, each seat once per game (``review_done``), all seats at the same time, 60 seconds each;
  ``review.progress``; the seat's model; what the model costs is part of THAT game's usage and budget;
* **cost** (H24): the calls of an initialization are in the error log (``purpose = "init"``, actor = the bot) and in ``<data>/bots/usage.json``,
  never in a game's budget; the log of every job is ``<data>/logs/learning/<job_id>.jsonl`` (what was done and how long it took - no prompt, no key).

Nothing here knows a game: identities, factions and tags come from the scenario, the words from the prompts and the engine.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.bots.types import INIT_TIMEOUT_S, REVIEW_TIMEOUT_S
from app.learning.init_runner import InitOutcome, run_init
from app.learning.log import LearningLog, LearningUsage
from app.learning.review_runner import ReviewOutcome, run_review
from app.storage.bots import BotNotFound
from app.world.types import UiEvent

logger = logging.getLogger(__name__)

__all__ = [
    "LEARNING_MAX_TOKENS",
    "InitJob",
    "InitRun",
    "LearningHub",
    "LearningRefused",
    "ReviewJob",
    "ReviewRun",
]

#: What one answer of a learning run may take (thinking included: a reasoning model counts it against the limit).
LEARNING_MAX_TOKENS = 12_000
#: ``bot.init.progress`` / ``review.progress`` of a running bot are sent at this rate (state changes are sent at once).
PROGRESS_INTERVAL_S = 1.0


class LearningRefused(RuntimeError):
    """A request the learning jobs refuse.  ``status`` is the HTTP status (409 in the way / not ready, 404 an unknown bot, 422 an invalid id, 400 a request for nothing),
    ``code`` what the client acts on (``game_active``, ``init_busy``, ``no_binding``, ``NOT_READY``, ``game_not_finished``, ``review_unsupported``, ``nothing_to_do`` ...),
    ``problems`` the ``Problem`` objects of contract §13.7 when there are any.  The message is Chinese."""

    status = 409
    code = ""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None, problems: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.problems = problems or []


# ======================================================================================================================
# the state of the jobs
# ======================================================================================================================
@dataclass
class InitRun:
    """One bot of an initialization job: ``state`` is ``queued`` | ``running`` | ``done`` | ``failed`` | ``timeout`` | ``cancelled``."""

    bot_id: str
    name: str
    model: str
    state: str = "queued"
    started: float | None = None  # monotonic
    files_written: list[str] = field(default_factory=list)
    error: str | None = None
    rounds: int = 0
    outcome: InitOutcome | None = None
    task: asyncio.Task[Any] | None = None
    last_emit: float = -1e9

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")


@dataclass
class InitJob:
    job_id: str
    scenario_id: str
    started_at: int  # unix seconds
    deadline_s: int
    runs: dict[str, InitRun]
    spec: Any
    contract: Any
    materials: dict[str, str]
    hashes: Any
    llm: Any
    log: LearningLog
    slots: asyncio.Semaphore | None = None
    state: str = "running"  # running | done | cancelled
    ticker: asyncio.Task[Any] | None = None
    usage: LearningUsage | None = None

    @property
    def ids(self) -> list[str]:
        return list(self.runs)

    def active_ids(self) -> set[str]:
        return {bot_id for bot_id, run in self.runs.items() if run.active}


@dataclass
class ReviewRun:
    """One seat of a review job: ``state`` is ``queued`` | ``running`` | ``done`` | ``error`` | ``timeout``."""

    seat_id: str
    bot_id: str
    name: str
    state: str = "queued"
    started: float | None = None
    message: str = ""
    changes: dict[str, Any] | None = None
    task: asyncio.Task[Any] | None = None
    last_emit: float = -1e9

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")


@dataclass
class ReviewJob:
    job_id: str
    session: Any
    session_id: str
    scenario_id: str
    world: Any
    spec: Any
    contract: Any
    materials: dict[str, str]
    spans: list[Any]
    learn_style: dict[str, bool]
    llm: Any
    log: LearningLog
    runs: dict[str, ReviewRun]
    slots: asyncio.Semaphore | None = None
    ticker: asyncio.Task[Any] | None = None

    def active_ids(self) -> set[str]:
        return {seat for seat, run in self.runs.items() if run.active}


# ======================================================================================================================
# the hub
# ======================================================================================================================
class LearningHub:
    """The learning jobs of one orchestrator (``host``).  The host offers: ``session``, ``world``, ``spec``, ``bus``, ``usage``, ``cfg``,
    ``demo_mode``, the bot store (``_bots()``), its sources (``sources``), the table (``read_table()``), the model of a bot
    (``_bot_binding`` / ``_binding_view``), the model port of a run (``learning_llm()``), ``_contract_of_game()``, ``broadcast_state`` and
    ``publish_bots_updated`` - all of them the orchestrator's own (see ``app.game.bots_runtime`` / ``learning_runtime``)."""

    def __init__(
        self,
        host: Any,
        *,
        mono: Any = time.monotonic,
        wall: Any = time.time,
        tick_s: float = PROGRESS_INTERVAL_S,
    ) -> None:
        self.host = host
        self._mono = mono
        self._wall = wall
        self._tick_s = float(tick_s)
        self._init_job: InitJob | None = None
        self._review_jobs: list[ReviewJob] = []
        self._usage: LearningUsage | None = None
        self.last_init: dict[str, Any] | None = None  # how the last job ended (``{"job_id", "state", "bots": {id: state}}``)

    # ------------------------------------------------------------------------------------------------ what is running
    def init_running_ids(self) -> set[str]:
        job = self._init_job
        return job.active_ids() if job is not None else set()

    def review_running_ids(self) -> set[str]:
        running: set[str] = set()
        for job in self._review_jobs:
            running |= job.active_ids()
        return running

    def init_job_view(self) -> dict[str, Any] | None:
        """``BotsPayload.init_job`` (§13.7)."""
        job = self._init_job
        if job is None or job.state != "running":
            return None
        return {"job_id": job.job_id, "state": "running", "ids": job.ids, "started_at": job.started_at, "deadline_s": job.deadline_s}

    def review_seconds_left(self) -> int | None:
        """The longest time a running review has left (``PublicSession.review.seconds_left``), ``None`` when none runs."""
        left: list[int] = []
        for job in self._review_jobs:
            for run in job.runs.values():
                if run.state == "running" and run.started is not None:
                    left.append(self._seconds_left(run.started, REVIEW_TIMEOUT_S))
                elif run.state == "queued":
                    left.append(REVIEW_TIMEOUT_S)
        return max(left) if left else None

    # ------------------------------------------------------------------------------------------------ the cost of the initializations
    def _usage_store(self) -> LearningUsage:
        if self._usage is None:
            from app.storage.paths import get_data_dir

            self._usage = LearningUsage(Path(get_data_dir()) / "bots" / "usage.json")
        return self._usage

    def usage_view(self) -> dict[str, Any]:
        """``BotsPayload.learning_usage``: ``{"calls", "tokens", "cost_usd"}`` of the initializations so far (never raises)."""
        try:
            return self._usage_store().view()
        except Exception:  # noqa: BLE001 - the page must stay available
            logger.warning("Could not read the learning usage", exc_info=True)
            return {"calls": 0, "tokens": 0, "cost_usd": 0.0}

    def forget(self) -> None:
        """The data directory was replaced (an archive import): the totals are read from its file again."""
        self._usage = None

    def _logs_dir(self) -> Path:
        from app.storage.paths import get_data_dir

        return Path(get_data_dir()) / "logs" / "learning"

    def _new_log(self, job_id: str, bindings: list[Any]) -> LearningLog:
        """The log of a job; the keys of the providers it will use are registered with its scrubber (a key without a known shape is
        still never written)."""
        log = LearningLog(self._logs_dir(), job_id)
        try:
            store = self.host._providers()
            seen: set[str] = set()
            for binding in bindings:
                provider_id = getattr(binding, "provider_id", None)
                if binding is None or provider_id in seen:
                    continue
                seen.add(provider_id)
                try:
                    provider, _model = store.resolve(binding)
                except Exception:  # noqa: BLE001 - an unresolved binding has no key to protect
                    continue
                log.add_secret(str(getattr(provider, "api_key", "") or ""))
        except Exception:  # noqa: BLE001 - logging hygiene must never stop a job
            logger.warning("Could not register the provider keys with the learning log", exc_info=True)
        return log

    def _cap(self) -> asyncio.Semaphore | None:
        """How many bots may be at work at the same time (``runtime.learning_concurrency`` of ``config.yml``; 0 = all of them, the default:
        the real limit is the concurrency gate of the model port)."""
        try:
            limit = int(((self.host.cfg or {}).get("runtime") or {}).get("learning_concurrency", 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        return asyncio.Semaphore(limit) if limit > 0 else None

    def _max_tokens(self) -> int:
        try:
            value = int(((self.host.cfg or {}).get("runtime") or {}).get("learning_max_tokens", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else LEARNING_MAX_TOKENS

    def _seconds_left(self, started: float, deadline_s: float) -> int:
        return max(0, int(math.ceil(deadline_s - (self._mono() - started))))

    # ================================================================================================ initialization
    def _refuse_init_if_busy(self) -> None:
        session = self.host.session
        if session.status not in ("idle", "finished"):
            raise LearningRefused(
                "初始化只能在对局开始之前（或这局结束之后）进行；请先新建对局，或等这局结束。", code="game_active"
            )
        job = self._init_job
        if job is not None and job.state == "running":
            raise LearningRefused("已经有一个初始化任务在运行，请等它结束，或者先取消它。", code="init_busy")
        if self.review_running_ids():
            raise LearningRefused("赛后复盘正在进行（最多 %d 秒），请等它结束后再初始化。" % REVIEW_TIMEOUT_S, code="init_busy")

    async def start_init(self, ids: list[str] | None = None, rebuild: bool = False) -> dict[str, Any]:
        """Start the initialization of ``ids`` (default: every bot that is not ``ready`` - never initialized, out of date, failed; ``rebuild``:
        all of them).  Returns ``{"job_id", "ids", "deadline_s"}``; :class:`LearningRefused` says why it cannot be started (nothing was changed)."""
        host = self.host
        self._refuse_init_if_busy()
        table = host.read_table()
        names = {str(row["id"]): str(row["name"]) for row in table.summaries}
        if ids:
            unknown = [bot_id for bot_id in ids if bot_id not in names]
            if unknown:
                raise LearningRefused(f"没有这个玩家：{str(unknown[0])[:24]}。", status=404, code="not_found")
            chosen = list(dict.fromkeys(ids))
        elif rebuild:
            chosen = list(table.lineup)
        else:
            chosen = [str(row["id"]) for row in table.summaries if row["init"]["status"] != "ready"]
            if not chosen:
                raise LearningRefused(
                    "没有需要初始化的玩家：所有玩家都已经初始化好了。要重新来过，请选择“全部重新初始化”。", status=400, code="nothing_to_do"
                )  # (400, like the page's mock: this is not a conflict with anything, the request just asks for nothing)
        # ---- the sources the initialization is about: read ONCE, and every result is stamped with their hashes of this moment
        spec, contract, scenario_id, materials, hashes = self._read_sources()
        # ---- a model for every bot (its own default binding)
        cache: dict[tuple[str, str], dict[str, Any]] = {}
        models: dict[str, str] = {}
        bindings: list[Any] = []
        problems: list[dict[str, Any]] = []
        for bot_id in chosen:
            binding = host._bot_binding(bot_id)
            bindings.append(binding)
            view = host._binding_view(binding, cache)
            if not view["ok"]:
                problems.append(
                    host._problem(
                        "binding", "no_binding",
                        f"{names[bot_id]} 还没有可用的模型：{view['label']}。初始化用这个玩家自己的模型。", "bind_model",
                        bot_id=bot_id, seat=host._seat_of(bot_id), name=names[bot_id],
                    )
                )
            models[bot_id] = str(view["label"])
        if problems:
            who = "、".join(str(p["name"]) for p in problems[:4])
            more = f" 等 {len(problems)} 位" if len(problems) > 4 else ""
            raise LearningRefused(
                f"无法初始化：{who}{more}还没有可用的模型。请先在设置里为他们选择模型。", code="no_binding", problems=problems
            )
        job_id = uuid.uuid4().hex[:12]
        log = self._new_log(job_id, bindings)
        usage = self._usage_store()
        job = InitJob(
            job_id=job_id,
            scenario_id=scenario_id,
            started_at=int(self._wall()),
            deadline_s=INIT_TIMEOUT_S,
            runs={bot_id: InitRun(bot_id, names[bot_id], models[bot_id]) for bot_id in chosen},
            spec=spec,
            contract=contract,
            materials=materials,
            hashes=hashes,
            llm=host.learning_llm(),
            log=log,
            slots=self._cap(),
            usage=usage,
        )
        log.write("job", "-", kind="init", job_id=job_id, ids=job.ids, scenario_id=scenario_id, deadline_s=job.deadline_s)
        self._init_job = job
        for run in job.runs.values():
            run.task = asyncio.get_running_loop().create_task(self._run_init_bot(job, run), name=f"init-{run.bot_id}")
            run.task.add_done_callback(lambda _task, job=job: self._init_task_done(job))
        job.ticker = asyncio.get_running_loop().create_task(self._tick_init(job), name=f"init-ticker-{job_id}")
        for run in job.runs.values():
            self._emit_init(job, run, force=True)
        host.publish_bots_updated()
        await host.broadcast_state("learning_updated")
        return {"job_id": job_id, "ids": job.ids, "deadline_s": job.deadline_s}

    def _read_sources(self) -> tuple[Any, Any, str, dict[str, str], Any]:
        """``(spec, contract, scenario_id, materials, hashes)`` of the scenario in force and the official contract, or
        :class:`LearningRefused` (``NOT_READY`` with the problem) when one of them cannot be used."""
        from app.game.sources import SourceError

        host = self.host
        hub = host.sources
        try:
            scenario_id = hub.current_scenario_id()
            spec = hub.spec(scenario_id)
            contract = hub.contract()
        except SourceError as exc:
            fix = "import_contract" if exc.kind == "contract" else "import_rules"
            raise LearningRefused(str(exc), code="NOT_READY", problems=[host._problem(exc.kind, exc.code, str(exc), fix)]) from exc
        problem = hub.contract_problem(spec)
        if problem is not None:
            raise LearningRefused(
                str(problem), code="NOT_READY", problems=[host._problem("contract", problem.code, str(problem), "import_contract")]
            )
        return spec, contract, scenario_id, hub.materials(scenario_id), hub.hashes(scenario_id)

    async def _run_init_bot(self, job: InitJob, run: InitRun) -> None:
        host = self.host
        try:
            if job.slots is not None:
                async with job.slots:
                    await self._init_one(job, run)
            else:
                await self._init_one(job, run)
        except asyncio.CancelledError:
            run.state = "cancelled"
            job.log.write("result", run.bot_id, state="cancelled")
            self._emit_init(job, run, force=True)
            host.publish_bots_updated()
            raise
        except Exception as exc:  # noqa: BLE001 - a bug in one run must not leave the card spinning: it failed
            logger.warning("The initialization of %s failed unexpectedly", run.bot_id, exc_info=True)
            run.state = "failed"
            run.error = (
                "这个玩家已经不在了（被删除或换了编号），没有初始化。"
                if isinstance(exc, BotNotFound)
                else f"初始化出错（{type(exc).__name__}），原来的记忆没有改动，可以重试。"
            )
            try:
                host._bots().fail_init(run.bot_id, run.error, model=run.model, duration_s=0.0)
            except Exception:  # noqa: BLE001 - the bot is gone: nothing to mark
                logger.debug("could not mark %s as failed", run.bot_id, exc_info=True)
        job.log.write(
            "result", run.bot_id, state=run.state, error=run.error, files=run.files_written, rounds=run.rounds,
            duration_s=run.outcome.duration_s if run.outcome else None,
        )
        self._emit_init(job, run, force=True)
        host.publish_bots_updated()  # this card is final: the page shows it as it is now

    async def _init_one(self, job: InitJob, run: InitRun) -> None:
        host = self.host
        run.state = "running"
        run.started = self._mono()
        self._emit_init(job, run, force=True)

        def progress(info: dict[str, Any]) -> None:
            run.files_written = list(info.get("written") or [])
            run.rounds = int(info.get("rounds") or 0)
            self._emit_init(job, run)

        def account(resp: Any) -> None:
            try:
                job.usage.add(resp)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001 - accounting must never end a run
                logger.debug("could not account a learning call", exc_info=True)

        outcome = await run_init(
            store=host._bots(),
            bot_id=run.bot_id,
            scenario_id=job.scenario_id,
            spec=job.spec,
            contract=job.contract,
            materials=job.materials,
            sources=job.hashes,
            llm=job.llm,
            agent_id=run.bot_id,
            model=run.model,
            deadline_s=job.deadline_s,
            on_progress=progress,
            on_response=account,
            trace=job.log,
            max_tokens=self._max_tokens(),
            clock=self._mono,
        )
        run.outcome = outcome
        run.rounds = outcome.rounds
        run.error = outcome.error
        if outcome.state == "done":
            run.files_written = list(outcome.files_written)
        run.state = outcome.state
        try:
            job.usage.flush()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            logger.debug("could not write the learning usage", exc_info=True)

    def _emit_init(self, job: InitJob, run: InitRun, *, force: bool = False) -> None:
        """``bot.init.progress`` (§13.8); a running bot's is sent at most once a second unless ``force``."""
        now = self._mono()
        if not force and now - run.last_emit < self._tick_s:
            return
        run.last_emit = now
        left = self._seconds_left(run.started, job.deadline_s) if run.state == "running" and run.started is not None else None
        self._publish(
            "bot.init.progress",
            {
                "job_id": job.job_id,
                "bot_id": run.bot_id,
                "name": run.name,
                "state": run.state,
                "seconds_left": left,
                "deadline_s": job.deadline_s,
                "files_written": list(run.files_written),
                "error": run.error,
            },
        )

    async def _tick_init(self, job: InitJob) -> None:
        """Once a second every running bot says how much time it has left (the page counts down from it)."""
        try:
            while job.state == "running":
                await asyncio.sleep(self._tick_s)
                for run in job.runs.values():
                    if run.state == "running":
                        self._emit_init(job, run, force=True)
        except asyncio.CancelledError:
            return

    def _init_task_done(self, job: InitJob) -> None:
        if all(run.task is not None and run.task.done() for run in job.runs.values()):
            self._finish_init(job)

    def _finish_init(self, job: InitJob) -> None:
        """Every bot of the job is through (idempotent): the job is over, the clients are told once more, the totals are written."""
        if job.state != "running":
            return
        cancelled = any(run.state == "cancelled" for run in job.runs.values())
        job.state = "cancelled" if cancelled else "done"
        if job.ticker is not None:
            job.ticker.cancel()
        try:
            job.usage.flush()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            logger.debug("could not write the learning usage", exc_info=True)
        job.log.write("end", "-", job_id=job.job_id, state=job.state, bots={bot_id: run.state for bot_id, run in job.runs.items()})
        self.last_init = {"job_id": job.job_id, "state": job.state, "bots": {bot_id: run.state for bot_id, run in job.runs.items()}}
        if self._init_job is job:
            self._init_job = None
        self.host.publish_bots_updated()
        try:
            self.host._spawn(self.host.broadcast_state("learning_updated"))
        except RuntimeError:  # no running loop (the loop is closing): nobody is listening any more
            pass

    async def cancel_init(self) -> bool:
        """Stop the running initialization: every bot that has not finished goes back to what it was (its files were never touched), the
        ones that already finished stay finished.  ``False`` when there was none."""
        job = self._init_job
        if job is None or job.state != "running":
            return False
        tasks = [run.task for run in job.runs.values() if run.task is not None and not run.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._finish_init(job)
        return True

    # ================================================================================================ review
    async def start_review(self, ids: list[str] | None = None, learn_style: dict[str, bool] | None = None) -> dict[str, Any]:
        """Start the review of the seats ``ids`` (default: all) of the finished game that has not been reviewed yet.  Returns
        ``{"started": [seat ids], "deadline_s"}``; :class:`LearningRefused` when the game is not over, is a v5 game, or an initialization is running."""
        host = self.host
        session = host.session
        if session.legacy:
            raise LearningRefused(
                "这局是旧版本（v0.13）的对局，它的玩家没有对应的 bot，不能复盘。", code="review_unsupported"
            )
        if session.status != "finished":
            raise LearningRefused("对局结束后才能复盘。", code="game_not_finished")
        if self.init_running_ids():
            raise LearningRefused("初始化正在进行，请等它结束后再复盘。", code="init_busy")
        known = {p.id: p for p in session.participants}
        if ids:
            if any(seat not in known for seat in ids):
                raise LearningRefused("复盘的玩家 id 无效。", status=422, code="invalid")
            wanted = list(dict.fromkeys(ids))
        else:
            wanted = list(known)
        running = self.review_running_ids()
        todo = [seat for seat in wanted if seat not in session.review_done and seat not in running]
        if not todo:
            return {"started": [], "deadline_s": REVIEW_TIMEOUT_S}
        spec = host.spec
        scenario_id = str(spec.id)
        contract = host._contract_of_game()
        job_id = uuid.uuid4().hex[:12]
        bindings = [session.get_model_binding(seat) for seat in todo]
        log = self._new_log(job_id, bindings)
        job = ReviewJob(
            job_id=job_id,
            session=session,
            session_id=session.session_id,
            scenario_id=scenario_id,
            world=host.world,
            spec=spec,
            contract=contract,
            materials=host.sources.materials(scenario_id),
            spans=[span.model_copy() for span in session.human_spans],
            learn_style={str(k): bool(v) for k, v in (learn_style or {}).items()},
            llm=host.learning_llm(),
            log=log,
            runs={seat: ReviewRun(seat, known[seat].bot_id, known[seat].name) for seat in todo},
            slots=self._cap(),
        )
        log.write("job", "-", kind="review", job_id=job_id, game=session.session_id, seats=todo, deadline_s=REVIEW_TIMEOUT_S)
        self._review_jobs.append(job)
        loop = asyncio.get_running_loop()
        for run in job.runs.values():
            run.task = loop.create_task(self._run_review_seat(job, run), name=f"review-{run.seat_id}")
            run.task.add_done_callback(lambda _task, job=job: self._review_task_done(job))
        job.ticker = loop.create_task(self._tick_review(job), name=f"review-ticker-{job_id}")
        for run in job.runs.values():
            self._emit_review(job, run, force=True)
        await host.broadcast_state("review_updated")
        return {"started": todo, "deadline_s": REVIEW_TIMEOUT_S}

    async def _run_review_seat(self, job: ReviewJob, run: ReviewRun) -> None:
        host = self.host
        try:
            if job.slots is not None:
                async with job.slots:
                    await self._review_one(job, run)
            else:
                await self._review_one(job, run)
        except asyncio.CancelledError:
            run.state = "error"
            run.message = "复盘被中断了，可以重试。"
            job.log.write("result", run.seat_id, state=run.state, message=run.message)
            self._emit_review(job, run, force=True)
            raise
        except Exception as exc:  # noqa: BLE001 - one seat's failure must not stop the others
            logger.warning("The review of %s failed unexpectedly", run.seat_id, exc_info=True)
            run.state = "error"
            run.message = (
                "这个座位上的玩家（bot）已经不在了（被删除，或改名后换了编号），无法复盘。"
                if isinstance(exc, BotNotFound)
                else f"复盘出错（{type(exc).__name__}），这次没有记入，可以重试。"
            )
        job.log.write("result", run.seat_id, state=run.state, message=run.message)
        self._emit_review(job, run, force=True)
        host.publish_bots_updated()

    async def _review_one(self, job: ReviewJob, run: ReviewRun) -> None:
        host = self.host
        run.state = "running"
        run.started = self._mono()
        self._emit_review(job, run, force=True)
        binding = job.session.get_model_binding(run.seat_id)
        if not getattr(host, "demo_mode", False) and not host._binding_view(binding, {})["ok"]:
            run.state = "error"
            run.message = "这个座位没有可用的模型，无法复盘；请先为它选择模型，再重试。"
            return

        def progress(_info: dict[str, Any]) -> None:
            self._emit_review(job, run)

        def account(resp: Any) -> None:
            host._record_oneshot(run.seat_id, resp)  # (what a review costs is part of the game that was reviewed)

        outcome: ReviewOutcome = await run_review(
            store=host._bots(),
            bot_id=run.bot_id,
            seat_id=run.seat_id,
            session_id=job.session_id,
            scenario_id=job.scenario_id,
            world=job.world,
            spec=job.spec,
            contract=job.contract,
            materials=job.materials,
            spans=job.spans,
            learn_style=bool(job.learn_style.get(run.seat_id)),
            llm=job.llm,
            agent_id=run.seat_id,
            deadline_s=REVIEW_TIMEOUT_S,
            on_progress=progress,
            on_response=account,
            trace=job.log,
            max_tokens=self._max_tokens(),
            clock=self._mono,
        )
        run.state = outcome.state
        run.message = outcome.message
        run.changes = outcome.changes if outcome.state in ("done", "timeout") else None
        if outcome.state in ("done", "timeout") and run.seat_id not in job.session.review_done:
            job.session.review_done.append(run.seat_id)  # (the game of THIS review, whichever is live by now)

    def _emit_review(self, job: ReviewJob, run: ReviewRun, *, force: bool = False) -> None:
        """``review.progress`` (§13.8); a running seat's is sent at most once a second unless ``force``."""
        now = self._mono()
        if not force and now - run.last_emit < self._tick_s:
            return
        run.last_emit = now
        left = self._seconds_left(run.started, REVIEW_TIMEOUT_S) if run.state == "running" and run.started is not None else None
        self._publish(
            "review.progress",
            {
                "id": run.seat_id,
                "bot_id": run.bot_id,
                "name": run.name,
                "state": run.state,
                "message": run.message,
                "seconds_left": left,
                "deadline_s": REVIEW_TIMEOUT_S,
                "changes": run.changes if run.state in ("done", "timeout") else None,
            },
        )

    async def _tick_review(self, job: ReviewJob) -> None:
        try:
            while job.active_ids():
                await asyncio.sleep(self._tick_s)
                for run in job.runs.values():
                    if run.state == "running":
                        self._emit_review(job, run, force=True)
        except asyncio.CancelledError:
            return

    def _review_task_done(self, job: ReviewJob) -> None:
        if not all(run.task is not None and run.task.done() for run in job.runs.values()):
            return
        if job not in self._review_jobs:
            return
        self._review_jobs.remove(job)
        if job.ticker is not None:
            job.ticker.cancel()
        job.log.write("end", "-", job_id=job.job_id, seats={seat: run.state for seat, run in job.runs.items()})
        self.host.publish_bots_updated()
        try:
            self.host._spawn(self.host.broadcast_state("review_updated"))  # (the save keeps ``review_done``)
        except RuntimeError:
            pass

    # ================================================================================================ the rest
    def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            self.host.bus.publish(UiEvent(type=event_type, data=data))
        except Exception:  # noqa: BLE001 - the page is told again by the next event; a run never fails for it
            logger.debug("could not publish %s", event_type, exc_info=True)

    async def shutdown(self) -> None:
        """Stop everything that runs (the application is closing): runs are cancelled - nothing of an initialization was written, what a review
        wrote stays - and awaited."""
        tasks: list[asyncio.Task[Any]] = []
        job = self._init_job
        if job is not None:
            tasks += [run.task for run in job.runs.values() if run.task is not None]
            if job.ticker is not None:
                tasks.append(job.ticker)
        for review in self._review_jobs:
            tasks += [run.task for run in review.runs.values() if run.task is not None]
            if review.ticker is not None:
                tasks.append(review.ticker)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for finished in list(self._review_jobs):
            self._review_jobs.remove(finished)
        if job is not None and job.state == "running":
            job.state = "cancelled"
        self._init_job = None
        if self._usage is not None:
            try:
                self._usage.flush()
            except Exception:  # noqa: BLE001
                logger.debug("could not write the learning usage", exc_info=True)
