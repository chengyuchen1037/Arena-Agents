"""Roster, budget, review and "complete this for me" of the orchestrator (v0.12.1).

Mixed into :class:`~app.game.orchestrator.GameOrchestrator`.  It owns everything that is about *who the
players are* and *what the user allows the game to cost*, and nothing about how a game is played:

* the roster (names, avatars, persona, memory) is a template kept in the data directory; a game takes a copy
  when it is created (names / avatars) and when it starts (persona / memory, so edits made while the game is
  still idle apply to it);
* the spending limit (E10/E22) is checked after every accounted LLM call and may pause the game;
* the post-game review (E21) and the "complete it with the moderator's model" button (E16) are one-shot
  model calls outside the runtime loop.  v0.13.1: the review *revises the impressions* the player has of the other
  seats (G39, G40: the memory file is overwritten, the model's answer is merged into it, the raw answer goes to the
  trace log) and "complete it" writes a persona in the fixed format (G43) or impressions and lessons (G42).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.agents.review import (
    RAW_OUTPUT_CHARS,
    build_refine_messages,
    build_review_messages,
    clean_generated_text,
)
from app.game import budget as budget_mod
from app.game.outcome import player_won, role_label
from app.storage.roster import (
    LIMITS,
    RosterError,
    RosterInputError,
    RosterStore,
    clean_name,
    default_name,
    get_roster_store,
    memory_body,
    merge_review,
    normalize_persona,
    parse_review_output,
    persona_for_prompt,
    seat_id,
)
from app.world.types import MODERATOR_ID, UiEvent

logger = logging.getLogger(__name__)

BUDGET_PUSH_INTERVAL_S = 1.0
# Reasoning models count their thinking against the limit: leave room for it (the text itself is a few hundred tokens).
REFINE_MAX_TOKENS = 6000
REVIEW_MAX_TOKENS = 6000
REVIEW_TIMEOUT_S = 180.0


class RosterRuntimeMixin:
    # Class-level defaults so partially initialized instances (tests) stay valid.
    roster_store: RosterStore | None = None  # injectable (tests); ``None`` = the data directory's store
    _budget_watch: budget_mod.BudgetWatch | None = None
    _budget_pushed_at: float = 0.0
    _review_task: asyncio.Task[Any] | None = None
    _review_running: set[str] | None = None
    _roster_io_suspended: bool = False
    _memory_enabled_snapshot: bool = True

    # ------------------------------------------------------------------------------ roster ---
    def _roster(self) -> RosterStore:
        if self.roster_store is not None:
            return self.roster_store
        return get_roster_store(len(self.cfg.get("participants", [])) or 12)

    def _ensure_avatars(self, roster: RosterStore) -> None:
        """Give every seat an avatar once (a no-op when they all have one or the generator is missing)."""
        try:
            from app import avatars
        except ImportError:  # the pixel-avatar library is optional: the front end falls back to a plain disc
            return
        try:
            roster.ensure_avatars(lambda seat, taken: avatars.make_description(seat, taken))
        except Exception:  # noqa: BLE001 - a missing parts library must not stop a game from being created
            logger.warning("Could not generate avatars", exc_info=True)

    def _roster_io_allowed(self) -> bool:
        """No roster file is read or created while the first game is built (the data directory is not locked
        yet); ``startup()`` loads the roster afterwards."""
        return not self._roster_io_suspended

    def _roster_snapshot(self) -> dict[str, Any]:
        """The roster as the next game takes it; falls back to the config's names when it cannot be read."""
        nothing: dict[str, Any] = {"seats": [], "moderator": {"id": MODERATOR_ID, "name": "主持人", "avatar": None}}
        if not self._roster_io_allowed():
            return {"memory_enabled": True, **nothing}
        try:
            roster = self._roster()
            self._ensure_avatars(roster)
            return roster.snapshot()
        except Exception:  # noqa: BLE001 - never block a new game on a roster problem
            logger.warning("Could not read the roster", exc_info=True)
            return {"memory_enabled": False, **nothing}

    def _roster_players(self) -> list[dict[str, Any]]:
        """The seats of a new game: ``config.yml`` decides how many and their ids / seats, the roster
        decides names, avatars, persona and memory."""
        snapshot = self._roster_snapshot()
        by_id = {row["id"]: row for row in snapshot["seats"]}
        players: list[dict[str, Any]] = []
        for index, item in enumerate(self.cfg.get("participants", []), start=1):
            pid = str(item["id"])
            row = by_id.get(pid) or {}
            players.append(
                {
                    "id": pid,
                    "seat": str(item.get("seat", index)),
                    "name": str(row.get("name") or item.get("name") or default_name(index)),
                    "persona": str(row.get("persona") or item.get("persona") or ""),
                    "memory": str(row.get("memory") or ""),
                    "avatar": row.get("avatar"),
                }
            )
        self._memory_enabled_snapshot = bool(snapshot.get("memory_enabled", True))
        moderator = snapshot.get("moderator") or {}
        self._moderator_avatar_snapshot = moderator.get("avatar") if isinstance(moderator.get("avatar"), dict) else None
        return players

    def _roster_for_write(self) -> RosterStore:
        """The roster for a *change*."""
        return self._roster()

    def roster_payload(self) -> dict[str, Any]:
        """``GET /api/roster``: the editable roster plus whether names / avatars may be changed now."""
        roster = self._roster()
        self._ensure_avatars(roster)
        data = roster.get()
        data["editable"] = self.session.status == "idle"  # type: ignore[attr-defined]
        return data

    def _roster_event(self) -> UiEvent:
        return UiEvent(
            type="roster.updated",
            data={
                "participants": [{"id": p.id, "name": p.name, "avatar": p.avatar} for p in self.session.participants],
                "moderator": {"id": MODERATOR_ID, "avatar": self.session.moderator_avatar},  # type: ignore[attr-defined]
            },
        )

    def _require_idle(self, what: str) -> None:
        if self.session.status != "idle":
            raise RuntimeError(f"{what}只能在对局开始之前修改；请先新建对局。")

    async def _rebuild_idle_world(self) -> None:
        """Rename / re-avatar in an idle game: rebuild its world with the *same seed* (so the identities that
        were dealt stay exactly as they are) and refresh the session's seats from the roster."""
        seed = self.world.state.rng.seed
        players = self._roster_players()
        world, clock = self._new_world(self.spec, seed=seed, players=players)  # type: ignore[attr-defined]
        self.world, self.clock = world, clock
        from app.game.session import Participant

        self.session.participants = [Participant(**p) for p in players]
        self.session.memory_enabled = bool(getattr(self, "_memory_enabled_snapshot", True))
        self.session.moderator_avatar = getattr(self, "_moderator_avatar_snapshot", None)
        self._reset_conversations()  # type: ignore[attr-defined]
        await self.broadcast_state("identities_shuffled")  # type: ignore[attr-defined]  # a fresh snapshot, no autosave churn
        self.bus.publish(self._roster_event())  # type: ignore[attr-defined]

    async def update_roster_entry(
        self,
        agent_id: str,
        *,
        name: Any = None,
        avatar: Any = None,
        persona: Any = None,
        memory: Any = None,
        memory_parts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Change one seat (or the moderator, whose only setting is its avatar).  Name / avatar need an idle
        game; persona / memory are files and may be edited at any time (a running game keeps the copy it
        started with).  ``memory`` is a whole memory text, ``memory_parts`` (G42) only the parts to change
        (``impressions`` / ``experience`` / ``legacy``): not both.

        All or nothing: everything the request asks for is checked first (an idle game for the name / avatar, the name,
        the avatar description, every text and its limit, whether it can be written at all), and nothing is written
        unless all of it is fine - a refused request leaves the roster files and the live game exactly as they were."""
        roster = self._roster_for_write()
        entry = roster.entry(agent_id)  # validates the id
        if agent_id == MODERATOR_ID and (name is not None or persona is not None or memory is not None or memory_parts is not None):
            raise RosterError("主持人只能更换头像，名字、性格设定和记忆都是固定的。")
        if memory is not None and memory_parts is not None:
            raise RosterInputError("memory 和 memory_parts 只能给一个。")
        # ---- 1. is it allowed now (409) ----------------------------------------------------------------------
        rename = name is not None and str(name).strip() != entry["name"]
        if rename:
            self._require_idle("名字")
        if avatar is not None:
            self._require_idle("头像")
        # ---- 2. is every part of it valid (400 / 422): nothing has been written yet --------------------------
        if rename:
            name = roster.validate_name(agent_id, name)
        if avatar is not None:
            try:
                from app import avatars

                avatar = avatars.validate_description(avatar)
            except ImportError:
                pass
            except FileNotFoundError:
                raise RosterError("头像素材库不可用，请先运行 tools/avatar_lab.py 生成。") from None
            except ValueError as exc:
                raise RosterError(str(exc)) from None
            if not isinstance(avatar, dict) or not avatar:
                raise RosterError("头像描述无效。")
        if agent_id != MODERATOR_ID:
            roster.validate_texts(agent_id, persona=persona, memory=memory, memory_parts=memory_parts)
        # ---- 3. write -----------------------------------------------------------------------------------------
        rebuild = False
        if rename:
            roster.set_name(agent_id, name)
            rebuild = True
        if avatar is not None:
            roster.set_avatar(agent_id, avatar)
            rebuild = True
        if persona is not None:
            roster.set_persona(agent_id, persona)
        if memory is not None:
            roster.set_memory(agent_id, memory)
        if memory_parts:  # (an empty object changes nothing: the file is not even rewritten)
            roster.set_memory_parts(
                agent_id,
                impressions=memory_parts.get("impressions"),
                experience=memory_parts.get("experience"),
                legacy=memory_parts.get("legacy"),
            )
        if rebuild:
            await self._rebuild_idle_world()
        elif (persona is not None or memory is not None or memory_parts) and self.session.status == "idle":
            self._refresh_idle_texts()
        return roster.entry(agent_id)

    def _reload_fresh_game_from_data_dir(self) -> None:
        """At startup the first (idle) game was built before the data directory was locked: take the roster and
        the budget default from it now.  Same dealt identities (same seed), nothing is saved."""
        if self.session.status != "idle":  # type: ignore[attr-defined]
            return
        seed = self.world.state.rng.seed  # type: ignore[attr-defined]
        players = self._roster_players()
        world, clock = self._new_world(self.spec, seed=seed, players=players)  # type: ignore[attr-defined]
        self.world, self.clock = world, clock  # type: ignore[attr-defined]
        from app.game.session import Participant

        session = self.session  # type: ignore[attr-defined]
        session.participants = [Participant(**p) for p in players]
        session.memory_enabled = bool(getattr(self, "_memory_enabled_snapshot", True))
        session.moderator_avatar = getattr(self, "_moderator_avatar_snapshot", None)
        session.budget = self._load_budget_defaults()
        self._reset_conversations()  # type: ignore[attr-defined]

    def _refresh_idle_texts(self) -> None:
        """An idle game has not started: persona / memory edits made now belong to it."""
        snapshot = self._roster_snapshot()
        by_id = {row["id"]: row for row in snapshot["seats"]}
        for participant in self.session.participants:
            row = by_id.get(participant.id)
            if row is not None:
                participant.persona, participant.memory = row["persona"], row["memory"]
        self.session.memory_enabled = bool(snapshot.get("memory_enabled", True))
        self._brain = None  # a game that has not started takes the new texts when it starts

    async def regenerate_avatars(self, agent_id: str | None = None) -> dict[str, Any]:
        """New random avatars (idle games only): one seat, the moderator (``"moderator"``) or - without an id -
        all twelve seats *and* the moderator, picked together so that no two look alike.

        Returns the entry of the one that changed, or ``{"seats": [...], "moderator": {...}}`` for all."""
        self._require_idle("头像")
        try:
            from app import avatars
        except ImportError as exc:
            raise RosterError("头像素材库不可用。") from exc
        roster = self._roster()
        data = roster.get()
        rows = data["seats"]
        try:
            if agent_id is None:
                picked = avatars.make_roster(len(rows) + 1)  # the last one is the moderator's
                updates = {r["id"]: d for r, d in zip(rows, picked)}
                updates[MODERATOR_ID] = picked[len(rows)]
                roster.set_all_avatars(updates)
            else:
                roster.entry(agent_id)  # validates the id
                taken = [r["avatar"] for r in rows if r["id"] != agent_id and r["avatar"]]
                if agent_id != MODERATOR_ID and data["moderator"]["avatar"]:
                    taken.append(data["moderator"]["avatar"])  # (for the moderator, ``taken`` is every player's)
                seat = 0 if agent_id == MODERATOR_ID else int(agent_id[1:])
                roster.set_avatar(agent_id, avatars.make_description(seat, taken))
        except FileNotFoundError:
            raise RosterError("头像素材库不可用，请先运行 tools/avatar_lab.py 生成。") from None
        await self._rebuild_idle_world()
        fresh = roster.get()
        if agent_id is None:
            return {"seats": fresh["seats"], "moderator": fresh["moderator"]}
        return roster.entry(agent_id)

    async def set_memory_enabled(self, enabled: bool) -> dict[str, Any]:
        self._roster_for_write().set_memory_enabled(bool(enabled))
        if self.session.status == "idle":
            self._refresh_idle_texts()
        return self.roster_payload()

    async def clear_memory(self, *, include_persona: bool = False) -> dict[str, Any]:
        self._roster_for_write().clear(include_persona=include_persona)
        if self.session.status == "idle":
            self._refresh_idle_texts()
        return self.roster_payload()

    # --------------------------------------------------------------------- one-shot model calls ---
    def _oneshot_port(self) -> Any:
        """The model port for calls outside the runtime loop: the demo bots when installed, else the engine
        client behind the same per-agent bindings the game uses.  (v0.13.1, G34: every call of it is a line of the
        error log - ``review`` for a player's own review, ``refine`` for the moderator's model completing a text.)"""
        from app.game.observe_runtime import oneshot_label
        from app.runtime.llm_gate import GatedEngineLlm, LlmGate, LoggedLlm

        call_log = self.call_log()  # type: ignore[attr-defined]
        if self.llm_port_factory is not None:  # type: ignore[attr-defined]
            return LoggedLlm(
                self.llm_port_factory(self),  # type: ignore[attr-defined]
                call_log,
                self.session.get_model_binding,  # type: ignore[attr-defined]
                oneshot_label,
            )
        rt = self.cfg.get("runtime") or {}  # type: ignore[attr-defined]
        return GatedEngineLlm(
            self.llm,  # type: ignore[attr-defined]
            self.session.get_model_binding,  # type: ignore[attr-defined]
            LlmGate(int(rt.get("llm_concurrency", 0)), int(rt.get("llm_concurrency_per_provider", 0))),
            float(rt.get("llm_timeout_seconds", 120)),
            call_log=call_log,
            label=oneshot_label,
        )

    def _record_oneshot(self, who: str, resp: Any) -> None:
        try:
            self.usage.record_llm(who, resp)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.warning("Could not account a one-shot call", exc_info=True)

    def _trace_review(self, agent_id: str, event: str, *, session_id: str | None = None, **fields: Any) -> None:
        """Put the raw answer of a review into the finished game's trace log (G40: what the model said stays readable even
        though the memory only keeps what was made of it): ``review`` (``mode`` ``json`` | ``lines``) or ``review_failed``.
        ``session_id`` is the game the review is about - the one it was started for, not the one that is live when the model
        answers (a new game may have been created meanwhile).  Best effort: the log must never break a review; like every
        trace line it is scrubbed of API keys.  (A draft made by "complete it" is shown to the user, so it is not logged: an
        idle game has no log directory to put it in.)"""
        try:
            from app.runtime.trace_log import TraceLog

            trace = TraceLog(self._logs_root(), session_id or self.session.session_id)  # type: ignore[attr-defined]
            self._register_trace_secrets(trace)  # type: ignore[attr-defined]
            try:
                trace.write(event, agent_id, **fields)
            finally:
                trace.close()
        except Exception:  # noqa: BLE001
            logger.debug("could not write the %s trace line of %s", event, agent_id, exc_info=True)

    async def refine_text(self, agent_id: str, kind: str, text: str) -> str:
        """The draft of :meth:`refine_entry` (a text; see there)."""
        return (await self.refine_entry(agent_id, kind, text))["draft"]

    async def refine_entry(self, agent_id: str, kind: str, text: str) -> dict[str, Any]:
        """Turn a few notes into a persona or a memory with the *moderator's* model.  The draft is returned, never
        saved: the user reads it first.

        ``persona`` -> ``{"draft": …}``: the fixed format of G43 (the answer is normalized: the headings of the
        template are recognised, everything else is dropped, every line is cut).  ``memory`` ->
        ``{"draft": …, "memory_parts": {"impressions", "experience"}}``: the notes are merged into the impressions and
        lessons the seat has now (G42); ``draft`` is those two sections as text, ``memory_parts`` the same structured."""
        roster = self._roster()
        if agent_id == MODERATOR_ID:
            raise RosterError("主持人没有性格设定和记忆。")
        entry = roster.entry(agent_id)
        if kind not in ("persona", "memory"):
            raise RosterError("kind 必须是 persona 或 memory。")
        source = str(text or "").strip()
        if not source:
            raise RosterError("请先写下一些要点，再让主持人模型完善。")
        if len(source) > 6000:
            raise RosterError("要点太长了（上限 6000 字）。")
        if self.session.get_model_binding(MODERATOR_ID) is None and not getattr(self, "demo_mode", False):  # type: ignore[attr-defined]
            raise RosterError("主持人还没有绑定模型，无法完善；请先在“模型绑定”里给主持人选一个模型。")
        ctx, parts = roster.load_memory(agent_id)
        if kind == "persona":
            messages = build_refine_messages("persona", source, name=entry["name"], limits=LIMITS)
        else:
            messages = build_refine_messages(
                "memory",
                source,
                name=entry["name"],
                others=[(sid, ctx.names.get(sid, "")) for sid in ctx.others],
                impressions=parts.impressions,
                experience=parts.experience,
                limits=LIMITS,
            )
        resp = await self._oneshot_port().complete(MODERATOR_ID, messages, None, max_tokens=REFINE_MAX_TOKENS)
        self._record_oneshot("roster", resp)
        raw = clean_generated_text(getattr(resp, "content", "") or "", limit=RAW_OUTPUT_CHARS)
        if not raw:
            raise RosterError("模型没有返回内容，请稍后重试。")
        if kind == "persona":
            draft = normalize_persona(raw)
            if not draft:
                logger.warning("Refine of the persona of %s: the answer has none of the template's headings", agent_id)
                raise RosterError("模型没有按固定格式返回性格指引（需要【定位】【说话风格】等标题），请稍后重试。")
            return {"draft": draft}
        output = parse_review_output(raw, ctx)
        if output is None:
            logger.warning("Refine of the memory of %s: the answer has neither impressions nor lessons", agent_id)
            raise RosterError("模型没有按要求返回记忆（需要 JSON：impressions 和 experience），请稍后重试。")
        merged = merge_review(parts, output, ctx, keep_legacy=True)
        return {
            "draft": memory_body(merged, ctx),
            "memory_parts": {"impressions": dict(merged.impressions), "experience": list(merged.experience)},
        }

    # ----------------------------------------------------------------------------- review ---
    def review_view(self) -> dict[str, Any]:
        finished = self.session.status == "finished"  # type: ignore[attr-defined]
        return {
            "available": finished,
            "done": list(self.session.review_done),  # type: ignore[attr-defined]
            "running": bool(self._review_running),
        }

    async def start_review(self, ids: list[str] | None = None) -> list[str]:
        """Write the players' reviews of the finished game (each player reviews with his own model).  Returns
        the seats that were started; progress arrives as ``review.progress`` events."""
        if self.session.status != "finished":  # type: ignore[attr-defined]
            raise RuntimeError("对局结束后才能复盘。")
        known = [p.id for p in self.session.participants]  # type: ignore[attr-defined]
        wanted = [i for i in (ids or known) if i in known]
        if ids and len(wanted) != len(set(ids)):
            raise RosterError("复盘的玩家 id 无效。")
        if self._review_running is None:
            self._review_running = set()
        todo = [i for i in dict.fromkeys(wanted) if i not in self.session.review_done and i not in self._review_running]  # type: ignore[attr-defined]
        if not todo:
            return []
        self._review_running.update(todo)
        # (the id of *this* game: a review takes a while and a new game may have been created by the time it is done)
        self._spawn(self._review_many(todo, self.session.session_id))  # type: ignore[attr-defined]
        await self.broadcast_state("review_updated")  # type: ignore[attr-defined]
        return todo

    async def _review_many(self, ids: list[str], session_id: str | None = None) -> None:
        """The reviews of ``ids``, at the same time.  Whatever happens, none of them stays "running": a port that cannot be
        built fails every one of them (an ``error`` each, they can be asked for again) instead of leaving the seats marked
        as busy for ever."""
        try:
            port = self._oneshot_port()
            await asyncio.gather(*(self._review_one(port, pid, session_id=session_id) for pid in ids))
        except Exception as exc:  # noqa: BLE001 - (``_review_one`` catches its own; this is the port, or the unexpected)
            logger.warning("The reviews of %s could not be run: %s", ids, type(exc).__name__, exc_info=True)
            for pid in ids:
                if pid in (self._review_running or ()):
                    self._progress(pid, "error", f"无法发起复盘（{type(exc).__name__}），可以重试。")
        finally:
            if self._review_running is not None:
                self._review_running.difference_update(ids)
        await self.broadcast_state("review_updated")  # type: ignore[attr-defined]

    def _progress(self, agent_id: str, state: str, message: str = "", changes: dict[str, Any] | None = None) -> None:
        """``review.progress``; ``done`` carries ``changes`` (what the review changed in the memory, contract 12.5)."""
        data: dict[str, Any] = {"id": agent_id, "state": state, "message": message}
        if state == "done":
            data["changes"] = changes if changes is not None else {"impressions": [], "experience": None}
        self.bus.publish(UiEvent(type="review.progress", data=data))  # type: ignore[attr-defined]

    async def _review_one(self, port: Any, agent_id: str, *, session_id: str | None = None) -> None:
        """One player's review (G39, G40): he gets his impressions, lessons and old notes together with what he saw, with
        his own model he revises them, and the answer is merged into the memory file (overwritten, never appended to).
        An answer that is neither the JSON object nor ``座位号：印象`` lines fails this player's review and changes nothing.

        The memory the model is shown is remembered (``base``): while it works the user may edit a seat's impression or
        clear the memory, and the answer is merged with what is there *then* (``RosterStore.apply_review``): what the model
        only gave back as it was shown changes nothing, a text the user changed meanwhile stays as he left it.  ``session_id``
        is the game that was reviewed (the raw answer belongs to its log, not to the game that is live when the model answers)."""
        self._progress(agent_id, "running")
        try:
            spec, world, session = self.spec, self.world, self.session  # type: ignore[attr-defined]
            session_id = session_id or session.session_id
            player = world.player(agent_id)
            faction = getattr(player, "faction", "")
            won = player_won(spec, faction, world.state.winner)
            outcome = {
                True: "你所在的阵营获胜了。",
                False: "你所在的阵营失败了。",
                None: f"这局的结果是：{world.state.winner or '未知'}。",
            }[won]
            label = role_label(spec, getattr(player, "role", ""))
            # (a game created by an older version may carry a persona longer than today's limit: the prompt gets it cut)
            persona = persona_for_prompt(next((p.persona for p in session.participants if p.id == agent_id), ""))
            roster = self._roster()
            ctx, parts = roster.load_memory(agent_id)
            messages = build_review_messages(
                world,
                agent_id,
                outcome_text=outcome,
                role_text=f"{label}（{faction or '无阵营'}）",
                persona=persona,
                impressions=parts.impressions,
                experience=parts.experience,
                legacy=parts.legacy,
                limits=LIMITS,
            )
            async with asyncio.timeout(REVIEW_TIMEOUT_S):
                resp = await port.complete(agent_id, messages, None, max_tokens=REVIEW_MAX_TOKENS)
            self._record_oneshot(agent_id, resp)
            text = clean_generated_text(getattr(resp, "content", "") or "", limit=RAW_OUTPUT_CHARS)
            if not text:
                raise RosterError("模型没有返回复盘内容。")
            output = parse_review_output(text, ctx)
            if output is None:
                self._trace_review(agent_id, "review_failed", session_id=session_id, text=text)
                logger.warning("Review of %s: the answer has no impressions: %r", agent_id, text[:300])
                raise RosterError("模型没有按要求返回印象（需要 JSON，或每行“座位号：印象”），这次复盘没有保存，可以重试。")
            self._trace_review(agent_id, "review", session_id=session_id, mode=output.mode, text=text)
            applied = roster.apply_review(
                agent_id, role=label, won=won, impressions=output.impressions, experience=output.experience, base=parts
            )
            if agent_id not in session.review_done:
                session.review_done.append(agent_id)
            self._progress(agent_id, "done", changes=applied.changes)
        except Exception as exc:  # noqa: BLE001 - one player's failure must not stop the others
            logger.warning("Review of %s failed: %s", agent_id, type(exc).__name__, exc_info=True)
            self._progress(agent_id, "error", str(exc) if isinstance(exc, (RosterError, TimeoutError)) else f"{type(exc).__name__}")
        finally:
            if self._review_running is not None:
                self._review_running.discard(agent_id)

    # ----------------------------------------------------------------------------- budget ---
    def budget_settings(self) -> budget_mod.BudgetSettings:
        return self.session.budget  # type: ignore[attr-defined]

    def budget_payload(self) -> dict[str, Any]:
        usage = self.usage  # type: ignore[attr-defined]
        return budget_mod.public_view(self.budget_settings(), usage.total_tokens, usage.total_cost_usd)

    def _load_budget_defaults(self) -> budget_mod.BudgetSettings:
        try:
            raw = self._settings().get_budget_defaults()  # type: ignore[attr-defined]
            return budget_mod.BudgetSettings.model_validate(raw or {})
        except Exception:  # noqa: BLE001 - a bad default must not block a new game
            logger.warning("Could not load the default budget", exc_info=True)
            return budget_mod.BudgetSettings()

    async def set_budget(self, values: dict[str, Any], *, save_default: bool = True) -> dict[str, Any]:
        """Replace the limit of this game (and remember it as the default of the next ones)."""
        settings = budget_mod.BudgetSettings.model_validate(values)
        self.session.budget = settings  # type: ignore[attr-defined]
        if save_default:
            try:
                self._settings().save_budget_defaults(settings.model_dump(mode="json"))  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - the limit itself applies even when the default could not be saved
                logger.warning("Could not save the default budget", exc_info=True)
        self._budget_watch = budget_mod.BudgetWatch()
        self._budget_pushed_at = 0.0
        self._on_budget_changed(force=True)
        await self.broadcast_state("budget_updated")  # type: ignore[attr-defined]
        return self.budget_payload()

    def _refuse_when_over_budget(self) -> None:
        """Starting a game that already used up a *stopping* limit would pause it again at once: say why."""
        settings = self.budget_settings()
        if not settings.force_stop:
            return
        usage = self.usage  # type: ignore[attr-defined]
        if budget_mod.state_of(settings, usage.total_tokens, usage.total_cost_usd) == budget_mod.REACHED:
            raise RuntimeError(
                "本局已达到你设置的预算上限，不能继续：请提高上限、清除上限，或取消“达到上限后强制停止”后再继续。"
            )

    def _attach_budget(self) -> None:
        """Hook the (new) usage book of the live game into the limit check."""
        self._budget_watch = budget_mod.BudgetWatch()
        self._budget_pushed_at = 0.0
        self.usage.on_record = self._on_usage_recorded  # type: ignore[attr-defined]

    def _on_usage_recorded(self, agent_id: str, resp: Any) -> None:
        self._on_budget_changed(force=False)
        supervisor = getattr(self, "_supervisor", None)
        if supervisor is None or agent_id not in supervisor.agents:
            # Nobody publishes this actor's numbers (the post-game review, "complete it with the moderator's
            # model", or any call while no game is running): tell the clients now, so that what they add up
            # from the events stays equal to the session's own totals.
            try:
                self.bus.publish(UiEvent(type="agent.metrics", data=self.usage.metrics_for(agent_id)))  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - the numbers reach the clients with the next snapshot anyway
                logger.debug("could not publish the metrics of %s", agent_id, exc_info=True)

    def _on_budget_changed(self, *, force: bool) -> None:
        if self._budget_watch is None:
            self._budget_watch = budget_mod.BudgetWatch()
        settings = self.budget_settings()
        usage = self.usage  # type: ignore[attr-defined]
        announced = self._budget_watch.update(settings, usage.total_tokens, usage.total_cost_usd)
        now = time.monotonic()
        payload = {"budget": self.budget_payload()}
        if announced is not None:
            self.bus.publish(UiEvent(type=f"budget.{'reached' if announced == budget_mod.REACHED else 'warning'}", data=payload))  # type: ignore[attr-defined]
            if announced == budget_mod.REACHED and settings.force_stop and self.session.status == "running":  # type: ignore[attr-defined]
                self._spawn(  # type: ignore[attr-defined]
                    self.pause("budget", hold_reason="已达到你设置的预算上限；提高上限或取消“强制停止”后可以继续。")  # type: ignore[attr-defined]
                )
        if force or announced is not None or now - self._budget_pushed_at >= BUDGET_PUSH_INTERVAL_S:
            self._budget_pushed_at = now
            self.bus.publish(UiEvent(type="budget.updated", data=payload))  # type: ignore[attr-defined]
