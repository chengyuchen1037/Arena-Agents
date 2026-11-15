from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.storage._atomic import atomic_write, ensure_private_directory
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir

_FORMAT = "arena-agent-aes-gcm"
_FORMAT_VERSION = 1
_KEY_VERSION = 1
_NONCE_BYTES = 12
_KEY_BYTES = 32
_DPAPI_WRAPPER_NAME = "master.v1.dpapi"
_DPAPI_UI_FORBIDDEN = 0x1

TestKeyProvider = Callable[[Path], bytes]
_test_key_provider: TestKeyProvider | None = None
_master_key_cache: dict[Path, tuple[int, int, bytes]] = {}
_key_lock = threading.RLock()


def _decode_base64(value: str, *, invalid_message: str) -> bytes:
    try:
        encoded = value.strip().encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        return base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise StorageError(invalid_message) from exc


def _encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _validate_master_key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
        raise StorageError("Arena master key must contain exactly 32 bytes")
    return key


def _environment_master_key() -> bytes | None:
    configured = os.environ.get("ARENA_MASTER_KEY")
    if configured and configured.strip():
        return _validate_master_key(
            _decode_base64(configured, invalid_message="Invalid Arena master key encoding")
        )

    key_file = os.environ.get("ARENA_MASTER_KEY_FILE")
    if key_file and key_file.strip():
        try:
            encoded = Path(key_file).expanduser().read_text(encoding="ascii")
        except (OSError, UnicodeError) as exc:
            raise StorageError("Unable to read the Arena master key file") from exc
        return _validate_master_key(
            _decode_base64(encoded, invalid_message="Invalid Arena master key file")
        )
    return None


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _make_blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise StorageError("DPAPI is only available on Windows")
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        input_blob, input_buffer = _make_blob(data)
        output_blob = _DataBlob()
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        succeeded = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "Arena-Agent master key",
            None,
            None,
            None,
            _DPAPI_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        if not succeeded:
            raise StorageError("Windows DPAPI could not protect the master key")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError("Windows DPAPI is unavailable") from exc
    finally:
        _ = input_buffer if "input_buffer" in locals() else None


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise StorageError("DPAPI is only available on Windows")
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        input_blob, input_buffer = _make_blob(data)
        output_blob = _DataBlob()
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        succeeded = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            _DPAPI_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        if not succeeded:
            raise StorageError("Windows DPAPI could not unlock the master key")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError("Windows DPAPI could not unlock the master key") from exc
    finally:
        _ = input_buffer if "input_buffer" in locals() else None


def _read_dpapi_key(path: Path) -> bytes:
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(wrapper, dict)
            or wrapper.get("version") != _KEY_VERSION
            or wrapper.get("scope") != "CurrentUser"
            or not isinstance(wrapper.get("protected_key"), str)
        ):
            raise StorageError("Invalid DPAPI master key wrapper")
        protected = _decode_base64(
            wrapper["protected_key"], invalid_message="Invalid DPAPI master key wrapper"
        )
        return _validate_master_key(_dpapi_unprotect(protected))
    except StorageError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError("Unable to read the DPAPI master key wrapper") from exc


def _dpapi_master_key(data_dir: Path) -> bytes:
    ensure_private_directory(data_dir)
    wrapper_path = data_dir / _DPAPI_WRAPPER_NAME
    with _key_lock:
        try:
            stat = wrapper_path.stat()
        except FileNotFoundError:
            stat = None
        except OSError as exc:
            raise StorageError("Unable to inspect the DPAPI master key wrapper") from exc

        if stat is not None:
            cached = _master_key_cache.get(data_dir)
            if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                return cached[2]
            key = _read_dpapi_key(wrapper_path)
            _master_key_cache[data_dir] = (stat.st_mtime_ns, stat.st_size, key)
            return key

        if wrapper_path.exists():
            raise StorageError("Invalid DPAPI master key wrapper")
        key = AESGCM.generate_key(bit_length=256)
        wrapper = {
            "version": _KEY_VERSION,
            "scope": "CurrentUser",
            "protected_key": _encode_base64(_dpapi_protect(key)),
        }
        encoded = json.dumps(wrapper, sort_keys=True, separators=(",", ":")).encode("utf-8")
        atomic_write(wrapper_path, encoded)
        new_stat = wrapper_path.stat()
        _master_key_cache[data_dir] = (new_stat.st_mtime_ns, new_stat.st_size, key)
        return key


def _get_master_key(data_dir: Path) -> bytes:
    if _test_key_provider is not None:
        return _validate_master_key(_test_key_provider(data_dir))
    environment_key = _environment_master_key()
    if environment_key is not None:
        return environment_key
    if os.name == "nt":
        return _dpapi_master_key(data_dir)
    raise StorageError(
        "Encrypted storage needs ARENA_MASTER_KEY or ARENA_MASTER_KEY_FILE on this platform"
    )


def _aad(purpose: str) -> bytes:
    return json.dumps(
        {
            "format": _FORMAT,
            "version": _FORMAT_VERSION,
            "key_version": _KEY_VERSION,
            "purpose": purpose,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def encrypt_data(
    plaintext: bytes, purpose: str, *, data_dir: Path | None = None
) -> bytes:
    if not isinstance(plaintext, bytes):
        raise TypeError("plaintext must be bytes")
    if not isinstance(purpose, str) or not purpose:
        raise ValueError("encryption purpose must not be empty")
    root = (data_dir or get_data_dir()).resolve()
    key = _get_master_key(root)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _aad(purpose))
    envelope = {
        "format": _FORMAT,
        "version": _FORMAT_VERSION,
        "key_version": _KEY_VERSION,
        "purpose": purpose,
        "nonce": _encode_base64(nonce),
        "ciphertext": _encode_base64(ciphertext),
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decrypt_data(
    encrypted: bytes, purpose: str, *, data_dir: Path | None = None
) -> bytes:
    if not isinstance(encrypted, bytes):
        raise TypeError("encrypted data must be bytes")
    try:
        envelope = json.loads(encrypted.decode("utf-8"))
        if (
            not isinstance(envelope, dict)
            or envelope.get("format") != _FORMAT
            or envelope.get("version") != _FORMAT_VERSION
            or envelope.get("key_version") != _KEY_VERSION
            or envelope.get("purpose") != purpose
            or not isinstance(envelope.get("nonce"), str)
            or not isinstance(envelope.get("ciphertext"), str)
        ):
            raise StorageError("Invalid encrypted data envelope or purpose")
        nonce = _decode_base64(
            envelope["nonce"], invalid_message="Invalid encrypted data nonce"
        )
        ciphertext = _decode_base64(
            envelope["ciphertext"], invalid_message="Invalid encrypted data payload"
        )
        if len(nonce) != _NONCE_BYTES or len(ciphertext) < 16:
            raise StorageError("Truncated encrypted data")
    except StorageError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError("Invalid encrypted data envelope") from exc

    root = (data_dir or get_data_dir()).resolve()
    key = _get_master_key(root)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, _aad(purpose))
    except InvalidTag as exc:
        raise StorageError("Encrypted data authentication failed") from exc


def encrypt_json(
    payload: Any, purpose: str, *, data_dir: Path | None = None
) -> bytes:
    try:
        plaintext = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StorageError("Payload is not valid JSON data") from exc
    return encrypt_data(plaintext, purpose, data_dir=data_dir)


def decrypt_json(
    encrypted: bytes, purpose: str, *, data_dir: Path | None = None
) -> Any:
    plaintext = decrypt_data(encrypted, purpose, data_dir=data_dir)
    try:
        return json.loads(plaintext.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError("Decrypted payload is not valid JSON") from exc


def set_key_provider_for_tests(provider: TestKeyProvider | None) -> None:
    global _test_key_provider
    with _key_lock:
        _test_key_provider = provider
        _master_key_cache.clear()


def reset_crypto_for_tests() -> None:
    global _test_key_provider
    with _key_lock:
        _test_key_provider = None
        _master_key_cache.clear()


__all__ = [
    "StorageError",
    "atomic_write",
    "decrypt_data",
    "decrypt_json",
    "encrypt_data",
    "encrypt_json",
    "reset_crypto_for_tests",
    "set_key_provider_for_tests",
]
