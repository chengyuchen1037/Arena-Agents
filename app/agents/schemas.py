"""LLM tool schemas, derived from the intent payload models (``app.world.intents``).

Tool name = intent type without the ``mod.`` prefix; the only exception is the player tool
``challenge_moderator`` (intent ``challenge``).  Parameter names are the payload field names.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

from app.world.intents import MODERATOR_INTENTS, PLAYER_INTENTS
from app.world.spec import ParamSpec, ScenarioSpec

PLAYER_TOOL_INTENTS: dict[str, str] = {
    "speak": "speak",
    "speak_template": "speak_template",
    "pass": "pass",
    "respond": "respond",
    "use_ability": "use_ability",
    "challenge_moderator": "challenge",
}
MODERATOR_TOOL_INTENTS: dict[str, str] = {
    intent_type.removeprefix("mod."): intent_type for intent_type in MODERATOR_INTENTS
}

WAIT_TOOL = "wait"
SHARED_TOOL_NAMES = ("list_documents", "read_document", "write_note", "read_notes", "compact_context")

TOOL_HELP: dict[str, str] = {
    # ---- player ---------------------------------------------------------------
    "speak": "在指定频道发言。只有你在该频道拥有发言权时才会成功（状态板会告诉你现在能不能写）。"
    "正文有字数限制，超出部分会被截掉，请把关键结论放在前面。",
    "speak_template": "用剧本提供的结构化模板在频道里发言（例如公开表态）。同样受发言权限制。",
    "pass": "放弃你在该频道当前的发言权（轮到你但不想说时使用），让流程继续。",
    "respond": "回应主持人发给你的请求（投票、选择、回答问题）。选择题的 value 必须是选项 id；"
    "文字题的 value 是你的回答。截止前可以改答，以最后一次为准。",
    "use_ability": "发动你拥有的能力。可用的能力、参数与限制见本工具说明和状态板。",
    "challenge_moderator": "向主持人私下提出质询（例如你认为主持人执行规则有误）。请写明依据；不要滥用，"
    "反复骚扰的玩家可能被主持人关闭私聊。",
    # ---- moderator ------------------------------------------------------------
    "announce": "用剧本模板发布公告（敏感信息——出局、投票结果、查验结果、终局等——只能用模板）。"
    "你可以在任何频道发布，不受发言权限制。",
    "send_message": "在频道里发送自由文本。不要用它发布敏感信息（请用 announce 模板）。",
    "create_channel": "创建频道。你自动是成员；频道初始模式默认 closed（只有你能写）。",
    "update_channel": "修改频道：改名、增减成员、设置只读成员、改模式（closed / free / listen）、调整自由讨论限额。"
    "把玩家的系统私聊设为 closed 可以暂时禁止他向你私聊或质询（之后可再设回 free）。",
    "open_floor": "开放发言权。mode=turn：holders 里的每位玩家各得一个令牌（可设条数/字数/时限）；"
    "mode=free：开启自由讨论（引擎只会在时间/字数用尽或冷场时通知你，不会自动关闭，由你询问后用 close_floor 收尾）。",
    "set_floor_queue": "按 order 给出顺序，引擎自动依次把发言权交给每个人（轮流发言），整轮结束后唤醒你。"
    "每人一个令牌，限额相同。",
    "pause_queue": "暂停发言队列（冻结当前发言者的剩余时间）。",
    "resume_queue": "恢复发言队列（也会解除被能力触发的冻结）。",
    "close_floor": "收回频道的发言权/结束自由讨论，频道变为 closed。",
    "open_request": "向玩家发起请求（投票/选择/文字回答）。立即返回 request_id，不会阻塞你；全员作答或到期后你会被唤醒。"
    "sealed=true 时截止前谁也看不到答案；default 会应用到未作答者。",
    "cancel_request": "取消一个进行中的请求。",
    "reopen_request": "重新发起一个已关闭或已取消的请求（新 id）。只能重开，不能修改已发生的结果。",
    "update_player": "修改玩家的标签与属性（出局、复活、授予徽章等）。这是对当前状态的修改，历史记录不会被改写。",
    "set_phase": "更新顶栏的阶段文字。",
    "update_dashboard": "更新右侧的对局状态面板（阵营存活统计、标签统计）。",
    "random": "产生可审计、可复现的随机结果：int（闭区间整数）、choice（从 items 里不重复抽 k 个）、shuffle（洗牌）。",
    "evaluate": "执行剧本里的检查项（例如计票、胜负判定）。结果只是辅助，最终裁决由你做出。",
    "get_ledger": "读取你可见的历史条目（可按频道、起始序号、类型过滤）。",
    "resolve_challenge": "处理玩家的质询：verdict=upheld（成立）或 rejected（驳回），并给出回复。"
    "成立时请用公告更正、update_player 或 reopen_request 纠正，必要时 hold_game。",
    "hold_game": "暂停对局，等待人工处理（用于无法自行解决的严重问题）。",
    "end_game": "宣布对局结束并给出胜者与总结。结束后所有 agent 停止；用户可以点“继续”恢复。",
    "enable_ability": "启用某个能力（可限定持有者）。能力默认关闭。",
    "disable_ability": "关闭某个能力。",
    # ---- generic -------------------------------------------------------------
    "wait": "什么也不做，结束本次行动，等待下一次被唤醒。没有事情需要处理时调用它。",
    "list_documents": "列出可查阅的参考文档。",
    "read_document": "读取一份参考文档的全文。",
    "write_note": "写入你的私人笔记（只有你自己可见）。你的思考不会被保留，需要跨次记住的要点请写在这里。",
    "read_notes": "读取你的全部私人笔记。",
    "compact_context": "当可见历史过长时，把早期历史压缩成一份摘要（摘要会替代被压缩的原始记录）。",
}

FIELD_HELP: dict[str, str] = {
    "channel_id": "频道 id",
    "content": "正文",
    "template_id": "模板 id",
    "params": "模板/能力的参数对象",
    "request_id": "请求 id",
    "value": "选项 id（选择题）或文字回答",
    "reason": "理由（可选）",
    "ability_id": "能力 id",
    "ref_seq": "所指事件的序号（可选）",
    "name": "名称",
    "members": "成员 id 列表（主持人自动加入）",
    "add_members": "要加入的成员 id",
    "remove_members": "要移出的成员 id（不能移出主持人）",
    "readonly_members": "只读成员 id 列表（整体替换）",
    "mode": "模式",
    "holders": "发言者 id 列表",
    "order": "发言顺序（玩家 id 列表）",
    "limits": "令牌限额（条数/单条字数/总字数/时限秒数，缺省取剧本默认）",
    "free_limits": "自由讨论限额（缺省取剧本默认）",
    "to": "目标玩家 id 列表",
    "kind": "类型",
    "question": "问题",
    "options": "选项列表，每项 {id, label}",
    "duration_s": "时限（秒）",
    "sealed": "是否密封（截止前不可见）",
    "default": "未作答者的默认答案",
    "player_id": "玩家 id",
    "add_tags": "要添加的标签",
    "remove_tags": "要移除的标签",
    "set_attributes": "要合并的属性",
    "text": "文字",
    "factions": "阵营统计列表",
    "tag_stats": "标签统计列表",
    "lo": "下界（含）",
    "hi": "上界（含）",
    "items": "候选项",
    "k": "抽取个数",
    "check_id": "检查项 id",
    "after_seq": "只返回此序号之后的条目",
    "limit": "最多返回条数",
    "types": "条目类型过滤",
    "ticket_id": "质询工单 id",
    "verdict": "裁决：upheld（成立）/ rejected（驳回）",
    "reply": "给玩家的回复",
    "winner": "胜者",
    "summary": "总结",
    "summary_markdown": "对早期历史的 Markdown 摘要",
    "keep_recent_count": "保留的最近条数（4–25，默认 10）",
    "doc_name": "文档文件名",
    "title": "笔记标题",
    "note_mode": "overwrite（覆盖同名，默认）或 append（追加）",
}


def clean_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Provider-friendly JSON schema of ``model``: inline ``$defs``, no titles, ``anyOf`` with null
    collapsed, no ``additionalProperties`` / ``default: null`` noise."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [resolve(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = defs.get(str(node["$ref"]).rsplit("/", 1)[-1], {})
            # A model's docstring (English, developer-facing) must not leak into the tool schema.
            target = {k: v for k, v in copy.deepcopy(target).items() if k != "description"}
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return resolve(merged)
        if "anyOf" in node:
            options = [o for o in node["anyOf"] if not (isinstance(o, dict) and o.get("type") == "null")]
            rest = {k: v for k, v in node.items() if k != "anyOf"}
            if len(options) == 1:
                return resolve({**options[0], **rest})
            return resolve({**rest, "anyOf": options})
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("title", "additionalProperties"):
                continue
            if key == "default" and value is None:
                continue
            out[key] = resolve(value)
        return out

    schema = resolve(raw)
    for name, prop in (schema.get("properties") or {}).items():
        if isinstance(prop, dict) and "description" not in prop and name in FIELD_HELP:
            prop["description"] = FIELD_HELP[name]
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def describe_param(spec: ParamSpec) -> str:
    base = {
        "player_id": "玩家 id",
        "player_ids": "玩家 id 列表",
        "int": "整数" + (f"（{spec.min}~{spec.max}）" if spec.min is not None or spec.max is not None else ""),
        "text": f"文字（≤{spec.max_chars} 字）",
        "enum": "取值：" + " / ".join(spec.values),
    }[spec.type]
    return base + ("，可选" if spec.optional else "")


def describe_params(specs: dict[str, ParamSpec]) -> str:
    return "；".join(f"{name}：{describe_param(spec)}" for name, spec in specs.items()) or "无参数"


def player_templates(spec: ScenarioSpec) -> dict[str, Any]:
    return {tid: t for tid, t in spec.templates.items() if t.users in ("players", "any")}


def moderator_templates(spec: ScenarioSpec) -> dict[str, Any]:
    return {tid: t for tid, t in spec.templates.items() if t.users in ("moderator", "any")}


def _tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def shared_tool_schemas() -> list[dict[str, Any]]:
    string = {"type": "string"}
    return [
        _tool("list_documents", TOOL_HELP["list_documents"], _object({})),
        _tool(
            "read_document",
            TOOL_HELP["read_document"],
            _object({"doc_name": {**string, "description": FIELD_HELP["doc_name"]}}, ["doc_name"]),
        ),
        _tool(
            "write_note",
            TOOL_HELP["write_note"],
            _object(
                {
                    "title": {**string, "description": FIELD_HELP["title"]},
                    "content": {**string, "description": "笔记正文"},
                    "mode": {"type": "string", "enum": ["overwrite", "append"], "description": FIELD_HELP["note_mode"]},
                },
                ["title", "content"],
            ),
        ),
        _tool("read_notes", TOOL_HELP["read_notes"], _object({})),
        _tool(
            "compact_context",
            TOOL_HELP["compact_context"],
            _object(
                {
                    "summary_markdown": {**string, "description": FIELD_HELP["summary_markdown"]},
                    "keep_recent_count": {"type": "integer", "description": FIELD_HELP["keep_recent_count"]},
                },
                ["summary_markdown"],
            ),
        ),
    ]


def _intent_tool(tool_name: str, model: type[BaseModel], extra_description: str = "") -> dict[str, Any]:
    description = TOOL_HELP[tool_name] + (("\n" + extra_description) if extra_description else "")
    return _tool(tool_name, description, clean_schema(model))


def player_tool_schemas(spec: ScenarioSpec) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    templates = player_templates(spec)
    for tool_name, intent_type in PLAYER_TOOL_INTENTS.items():
        model = PLAYER_INTENTS[intent_type]
        extra = ""
        if tool_name == "speak_template":
            if not templates:
                continue
            extra = "可用模板：" + "；".join(
                f"{tid}（{describe_params(t.params)}）：{t.text}" for tid, t in templates.items()
            )
        elif tool_name == "use_ability":
            if not spec.abilities:
                continue
            extra = "能力：" + "；".join(
                f"{aid}（{describe_params(a.params)}）{('：' + a.description) if a.description else ''}"
                for aid, a in spec.abilities.items()
            )
        tools.append(_intent_tool(tool_name, model, extra))
    tools.append(_tool(WAIT_TOOL, TOOL_HELP[WAIT_TOOL], _object({})))
    tools.extend(shared_tool_schemas())
    return tools


def moderator_tool_schemas(spec: ScenarioSpec) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    templates = moderator_templates(spec)
    for tool_name, intent_type in MODERATOR_TOOL_INTENTS.items():
        model = MODERATOR_INTENTS[intent_type]
        extra = ""
        if tool_name == "announce":
            extra = "可用模板：" + (
                "；".join(
                    f"{tid}{'【敏感】' if t.sensitive else ''}（{describe_params(t.params)}）：{t.text}"
                    for tid, t in templates.items()
                )
                or "（剧本未定义模板）"
            )
        elif tool_name == "evaluate":
            extra = "可用检查项：" + (", ".join(spec.checks) or "（无）")
        elif tool_name in ("enable_ability", "disable_ability"):
            extra = "能力：" + (", ".join(spec.abilities) or "（无）")
        tools.append(_intent_tool(tool_name, model, extra))
    tools.append(_tool(WAIT_TOOL, TOOL_HELP[WAIT_TOOL], _object({})))
    tools.extend(shared_tool_schemas())
    return tools
