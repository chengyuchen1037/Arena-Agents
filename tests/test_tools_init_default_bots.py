"""``tools/init_default_bots.py``: the run that initializes the default bot library for real before a release - here with the demo's scripted bots as the
model, on a COPY of the library (the shipped ``bots/`` is never written by a test).

Nothing here calls a model: the tool takes a ``port_factory`` for the learning runs and its call budget counts and caps the calls of that fake port as it
does the real client's.  What is checked is the tool: the scratch data directory (the source is only read, the scratch is gone), the choice of the model
and of the bots, the real learning run through ``start_init``, the report, what ``--write-library`` copies and what it must not touch, the hard limits
(calls, time), ``--dry-run`` and ``--check`` - and that what it writes is what the application finds ``ready`` in a fresh installation.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any, Callable

import pytest
from tools_fakes import demo_factory, make_source, tree

import init_default_bots

ROOT = Path(__file__).resolve().parents[1]
NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(init_default_bots, "POLL_S", 0.02)


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    """A copy of the shipped default library: what the tool starts from and writes into."""
    copy = tmp_path / "library" / "bots"
    shutil.copytree(ROOT / "bots", copy)
    return copy


def run_tool(tmp_path: Path, library: Path, *argv: str, port_factory: Callable[[Any], Any] | None = None, source: Path | None = None) -> tuple[int, Path]:
    """Run the tool in-process on ``library``; ``(exit code, scratch root)``."""
    source = source or make_source(tmp_path)
    root = tmp_path / "scratch-root"
    args = init_default_bots.parse_args(["--from", str(source), "--library", str(library), *argv])
    code = asyncio.run(init_default_bots.run(args, port_factory=port_factory or demo_factory(), scratch_root=root))
    return code, root


def kept_scratch(out: str) -> Path:
    match = re.search(r"^scratch kept at (.+)$", out, re.MULTILINE)
    assert match, out
    return Path(match.group(1).strip())


def meta(library: Path, name: str) -> dict[str, Any]:
    return json.loads((library / name / "bot.json").read_text(encoding="utf-8"))


def failing_for(name: str) -> Callable[[Any], Any]:
    """The demo's bots, except that the model of the bot called ``name`` is down."""
    demo = demo_factory()

    def factory(orch: Any) -> Any:
        port = demo(orch)
        store = orch._bots()
        bad = {bot_id for bot_id in store.lineup() if store.name_of(bot_id) == name}

        class Port:
            async def complete(self, agent_id, messages, tools, *, max_tokens=None):
                if agent_id in bad:
                    raise RuntimeError("the model is down")
                return await port.complete(agent_id, messages, tools, max_tokens=max_tokens)

        return Port()

    return factory


# ===================================================================================================== dry run, no write
def test_a_dry_run_says_what_would_be_done_and_calls_no_model(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = make_source(tmp_path)
    before_source, before_library = tree(source), tree(library)
    made: list[Any] = []

    def never(orch: Any) -> Any:
        made.append(orch)  # (a port is only made when a learning run starts)
        raise AssertionError("a dry run must not ask a model")

    code, root = run_tool(tmp_path, library, "--dry-run", "--max-calls", "77", port_factory=never, source=source)
    out = capsys.readouterr().out
    assert code == 0 and not made, out
    assert "initialize the default bot library" in out
    assert re.search(r"model: Fake Provider / big  \(provider \w{32}; prices in \$1.0/M, cache read \$1.0/M, out \$2.0/M; context window 1000000, reasoning high\)", out)
    assert all(f"{name} [uninitialized]" in out for name in NAMES)  # (what the library has: L0 only)
    assert re.search(r"contract \w{12}…  scenario werewolf_12p \w{12}…  study materials: none", out)
    assert "to initialize (not up to date): " + ", ".join(NAMES) in out
    assert "budget: at most 77 model calls (about 5-8 per bot), 120 s per bot, 400 s in all, concurrency unlimited" in out
    assert "dry run: no model was called, nothing was written" in out
    assert tree(source) == before_source and tree(library) == before_library and not list(root.iterdir())


def test_without_write_library_the_result_is_reported_and_the_library_stays_as_it_is(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = make_source(tmp_path)
    before_source, before_library = tree(source), tree(library)
    code, root = run_tool(tmp_path, library, source=source)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "NOTE: without --write-library (and --keep) the result is only reported and then deleted" in out
    assert re.search(r"job \w+: 12 bots at the same time, at most 120s each", out) and "=== initialization ===" in out
    rows = re.findall(r"^  (\w+) +ready +\d+s +calls=(\d+)  in=(\d+) cached=(\d+) out=(\d+)  wrote: (.+)$", out, re.MULTILINE)
    assert [r[0] for r in rows] == NAMES and all("arch_notes" in r[5] and "digest" in r[5] and "playbook" in r[5] for r in rows)
    assert re.search(r"total: 48 calls, \d+ tokens, \$[\d.]+", out) and "model calls: 48 of 200 (refused over the budget: 0); errors none" in out
    assert "library NOT changed (no --write-library)" in out and "=== writing" not in out
    assert tree(library) == before_library and tree(source) == before_source and not list(root.iterdir())


# =========================================================================================================== write library
def test_write_library_puts_the_memory_and_the_record_of_every_bot_that_succeeded_into_its_folder_and_nothing_else(
    tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = tree(library)
    code, root = run_tool(tmp_path, library, "--write-library")
    out = capsys.readouterr().out
    assert code == 0, out
    assert f"=== writing {library} ===" in out and "library check: every initialized bot is up to date" in out
    after = tree(library)
    new = sorted(set(after) - set(before))
    assert new == sorted(
        f"{name}/{path}" for name in NAMES
        for path in ("arch/notes.md", "rules/werewolf_12p/digest.md", "rules/werewolf_12p/playbook.md", "rules/werewolf_12p/questions.md")
    )
    changed = sorted(path for path in before if after[path] != before[path])
    assert changed == sorted(f"{name}/bot.json" for name in NAMES)  # (only the record: the L0, the README, the names and avatars are as they were)
    for name in NAMES:
        old, now = json.loads(before[f"{name}/bot.json"]), meta(library, name)
        assert list(now) == ["format", "name", "avatar", "init"] and {k: now[k] for k in ("format", "name", "avatar")} == old
        init = now["init"]
        assert init["status"] == "ready" and init["model"] == "big" and init["error"] is None and init["scenario_id"] == "werewolf_12p"
        assert set(init["hashes"]) == {"core", "contract", "scenario", "materials"} and init["hashes"]["materials"] == ""
        assert isinstance(init["at"], int) and init["duration_s"] is not None
        raw = (library / name / "bot.json").read_bytes()
        assert raw.endswith(b"\n") and b"\r" not in raw  # (LF line ends, a final newline: what tests/test_bots_defaults.py asks of the library)
        for path in ("arch/notes.md", "rules/werewolf_12p/digest.md", "rules/werewolf_12p/playbook.md"):
            data = (library / name / path).read_bytes()
            assert data.strip() and b"\r" not in data and data.endswith(b"\n"), (name, path)
    assert not list(root.iterdir())


def test_what_is_written_is_what_a_fresh_installation_finds_ready_and_able_to_play(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from app.storage.bots import BotStore

    code, _root = run_tool(tmp_path, library, "--write-library")
    assert code == 0, capsys.readouterr().out
    hashes = init_default_bots.fresh_install_hashes()  # (what the stores of a new data directory hash: this test's own isolated one)
    assert init_default_bots.library_problems(library, hashes) == []
    store = BotStore(tmp_path / "a-new-data-directory", seats=12, library_dir=library)
    store.bootstrap()  # (the first start of an installation: the table is made from the library)
    for bot_id in store.lineup():
        summary = store.summary(bot_id, sources=hashes)
        assert summary["init"]["status"] == "ready" and summary["init"]["stale"] == [] and summary["init"]["model"] == "big", summary["name"]
        assert store.problems(bot_id, sources=hashes) == []  # (the start check has nothing to say about it)
        mind = store.load_mind(bot_id, hashes.scenario_id)
        assert mind.arch_notes.strip() and mind.rules.digest.strip() and mind.rules.playbook and mind.core.complete  # (what a game mounts)


def test_a_bot_that_failed_is_not_touched_and_the_others_are_written(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = tree(library)
    code, _root = run_tool(tmp_path, library, "--write-library", port_factory=failing_for("Kate"))
    out = capsys.readouterr().out
    assert code == 1, out
    assert re.search(r"^  Kate +failed ", out, re.MULTILINE) and "why: 调用模型失败（RuntimeError：the model is down）" in out
    assert "FAILED (not written; run again with --only to try just these): Kate (调用模型失败" in out
    after = tree(library)
    assert {path for path in after if path.startswith("Kate/")} == {path for path in before if path.startswith("Kate/")}
    assert all(after[path] == before[path] for path in before if path.startswith("Kate/"))  # (Kate: exactly as it was)
    assert all(f"{name}/arch/notes.md" in after for name in NAMES if name != "Kate") and "Kate: " not in out.split("=== writing")[1]


def test_only_the_named_bots_are_initialized_and_an_unknown_name_is_refused_with_the_names_there_are(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _root = run_tool(tmp_path, library, "--only", "kate, ALICE", "--write-library")
    out = capsys.readouterr().out
    assert code == 0, out
    assert re.search(r"job \w+: 2 bots at the same time", out) and "to initialize (--only): Kate, Alice" in out
    assert re.search(r"total: 8 calls", out) and "model calls: 8 of 200" in out  # (4 calls each: only the two were asked)
    done = sorted(name for name in NAMES if (library / name / "arch" / "notes.md").is_file())
    assert done == ["Alice", "Kate"] and meta(library, "Bob").get("init") is None and meta(library, "Kate")["init"]["status"] == "ready"
    with pytest.raises(SystemExit, match="no such bot in the library: Zed; the bots: Alice, Bob, Carol"):
        run_tool(tmp_path, library, "--only", "Alice,Zed")


def test_the_default_is_the_bots_that_are_not_up_to_date_and_all_takes_everybody(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_tool(tmp_path, library, "--write-library")[0] == 0
    capsys.readouterr()
    code, _root = run_tool(tmp_path, library, "--write-library")  # (everybody is ready now)
    out = capsys.readouterr().out
    assert code == 0 and "nothing to do: every bot of the library is up to date (--all initializes them again)" in out and "job " not in out
    (library / "Leo" / "core" / "identity.md").write_text(
        (library / "Leo" / "core" / "identity.md").read_text(encoding="utf-8").rstrip("\n") + "他最近迷上了数独。\n", encoding="utf-8", newline="\n"
    )  # (the L0 of one bot is edited: its initialization is out of date)
    code, _root = run_tool(tmp_path, library, "--dry-run")
    out = capsys.readouterr().out
    assert "Leo [stale/core]" in out and "Alice [ready]" in out and "to initialize (not up to date): Leo" in out
    code, _root = run_tool(tmp_path, library, "--all", "--dry-run")
    assert "to initialize (--all): " + ", ".join(NAMES) in capsys.readouterr().out
    code, _root = run_tool(tmp_path, library, "--write-library")  # (only Leo is done again)
    out = capsys.readouterr().out
    assert code == 0 and re.search(r"job \w+: 1 bots at the same time", out) and "library check: every initialized bot is up to date" in out


def test_an_initialization_is_done_from_scratch_whatever_the_library_had(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The memory a library bot already has is not revised: the run starts from the L0 only, so the result does not depend on the last one."""
    assert run_tool(tmp_path, library, "--write-library")[0] == 0
    capsys.readouterr()
    (library / "Bob" / "arch" / "notes.md").write_text("旧的操作笔记，不应该被带进新的初始化。\n", encoding="utf-8", newline="\n")
    code, _root = run_tool(tmp_path, library, "--only", "Bob", "--write-library")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "旧的操作笔记" not in (library / "Bob" / "arch" / "notes.md").read_text(encoding="utf-8")  # (replaced: the notes are this run's)


# ===================================================================================================== the model and the limits
def test_the_model_can_be_named_by_the_providers_name_or_id_and_is_the_one_written_into_the_record(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = make_source(tmp_path)
    provider_id = json.loads((source / "providers.json").read_text(encoding="utf-8"))["providers"][0]["id"]
    code, _root = run_tool(tmp_path, library, "--only", "Alice", "--model", "fake provider/small", "--write-library", source=source)  # (the name: any case)
    out = capsys.readouterr().out
    assert code == 0 and "model: Fake Provider / small" in out and meta(library, "Alice")["init"]["model"] == "small"
    code, _root = run_tool(tmp_path, library, "--only", "Bob", "--model", f"{provider_id}/big", "--write-library", source=source)  # (the id)
    assert code == 0 and meta(library, "Bob")["init"]["model"] == "big"
    capsys.readouterr()


def test_a_model_that_does_not_exist_is_refused_with_what_there_is_to_choose_from(tmp_path: Path, library: Path) -> None:
    source = make_source(tmp_path)
    before = tree(library)
    with pytest.raises(SystemExit, match="--model must be PROVIDER/MODEL"):
        run_tool(tmp_path, library, "--model", "nonsense", source=source)
    with pytest.raises(SystemExit, match=r"no \(single\) provider 'Ghost' .*Fake Provider \(\w{32}\): big, small"):
        run_tool(tmp_path, library, "--model", "Ghost/big", source=source)
    with pytest.raises(SystemExit, match=r"the model 'ghost-model' cannot be used \(ModelNotFound\).*Fake Provider"):
        run_tool(tmp_path, library, "--model", "Fake Provider/ghost-model", source=source)
    assert tree(library) == before and not list((tmp_path / "scratch-root").iterdir())  # (nothing was done; the scratch directories are gone)


def test_a_provider_without_a_key_is_refused(tmp_path: Path, library: Path) -> None:
    from app.providers.models import ModelBinding
    from app.providers.store import ProviderStore
    from app.storage.settings import SettingsStore

    source = make_source(tmp_path)
    store = ProviderStore(source)
    keyless = store.create_provider("No Key", "openai_chat", "https://example.invalid", "")
    store.add_model(keyless.id, "m", 1.0, 2.0)
    SettingsStore(source).save_default_bindings({"moderator": ModelBinding(provider_id=keyless.id, model_id="m")})
    with pytest.raises(SystemExit, match="the provider 'No Key' has no API key stored"):
        run_tool(tmp_path, library, source=source)


def test_the_call_budget_is_hard_and_the_bots_that_did_not_get_their_calls_are_not_written(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _root = run_tool(tmp_path, library, "--max-calls", "10", "--write-library")
    out = capsys.readouterr().out
    assert code == 1, out
    assert "model calls: 10 of 10" in out and "BudgetExhausted" in out and "the call budget of 10 model calls is spent" in out
    ready = re.findall(r"^  (\w+) +ready ", out, re.MULTILINE)
    assert len(ready) <= 2  # (four calls each: at most two bots can finish; the bots run at the same time, so maybe none does)
    written = sorted(name for name in NAMES if (library / name / "arch" / "notes.md").is_file())
    assert written == sorted(ready)  # exactly the bots that succeeded
    assert out.count("FAILED (not written; run again with --only to try just these):") == 1


def test_the_time_limit_cancels_the_run_and_nothing_is_written(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class Slow:
        async def complete(self, agent_id, messages, tools, *, max_tokens=None):
            await asyncio.sleep(30)

    before = tree(library)
    code, root = run_tool(tmp_path, library, "--timeout", "1", "--write-library", port_factory=lambda orch: Slow())
    out = capsys.readouterr().out
    assert code == 1 and "time limit reached; cancelling the initialization (the bots that were not done are not written)" in out, out
    assert not re.search(r"^  \w+ +ready ", out, re.MULTILINE) and "FAILED (not written" in out and "=== writing" in out  # (nothing to write)
    assert tree(library) == before and not list(root.iterdir())


def test_the_scratch_directory_can_be_kept_and_holds_the_bots_as_they_were_initialized(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _root = run_tool(tmp_path, library, "--only", "Eve", "--keep")
    out = capsys.readouterr().out
    scratch = kept_scratch(out)
    try:
        assert code == 0 and "NOTE: without" not in out and "library NOT changed (no --write-library); the scratch directory is kept" in out
        lineup = json.loads((scratch / "bots" / "lineup.json").read_text(encoding="utf-8"))["order"]
        eve = next(b for b in lineup if json.loads((scratch / "bots" / b / "bot.json").read_text(encoding="utf-8"))["name"] == "Eve")
        assert json.loads((scratch / "bots" / eve / "bot.json").read_text(encoding="utf-8"))["init"]["status"] == "ready"
        assert (scratch / "bots" / eve / "arch" / "notes.md").is_file() and (scratch / "providers.json").is_file()
        usage = json.loads((scratch / "bots" / "usage.json").read_text(encoding="utf-8"))
        assert usage["calls"] == 4  # (what the initialization cost is booked there)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_the_bots_are_bound_to_the_model_by_bot_in_the_scratch_and_a_v014_source_is_read_too(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = make_source(tmp_path, by_bot_id=True, seats=12)
    before = tree(source)
    code, _root = run_tool(tmp_path, library, "--only", "Carol", "--model", "Fake Provider/small", "--keep", source=source)
    out = capsys.readouterr().out
    scratch = kept_scratch(out)
    try:
        assert code == 0, out
        lineup = json.loads((scratch / "bots" / "lineup.json").read_text(encoding="utf-8"))["order"]
        bindings = json.loads((scratch / "settings.json").read_text(encoding="utf-8"))["default_bindings"]
        assert {bindings[b]["model_id"] for b in lineup} == {"small"} and bindings["moderator"]["model_id"] == "small"  # (every seat: the model asked for)
        assert tree(source) == before
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ============================================================================================================== --check
def test_check_passes_for_the_library_as_shipped_and_for_a_fresh_one_and_tells_what_is_out_of_date(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = init_default_bots.parse_args(["--check", "--library", str(library)])
    assert asyncio.run(init_default_bots.run(args, scratch_root=tmp_path / "root")) == 0
    out = capsys.readouterr().out
    assert re.search(r"the default bot library is up to date \(0 initialized bot\(s\) of 12; contract \w{12}, scenario werewolf_12p \w{12}\)", out)
    assert run_tool(tmp_path, library, "--write-library")[0] == 0
    capsys.readouterr()
    assert asyncio.run(init_default_bots.run(args, scratch_root=tmp_path / "root")) == 0
    assert "up to date (12 initialized bot(s) of 12" in capsys.readouterr().out
    # the L0 of one bot is edited afterwards, and so is the init record of another (a hash that is not the contract's)
    identity = library / "Frank" / "core" / "identity.md"
    identity.write_text(identity.read_text(encoding="utf-8").rstrip("\n") + "又加了一句。\n", encoding="utf-8", newline="\n")
    record = meta(library, "Grace")
    record["init"]["hashes"]["contract"] = "0" * 64
    (library / "Grace" / "bot.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    assert asyncio.run(init_default_bots.run(args, scratch_root=tmp_path / "root")) == 1
    out = capsys.readouterr().out
    assert "the default bot library is OUT OF DATE (2 problem(s)):" in out
    assert "bots/Frank 的初始化已经过期（底层设定在初始化之后改过）：请重新运行 tools/init_default_bots.py" in out
    assert "bots/Grace 的初始化已经过期（官方契约在初始化之后改过）" in out
    assert not list((tmp_path / "root").iterdir())


def test_check_needs_no_source_and_the_other_runs_do() -> None:
    assert init_default_bots.parse_args(["--check"]).check is True
    with pytest.raises(SystemExit) as exit_info:
        init_default_bots.parse_args([])
    assert exit_info.value.code == 2
    for bad in (["--only", "A", "--all"], ["--max-calls", "0"]):
        with pytest.raises(SystemExit) as exit_info:
            init_default_bots.parse_args(["--from", "x", *bad])
        assert exit_info.value.code == 2, bad
    args = init_default_bots.parse_args(["--from", "x"])
    assert (args.max_calls, args.timeout, args.concurrency, args.write_library, args.dry_run, args.all, args.keep) == (200, 400.0, 0, False, False, False, False)


# ================================================================================================================== help
def test_the_help_text_names_every_option_and_the_exit_codes() -> None:
    text = init_default_bots.build_parser().format_help()
    for option in ("--from", "--model", "--only", "--all", "--write-library", "--library", "--dry-run", "--check", "--max-calls", "--timeout", "--concurrency", "--keep"):
        assert option in text
    assert "docs/real-model-runs.md" in text and "Exit code:" in text and "git diff bots/" in text


def test_a_folder_that_is_not_a_library_is_refused_before_anything_is_started(tmp_path: Path) -> None:
    empty = tmp_path / "not-a-library"
    empty.mkdir()
    with pytest.raises(SystemExit, match="no default bot library at .*one folder per bot"):
        run_tool(tmp_path, empty, "--dry-run")
    args = init_default_bots.parse_args(["--check", "--library", str(tmp_path / "missing")])
    with pytest.raises(SystemExit, match="no default bot library"):
        asyncio.run(init_default_bots.run(args, scratch_root=tmp_path / "root"))
    assert not (tmp_path / "scratch-root").exists() and not (tmp_path / "root").exists()  # (not even a scratch directory was made)


def test_the_production_path_is_the_engines_own_client_with_only_the_network_faked(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``port_factory``: what the tool meters is the real ``LLMClient`` behind the real model port (concurrency gate, error log, the binding of every bot)."""
    from tools_fakes import fake_network

    seen = fake_network(monkeypatch)
    source = make_source(tmp_path)
    args = init_default_bots.parse_args(["--from", str(source), "--library", str(library), "--only", "Alice,Bob", "--max-calls", "30", "--write-library", "--concurrency", "2"])
    code = asyncio.run(init_default_bots.run(args, scratch_root=tmp_path / "scratch-root"))
    out = capsys.readouterr().out
    assert code == 0, out
    assert len(seen) == 8 and {s["model"] for s in seen} == {"big"} and all(s["tools"] and "write_memory" in s["tools"] and not s["moderator"] for s in seen)  # (the model of the bots)
    assert "model calls: 8 of 30 (refused over the budget: 0); errors none" in out and "total: 8 calls" in out
    assert sorted(name for name in NAMES if (library / name / "arch" / "notes.md").is_file()) == ["Alice", "Bob"] and meta(library, "Alice")["init"]["model"] == "big"
    assert "library check: every initialized bot is up to date" in out


def test_the_budget_caps_the_real_client_too(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools_fakes import fake_network

    seen = fake_network(monkeypatch)
    source = make_source(tmp_path)
    args = init_default_bots.parse_args(["--from", str(source), "--library", str(library), "--only", "Alice,Bob", "--max-calls", "3"])
    code = asyncio.run(init_default_bots.run(args, scratch_root=tmp_path / "scratch-root"))
    out = capsys.readouterr().out
    assert code == 1 and len(seen) == 3  # (three requests were sent, every other one was refused before it left)
    assert "model calls: 3 of 3" in out and "the call budget of 3 model calls is spent" in out


def test_the_concurrency_limit_holds_for_the_twelve_bots(tmp_path: Path, library: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools_fakes import fake_network

    network = fake_network(monkeypatch, delay=0.02)
    source = make_source(tmp_path)
    args = init_default_bots.parse_args(["--from", str(source), "--library", str(library), "--concurrency", "3", "--max-calls", "200"])
    assert asyncio.run(init_default_bots.run(args, scratch_root=tmp_path / "scratch-root")) == 0
    out = capsys.readouterr().out
    assert "concurrency 3" in out and len(network) == 48 and network.peak == 3  # (twelve bots, three requests at a time)
    unlimited = fake_network(monkeypatch, delay=0.02)
    args = init_default_bots.parse_args(["--from", str(source), "--library", str(library), "--all", "--max-calls", "200"])
    assert asyncio.run(init_default_bots.run(args, scratch_root=tmp_path / "scratch-root")) == 0
    assert unlimited.peak == 12  # (no limit: all of them at once)
