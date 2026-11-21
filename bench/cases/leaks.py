"""The desensitization check of the question bank: no sentence that a person or a bot said in a saved game is in a committed situation file.

The L4 situations are cut from real games and the speeches of those games are the private property of the people who played them (and of the bots that wrote them);
what the bank keeps is the *structure* of a game - who sat where, who claimed what, who voted for whom - and every word is the author's own.  This module is the test of
that promise: it takes every text a *player* wrote in a saved game (speeches in every channel, the reasons given with answers, the declarations of an ability), and every
text of a situation file (the lines its players say, the title, the brief, the truth, the comment at the top), and reports the places where the two share a run of ``n``
characters (``n = 10`` by default: about a clause, with punctuation and spaces taken out).  Moderator texts are not compared (they come from the manual of the scenario and
are the same in every game); neither are names (the default players are named Alice ... Leo in every game; a *foreign* name in a game is reported by :func:`foreign_names`).

Run against the games of this machine with ``python -m bench.cases.leaks`` (``--games <dir with games/<id>/game.json>``; default ``data``); ``tests/bench/test_bank.py`` does it
when ``ARENA_BENCH_SAVED_GAMES=1``, and proves with a built-in leak sample that the check does catch a leak.  Nothing here writes anything: saved games are only read.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_N = 10
_DROP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
#: Keys of a template's ``params`` that are structure (a seat, a side), not words.
_STRUCTURE_PARAMS = {"target", "side", "players", "player", "hunter", "direction", "winner", "template"}


def normalize(text: Any) -> str:
    """The characters that carry the words: letters, digits and Chinese characters, lower-cased (no spaces, no punctuation, no markdown)."""
    return _DROP.sub("", str(text or "")).lower()


def grams(text: Any, n: int = DEFAULT_N) -> set[str]:
    s = normalize(text)
    return {s[i : i + n] for i in range(len(s) - n + 1)}


@dataclass(frozen=True)
class Leak:
    where: str  # which file / step of the bank
    shared: str  # the longest run of shared characters (normalized: no punctuation)
    length: int
    source: str  # where in which game it is said

    def __str__(self) -> str:
        return f"{self.where}：和存档 {self.source} 共有 {self.length} 个字的连续片段「{self.shared[:40]}」"


# ---------------------------------------------------------------------------------------------------------------------- what players wrote
def _params_text(params: Any) -> list[str]:
    if not isinstance(params, Mapping):
        return []
    return [str(v) for k, v in params.items() if k not in _STRUCTURE_PARAMS and isinstance(v, str) and v.strip()]


def texts_of_steps(steps: Iterable[Any], *, label: str = "step") -> list[tuple[str, str]]:
    """Every text a player writes in a script of steps (the format of ``bench.situation``): ``say``, the ``content`` / ``reason`` / ``declaration`` of a player's tool call, the
    ``reasons`` of an ``answer``.  The moderator's tool calls are not players' words."""
    out: list[tuple[str, str]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        if isinstance(step.get("say"), Mapping):
            out.extend((f"{label} {index}: {seat} says", str(text)) for seat, text in step["say"].items())
        if isinstance(step.get("reasons"), Mapping):
            out.extend((f"{label} {index}: reason of {seat}", str(text)) for seat, text in step["reasons"].items())
        actor = str(step.get("as") or "")
        if actor and actor != "moderator":
            args = step.get("args") or {}
            for key in ("content", "reason", "text", "message"):
                if isinstance(args.get(key), str) and args[key].strip():
                    out.append((f"{label} {index}: {actor} {step.get('tool')}.{key}", args[key]))
            value = args.get("value")
            if step.get("tool") == "respond" and isinstance(value, str) and len(value.strip()) > 12:  # (the answer to a text request is written by the player; an option id is not)
                out.append((f"{label} {index}: {actor} respond.value", value))
            out.extend((f"{label} {index}: {actor} {step.get('tool')}.params", text) for text in _params_text(args.get("params")))  # (a declaration, a reason of a template)
    return out


def author_texts(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The author's own texts of a situation: title, brief, the strings of ``truth`` / ``judge`` / ``meta``."""
    out: list[tuple[str, str]] = []

    def walk(value: Any, where: str) -> None:
        if isinstance(value, str):
            if value.strip():
                out.append((where, value))
        elif isinstance(value, Mapping):
            for key, item in value.items():
                walk(item, f"{where}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{where}[{index}]")

    for key in ("title", "brief"):
        walk(data.get(key), key)
    for key in ("truth", "judge", "meta", "notes"):
        walk(data.get(key), key)
    return out


def texts_of_file(path: Path) -> list[tuple[str, str]]:
    """Every text that is the author's or a player's in a situation or pair file: the comment at the top, the author's fields, the players' lines."""
    raw = path.read_text(encoding="utf-8")
    name = path.name
    out = [(f"{name}: header", "\n".join(line[1:] for line in raw.splitlines() if line.startswith("#")))]
    data = yaml.safe_load(raw)
    if isinstance(data, Mapping):
        out.extend((f"{name}: {where}", text) for where, text in author_texts(data))
        out.extend((f"{name}: {where}", text) for where, text in texts_of_steps(data.get("steps") or []))
    return out


def texts_of_game(game: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Every text a player wrote in a saved game: speeches (public, team, private), the reasons given with answers, the declarations of an ability, the words of a challenge.
    (The moderator's texts are the manual's and the same in every game; they are not in the list.)"""
    from bench.replay import convert_ledger

    conversion = convert_ledger(dict(game), upto_seq=None)
    steps = []
    for step in conversion.steps:
        if step.kind != "tool" or step.actor == "moderator":
            continue
        steps.append({"as": step.actor, "tool": step.tool, "args": dict(step.args), "index": step.index})
    return texts_of_steps(steps, label="step")


def foreign_names(game: Mapping[str, Any], defaults: Iterable[str]) -> set[str]:
    """The names in a game's roster that are not one of the default players (the name a person chose for himself)."""
    known = set(defaults)
    return {str(p.get("name")) for p in game.get("participants") or [] if p.get("name") and str(p.get("name")) not in known}


# ---------------------------------------------------------------------------------------------------------------------- comparing
def rules_grams(n: int = DEFAULT_N, directory: Path | None = None) -> set[str]:
    """The n-grams of the rules of the scenarios (``scenarios/*.md``: the rules, the manual of the moderator, the templates).  A player who quotes the rules ("猎人被白狼王自爆带走")
    says what the rules say: that is not a sentence of his own, and the bank may say it too."""
    root = directory or Path(__file__).resolve().parents[2] / "scenarios"
    found: set[str] = set()
    for path in sorted(root.glob("*.md")):
        try:
            found |= grams(path.read_text(encoding="utf-8"), n)
        except (OSError, UnicodeError):
            continue
    return found


def corpus_index(games: Mapping[str, Mapping[str, Any]], *, n: int = DEFAULT_N, exempt: set[str] | None = None) -> dict[str, str]:
    """``{n-gram: "<game id> <where>"}`` of every text of the games (the first place it is said), except the n-grams of ``exempt`` (the rules: see :func:`rules_grams`;
    default: the rules of ``scenarios/``)."""
    skip = rules_grams(n) if exempt is None else exempt
    index: dict[str, str] = {}
    for game_id, game in games.items():
        for where, text in texts_of_game(game):
            for gram in grams(text, n):
                if gram not in skip:
                    index.setdefault(gram, f"{game_id[:8]} {where}")
    return index


def find_leaks(texts: Iterable[tuple[str, str]], index: Mapping[str, str], *, n: int = DEFAULT_N) -> list[Leak]:
    """The texts that share a run of at least ``n`` characters with the corpus: ``(where, text)`` in, a :class:`Leak` for each text out (its longest shared run)."""
    leaks: list[Leak] = []
    for where, text in texts:
        s = normalize(text)
        covered = [False] * len(s)
        source = ""
        for i in range(len(s) - n + 1):
            hit = index.get(s[i : i + n])
            if hit:
                source = source or hit
                for j in range(i, i + n):
                    covered[j] = True
        if not source:
            continue
        best = start = run = 0
        for j, flag in enumerate(covered + [False]):
            if flag:
                run += 1
                if run > best:
                    best, start = run, j - run + 1
            else:
                run = 0
        leaks.append(Leak(where, s[start : start + best], best, source))
    return leaks


def bank_files(root: Path) -> list[Path]:
    """Every file of the bank: the situations and the pairs (the files with a leading ``_`` are the examples of the documentation: written by hand, nobody else's words)."""
    return sorted(p for sub in ("situations", "pairs") for p in (root / sub).glob("*.yaml") if not p.name.startswith("_"))


#: The files cut from saved games are held to ``n``; the others (written from the rules by the generators, every word the author's) to this longer run: a short
#: sentence of the rules ("猎人被白狼王自爆带走") is said by a player and asked by a question alike.
GENERATED_N = 14
REAL_PREFIX = "l4-"


def scan_bank(root: Path, games: Mapping[str, Mapping[str, Any]], *, n: int = DEFAULT_N, generated_n: int = GENERATED_N) -> list[Leak]:
    """Every leak of the bank below ``root`` (``bench/fixtures``) against ``games``: a run of ``n`` characters in the files that were cut from a game (``l4-*``),
    ``generated_n`` in the rest."""
    indices = {size: corpus_index(games, n=size) for size in {n, generated_n}}
    leaks: list[Leak] = []
    for path in bank_files(root):
        size = n if path.name.startswith(REAL_PREFIX) else generated_n
        leaks.extend(find_leaks(texts_of_file(path), indices[size], n=size))
    return leaks


def load_games(data_dir: Path) -> dict[str, dict[str, Any]]:
    """The saved games below ``<data_dir>/games/<id>/game.json`` (read only)."""
    games: dict[str, dict[str, Any]] = {}
    for path in sorted((data_dir / "games").glob("*/game.json")):
        try:
            games[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return games


def main(argv: Sequence[str] | None = None) -> int:
    from bench.situation import DEFAULT_NAMES

    parser = argparse.ArgumentParser(prog="python -m bench.cases.leaks", description="Check that the question bank holds no text of a saved game (nothing is written).")
    parser.add_argument("--games", type=Path, default=Path("data"), help="the data directory with games/<id>/game.json (default: data)")
    parser.add_argument("--fixtures", type=Path, default=Path(__file__).resolve().parents[1] / "fixtures", help="the bank (default: bench/fixtures)")
    parser.add_argument("--n", type=int, default=DEFAULT_N, help=f"the length of a shared run that counts as a leak in the files cut from a game (default {DEFAULT_N})")
    parser.add_argument("--generated-n", type=int, default=GENERATED_N, help=f"... and in the files the generators wrote from the rules (default {GENERATED_N})")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    games = load_games(args.games)
    if not games:
        print(f"{args.games} 里没有存档（games/<id>/game.json）：没有东西可以对拍。")
        return 0
    leaks = scan_bank(args.fixtures, games, n=args.n, generated_n=args.generated_n)
    foreign = {gid: foreign_names(game, DEFAULT_NAMES) for gid, game in games.items()}
    blob = "\n".join(f"{where}\n{text}" for path in bank_files(args.fixtures) for where, text in texts_of_file(path))
    named = sorted({name for names in foreign.values() for name in names if name in blob})
    for leak in leaks:
        print(leak)
    for name in named:
        print(f"题库里出现了存档里的非默认名字：{name}")
    print(f"对拍了 {len(games)} 局存档（切自存档的题 {args.n} 字、其余 {args.generated_n} 字的连续片段）：{'没有泄漏。' if not leaks and not named else f'{len(leaks)} 处泄漏，{len(named)} 个名字。'}")
    return 1 if leaks or named else 0


__all__ = [
    "DEFAULT_N", "GENERATED_N", "Leak", "author_texts", "bank_files", "corpus_index", "find_leaks", "foreign_names", "grams", "load_games", "normalize", "rules_grams", "scan_bank", "texts_of_file",
    "texts_of_game", "texts_of_steps",
]

if __name__ == "__main__":
    raise SystemExit(main())
