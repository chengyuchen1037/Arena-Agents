from __future__ import annotations

import re

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("session_id must match ^[A-Za-z0-9_-]{1,64}$")
    if session_id.upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError("session_id must not be a Windows reserved name")
    return session_id
