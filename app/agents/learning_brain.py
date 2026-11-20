"""The learning brain (v0.14 H20, H21, H32, H34; docs/v0.12-contracts.md §13.6): the prompt and the tools of an *initialization* and of a
*review* - the two times a bot studies instead of playing.

A learning run is the bot working on its own memory as itself: its L0 comes first in the prompt (who it is, how it reasons), then the
task (``prompts/learning/init.md`` or ``review.md``), and the model has a few tools:

* ``read_source(source, part?)``  the contract, the official rules, the study materials, the bot's own L0 and memory;
* ``write_memory(file, content)`` one memory file, **the whole file** (a second write replaces the first), which the port checks and
  stores at once; a refusal says why and what the limit is, in Chinese, and the model rewrites;
* ``finish(summary?)``            the end.  An initialization that is finished before its three required files (``arch_notes``,
  ``digest``, ``playbook``) are written is told so once - after that it is let go, and the run fails as a whole (all or nothing);
* review only: ``read_record(query?, start?, limit?)`` (what the bot could see in the game that is over) and
  ``propose_core(section, content, reason)`` (a change of the L0 - never applied: the user decides).

Several tool calls in one answer are fine (the runner executes them one after the other).  The brain knows no game: the words
are the prompt files', the texts of the port's and of the engine's.  It teaches no way to play: the prompts only ask the bot to
write down - in its own words - what it understands, what it means to try and what it is unsure of (as hypotheses that the results
will test), and, after a game, to hold its memory against the truth.

The tools never run a model and never touch a file themselves: everything goes through the ``LearningPort`` (``app.bots.types``),
which ``app.learning`` implements over the real files / the record of the game and tests fake.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import app.config as app_config
from app.agents.composer import core_text
from app.bots.types import (
    ARCH_NOTES_CHARS,
    CORE_EXTRA_CHARS,
    CORE_LIMITS,
    DIGEST_CHARS,
    IMPRESSION_CHARS,
    MEMORY_PARTS,
    PEOPLE_MAX,
    PLAYBOOK_ENTRY_CHARS,
    PLAYBOOK_MAX,
    QUESTIONS_CHARS,
    SLUG_RE,
    LearningJob,
    LearningPort,
    WriteResult,
)
from app.runtime.ports import ToolOutcome

#: The files an initialization has to write (all or nothing); ``questions`` is optional and ``people`` is not part of it.
INIT_REQUIRED = ("arch_notes", "digest", "playbook")
INIT_FILES = ("arch_notes", "digest", "playbook", "questions")
SOURCES = ("contract", "rules", "materials", "my_core", "my_memory")
CORE_SECTION_NAMES = ("identity", "voice", "mind")
READ_RECORD_DEFAULT = 40
READ_RECORD_MAX = 100
SUMMARY_CHARS = 200

FILE_LABELS = {"arch_notes": "操作笔记", "digest": "规则要点", "playbook": "打法", "questions": "待解问题", "people": "印象"}
CORE_LABELS = {"identity": "身份", "voice": "说话方式", "mind": "分析方式"}


def file_rule(file: str) -> str:
    """What the port demands of a memory file (the limits of ``app.bots.types``), for the refusal of a write."""
    if file == "playbook":
        return (
            f"最多 {PLAYBOOK_MAX} 条，一条一行，格式 - [把握:低|支持:0|反例:0|来源:复盘|#标签] 正文，正文不超过 {PLAYBOOK_ENTRY_CHARS} 字，"
            "不符合这个格式的行会被忽略"
        )
    if file == "people":
        return f"最多 {PEOPLE_MAX} 人，一人一行，写成 - 名字：印象，每条不超过 {IMPRESSION_CHARS} 字，名字里不能有冒号"
    limits = {"arch_notes": ARCH_NOTES_CHARS, "digest": DIGEST_CHARS, "questions": QUESTIONS_CHARS}
    return f"最多 {limits.get(file, '?')} 字，自由文本"


def core_rule(section: str) -> str:
    limit = CORE_LIMITS.get(section, CORE_EXTRA_CHARS)
    return f"这个分区最多 {limit} 字，要写修改后的完整文字，不是增量"


# ---------------------------------------------------------------------------------------------------- the prompt files
_FALLBACK = {
    "init": "【初始化】读平台契约和官方规则，用你自己的话写下理解和打算：用 write_memory 写 arch_notes、digest、playbook（必需），可以再写 questions，"
    "写完调用 finish。你只有 {{seconds}} 秒。这些都是假设，以后用对局的结果检验。",
    "review": "【复盘】这局结束了，真相已经公开。对照真相看你哪些判断对了、哪些错了、各自依据了什么，据此用 write_memory 修订你的记忆，写完调用 finish。"
    "你只有 {{seconds}} 秒。",
}
_VAR_RE = re.compile(r"\{\{(\w+)\}\}")
_BLOCK_RE = re.compile(r"\{\{#(\w+)\}\}\n?(.*?)\{\{/\1\}\}\n?", re.DOTALL)


def prompt_path(kind: str) -> Path:
    """``prompts/learning/<kind>.md`` below the program directory (``app.config.BASE_DIR``)."""
    return Path(app_config.BASE_DIR) / "prompts" / "learning" / f"{kind}.md"


def load_prompt(kind: str) -> str:
    """The task text of an ``init`` / ``review`` run: the prompt file, or a short built-in text when the file is missing."""
    try:
        text = prompt_path(kind).read_text(encoding="utf-8")
    except OSError:
        text = ""
    return text if text.strip() else _FALLBACK.get(kind, "")


def fill_prompt(template: str, flags: Mapping[str, bool], values: Mapping[str, Any]) -> str:
    """The template with its ``{{#flag}}...{{/flag}}`` blocks kept or dropped and its ``{{variable}}``s filled in - in ONE pass
    each, so that a text that is put in (a scenario title) is never scanned again; a ``{{name}}`` that is not a variable stays."""
    text = _BLOCK_RE.sub(lambda m: m.group(2) if flags.get(m.group(1)) else "", template)
    text = _VAR_RE.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def _clean_line(text: str, limit: int = 80) -> str:
    return " ".join(str(text or "").split())[:limit]


def _sentence(text: str | None) -> str:
    """``text`` as it is, ending with a stop (``""`` when there is none)."""
    clean = (text or "").strip()
    return clean if not clean or clean[-1] in "。！？!?）)" else clean + "。"


def _refusal(headline: str, result: WriteResult, requirement: str, advice: str) -> str:
    """What a model is told when the port refused a write or a proposal.  The port's own sentence (the real store's already says
    what is wrong, what the limit is and that nothing was saved) comes first and the requirement of the file follows; a port that
    gave no reason gets ``headline：没有说明原因``."""
    reason = (result.message or "").strip()
    if reason and ("没有保存" in reason or headline in reason):
        return f"{_sentence(reason)}（{requirement}。）"
    return f"{headline}：{reason.rstrip('。') or '没有说明原因'}。{requirement}。{advice}。"


# ---------------------------------------------------------------------------------------------------- the brain
class LearningBrain:
    """``system_prompt`` / ``first_message`` / ``tool_schemas`` / ``execute_tool`` / ``no_tool_nudge`` and what has been written
    (``written``: the memory files that went through, ``proposed``: the L0 sections the bot proposed a change of)."""

    def __init__(self, job: LearningJob, port: LearningPort, *, template: str | None = None) -> None:
        if job.kind not in ("init", "review"):
            raise ValueError(f"学习运行的类型只能是 init 或 review，不能是 {job.kind!r}。")
        self.job = job
        self.port = port
        self.kind = job.kind
        self.files: tuple[str, ...] = INIT_FILES if job.kind == "init" else tuple(MEMORY_PARTS)
        self._written: list[str] = []
        self._proposed: list[str] = []
        self._summary = ""
        self._finished = False
        self._reminded = False
        self._nudged = False
        self._system = self._build_system(template)
        self._first = self._build_first()
        self._tools = self._build_tools()
        self._handlers = {
            "read_source": self._read_source,
            "write_memory": self._write_memory,
            "finish": self._finish,
            **({"read_record": self._read_record, "propose_core": self._propose_core} if job.kind == "review" else {}),
        }

    # ------------------------------------------------------------------------------------------------ prompts
    @property
    def title(self) -> str:
        return _clean_line(self.job.scenario_title, 60) or self.job.scenario_id

    def _build_system(self, template: str | None) -> str:
        job = self.job
        limits = {
            "scenario": self.title,
            "name": job.mind.name,
            "seconds": job.deadline_s,
            "arch_notes_chars": ARCH_NOTES_CHARS,
            "digest_chars": DIGEST_CHARS,
            "questions_chars": QUESTIONS_CHARS,
            "playbook_max": PLAYBOOK_MAX,
            "entry_chars": PLAYBOOK_ENTRY_CHARS,
            "people_max": PEOPLE_MAX,
            "impression_chars": IMPRESSION_CHARS,
            "identity_chars": CORE_LIMITS["identity"],
            "voice_chars": CORE_LIMITS["voice"],
            "mind_chars": CORE_LIMITS["mind"],
            "extra_chars": CORE_EXTRA_CHARS,
        }
        flags = {"human": bool(job.human_note.strip()), "style": bool(job.learn_style)}
        task = fill_prompt(template if template is not None else load_prompt(self.kind), flags, limits)
        head = core_text(job.mind, job.seat_id if self.kind == "review" else "")
        return (head + "\n\n" + task) if head else task

    def system_prompt(self) -> str:
        return self._system

    def _memory_status(self) -> str:
        mind = self.job.mind
        have: list[str] = []
        if mind.arch_notes.strip():
            have.append(f"操作笔记 {len(mind.arch_notes.strip())} 字")
        if mind.rules.digest.strip():
            have.append(f"规则要点 {len(mind.rules.digest.strip())} 字")
        if mind.rules.playbook:
            have.append(f"打法 {len(mind.rules.playbook)} 条")
        if mind.rules.questions.strip():
            have.append(f"待解问题 {len(mind.rules.questions.strip())} 字")
        if mind.people and self.kind == "review":  # (an initialization does not touch the impressions)
            have.append(f"印象 {len(mind.people)} 人")
        if not have:
            return "你现在还没有任何记忆。"
        tail = "这次是修订，请先用 read_source 读出来再改。" if self.kind == "init" else "改之前请先用 read_source 读出来。"
        return "你现在已经有这些记忆：" + "、".join(have) + "。" + tail

    def _build_first(self) -> str:
        job = self.job
        if self.kind == "init":
            return f"现在开始初始化《{self.title}》，你有 {job.deadline_s} 秒。{self._memory_status()}"
        parts = [f"这局《{self.title}》结束了，你有 {job.deadline_s} 秒复盘。"]
        if job.outcome_text.strip():
            parts.append(f"【结果】{job.outcome_text.strip()}")
        if job.role_text.strip():
            parts.append(f"【你的身份】{job.role_text.strip()}")
        if job.others_text.strip():
            parts.append("【大家的身份与结果】\n" + job.others_text.strip())
        jots = [line.strip() for line in job.jots if str(line).strip()]
        if jots:
            parts.append("【你在对局中记下的线索】\n" + "\n".join(jots))
        if job.human_note.strip():
            parts.append("【有人代你出场】\n" + job.human_note.strip())
        parts.append(self._memory_status() + "先用 read_record 看看这一局你能看到的记录，再对照上面的真相修订记忆。")
        return "\n\n".join(parts)

    def first_message(self) -> str:
        return self._first

    # ------------------------------------------------------------------------------------------------ tools
    def _build_tools(self) -> list[dict[str, Any]]:
        def tool(name: str, description: str, properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
            parameters: dict[str, Any] = {"type": "object", "properties": properties}
            if required:
                parameters["required"] = list(required)
            return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}

        string = {"type": "string"}
        files = list(self.files)
        tools = [
            tool(
                "read_source",
                "读一份资料。source：contract（平台契约，part 填 must_read 或 reference）、rules（官方规则）、materials（学习材料：不带 part 列出有哪些，"
                "带名字读其中一份）、my_core（你的底层设定）、my_memory（你现在的记忆，part 可填 " + "、".join(MEMORY_PARTS) + "，不填读全部）。",
                {
                    "source": {**string, "enum": list(SOURCES), "description": "要读的资料"},
                    "part": {**string, "description": "可选：契约的哪一部分 / 哪份学习材料 / 哪个记忆文件"},
                },
                ("source",),
            ),
            tool(
                "write_memory",
                "把一个记忆文件整份写进去（同一个文件再写一次就是整份覆盖，所以要写完整的内容，不是增量）。"
                + ("写进去之后立即生效。" if self.kind == "review" else "初始化结束时统一检查，全部通过才会生效。")
                + "写入被拒绝时，结果里会写明原因和限制，改好后重写。",
                {
                    "file": {**string, "enum": files, "description": "要写的文件：" + "、".join(f"{f}（{FILE_LABELS[f]}）" for f in files)},
                    "content": {**string, "description": "这个文件的完整内容"},
                },
                ("file", "content"),
            ),
            tool(
                "finish",
                "表示你写完了，结束这次" + ("初始化" if self.kind == "init" else "复盘") + "。summary 可选，一两句话说说这次写了什么。",
                {"summary": {**string, "description": "可选：一两句话的小结"}},
            ),
        ]
        if self.kind == "review":
            tools += [
                tool(
                    "read_record",
                    f"读这一局你能看到的记录（按顺序）。query 可选：只看含这个关键词的；start 可选：从第几条记录开始往后翻；"
                    f"limit 可选：这次最多读几条（默认 {READ_RECORD_DEFAULT}，最多 {READ_RECORD_MAX}）。",
                    {
                        "query": {**string, "description": "可选：关键词"},
                        "start": {"type": "integer", "description": "可选：从第几条记录开始"},
                        "limit": {"type": "integer", "description": f"可选：最多几条，默认 {READ_RECORD_DEFAULT}"},
                    },
                ),
                tool(
                    "propose_core",
                    "对你的底层设定提出修改建议。底层设定只有使用者能改：这条建议不会自动生效，要使用者确认。content 是这个分区修改后的完整文字（不是增量）。",
                    {
                        "section": {**string, "description": "identity、voice、mind，或 extra:<附加分区的标识>"},
                        "content": {**string, "description": "这个分区修改后的完整文字"},
                        "reason": {**string, "description": "为什么要这样改"},
                    },
                    ("section", "content", "reason"),
                ),
            ]
        return tools

    def tool_schemas(self) -> list[dict[str, Any]]:
        return self._tools

    def execute_tool(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Run one tool call; every answer is a short Chinese text for the model.  Never raises for what a model can send."""
        if not isinstance(args, dict):
            return ToolOutcome(text=f"工具 {name} 的参数必须是 JSON 对象。")
        handler = self._handlers.get(str(name))
        if handler is None:
            return ToolOutcome(text=f"未知工具：{name}。可用的工具：{'、'.join(self._handlers)}。")
        try:
            return handler(args)
        except Exception as exc:  # noqa: BLE001 - a port that fails must not end the run: the model is told, and goes on
            return ToolOutcome(text=f"工具 {name} 没有成功：{type(exc).__name__}：{exc}")

    # ------------------------------------------------------------------------------------------------ read_source
    def _read_source(self, args: dict[str, Any]) -> ToolOutcome:
        source = str(args.get("source") or "").strip()
        raw = args.get("part")
        part = None if raw is None or not str(raw).strip() else str(raw).strip()
        if source not in SOURCES:
            return ToolOutcome(text=f"source 必须是：{'、'.join(SOURCES)}。")
        if source == "contract" and part not in (None, "must_read", "reference"):
            return ToolOutcome(text="读契约时 part 只能是 must_read（必读）或 reference（参考），不填就读全部。")
        if source == "my_memory" and part not in (None, *MEMORY_PARTS):
            return ToolOutcome(text=f"读记忆时 part 只能是：{'、'.join(MEMORY_PARTS)}，不填读全部。")
        text = self.port.read_source(source, part)
        if source == "contract":
            label = {"must_read": "平台契约·必读", "reference": "平台契约·参考"}.get(part or "", "平台契约")
        elif source == "materials":
            label = "学习材料列表" if part is None else f"学习材料·{part}"
        elif source == "my_memory":
            label = f"我的记忆·{FILE_LABELS[part]}" if part else "我的记忆"
        else:
            label = {"rules": "官方规则", "my_core": "我的底层设定"}[source]
        return ToolOutcome(text=f"【{label}】\n" + ((text or "").strip() or "（这一项是空的。）"))

    # ------------------------------------------------------------------------------------------------ write_memory
    def _missing_required(self) -> list[str]:
        return [file for file in INIT_REQUIRED if file not in self._written] if self.kind == "init" else []

    def _write_memory(self, args: dict[str, Any]) -> ToolOutcome:
        file = str(args.get("file") or "").strip()
        content = args.get("content")
        if file not in self.files:
            extra = "初始化时不写印象（people），印象在复盘时写。" if file == "people" and self.kind == "init" else ""
            return ToolOutcome(text=f"file 必须是：{'、'.join(self.files)}。{extra}")
        if not isinstance(content, str) or not content.strip():
            return ToolOutcome(text=f"没有写入 {file}：content 不能是空的。要写这个文件的完整内容，不是增量。")
        result: WriteResult = self.port.write_memory(file, content)
        if not result.ok:
            return ToolOutcome(text=_refusal(f"没有写入 {file}", result, f"{file} 的要求：{file_rule(file)}", "请改好后整份重写一次"))
        if file not in self._written:
            self._written.append(file)
        chars = result.chars or len(content.strip())
        # (the port's own sentence says what happened - "已写入打法（5 条，300 字）。有 2 行没有读出，已忽略。" - ours is the fallback)
        text = _sentence(result.message) or f"已写入 {file}（{FILE_LABELS[file]}，{chars} 字）。"
        if self.kind == "review":
            return ToolOutcome(text=text + "已经生效。")
        missing = self._missing_required()
        tail = f"还没写的必需文件：{'、'.join(missing)}。" if missing else "三个必需的文件都写好了，可以再检查一遍，或者调用 finish 结束。"
        return ToolOutcome(text=text + tail)

    # ------------------------------------------------------------------------------------------------ finish
    def _finish(self, args: dict[str, Any]) -> ToolOutcome:
        missing = self._missing_required()
        if missing and not self._reminded:
            self._reminded = True
            return ToolOutcome(
                text=(
                    f"先别结束：这几个必需的文件还没有写进去：{'、'.join(missing)}。写完再调用 finish。"
                    "如果你确实写不出来，再调用一次 finish，这次初始化会因为缺文件而作废，你原来的记忆不会有任何改动。"
                )
            )
        self._finished = True
        self._summary = _clean_line(args.get("summary"), SUMMARY_CHARS)
        return ToolOutcome(text="已结束。", ends_run=True)

    # ------------------------------------------------------------------------------------------------ read_record
    @staticmethod
    def _number(value: Any, default: int | None, low: int, high: int) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, number))

    def _read_record(self, args: dict[str, Any]) -> ToolOutcome:
        raw = args.get("query")
        query = None if raw is None or not str(raw).strip() else str(raw).strip()
        start = self._number(args.get("start"), None, 0, 10**9)
        limit = self._number(args.get("limit"), READ_RECORD_DEFAULT, 1, READ_RECORD_MAX)
        text = self.port.read_record(query, start, limit or READ_RECORD_DEFAULT)
        return ToolOutcome(text=(text or "").strip() or "（没有符合的记录。）")

    # ------------------------------------------------------------------------------------------------ propose_core
    def _propose_core(self, args: dict[str, Any]) -> ToolOutcome:
        section = str(args.get("section") or "").strip()
        content, reason = args.get("content"), args.get("reason")
        slug = section.removeprefix("extra:") if section.startswith("extra:") else ""
        if section not in CORE_SECTION_NAMES and not (section.startswith("extra:") and SLUG_RE.match(slug)):
            return ToolOutcome(text="section 必须是 identity、voice、mind，或者 extra:<附加分区的标识>（标识由小写字母、数字和连字符组成）。")
        if not isinstance(content, str) or not content.strip():
            return ToolOutcome(text="content 要写这个分区修改后的完整文字，不能是空的。")
        if not isinstance(reason, str) or not reason.strip():
            return ToolOutcome(text="reason 要说明为什么这样改，不能是空的。")
        result: WriteResult = self.port.propose_core(section, content, reason)
        if not result.ok:
            return ToolOutcome(text=_refusal("没有提交这条建议", result, core_rule(section), "请改好后再提一次"))
        if section not in self._proposed:
            self._proposed.append(section)
        label = CORE_LABELS.get(section, f"附加分区 {slug}")
        return ToolOutcome(text=f"已提交对「{label}」的修改建议。它不会自动生效，要使用者确认后才会改。")

    # ------------------------------------------------------------------------------------------------ the end of a run
    def no_tool_nudge(self, content: str, round_idx: int) -> str | None:
        """The answer had no tool call: say once what to do (what is only in the answer is not kept); the second time, nothing -
        the run ends."""
        if self._nudged or self._finished:
            return None
        self._nudged = True
        if self.kind == "init":
            missing = self._missing_required()
            todo = f"还没写的必需文件：{'、'.join(missing)}。" if missing else ""
            return (
                "（系统提示）你的回答里没有调用任何工具，只写在回答里的话不会被保存。要保存的内容请用 write_memory 写进文件，"
                f"{todo}全部写完后调用 finish。"
            )
        return (
            "（系统提示）你的回答里没有调用任何工具，只写在回答里的话不会被保存。要修订的记忆请用 write_memory 写进文件，"
            "想对底层设定提建议用 propose_core；没有要改的，调用 finish 结束。"
        )

    @property
    def written(self) -> tuple[str, ...]:
        """The memory files that went through, in the order they first did (the progress of the run shows them)."""
        return tuple(self._written)

    @property
    def proposed(self) -> tuple[str, ...]:
        """The L0 sections the bot proposed a change of (review only)."""
        return tuple(self._proposed)

    @property
    def finished(self) -> bool:
        """The model called ``finish`` (and was let go)."""
        return self._finished

    @property
    def summary(self) -> str:
        """The one or two sentences the model gave with ``finish`` (``""`` when it gave none)."""
        return self._summary

    @property
    def missing(self) -> tuple[str, ...]:
        """The required files an initialization has not written (always empty for a review)."""
        return tuple(self._missing_required())

    def describe(self) -> str:
        """A line for the logs: what kind of run and what it has done (no text of the memory)."""
        return json.dumps(
            {"kind": self.kind, "written": self.written, "proposed": self.proposed, "finished": self.finished}, ensure_ascii=False
        )


def build_learning_brain(job: LearningJob, port: LearningPort) -> LearningBrain:
    """The brain of one learning run (``job.kind`` ``init`` or ``review``) over ``port`` (contract §13.6)."""
    return LearningBrain(job, port)


__all__ = [
    "CORE_LABELS",
    "FILE_LABELS",
    "INIT_FILES",
    "INIT_REQUIRED",
    "READ_RECORD_DEFAULT",
    "READ_RECORD_MAX",
    "SOURCES",
    "LearningBrain",
    "build_learning_brain",
    "core_rule",
    "file_rule",
    "fill_prompt",
    "load_prompt",
    "prompt_path",
]
