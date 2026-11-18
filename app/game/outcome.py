"""Who won a finished game, from the moderator's free-text winner (v0.12.1, E21).

``end_game(winner=…)`` carries free text.  To keep per-player records the application matches it against
the factions of the scenario: the faction ids and the optional ``setup.faction_names`` display names.

* the winner names the player's own faction  -> he won;
* it names another faction                   -> he lost;
* it names nobody (a draw, an odd phrase)    -> unknown: the review is written but the record is untouched.
"""

from __future__ import annotations

from typing import Any

WON, LOST, UNKNOWN = True, False, None


def _names_of(spec: Any, faction: str) -> list[str]:
    names = [faction]
    label = (getattr(spec.setup, "faction_names", None) or {}).get(faction)
    if label:
        names.append(label)
    return [n.strip().casefold() for n in names if n and n.strip()]


def _mentions(winner: str, names: list[str]) -> bool:
    text = winner.strip().casefold()
    return any(name and (name in text or text == name) for name in names)


def player_won(spec: Any, faction: str, winner: str | None) -> bool | None:
    """``True`` / ``False`` / ``None`` (unknown) for a player of ``faction`` when ``winner`` was announced."""
    text = (winner or "").strip()
    if not text or not faction:
        return UNKNOWN
    if _mentions(text, _names_of(spec, faction)):
        # "好人阵营" also appears inside e.g. "非好人阵营"; a longer name of another faction that contains ours wins
        others = {
            f
            for f in {r.faction for r in spec.setup.roles.values() if r.faction} | set(spec.setup.faction_names)
            if f != faction
        }
        mine_len = max(len(n) for n in _names_of(spec, faction))
        for other in others:
            if _mentions(text, _names_of(spec, other)) and max(len(n) for n in _names_of(spec, other)) > mine_len:
                return LOST
        return WON
    others = {
        f for f in {r.faction for r in spec.setup.roles.values() if r.faction} | set(spec.setup.faction_names) if f != faction
    }
    if any(_mentions(text, _names_of(spec, other)) for other in others):
        return LOST
    return UNKNOWN


def role_label(spec: Any, role_id: str) -> str:
    """How a role is called in a record: its first tag (the scenario's name for it), else the role id."""
    role = spec.setup.roles.get(role_id)
    if role is not None and role.tags:
        return str(role.tags[0])
    return role_id or "未知身份"
