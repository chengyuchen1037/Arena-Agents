"""``docs/real-model-runs.md`` says what ``--help`` of the two real-model tools says: the same options with the same defaults, only options the scripts have in
the command examples, and the exit codes of both.  (``tests/test_docs.py`` checks the links and the privacy of every document, this one the contents.)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import tools_fakes  # noqa: F401  (puts tools/ on the path)

import init_default_bots
import real_smoke

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "real-model-runs.md"
SCRIPTS = [("tools/real_smoke.py", real_smoke), ("tools/init_default_bots.py", init_default_bots)]


def text() -> str:
    return DOC.read_text(encoding="utf-8").replace("\r\n", "\n")


def section(heading: str) -> str:
    """The part of the document under ``## `<heading>`: …`` (up to the next ``## ``)."""
    match = re.search(rf"^## `{re.escape(heading)}`.*?$(.*?)(?=^## |\Z)", text(), re.MULTILINE | re.DOTALL)
    assert match, f"the document has no section for {heading}"
    return match.group(1)


def options_of(module) -> dict[str, object]:
    """``{first option string: default}`` of the parser of a script (the help option left out)."""
    return {a.option_strings[0]: a.default for a in module.build_parser()._actions if a.option_strings and a.dest != "help"}


def shown(default: object) -> str:
    """How the documentation writes a default: ``—`` for none / a switch that is off, the number or the word otherwise."""
    if default is None or default is False:  # (identity: ``0 == False``, and a default of 0 is a number the table shows)
        return "—"
    return f"{default:g}" if isinstance(default, float) else str(default)


@pytest.mark.parametrize("heading, module", SCRIPTS, ids=[h for h, _ in SCRIPTS])
def test_the_option_table_is_the_parser_of_the_script(heading: str, module) -> None:
    rows = dict(re.findall(r"^\| `(--[a-z-]+)` \| (.*?) \|", section(heading), re.MULTILINE))
    options = options_of(module)
    assert set(rows) == set(options), f"the table of {heading} and its --help differ: {sorted(set(rows) ^ set(options))}"
    for option, default in options.items():
        assert rows[option].strip().strip("`") == shown(default), f"{heading} {option}: the table says {rows[option]!r}, the parser {default!r}"


@pytest.mark.parametrize("heading, module", SCRIPTS, ids=[h for h, _ in SCRIPTS])
def test_every_command_example_uses_options_the_script_has(heading: str, module) -> None:
    known = set(options_of(module)) | {"--help"}
    examples = [line for block in re.findall(r"```\n(.*?)```", text(), re.DOTALL) for line in block.splitlines() if line.startswith(f"python {heading}")]
    assert examples, f"no command example of {heading}"
    for line in examples:
        used = set(re.findall(r"(?<!\S)(--[a-z-]+)", line))
        assert used <= known, f"{line!r} uses {sorted(used - known)}"


def test_the_help_of_both_scripts_points_to_the_document_and_the_exit_codes_are_in_both() -> None:
    body = text()
    for _heading, module in SCRIPTS:
        help_text = module.build_parser().format_help()
        assert "docs/real-model-runs.md" in help_text and "Exit code:" in help_text
    # the exit codes of real_smoke.py and of init_default_bots.py (each section lists its own)
    smoke_codes = section("tools/real_smoke.py")
    assert "退出码：0 跑完了" in smoke_codes and "2 模型绑定缺失" in smoke_codes and "3 对局没能开始" in smoke_codes
    init_codes = section("tools/init_default_bots.py")
    assert "退出码：0 要做的都做完了；1 有 bot 失败" in init_codes and "2 参数不对" in init_codes
    for name in ("--keep", "ARENA_DATA_DIR", ".demo-data", "--write-library", "--dry-run", "--check", "tests/test_bots_library_fresh.py"):
        assert name in body


def test_the_documents_hard_promises_are_the_scripts_defaults() -> None:
    """What the prose says about budgets: ``--max-calls`` is a hard budget (60 for the smoke test, 200 for the library run) and the one-shots add 2 + 8 per bot."""
    assert real_smoke.REFINE_CALLS == 2 and real_smoke.REVIEW_CALLS_PER_BOT == 8
    assert real_smoke.parse_args(["--from", "x"]).max_calls == 60 and init_default_bots.DEFAULT_MAX_CALLS == 200
    body = text()
    assert "`2 + 8N`" in body and "超出的调用不会发出" in body
