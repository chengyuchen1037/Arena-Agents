"""The bots a benchmark run tries: the v0.13.1 baseline (``LegacyBrain``) and v0.14 bots (``BotBrain``), made from the default library or
from a bot directory, optionally with a layer taken away (the M1 ablations).

A bot is named by a short spec string (the ``--bots`` argument, the ``label`` of every result row)::

    legacy                 the v0.13.1 player: ``prompts/participant.md``, no persona, no memory (what a fresh v0.13.1 install played with)
    legacy:Alice           the same, the seat is called Alice
    bot:Alice              the default library's Alice (bots/Alice/): L0 only - the library holds no initialization output yet
    bot:path/to/dir        a bot directory (core/, arch/notes.md, rules/<scenario>/..., people.md): its memory is mounted too
    bot:Alice@nomem        ... with the memory switched off (L0 + contract + rules: ``memory_enabled=False``)
    bot:Alice@no-playbook,no-people    ... without those parts (also: no-notes, no-digest, no-questions, no-record, blank-core)
    default12 / legacy12   the twelve default names as ``bot:`` / ``legacy:`` bots (the S2 line-up)

``BrainFactory = Callable[[str, World, ScenarioSpec], Brain]`` (contract §13.11): ``factory(seat_id, world, spec)`` gives the brain of the
seat under test.  Everything the factory needs (the L0, the memory snapshot, the contract) is read when the bot is made, so a run is
repeatable and nothing is read in the middle of a game.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import app.config as app_config
from app.agents.contract import load_default_contract, parse_contract
from app.agents.gates import allows_oral_lists
from app.agents.legacy import LegacyBrain
from app.agents.mind import BotBrain
from app.bots.types import BotMind, ContractDoc, CoreFiles, RulesMemory
from app.storage import botfiles
from app.world.spec import ScenarioSpec
from app.world.world import World
from bench.situation import DEFAULT_NAMES

BrainFactory = Callable[[str, World, ScenarioSpec], Any]

#: The layers a bot spec may take away (``@no-...``); ``nomem`` switches the whole memory off, ``blank-core`` replaces the L0.
ABLATIONS = ("nomem", "no-notes", "no-digest", "no-playbook", "no-questions", "no-people", "no-record", "blank-core")
#: ``@history=6000``: the bot sees at most this many characters of the visible history (the default is 12000).  The M2 suite narrows it, the way a long game does:
#: what happened early has scrolled out, and only what the bot kept (the hard-fact table, its notes) is left.  Works for ``legacy`` too.
HISTORY_OPTION = re.compile(r"^history=(\d{3,6})$")


def history_chars_of(options: Iterable[str]) -> int | None:
    """The ``history=N`` of a spec's options (``None`` = the brain's own default)."""
    for option in options:
        match = HISTORY_OPTION.match(option)
        if match:
            return int(match.group(1))
    return None
#: The L0 of a ``blank-core`` bot: nobody in particular (the M1 control for "the L0 does something").
NEUTRAL_CORE = CoreFiles(
    identity="一名普通的玩家。",
    voice="你像平时聊天那样说话：自然、直接。",
    mind="你根据眼前的局面做出自己的判断。",
)
_GROUPS = {"default12": "bot", "legacy12": "legacy"}


class BotSpecError(ValueError):
    """A bot spec that cannot be used (unknown bot, an L0 that is not complete, an unknown option)."""


@dataclass(frozen=True)
class BenchBot:
    """One participant of a benchmark run."""

    label: str  # the spec string: the name of the bot in the results
    kind: str  # legacy | bot
    name: str  # the name the seat gets ("" = the script's own name for the seat)
    brain: str  # LegacyBrain | BotBrain
    factory: BrainFactory = field(repr=False, compare=False)
    mind: BotMind | None = None
    memory_enabled: bool = True
    options: tuple[str, ...] = ()

    @property
    def voice(self) -> str:
        return self.mind.core.voice if self.mind is not None else ""

    @property
    def oral_lists(self) -> bool:
        """The voice allows spoken enumerations (the output gate's ``list`` rule is off for this bot)."""
        return allows_oral_lists(self.voice)

    @property
    def card(self) -> str:
        """The style card a judge matches speeches against (S2): who the bot is and how it talks - its own L0 text."""
        if self.mind is None:
            return f"{self.name or self.label}（v0.13.1 的默认玩家：没有设定性格，说话方式由模型自己决定）"
        core = self.mind.core
        return f"{self.mind.name}\n身份：{core.identity.strip()}\n说话方式：{core.voice.strip()}"

    @property
    def style_card(self) -> str:
        """What a judge matches a speech against in S2: three lines of the bot's own ``voice`` (how it talks, how it sounds, how long) - no name, nothing
        else of the bot.  Empty for a bot without a voice (``legacy`` has none: there is no style to recognise)."""
        if self.mind is None:
            return ""
        lines = [line.strip() for line in self.mind.core.voice.strip().splitlines() if line.strip()]
        if len(lines) > 3:
            band = next((line for line in lines if line.startswith(("篇幅：", "篇幅:"))), lines[2])
            lines = [*lines[:2], band]
        text = "\n".join(lines)
        for name in sorted({self.mind.name, self.name} - {""}, key=len, reverse=True):
            text = text.replace(name, "我")  # (a voice that names itself must not name the bot on a card)
        return text

    def make_brain(self, seat: str, world: World, spec: ScenarioSpec) -> Any:
        return self.factory(seat, world, spec)

    def describe(self) -> dict[str, Any]:
        """What ``run.json`` records about the bot (no text of its memory)."""
        info: dict[str, Any] = {
            "label": self.label, "kind": self.kind, "name": self.name, "brain": self.brain,
            "memory_enabled": self.memory_enabled, "options": list(self.options),
        }
        if self.mind is not None:
            info["core_sha"] = hashlib.sha256(json.dumps(self.mind.core.to_dict(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
            info["playbook_entries"] = len(self.mind.rules.playbook)
            info["people"] = len(self.mind.people)
        return info


# ---------------------------------------------------------------------------------------------------------------------- the library
def library_dir() -> Path:
    """``bots/`` below the program directory: the default library (``app.config.BASE_DIR`` is the repository, or a test's copy)."""
    return Path(app_config.BASE_DIR) / "bots"


def default_names(library: Path | None = None) -> list[str]:
    """The default bots of the library, in the order of ``DEFAULT_NAMES``."""
    root = library or library_dir()
    return [name for name in DEFAULT_NAMES if (root / name / "core").is_dir()]


def _find_bot_dir(ref: str, library: Path | None) -> Path:
    candidate = Path(ref)
    if candidate.is_dir() and (candidate / "core").is_dir():
        return candidate
    root = library or library_dir()
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and child.name.casefold() == ref.casefold() and (child / "core").is_dir():
                return child
    raise BotSpecError(f"找不到 bot {ref!r}（既不是 {root} 里的默认 bot，也不是一个带 core/ 的目录）。")


def _bot_id(name: str) -> str:
    """A stable id from the name (the runner needs no store: the same name is the same bot in every run)."""
    return "b-" + hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()[:8]


def load_bot_mind(ref: str, *, scenario_id: str = "", memory: bool = True, library: Path | None = None) -> BotMind:
    """The snapshot a game would start with, read from a bot directory (``BotStore.load_mind`` without the store: pure file reading): L0 and - when
    ``memory`` - the operating notes, the digest / playbook / questions of ``scenario_id``, the impressions and the record."""
    directory = _find_bot_dir(ref, library)
    meta: dict[str, Any] = {}
    meta_file = directory / "bot.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    name = str(meta.get("name") or directory.name)
    core = botfiles.read_core(directory / "core")
    if not core.complete:
        raise BotSpecError(f"bot {name} 的底层设定不完整（缺少：{'、'.join(core.missing)}）。")
    bot_id = _bot_id(name)
    if not memory:
        return BotMind(bot_id=bot_id, name=name, core=core, rules=RulesMemory(scenario_id=scenario_id))
    rules = RulesMemory(scenario_id=scenario_id)
    if scenario_id:
        rules = RulesMemory(
            scenario_id=scenario_id,
            digest=botfiles.read_part(directory, "digest", scenario_id),
            playbook=tuple(botfiles.parse_playbook(botfiles.read_part(directory, "playbook", scenario_id))),
            questions=botfiles.read_part(directory, "questions", scenario_id),
        )
    return BotMind(
        bot_id=bot_id,
        name=name,
        core=core,
        arch_notes=botfiles.read_part(directory, "arch_notes"),
        rules=rules,
        people=botfiles.parse_people(botfiles.read_part(directory, "people")),
        record=botfiles.record_text(botfiles.read_record(directory)),
    )


def ablate(mind: BotMind, options: Iterable[str]) -> BotMind:
    """``mind`` without the layers ``options`` name (``no-notes`` ... ``blank-core``; ``nomem`` is the brain's switch, not a change of the mind)."""
    changes: dict[str, Any] = {}
    rules = mind.rules
    for option in options:
        if option == "no-notes":
            changes["arch_notes"] = ""
        elif option == "no-digest":
            rules = dataclasses.replace(rules, digest="")
        elif option == "no-playbook":
            rules = dataclasses.replace(rules, playbook=())
        elif option == "no-questions":
            rules = dataclasses.replace(rules, questions="")
        elif option == "no-people":
            changes["people"] = {}
        elif option == "no-record":
            changes["record"] = ""
        elif option == "blank-core":
            changes["core"] = NEUTRAL_CORE
    return dataclasses.replace(mind, rules=rules, **changes)


# ---------------------------------------------------------------------------------------------------------------------- the factories
def legacy_bot(
    name: str = "", *, label: str = "", persona: str = "", memory: str = "", cfg: dict[str, Any] | None = None, history_chars: int | None = None, options: Sequence[str] = ()
) -> BenchBot:
    """The v0.13.1 player.  ``persona`` / ``memory`` are the roster texts of that version (empty = a fresh install: no persona, no memory)."""

    def factory(seat: str, world: World, spec: ScenarioSpec) -> LegacyBrain:
        limits = {"player": history_chars, "moderator": history_chars} if history_chars else None
        return LegacyBrain(world, spec, cfg, personas={seat: persona} if persona else {}, memories={seat: memory} if memory else {}, history_chars=limits)

    return BenchBot(label=label or (f"legacy:{name}" if name else "legacy"), kind="legacy", name=name, brain="LegacyBrain", factory=factory, options=tuple(options))


def mind_bot(
    mind: BotMind,
    contract: ContractDoc | None = None,
    *,
    label: str = "",
    memory_enabled: bool = True,
    options: Sequence[str] = (),
    cfg: dict[str, Any] | None = None,
) -> BenchBot:
    """A v0.14 bot made from a ready ``BotMind`` (the snapshot a game starts with) and the official contract."""
    doc = contract if contract is not None else load_default_contract()
    history_chars = history_chars_of(options)

    def factory(seat: str, world: World, spec: ScenarioSpec) -> BotBrain:
        return BotBrain(world, spec, seat, mind, doc, cfg=cfg, memory_enabled=memory_enabled, history_chars=history_chars)

    return BenchBot(
        label=label or f"bot:{mind.name}", kind="bot", name=mind.name, brain="BotBrain", factory=factory, mind=mind,
        memory_enabled=memory_enabled, options=tuple(options),
    )


def split_spec(text: str) -> tuple[str, str, tuple[str, ...]]:
    """``"bot:Alice@nomem,no-people"`` -> ``("bot", "Alice", ("nomem", "no-people"))``."""
    body, _, raw_options = text.strip().partition("@")
    kind, _, ref = body.partition(":")
    options = tuple(o.strip() for o in raw_options.split(",") if o.strip())
    return kind.strip().lower(), ref.strip(), options


def check_spec_text(text: str) -> None:
    """The shape of a spec string - its kind, its reference, its options - without loading anything.  Raises :class:`BotSpecError`."""
    kind, ref, options = split_spec(text)
    label = text.strip()
    unknown = [o for o in options if o not in ABLATIONS and not HISTORY_OPTION.match(o)]
    if unknown:
        raise BotSpecError(f"{label}：不认识的选项 {'、'.join(unknown)}。可用的：{'、'.join(ABLATIONS)}，以及 history=字数（例如 history=6000）。")
    if kind == "legacy":
        if [o for o in options if not HISTORY_OPTION.match(o)]:
            raise BotSpecError(f"{label}：legacy 没有可以拿掉的层（选项只对 bot: 有用；legacy 只认 history=字数）。")
    elif kind == "bot":
        if not ref:
            raise BotSpecError(f"{label}：bot: 后面要写默认库里的名字或一个目录。")
    else:
        raise BotSpecError(f"{label}：bot 的种类只能是 legacy 或 bot（现在是 {kind!r}）。")


def check_playbook_text(text: str) -> None:
    """A playbook written in a situation file: every line must be an entry (``- [把握:高|支持:3|反例:0|来源:复盘|#投票] 一句话``) or a heading."""
    entries, ignored = botfiles.parse_playbook_ex(text)
    if ignored:
        raise BotSpecError(f"playbook 里有 {ignored} 行不是打法条目（每条写成 `- [把握:高|支持:3|反例:0|来源:复盘|#投票] 一句话`）。")
    problems = botfiles.check_playbook(entries)
    if problems:
        raise BotSpecError("playbook 有问题：" + problems[0])


def _resolve_ref(ref: str, base_dir: Path | None) -> str:
    """A directory written relative to the file that names it (``bot:./minds/zed`` in a situation file) is looked for there first."""
    if base_dir is not None and ref and not Path(ref).is_absolute():
        candidate = Path(base_dir) / ref
        if (candidate / "core").is_dir():
            return str(candidate)
    return ref


def parse_bot_spec(
    text: str, *, scenario_id: str = "", library: Path | None = None, contract: ContractDoc | None = None, base_dir: Path | None = None
) -> BenchBot:
    """The bot a spec string stands for (see the module docstring).  ``base_dir``: where a relative directory is looked for first (the situation
    file's own).  Raises :class:`BotSpecError`."""
    check_spec_text(text)
    kind, ref, options = split_spec(text)
    label = text.strip()
    if kind == "legacy":
        return legacy_bot(ref, label=label, history_chars=history_chars_of(options), options=options)
    memory = "nomem" not in options
    mind = ablate(load_bot_mind(_resolve_ref(ref, base_dir), scenario_id=scenario_id, memory=memory, library=library), options)
    return mind_bot(mind, contract, label=label, memory_enabled=memory, options=options)


def mind_from_definition(definition: Mapping[str, Any], *, scenario_id: str = "") -> BotMind:
    """The snapshot an inline bot of a situation file stands for (``name``, ``core``, ``notes``, ``digest``, ``playbook``, ``questions``, ``people``,
    ``record``: what a game would start the bot with; the memory of the *scenario* of the situation).  Raises :class:`BotSpecError`."""
    name = str(definition.get("name") or "").strip()
    core_in = definition.get("core") or {}
    core = CoreFiles(identity=str(core_in.get("identity") or ""), voice=str(core_in.get("voice") or ""), mind=str(core_in.get("mind") or ""))
    if not name or not core.complete:
        raise BotSpecError(f"bot {name or '（没有名字）'} 的底层设定不完整（缺少：{'、'.join(core.missing) or 'name'}）。")
    playbook = str(definition.get("playbook") or "")
    check_playbook_text(playbook)
    return BotMind(
        bot_id=_bot_id(name),
        name=name,
        core=core,
        arch_notes=str(definition.get("notes") or "").strip(),
        rules=RulesMemory(
            scenario_id=scenario_id,
            digest=str(definition.get("digest") or "").strip(),
            playbook=tuple(botfiles.parse_playbook(playbook)),
            questions=str(definition.get("questions") or "").strip(),
        ),
        people={str(who): str(text).strip() for who, text in (definition.get("people") or {}).items()},
        record=str(definition.get("record") or "").strip(),
    )


def bot_from_definition(
    definition: Mapping[str, Any],
    *,
    scenario_id: str = "",
    library: Path | None = None,
    contract: ContractDoc | None = None,
    base_dir: Path | None = None,
) -> BenchBot:
    """The bot a ``bots:`` entry of a situation file stands for: ``{"spec": "bot:Alice@no-people"}`` or an inline snapshot."""
    if "spec" in definition:
        return parse_bot_spec(str(definition["spec"]), scenario_id=scenario_id, library=library, contract=contract, base_dir=base_dir)
    mind = mind_from_definition(definition, scenario_id=scenario_id)
    return mind_bot(mind, contract, label=str(definition.get("label") or f"bot:{mind.name}"))


def split_spec_list(raw: str) -> list[str]:
    """``"legacy,bot:Alice@nomem,no-people,bot:Bob"`` -> ``["legacy", "bot:Alice@nomem,no-people", "bot:Bob"]``.  Specs are separated by commas, and so
    are the options of one spec (``@nomem,no-people``): a piece that is exactly the name of an option belongs to the spec before it when that one
    already has an ``@`` (an option name is never a spec)."""
    out: list[str] = []
    for token in str(raw).split(","):
        token = token.strip()
        if not token:
            continue
        if (token in ABLATIONS or HISTORY_OPTION.match(token)) and out and "@" in out[-1]:
            out[-1] += "," + token
        else:
            out.append(token)
    return out


def expand_bot_specs(items: Sequence[str], *, library: Path | None = None) -> list[str]:
    """``default12`` / ``legacy12`` expand to their twelve specs; everything else stays.  Duplicates are an error (the label is the key)."""
    out: list[str] = []
    for raw in items:
        for item in split_spec_list(raw):
            if item in _GROUPS:
                names = default_names(library) if _GROUPS[item] == "bot" else list(DEFAULT_NAMES)
                if not names:
                    raise BotSpecError(f"{item}：默认库里一个 bot 也没有（{library_dir() if library is None else library}）。")
                out.extend(f"{_GROUPS[item]}:{name}" for name in names)
            else:
                out.append(item)
    duplicated = sorted({x for x in out if out.count(x) > 1})
    if duplicated:
        raise BotSpecError(f"重复的 bot：{'、'.join(duplicated)}。")
    return out


def make_bots(
    items: Sequence[str], *, scenario_id: str = "", library: Path | None = None, contract: ContractDoc | None = None
) -> list[BenchBot]:
    """The bots of ``--bots`` (groups expanded), each made once."""
    doc = contract
    if doc is None and any(split_spec(spec)[0] == "bot" for spec in expand_bot_specs(items, library=library)):
        doc = load_default_contract()
    return [parse_bot_spec(spec, scenario_id=scenario_id, library=library, contract=doc) for spec in expand_bot_specs(items, library=library)]


def pinned_bots(
    situation: Any, *, library: Path | None = None, contract: ContractDoc | None = None
) -> dict[str, BenchBot]:
    """``{seat: bot}`` for the seats a situation file puts a bot on (``bots:``); ``{}`` for a situation that does not.  A directory written relative
    (``bot:./minds/zed``) is looked for next to the situation file.  Raises :class:`BotSpecError`, naming the situation and the seat."""
    base = Path(situation.path).parent if getattr(situation, "path", "") else None
    out: dict[str, BenchBot] = {}
    for seat, definition in situation.bots.items():
        try:
            out[seat] = bot_from_definition(definition, scenario_id=situation.scenario, library=library, contract=contract, base_dir=base)
        except BotSpecError as exc:
            raise BotSpecError(f"{situation.id}（bots.{seat}）：{exc}") from None
    return out


def contract_from_markdown(markdown: str) -> ContractDoc:
    """A contract given as text (a snapshot in a saved game: ``parse_contract(snapshot["markdown"])``)."""
    return parse_contract(markdown)


__all__ = [
    "ABLATIONS",
    "NEUTRAL_CORE",
    "BenchBot",
    "BotSpecError",
    "BrainFactory",
    "ablate",
    "bot_from_definition",
    "check_playbook_text",
    "check_spec_text",
    "contract_from_markdown",
    "default_names",
    "expand_bot_specs",
    "legacy_bot",
    "library_dir",
    "load_bot_mind",
    "make_bots",
    "mind_bot",
    "mind_from_definition",
    "parse_bot_spec",
    "pinned_bots",
    "split_spec",
    "split_spec_list",
]
