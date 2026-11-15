from __future__ import annotations

import asyncio
import copy
import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.engine.expressions import (
    ParticipantView,
    SafeExpression,
    leaders,
    render_values,
    seat_order,
    tally,
)
from app.engine.llm import ToolCallData
from app.engine.scenario import ScenarioDocument, ScenarioError
from app.engine.state import FactionStat, GameSession, TagStat


class _GotoSignal(Exception):
    def __init__(self, step_id: str) -> None:
        self.step_id = step_id


class _BreakSignal(Exception):
    pass


class ScenarioToolError(ScenarioError):
    """A tool invoked by the scenario reported a failure."""


@dataclass
class _ExecutionResult:
    next_step: str | None = None


class ScenarioDirector:
    """Execute a game-agnostic, snapshotted Markdown workflow."""

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator
        self.session: GameSession = orchestrator.session
        snapshot = self.session.scenario_snapshot or orchestrator.scenario.snapshot()
        self.document = ScenarioDocument.from_snapshot(snapshot)
        self.definition = self.document.definition
        self.runtime = self.session.scenario_runtime
        if not self.runtime:
            self.runtime = {
                "id": self.document.scenario_id,
                "version": self.document.version,
                "checksum": self.document.checksum,
                "step_id": str(self.definition["entry"]),
                "variables": copy.deepcopy(self.definition.get("variables", {})),
                "events": [],
                "event_history": [],
                "steps_executed": 0,
                "completed_actions": [],
                "control_decisions": {},
                "loop_snapshots": {},
                "active_loops": {},
                "finished": False,
            }
            self.session.scenario_runtime = self.runtime
        if self.runtime.get("checksum") != self.document.checksum:
            raise ScenarioError("Running session scenario checksum does not match its snapshot")
        self.steps = {str(step["id"]): step for step in self.definition["workflow"]}
        self._views: dict[str, ParticipantView] = {}
        self._locals: dict[str, Any] = {}
        self.max_steps = int(self.definition.get("limits", {}).get("max_steps", 10_000))
        self.max_events = int(self.definition.get("limits", {}).get("max_events", 200))

    @property
    def variables(self) -> dict[str, Any]:
        return self.runtime.setdefault("variables", {})

    def _refresh_views(self) -> list[ParticipantView]:
        views = [
            ParticipantView(
                id=participant.id,
                seat=participant.seat,
                name=participant.name,
                tags=tuple(participant.tags),
                data=copy.deepcopy(participant.data),
            )
            for participant in self.session.participants
        ]
        self._views = {view.id: view for view in views}
        return views

    def _view(self, player_id: str) -> ParticipantView:
        self._refresh_views()
        try:
            return self._views[str(player_id)]
        except KeyError as exc:
            raise ScenarioError(f"Unknown participant: {player_id}") from exc

    @staticmethod
    def _ids(items: Any) -> list[str]:
        if items is None:
            return []
        if isinstance(items, (str, ParticipantView)):
            items = [items]
        result: list[str] = []
        for item in items:
            if isinstance(item, ParticipantView):
                result.append(item.id)
            elif isinstance(item, dict):
                result.append(str(item.get("id", "")))
            else:
                result.append(str(item))
        return [item for item in result if item]

    @staticmethod
    def _get(value: Any, key: Any, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(key, default)
        if isinstance(value, (list, tuple)) and isinstance(key, int):
            try:
                return value[key]
            except IndexError:
                return default
        return default

    def _evaluator(self, local_names: dict[str, Any] | None = None) -> SafeExpression:
        views = self._refresh_views()
        names: dict[str, Any] = {
            **self.variables,
            "variables": self.variables,
            "players": views,
            "events": self.runtime.setdefault("events", []),
            **self._locals,
            **(local_names or {}),
        }

        def selected(items: Any = None) -> list[Any]:
            return views if items is None else list(items)

        def alive(items: Any = None) -> list[Any]:
            return [item for item in selected(items) if item.data.get("alive")]

        def by_role(role: str, items: Any = None) -> list[Any]:
            return [item for item in selected(items) if item.data.get("role") == role]

        def by_faction(faction: str, items: Any = None) -> list[Any]:
            return [item for item in selected(items) if item.data.get("faction") == faction]

        def by_tag(tag: str, items: Any = None) -> list[Any]:
            return [item for item in selected(items) if tag in item.tags]

        functions = {
            "players": lambda: views,
            "alive": alive,
            "by_role": by_role,
            "by_faction": by_faction,
            "by_tag": by_tag,
            "ids": self._ids,
            "first": lambda items, default=None: next(iter(items), default),
            "count": lambda items: len(items),
            "len": lambda items: len(items),
            "sum": lambda items: sum(items),
            "min": lambda items: min(items),
            "max": lambda items: max(items),
            "get": self._get,
            "var": lambda name, default=None: self.variables.get(name, default),
            "tally": tally,
            "leaders": leaders,
            "seat_order": seat_order,
        }
        return SafeExpression(names, functions)

    def _value(self, value: Any, local_names: dict[str, Any] | None = None) -> Any:
        return render_values(value, lambda expression: self._evaluator(local_names).evaluate(expression))

    def _set_variable(self, path: str, value: Any) -> None:
        parts = [part for part in str(path).split(".") if part]
        if not parts or any(part.startswith("_") for part in parts):
            raise ScenarioError(f"Invalid variable path: {path!r}")
        target = self.variables
        for part in parts[:-1]:
            current = target.get(part)
            if not isinstance(current, dict):
                current = {}
                target[part] = current
            target = current
        target[parts[-1]] = value

    async def run(self) -> None:
        while (
            not self.orchestrator._pause_requested
            and self.session.status == "running"
            and not self.runtime.get("finished")
        ):
            if int(self.runtime.get("steps_executed", 0)) >= self.max_steps:
                raise ScenarioError(f"Scenario exceeded max_steps={self.max_steps}")
            await self.run_step()
            await asyncio.sleep(0.05)

    async def run_step(self) -> None:
        step_id = str(self.runtime.get("step_id", ""))
        step = self.steps.get(step_id)
        if step is None:
            raise ScenarioError(f"Unknown runtime step: {step_id}")
        self.session.active_status_text = f"剧本步骤 · {step_id}"
        self.session.set_moderator_state(True, f"执行声明式剧本步骤：{step_id}")
        await self.orchestrator.broadcast_state("scenario_step_start")
        try:
            try:
                await self._execute_actions(step.get("do", []))
                if self.orchestrator._pause_requested or self.session.status != "running":
                    return
                next_step = self._value(step.get("next")) if step.get("next") is not None else None
            except _GotoSignal as signal:
                next_step = signal.step_id
            if next_step is not None:
                next_step = str(next_step)
                if next_step not in self.steps:
                    raise ScenarioError(f"Unknown next step: {next_step}")
                self.runtime["step_id"] = next_step
                self.runtime["completed_actions"] = []
                self.runtime["control_decisions"] = {}
                self.runtime["loop_snapshots"] = {}
                self.runtime["active_loops"] = {}
            self.runtime["steps_executed"] = int(self.runtime.get("steps_executed", 0)) + 1
            await self._refresh_dashboard()
            await self._check_termination()
            if next_step is None and not self.runtime.get("finished"):
                raise ScenarioError(f"Step {step_id!r} has no next step and did not terminate")
        finally:
            await self.orchestrator.broadcast_state("scenario_step_done")

    def _mark_action_complete(self, key: str) -> None:
        completed = self.runtime.setdefault("completed_actions", [])
        if key not in completed:
            completed.append(key)

    async def _execute_actions(self, actions: list[dict[str, Any]], path: str = "do") -> None:
        completed = self.runtime.setdefault("completed_actions", [])
        for index, action in enumerate(actions):
            action_key = f"{path}/{index}"
            if action_key in completed:
                continue
            if self.orchestrator._pause_requested or self.session.status != "running":
                return
            name, raw_specification = next(iter(action.items()))
            if name == "if":
                specification = raw_specification
                decisions = self.runtime.setdefault("control_decisions", {})
                if action_key not in decisions:
                    decisions[action_key] = "do" if self._value(specification["when"]) else "else"
                branch_name = decisions[action_key]
                await self._execute_actions(
                    specification.get(branch_name, []), f"{action_key}/{branch_name}"
                )
                if not self.orchestrator._pause_requested and self.session.status == "running":
                    self._mark_action_complete(action_key)
                continue
            if name == "for_each":
                await self._execute_for_each(raw_specification, action_key)
                if not self.orchestrator._pause_requested and self.session.status == "running":
                    self._mark_action_complete(action_key)
                continue
            if name == "while":
                await self._execute_while(raw_specification, action_key)
                if not self.orchestrator._pause_requested and self.session.status == "running":
                    self._mark_action_complete(action_key)
                continue
            if name == "goto":
                target = str(self._value(raw_specification))
                raise _GotoSignal(target)
            if name == "break":
                raise _BreakSignal()

            if name == "dispatch_events" and isinstance(raw_specification, dict):
                render_specification = dict(raw_specification)
                deferred_template = render_specification.pop("template", None)
                specification = self._value(render_specification)
                if deferred_template is not None:
                    specification["template"] = deferred_template
            else:
                specification = self._value(raw_specification)
            if name == "tool":
                await self._action_tool(specification)
            elif name == "deal_identities":
                await self._action_deal_identities()
            elif name == "set":
                self._action_set(specification)
            elif name == "patch_player":
                await self._action_patch_player(specification)
            elif name == "choice":
                await self._action_choice(specification)
            elif name == "speak":
                await self._action_speak(specification)
            elif name == "vote":
                await self._action_vote(specification)
            elif name == "plan":
                await self._action_plan(specification)
            elif name == "interrupt":
                await self._action_interrupt(specification)
            elif name == "eliminate":
                await self._action_eliminate(specification)
            elif name == "dispatch_events":
                await self._action_dispatch_events(specification, action_key)
            elif name == "refresh_dashboard":
                await self._refresh_dashboard()
            else:
                raise ScenarioError(f"Unsupported runtime action: {name}")
            if name != "dispatch_events" or (
                not self.orchestrator._pause_requested and self.session.status == "running"
            ):
                self._mark_action_complete(action_key)

    def _pack_loop_item(self, item: Any) -> Any:
        if isinstance(item, ParticipantView):
            return {"__arena_participant__": item.id}
        if isinstance(item, dict):
            return {str(key): self._pack_loop_item(value) for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [self._pack_loop_item(value) for value in item]
        return copy.deepcopy(item)

    def _unpack_loop_item(self, item: Any) -> Any:
        if isinstance(item, dict) and set(item) == {"__arena_participant__"}:
            return self._view(str(item["__arena_participant__"]))
        if isinstance(item, dict):
            return {key: self._unpack_loop_item(value) for key, value in item.items()}
        if isinstance(item, list):
            return [self._unpack_loop_item(value) for value in item]
        return copy.deepcopy(item)

    async def _execute_for_each(self, specification: dict[str, Any], path: str) -> None:
        variable_name = str(specification["as"])
        if variable_name.startswith("_"):
            raise ScenarioError("Loop variable cannot be private")
        snapshots = self.runtime.setdefault("loop_snapshots", {})
        if path in snapshots:
            items = [self._unpack_loop_item(item) for item in snapshots[path]]
        else:
            items = self._value(specification["items"])
            if items is None:
                return
            if isinstance(items, (str, bytes, dict)):
                raise ScenarioError("for_each.items must resolve to a sequence")
            snapshots[path] = [self._pack_loop_item(item) for item in items]
            items = [self._unpack_loop_item(item) for item in snapshots[path]]
        limit = int(specification.get("max_iterations", 200))
        occurrences: dict[str, int] = {}
        for index, item in enumerate(items):
            if index >= limit:
                raise ScenarioError(f"for_each exceeded max_iterations={limit}")
            previous = self._locals.get(variable_name, ...)
            self._locals[variable_name] = item
            try:
                base_key = item.id if isinstance(item, ParticipantView) else str(item)
                # Duplicate items must not share a completion key (the second one would be
                # skipped as "already done").  The first occurrence keeps the historical key
                # so saves taken mid-loop by older versions still resume correctly.
                seen = occurrences.get(base_key, 0)
                occurrences[base_key] = seen + 1
                item_key = base_key if seen == 0 else f"{base_key}#{seen}"
                await self._execute_actions(
                    specification.get("do", []), f"{path}/item:{item_key}"
                )
            except _BreakSignal:
                break
            finally:
                if previous is ...:
                    self._locals.pop(variable_name, None)
                else:
                    self._locals[variable_name] = previous

    async def _execute_while(self, specification: dict[str, Any], path: str) -> None:
        limit = int(specification.get("max_iterations", 100))
        iterations = 0
        active_loops = self.runtime.setdefault("active_loops", {})
        while True:
            active_iteration = active_loops.get(path)
            if active_iteration is None:
                if not self._value(specification["when"]):
                    break
                if iterations >= limit:
                    raise ScenarioError(f"while exceeded max_iterations={limit}")
                iterations += 1
                active_iteration = iterations
                active_loops[path] = active_iteration
            else:
                iterations = max(iterations, int(active_iteration))
            try:
                await self._execute_actions(
                    specification.get("do", []), f"{path}/iteration:{active_iteration}"
                )
            except _BreakSignal:
                active_loops.pop(path, None)
                break
            if self.orchestrator._pause_requested or self.session.status != "running":
                return
            active_loops.pop(path, None)

    async def _call_moderator_tool(
        self, name: str, args: dict[str, Any], *, on_error: str = "pause"
    ) -> tuple[str, Any]:
        """Run a moderator tool; a failed tool is an error, never a "normal" empty result.

        ``on_error="pause"`` (default) raises :class:`ScenarioToolError`, which pauses the
        game with the tool's own message.  ``on_error="continue"`` lets a scenario
        explicitly tolerate a failing tool (the failure is still logged).
        """
        call = ToolCallData(
            id=f"scenario_{uuid.uuid4().hex[:10]}",
            name=name,
            arguments_json=json.dumps(args, ensure_ascii=False),
        )
        self.orchestrator._last_tool_data = None
        self.orchestrator._last_tool_error = None
        message, _ = await self.orchestrator._execute_moderator_tool(call)
        error = getattr(self.orchestrator, "_last_tool_error", None)
        if error and on_error != "continue":
            raise ScenarioToolError(f"Tool {name!r} failed: {error}")
        return message, self.orchestrator._last_tool_data

    @staticmethod
    def _require_channel(specification: dict[str, Any], action: str) -> str:
        channel_id = specification.get("channel_id")
        if channel_id is None or not str(channel_id).strip():
            raise ScenarioError(f"{action} requires a channel_id")
        return str(channel_id).strip()

    async def _action_tool(self, specification: dict[str, Any]) -> None:
        name = str(specification.get("name", ""))
        args = specification.get("args") or {}
        if not name or not isinstance(args, dict):
            raise ScenarioError("tool requires name and mapping args")
        on_error = str(specification.get("on_error", "pause"))
        if on_error not in ("pause", "continue"):
            raise ScenarioError("tool.on_error must be 'pause' or 'continue'")
        message, data = await self._call_moderator_tool(name, args, on_error=on_error)
        save_as = specification.get("save_as")
        if save_as:
            self._set_variable(str(save_as), data if data is not None else {"message": message})

    async def _action_deal_identities(self) -> None:
        roles = self.definition.get("roles", {})
        identity_setup = self.definition.get("identity_setup", {})
        expected_counts = {
            str(item["role"]): int(item.get("count", 1))
            for item in identity_setup.get("pool", [])
        }
        if expected_counts and sum(expected_counts.values()) != len(self.session.participants):
            raise ScenarioError("Identity pool size must equal the participant count")
        assigned_counts: dict[str, int] = {}
        for assignment in self.session.initial_assignments.values():
            role_name = str(assignment.get("role", ""))
            assigned_counts[role_name] = assigned_counts.get(role_name, 0) + 1
        if expected_counts and assigned_counts != expected_counts:
            raise ScenarioError(
                f"Identity assignments do not match the declared pool: {assigned_counts} != {expected_counts}"
            )
        default_tags = list(identity_setup.get("default_status_tags", []))
        for participant in self.session.participants:
            assignment = self.session.initial_assignments.get(participant.id)
            if not assignment or assignment.get("role") not in roles:
                raise ScenarioError(f"Missing declared identity assignment for {participant.id}")
            role_name = str(assignment["role"])
            role = roles[role_name]
            attributes = copy.deepcopy(role.get("attributes", {}))
            attributes["role"] = role_name
            attributes["faction"] = str(assignment.get("faction") or role.get("faction", ""))
            attributes.setdefault("alive", True)
            participant.data = attributes
            tags = [*default_tags, *list(role.get("tags", [role_name]))]
            participant.tags = list(dict.fromkeys(str(tag) for tag in tags))
        self._set_variable("initialized", True)
        self.orchestrator._log_tool("deal_identities", {}, "身份种子已按剧本角色定义发放", None)
        await self.orchestrator.broadcast_state("identities_dealt")

    def _action_set(self, specification: Any) -> None:
        if isinstance(specification, dict):
            entries = [{"path": path, "value": value} for path, value in specification.items()]
        elif isinstance(specification, list):
            entries = specification
        else:
            raise ScenarioError("set requires a mapping or a list of path/value entries")
        for entry in entries:
            if not isinstance(entry, dict) or "path" not in entry:
                raise ScenarioError("Every set entry requires path and value")
            self._set_variable(str(entry["path"]), entry.get("value"))

    async def _action_patch_player(self, specification: dict[str, Any]) -> None:
        player_id = str(specification.get("player_id", ""))
        participant = self.session.get_participant(player_id)
        if participant is None:
            raise ScenarioError(f"patch_player unknown participant: {player_id}")
        for key, value in (specification.get("data") or {}).items():
            if str(key).startswith("_"):
                raise ScenarioError("Participant data keys cannot be private")
            participant.data[str(key)] = copy.deepcopy(value)
        remove_tags = {str(tag) for tag in specification.get("remove_tags", [])}
        if remove_tags:
            participant.tags = [tag for tag in participant.tags if tag not in remove_tags]
        for tag in specification.get("add_tags", []):
            if str(tag) not in participant.tags:
                participant.tags.append(str(tag))
        if "tags" in specification:
            participant.tags = [str(tag) for tag in specification["tags"]]
        self.orchestrator._log_tool("patch_player", specification, f"已更新 {player_id}", None)
        await self.orchestrator.broadcast_state("participant_patched")

    async def _action_choice(self, specification: dict[str, Any]) -> None:
        actors = self._ids(specification.get("actors"))
        options = specification.get("options") or []
        if not actors or not isinstance(options, list) or not options:
            raise ScenarioError("choice requires actors and at least one option")
        normalized_options = [
            {"id": str(option["id"]), "label": str(option.get("label", option["id"]))}
            for option in options
        ]
        requests = [
            {
                "player_id": player_id,
                "question": str(specification.get("question", "请做出选择")),
                "options": normalized_options,
            }
            for player_id in actors
        ]
        message, data = await self._call_moderator_tool(
            "request_choices",
            {"requests": requests, "save_as": specification.get("save_as", "choice")},
        )
        if data is None:
            raise ScenarioToolError(f"choice returned no structured result: {message}")
        result = data
        if specification.get("save_as"):
            self._set_variable(str(specification["save_as"]), result)

    async def _action_speak(self, specification: dict[str, Any]) -> None:
        player_id = str(specification.get("player_id") or "")
        channel_id = self._require_channel(specification, "speak")
        channel = self.session.get_channel(channel_id)
        participant = self.session.get_participant(player_id)
        if not channel or not participant:
            raise ScenarioError(f"speak channel or participant does not exist: {channel_id}/{player_id}")
        if channel.status != "open" or player_id not in channel.members:
            raise ScenarioError(f"speak is not authorized in channel {channel_id} for {player_id}")
        content, _ = await self.orchestrator._run_participant_speak(
            player_id,
            channel_id,
            str(specification.get("instruction", "")),
        )
        result = {"player_id": player_id, "channel_id": channel_id, "content": content}
        self.orchestrator._log_tool("speak", specification, content, channel_id)
        if specification.get("save_as"):
            self._set_variable(str(specification["save_as"]), result)

    async def _action_vote(self, specification: dict[str, Any]) -> None:
        args = {
            "channel_id": self._require_channel(specification, "vote"),
            "voter_ids": self._ids(specification.get("voter_ids")),
            "candidate_ids": self._ids(specification.get("candidate_ids")),
            "vote_topic": specification.get("topic", "投票表决"),
            "allow_abstain": bool(specification.get("allow_abstain", True)),
        }
        message, data = await self._call_moderator_tool("request_players_vote", args)
        if data is None:
            # A successful vote always yields structured ballots; never invent "nobody voted".
            raise ScenarioToolError(f"vote returned no structured result: {message}")
        if specification.get("save_as"):
            self._set_variable(str(specification["save_as"]), data)

    async def _action_plan(self, specification: dict[str, Any]) -> None:
        args = {
            "player_ids": self._ids(specification.get("player_ids")),
            "phase_topic": specification.get("phase_topic", "战略规划"),
            "instruction": specification.get("instruction", ""),
        }
        message, data = await self._call_moderator_tool("request_players_strategic_planning", args)
        if specification.get("save_as"):
            self._set_variable(str(specification["save_as"]), data or {"message": message})

    async def _action_interrupt(self, specification: dict[str, Any]) -> None:
        args = {
            "channel_id": self._require_channel(specification, "interrupt"),
            "player_id": specification.get("player_id"),
            "instruction": specification.get("instruction", "请提交插话内容与可选目标"),
            "candidate_ids": self._ids(specification.get("candidate_ids")),
        }
        message, data = await self._call_moderator_tool("request_player_interrupt", args)
        if data is None:
            raise ScenarioToolError(f"interrupt returned no structured result: {message}")
        if specification.get("save_as"):
            self._set_variable(str(specification["save_as"]), data)

    async def _action_eliminate(self, specification: dict[str, Any]) -> None:
        player_id = str(specification.get("player_id", ""))
        participant = self.session.get_participant(player_id)
        if participant is None:
            raise ScenarioError(f"eliminate unknown participant: {player_id}")
        if participant.data.get("alive") is False:
            return
        previous_tags = list(participant.tags)
        participant.data["alive"] = False
        elimination = self.definition.get("elimination", {})
        remove_tags = {str(tag) for tag in elimination.get("remove_tags", [])}
        participant.tags = [tag for tag in participant.tags if tag not in remove_tags]
        for tag in elimination.get("add_tags", []):
            if str(tag) not in participant.tags:
                participant.tags.append(str(tag))
        event = {
            "id": f"event_{uuid.uuid4().hex[:10]}",
            "type": "eliminated",
            "player_id": player_id,
            "cause": str(specification.get("cause", "unspecified")),
            "actor_id": str(specification.get("actor_id", "")),
            "day": self.variables.get("day"),
            "previous_tags": previous_tags,
        }
        self.runtime.setdefault("events", []).append(event)
        if len(self.runtime["events"]) > self.max_events:
            raise ScenarioError(f"Pending event queue exceeded max_events={self.max_events}")
        self.orchestrator._log_tool("eliminate", specification, f"已产生 {player_id} 出局事件", None)
        await self.orchestrator.broadcast_state("scenario_event_queued")

    async def _action_dispatch_events(self, specification: dict[str, Any], path: str) -> None:
        events = self.runtime.setdefault("events", [])
        if not events:
            empty_message = str(specification.get("empty_message", ""))
            empty_key = f"{path}/empty_announcement"
            if empty_message and empty_key not in self.runtime.setdefault("completed_actions", []):
                await self._call_moderator_tool(
                    "send_message",
                    {
                        "channel_id": self._require_channel(specification, "dispatch_events"),
                        "content": empty_message,
                    },
                )
                self._mark_action_complete(empty_key)
            return
        processed = 0
        while events:
            if processed >= self.max_events:
                raise ScenarioError(f"Event cascade exceeded max_events={self.max_events}")
            event = events[0]
            event_key = f"{path}/event:{event['id']}"
            player = self._view(str(event["player_id"]))
            event_view = {**event, "player": player}
            local_names = {
                "event": event_view,
                "player": player,
                "cause": event.get("cause"),
                "event_day": event.get("day"),
            }
            announcement_key = f"{event_key}/announcement"
            # ``announced`` lives on the event itself, so it survives a goto that changes the
            # step (and with it the completed-action keys): an event is announced exactly once.
            if announcement_key not in self.runtime.setdefault(
                "completed_actions", []
            ) and not event.get("announced"):
                template = specification.get("template", "{{player.seat}} {{player.name}} 出局")
                content = str(self._value(template, local_names))
                if content:
                    await self._call_moderator_tool(
                        "send_message",
                        {
                            "channel_id": self._require_channel(specification, "dispatch_events"),
                            "content": content,
                        },
                    )
                event["announced"] = True
                self._mark_action_complete(announcement_key)
            hooks = self.definition.get("hooks", {}).get(event.get("type"), [])
            try:
                for hook_index, hook in enumerate(hooks):
                    when = hook.get("when", True)
                    if self._value(when, local_names):
                        previous_locals = self._locals
                        self._locals = {**previous_locals, **local_names}
                        try:
                            await self._execute_actions(
                                hook.get("do", []), f"{event_key}/hook:{hook_index}"
                            )
                        finally:
                            self._locals = previous_locals
                    if self.orchestrator._pause_requested or self.session.status != "running":
                        return
            except _GotoSignal:
                # A hook jumped to another step: this event is fully handled.  Retire it so
                # the step we jump to does not re-announce it or re-run its hooks.
                if events and events[0].get("id") == event.get("id"):
                    events.pop(0)
                    self.runtime.setdefault("event_history", []).append(event)
                raise
            if events[0].get("id") != event.get("id"):
                raise ScenarioError("Event queue order changed during dispatch")
            events.pop(0)
            self.runtime.setdefault("event_history", []).append(event)
            processed += 1
        await self.orchestrator.broadcast_state("scenario_events_dispatched")

    async def _refresh_dashboard(self) -> None:
        dashboard = self.definition.get("dashboard")
        if not dashboard:
            return
        rendered = self._value(dashboard)
        factions = [
            FactionStat(
                name=str(item.get("name", "")),
                alive=int(item.get("alive", 0)),
                total=int(item.get("total", 0)),
                variant=str(item.get("variant", "neutral")),
            )
            for item in rendered.get("factions", [])
        ]
        stats = [
            TagStat(
                name=str(item.get("name", "")),
                count=int(item.get("count", 0)),
                variant=str(item.get("variant", "neutral")),
            )
            for item in rendered.get("tag_stats", [])
        ]
        self.session.dashboard.factions = factions
        self.session.dashboard.tag_stats = stats

    async def _check_termination(self) -> None:
        if self.runtime.get("finished") or self.runtime.setdefault("events", []):
            return
        for rule in self.definition.get("termination", []):
            if not self._value(rule.get("when")):
                continue
            winner = str(self._value(rule.get("winner")))
            summary = str(self._value(rule.get("summary", "")))
            channel_id = str(self._value(rule.get("channel_id", "")))
            self.runtime["finished"] = True
            self.session.winner_faction = winner
            self.session.status = "finished"
            self.session.clear_all_generating()
            self.session.active_status_text = f"已结束 · {winner}"
            self.session.set_moderator_state(False, f"对局已结束 · {winner}")
            if channel_id and self.session.get_channel(channel_id):
                self.session.add_record(
                    stream="channel",
                    channel_id=channel_id,
                    kind="message",
                    actor_id="moderator",
                    content=summary,
                )
            self.orchestrator._log_tool("terminate", rule, winner, channel_id or None)
            await self.orchestrator.broadcast_state("game_ended")
            return
