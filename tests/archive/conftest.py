from __future__ import annotations

from pathlib import Path

import pytest

from app.archive import kdf

from .helpers import KEY_1, KEY_2, PROVIDER_1, PROVIDER_2, FakeSecrets, populate


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """scrypt at the real cost (128 MiB, ~1 s) is exercised by one dedicated test; everything else uses a small ``n``."""
    monkeypatch.setattr(kdf, "SCRYPT_N", 2**10)


@pytest.fixture
def data_dir(isolated_external_storage: Path) -> Path:
    isolated_external_storage.mkdir(parents=True, exist_ok=True)
    return isolated_external_storage


@pytest.fixture
def populated(data_dir: Path) -> dict[str, bytes]:
    """The data directory is filled; the value is what an export must contain (archive path -> bytes)."""
    return populate(data_dir)


@pytest.fixture
def local_keys() -> FakeSecrets:
    return FakeSecrets({PROVIDER_1: KEY_1, PROVIDER_2: KEY_2})
