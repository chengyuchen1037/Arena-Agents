"""The labelling page (bench/label/) and its two ends: ``calibration-set`` writes what the page opens, ``calibrate`` reads what the page exports.

* runs ``tests/bench/test_label_page.js`` with node (skipped when node is missing);
* the page's check of a label (``validateLabel``) and ``bench.judging.validate_verdict`` agree on the schemas of every rubric of the repository;
* the whole way: ``build_set`` -> the page's own controller labels every item (a node bridge, no browser) -> the page's export -> ``build_report``.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from bench.calibrate import build_report, build_set
from bench.judging import load_rubrics, rubric_examples, validate_verdict
from calibration_helpers import judgment_of, make_e2, make_item, make_m2, make_s2

ROOT = Path(__file__).resolve().parents[2]
PAGE_TESTS = ROOT / "tests" / "bench" / "test_label_page.js"
PAGE = ROOT / "bench" / "label" / "label.js"
RUBRICS = load_rubrics()

_WINDOWS_NODE = Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"


def _find_node() -> str | None:
    env = os.environ.get("NODE_BIN")
    if env and Path(env).exists():
        return env
    return shutil.which("node") or (str(_WINDOWS_NODE) if _WINDOWS_NODE.exists() else None)


NODE = _find_node()
pytestmark = pytest.mark.skipif(NODE is None, reason="node executable not found")

#: runs in node: argv[2] = the repository root, argv[3] = a JSON job; prints one JSON document.  The page's own code - not a copy of it.
BRIDGE = r"""
const fs = require("node:fs");
const path = require("node:path");
const L = require(path.join(process.argv[2], "bench", "label", "label.js"));
const job = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = {};
if (job.op === "validate") {
  out.results = job.cases.map((c) => L.validateLabel(c.schema, c.value).length);
}
if (job.op === "session") {
  const data = new Map();
  const storage = { getItem: (k) => (data.has(k) ? data.get(k) : null), setItem: (k, v) => void data.set(k, String(v)), removeItem: (k) => void data.delete(k) };
  let tick = 0;
  const now = () => `2026-11-20T10:${String(Math.floor(tick / 60)).padStart(2, "0")}:${String(tick++ % 60).padStart(2, "0")}.000Z`;
  const controller = L.createController({ storage, now });
  out.welcome = L.renderHtml(L.createController({ storage: null }).view());
  out.loaded = controller.load(job.text, "set.jsonl");
  controller.dispatch({ type: "labeler", value: "测试者" });
  out.opened = L.renderHtml(controller.view());
  controller.dispatch({ type: "saveNext" }); // nothing answered yet: a warning
  out.warned = L.renderHtml(controller.view());
  out.htmls = [];
  out.after = [];
  out.picks = {};
  for (let guard = 0; guard < 1000; guard++) {
    const view = controller.view();
    out.htmls.push(L.renderHtml(view));
    const cur = view.current;
    const picks = {};
    cur.form.fields.forEach((field, index) => {
      if (field.options) {
        picks[field.name] = (cur.index + index) % field.options.length;
        controller.dispatch({ type: "choose", field: field.name, option: picks[field.name] });
      } else if (field.type === "text") {
        controller.dispatch({ type: "text", field: field.name, value: "页面里写的话 " + cur.number });
      }
    });
    out.picks[cur.blindId] = picks;
    controller.dispatch({ type: "saveNext" });
    out.after.push(L.renderHtml(controller.view()));
    if (controller.view().current.index === cur.index) break; // the last one
  }
  const exported = controller.dispatch({ type: "export" });
  out.exported = exported.download ? exported.download.text : "";
  out.exportPage = L.renderHtml(controller.view());
  controller.dispatch({ type: "jump", index: 0 });
  out.reviewPage = L.renderHtml(controller.view());
  out.done = controller.view().done;
  out.total = controller.view().total;
  out.stored = Array.from(data.keys());
}
process.stdout.write(JSON.stringify(out));
"""


def _node(tmp_path: Path, job: dict[str, Any]) -> dict[str, Any]:
    (tmp_path / "bridge.js").write_text(BRIDGE, encoding="utf-8")
    (tmp_path / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "bridge.js"), str(ROOT), str(tmp_path / "job.json")], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


class _Balance(HTMLParser):
    """Checks what a browser would silently repair: every tag closed in order, no attribute twice on a tag, no id used twice."""

    VOID = {"input", "br", "hr", "img", "meta", "link"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.problems: list[str] = []
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = [name for name, _ in attrs]
        if len(names) != len(set(names)):
            self.problems.append(f"<{tag}> has an attribute twice: {names}")
        self.ids += [value or "" for name, value in attrs if name == "id"]
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"</{tag}> where {self.stack[-1:] or 'nothing'} is open")
        else:
            self.stack.pop()


def _problems_of(html: str) -> list[str]:
    parser = _Balance()
    parser.feed(html)
    parser.close()
    return parser.problems + [f"not closed: {tag}" for tag in parser.stack] + [f"id twice: {i}" for i in set(parser.ids) if parser.ids.count(i) > 1]


def test_the_html_the_page_draws_is_well_formed(tmp_path: Path) -> None:
    items = [make_item(n) for n in range(4)] + [make_s2(100 + n) for n in range(4)] + [make_e2(200 + n) for n in range(4)] + [make_m2(300 + n) for n in range(4)]
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in build_set(items, n=4, seed=1).rows)
    result = _node(tmp_path, {"op": "session", "text": text})
    pages = {"welcome": result["welcome"], "opened": result["opened"], "warned": result["warned"], "export": result["exportPage"], "review": result["reviewPage"]}
    pages.update({f"item {i}": html for i, html in enumerate(result["htmls"])})
    pages.update({f"after {i}": html for i, html in enumerate(result["after"])})
    assert len(pages) == 5 + 16 + 16
    for name, html in pages.items():
        assert _problems_of(html) == [], name
        assert html.startswith('<div class="app">') and html.endswith("</div>"), name
    assert 'class="notice notice-warn"' in result["warned"] and "还差" in result["warned"]
    assert 'class="notice notice-ok"' in result["exportPage"] and "已导出 16 条" in result["exportPage"]
    assert 'aria-pressed="true"' in result["reviewPage"] and "✔ 已标" in result["reviewPage"], "going back shows what was answered"
    assert _problems_of("<div><p></div>") != [] and _problems_of("<div></div></div>") != [], "(the check itself can fail)"


def test_the_node_tests_of_the_labelling_page_pass() -> None:
    done = subprocess.run([NODE, str(PAGE_TESTS)], capture_output=True, text=True, encoding="utf-8", timeout=180, cwd=ROOT)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    last = done.stdout.strip().splitlines()[-1]
    assert last.endswith(" passed") and last.split()[0].split("/")[0] == last.split()[0].split("/")[1], last
    assert int(last.split("/")[1].split()[0]) >= 50, "the page's tests are all there"


def test_the_script_of_the_page_is_valid_javascript() -> None:
    done = subprocess.run([NODE, "--check", str(PAGE)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr


def _cases() -> list[dict[str, Any]]:
    """Every schema of the repository's rubrics with its worked examples and what a careless person could make of them."""
    cases = []
    for rubric in RUBRICS.values():
        for example in rubric_examples(rubric):
            verdict = example["verdict"]
            cases.append({"schema": rubric.schema, "value": verdict})
            for name in verdict:
                without = {k: v for k, v in verdict.items() if k != name}
                cases.append({"schema": rubric.schema, "value": without})
                cases.append({"schema": rubric.schema, "value": {**verdict, name: ["a list"]}})
                cases.append({"schema": rubric.schema, "value": {**verdict, name: 7}})
                cases.append({"schema": rubric.schema, "value": {**verdict, name: "no such option"}})
                cases.append({"schema": rubric.schema, "value": {**verdict, name: not verdict[name] if isinstance(verdict[name], bool) else None}})
            cases.append({"schema": rubric.schema, "value": {**verdict, "extra": 1}})
        cases += [{"schema": rubric.schema, "value": value} for value in ({}, [], "x", 3, None)]
    cases += [
        {"schema": {"type": "integer"}, "value": 1.5}, {"schema": {"type": "integer"}, "value": 2}, {"schema": {"type": "number"}, "value": True},
        {"schema": {"const": "a"}, "value": "b"}, {"schema": {"oneOf": [{"type": "string"}, {"enum": ["a"]}]}, "value": "a"},
        {"schema": {"type": "array", "items": {"type": "string", "enum": ["a"]}}, "value": ["a", "b"]},
    ]
    return cases


def test_the_page_and_the_judges_check_a_label_the_same_way(tmp_path: Path) -> None:
    cases = _cases()
    assert len(cases) > 150
    page = _node(tmp_path, {"op": "validate", "cases": cases})["results"]
    python = [len(validate_verdict(case["schema"], case["value"])) for case in cases]
    assert [bool(n) for n in page] == [bool(n) for n in python], [(i, cases[i]["value"]) for i, (a, b) in enumerate(zip(page, python)) if bool(a) != bool(b)][:5]
    assert page == python, "the same number of problems, too"
    assert sum(1 for n in python if n == 0) >= 12 and sum(1 for n in python if n) >= 100, "both outcomes are in the cases"


def test_a_set_labelled_in_the_page_is_what_calibrate_reads(tmp_path: Path) -> None:
    items = [make_item(n, bot=["bot:Alice@no-people", "bot:Bob", "legacy"][n % 3]) for n in range(12)]
    items += [make_s2(100 + n, cards=("A", "B", "C", "D")) for n in range(12)] + [make_e2(200 + n) for n in range(12)] + [make_m2(300 + n) for n in range(12)]
    sample = build_set(items, n=12, seed=3)
    assert len(sample.rows) == 48 and {row["rubric"] for row in sample.rows} == {"L3", "S2", "E2", "M2"}
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in sample.rows)

    result = _node(tmp_path, {"op": "session", "text": text})
    assert result["loaded"]["ok"] is True and (result["done"], result["total"]) == (48, 48)
    assert len(result["stored"]) == 2, "the progress of the file and the preferences are all the page keeps"

    # nothing that names a bot, the run, a situation or the truth ever reached a page the person saw
    shown = "\n".join(result["htmls"])
    assert len(result["htmls"]) == 48
    # ("N6" is only in the truth of the items - the wrong answer; "correct" / "wrong" are not looked for: they are two options of M2)
    for secret in ["bot:Alice", "bot:Bob", "legacy", "20261120-aaaa", "sit-", "item_id", "no-people", "BotBrain", "N6", "秘密思考"]:
        assert secret not in shown, secret
    assert "决定性事实" in shown and "风格卡" in shown and "候选 X" in shown and "标准答案" in shown, "and the four kinds of item were all drawn"

    # what the page exports is a label of the rubric's schema - and the stricter schema of the item
    labels = [json.loads(line) for line in result["exported"].splitlines()]
    assert len(labels) == 48 and result["exported"].endswith("\n")
    by_blind = {row["blind_id"]: row for row in sample.rows}
    assert [label["blind_id"] for label in labels] == [row["blind_id"] for row in sample.rows], "in the order the person saw them"
    for label in labels:
        assert list(label) == ["format", "version", "blind_id", "rubric", "rubric_version", "label", "labeler", "ts"]
        assert (label["format"], label["version"], label["labeler"]) == ("arena-bench-label", 1, "测试者")
        row = by_blind[label["blind_id"]]
        assert (label["rubric"], label["rubric_version"]) == (row["rubric"], row["rubric_version"])
        assert validate_verdict(RUBRICS[label["rubric"]].schema, label["label"]) == [], label
        assert validate_verdict(row["schema"], label["label"]) == [], label
        assert list(label["label"]) == list(RUBRICS[label["rubric"]].schema["properties"]), "the fields of the schema, in its order"
        chosen = label["label"][row["primary"]]
        assert chosen in row["schema"]["properties"][row["primary"]]["enum"]
        assert label["ts"].startswith("2026-11-20T10:")
    s2 = [label["label"] for label in labels if label["rubric"] == "S2"]
    assert {entry["match"] for entry in s2} <= {"A", "B", "C", "D"} and len({entry["match"] for entry in s2}) == 4, "the cards are the answers, and all were chosen"
    assert all(entry["cues"].startswith("页面里写的话") for entry in s2), "a text typed in the page is the label's text"

    # ... and calibrate takes them: in every rubric the judge agrees on three items of four (the rubrics are interleaved: count inside each one)
    judgments = []
    rank: dict[str, int] = {}
    for label in labels:
        verdict = copy.deepcopy(label["label"])
        rank[label["rubric"]] = rank.get(label["rubric"], -1) + 1
        if rank[label["rubric"]] % 4 == 3:
            primary = by_blind[label["blind_id"]]["primary"]
            options = by_blind[label["blind_id"]]["schema"]["properties"][primary]["enum"]
            verdict[primary] = next(option for option in options if option != verdict[primary])
        judgments.append(judgment_of(label["blind_id"], verdict, rubric=label["rubric"]))
    report = build_report(labels, judgments, min_n=10)
    assert sorted(report["rubrics"]) == ["E2", "L3", "M2", "S2"] and report["problems"] == []
    for rubric_id, entry in report["rubrics"].items():
        assert (entry["n"], entry["invalid_labels"], entry["invalid_judgments"], entry["stale_pairs"], entry["unpaired_labels"]) == (12, 0, 0, 0, 0), rubric_id
        assert entry["agreement"] == 0.75, rubric_id
        assert entry["kappa"] is not None and entry["kappa"] > 0, (rubric_id, entry["kappa"])
    agreeing = [judgment_of(label["blind_id"], label["label"], rubric=label["rubric"]) for label in labels]
    perfect = build_report(labels, agreeing, min_n=10)
    assert all(entry["kappa"] == 1.0 and entry["counts_for_acceptance"] for entry in perfect["rubrics"].values())
    small = build_report(labels, agreeing)
    assert all(entry["kappa"] == 1.0 and not entry["counts_for_acceptance"] and entry["reason"] == "n 太少（12 < 20）" for entry in small["rubrics"].values()), "twelve pairs are too few by default"
