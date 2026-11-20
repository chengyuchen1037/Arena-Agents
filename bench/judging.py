"""The judges (docs/v0.14-plan.md H48): the rubric files, the packs a DSH ``workflow`` takes, and the verdicts that come back.

A *judge* is a DSH sub-agent - never the player model.  The runner (``bench.runner``) writes ``judge_items.jsonl``: one item per (result row × rubric), each with
an opaque ``blind_id``.  This module turns the items into **packs** (``judge-pack``): for every rubric, batches of at most :data:`BATCH` items, each item already
a complete, *blind* prompt (the rubric text, then what the judge may see: ``bench.runner.blind_view`` - no bot, no run, no sample, no situation id, no truth
beyond what the rubric allows).  ``bench/judges/judge.workflow.js`` takes one pack as ``args.pack`` and opens one sub-agent per item (DSH's ``workflow`` tool);
what it returns is saved to a file and merged back by ``judge-merge`` into ``judgments.jsonl`` - after every verdict has been checked against the rubric's
schema.  ``judge-check`` checks the rubric files themselves (the schema only uses what the workflow tool allows; the worked examples in each rubric are valid
verdicts of its own schema).

The rubric files (``bench/judges/<id>.md``) are a front matter (``rubric``, ``version``, ``title``, ``kind``, ``primary``, ``subject``, ``schema``) and the text
the judge is given.  Nothing here calls a model.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

BENCH_DIR = Path(__file__).resolve().parent
JUDGES_DIR = BENCH_DIR / "judges"
WORKFLOW_FILE = JUDGES_DIR / "judge.workflow.js"
PACK_FORMAT = "arena-bench-judge-pack"
VERDICTS_FORMAT = "arena-bench-verdicts"
JUDGMENT_FORMAT = "arena-bench-judgment"
BATCH = 40
KINDS = ("basis", "match", "pairwise", "recall")
#: What DSH's ``workflow`` tool accepts in a schema (``agent(prompt, {schema})``): a rubric must stay inside it.
SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum", "const", "oneOf"})
_FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.S)
_EXAMPLE = re.compile(r"```json\s*\n(.*?)\n```", re.S)


class RubricError(ValueError):
    """A rubric file that cannot be used; the message names the file and what is wrong."""


class JudgingError(ValueError):
    """Items, packs or verdicts that do not fit together."""


@dataclass(frozen=True)
class Rubric:
    id: str
    version: int
    title: str
    kind: str
    primary: str
    subject: tuple[str, ...]
    schema: dict[str, Any]
    body: str
    path: Path | None = None


# ---------------------------------------------------------------------------------------------------------------------- the rubric files
def parse_rubric(text: str, *, source: str = "rubric") -> Rubric:
    match = _FRONT.match(text.replace("\r\n", "\n"))
    if not match:
        raise RubricError(f"{source}：文件要以 --- 包住的 front matter 开头")
    try:
        head = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise RubricError(f"{source}：front matter 不是合法的 YAML（{exc}）") from exc
    if not isinstance(head, dict):
        raise RubricError(f"{source}：front matter 要是一个映射")
    for key in ("rubric", "version", "title", "kind", "primary", "subject", "schema"):
        if key not in head:
            raise RubricError(f"{source}：front matter 缺 {key}")
    rubric_id, version = head["rubric"], head["version"]
    if not isinstance(rubric_id, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,3}", rubric_id):
        raise RubricError(f"{source}：rubric 要是 S2 / L1 这样的名字，不是 {rubric_id!r}")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RubricError(f"{source}：version 要是正整数")
    if head["kind"] not in KINDS:
        raise RubricError(f"{source}：kind 要是 {', '.join(KINDS)} 之一，不是 {head['kind']!r}")
    subject = head["subject"]
    if not isinstance(subject, list) or not subject or not all(isinstance(k, str) and k for k in subject):
        raise RubricError(f"{source}：subject 要是非空的字段名列表")
    schema = head["schema"]
    problems = schema_problems(schema)
    if problems:
        raise RubricError(f"{source}：schema 有问题：" + "；".join(problems))
    primary = head["primary"]
    if schema.get("type") != "object" or primary not in (schema.get("properties") or {}):
        raise RubricError(f"{source}：primary={primary!r} 要是 schema 里的一个字段")
    return Rubric(
        id=rubric_id, version=version, title=str(head["title"]), kind=head["kind"], primary=primary, subject=tuple(subject), schema=schema,
        body=match.group(2).strip(), path=None,
    )


def load_rubric(source: str | Path, *, directory: Path = JUDGES_DIR) -> Rubric:
    path = Path(source)
    if not path.suffix:
        path = directory / f"{source}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RubricError(f"读不了 rubric 文件 {path}：{exc}") from exc
    rubric = parse_rubric(text, source=path.name)
    if path.stem != rubric.id:
        raise RubricError(f"{path.name}：文件名要和 rubric 的名字一致（{rubric.id}.md）")
    return Rubric(**{**rubric.__dict__, "path": path})


def load_rubrics(directory: Path = JUDGES_DIR) -> dict[str, Rubric]:
    """Every ``<id>.md`` of the directory that is a rubric (``README.md`` and the like are not)."""
    found: dict[str, Rubric] = {}
    for path in sorted(directory.glob("*.md")):
        if re.fullmatch(r"[A-Z][A-Z0-9]{0,3}", path.stem):
            found[path.stem] = load_rubric(path, directory=directory)
    return found


def rubric_examples(rubric: Rubric) -> list[dict[str, Any]]:
    """The worked examples of the rubric text: ``[{"input": ..., "verdict": ...}]`` (a json block with both keys)."""
    out: list[dict[str, Any]] = []
    for block in _EXAMPLE.findall(rubric.body):
        try:
            data = json.loads(block)
        except json.JSONDecodeError as exc:
            raise RubricError(f"{rubric.id}：示例不是合法的 JSON（{exc}）") from exc
        if isinstance(data, dict) and "verdict" in data:
            out.append(data)
    return out


# ---------------------------------------------------------------------------------------------------------------------- schemas
def schema_problems(schema: Any, path: str = "schema") -> list[str]:
    """What is wrong with ``schema`` for DSH's workflow tool: only :data:`SCHEMA_KEYS` are allowed, the root is an object."""
    problems: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path} 要是一个对象"]
    for key in schema:
        if key not in SCHEMA_KEYS:
            problems.append(f"{path}.{key} 不在允许的子集里（{', '.join(sorted(SCHEMA_KEYS))}）")
    if path == "schema" and schema.get("type") != "object":
        problems.append("schema 的根要是 type: object")
    for name, sub in (schema.get("properties") or {}).items():
        problems += schema_problems(sub, f"{path}.properties.{name}")
    if "items" in schema:
        problems += schema_problems(schema["items"], f"{path}.items")
    for index, sub in enumerate(schema.get("oneOf") or []):
        problems += schema_problems(sub, f"{path}.oneOf[{index}]")
    return problems


def validate_verdict(schema: Mapping[str, Any], value: Any, path: str = "verdict") -> list[str]:
    """What is wrong with ``value`` as an answer to ``schema`` (the allowed subset only); an empty list = it fits."""
    problems: list[str] = []
    if "const" in schema and value != schema["const"]:
        problems.append(f"{path}：只能是 {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}：{value!r} 不在 {schema['enum']} 里")
    if "oneOf" in schema:
        if sum(1 for sub in schema["oneOf"] if not validate_verdict(sub, value, path)) != 1:
            problems.append(f"{path}：要正好符合 oneOf 里的一项")
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return problems + [f"{path}：要是一个对象"]
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                problems.append(f"{path}：缺 {name}")
        if schema.get("additionalProperties") is False:
            problems += [f"{path}：多出 {name}" for name in value if name not in properties]
        for name, sub in properties.items():
            if name in value:
                problems += validate_verdict(sub, value[name], f"{path}.{name}")
    elif kind == "array":
        if not isinstance(value, list):
            return problems + [f"{path}：要是一个数组"]
        if "items" in schema:
            for index, item in enumerate(value):
                problems += validate_verdict(schema["items"], item, f"{path}[{index}]")
    elif kind == "string" and not isinstance(value, str):
        problems.append(f"{path}：要是字符串")
    elif kind == "boolean" and not isinstance(value, bool):
        problems.append(f"{path}：要是 true / false")
    elif kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        problems.append(f"{path}：要是整数")
    elif kind == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        problems.append(f"{path}：要是数字")
    return problems


# ---------------------------------------------------------------------------------------------------------------------- packs
INTRO = (
    "你是一名评审。请严格按下面的评分标准，评判“待评内容”，只给出符合格式的判定。\n"
    "- 只依据这条消息里给出的内容判断。不要使用任何工具，不要读取任何文件，不要联网。\n"
    "- 不要猜这条内容出自哪个 AI、哪个版本或哪一轮；你也看不到它们。\n"
    "- 判定是一个 JSON 对象，字段与取值按评分标准末尾“判定格式”给出的 schema。"
)


def judge_prompt(rubric: Rubric, view: Mapping[str, Any]) -> str:
    """The whole prompt of one item: the instructions, the rubric text, and the blind view of the item."""
    shown = json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True)
    schema = json.dumps(rubric.schema, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        f"{INTRO}\n\n# 评分标准（{rubric.id} v{rubric.version}：{rubric.title}）\n\n{rubric.body}\n\n"
        f"# 判定格式\n\n```json\n{schema}\n```\n\n# 待评内容\n\n```json\n{shown}\n```\n"
    )


def blind_view_of(item: Mapping[str, Any], rubric: Rubric) -> dict[str, Any]:
    from bench.runner import blind_view  # (the one definition of what a judge may see)

    return blind_view(item, subject=rubric.subject)


def pack_items(
    items: Iterable[Mapping[str, Any]], *, rubrics: Mapping[str, Rubric] | None = None, batch: int = BATCH, only: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Packs for ``bench/judges/judge.workflow.js``: per rubric, batches of at most ``batch`` items.  An item of an unknown rubric is an error, not a skip."""
    rubrics = dict(rubrics if rubrics is not None else load_rubrics())
    if batch < 1:
        raise JudgingError("batch 要 ≥ 1")
    wanted = set(only)
    by_rubric: dict[str, list[Mapping[str, Any]]] = {}
    seen: set[str] = set()
    for item in items:
        blind_id, rubric_id = item.get("blind_id"), item.get("rubric")
        if not isinstance(blind_id, str) or not blind_id:
            raise JudgingError(f"有一条裁判条目没有 blind_id：{str(item)[:120]}")
        if rubric_id not in rubrics:
            raise JudgingError(f"裁判条目 {blind_id} 的 rubric {rubric_id!r} 没有对应的 bench/judges/{rubric_id}.md")
        if wanted and rubric_id not in wanted:
            continue
        if blind_id in seen:
            continue  # (the same row listed twice: judge it once)
        seen.add(blind_id)
        by_rubric.setdefault(rubric_id, []).append(item)
    packs: list[dict[str, Any]] = []
    for rubric_id in sorted(by_rubric):
        rubric = rubrics[rubric_id]
        rows = by_rubric[rubric_id]
        chunks = [rows[i:i + batch] for i in range(0, len(rows), batch)]
        for number, chunk in enumerate(chunks, start=1):
            packs.append(
                {
                    "format": PACK_FORMAT, "version": 1, "rubric": rubric.id, "rubric_version": rubric.version, "title": rubric.title,
                    "primary": rubric.primary, "schema": rubric.schema, "batch": number, "batches": len(chunks),
                    "items": [{"blind_id": it["blind_id"], "prompt": judge_prompt(rubric, blind_view_of(it, rubric))} for it in chunk],
                }
            )
    return packs


def forbidden_words(items: Iterable[Mapping[str, Any]]) -> list[str]:
    """What must never be in a judge's prompt: the labels and names of the bots, the run, the situation ids (they are all in the items, not in the view)."""
    words: set[str] = set()
    for item in items:
        for key in ("bot", "brain", "run", "situation", "item_id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                words.add(value)
        label = item.get("bot_label")
        if isinstance(label, str) and label:
            words.add(label)
    return sorted(words)


# ---------------------------------------------------------------------------------------------------------------------- verdicts
def merge_verdicts(
    items: Sequence[Mapping[str, Any]], docs: Sequence[Mapping[str, Any]], *, rubrics: Mapping[str, Rubric] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """``(judgments, problems)``: the verdicts of the workflow (``docs``) checked against the rubric schemas and joined to the items by ``blind_id``.
    A verdict that does not fit is *not* kept - it is a problem."""
    rubrics = dict(rubrics if rubrics is not None else load_rubrics())
    by_blind = {str(item["blind_id"]): item for item in items}
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    done: set[str] = set()
    for number, doc in enumerate(docs, start=1):
        where = f"第 {number} 份判定"
        if not isinstance(doc, Mapping) or doc.get("format") != VERDICTS_FORMAT:
            problems.append(f"{where}：不是 judge.workflow.js 的返回（format 要是 {VERDICTS_FORMAT}）")
            continue
        rubric = rubrics.get(str(doc.get("rubric")))
        if rubric is None:
            problems.append(f"{where}：未知的 rubric {doc.get('rubric')!r}")
            continue
        if doc.get("rubric_version") != rubric.version:
            problems.append(f"{where}：rubric {rubric.id} 的版本是 v{doc.get('rubric_version')}，现在的是 v{rubric.version}——请重新打包再判")
            continue
        for blind_id in doc.get("errors") or []:
            problems.append(f"{where}：{blind_id} 没有得到判定（子 agent 失败或返回为空）")
        for entry in doc.get("verdicts") or []:
            blind_id = str(entry.get("blind_id")) if isinstance(entry, Mapping) else ""
            item = by_blind.get(blind_id)
            if item is None:
                problems.append(f"{where}：{blind_id or '（缺 blind_id）'} 不在裁判条目里")
                continue
            if item.get("rubric") != rubric.id:
                problems.append(f"{where}：{blind_id} 属于 rubric {item.get('rubric')}，不是 {rubric.id}")
                continue
            if blind_id in done:
                problems.append(f"{where}：{blind_id} 重复了，保留第一份")
                continue
            bad = validate_verdict(rubric.schema, entry.get("verdict"))
            if bad:
                problems.append(f"{where}：{blind_id} 的判定不符合 {rubric.id} 的格式：" + "；".join(bad))
                continue
            done.add(blind_id)
            rows.append(
                {
                    "format": JUDGMENT_FORMAT, "version": 1, "blind_id": blind_id, "item_id": item.get("item_id"), "rubric": rubric.id,
                    "rubric_version": rubric.version, "verdict": entry["verdict"], "judge": {"source": "dsh-subagent", "attempts": entry.get("attempts")},
                }
            )
    return rows, problems


def missing_items(items: Sequence[Mapping[str, Any]], judgments: Sequence[Mapping[str, Any]], *, only: Sequence[str] = ()) -> list[str]:
    """The ``blind_id`` of every item (of ``only`` rubrics, when given) that has no judgment."""
    have = {str(j.get("blind_id")) for j in judgments}
    wanted = set(only)
    return [str(i["blind_id"]) for i in items if (not wanted or i.get("rubric") in wanted) and str(i["blind_id"]) not in have]


# ---------------------------------------------------------------------------------------------------------------------- the command line
def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JudgingError(f"{path.name} 第 {number} 行不是合法的 JSON：{exc}") from exc
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _print(text: str = "") -> None:
    print(text)


def cmd_judge_check(args: argparse.Namespace) -> int:
    problems: list[str] = []
    try:
        rubrics = load_rubrics(args.judges)
    except RubricError as exc:
        _print(f"rubric 文件有问题：{exc}")
        return 1
    for rubric in rubrics.values():
        try:
            examples = rubric_examples(rubric)
        except RubricError as exc:
            problems.append(str(exc))
            continue
        if len(examples) < 3:
            problems.append(f"{rubric.id}：至少要有 3 个示例判例（现在 {len(examples)} 个）")
        for number, example in enumerate(examples, start=1):
            bad = validate_verdict(rubric.schema, example.get("verdict"))
            if bad:
                problems.append(f"{rubric.id} 示例 {number} 的判定不符合自己的 schema：" + "；".join(bad))
        _print(f"{rubric.id} v{rubric.version}  {rubric.kind}  主指标 {rubric.primary}  示例 {len(examples)}  {rubric.title}")
    if not (args.judges / "judge.workflow.js").is_file():
        problems.append("缺 judge.workflow.js")
    for problem in problems:
        _print(f"问题：{problem}")
    _print(f"共 {len(rubrics)} 个 rubric，{len(problems)} 个问题")
    return 1 if problems else 0


def cmd_judge_pack(args: argparse.Namespace) -> int:
    try:
        items = read_jsonl(args.items)
        packs = pack_items(items, batch=args.batch, only=[r for r in (args.rubrics or "").split(",") if r])
    except (JudgingError, RubricError, OSError) as exc:
        _print(f"打包失败：{exc}")
        return 1
    if not packs:
        _print("没有需要裁判的条目。")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    for pack in packs:
        path = args.out / f"{pack['rubric']}-{pack['batch']:02d}.json"
        path.write_text(json.dumps(pack, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        _print(f"{path}  {pack['rubric']} v{pack['rubric_version']}  第 {pack['batch']}/{pack['batches']} 批  {len(pack['items'])} 条")
    _print(
        f"\n共 {len(packs)} 个包。在 DSH 里对每个包各跑一次 workflow 工具：script = {WORKFLOW_FILE} 的内容，args = {{\"pack\": <该文件的内容>}}；"
        f"把返回的 JSON 存成文件（例如 verdicts-<rubric>-<批>.json），再用 judge-merge 合并。"
    )
    return 0


def cmd_judge_merge(args: argparse.Namespace) -> int:
    try:
        items = read_jsonl(args.items)
        docs = [json.loads(path.read_text(encoding="utf-8")) for path in args.verdicts]
    except (JudgingError, OSError, json.JSONDecodeError) as exc:
        _print(f"合并失败：{exc}")
        return 1
    rows, problems = merge_verdicts(items, docs)
    for problem in problems:
        _print(f"问题：{problem}")
    missing = missing_items(items, rows)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    _print(f"合并了 {len(rows)} 条判定（条目 {len(items)}，还没有判定的 {len(missing)}，问题 {len(problems)}）" + (f"，写到 {args.out}" if args.out else ""))
    if problems and not args.allow_problems:
        return 1
    if missing and not args.allow_missing:
        _print("有条目还没有判定（--allow-missing 可以只合并已有的）。")
        return 1
    return 0


def add_commands(sub: Any) -> None:
    check = sub.add_parser("judge-check", help="check the rubric files: the schema stays in what the workflow tool allows, the examples fit it")
    check.add_argument("--judges", type=Path, default=JUDGES_DIR)
    pack = sub.add_parser("judge-pack", help="judge_items.jsonl -> packs (blind prompts, ≤ 40 items each) for bench/judges/judge.workflow.js")
    pack.add_argument("--items", required=True, type=Path)
    pack.add_argument("--out", required=True, type=Path)
    pack.add_argument("--batch", type=int, default=BATCH)
    pack.add_argument("--rubrics", help="only these rubrics (comma separated)")
    merge = sub.add_parser("judge-merge", help="the verdicts of the workflow -> judgments.jsonl (every verdict is checked against its rubric schema)")
    merge.add_argument("--items", required=True, type=Path)
    merge.add_argument("--verdicts", required=True, nargs="+", type=Path)
    merge.add_argument("--out", type=Path)
    merge.add_argument("--allow-missing", action="store_true")
    merge.add_argument("--allow-problems", action="store_true", help="keep the verdicts that fit and only report the others (exit 0)")


HANDLERS = {"judge-check": cmd_judge_check, "judge-pack": cmd_judge_pack, "judge-merge": cmd_judge_merge}


def main(argv: Sequence[str] | None = None) -> int:  # (python -m bench.judging ... for a quick look; the real entry is python -m bench)
    parser = argparse.ArgumentParser(prog="python -m bench.judging")
    sub = parser.add_subparsers(dest="command", required=True)
    add_commands(sub)
    args = parser.parse_args(argv)
    return HANDLERS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
