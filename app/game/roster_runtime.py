"""Roster, budget, review and "complete this for me" of the orchestrator (v0.12.1).

Mixed into :class:`~app.game.orchestrator.GameOrchestrator`.  It owns everything that is about *who the
players are* and *what the user allows the game to cost*, and nothing about how a game is played:

* the roster (names, avatars, persona, memory) is a template kept in the workspace; a game takes a copy
  when it is created (names / avatars) and when it starts (persona / memory, so edits made while the game is
  still idle apply to it);
* the spending limit (E10/E22) is checked after every accounted LLM call and may pause the game;
* the post-game review (E21) and the "complete it with the moderator's model" button (E16) are one-shot
  model calls outside the runtime loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.agents.review import (
    REFINE_MAX_CHARS,
    REVIEW_MAX_CHARS,
    build_refine_messages,
    build_review_messages,
    clean_generated_text,
)
from app.game import budget as budget_mod
from app.game.outcome import player_won, role_label
from app.storage.roster import (
    RosterError,
    RosterStore,
    clean_name,
    default_name,
    get_roster_store,
    seat_id,
)
from app.world.types import MODERATOR_ID, UiEvent

logger = logging.getLogger(__name__)

BUDGET_PUSH_INTERVAL_S = 1.0
REFINE_MAX_TOKENS = 2000
REVIEW_MAX_TOKENS = 2000
REVIEW_TIMEOUT_S = 180.0


class RosterRuntimeMixin:
    # Class-level defaults so partially initialized instances (tests) stay valid.
    roster_store: RosterStore | None = None  # injectable (tests); ``None`` = the workspace's store
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
        """No roster file is read or created before the workspace is active, or when it could not be opened."""
        return not self._roster_io_suspended and not getattr(self, "_workspace_failed", False)

    def _roster_snapshot(self) -> dict[str, Any]:
        """The roster as the next game takes it; falls back to the config's names when it cannot be read."""
        if not self._roster_io_allowed():
            return {"memory_enabled": True, "seats": []}
        try:
            roster = self._roster()
            self._ensure_avatars(roster)
            return roster.snapshot()
        except Exception:  # noqa: BLE001 - never block a new game on a roster problem
            logger.warning("Could not read the roster", exc_info=True)
            return {"memory_enabled": False, "seats": []}

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
        return players

    def roster_payload(self) -> dict[str, Any]:
        """``GET /api/roster``: the editable roster plus whether names / avatars may be changed now."""
        roster = self._roster()
        self._ensure_avatars(roster)
        data = roster.get()
        data["editable"] = self.session.status == "idle" and not getattr(self, "_switching_workspace", False)
        return data

    def _roster_event(self) -> UiEvent:
        return UiEvent(
            type="roster.updated",
            data={"participants": [{"id": p.id, "name": p.name, "avatar": p.avatar} for p in self.session.participants]},
        )

    def _require_idle(self, what: str) -> None:
        if self.session.status != "idle":
            raise RuntimeError(f"{what}只能在对局开始之前修改；请先新建对局。")
        self._ensure_not_switching()  # type: ignore[attr-defined]

    async def _rebuild_idle_world(self) -> None:
        """Rename / re-avatar in an idle game: rebuild its world with the *same seed* (so the identities that
        were dealt stay exactly as they are) and refresh the session's seats from the roster."""
        seed = self.world.state.rng.seed
        players = self._roster_players()
        world, clock = self._new_world(self.spec, seed=seed)  # type: ignore[attr-defined]
        self.world, self.clock = world, clock
        from app.game.session import Participant

        self.session.participants = [Participant(**p) for p in players]
        self.session.memory_enabled = bool(getattr(self, "_memory_enabled_snapshot", True))
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
    ) -> dict[str, Any]:
        """Change one seat.  Name / avatar need an idle game; persona / memory are files and may be edited at
        any time (a running game keeps the copy it started with)."""
        roster = self._roster()
        entry = roster.entry(agent_id)  # validates the id
        rebuild = False
        if name is not None and str(name).strip() != entry["name"]:
            self._require_idle("名字")
            roster.set_name(agent_id, name)
            rebuild = True
        if avatar is not None:
            self._require_idle("头像")
            try:
                from app import avatars

                avatar = avatars.validate_description(avatar)
            except ImportError:
                pass
            except ValueError as exc:
                raise RosterError(str(exc)) from None
            roster.set_avatar(agent_id, avatar)
            rebuild = True
        if persona is not None:
            roster.set_persona(agent_id, persona)
        if memory is not None:
            roster.set_memory(agent_id, memory)
        if rebuild:
            await self._rebuild_idle_world()
        elif (persona is not None or memory is not None) and self.session.status == "idle":
            self._refresh_idle_texts()
        return roster.entry(agent_id)

    def _reload_fresh_game_from_workspace(self) -> None:
        """At startup the first (idle) game was built before the workspace was active: take the roster and
        the budget default from the workspace now.  Same dealt identities (same seed), nothing is saved."""
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

    async def regenerate_avatars(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """New random avatars for one seat or all of them (idle games only)."""
        self._require_idle("头像")
        try:
            from app import avatars
        except ImportError as exc:
            raise RosterError("头像素材库不可用。") from exc
        roster = self._roster()
        rows = roster.get()["seats"]
        if agent_id is not None:
            roster.entry(agent_id)
            taken = [r["avatar"] for r in rows if r["id"] != agent_id and r["avatar"]]
            roster.set_avatar(agent_id, avatars.make_description(int(agent_id[1:]), taken))
        else:
            roster.set_all_avatars({r["id"]: d for r, d in zip(rows, avatars.make_roster(len(rows)))})
        await self._rebuild_idle_world()
        return roster.get()["seats"] if agent_id is None else [roster.entry(agent_id)]

    async def set_memory_enabled(self, enabled: bool) -> dict[str, Any]:
        self._roster().set_memory_enabled(bool(enabled))
        if self.session.status == "idle":
            self._refresh_idle_texts()
        return self.roster_payload()

    async def clear_memory(self, *, include_persona: bool = False) -> dict[str, Any]:
        self._roster().clear(include_persona=include_persona)
        if self.session.status == "idle":
            self._refresh_idle_texts()
        return self.roster_payload()

    def export_memory(self) -> bytes:
        return self._roster().export_zip()

    async def import_memory(self, data: bytes, *, dry_run: bool) -> dict[str, Any]:
        roster = self._roster()
        changes = roster.preview_import(data) if dry_run else roster.import_zip(data)
        if not dry_run and self.session.status == "idle":
            self._refresh_idle_texts()
        return {"dry_run": dry_run, "changes": changes}

    # --------------------------------------------------------------------- one-shot model calls ---
    def _oneshot_port(self) -> Any:
        """The model port for calls outside the runtime loop: the demo bots when installed, else the engine
        client behind the same per-agent bindings the game uses."""
        if self.llm_port_factory is not None:  # type: ignore[attr-defined]
            return self.llm_port_factory(self)  # type: ignore[attr-defined]
        from app.runtime.llm_gate import GatedEngineLlm, LlmGate

        rt = self.cfg.get("runtime") or {}  # type: ignore[attr-defined]
        return GatedEngineLlm(
            self.llm,  # type: ignore[attr-defined]
            self.session.get_model_binding,  # type: ignore[attr-defined]
            LlmGate(int(rt.get("llm_concurrency", 0)), int(rt.get("llm_concurrency_per_provider", 0))),
            float(rt.get("llm_timeout_seconds", 120)),
        )

    def _record_oneshot(self, who: str, resp: Any) -> None:
        try:
            self.usage.record_llm(who, resp)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.warning("Could not account a one-shot call", exc_info=True)

    async def refine_text(self, agent_id: str, kind: str, text: str) -> str:
        """Expand a few notes into a complete persona / memory with the *moderator's* model.  The draft is
        returned, never saved: the user reads it first."""
        roster = self._roster()
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
        messages = build_refine_messages(kind, source, name=entry["name"])
        resp = await self._oneshot_port().complete(MODERATOR_ID, messages, None, max_tokens=REFINE_MAX_TOKENS)
        self._record_oneshot("roster", resp)
        draft = clean_generated_text(getattr(resp, "content", "") or "", limit=REFINE_MAX_CHARS)
        if not draft:
            raise RosterError("模型没有返回内容，请稍后重试。")
        return draft

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
        self._spawn(self._review_many(todo))  # type: ignore[attr-defined]
        await self.broadcast_state("review_updated")  # type: ignore[attr-defined]
        return todo

    async def _review_many(self, ids: list[str]) -> None:
        port = self._oneshot_port()
        await asyncio.gather(*(self._review_one(port, pid) for pid in ids))
        await self.broadcast_state("review_updated")  # type: ignore[attr-defined]

    def _progress(self, agent_id: str, state: str, message: str = "") -> None:
        self.bus.publish(  # type: ignore[attr-defined]
            UiEvent(type="review.progress", data={"id": agent_id, "state": state, "message": message})
        )

    async def _review_one(self, port: Any, agent_id: str) -> None:
        self._progress(agent_id, "running")
        try:
            spec, world, session = self.spec, self.world, self.session  # type: ignore[attr-defined]
            player = world.player(agent_id)
            faction = getattr(player, "faction", "")
            won = player_won(spec, faction, world.state.winner)
            outcome = {
                True: "你所在的阵营获胜了。",
                False: "你所在的阵营失败了。",
                None: f"这局的结果是：{world.state.winner or '未知'}。",
            }[won]
            label = role_label(spec, getattr(player, "role", ""))
            persona = next((p.persona for p in session.participants if p.id == agent_id), "")
            messages = build_review_messages(
                world, agent_id, outcome_text=outcome, role_text=f"{label}（{faction or '无阵营'}）", persona=persona
            )
            async with asyncio.timeout(REVIEW_TIMEOUT_S):
                resp = await port.complete(agent_id, messages, None, max_tokens=REVIEW_MAX_TOKENS)
            self._record_oneshot(agent_id, resp)
            text = clean_generated_text(getattr(resp, "content", "") or "", limit=REVIEW_MAX_CHARS)
            if not text:
                raise RosterError("模型没有返回复盘内容。")
            roster = self._roster()
            if won is None:
                # an unknown outcome adds the review but leaves the win / loss record alone
                current = roster.entry(agent_id)["memory"]
                from app.storage.roster import SECTION_MEMORY, join_memory, split_memory

                sections = split_memory(current)
                previous = sections.get(SECTION_MEMORY, "").strip("\n")
                sections[SECTION_MEMORY] = (previous + "\n\n" if previous else "") + text
                roster.set_memory(agent_id, join_memory(sections))
            else:
                roster.apply_review(agent_id, role=label, won=won, review_text=text)
            if agent_id not in session.review_done:
                session.review_done.append(agent_id)
            self._progress(agent_id, "done")
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
