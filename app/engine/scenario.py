from __future__ import annotations

import datetime
import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.config import BASE_DIR


class ScenarioError(ValueError):
    """A scenario document is malformed or references an invalid workflow step."""


SUPPORTED_ACTIONS = {
    "tool",
    "deal_identities",
    "set",
    "patch_player",
    "choice",
    "speak",
    "vote",
    "plan",
    "interrupt",
    "eliminate",
    "dispatch_events",
    "refresh_dashboard",
    "if",
    "for_each",
    "while",
    "goto",
    "break",
}


MAX_SNAPSHOT_BODY_CHARS = 500_000
MAX_SNAPSHOT_DEFINITION_BYTES = 2_000_000
MAX_SNAPSHOT_DEPTH = 40
MAX_SNAPSHOT_NODES = 200_000


def _canonical_json(value: Any) -> str:
    # Round-trip through JSON first so every mapping key is a string; this
    # makes sort_keys safe for mixed key types and makes YAML-loaded values
    # (int keys, dates) compare equal to their JSON-restored counterparts.
    normalized = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_content_hash(definition: Any, body: str) -> str:
    payload = _canonical_json({"definition": definition, "body": body})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_limits(definition: Any) -> None:
    """Reject absurdly deep / large / non-plain-JSON scenario definitions."""

    nodes = 0
    stack: list[tuple[Any, int]] = [(definition, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > MAX_SNAPSHOT_DEPTH:
            raise ScenarioError("scenario_snapshot.definition is nested too deeply")
        if nodes > MAX_SNAPSHOT_NODES:
            raise ScenarioError("scenario_snapshot.definition is too large")
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend((item, depth + 1) for item in value)
        elif value is not None and not isinstance(
            value, (str, int, float, bool, datetime.date)
        ):
            raise ScenarioError("scenario_snapshot.definition contains unsupported values")
    try:
        size = len(json.dumps(definition, ensure_ascii=False, default=str))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ScenarioError("scenario_snapshot.definition is not serializable") from exc
    if size > MAX_SNAPSHOT_DEFINITION_BYTES:
        raise ScenarioError("scenario_snapshot.definition is too large")


def _verify_snapshot_integrity(snapshot: dict[str, Any], document: "ScenarioDocument") -> None:
    expected = document.content_hash()
    claimed = snapshot.get("content_sha256")
    if claimed is not None:
        if not isinstance(claimed, str) or not hmac.compare_digest(claimed, expected):
            raise ScenarioError("scenario_snapshot content hash mismatch (snapshot was modified)")

    # If the snapshot claims to be the scenario file currently on disk (same
    # checksum), its content must be exactly that file's content.
    scenarios_dir = (BASE_DIR / "scenarios").resolve()
    if not document.path or not document.checksum:
        return
    try:
        candidate = Path(document.path)
        if not candidate.is_absolute():
            candidate = BASE_DIR / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(scenarios_dir) or not candidate.is_file():
            return
        if candidate.suffix.lower() not in (".md", ".markdown"):
            return
        source = candidate.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return
    if hashlib.sha256(source.encode("utf-8")).hexdigest() != document.checksum:
        return  # a different (older/newer) revision of the file; nothing to compare
    try:
        disk_definition, disk_body = _front_matter(source, candidate)
        disk_hash = compute_content_hash(disk_definition, disk_body)
    except (ScenarioError, ValueError, TypeError):
        return
    if not hmac.compare_digest(disk_hash, expected):
        raise ScenarioError("scenario_snapshot does not match the scenario file its checksum refers to")


@dataclass(frozen=True)
class ScenarioDocument:
    path: str
    checksum: str
    definition: dict[str, Any]
    body: str

    @property
    def scenario_id(self) -> str:
        return str(self.definition["id"])

    @property
    def version(self) -> str:
        return str(self.definition["version"])

    def content_hash(self) -> str:
        return compute_content_hash(self.definition, self.body)

    def snapshot(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "checksum": self.checksum,
            "content_sha256": self.content_hash(),
            "definition": self.definition,
            "body": self.body,
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "ScenarioDocument":
        """Rebuild a document from an (untrusted) snapshot.

        The snapshot is size/depth limited, structurally validated, and its
        ``content_sha256`` is recomputed server-side and compared. When the
        snapshot claims the checksum of the scenario file currently on disk,
        its content must also equal that file's content.
        """

        if not isinstance(snapshot, dict):
            raise ScenarioError("scenario_snapshot must be a mapping")
        definition = snapshot.get("definition") or {}
        if not isinstance(definition, dict):
            raise ScenarioError("scenario_snapshot.definition must be a mapping")
        body = snapshot.get("body", "")
        if not isinstance(body, str):
            raise ScenarioError("scenario_snapshot.body must be a string")
        raw_path = snapshot.get("path", "")
        raw_checksum = snapshot.get("checksum", "")
        if not isinstance(raw_path, str) or len(raw_path) > 1024:
            raise ScenarioError("scenario_snapshot.path is invalid")
        if not isinstance(raw_checksum, str) or len(raw_checksum) > 128:
            raise ScenarioError("scenario_snapshot.checksum is invalid")
        if len(body) > MAX_SNAPSHOT_BODY_CHARS:
            raise ScenarioError("scenario_snapshot.body is too large")
        _check_limits(definition)

        document = cls(
            path=raw_path,
            checksum=raw_checksum,
            definition=dict(definition),
            body=body,
        )
        try:
            validate_scenario(document.definition)
            _verify_snapshot_integrity(snapshot, document)
        except ScenarioError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise ScenarioError(f"scenario_snapshot is malformed: {exc}") from exc
        return document


def _front_matter(source: str, path: Path) -> tuple[dict[str, Any], str]:
    normalized = source.lstrip("\ufeff")
    lines = normalized.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ScenarioError(f"{path}: Markdown must start with YAML front matter (---)")
    end_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if end_index is None:
        raise ScenarioError(f"{path}: YAML front matter is not closed")
    yaml_source = "".join(lines[1:end_index])
    body = "".join(lines[end_index + 1 :])
    try:
        definition = yaml.safe_load(yaml_source) or {}
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: invalid YAML front matter: {exc}") from exc
    if not isinstance(definition, dict):
        raise ScenarioError(f"{path}: front matter must be a mapping")
    return definition, body


def load_scenario(path: str | Path) -> ScenarioDocument:
    target = Path(path)
    if not target.is_absolute():
        target = BASE_DIR / target
    target = target.resolve()
    if not target.exists() or not target.is_file():
        raise ScenarioError(f"Scenario document not found: {target}")
    if target.suffix.lower() not in (".md", ".markdown"):
        raise ScenarioError(f"Scenario must be a Markdown document: {target}")
    source = target.read_text(encoding="utf-8")
    definition, body = _front_matter(source, target)
    validate_scenario(definition)
    relative_path = str(target.relative_to(BASE_DIR)) if target.is_relative_to(BASE_DIR) else str(target)
    return ScenarioDocument(
        path=relative_path,
        checksum=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        definition=definition,
        body=body,
    )


def load_config_scenario(cfg: dict[str, Any]) -> ScenarioDocument | None:
    scenario_cfg = cfg.get("scenario") or {}
    if not isinstance(scenario_cfg, dict):
        raise ScenarioError("config.scenario must be a mapping")
    path = scenario_cfg.get("path") or scenario_cfg.get("document")
    if not path:
        return None
    return load_scenario(str(path))


def _validate_actions(
    actions: Any, step_ids: set[str], location: str, in_loop: bool = False
) -> None:
    if not isinstance(actions, list):
        raise ScenarioError(f"{location} must be a list")
    for index, action in enumerate(actions):
        action_location = f"{location}[{index}]"
        if not isinstance(action, dict) or len(action) != 1:
            raise ScenarioError(f"{action_location} must be a single-action mapping")
        name, specification = next(iter(action.items()))
        if name not in SUPPORTED_ACTIONS:
            raise ScenarioError(f"{action_location}: unsupported action {name!r}")
        if name == "goto":
            target = str(specification)
            if target not in step_ids and not target.startswith("${"):
                raise ScenarioError(f"{action_location}: unknown target step {target!r}")
        if name == "break" and not in_loop:
            raise ScenarioError(
                f"{action_location}: break is only allowed inside a for_each or while loop"
            )
        if name == "if":
            if not isinstance(specification, dict) or "when" not in specification:
                raise ScenarioError(f"{action_location}: if requires when")
            _validate_actions(
                specification.get("do", []), step_ids, f"{action_location}.do", in_loop
            )
            if "else" in specification:
                _validate_actions(
                    specification["else"], step_ids, f"{action_location}.else", in_loop
                )
        if name in ("for_each", "while"):
            if not isinstance(specification, dict):
                raise ScenarioError(f"{action_location}: {name} requires a mapping")
            if name == "for_each" and ("items" not in specification or "as" not in specification):
                raise ScenarioError(f"{action_location}: for_each requires items and as")
            if name == "while" and "when" not in specification:
                raise ScenarioError(f"{action_location}: while requires when")
            _validate_actions(
                specification.get("do", []), step_ids, f"{action_location}.do", True
            )


def validate_scenario(definition: dict[str, Any]) -> None:
    if int(definition.get("schema_version", 0)) != 1:
        raise ScenarioError("schema_version must be 1")
    for key in ("id", "version", "entry"):
        if not isinstance(definition.get(key), str) or not definition[key].strip():
            raise ScenarioError(f"{key} must be a non-empty string")
    workflow = definition.get("workflow")
    if not isinstance(workflow, list) or not workflow:
        raise ScenarioError("workflow must be a non-empty list")
    step_ids: set[str] = set()
    for index, step in enumerate(workflow):
        if not isinstance(step, dict) or not isinstance(step.get("id"), str) or not step["id"].strip():
            raise ScenarioError(f"workflow[{index}] requires a non-empty id")
        if step["id"] in step_ids:
            raise ScenarioError(f"Duplicate workflow step id: {step['id']}")
        step_ids.add(step["id"])
    if definition["entry"] not in step_ids:
        raise ScenarioError(f"Unknown entry step: {definition['entry']}")
    for index, step in enumerate(workflow):
        _validate_actions(step.get("do", []), step_ids, f"workflow[{index}].do")
        next_step = step.get("next")
        if next_step is not None and not str(next_step).startswith("${") and str(next_step) not in step_ids:
            raise ScenarioError(f"workflow[{index}].next references unknown step {next_step!r}")

    roles = definition.get("roles", {})
    if not isinstance(roles, dict):
        raise ScenarioError("roles must be a mapping")
    identity_setup = definition.get("identity_setup", {})
    if not isinstance(identity_setup, dict):
        raise ScenarioError("identity_setup must be a mapping")
    total_count = 0
    for index, item in enumerate(identity_setup.get("pool", [])):
        if not isinstance(item, dict) or item.get("role") not in roles:
            raise ScenarioError(f"identity_setup.pool[{index}] references an undeclared role")
        count = int(item.get("count", 1))
        if count < 0:
            raise ScenarioError(f"identity_setup.pool[{index}].count cannot be negative")
        total_count += count
    if identity_setup.get("pool") and total_count <= 0:
        raise ScenarioError("identity_setup.pool cannot be empty")

    termination = definition.get("termination", [])
    if not isinstance(termination, list):
        raise ScenarioError("termination must be a list")
    for index, rule in enumerate(termination):
        if not isinstance(rule, dict) or "when" not in rule or "winner" not in rule:
            raise ScenarioError(f"termination[{index}] requires when and winner")

    hooks = definition.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ScenarioError("hooks must be a mapping")
    for event_name, event_hooks in hooks.items():
        if not isinstance(event_hooks, list):
            raise ScenarioError(f"hooks.{event_name} must be a list")
        for index, hook in enumerate(event_hooks):
            if not isinstance(hook, dict):
                raise ScenarioError(f"hooks.{event_name}[{index}] must be a mapping")
            _validate_actions(hook.get("do", []), step_ids, f"hooks.{event_name}[{index}].do")
