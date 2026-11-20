"""The password rule and ``secrets.enc`` (docs/archive.md "加密")."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from app.archive import kdf
from app.archive.errors import ArchiveFormatError, ArchivePasswordError, ArchivePasswordRuleError
from app.archive.format import FORMAT_NAME, FORMAT_VERSION
from app.archive.kdf import SECRETS_FORMAT_VERSION

from .helpers import KEY_1, KEY_2, PASSWORD, PROVIDER_1, PROVIDER_2

ARCHIVE_ID = "0123456789abcdef0123456789abcdef"
OTHER_ID = "fedcba9876543210fedcba9876543210"
KEYS = {PROVIDER_1: KEY_1, PROVIDER_2: KEY_2}
CASES = json.loads((Path(__file__).parent / "password_cases.json").read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------------------- the rule
@pytest.mark.parametrize("case", CASES, ids=[c["note"] for c in CASES])
def test_password_rule_table(case: dict) -> None:
    """The same table is run by the front end (tests/frontend/test_archive_ui.js): the two rules cannot drift apart."""
    assert (kdf.password_problem(case["password"]) is None) is case["ok"]


def test_password_rule_constants_and_texts() -> None:
    assert (kdf.MIN_PASSWORD_CHARS, kdf.MIN_PASSWORD_CLASSES, kdf.MAX_PASSWORD_CHARS) == (12, 3, 256)
    assert "12" in kdf.PASSWORD_RULE_TEXT and "三类" in kdf.PASSWORD_RULE_TEXT
    assert kdf.password_classes("Abc123!!") == ["upper", "lower", "digit", "symbol"]
    assert kdf.password_classes("abc") == ["lower"]


def test_a_new_password_that_breaks_the_rule_is_refused_with_the_reason_but_never_echoed() -> None:
    secret = "short-Pw1"
    with pytest.raises(ArchivePasswordRuleError) as excinfo:
        kdf.check_password(secret)
    assert secret not in str(excinfo.value)
    assert "12" in str(excinfo.value)
    assert kdf.password_problem(None) and kdf.password_problem(12345678901234)


def test_check_password_normalizes_to_nfc() -> None:
    decomposed = "Cafe\u0301-Latte-2024"
    assert kdf.check_password(decomposed) == "Café-Latte-2024"


# ----------------------------------------------------------------------------------------- sealing
def test_seal_and_open_round_trip() -> None:
    blob = kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID)
    assert kdf.open_secrets(blob, PASSWORD, ARCHIVE_ID) == KEYS


def test_the_blob_never_contains_the_password_or_a_key() -> None:
    blob = kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID)
    for secret in (PASSWORD, KEY_1, KEY_2, base64.b64encode(KEY_1.encode()).decode()):
        assert secret.encode() not in blob


def test_header_is_plain_json_with_the_documented_fields() -> None:
    header = json.loads(kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID))
    assert set(header) == {"format", "version", "kdf", "n", "r", "p", "salt", "cipher", "nonce", "aad", "ciphertext"}
    assert (header["format"], header["version"], header["kdf"], header["cipher"]) == (FORMAT_NAME, SECRETS_FORMAT_VERSION, "scrypt", "aes-256-gcm")
    assert (header["r"], header["p"]) == (8, 1)
    assert len(base64.b64decode(header["salt"])) == 16 and len(base64.b64decode(header["nonce"])) == 12
    aad = json.loads(base64.b64decode(header["aad"]))
    assert aad == {"archive_id": ARCHIVE_ID, "format": FORMAT_NAME, "format_version": SECRETS_FORMAT_VERSION, "purpose": "secrets"}


def test_every_seal_uses_a_fresh_salt_and_nonce() -> None:
    first = json.loads(kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID))
    second = json.loads(kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID))
    assert first["salt"] != second["salt"] and first["nonce"] != second["nonce"] and first["ciphertext"] != second["ciphertext"]


def test_the_real_cost_parameters_are_n_2_17_r_8_p_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """One test at the real cost (128 MiB, about a second): the others run with a cheap ``n``."""
    monkeypatch.setattr(kdf, "SCRYPT_N", 2**17)
    blob = kdf.seal_secrets({PROVIDER_1: KEY_1}, PASSWORD, ARCHIVE_ID)
    header = json.loads(blob)
    assert (header["n"], header["r"], header["p"]) == (131072, 8, 1)
    assert kdf.open_secrets(blob, PASSWORD, ARCHIVE_ID) == {PROVIDER_1: KEY_1}


# ----------------------------------------------------------------------- failures: one message for all
def _change(blob: bytes, mutate) -> bytes:  # type: ignore[no-untyped-def]
    header = json.loads(blob)
    mutate(header)
    return json.dumps(header).encode("ascii")


def _flip(value: str) -> str:
    raw = bytearray(base64.b64decode(value))
    raw[len(raw) // 2] ^= 0x01
    return base64.b64encode(bytes(raw)).decode("ascii")


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda h: h.update(ciphertext=_flip(h["ciphertext"])), id="ciphertext"),
        pytest.param(lambda h: h.update(salt=_flip(h["salt"])), id="salt"),
        pytest.param(lambda h: h.update(nonce=_flip(h["nonce"])), id="nonce"),
        pytest.param(lambda h: h.update(aad=_flip(h["aad"])), id="aad"),
        pytest.param(lambda h: h.update(ciphertext=h["ciphertext"][:-8]), id="truncated"),
        pytest.param(lambda h: h.update(n=2**11), id="cost-parameter"),
        pytest.param(lambda h: h.update(version=SECRETS_FORMAT_VERSION + 1), id="version"),
        pytest.param(lambda h: h.update(format="other"), id="format-name"),
        pytest.param(lambda h: h.pop("salt"), id="missing-field"),
        pytest.param(lambda h: h.update(extra=1), id="extra-field"),
    ],
)
def test_a_changed_blob_fails_with_the_same_error_as_a_wrong_password(tamper) -> None:  # type: ignore[no-untyped-def]
    blob = kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID)
    with pytest.raises(ArchivePasswordError) as excinfo:
        kdf.open_secrets(_change(blob, tamper), PASSWORD, ARCHIVE_ID)
    assert str(excinfo.value) == "密码错误或 API Key 部分已损坏"


def test_wrong_password_and_foreign_archive_id_fail_the_same_way() -> None:
    blob = kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID)
    for password, archive_id in (("Wrong-Horse-Battery-9", ARCHIVE_ID), (PASSWORD, OTHER_ID), ("", ARCHIVE_ID)):
        with pytest.raises(ArchivePasswordError, match="密码错误或 API Key 部分已损坏"):
            kdf.open_secrets(blob, password, archive_id)
    for garbage in (b"", b"not json", b"[]", b"\xff\xfe"):
        with pytest.raises(ArchivePasswordError):
            kdf.open_secrets(garbage, PASSWORD, ARCHIVE_ID)


def test_the_password_check_error_carries_no_cause_and_no_secret() -> None:
    blob = kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID)
    with pytest.raises(ArchivePasswordError) as excinfo:
        kdf.open_secrets(blob, "Wrong-Horse-Battery-9", ARCHIVE_ID)
    error = excinfo.value
    assert error.__cause__ is None and error.__suppress_context__
    assert "Wrong-Horse" not in repr(error) and KEY_1 not in repr(error)


def test_header_shape_is_checked_without_a_password() -> None:
    blob = kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID)
    header = kdf.parse_secrets_header(blob)
    assert header.n == kdf.SCRYPT_N and len(header.salt) == 16 and len(header.nonce) == 12
    with pytest.raises(ArchiveFormatError):
        kdf.parse_secrets_header(b"{}")
    with pytest.raises(ArchiveFormatError):
        kdf.parse_secrets_header(_change(blob, lambda h: h.update(n=2**30)))  # a file cannot ask the reader for gigabytes


ENDPOINTS = {
    PROVIDER_1: {"protocol": "openai_chat", "base_url": "https://api.example.com", "auto_append_path": True},
    PROVIDER_2: {"protocol": "anthropic_messages", "base_url": "https://api.example.org/v1", "auto_append_path": False},
}


def test_every_key_is_sealed_together_with_the_address_it_belongs_to() -> None:
    blob = kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID, ENDPOINTS)
    sealed = kdf.open_secrets_bound(blob, PASSWORD, ARCHIVE_ID)
    assert sealed.keys == KEYS and sealed.endpoints == ENDPOINTS
    assert kdf.open_secrets(blob, PASSWORD, ARCHIVE_ID) == KEYS, "the plain reader still gives the keys"
    for secret in ("api.example.com", "anthropic_messages"):
        assert secret.encode() not in blob, "the addresses are inside the ciphertext as well: nobody can read or edit them"
    # without endpoints (a library caller that did not pass them) there is nothing bound
    assert kdf.open_secrets_bound(kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID), PASSWORD, ARCHIVE_ID).endpoints == {}


def test_the_endpoints_must_match_the_keys_and_have_the_three_fields() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID, {PROVIDER_1: ENDPOINTS[PROVIDER_1]})  # a key without an address
    with pytest.raises(ValueError):
        kdf.seal_secrets({PROVIDER_1: KEY_1}, PASSWORD, ARCHIVE_ID, {PROVIDER_1: {"protocol": "openai_chat"}})
    with pytest.raises(ValueError):
        kdf.seal_secrets({PROVIDER_1: KEY_1}, PASSWORD, ARCHIVE_ID, {PROVIDER_1: {**ENDPOINTS[PROVIDER_1], "extra": 1}})
    with pytest.raises(ValueError):
        kdf.seal_secrets({PROVIDER_1: KEY_1}, PASSWORD, ARCHIVE_ID, {PROVIDER_1: {**ENDPOINTS[PROVIDER_1], "auto_append_path": "yes"}})


def test_a_lone_surrogate_is_not_a_password() -> None:
    assert kdf.password_problem("Abcdefghij1!\ud800") is not None, "no key can be derived from it (and it must not crash)"


def test_seal_validates_its_input() -> None:
    with pytest.raises(ValueError):
        kdf.seal_secrets({}, PASSWORD, ARCHIVE_ID)
    with pytest.raises(ValueError):
        kdf.seal_secrets({"p": ""}, PASSWORD, ARCHIVE_ID)
    with pytest.raises(ValueError):
        kdf.seal_secrets(KEYS, PASSWORD, "not-an-id")
    with pytest.raises(ArchivePasswordRuleError):
        kdf.seal_secrets(KEYS, "weak", ARCHIVE_ID)


def test_unicode_passwords_open_with_either_normalization() -> None:
    composed = "Café-Latte-2024"
    decomposed = "Cafe\u0301-Latte-2024"
    blob = kdf.seal_secrets(KEYS, composed, ARCHIVE_ID)
    assert kdf.open_secrets(blob, decomposed, ARCHIVE_ID) == KEYS


def test_the_key_blob_does_not_follow_the_version_of_the_archive_format() -> None:
    """"API Key 沿用 v0.13 规范": the way keys are sealed is the same in a format 1 and a format 2 archive (the blob has its own version)."""
    assert SECRETS_FORMAT_VERSION == 1 and FORMAT_VERSION == 2
    header = json.loads(kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID))
    assert header["version"] == 1
    # a blob exactly as v0.13 wrote it still opens: its header says version 1 and its associated data binds format_version 1
    assert json.loads(base64.b64decode(header["aad"]))["format_version"] == 1
    assert kdf.open_secrets(kdf.seal_secrets(KEYS, PASSWORD, ARCHIVE_ID), PASSWORD, ARCHIVE_ID) == KEYS
