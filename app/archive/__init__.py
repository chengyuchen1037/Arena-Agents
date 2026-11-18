"""Archives (v0.13, decisions F26-F33): export the whole data directory to one ``.arenaagents`` file and import it again.

Modules: :mod:`~app.archive.format` (container, manifest, path allow-list), :mod:`~app.archive.kdf` (password rule and the
encrypted API keys), :mod:`~app.archive.export`, :mod:`~app.archive.importer` (check / unpack / swap with backup and
rollback), :mod:`~app.archive.jobs` (background jobs and progress events), :mod:`~app.archive.legacy` (the "old v0.12.1
data" notice).  The format is documented in ``docs/archive.md``.
"""

from app.archive.errors import (
    ArchiveBusyError,
    ArchiveError,
    ArchiveFormatError,
    ArchiveIOError,
    ArchiveLimitError,
    ArchiveNotFoundError,
    ArchivePasswordError,
    ArchivePasswordRuleError,
    ArchiveStateError,
    ArchiveVersionError,
)
from app.archive.format import FILE_SUFFIX, FORMAT_NAME, FORMAT_VERSION

__all__ = [
    "FILE_SUFFIX",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "ArchiveBusyError",
    "ArchiveError",
    "ArchiveFormatError",
    "ArchiveIOError",
    "ArchiveLimitError",
    "ArchiveNotFoundError",
    "ArchivePasswordError",
    "ArchivePasswordRuleError",
    "ArchiveStateError",
    "ArchiveVersionError",
]
