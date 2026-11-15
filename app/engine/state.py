from __future__ import annotations

import copy
import random
import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.providers.models import ModelBinding


def now_time_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Participant(BaseModel):
    id: str
    seat: str
    name: str
    persona: str = ""
    tags: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    generating: bool = False
    active_stage: str = ""


class InterruptQuota(BaseModel):
    player_id: str
    player_name: str
    used: int = 0
    max_count: int = 1
    reason: str = ""


class Channel(BaseModel):
    id: str
    name: str
    type: Literal["public", "group", "dm"] = "public"
    status: Literal["open", "closed"] = "open"
    members: list[str] = Field(default_factory=list)
    interrupt_quotas: dict[str, InterruptQuota] = Field(default_factory=dict)


class FeedRecord(BaseModel):
    id: str
    timestamp: str
    stream: Literal["engine", "channel"] = "channel"
    channel_id: str | None = None
    kind: Literal["message", "thought", "tool_call", "note"] = "message"
    actor_id: str = "moderator"
    actor_name: str = "主持人"
    content: str = ""
    badge: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    visible_to: list[str] = Field(default_factory=list)
    visible_to_ids: list[str] = Field(default_factory=list)
    action_id: str | None = None
    action_type: str | None = None


class StreamingDraft(BaseModel):
    actor_id: str
    actor_name: str
    stream: Literal["engine", "channel"] = "channel"
    channel_id: str | None = None
    badge: str | None = "思维链"
    thought_text: str = ""
    content_text: str = ""
    visible_to: list[str] = Field(default_factory=list)
    visible_to_ids: list[str] = Field(default_factory=list)
    action_id: str | None = None
    action_type: str | None = None


class FactionStat(BaseModel):
    name: str
    alive: int
    total: int
    variant: str = "neutral"


class TagStat(BaseModel):
    name: str
    count: int
    variant: str = "neutral"


class DashboardState(BaseModel):
    factions: list[FactionStat] = Field(default_factory=list)
    tag_stats: list[TagStat] = Field(default_factory=list)


class ActorUsage(BaseModel):
    """Per-participant usage; cost is the sum of per-call price snapshots."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class ModelUsage(ActorUsage):
    """Per-model usage; the prices are those of the most recent call (a snapshot)."""

    provider_id: str = ""
    model_id: str = ""
    input_price_per_m: float = 0.0
    output_price_per_m: float = 0.0


class UsageStats(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Sum of per-call costs, each computed with the price snapshot carried by that call's
    # response.  Editing a model's price later never recomputes history.
    total_cost_usd: float = 0.0
    # Legacy single-model fields: used only to price calls that carry no snapshot (old saves,
    # cfg-fallback clients) and kept for backward compatibility of saves / the API.
    input_price_per_m: float = 0.27
    output_price_per_m: float = 1.10
    context_window_tokens: int = 1_000_000
    by_model: dict[str, ModelUsage] = Field(default_factory=dict)  # "<provider_id>::<model_id>"
    by_actor: dict[str, ActorUsage] = Field(default_factory=dict)  # actor id -> totals


class BindingIssue(BaseModel):
    actor_id: str
    message: str


SESSION_SCHEMA_VERSION = 2
APP_ENGINE_VERSION = "v0.11"
MODERATOR_ID = "moderator"

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class GameSession(BaseModel):
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    schema_version: int = SESSION_SCHEMA_VERSION
    version: str = APP_ENGINE_VERSION
    engine_version: str = APP_ENGINE_VERSION
    title: str = "Arena-Agents"
    scenario_name: str = "v0.8"
    status: Literal["idle", "running", "paused", "finished"] = "idle"
    phase_text: str = "未开始 · 等待主持人初始化"
    active_status_text: str = "等待开局"
    active_actor_id: str | None = None
    active_action_id: str | None = None
    active_action_type: str | None = None
    execution_depth: Literal["low", "medium", "high", "xhigh"] = "medium"
    moderator_active: bool = False
    moderator_status: str = "待命 · 等待开局"
    winner_faction: str | None = None

    participants: list[Participant] = Field(default_factory=list)
    channels: list[Channel] = Field(default_factory=list)
    records: list[FeedRecord] = Field(default_factory=list)
    streaming_draft: StreamingDraft | None = None
    concurrent_streaming_drafts: dict[str, StreamingDraft] = Field(default_factory=dict)
    dashboard: DashboardState = Field(default_factory=DashboardState)
    usage_stats: UsageStats = Field(default_factory=UsageStats)
    tag_styles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    agent_notes: dict[str, dict[str, str]] = Field(default_factory=dict)
    agent_compacted_state: dict[str, dict[str, Any]] = Field(default_factory=dict)

    initial_assignments: dict[str, dict[str, str]] = Field(default_factory=dict)
    moderator_messages: list[dict[str, Any]] = Field(default_factory=list)
    scenario_snapshot: dict[str, Any] = Field(default_factory=dict)
    scenario_runtime: dict[str, Any] = Field(default_factory=dict)
    prompt_cache: dict[str, str] = Field(default_factory=dict)
    record_seq: int = 0

    # --- v0.10: per-actor model routing (ids only — never keys/URLs) -----------------
    # actor id ("moderator" or a participant id) -> ModelBinding
    model_bindings: dict[str, ModelBinding] = Field(default_factory=dict)
    # Bindings that cannot be used right now (recomputed, shown by the UI).
    binding_issues: list[BindingIssue] = Field(default_factory=list)
    # Set when the game was restored from disk / imported (and left paused).
    restored_from_save: bool = False
    restore_notice: str = ""
    # Last automatic-save failure ("" = fine); shown to the user, never swallowed silently.
    autosave_error: str = ""

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        # session_id is embedded in save-file names, so it must never be able
        # to carry path separators, dots, or other special characters.
        if not isinstance(value, str) or not SESSION_ID_PATTERN.fullmatch(value):
            raise ValueError("session_id must match ^[A-Za-z0-9_-]{1,64}$")
        return value

    def get_model_binding(self, actor_id: str) -> ModelBinding | None:
        """The binding of a participant (or of ``"moderator"``), if one is set."""
        return self.model_bindings.get(actor_id)

    def record_usage(
        self,
        actor_id: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        provider_id: str | None = None,
        model_id: str | None = None,
        input_price_per_m: float | None = None,
        output_price_per_m: float | None = None,
    ) -> float:
        """Account one LLM call; returns its cost.

        The cost uses the call's own price snapshot (``input_price_per_m`` /
        ``output_price_per_m``).  Calls without a snapshot fall back to the legacy
        session-wide prices.  History is never recomputed.
        """
        p_tok = max(0, int(prompt_tokens or 0))
        c_tok = max(0, int(completion_tokens or 0))
        stats = self.usage_stats
        in_price = stats.input_price_per_m if input_price_per_m is None else float(input_price_per_m)
        out_price = stats.output_price_per_m if output_price_per_m is None else float(output_price_per_m)
        cost = p_tok / 1_000_000.0 * in_price + c_tok / 1_000_000.0 * out_price

        stats.prompt_tokens += p_tok
        stats.completion_tokens += c_tok
        stats.total_tokens = stats.prompt_tokens + stats.completion_tokens
        stats.total_cost_usd = round(stats.total_cost_usd + cost, 10)

        actor = stats.by_actor.setdefault(actor_id, ActorUsage())
        actor.calls += 1
        actor.prompt_tokens += p_tok
        actor.completion_tokens += c_tok
        actor.cost_usd = round(actor.cost_usd + cost, 10)

        if provider_id or model_id:
            key = f"{provider_id or ''}::{model_id or ''}"
            model = stats.by_model.setdefault(
                key, ModelUsage(provider_id=provider_id or "", model_id=model_id or "")
            )
            model.calls += 1
            model.prompt_tokens += p_tok
            model.completion_tokens += c_tok
            model.cost_usd = round(model.cost_usd + cost, 10)
            model.input_price_per_m = in_price
            model.output_price_per_m = out_price
        return cost

    def record_response_usage(self, actor_id: str, resp: Any) -> float:
        """Account an ``LLMResponse`` (reads its provider/model/price snapshot fields)."""
        provider_id = getattr(resp, "provider_id", None) or None
        model_id = getattr(resp, "model_id", None) or None
        # A response carries a price snapshot only when it was routed through a binding
        # (it then names its provider/model).  A snapshot of 0.0 is a legitimate free model.
        has_snapshot = bool(provider_id or model_id)
        return self.record_usage(
            actor_id,
            prompt_tokens=getattr(resp, "prompt_tokens", 0),
            completion_tokens=getattr(resp, "completion_tokens", 0),
            provider_id=provider_id,
            model_id=model_id,
            input_price_per_m=getattr(resp, "input_price_per_m", None) if has_snapshot else None,
            output_price_per_m=getattr(resp, "output_price_per_m", None) if has_snapshot else None,
        )

    def add_token_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Legacy entry point (no actor, no snapshot): priced with the legacy session prices."""
        if int(prompt_tokens or 0) > 0 or int(completion_tokens or 0) > 0:
            self.record_usage(
                "unknown", prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            )

    def write_agent_note(
        self, actor_id: str, title: str, content: str, mode: str = "overwrite"
    ) -> str:
        notes = self.agent_notes.setdefault(actor_id, {})
        key = (title or "默认笔记").strip()
        if mode == "append" and key in notes and notes[key]:
            notes[key] = f"{notes[key]}\n{content.strip()}"
        else:
            notes[key] = content.strip()
        return notes[key]

    def get_agent_notes_text(self, actor_id: str) -> str:
        notes = self.agent_notes.get(actor_id, {})
        if not notes:
            return "（当前你的私人笔记本为空，可调用 write_note 记录关键推理与信息）"
        lines: list[str] = []
        for k, v in notes.items():
            lines.append(f"#### 📌 {k}\n{v}")
        return "\n\n".join(lines)

    def get_engine_notes_text(self) -> str:
        """Return all `record_engine_note` entries so the Moderator never loses night/day settlements across sliding windows."""
        engine_notes = [
            f"- [{r.timestamp}] {r.content}"
            for r in self.records
            if r.stream == "engine" and r.kind == "note" and r.actor_id == "moderator"
        ]
        if not engine_notes:
            return "（暂无内部裁判结算记录）"
        return "\n".join(engine_notes[-30:])

    def compact_agent_context(
        self,
        actor_id: str,
        summary_markdown: str,
        keep_recent_count: int = 10,
    ) -> dict[str, Any]:
        """
        Save a compacted context summary for `actor_id` and mark the current
        `record_seq` cutoff so older history is replaced by this summary.
        """
        keep_n = max(4, min(25, int(keep_recent_count or 10)))
        prev = self.agent_compacted_state.get(actor_id, {})
        comp_count = int(prev.get("count", 0)) + 1
        entry = {
            "summary": summary_markdown.strip(),
            "keep_recent_count": keep_n,
            "compacted_at": now_time_str(),
            "compacted_record_seq": self.record_seq,
            "count": comp_count,
        }
        self.agent_compacted_state[actor_id] = entry

        if actor_id == "moderator" and len(self.moderator_messages) > keep_n + 2:
            start_idx = max(1, len(self.moderator_messages) - keep_n)
            while (
                start_idx < len(self.moderator_messages)
                and self.moderator_messages[start_idx].get("role") == "tool"
            ):
                start_idx += 1
            tail = list(self.moderator_messages[start_idx:])
            head = (
                [self.moderator_messages[0]]
                if self.moderator_messages
                and self.moderator_messages[0].get("role") == "user"
                else []
            )
            self.moderator_messages = head + tail

        return entry

    def get_agent_compacted_summary(self, actor_id: str) -> str:
        entry = self.agent_compacted_state.get(actor_id)
        if not entry or not entry.get("summary"):
            return ""
        return (
            f"📦 **【已压缩早期历史摘要（第 {entry.get('count', 1)} 次压缩于 {entry.get('compacted_at', '')}）】**\n"
            f"{entry['summary']}"
        )

    def next_record_id(self) -> str:
        self.record_seq += 1
        return f"rec_{self.record_seq:04d}"

    def get_participant(self, player_id: str) -> Participant | None:
        for p in self.participants:
            if p.id == player_id:
                return p
        return None

    def get_actor_display_name(self, actor_id: str) -> str:
        if actor_id == "moderator":
            return "主持人"
        p = self.get_participant(actor_id)
        return p.name if p else actor_id

    def get_channel(self, channel_id: str) -> Channel | None:
        for ch in self.channels:
            if ch.id == channel_id:
                return ch
        return None

    def get_channel_member_names(self, channel_id: str) -> list[str]:
        ch = self.get_channel(channel_id)
        if not ch:
            return []
        return [self.get_actor_display_name(m) for m in ch.members]

    def set_moderator_state(self, active: bool, status_text: str) -> None:
        self.moderator_active = active
        clean = status_text.removeprefix("主持人:").removeprefix("主持人：").strip()
        self.moderator_status = clean

    def set_participant_stage(
        self, player_id: str, generating: bool, stage_text: str = ""
    ) -> None:
        p = self.get_participant(player_id)
        if p:
            p.generating = generating
            p.active_stage = stage_text if generating else ""

    def clear_all_generating(self) -> None:
        self.active_actor_id = None
        for p in self.participants:
            p.generating = False
            p.active_stage = ""

    def set_generating(
        self,
        actor_id: str | None,
        status_text: str,
        *,
        stage_text: str = "生成中",
    ) -> None:
        self.active_actor_id = actor_id
        self.active_status_text = status_text
        if actor_id == "moderator":
            self.set_moderator_state(True, status_text)
        for p in self.participants:
            if p.id == actor_id:
                p.generating = True
                p.active_stage = stage_text
            else:
                p.generating = False
                p.active_stage = ""

    def update_streaming_draft(
        self,
        actor_id: str,
        actor_name: str,
        channel_id: str | None = None,
        thought_text: str = "",
        content_text: str = "",
        action_id: str | None = None,
        action_type: str | None = None,
    ) -> StreamingDraft:
        act_id = action_id or self.active_action_id
        act_type = action_type or self.active_action_type
        draft = StreamingDraft(
            actor_id=actor_id,
            actor_name=actor_name,
            channel_id=channel_id,
            badge="思维链",
            thought_text=thought_text,
            content_text=content_text,
            action_id=act_id,
            action_type=act_type,
        )
        self.concurrent_streaming_drafts[actor_id] = draft
        self.streaming_draft = draft
        return draft

    def clear_streaming_draft(self, actor_id: str | None = None) -> None:
        if actor_id:
            self.concurrent_streaming_drafts.pop(actor_id, None)
            if self.streaming_draft and self.streaming_draft.actor_id == actor_id:
                self.streaming_draft = next(iter(self.concurrent_streaming_drafts.values()), None)
        else:
            self.concurrent_streaming_drafts.clear()
            self.streaming_draft = None

    def add_record(
        self,
        *,
        stream: Literal["engine", "channel"],
        kind: Literal["message", "thought", "tool_call", "note"],
        actor_id: str,
        content: str = "",
        channel_id: str | None = None,
        badge: str | None = None,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_result: str | None = None,
        action_id: str | None = None,
        action_type: str | None = None,
    ) -> FeedRecord:
        visible_to = (
            self.get_channel_member_names(channel_id)
            if channel_id and stream == "channel"
            else []
        )
        visible_to_ids = (
            list(self.get_channel(channel_id).members)
            if channel_id and stream == "channel" and self.get_channel(channel_id)
            else []
        )
        # Standardize badges per v0.6 requirement:
        # Badges only distinguish "思维链" and "工具调用", no specific textual details
        norm_badge: str | None = badge
        if kind == "thought":
            norm_badge = "思维链"
        elif kind == "tool_call":
            norm_badge = "工具调用"
        elif kind == "message":
            norm_badge = None
        elif kind == "note":
            norm_badge = "内部记录"

        rec = FeedRecord(
            id=self.next_record_id(),
            timestamp=now_time_str(),
            stream=stream,
            channel_id=channel_id,
            kind=kind,
            actor_id=actor_id,
            actor_name=self.get_actor_display_name(actor_id),
            content=content,
            badge=norm_badge,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
            visible_to=visible_to,
            visible_to_ids=visible_to_ids,
            action_id=action_id or self.active_action_id,
            action_type=action_type or self.active_action_type,
        )
        self.records.append(rec)
        return rec


def create_session_from_config(
    cfg: dict[str, Any],
    *,
    randomize_identities: bool = True,
    default_bindings: dict[str, ModelBinding] | None = None,
) -> GameSession:
    """
    Create a fresh, unstarted GameSession.
    All channels, player tags, and dashboard stats start empty and are
    populated at runtime by the Moderator Agent via tools.
    """
    app_cfg = cfg.get("app", {})
    init_cfg = cfg.get("initial_state", {})
    llm_cfg = cfg.get("llm", {})
    pricing_cfg = llm_cfg.get("pricing", {})
    tag_styles = copy.deepcopy(cfg.get("tag_styles", {}))

    participants: list[Participant] = []
    for p_cfg in cfg.get("participants", []):
        participants.append(
            Participant(
                id=p_cfg["id"],
                seat=p_cfg.get("seat", p_cfg["id"].upper()),
                name=p_cfg["name"],
                persona=p_cfg.get("persona", ""),
                tags=[],
                generating=False,
                active_stage="",
            )
        )

    ver = str(app_cfg.get("version", APP_ENGINE_VERSION))
    scenario_snapshot = copy.deepcopy(cfg.get("_scenario_snapshot", {}))
    scenario_definition = scenario_snapshot.get("definition", {})
    session = GameSession(
        version=ver,
        engine_version=ver,
        title=app_cfg.get("title", "Arena-Agents"),
        scenario_name=str(scenario_definition.get("id") or ver),
        status="idle",
        phase_text=init_cfg.get("phase_text", "未开始 · 等待主持人初始化"),
        active_status_text="未开始 · 点击「开始」由主持人初始化对局",
        moderator_active=False,
        moderator_status="待命 · 等待开局",
        participants=participants,
        channels=[],
        dashboard=DashboardState(factions=[], tag_stats=[]),
        usage_stats=UsageStats(
            input_price_per_m=float(pricing_cfg.get("input_price_per_m", 0.27)),
            output_price_per_m=float(pricing_cfg.get("output_price_per_m", 1.10)),
            context_window_tokens=int(llm_cfg.get("context_window_tokens", 1_000_000)),
        ),
        execution_depth=str(llm_cfg.get("execution_depth", "medium")),
        tag_styles=tag_styles,
        scenario_snapshot=scenario_snapshot,
    )

    # A fresh game starts from the user's saved default bindings (ids only; the
    # actors that exist in this game, plus the moderator).  Missing ones stay unbound.
    from app.engine.bindings import load_default_bindings

    defaults = load_default_bindings() if default_bindings is None else default_bindings
    session.model_bindings = {
        actor_id: binding.model_copy(deep=True)
        for actor_id, binding in defaults.items()
        if actor_id == MODERATOR_ID or session.get_participant(actor_id) is not None
    }

    prepare_identity_seed(session, cfg, randomize=randomize_identities)
    return session


def prepare_identity_seed(
    session: GameSession,
    cfg: dict[str, Any],
    *,
    randomize: bool = True,
) -> dict[str, dict[str, str]]:
    """
    Generate or shuffle the candidate role distribution seed for the Moderator.
    Does NOT hardcode tags onto participant cards before the Moderator runs;
    the Moderator assigns tags at runtime via tools (or when previewing).
    """
    scenario_definition = cfg.get("_scenario_snapshot", {}).get("definition", {})
    id_setup = scenario_definition.get("identity_setup") or cfg.get("identity_setup", {})
    pool_cfg: list[dict[str, Any]] = id_setup.get("pool", [])

    expanded_pool: list[dict[str, str]] = []
    for item in pool_cfg:
        role_name = str(item["role"])
        faction_name = str(item.get("faction", ""))
        count = int(item.get("count", 1))
        for _ in range(count):
            expanded_pool.append({"role": role_name, "faction": faction_name})

    default_assignments: dict[str, dict[str, str]] = id_setup.get(
        "default_assignments", {}
    )
    assignments: dict[str, dict[str, str]] = {}
    if not randomize and default_assignments:
        assignments = copy.deepcopy(default_assignments)
    else:
        shuffled = list(expanded_pool)
        random.shuffle(shuffled)
        for idx, participant in enumerate(session.participants):
            if idx < len(shuffled):
                assignments[participant.id] = shuffled[idx]
            elif expanded_pool:
                assignments[participant.id] = copy.deepcopy(expanded_pool[-1])
            else:
                assignments[participant.id] = {"role": "", "faction": ""}

    session.initial_assignments = assignments
    return assignments
