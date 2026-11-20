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
from collections.abc import Callable, Iterable, Sequence
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
    bot_id = "b-" + hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()[:8]
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
def legacy_bot(name: str = "", *, label: str = "", persona: str = "", memory: str = "", cfg: dict[str, Any] | None = None) -> BenchBot:
    """The v0.13.1 player.  ``persona`` / ``memory`` are the roster texts of that version (empty = a fresh install: no persona, no memory)."""

    def factory(seat: str, world: World, spec: ScenarioSpec) -> LegacyBrain:
        return LegacyBrain(world, spec, cfg, personas={seat: persona} if persona else {}, memories={seat: memory} if memory else {})

    return BenchBot(label=label or (f"legacy:{name}" if name else "legacy"), kind="legacy", name=name, brain="LegacyBrain", factory=factory)


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

    def factory(seat: str, world: World, spec: ScenarioSpec) -> BotBrain:
        return BotBrain(world, spec, seat, mind, doc, cfg=cfg, memory_enabled=memory_enabled)

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


def parse_bot_spec(text: str, *, scenario_id: str = "", library: Path | None = None, contract: ContractDoc | None = None) -> BenchBot:
    """The bot a spec string stands for (see the module docstring).  Raises :class:`BotSpecError`."""
    kind, ref, options = split_spec(text)
    label = text.strip()
    unknown = [o for o in options if o not in ABLATIONS]
    if unknown:
        raise BotSpecError(f"{label}：不认识的选项 {'、'.join(unknown)}。可用的：{'、'.join(ABLATIONS)}。")
    if kind == "legacy":
        if options:
            raise BotSpecError(f"{label}：legacy 没有可以拿掉的层（选项只对 bot: 有用）。")
        return legacy_bot(ref, label=label)
    if kind == "bot":
        if not ref:
            raise BotSpecError(f"{label}：bot: 后面要写默认库里的名字或一个目录。")
        memory = "nomem" not in options
        mind = ablate(load_bot_mind(ref, scenario_id=scenario_id, memory=memory, library=library), options)
        return mind_bot(mind, contract, label=label, memory_enabled=memory, options=options)
    raise BotSpecError(f"{label}：bot 的种类只能是 legacy 或 bot（现在是 {kind!r}）。")


def expand_bot_specs(items: Sequence[str], *, library: Path | None = None) -> list[str]:
    """``default12`` / ``legacy12`` expand to their twelve specs; everything else stays.  Duplicates are an error (the label is the key)."""
    out: list[str] = []
    for raw in items:
        for item in str(raw).split(","):
            item = item.strip()
            if not item:
                continue
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
    "contract_from_markdown",
    "default_names",
    "expand_bot_specs",
    "legacy_bot",
    "library_dir",
    "load_bot_mind",
    "make_bots",
    "mind_bot",
    "parse_bot_spec",
    "split_spec",
]
