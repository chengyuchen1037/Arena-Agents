"""Playing a seat: the person at the keyboard instead of an agent (v0.13, docs/v0.13-plan.md F2-F12).

Mixed into :class:`~app.game.orchestrator.GameOrchestrator`.  A person can take over ONE player seat while the
game has not started or is paused.  For as long as they play it

* the seat has no agent: ``_build_runtime`` leaves it out, so nothing calls a model for it and the envelopes the
  world addresses to it are dropped by the supervisor (the person reads the world through ``takeover.updated``);
* every action of the person goes through the very same commit gate as an agent's tool call
  (``world.submit(Intent)``): turns, character and message limits, deadlines and ``STALE`` drafts are the engine's
  rules, not this module's;
* the actor is always the seat that is taken over - the client cannot name another one;
* the seat's *state* (what it may do in every channel, its open requests, its usable abilities, what waits for
  it) is computed by :func:`app.world.seat.seat_status` - the same function that is locked to ``speech_gate`` -
  and pushed as ``takeover.updated`` after every change of the world (at most once per loop turn, and only when
  something changed); it is part of ``PublicSession.takeover`` as well.  Nothing here is saved by those pushes.

Nobody is told (F6): the moderator and the other players cannot tell a person from an agent, and the agent of
the seat is not told afterwards either - when the seat is given back its conversation starts again from the full
board and history, in which the person's words stand as its own.

The *control lock* of the orchestrator (``_control()``) serialises start / resume, the re-deal and taking over /
giving back a seat, so a re-deal can never overtake a start (F5).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from app.game.session import MODERATOR_ID, GameSession, HumanSpan
from app.world.seat import plain_names, seat_status
from app.world.types import Intent, Receipt, UiEvent

logger = logging.getLogger(__name__)

#: UI events after which the seat state is not recomputed: they cannot change what the person may do.
SEAT_QUIET_EVENTS = frozenset(
    {"takeover.updated", "agent.metrics", "budget.updated", "budget.warning", "budget.reached", "llm.call"}
)


class TakeoverError(RuntimeError):
    """The game refuses a takeover, a seat action or a re-deal; ``status`` is the HTTP status the API answers with."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def _without_clock(value: Any) -> Any:
    """``value`` without the ``seconds_left`` fields: they change with every look at the clock."""
    if isinstance(value, dict):
        return {k: _without_clock(v) for k, v in value.items() if k != "seconds_left"}
    if isinstance(value, list):
        return [_without_clock(v) for v in value]
    return value


class TakeoverRuntimeMixin:
    # Class-level defaults keep partially initialized instances (tests) valid.
    _control_lock_obj: asyncio.Lock | None = None
    _takeover_push_scheduled: bool = False
    _takeover_signature: str = ""

    # ------------------------------------------------------------------ the control lock ---
    def _control(self) -> asyncio.Lock:
        """The lock that serialises start / resume, the re-deal and taking a seat over / giving it back."""
        if self._control_lock_obj is None:
            self._control_lock_obj = asyncio.Lock()
        return self._control_lock_obj

    # ------------------------------------------------------------------------ the state ---
    @property
    def takeover_seat(self) -> str | None:
        """The seat a person plays right now (``None``: nobody)."""
        seat = self.session.takeover.seat  # type: ignore[attr-defined]
        return str(seat) if seat else None

    def game_has_started(self) -> bool:
        """The game has been started at least once (F5): it has a ``started_at``, or the ledger recorded a
        ``running`` status.  A re-deal is never allowed after that."""
        if self.session.started_at:  # type: ignore[attr-defined]
            return True
        return any(
            e.type == "game.status" and (e.data or {}).get("status") == "running"
            for e in self.world.ledger  # type: ignore[attr-defined]
        )

    def reshuffle_available(self) -> bool:
        """The identities may still be dealt again: the game is idle and has never been started."""
        return self.session.status == "idle" and not self.game_has_started()  # type: ignore[attr-defined]

    def takeover_payload(self) -> dict[str, Any]:
        """``PublicSession.takeover`` (contract §11.7): ``{"seat", "reshuffle_available"}`` and, while a seat is
        played, the whole state of that seat (``seq``, ``channels``, ``requests``, ``abilities``, ``attention``)."""
        seat = self.takeover_seat
        payload: dict[str, Any] = {"seat": seat, "reshuffle_available": self.reshuffle_available()}
        if seat is None:
            return payload
        try:
            state = seat_status(self.world, seat)  # type: ignore[attr-defined]
        except KeyError:  # a seat the world does not have (a damaged save): nothing to show
            logger.warning("The seat taken over (%s) does not exist in the world", seat)
            return {"seat": None, "reshuffle_available": payload["reshuffle_available"]}
        except Exception:  # noqa: BLE001 - this document is part of every snapshot: it must never break /api/state
            # Fail closed: the seat is played, but nothing is offered (the page shows "禁言中" everywhere).
            logger.exception("Could not compute the state of the seat %s", seat)
            return {
                **payload,
                "seq": int(self.world.seq),  # type: ignore[attr-defined]
                "channels": {},
                "requests": [],
                "abilities": [],
                "attention": [],
            }
        state.pop("seat", None)
        payload.update(state)
        return payload

    def _agent_state_of(self, agent_id: str) -> str:
        """The state shown on the card of ``agent_id``: ``human`` for the seat a person plays."""
        if agent_id == self.takeover_seat:
            return "human"
        return self.agent_state.get(agent_id, "sleeping")  # type: ignore[attr-defined]

    def _sleeping_states(self) -> dict[str, str]:
        """``agent_state`` of a stopped game: everybody sleeps, except the seat a person plays."""
        seat = self.takeover_seat
        states = {k: "sleeping" for k in self.agent_state}  # type: ignore[attr-defined]
        if seat is not None:
            states[seat] = "human"
        return states

    def _sync_untimed_holders(self, world: Any = None, *, publish: bool = False) -> None:
        """Tell the world which seat a person plays: its floor in a public channel has no deadline (v0.13.1, G10 / G14).

        Called whenever the played seat changes or a world / session becomes the live one (a new game, a restored or
        imported one, a re-deal).  A floor the seat holds at that moment is re-judged by the world.  With ``publish``
        (taking over / giving back, when somebody is looking) the ``floor.changed`` events of that are published."""
        world = world if world is not None else getattr(self, "world", None)
        session = getattr(self, "session", None)
        if world is None:
            return
        seat = session.takeover.seat if session is not None else None
        world.set_untimed_holders([str(seat)] if seat else [])
        if publish:
            self._flush_world()  # type: ignore[attr-defined]

    # ----------------------------------------------------------------------- the push ---
    def _after_publish(self, event: UiEvent) -> None:
        """Called by the orchestrator after every UI event it published."""
        if event.type not in SEAT_QUIET_EVENTS:
            self._schedule_takeover_push()

    def _schedule_takeover_push(self) -> None:
        """Recompute and publish the seat state once, after everything this loop turn has queued."""
        if self.takeover_seat is None or self._takeover_push_scheduled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # not on an event loop (a plain unit test): the caller pushes explicitly
        self._takeover_push_scheduled = True
        loop.call_soon(self._push_takeover)

    def _push_takeover(self, *, force: bool = False) -> None:
        """Publish ``takeover.updated`` when the seat state changed (or ``force``)."""
        self._takeover_push_scheduled = False
        try:
            payload = self.takeover_payload()
            if not force and payload["seat"] is None:
                return  # the seat was given back (or the game replaced) since this push was scheduled
            signature = json.dumps(_without_clock(payload), sort_keys=True, ensure_ascii=False)
            if not force and signature == self._takeover_signature:
                return
            self._takeover_signature = signature
            self.bus.publish(UiEvent(type="takeover.updated", data={"takeover": payload}))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - a broken seat state must never break the game loop
            logger.exception("Could not publish the seat state")

    # ------------------------------------------------------------- takeover / release ---
    def _close_human_spans(self, seat: str) -> None:
        """Close every open stretch the person played ``seat`` at the world's current ``seq`` (F22)."""
        seq = int(self.world.seq)  # type: ignore[attr-defined]
        for span in self.session.human_spans:  # type: ignore[attr-defined]
            if span.seat == seat and span.to_seq is None:
                span.to_seq = seq

    async def takeover(self, seat: str) -> GameSession:
        """Play ``seat`` yourself (F2).

        Only a player seat; only one at a time; only while the game has not started or is paused.  The seat's
        agent is not created any more (``_build_runtime``), its state becomes ``human``, a ``HumanSpan`` starts at
        the world's current ``seq`` and the takeover is part of the game's save.  Taking over the seat that is
        played already does nothing.  Raises :class:`TakeoverError` (with the HTTP status) otherwise.
        """
        async with self._control():
            session = self.session  # type: ignore[attr-defined]
            seat = str(seat or "").strip()
            if seat == MODERATOR_ID:
                raise TakeoverError("只能接管玩家座位，不能接管主持人。", 422)
            if self.world.player(seat) is None:  # type: ignore[attr-defined]
                raise TakeoverError("这个座位不存在。", 404)
            if session.takeover.seat == seat:
                return session
            if session.takeover.seat is not None:
                raise TakeoverError("请先退出当前接管，再接管别的座位。")
            if self._is_active() or session.status == "running":  # type: ignore[attr-defined]
                raise TakeoverError("对局进行中不能接管：请先暂停对局。")
            if session.status == "finished":
                raise TakeoverError("对局已结束，不能接管；可以先点「继续」再暂停。")
            self._close_human_spans(seat)  # (a stale open stretch of a damaged save)
            session.takeover.seat = seat
            session.human_spans.append(HumanSpan(seat=seat, from_seq=int(self.world.seq)))  # type: ignore[attr-defined]
            self.agent_state[seat] = "human"  # type: ignore[attr-defined]
            self._sync_untimed_holders(publish=True)  # (v0.13.1) its public floors lose their deadline, now and later
            self.schedule_autosave("takeover")  # type: ignore[attr-defined]
            self._push_takeover(force=True)
            logger.info("Seat %s is played by a person", seat)
            return session

    async def release_takeover(self) -> GameSession:
        """Give the seat back to its agent (F4).  Refused while the game runs; allowed when it has not started,
        is paused or is finished.  The seat's conversation is forgotten, so its agent starts again from the full
        board and history the next time it is woken - the person's words stand in it as its own - and it is not
        told that somebody else played (F6).  Giving back when nobody plays does nothing."""
        async with self._control():
            session = self.session  # type: ignore[attr-defined]
            seat = session.takeover.seat
            if seat is None:
                return session
            if self._is_active() or session.status == "running":  # type: ignore[attr-defined]
                raise TakeoverError("对局进行中不能退出接管：请先暂停对局。")
            self._close_human_spans(seat)
            session.takeover.seat = None
            self.agent_state[seat] = "sleeping"  # type: ignore[attr-defined]
            self._book.reset(seat)  # type: ignore[attr-defined]
            brain = self._brain  # type: ignore[attr-defined]
            if brain is not None:
                brain.reset_conversation(seat)
            self._sync_untimed_holders(publish=True)  # (v0.13.1) a floor it holds is timed again, with the time it had left
            self.schedule_autosave("takeover")  # type: ignore[attr-defined]
            self._push_takeover(force=True)
            logger.info("Seat %s is given back to its agent", seat)
            return session

    def _sanitize_takeover(self, loaded: GameSession) -> None:
        """Called for every game that becomes the live one (a restart, "continue" in the history, an import).

        The takeover itself is kept as saved - the game comes back paused and the seat is still played (F10) - but a
        save that contradicts itself (a damaged or hand-edited file) is made consistent: a seat the game does not
        have is played by nobody, an open stretch of a seat nobody plays is closed where it began, and a played seat
        always has its open stretch.  Whatever the previous game had pushed to the clients is forgotten."""
        self._takeover_signature = ""
        seat = loaded.takeover.seat
        if seat is not None and seat not in {p.id for p in loaded.participants}:
            logger.warning("A saved game names the unknown seat %r as taken over; ignoring it", seat)
            loaded.takeover.seat = None
            seat = None
        for span in loaded.human_spans:
            if span.to_seq is None and span.seat != seat:
                span.to_seq = span.from_seq  # an open stretch of a seat nobody plays: close it where it began
        if seat is not None and not any(s.seat == seat and s.to_seq is None for s in loaded.human_spans):
            ledger = (loaded.world or {}).get("ledger") or []
            last = ledger[-1].get("seq", 0) if ledger and isinstance(ledger[-1], dict) else 0
            loaded.human_spans.append(HumanSpan(seat=seat, from_seq=int(last or 0)))

    # ----------------------------------------------------------------------- re-deal ---
    def _guard_reshuffle(self) -> None:
        """The identities are only dealt again before the game has ever started (F5): anything else is a 409."""
        if self.session.status != "idle" or self.game_has_started():  # type: ignore[attr-defined]
            raise TakeoverError("对局已经开始，不能再重新发牌。")

    # ------------------------------------------------------------------ seat actions ---
    def _seat_actor(self) -> str:
        """The seat the person plays, when the game is running (an action in any other state is refused)."""
        seat = self.takeover_seat
        if seat is None:
            raise TakeoverError("请先接管一个座位。")
        status = self.session.status  # type: ignore[attr-defined]
        if status != "running" or not self.world.running:  # type: ignore[attr-defined]
            why = {
                "idle": "对局还没有开始，不能操作：请先点「开始」。",
                "finished": "对局已结束，不能操作。",
            }.get(status, "对局已暂停，不能操作：请先点「继续」。")
            raise TakeoverError(why)
        return seat

    def _seat_receipt(self, receipt: Receipt) -> dict[str, Any]:
        """The slim JSON of a receipt the seat endpoints answer with (a refusal is an answer, not an error)."""
        out: dict[str, Any] = {
            "accepted": bool(receipt.ok),
            "seq": receipt.seq,
            "code": receipt.code,
            "message": plain_names(self.world, receipt.message),  # type: ignore[attr-defined]
        }
        if receipt.truncated:
            out["truncated"] = dict(receipt.truncated)
        if receipt.data:
            out["data"] = dict(receipt.data)
        return out

    def _submit_seat(self, type_: str, payload: dict[str, Any], *, based_on_seq: int | None = None) -> dict[str, Any]:
        """One action of the person: an ``Intent`` of the played seat through the world's commit gate."""
        seat = self._seat_actor()
        world = self.world  # type: ignore[attr-defined]
        seen = world.seq if based_on_seq is None else max(0, min(int(based_on_seq), int(world.seq)))
        intent = Intent(
            intent_id=uuid.uuid4().hex[:10], actor_id=seat, based_on_seq=seen, type=type_, payload=payload
        )
        receipt = world.submit(intent)
        self._schedule_takeover_push()
        return self._seat_receipt(receipt)

    def seat_speak(self, channel_id: str, content: str, based_on_seq: int | None = None) -> dict[str, Any]:
        """``speak`` in ``channel_id``.  ``based_on_seq`` is what the person had seen when the draft began (omitted:
        what is current): a draft written across an invalidating entry is ``STALE`` (F11)."""
        return self._submit_seat("speak", {"channel_id": channel_id, "content": content}, based_on_seq=based_on_seq)

    def seat_pass(self, channel_id: str) -> dict[str, Any]:
        """End the seat's turn in ``channel_id`` (``pass``)."""
        return self._submit_seat("pass", {"channel_id": channel_id})

    def seat_respond(self, request_id: str, value: str, reason: str = "") -> dict[str, Any]:
        """Answer (or change the answer to) an open request."""
        return self._submit_seat("respond", {"request_id": request_id, "value": value, "reason": reason or ""})

    def seat_ability(self, ability_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Use an ability the moderator has enabled for the seat; ``params`` follow the scenario's definition."""
        return self._submit_seat("use_ability", {"ability_id": ability_id, "params": dict(params or {})})
