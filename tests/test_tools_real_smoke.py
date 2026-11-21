"""``tools/real_smoke.py`` (v0.14): the real-model smoke test of the engine, run here with the demo's scripted bots instead of real models.

Nothing here calls a model: the tool takes a ``port_factory`` for the agents and the learning runs, and the call budget counts and caps the calls of
that fake port exactly as it does the real client's.  What is checked is the tool - the scratch data directory (the source is only read, the scratch
is gone afterwards), the table of bots and the bindings it starts from, the initialization (``fixed``: no calls; ``real``: the learning runs), the game
with its monitor and report, the budget, the one-shots (``core:refine`` and the review), and that ``--help`` says what the documentation says.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

import pytest
from tools_fakes import SOURCE_KEY, demo_factory, make_source, tree

import real_smoke

ROOT = Path(__file__).resolve().parents[1]
WEREWOLF = ["--scenario", "scenarios/werewolf_12p.md", "--players", "12"]


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(real_smoke, "POLL_S", 0.05)  # (the monitor looks at the game every second: the tests' games take seconds)


def smoke(tmp_path: Path, *argv: str, port_factory=None, source: Path | None = None) -> tuple[int, Path]:
    """Run the tool in-process; ``(exit code, scratch root)``."""
    source = source or make_source(tmp_path)
    root = tmp_path / "scratch-root"
    args = real_smoke.parse_args(["--from", str(source), *argv])
    code = asyncio.run(real_smoke.run(args, port_factory=port_factory or demo_factory(), scratch_root=root))
    return code, root


def kept_scratch(out: str) -> Path:
    match = re.search(r"^scratch kept at (.+)$", out, re.MULTILINE)
    assert match, out
    return Path(match.group(1).strip())


# ======================================================================================================== the whole run
def test_a_whole_game_with_the_fixed_initialization_is_played_and_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = make_source(tmp_path)
    before = tree(source)
    code, root = smoke(tmp_path, *WEREWOLF, "--max-calls", "100000", "--timeout", "120", source=source)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "scenario=werewolf_12p players=12" in out and "init=fixed" in out
    assert "initialization: fixed (no model call): 12 of 12 bots committed" in out
    assert "=== initialization (real) ===" not in out  # (no learning run: nothing to report)
    assert "=== report ===" in out and "status: finished" in out and "tool usage:" in out
    assert re.search(r"wall time \d+s \| LLM calls \d+ \(refused over the budget: 0\) \| call errors \{\}", out)
    assert re.search(r"tokens: uncached input \d+ \| cache read \d+ \| cache write \d+ \| output \d+ \| total \d+ \| cache hit [\d.]+% \| cost", out)
    assert re.search(r"N1 +act= *\d+ calls", out) and "moderator  act=" in out  # (an activation table per agent)
    assert re.search(r"mounted at the start of a conversation \(average per agent, chars\): L0 \d+, L1 \d+, L2 \d+", out)  # (what the bots' memory put in)
    assert re.search(r"prompt cache: \d+/\d+ calls after an agent's first one", out)
    assert "rejections by the engine (receipts): TOO_LONG x0, FORMAT x0" in out
    assert re.search(r"floor timeline \(game seconds; the first 60 of \d+ entries\)", out) and "last public messages:" in out
    assert tree(source) == before  # the live data directory was only read
    assert not list(root.iterdir())  # the scratch directory is gone


def test_the_table_the_bindings_and_the_l0_the_game_starts_from(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = make_source(tmp_path)
    (source / "games" / "g1").mkdir(parents=True)
    (source / "games" / "g1" / "game.json").write_text("{}", encoding="utf-8")  # games are not copied
    (source / "roster").mkdir()
    (source / "roster" / "roster.json").write_text("{}", encoding="utf-8")
    code, _root = smoke(tmp_path, *WEREWOLF, "--max-calls", "12", "--timeout", "60", "--keep", source=source)
    out = capsys.readouterr().out
    scratch = kept_scratch(out)
    try:
        assert code == 0, out
        assert not (scratch / "games" / "g1").exists() and not (scratch / "roster").exists()  # (nothing but the providers came over)
        lineup = json.loads((scratch / "bots" / "lineup.json").read_text(encoding="utf-8"))["order"]
        names = [json.loads((scratch / "bots" / b / "bot.json").read_text(encoding="utf-8"))["name"] for b in lineup]
        assert names == ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]  # (the default library)
        bindings = json.loads((scratch / "settings.json").read_text(encoding="utf-8"))["default_bindings"]
        assert [bindings[b]["model_id"] for b in lineup] == ["small"] + ["big"] * 11 and bindings["moderator"]["model_id"] == "big"  # (by bot, as v0.14 saves)
        library = ROOT / "bots" / "Alice" / "core"
        assert (scratch / "bots" / lineup[0] / "core" / "identity.md").read_bytes() == (library / "identity.md").read_bytes()  # (the L0 of the library)
        init = json.loads((scratch / "bots" / lineup[0] / "bot.json").read_text(encoding="utf-8"))["init"]
        assert init["status"] == "ready" and init["model"] == "演示模式" and init["scenario_id"] == "werewolf_12p"  # (the fixed initialization)
        assert "MODELS" not in out and "models: " in out
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_a_v014_data_directory_is_read_the_same_way(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = make_source(tmp_path, by_bot_id=True, seats=4)  # (bindings saved by bot id, with the line-up of the directory)
    before = tree(source)
    code, root = smoke(tmp_path, "--max-calls", "100000", "--timeout", "120", source=source, port_factory=_four_seat_game())
    out = capsys.readouterr().out
    assert code == 0, out
    assert "scenario=minimal_test players=4" in out and "initialization: fixed (no model call): 4 of 4 bots committed" in out
    assert tree(source) == before and not list(root.iterdir())


def _four_seat_game():
    """The scripted agents of the 4-player test scenario (the demo plays the 12-player one): the moderator ends the game at once."""
    from test_call_log_api import end_at_once
    from v12.bots import BotLlm, simple_player

    return lambda orch: BotLlm(orch.world, {"moderator": end_at_once, **{seat: simple_player for seat in ("N1", "N2", "N3", "N4")}})


def test_the_sample_characters_are_written_into_the_l0_of_the_bots(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _root = smoke(tmp_path, *WEREWOLF, "--core-from-sample", "--max-calls", "5", "--timeout", "60", "--keep")
    out = capsys.readouterr().out
    scratch = kept_scratch(out)
    try:
        assert code == 0 and "L0 written from the sample characters: Alice, Bob, Carol" in out
        lineup = json.loads((scratch / "bots" / "lineup.json").read_text(encoding="utf-8"))["order"]
        for bot_id, (name, persona) in zip(lineup, real_smoke.SEATS):
            core = scratch / "bots" / bot_id / "core"
            assert (core / "identity.md").read_text(encoding="utf-8").strip() == f"你是{name}。{persona}"
            assert persona in (core / "voice.md").read_text(encoding="utf-8") and persona in (core / "mind.md").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_the_sample_l0_is_valid_for_every_seat() -> None:
    from app.bots.types import CORE_LIMITS
    from app.storage import botfiles

    for name, persona in real_smoke.SEATS:
        core = real_smoke.sample_core(name, persona)
        assert core.complete and all(len(getattr(core, key)) <= CORE_LIMITS[key] for key in CORE_LIMITS)
        assert botfiles.parse_core_markdown(botfiles.render_core_markdown(core, name)).core == core  # (it survives the merged file)


# ========================================================================================================== the real init
def test_the_real_initialization_is_reported_bot_by_bot_and_so_are_the_one_shots(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, root = smoke(tmp_path, *WEREWOLF, "--init", "real", "--oneshots", "2", "--max-calls", "100000", "--timeout", "120")
    out = capsys.readouterr().out
    assert code == 0, out
    assert re.search(r"initialization: real - job \w+: 12 bots at the same time, at most 120s each", out) and "=== initialization (real) ===" in out
    rows = re.findall(r"^  (\w+) +ready +\d+s +calls=(\d+)  in=(\d+) cached=(\d+) out=(\d+)  wrote: (.+)$", out, re.MULTILINE)
    assert [r[0] for r in rows] == ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]
    assert all(int(r[1]) >= 3 and "arch_notes" in r[5] and "digest" in r[5] and "playbook" in r[5] and "字" in r[5] for r in rows)  # (calls, files and their size)
    assert re.search(r"total: 48 calls, \d+ tokens, \$[\d.]+ \(bots/usage.json", out)
    assert "=== report ===" in out and "status: finished" in out  # the game was played after it
    # one-shots: the moderator's "complete the core" (printed, not saved) and the real review of the first two bots
    assert "=== one-shot calls (core:refine / review) ===" in out
    assert re.search(r"refine core of Alice: \d+ chars, sections \{'identity': \d+, 'voice': \d+, 'mind': \d+\}, extras 0, missing none", out)
    assert "refine core of Bob:" in out and "review started for ['N1', 'N2']; done: ['N1', 'N2']" in out
    assert "--- N1 Alice: done - 复盘完成。" in out and "--- N2 Bob: done" in out
    assert re.search(r"    people: 0 -> \d+ chars", out) and re.search(r"    playbook: \d+ -> \d+ chars", out)  # (what each wrote: a summary, not the old text)
    assert not list(root.iterdir())


def test_a_real_initialization_that_fails_stops_the_run_with_the_reasons_and_no_game(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class Broken:
        async def complete(self, agent_id, messages, tools, *, max_tokens=None):
            raise RuntimeError(f"401 Unauthorized: bad key {SOURCE_KEY} / sk-test-abcdefgh12345678")

    code, root = smoke(tmp_path, *WEREWOLF, "--init", "real", "--max-calls", "100", port_factory=lambda orch: Broken())
    out = capsys.readouterr().out
    assert code == 3
    assert len(re.findall(r"^  \w+ +failed ", out, re.MULTILINE)) == 12
    assert out.count("why: 调用模型失败") == 12 and "401 Unauthorized" in out
    assert "sk-test-abcdefgh12345678" not in out and SOURCE_KEY not in out  # (what a provider's error echoes is scrubbed)
    assert "BOTS THAT ARE NOT INITIALIZED (the game cannot start)" in out and "=== report ===" not in out  # (no game was started)
    assert not list(root.iterdir())


def test_the_real_initialization_is_capped_by_the_budget(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _root = smoke(tmp_path, *WEREWOLF, "--init", "real", "--max-calls", "20")
    out = capsys.readouterr().out
    assert code == 3
    assert "BudgetExhausted" in out and "the call budget of 20 model calls is spent" in out  # (the bots that were refused say why)
    assert re.search(r"total: 20 calls", out)  # (never more than the budget was sent)
    assert "=== report ===" not in out


# ============================================================================================================== the game
def test_the_budget_pauses_the_game_and_the_resume_reports_whether_the_prompt_cache_survived(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _root = smoke(tmp_path, *WEREWOLF, "--max-calls", "40", "--resume-calls", "30", "--timeout", "120")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "call budget reached (40); pausing" in out
    assert re.search(r"=== resume: the game is paused at call \d+; resuming the same game for 30 more calls ===", out)
    assert re.search(r"after the resume: \d+ calls, \d+ reported cached input \(\d+%\); cache read \d+ vs uncached \d+ tokens", out)
    kept = re.search(r"conversations kept across the pause: (\d+)/(\d+) still start with what they had; system prompts byte-identical: (\d+)/(\d+)", out)
    assert kept and kept.group(1) == kept.group(2) == kept.group(3) == kept.group(4) == "13"  # (12 players and the moderator: nothing was rebuilt)
    assert "first call of each agent after the resume reported no cache hit for:" in out


def test_a_scenario_that_does_not_fit_the_number_of_seats_is_said_in_plain_words(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, root = smoke(tmp_path, "--scenario", "scenarios/werewolf_12p.md", "--players", "4")  # (the identity pool of the 12-player scenario against 4 seats)
    out = capsys.readouterr().out
    assert code == 2
    assert "THE TABLE DOES NOT FIT THE SCENARIO:" in out and "身份池共有 12 个身份，但对局有 4 名玩家" in out and "--players 4 against --scenario" in out
    assert not list(root.iterdir())


def test_a_start_that_the_check_refuses_prints_the_problems_and_exits_with_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.game.sources import SourceError, Sources

    monkeypatch.setattr(Sources, "contract_problem", lambda self, spec: SourceError("官方契约有问题：测试。", kind="contract", code="contract_invalid"))
    code, _root = smoke(tmp_path, *WEREWOLF)
    out = capsys.readouterr().out
    assert code == 3
    assert "THE GAME DID NOT START:" in out and "  - contract ['官方契约有问题：测试。']" in out and "=== report ===" not in out


def test_bots_that_stay_uninitialized_are_named_before_a_start_is_even_tried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.game.demo as demo

    monkeypatch.setattr(demo, "prepare_demo", lambda orch: [])  # (the fixed initialization does nothing)
    code, _root = smoke(tmp_path, *WEREWOLF)
    out = capsys.readouterr().out
    assert code == 3 and "BOTS THAT ARE NOT INITIALIZED (the game cannot start): {'Alice': 'uninitialized'" in out


def test_a_model_binding_that_does_not_exist_is_reported_and_nothing_is_played(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from app.providers.models import ModelBinding
    from app.storage.settings import SettingsStore

    source = make_source(tmp_path)
    ghost = ModelBinding(provider_id="0" * 32, model_id="ghost")
    SettingsStore(source).save_default_bindings({"moderator": ghost, **{f"N{i}": ghost for i in range(1, 5)}})
    code, root = smoke(tmp_path, source=source)
    out = capsys.readouterr().out
    assert code == 2 and "BINDING ISSUES:" in out and "在本机不存在" in out
    assert "=== report ===" not in out and not list(root.iterdir())


def test_a_source_without_keys_is_refused_before_anything_is_started(tmp_path: Path) -> None:
    from app.providers.store import ProviderStore

    keyless = tmp_path / "keyless"
    ProviderStore(keyless).create_provider("P", "openai_chat", "https://example.invalid", "")
    args = real_smoke.parse_args(["--from", str(keyless)])
    with pytest.raises(SystemExit, match="secrets.v1.enc"):
        asyncio.run(real_smoke.run(args, port_factory=demo_factory(), scratch_root=tmp_path / "root"))
    assert not list((tmp_path / "root").iterdir())  # (the scratch directory that was made for it is gone again)


# ============================================================================================================ the arguments
def test_the_arguments_are_checked() -> None:
    for bad in (["--players", "0"], ["--players", "13"], ["--max-calls", "0"], ["--oneshots", "5"], ["--init", "maybe"], ["--oneshots", "-1"]):
        with pytest.raises(SystemExit) as exit_info:
            real_smoke.parse_args(["--from", "x", *bad])
        assert exit_info.value.code == 2, bad
    args = real_smoke.parse_args(["--from", "x"])
    assert (args.players, args.init, args.max_calls, args.oneshots, args.core_from_sample, args.keep) == (4, "fixed", 60, 0, False, False)
    assert real_smoke.parse_args(["--from", "x", "--players", "12", "--oneshots", "12"]).oneshots == 12


def test_the_scenario_is_found_whatever_the_working_directory_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert Path(real_smoke.scenario_path("scenarios/minimal_test.md")) == ROOT / "scenarios" / "minimal_test.md"  # (relative to the program directory)
    mine = tmp_path / "mine.md"
    mine.write_text("x", encoding="utf-8")
    assert Path(real_smoke.scenario_path("mine.md")) == mine  # (relative to the working directory, when it is there)
    assert Path(real_smoke.scenario_path(str(mine))) == mine


# ========================================================================================== the scratch of the old layout
def test_prepare_scratch_still_makes_the_copy_for_the_seats_it_is_given(tmp_path: Path) -> None:
    """``prepare_scratch`` (the two steps of the run in one): a directory below the root with the registry, the keys and the bindings of the seats."""
    from app.providers.store import ProviderStore

    source = make_source(tmp_path)
    before = tree(source)
    scratch = real_smoke.prepare_scratch(source, ["N1", "N2"], 8000, scratch_root=tmp_path / "root")
    assert scratch.parent == (tmp_path / "root").resolve() and {p.name for p in scratch.iterdir()} == {"providers.json", "secrets.v1.enc", "settings.json"}
    saved = json.loads((scratch / "settings.json").read_text(encoding="utf-8"))["default_bindings"]
    assert set(saved) == {"moderator", "N1", "N2"} and {b["context_window_tokens"] for b in saved.values()} == {8000}
    assert ProviderStore(scratch).list_providers()[0].api_key == SOURCE_KEY  # (the copy opens with the same master key)
    assert tree(source) == before


# ================================================================================================================== help
def test_the_help_text_names_every_option(capsys: pytest.CaptureFixture[str]) -> None:
    text = real_smoke.build_parser().format_help()
    for option in ("--from", "--scenario", "--players", "--init", "--core-from-sample", "--max-calls", "--timeout", "--concurrency", "--price-source",
                   "--window", "--resume-calls", "--oneshots", "--keep"):
        assert option in text
    assert "docs/real-model-runs.md" in text and "ONE hard budget" in text and "Exit code:" in text
    assert "--persona" not in text  # (the v0.13 option is gone: it wrote the roster files that v0.14 has no more)


def test_the_production_path_with_the_real_client_and_only_the_network_faked(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``port_factory``: the real ``LLMClient`` behind the real model port, the budget around the client, the learning runs with the bots' own bindings."""
    from tools_fakes import fake_network

    seen = fake_network(monkeypatch, ends_game_with="红队")
    source = make_source(tmp_path)
    args = real_smoke.parse_args(["--from", str(source), "--init", "real", "--oneshots", "1", "--max-calls", "100", "--timeout", "60"])
    code = asyncio.run(real_smoke.run(args, scratch_root=tmp_path / "scratch-root"))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "initialization: real - job" in out and "total: 16 calls" in out  # (4 bots, 4 calls each)
    assert "game left 'running': status=finished" in out and "=== report ===" in out and "winner: 红队" in out
    assert re.search(r"LLM calls \d+ \(refused over the budget: 0\) \| call errors \{\}", out)
    learning = [s for s in seen if "write_memory" in s["tools"]]
    assert len(learning) >= 16 + 1 and {s["model"] for s in learning} <= {"big", "small"}  # (every bot asks the model bound to it: seat 1 has "small")
    assert {s["model"] for s in learning if not s["moderator"]} == {"big", "small"}
    assert re.search(r"refine core of Alice: \d+ chars", out) and "review started for ['N1']" in out
    assert re.search(r"one-shot calls so far: \d+ \(budget 110\)", out)  # (100 + the 2 + 8 of --oneshots 1)


def test_the_window_and_the_concurrency_limit_reach_the_run(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools_fakes import fake_network

    network = fake_network(monkeypatch, ends_game_with="红队", delay=0.02)
    source = make_source(tmp_path)
    args = real_smoke.parse_args(["--from", str(source), "--init", "real", "--window", "16000", "--concurrency", "1", "--max-calls", "100", "--timeout", "60", "--keep"])
    code = asyncio.run(real_smoke.run(args, scratch_root=tmp_path / "scratch-root"))
    out = capsys.readouterr().out
    scratch = kept_scratch(out)
    try:
        assert code == 0, out
        assert "concurrency=1" in out and network.peak == 1  # (the model calls of the four bots and of the game went one at a time)
        bindings = json.loads((scratch / "settings.json").read_text(encoding="utf-8"))["default_bindings"]
        assert {b["context_window_tokens"] for b in bindings.values()} == {16000}  # (a short window on purpose)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_the_price_source_is_applied_to_the_bound_models_in_the_scratch_copy(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import devlib

    asked: list[list] = []

    async def fake_prices(bindings):
        asked.append(sorted({(b.model_id, b.reasoning_depth) for b in bindings}))
        return ["price table: 1 models from https://prices.example", "  big: in $0.3/M, cache read $0.006/M, cache write $0.0/M, out $1.2/M  (litellm:big)"]

    monkeypatch.setattr(devlib, "apply_price_source", fake_prices)
    code, _root = smoke(tmp_path, *WEREWOLF, "--price-source", "--max-calls", "8", "--timeout", "30")  # (the demo plays the 12-player scenario)
    out = capsys.readouterr().out
    assert code == 0 and asked == [[("big", "high"), ("small", "high")]]  # (the models the table is bound to: once each)
    assert "price table: 1 models from https://prices.example" in out and "big: in $0.3/M" in out


def test_a_paused_game_is_reviewed_with_an_unknown_result(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--oneshots`` on a game that did not end (the budget paused it): the reviews run all the same and the result is "not decided"."""
    code, _root = smoke(tmp_path, *WEREWOLF, "--max-calls", "20", "--oneshots", "1", "--timeout", "60")
    out = capsys.readouterr().out
    assert code == 0 and "call budget reached (20); pausing" in out
    assert "review started for ['N1']; done: ['N1']" in out and "--- N1 Alice: done" in out
