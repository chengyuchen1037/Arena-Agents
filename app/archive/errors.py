"""Errors of the archive subsystem (docs/archive.md).

Every message is written for the user (Chinese) and is safe to show and to log: it never carries a password,
an API key, a derived key or the content of a file - at most a short, sanitized archive path.  ``code`` is the
machine-readable reason the HTTP layer and the UI branch on; ``status`` is the HTTP status the API answers with.
"""

from __future__ import annotations

import re

#: What a wrong password and a tampered ``secrets.enc`` have in common: AES-GCM cannot tell them apart, and the
#: user must not be able to either (docs/archive.md "加密").
WRONG_PASSWORD_MESSAGE = "密码错误或 API Key 部分已损坏"

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe_text(value: object, limit: int = 80) -> str:
    """A short, printable rendering of an untrusted string (an archive path, a file name) for messages."""
    text = _CONTROL.sub("?", str(value))
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ArchiveError(Exception):
    """An archive operation failed; ``str(error)`` is a user-facing Chinese sentence."""

    code = "archive_error"
    status = 400

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""


class ArchiveFormatError(ArchiveError):
    """The file is not a valid ``.arenaagents`` archive (structure, listing, checksum, content)."""

    code = "invalid_archive"
    status = 422


class ArchiveVersionError(ArchiveFormatError):
    """The archive was written by a newer format than this program understands."""

    code = "unsupported_version"


class ArchiveKeysMismatchError(ArchiveFormatError):
    """The sealed keys were made for other provider addresses than ``providers.json`` now names: the archive was changed
    after the keys were encrypted.  The rest of the archive may still be fine (import it with the keys skipped)."""

    code = "keys_mismatch"


class ArchivePasswordError(ArchiveError):
    """Wrong password, or a ``secrets.enc`` that was changed: one message for both (see ``WRONG_PASSWORD_MESSAGE``)."""

    code = "wrong_password"
    status = 422

    def __init__(self, message: str = WRONG_PASSWORD_MESSAGE) -> None:
        super().__init__(message)


class ArchivePasswordRuleError(ArchiveError):
    """A new password does not meet the rule (length / character classes)."""

    code = "weak_password"
    status = 422


class ArchiveLimitError(ArchiveError):
    """A size limit was exceeded (upload size, entry count, unpacked size, compression ratio)."""

    code = "too_large"
    status = 413


class ArchiveBusyError(ArchiveError):
    """Another export / import is already running."""

    code = "busy"
    status = 409


class ArchiveStateError(ArchiveError):
    """The application is in a state that does not allow this (a game is running)."""

    code = "game_running"
    status = 409


class ArchiveNotFoundError(ArchiveError):
    """No such export file / import session / job."""

    code = "not_found"
    status = 404


class ArchiveIOError(ArchiveError):
    """The disk or the key store failed (full disk, locked file, key storage unavailable)."""

    code = "io_error"
    status = 500
