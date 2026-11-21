"""The password rule and ``secrets.enc``: the API keys of an archive, encrypted with a password.

``secrets.enc`` is a JSON object (everything but the ciphertext is plain, nothing in it is secret)::

    {"format": "arenaagents", "version": 1,
     "kdf": "scrypt", "n": 131072, "r": 8, "p": 1, "salt": <16 bytes>,
     "cipher": "aes-256-gcm", "nonce": <12 bytes>, "aad": <the authenticated header>,
     "ciphertext": <AES-256-GCM of {"schema_version": 1, "keys": {provider_id: api_key},
                                   "endpoints": {provider_id: {"protocol", "base_url", "auto_append_path"}}}>}

(bytes are standard base64).  The key is ``scrypt(NFC(password), salt, n=2**17, r=8, p=1) -> 32 bytes``.  The AEAD's
associated data is the JSON ``{"archive_id", "format", "format_version", "purpose": "secrets"}`` (sorted keys, compact), so
a ``secrets.enc`` cannot be moved into another archive or be read as another format version.

The ``version`` here is the version of *this blob* (:data:`SECRETS_FORMAT_VERSION`), not of the archive that carries it: the way the keys are
sealed did not change when the archive format went from 1 to 2 (v0.14), so a v0.13 archive's keys and a v0.14 archive's keys are the same
format - "API Key 沿用 v0.13 规范".  A wrong password and a
changed file fail the same way, with the same message: AES-GCM cannot tell them apart and neither may we.

The ``endpoints`` bind every key to the place it may be sent to: ``providers.json`` is not encrypted, so somebody who can
change the file could point a provider at his own server and let the owner's password open the key for it.  The importer
compares the sealed endpoints with the ones in ``providers.json`` and refuses to import the keys when they differ.

Neither the password nor the derived key is ever logged, put into an exception or kept after the call.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import unicodedata
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from app.archive.errors import ArchiveFormatError, ArchivePasswordError, ArchivePasswordRuleError
from app.archive.format import ARCHIVE_ID_RE, FORMAT_NAME

# ---------------------------------------------------------------------------------------- password rule
#: The one rule for a new archive password; ``static/js/archive.js`` implements the same function and both are
#: tested against ``tests/archive/password_cases.json``.
MIN_PASSWORD_CHARS = 12
MAX_PASSWORD_CHARS = 256
MIN_PASSWORD_CLASSES = 3
PASSWORD_RULE_TEXT = f"至少 {MIN_PASSWORD_CHARS} 位，并且至少包含大写字母、小写字母、数字、符号中的三类"

CLASS_NAMES = ("upper", "lower", "digit", "symbol")


def _char_class(ch: str) -> str:
    if "A" <= ch <= "Z":
        return "upper"
    if "a" <= ch <= "z":
        return "lower"
    if "0" <= ch <= "9":
        return "digit"
    return "symbol"  # every other character counts: punctuation, spaces, non-ASCII letters, CJK, emoji


def normalize_password(password: str) -> str:
    """NFC, so the same typed password derives the same key on every platform and keyboard."""
    return unicodedata.normalize("NFC", password)


def password_classes(password: str) -> list[str]:
    """Which of ``upper`` / ``lower`` / ``digit`` / ``symbol`` the password contains."""
    present = {_char_class(ch) for ch in normalize_password(password)}
    return [name for name in CLASS_NAMES if name in present]


def password_problem(password: object) -> str | None:
    """The reason a new password is refused (Chinese, no echo of the password), or ``None`` when it is fine."""
    if not isinstance(password, str):
        return "请输入密码。"
    text = normalize_password(password)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate (a JSON body can carry one): no key can be derived from it
        return "密码包含无法处理的字符。"
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in text):
        return "密码不能包含控制字符。"
    if len(text) > MAX_PASSWORD_CHARS:
        return f"密码最长 {MAX_PASSWORD_CHARS} 位。"
    if len(text) < MIN_PASSWORD_CHARS:
        return f"密码至少需要 {MIN_PASSWORD_CHARS} 位（现在 {len(text)} 位）。"
    classes = password_classes(text)
    if len(classes) < MIN_PASSWORD_CLASSES:
        return f"密码需要包含大写字母、小写字母、数字、符号中的至少 {MIN_PASSWORD_CLASSES} 类（现在 {len(classes)} 类）。"
    return None


def check_password(password: object) -> str:
    """The normalized password when it meets the rule; :class:`ArchivePasswordRuleError` otherwise."""
    problem = password_problem(password)
    if problem is not None:
        raise ArchivePasswordRuleError(problem)
    return normalize_password(str(password))


# --------------------------------------------------------------------------------------- parameters
SCRYPT_N = 2**17  # 128 MiB with r=8; tests lower this (module global, read on every call) to stay fast
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32
MAX_KEYS = 1000
MAX_KEY_CHARS = 8192
SECRETS_SCHEMA_VERSION = 1
#: the version of ``secrets.enc`` itself (its header and its associated data): unchanged since v0.13, whatever the archive's format version is
SECRETS_FORMAT_VERSION = 1
#: what decides where a request (and so the key in it) goes: sealed together with the key, compared on import
ENDPOINT_FIELDS = ("protocol", "base_url", "auto_append_path")
_HEADER_FIELDS = {"format", "version", "kdf", "n", "r", "p", "salt", "cipher", "nonce", "aad", "ciphertext"}


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: Any, *, length: int | None = None, minimum: int = 0, maximum: int = 1 << 20) -> bytes:
    if not isinstance(value, str):
        raise ValueError("not base64")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise ValueError("not base64") from None
    if (length is not None and len(raw) != length) or len(raw) < minimum or len(raw) > maximum:
        raise ValueError("bad length")
    return raw


def aad_for(archive_id: str, *, format_name: str = FORMAT_NAME, version: int = SECRETS_FORMAT_VERSION) -> bytes:
    """The associated data AES-GCM authenticates together with the ciphertext."""
    return json.dumps(
        {"archive_id": archive_id, "format": format_name, "format_version": version, "purpose": "secrets"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    kdf = Scrypt(salt=salt, length=KEY_BYTES, n=n, r=r, p=p)
    return kdf.derive(normalize_password(password).encode("utf-8"))


@dataclass(frozen=True)
class SecretsHeader:
    format: str
    version: int
    n: int
    r: int
    p: int
    salt: bytes
    nonce: bytes
    aad: bytes
    ciphertext: bytes


def parse_secrets_header(blob: bytes) -> SecretsHeader:
    """Check the *shape* of a ``secrets.enc`` (no password needed): the fixed parameters, the lengths, the base64."""
    bad = ArchiveFormatError("secrets.enc 的格式不正确。")
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise bad from None
    if not isinstance(data, dict) or set(data) != _HEADER_FIELDS:
        raise bad
    try:
        n, r, p, version = data["n"], data["r"], data["p"], data["version"]
        if (
            data["format"] != FORMAT_NAME
            or version != SECRETS_FORMAT_VERSION
            or data["kdf"] != "scrypt"
            or data["cipher"] != "aes-256-gcm"
            or any(isinstance(v, bool) or not isinstance(v, int) for v in (n, r, p, version))
            # the cost parameters are part of the format: a file must not make the reader burn memory / time
            or (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
        ):
            raise bad
        return SecretsHeader(
            format=data["format"],
            version=version,
            n=n,
            r=r,
            p=p,
            salt=_unb64(data["salt"], length=SALT_BYTES),
            nonce=_unb64(data["nonce"], length=NONCE_BYTES),
            aad=_unb64(data["aad"], minimum=2, maximum=1024),
            ciphertext=_unb64(data["ciphertext"], minimum=16, maximum=4 * 1024 * 1024),
        )
    except ValueError:
        raise bad from None


def _validate_keys(keys: Any) -> dict[str, str]:
    if not isinstance(keys, dict) or not keys:
        raise ValueError("no keys")
    if len(keys) > MAX_KEYS:
        raise ValueError("too many keys")
    clean: dict[str, str] = {}
    for provider_id, key in keys.items():
        if not isinstance(provider_id, str) or not provider_id or not isinstance(key, str) or not key:
            raise ValueError("bad key entry")
        if len(provider_id) > 256 or len(key) > MAX_KEY_CHARS:
            raise ValueError("key too long")
        clean[provider_id] = key
    return clean


def _validate_endpoints(endpoints: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(endpoints, dict) or len(endpoints) > MAX_KEYS:
        raise ValueError("bad endpoints")
    clean: dict[str, dict[str, Any]] = {}
    for provider_id, endpoint in endpoints.items():
        if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 256:
            raise ValueError("bad endpoint id")
        if not isinstance(endpoint, dict) or set(endpoint) != set(ENDPOINT_FIELDS):
            raise ValueError("bad endpoint")
        if not (
            isinstance(endpoint["protocol"], str)
            and isinstance(endpoint["base_url"], str)
            and len(endpoint["base_url"]) <= 4096
            and isinstance(endpoint["auto_append_path"], bool)
        ):
            raise ValueError("bad endpoint field")
        clean[provider_id] = {name: endpoint[name] for name in ENDPOINT_FIELDS}
    return clean


@dataclass(frozen=True)
class SealedSecrets:
    """What a ``secrets.enc`` holds: the keys and the endpoint each of them was sealed for (``{}`` when it has none)."""

    keys: dict[str, str]
    endpoints: dict[str, dict[str, Any]]


def seal_secrets(
    keys: dict[str, str], password: str, archive_id: str, endpoints: dict[str, dict[str, Any]] | None = None
) -> bytes:
    """Encrypt ``{provider_id: api_key}`` for an archive: the content of ``secrets.enc``.  ``endpoints`` gives, for every
    key, the provider's ``protocol`` / ``base_url`` / ``auto_append_path`` it is bound to (the exporter always passes them)."""
    if not isinstance(archive_id, str) or not ARCHIVE_ID_RE.fullmatch(archive_id):
        raise ValueError("archive_id must be 32 lowercase hex digits")
    text = check_password(password)
    try:
        clean = _validate_keys(keys)
        bound = _validate_endpoints(endpoints if endpoints is not None else {})
    except ValueError:
        raise ValueError("keys must be a non-empty {provider_id: api_key} mapping (and endpoints a mapping of the three fields)") from None
    if endpoints is not None and set(bound) != set(clean):
        raise ValueError("every key needs exactly one endpoint")
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    aad = aad_for(archive_id)
    plaintext = json.dumps(
        {"schema_version": SECRETS_SCHEMA_VERSION, "keys": clean, "endpoints": bound},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    cipher_key = _derive(text, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    ciphertext = AESGCM(cipher_key).encrypt(nonce, plaintext, aad)
    header = {
        "format": FORMAT_NAME,
        "version": SECRETS_FORMAT_VERSION,
        "kdf": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": _b64(salt),
        "cipher": "aes-256-gcm",
        "nonce": _b64(nonce),
        "aad": _b64(aad),
        "ciphertext": _b64(ciphertext),
    }
    return (json.dumps(header, indent=1) + "\n").encode("ascii")


def open_secrets(blob: bytes, password: str, archive_id: str) -> dict[str, str]:
    """The keys of a ``secrets.enc`` (see :func:`open_secrets_bound`)."""
    return open_secrets_bound(blob, password, archive_id).keys


def open_secrets_bound(blob: bytes, password: str, archive_id: str) -> SealedSecrets:
    """Decrypt a ``secrets.enc``.  A wrong password, a tampered file, a foreign archive id and a malformed file all
    raise the same :class:`ArchivePasswordError`."""
    try:
        if not isinstance(password, str) or not password or len(password) > 4 * MAX_PASSWORD_CHARS:
            raise ArchivePasswordError()
        if not isinstance(archive_id, str) or not ARCHIVE_ID_RE.fullmatch(archive_id):
            raise ArchivePasswordError()
        header = parse_secrets_header(blob)
        if not hmac.compare_digest(header.aad, aad_for(archive_id, format_name=header.format, version=header.version)):
            raise ArchivePasswordError()
        cipher_key = _derive(password, header.salt, header.n, header.r, header.p)
        plaintext = AESGCM(cipher_key).decrypt(header.nonce, header.ciphertext, header.aad)
        data = json.loads(plaintext.decode("utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != SECRETS_SCHEMA_VERSION:
            raise ValueError("schema")
        keys = _validate_keys(data.get("keys"))
        endpoints = _validate_endpoints(data.get("endpoints", {}))
        return SealedSecrets(keys=keys, endpoints=endpoints)
    except (ArchivePasswordError, ArchiveFormatError, InvalidTag, ValueError, UnicodeError, RecursionError):
        raise ArchivePasswordError() from None
