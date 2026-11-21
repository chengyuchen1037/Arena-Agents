"""Read a saved game and its trace into the plain shapes the metrics use (no model, no engine, no data directory)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODERATOR = "moderator"


@dataclass(frozen=True)
class Speech:
    """One piece of speech of a player: what is in the ledger (``text`` is what the others read)."""

    seq: int
    seat: str  # the player id of that game
    name: str
    channel: str
    text: str
    kind: str = "speech"
    truncated: int = 0  # characters the engine cut off (v0.13: the cap; v0.14: always 0 for a player)
    human: bool = False  # a person played the seat when it was said (``human_spans``)


def load_game(path: str | Path) -> dict[str, Any]:
    """The saved game (``game.json``) as a dict; a damaged file raises ``ValueError``."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read the game {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("world"), dict):
        raise ValueError(f"{path} is not a saved game")
    return data


def ledger_of(game: dict[str, Any]) -> list[dict[str, Any]]:
    return list((game.get("world") or {}).get("ledger") or [])


def names_of(game: dict[str, Any]) -> dict[str, str]:
    """``{player id: name}`` (the names of the game's participants)."""
    return {str(p.get("id")): str(p.get("name") or p.get("id")) for p in game.get("participants") or []}


def _human_spans(game: dict[str, Any]) -> list[tuple[str, int, float]]:
    spans = []
    for span in game.get("human_spans") or []:
        end = span.get("to_seq")
        spans.append((str(span.get("seat")), int(span.get("from_seq") or 0), float("inf") if end is None else float(end)))
    return spans


def speeches_of(game: dict[str, Any], *, scope: str = "public") -> list[Speech]:
    """Every player's speech of the game in ledger order.  ``scope``: ``"public"`` (channels of type public), ``"all"`` (every
    channel a player wrote a ``speech`` message in, wolf chat and private chats included) or one channel id."""
    names = names_of(game)
    spans = _human_spans(game)
    state = (game.get("world") or {}).get("state") or {}
    public = {str(c.get("id")) for c in state.get("channels") or [] if c.get("type") == "public"} or {"public"}
    out: list[Speech] = []
    for entry in ledger_of(game):
        if entry.get("type") != "message":
            continue
        data = entry.get("data") or {}
        actor = str(entry.get("actor_id") or "")
        if data.get("kind", "speech") != "speech" or actor in ("", MODERATOR, "engine"):
            continue
        channel = str(entry.get("channel_id") or "")
        if scope == "public" and channel not in public:
            continue
        if scope not in ("public", "all") and channel != scope:
            continue
        seq = int(entry.get("seq") or 0)
        truncated = data.get("truncated") or {}
        out.append(
            Speech(
                seq=seq,
                seat=actor,
                name=names.get(actor, actor),
                channel=channel,
                text=str(data.get("content") or ""),
                kind=str(data.get("kind") or "speech"),
                truncated=int(truncated.get("dropped_chars") or 0) if isinstance(truncated, dict) else 0,
                human=any(seat == actor and start <= seq <= end for seat, start, end in spans),
            )
        )
    return out


def read_trace(path: str | Path) -> list[dict[str, Any]]:
    """The events of a trace file (a half-written last line is skipped)."""
    return list(iter_trace(path))


def iter_trace(path: str | Path) -> Iterator[dict[str, Any]]:
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def by_seat(speeches: Iterable[Speech]) -> dict[str, list[Speech]]:
    grouped: dict[str, list[Speech]] = {}
    for speech in speeches:
        grouped.setdefault(speech.seat, []).append(speech)
    return grouped
