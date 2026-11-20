"""Writing the question bank: ``all_cases()`` -> the static YAML files below ``bench/fixtures`` (and the pair files of L1), or checking that the committed
files are exactly what the code writes.  ``python -m bench fixtures`` (write) / ``python -m bench fixtures --check``."""

from __future__ import annotations

from pathlib import Path

import yaml

from bench.authoring import dump
from bench.cases import all_cases
from bench.cases.common import Case

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def render_case(case: Case) -> str:
    header, data = case.build()
    return dump(data, header=header)


def render_pair(pair_id: str, a: Case, b: Case) -> str:
    """``fixtures/pairs/<id>.yaml``: the two situations of an L1 pair, the right answer (the same in both) and who talks how in each."""
    _ha, da = a.build()
    _hb, db = b.build()
    title = str(da["title"]).rsplit("（", 1)[0]
    sides = {"a": da["truth"]["sides"], "b": db["truth"]["sides"]}
    body = {
        "format": "arena-pair", "version": 1, "id": pair_id, "title": title, "suite": "L1", "a": a.path, "b": b.path, "answer": da["truth"]["answer"],
        "decisive": da["truth"]["decisive"], "sides": sides,
    }
    header = (
        f"L1 成对题 {pair_id}：{title}\n"
        "A、B 两个局面的事实完全相同（同一份代码、同一个种子；只有标了 meta.styled 的发言不同）：A 里狼说得长而有条理、好人说得短而直，B 里两边对调。\n"
        "表达偏差 = P(投给说得好听的一方) − 0.5；两版都投正确答案的 bot 偏差为 0。本文件由 bench/cases 生成，不要手改（python -m bench fixtures）。"
    )
    return "".join(f"# {line}\n" for line in header.splitlines()) + yaml.safe_dump(body, allow_unicode=True, sort_keys=False, width=140, default_flow_style=False)


def expected_files() -> dict[str, str]:
    """``{path below fixtures: text}`` for every generated file."""
    cases = all_cases()
    files = {case.path: render_case(case) for case in cases}
    pairs: dict[str, list[Case]] = {}
    for case in cases:
        if case.pair:
            pairs.setdefault(case.pair, []).append(case)
    for pair_id, members in pairs.items():
        a, b = sorted(members, key=lambda c: c.id)
        files[f"pairs/{pair_id}.yaml"] = render_pair(pair_id, a, b)
    return files


def check(root: Path = FIXTURES) -> list[str]:
    """What differs between the code and the committed files: ``missing``, ``changed``."""
    problems: list[str] = []
    for path, text in expected_files().items():
        target = root / path
        if not target.is_file():
            problems.append(f"缺少 {path}（运行 python -m bench fixtures）")
        elif target.read_text(encoding="utf-8") != text:
            problems.append(f"{path} 和生成器写出的不一样（手改了，或者代码变了：运行 python -m bench fixtures）")
    return problems


def write(root: Path = FIXTURES) -> list[str]:
    """Write every generated file (only the ones that change); returns the paths written."""
    written: list[str] = []
    for path, text in expected_files().items():
        target = root / path
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


__all__ = ["FIXTURES", "check", "expected_files", "render_case", "render_pair", "write"]
