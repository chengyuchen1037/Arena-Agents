from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.providers.models import ModelBinding, ModelTemplate
from app.storage._atomic import atomic_write
from app.storage._validation import validate_session_id
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir

_SETTINGS_FILE = "settings.json"
_SCHEMA_VERSION = 2
# v0.12.0 named the seats ``p01``..``p12``; v0.12.1 uses ``N1``..``N12``.  Bindings saved under the old
# names are read as the new ones where the game has such a seat (the file itself is only rewritten when the
# user changes the bindings).
_LEGACY_ACTOR_ID = re.compile(r"^p(\d{2})$")
# v0.14 (H4): the model binding follows the *bot*, not the seat.  ``default_bindings`` of ``settings.json`` is keyed by bot id
# (``b-1a2b3c4d``) and ``moderator``; callers (the orchestrator, ``/api/settings/bindings``) still speak in seats (``N1``..)
# - :meth:`SettingsStore.get_default_bindings` and :meth:`SettingsStore.save_default_bindings` translate through the line-up
# of the data directory (``bots/lineup.json``).  A key that is a seat (what v0.13 saved) is moved to the bot by
# :meth:`SettingsStore.migrate_seat_bindings`; until then it is read as the binding of the bot in that seat.
_SEAT_KEY = re.compile(r"^(?:N([1-9][0-9]?)|p(\d{2}))$")
_BOT_ID_KEY = re.compile(r"^b-[0-9a-f]{8}$")


def _seat_number(key: str) -> int | None:
    match = _SEAT_KEY.match(key)
    if not match:
        return None
    number = int(match.group(1) or match.group(2))
    return number if number >= 1 else None


def migrate_legacy_actor_ids(bindings: dict[str, Any], known_ids: Any) -> dict[str, Any]:
    """``{"p03": b}`` -> ``{"N3": b}`` for the seats in ``known_ids`` that have no binding under the new name.

    A key that is itself a seat of the game (a custom ``config.yml`` may still use ``p03``) is left alone."""
    known = set(known_ids)
    migrated: dict[str, Any] = {}
    renamed: dict[str, Any] = {}
    for actor_id, binding in bindings.items():
        match = _LEGACY_ACTOR_ID.match(actor_id)
        new_id = f"N{int(match.group(1))}" if match else None
        if new_id is not None and actor_id not in known and new_id in known:
            renamed[new_id] = binding
        else:
            migrated[actor_id] = binding
    for actor_id, binding in renamed.items():
        migrated.setdefault(actor_id, binding)
    return migrated


def _default_settings() -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "default_bindings": {},
        "last_session_id": None,
        "ui": {},
        "model_templates": [],
    }


def _validate_templates(raw_templates: Any) -> list[ModelTemplate]:
    if not isinstance(raw_templates, list):
        raise StorageError("Invalid model templates in settings")
    try:
        templates = [ModelTemplate.model_validate(item) for item in raw_templates]
    except (ValidationError, TypeError) as exc:
        raise StorageError("Invalid model template in external settings") from exc
    slots = [template.slot for template in templates]
    if len(slots) != len(set(slots)):
        raise StorageError("Model template slots must be unique")
    return templates


class SettingsStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = (data_dir or get_data_dir()).resolve()
        self._path = self._data_dir / _SETTINGS_FILE
        self._lock = threading.RLock()

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return _default_settings()
        if self._path.is_symlink():
            raise StorageError("Refusing to read a symbolic-link settings file")
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise StorageError("Invalid settings schema")
            schema_version = payload.get("schema_version")
            if schema_version not in (1, _SCHEMA_VERSION):
                raise StorageError("Invalid settings schema")
            if (
                not isinstance(payload.get("default_bindings"), dict)
                or not isinstance(payload.get("ui"), dict)
                or "last_session_id" not in payload
            ):
                raise StorageError("Invalid settings schema")
            raw_templates = payload.get("model_templates", [])
            if schema_version == _SCHEMA_VERSION and "model_templates" not in payload:
                raise StorageError("Invalid settings schema")
            templates = _validate_templates(raw_templates)
            bindings = payload["default_bindings"]
            for actor_id, binding in bindings.items():
                if not isinstance(actor_id, str) or not actor_id:
                    raise StorageError("Invalid actor id in settings")
                ModelBinding.model_validate(binding)
            last_session_id = payload["last_session_id"]
            if last_session_id is not None:
                validate_session_id(last_session_id)
            return {
                "schema_version": _SCHEMA_VERSION,
                "default_bindings": copy.deepcopy(bindings),
                "last_session_id": last_session_id,
                "ui": copy.deepcopy(payload["ui"]),
                "model_templates": [
                    template.model_dump(mode="json") for template in templates
                ],
            }
        except StorageError:
            raise
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise StorageError("Unable to read external settings") from exc

    def _write(self, payload: dict[str, Any]) -> None:
        payload["schema_version"] = _SCHEMA_VERSION
        payload.setdefault("model_templates", [])
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise StorageError("Settings are not valid JSON data") from exc
        atomic_write(self._path, encoded)
        try:
            verified = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StorageError("Unable to verify external settings") from exc
        if verified != payload:
            raise StorageError("External settings verification failed")

    def get_settings(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._read())

    def _lineup_ids(self) -> list[str]:
        """The bot ids of the line-up of this data directory by seat, ``[]`` when there are no bots (yet)."""
        try:
            from app.storage.bots import read_lineup_ids

            return read_lineup_ids(self._data_dir)
        except Exception:  # noqa: BLE001 - without a line-up the bindings are read as they are stored
            return []

    def _seat_view(self, raw_bindings: dict[str, Any]) -> dict[str, Any]:
        """``default_bindings`` as the callers see it: keyed by seat id (``N1``..) for the bot in that seat, ``moderator`` and any
        other key as stored.  A bot that has no seat (not in the line-up) is not shown; a binding stored under a bot id wins
        over a v0.13 entry stored under the seat id."""
        lineup = self._lineup_ids()
        if not lineup:
            return dict(raw_bindings)
        seat_of_bot = {bot_id: f"N{index}" for index, bot_id in enumerate(lineup, start=1)}
        view: dict[str, Any] = {}
        for key, binding in raw_bindings.items():
            if key not in seat_of_bot and not _BOT_ID_KEY.match(key):
                view[key] = binding
        for key, binding in raw_bindings.items():
            if key in seat_of_bot:
                view[seat_of_bot[key]] = binding
        return view

    def get_default_bindings(self) -> dict[str, ModelBinding]:
        """The default model bindings, keyed by the seat they apply to now (``N1``..: the binding of the bot that sits there) and
        ``moderator``.  They are *stored* by bot id (H4), so moving a bot to another seat moves its binding along."""
        with self._lock:
            raw_bindings = self._seat_view(self._read()["default_bindings"])
            try:
                return {
                    actor_id: ModelBinding.model_validate(binding)
                    for actor_id, binding in raw_bindings.items()
                }
            except ValidationError as exc:
                raise StorageError("Invalid model binding in external settings") from exc

    def save_default_bindings(self, bindings: dict[str, ModelBinding]) -> None:
        """Replace the default bindings.  ``bindings`` is keyed by seat (``N1``..) and ``moderator`` like
        :meth:`get_default_bindings`; a seat is stored under the id of the bot that sits there (without bots: as it is)."""
        if not isinstance(bindings, dict):
            raise TypeError("bindings must be a dict")
        serialized: dict[str, dict] = {}
        try:
            for actor_id, binding in bindings.items():
                if not isinstance(actor_id, str) or not actor_id:
                    raise ValueError("actor id must not be empty")
                if not isinstance(binding, ModelBinding):
                    binding = ModelBinding.model_validate(binding)
                serialized[actor_id] = binding.model_dump(mode="json")
        except (ValidationError, TypeError, ValueError) as exc:
            raise ValueError("Invalid default model bindings") from exc
        lineup = self._lineup_ids()
        if lineup:
            by_bot: dict[str, dict] = {}
            for actor_id, value in serialized.items():
                number = _seat_number(actor_id) if actor_id.startswith("N") else None
                if number is not None and number <= len(lineup):
                    by_bot[lineup[number - 1]] = value
                else:
                    by_bot[actor_id] = value
            serialized = by_bot
        with self._lock:
            payload = self._read()
            payload["default_bindings"] = serialized
            self._write(payload)

    # v0.14: the bindings by bot (what the learning runs and the bot store use; the seat view above is for the game).
    def get_bot_bindings(self) -> dict[str, ModelBinding]:
        """The stored default bindings as they are: keyed by bot id (and ``moderator``)."""
        with self._lock:
            try:
                return {key: ModelBinding.model_validate(value) for key, value in self._read()["default_bindings"].items()}
            except ValidationError as exc:
                raise StorageError("Invalid model binding in external settings") from exc

    def get_bot_binding(self, bot_id: str) -> ModelBinding | None:
        """The default model binding of one bot (``None`` when it has none) - what its initialization uses (H20)."""
        with self._lock:
            raw_bindings = self._read()["default_bindings"]
            binding = raw_bindings.get(bot_id)
            if binding is None:
                lineup = self._lineup_ids()
                if bot_id in lineup:  # (v0.13 saved it under the seat: not moved to the bot yet)
                    binding = raw_bindings.get(f"N{lineup.index(bot_id) + 1}")
            if binding is None:
                return None
            try:
                return ModelBinding.model_validate(binding)
            except ValidationError as exc:
                raise StorageError("Invalid model binding in external settings") from exc

    def save_bot_binding(self, bot_id: str, binding: ModelBinding | None) -> None:
        """Set (or, with ``None``, remove) the default binding of one bot."""
        if not isinstance(bot_id, str) or not bot_id:
            raise ValueError("bot id must not be empty")
        with self._lock:
            payload = self._read()
            if binding is None:
                if bot_id not in payload["default_bindings"]:
                    return
                payload["default_bindings"].pop(bot_id)
            else:
                if not isinstance(binding, ModelBinding):
                    binding = ModelBinding.model_validate(binding)
                payload["default_bindings"][bot_id] = binding.model_dump(mode="json")
            self._write(payload)

    def rename_binding_key(self, old_id: str, new_id: str) -> bool:
        """A bot got a new id (it was renamed after it had been initialized): its default binding moves along.  ``True`` when there
        was one.  Nothing is written when there is none."""
        with self._lock:
            payload = self._read()
            bindings = payload["default_bindings"]
            if old_id not in bindings:
                return False
            bindings[new_id] = bindings.pop(old_id)
            self._write(payload)
            return True

    def prune_bot_bindings(self, keep: Any) -> bool:
        """Forget the default bindings of bots that are gone: every key that is a bot id and not in ``keep`` is removed (seat keys,
        ``moderator`` and anything else stay).  ``True`` when the file was rewritten."""
        keep_ids = set(keep)
        with self._lock:
            if not self._path.exists():
                return False
            payload = self._read()
            bindings = payload["default_bindings"]
            gone = [key for key in bindings if _BOT_ID_KEY.match(key) and key not in keep_ids]
            for key in gone:
                bindings.pop(key)
            if gone:
                self._write(payload)
            return bool(gone)

    def migrate_seat_bindings(self) -> bool:
        """Move what v0.13 saved by seat (``N3``, v0.12.0's ``p03``) to the bot that sits there, once the line-up exists (a binding
        the bot already has wins).  ``True`` when the file was rewritten; without a line-up or without such keys nothing is
        read twice and nothing is written."""
        with self._lock:
            lineup = self._lineup_ids()
            if not lineup or not self._path.exists():
                return False
            payload = self._read()
            bindings = payload["default_bindings"]
            changed = False
            for key in list(bindings):
                number = _seat_number(key)
                if number is not None and number <= len(lineup):
                    value = bindings.pop(key)
                    bindings.setdefault(lineup[number - 1], value)
                    changed = True
            if changed:
                self._write(payload)
            return changed

    def get_model_templates(self) -> list[ModelTemplate]:
        with self._lock:
            return _validate_templates(self._read()["model_templates"])

    def save_model_templates(self, templates) -> None:
        if not isinstance(templates, (list, tuple)):
            raise TypeError("templates must be a list")
        try:
            validated = [ModelTemplate.model_validate(item) for item in templates]
        except (ValidationError, TypeError, ValueError) as exc:
            raise ValueError("Invalid model templates") from exc
        slots = [template.slot for template in validated]
        if len(slots) != len(set(slots)):
            raise ValueError("Model template slots must be unique")

        # References are validated against the same data directory as the
        # settings file, never against a stale global/workspace store.
        from app.providers.store import ProviderStore

        provider_store = ProviderStore(self.data_dir)
        for template in validated:
            provider_store.resolve(
                ModelBinding(
                    provider_id=template.provider_id,
                    model_id=template.model_id,
                    reasoning_depth=template.reasoning_depth,
                )
            )
        with self._lock:
            payload = self._read()
            payload["model_templates"] = [
                template.model_dump(mode="json") for template in validated
            ]
            self._write(payload)

    # v0.12.1: the user's default spending limit (copied into every new game).  It lives in the free-form
    # ``ui`` dict of the settings, so the settings schema is unchanged.
    def get_budget_defaults(self) -> dict[str, Any]:
        with self._lock:
            raw = self._read()["ui"].get("budget_defaults")
        return copy.deepcopy(raw) if isinstance(raw, dict) else {}

    def save_budget_defaults(self, budget: dict[str, Any]) -> None:
        if not isinstance(budget, dict):
            raise TypeError("budget must be a dict")
        with self._lock:
            payload = self._read()
            payload["ui"] = {**payload["ui"], "budget_defaults": copy.deepcopy(budget)}
            self._write(payload)

    def get_last_session_id(self) -> str | None:
        with self._lock:
            return self._read()["last_session_id"]

    def set_last_session_id(self, session_id) -> None:
        if session_id is not None:
            validate_session_id(session_id)
        with self._lock:
            payload = self._read()
            payload["last_session_id"] = session_id
            self._write(payload)

    # v0.13: single user-interface preferences (for example "the old-data notice was dismissed") in the same free-form
    # ``ui`` dict as the budget defaults.
    def get_ui_pref(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._read()["ui"].get(key, default))

    def set_ui_pref(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("ui preference key must not be empty")
        with self._lock:
            payload = self._read()
            payload["ui"] = {**payload["ui"], key: copy.deepcopy(value)}
            self._write(payload)


_store_lock = threading.RLock()
_settings_store: SettingsStore | None = None


def get_settings_store() -> SettingsStore:
    global _settings_store
    with _store_lock:
        data_dir = get_data_dir()
        if _settings_store is None or _settings_store.data_dir != data_dir:
            _settings_store = SettingsStore(data_dir)
        return _settings_store


def reset_settings_store() -> None:
    global _settings_store
    with _store_lock:
        _settings_store = None


def get_settings() -> dict:
    return get_settings_store().get_settings()


def get_default_bindings() -> dict[str, ModelBinding]:
    return get_settings_store().get_default_bindings()


def save_default_bindings(bindings: dict[str, ModelBinding]) -> None:
    get_settings_store().save_default_bindings(bindings)


def get_bot_binding(bot_id: str) -> ModelBinding | None:
    return get_settings_store().get_bot_binding(bot_id)


def save_bot_binding(bot_id: str, binding: ModelBinding | None) -> None:
    get_settings_store().save_bot_binding(bot_id, binding)


def get_model_templates() -> list[ModelTemplate]:
    return get_settings_store().get_model_templates()


def save_model_templates(templates) -> None:
    get_settings_store().save_model_templates(templates)


def get_budget_defaults() -> dict:
    return get_settings_store().get_budget_defaults()


def save_budget_defaults(budget: dict) -> None:
    get_settings_store().save_budget_defaults(budget)


def get_last_session_id() -> str | None:
    return get_settings_store().get_last_session_id()


def set_last_session_id(session_id) -> None:
    get_settings_store().set_last_session_id(session_id)


def get_ui_pref(key: str, default: Any = None) -> Any:
    return get_settings_store().get_ui_pref(key, default)


def set_ui_pref(key: str, value: Any) -> None:
    get_settings_store().set_ui_pref(key, value)


__all__ = [
    "SettingsStore",
    "migrate_legacy_actor_ids",
    "get_bot_binding",
    "get_budget_defaults",
    "get_default_bindings",
    "get_last_session_id",
    "get_model_templates",
    "get_settings",
    "get_settings_store",
    "get_ui_pref",
    "reset_settings_store",
    "save_bot_binding",
    "save_budget_defaults",
    "save_default_bindings",
    "save_model_templates",
    "set_last_session_id",
    "set_ui_pref",
]
