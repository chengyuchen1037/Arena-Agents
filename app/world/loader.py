"""Scenario v2 loader: a Markdown file (YAML front matter + body) -> :class:`ScenarioSpec`.

The body is split by heading: the section whose heading text equals ``manual_heading`` (default
"主持人手册") becomes ``manual_text`` (moderator only), everything else becomes ``rules_text``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

import app.config as app_config
from app.world.spec import ScenarioError, ScenarioSpec

DEFAULT_MANUAL_HEADING = "主持人手册"
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

__all__ = ["ScenarioError", "load_scenario_v2", "parse_scenario_v2", "split_body"]


def _front_matter(source: str, where: str) -> tuple[dict[str, Any], str]:
    text = source.lstrip("\ufeff")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ScenarioError(f"{where}: 剧本必须以 YAML front matter（---）开头")
    end = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if end is None:
        raise ScenarioError(f"{where}: YAML front matter 没有以 --- 结束")
    try:
        data = yaml.safe_load("".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{where}: front matter 不是合法的 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise ScenarioError(f"{where}: front matter 必须是一个映射")
    return data, "".join(lines[end + 1 :])


def split_body(body: str, manual_heading: str = DEFAULT_MANUAL_HEADING) -> tuple[str, str]:
    """``(rules_text, manual_text)``.  Headings inside fenced code blocks are ignored."""
    lines = body.splitlines()
    in_fence = False
    headings: list[tuple[int, int, str]] = []  # (line index, level, text)
    for index, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))
    start = next((h for h in headings if h[2] == manual_heading.strip()), None)
    if start is None:
        return body.strip(), ""
    start_index, level, _ = start
    end_index = next((h[0] for h in headings if h[0] > start_index and h[1] <= level), len(lines))
    manual = "\n".join(lines[start_index:end_index]).strip()
    rules = "\n".join(lines[:start_index] + lines[end_index:]).strip()
    return rules, manual


def _check_expression(expression: str, where: str) -> None:
    text = expression.strip()
    if text.startswith("${") and text.endswith("}"):
        text = text[2:-1]
    try:
        ast.parse(text.strip(), mode="eval")
    except SyntaxError as exc:
        raise ScenarioError(f"{where}: 表达式无法解析（{exc.msg}）：{expression!r}") from exc


def _validate_extras(spec: ScenarioSpec, where: str) -> None:
    for name, expression in spec.checks.items():
        _check_expression(expression, f"{where}: checks.{name}")
    for index, constraint in enumerate(spec.constraints):
        _check_expression(constraint.expr, f"{where}: constraints[{index}]")
    for tid, template in spec.templates.items():
        unknown = sorted(set(_PLACEHOLDER.findall(template.text)) - set(template.params))
        if unknown:
            raise ScenarioError(f"{where}: templates.{tid} 的占位符 {unknown} 没有在 params 中声明")
    for aid, ability in spec.abilities.items():
        _check_expression(ability.holders, f"{where}: abilities.{aid}.holders")
        if ability.reveal is not None:
            if not ability.reveal.channel_id.strip():
                raise ScenarioError(f"{where}: abilities.{aid}.reveal.channel_id 不能为空")
            allowed = {*ability.params, "actor"}
            unknown = sorted(set(_PLACEHOLDER.findall(ability.reveal.text)) - allowed)
            if unknown:
                raise ScenarioError(f"{where}: abilities.{aid}.reveal 的占位符 {unknown} 没有对应的参数")


def parse_scenario_v2(source: str, *, where: str = "<scenario>") -> ScenarioSpec:
    data, body = _front_matter(source, where)
    if int(data.get("schema_version", 0) or 0) != 2:
        raise ScenarioError(f"{where}: schema_version 必须是 2（收到 {data.get('schema_version')!r}）")
    manual_heading = str(data.pop("manual_heading", DEFAULT_MANUAL_HEADING) or DEFAULT_MANUAL_HEADING)
    rules_text, manual_text = split_body(body, manual_heading)
    data["rules_text"], data["manual_text"] = rules_text, manual_text
    try:
        spec = ScenarioSpec.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(根)'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)[:6]
        )
        raise ScenarioError(f"{where}: 剧本校验失败：{problems}") from exc
    _validate_extras(spec, where)
    return spec


def load_scenario_v2(path: str | Path) -> ScenarioSpec:
    target = Path(path)
    if not target.is_absolute():
        target = Path(app_config.BASE_DIR) / target
    target = target.resolve()
    if not target.is_file():
        raise ScenarioError(f"找不到剧本文件：{target}")
    if target.suffix.lower() not in (".md", ".markdown"):
        raise ScenarioError(f"剧本必须是 Markdown 文档：{target}")
    try:
        source = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ScenarioError(f"无法读取剧本 {target}：{exc}") from exc
    return parse_scenario_v2(source, where=target.name)
