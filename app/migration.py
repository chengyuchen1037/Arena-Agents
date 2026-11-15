from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from app.providers.store import ProviderStore, get_provider_store
from app.storage._atomic import atomic_write, ensure_private_directory
from app.storage._validation import validate_session_id
from app.storage.errors import StorageError
from app.storage.sessions import SessionStore, get_session_store
from app.storage.settings import get_settings_store

_LEGACY_PROVIDER_NAME = "DeepSeek（旧配置迁移）"
_DEEPSEEK_LINE = re.compile(r"^\s*(?:export\s+)?DEEPSEEK_[A-Z0-9_]+\s*=")
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _new_report() -> dict[str, Any]:
    return {
        "status": "ok",
        "provider_status": "not-needed",
        "providers_migrated": 0,
        "sessions_migrated": 0,
        "sessions_failed": 0,
        "migrated_session_files": [],
        "quarantined_session_files": [],
        "failed_files": [],
        "env_updated": False,
    }


def _mark_failed(report: dict[str, Any], filename: str) -> None:
    if filename not in report["failed_files"]:
        report["failed_files"].append(filename)
    report["status"] = "partial"


def _legacy_llm_config(cfg: dict, dotenv_cfg: dict[str, str]) -> dict[str, Any]:
    llm_cfg = cfg.get("llm") if isinstance(cfg, dict) else {}
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
    pricing = llm_cfg.get("pricing")
    if not isinstance(pricing, dict):
        pricing = {}
    return {
        "base_url": dotenv_cfg.get("DEEPSEEK_BASE_URL")
        or llm_cfg.get("base_url")
        or "https://api.deepseek.com",
        "model_id": dotenv_cfg.get("DEEPSEEK_MODEL")
        or llm_cfg.get("default_model")
        or "deepseek-flash",
        "input_price_per_m": pricing.get("input_price_per_m", 0.27),
        "output_price_per_m": pricing.get("output_price_per_m", 1.10),
        "context_window_tokens": llm_cfg.get("context_window_tokens", 1_000_000),
    }


def _rewrite_env_without_deepseek(env_path: Path) -> None:
    original = env_path.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Legacy .env is not UTF-8") from exc
    retained = [
        line
        for line in text.splitlines(keepends=True)
        if not _DEEPSEEK_LINE.match(line)
    ]
    atomic_write(
        env_path,
        "".join(retained).encode("utf-8"),
        secure_directory=False,
    )


def _clear_migrated_environment(dotenv_cfg: dict[str, str]) -> None:
    """Remove only values that actually came from the migrated dotenv file."""
    for key, migrated_value in dotenv_cfg.items():
        if (
            key.startswith("DEEPSEEK_")
            and key in os.environ
            and os.environ[key] == migrated_value
        ):
            os.environ.pop(key, None)


def _migrate_provider(
    base_dir: Path, cfg: dict, report: dict[str, Any]
) -> None:
    env_path = base_dir / ".env"
    if not env_path.exists():
        return
    if env_path.is_symlink() or not env_path.is_file():
        _mark_failed(report, ".env")
        report["provider_status"] = "failed"
        return
    try:
        dotenv_cfg = {
            key: value
            for key, value in dotenv_values(env_path).items()
            if value is not None
        }
    except Exception:
        _mark_failed(report, ".env")
        report["provider_status"] = "failed"
        return

    api_key = dotenv_cfg.get("DEEPSEEK_API_KEY")
    if api_key is None or not api_key.strip():
        return

    try:
        legacy = _legacy_llm_config(cfg, dotenv_cfg)
        store = get_provider_store()
        existing_ids = {
            provider.id
            for provider in store.list_providers()
            if provider.source == "legacy-migration"
        }
        provider = store.import_legacy_provider(
            name=_LEGACY_PROVIDER_NAME,
            base_url=str(legacy["base_url"]),
            api_key=api_key,
            model_id=str(legacy["model_id"]),
            input_price_per_m=legacy["input_price_per_m"],
            output_price_per_m=legacy["output_price_per_m"],
            context_window_tokens=legacy["context_window_tokens"],
        )
        created = provider.id not in existing_ids

        # Re-open the ciphertext through a separate store before changing .env.
        verified = ProviderStore(store.data_dir).get_provider(provider.id)
        if (
            verified.source != "legacy-migration"
            or verified.protocol != "openai_chat"
            or verified.base_url != legacy["base_url"]
            or verified.api_key != api_key
        ):
            raise RuntimeError("Provider migration verification failed")
        model = next(
            (item for item in verified.models if item.id == legacy["model_id"]), None
        )
        if model is None:
            raise RuntimeError("Provider model migration verification failed")
        if created and (
            model.input_price_per_m != float(legacy["input_price_per_m"])
            or model.output_price_per_m != float(legacy["output_price_per_m"])
            or model.context_window_tokens != int(legacy["context_window_tokens"])
        ):
            raise RuntimeError("Provider pricing migration verification failed")

        report["providers_migrated"] = 1 if created else 0
        report["provider_status"] = "migrated" if created else "already-migrated"
    except Exception:
        _mark_failed(report, ".env")
        report["provider_status"] = "failed"
        return

    try:
        _rewrite_env_without_deepseek(env_path)
        report["env_updated"] = True
    except Exception:
        _mark_failed(report, ".env")
        return
    _clear_migrated_environment(dotenv_cfg)


def _read_legacy_session(path: Path) -> tuple[str, dict, float]:
    if path.is_symlink():
        raise ValueError("Refusing symbolic-link legacy saves")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Legacy session must be a JSON object")
    session_id = payload.get("session_id") or path.stem
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Legacy session has no usable session_id")
    validate_session_id(session_id)
    return session_id, payload, path.stat().st_mtime


def _duplicate_session_id(original_id: str, path: Path, payload: dict) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(original_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(canonical)
    return f"legacy-{digest.hexdigest()[:12]}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _move_to_quarantine(path: Path, data_dir: Path) -> str:
    """Copy+fsync+verify outside the project, then remove the legacy original."""
    quarantine_dir = data_dir / "quarantine"
    ensure_private_directory(quarantine_dir)
    legacy_dir = quarantine_dir / "legacy-saves"
    ensure_private_directory(legacy_dir)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = legacy_dir / f"{timestamp}_{path.name}"
    created = False
    try:
        with path.open("rb") as source:
            with destination.open("xb") as target:
                created = True
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
        if os.name != "nt":
            destination.chmod(0o600)
        shutil.copystat(path, destination)
        if _sha256(path) != _sha256(destination):
            raise StorageError("Legacy save quarantine verification failed")
        path.unlink()
        return destination.name
    except Exception:
        # A failed move must leave the project original in place. Remove only a
        # partial quarantine copy created by this operation, never user data.
        if created and path.exists():
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _migrate_sessions(base_dir: Path, report: dict[str, Any]) -> None:
    saves_dir = base_dir / "saves"
    if not saves_dir.exists():
        return
    if saves_dir.is_symlink() or not saves_dir.is_dir():
        _mark_failed(report, "saves")
        return
    try:
        paths = sorted(
            path
            for path in saves_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".json"
        )
    except OSError:
        _mark_failed(report, "saves")
        return

    groups: dict[str, list[tuple[Path, dict, float]]] = {}
    for path in paths:
        try:
            session_id, payload, mtime = _read_legacy_session(path)
            groups.setdefault(session_id, []).append((path, payload, mtime))
        except Exception:
            report["sessions_failed"] += 1
            _mark_failed(report, path.name)

    planned: list[tuple[Path, str, dict, float]] = []
    for original_id in sorted(groups):
        group = groups[original_id]
        newest = max(group, key=lambda item: (item[2], item[0].name))
        newest_path, newest_payload, newest_mtime = newest
        planned.append((newest_path, original_id, newest_payload, newest_mtime))
        older = sorted(
            (item for item in group if item is not newest),
            key=lambda item: (item[2], item[0].name),
            reverse=True,
        )
        for path, payload, mtime in older:
            new_id = _duplicate_session_id(original_id, path, payload)
            payload["session_id"] = new_id
            planned.append((path, new_id, payload, mtime))

    migrated: list[tuple[float, str, str]] = []
    for path, session_id, payload, mtime in planned:
        try:
            store = get_session_store()
            if store.exists(session_id):
                if store.load(session_id) != payload:
                    raise ValueError("Refusing to overwrite a different external session")
            else:
                store.save(session_id, payload)
            verified = SessionStore(store.data_dir).load(session_id)
            if verified != payload:
                raise RuntimeError("Session migration verification failed")
            quarantined_name = _move_to_quarantine(path, store.data_dir)
            migrated.append((mtime, path.name, session_id))
            report["sessions_migrated"] += 1
            report["migrated_session_files"].append(path.name)
            report["quarantined_session_files"].append(quarantined_name)
        except Exception:
            report["sessions_failed"] += 1
            _mark_failed(report, path.name)

    if not migrated:
        return
    try:
        settings = get_settings_store()
        if settings.get_last_session_id() is None:
            _, _, latest_session_id = max(
                migrated, key=lambda item: (item[0], item[1])
            )
            settings.set_last_session_id(latest_session_id)
    except Exception:
        _mark_failed(report, "settings.json")


def migrate_legacy_data(base_dir: Path, cfg: dict) -> dict:
    """Migrate legacy local secrets and saves without exposing sensitive values."""
    report = _new_report()
    disable_value = os.environ.get("ARENA_DISABLE_LEGACY_MIGRATION", "")
    if disable_value.strip().lower() in _TRUE_VALUES:
        report["status"] = "disabled"
        return report

    base_dir = Path(base_dir)
    _migrate_provider(base_dir, cfg, report)
    _migrate_sessions(base_dir, report)
    return report


__all__ = ["migrate_legacy_data"]
