"""The helpers the real-model tools share (``tools/devlib.py``): the scratch data directory, the call budget, the traces.

``tests/test_dev_tools.py`` has the price-source helper; this file has the rest.  No model is called: the budget is tried on fakes.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace as ns
from typing import Any

import pytest
from tools_fakes import SOURCE_KEY, make_source, tree

import devlib
from app.providers.models import ModelBinding
from app.storage import reset_stores_for_tests
from app.storage.settings import SettingsStore


# ================================================================================================================ budget
def test_the_meter_lets_the_budget_through_and_refuses_the_rest() -> None:
    meter = devlib.Meter(2)
    assert (meter.reserve(), meter.reserve()) == (1, 2)
    with pytest.raises(devlib.BudgetExhausted, match="budget of 2"):
        meter.reserve()
    assert (meter.calls, meter.refused) == (2, 1)
    meter.extend(3)  # a later phase has its own allowance
    assert [meter.reserve() for _ in range(3)] == [3, 4, 5] and meter.max_calls == 5
    meter.extend(-4)  # (it only ever grows)
    assert meter.max_calls == 5


class FakeClient:
    """What the engine's ``LLMClient`` offers the model port: ``chat_stream`` (real signature), ``reconfigure``, ``aclose``, an attribute."""

    reasoning_effort = "high"

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.closed = False

    async def chat_stream(self, messages, *, is_moderator=False, tools=None, on_chunk=None, max_tokens_override=None, binding=None, on_retry=None):
        self.seen.append({"messages": messages, "is_moderator": is_moderator, "tools": tools, "max": max_tokens_override, "binding": binding, "retry": on_retry})
        if messages and messages[0] == "explode":
            raise RuntimeError("the gateway is down")
        return ns(content="ok")

    def reconfigure(self, cfg: dict[str, Any]) -> None:
        self.seen.append({"reconfigured": cfg})

    async def aclose(self) -> None:
        self.closed = True


def test_the_client_behind_the_meter_counts_caps_and_forwards_everything_else() -> None:
    meter, client = devlib.Meter(2), FakeClient()
    metered = meter.client(client)
    answer = asyncio.run(metered.chat_stream(["hi"], is_moderator=True, tools=[{"x": 1}], max_tokens_override=77, binding="b", on_retry=print))
    assert answer.content == "ok" and client.seen[0] == {"messages": ["hi"], "is_moderator": True, "tools": [{"x": 1}], "max": 77, "binding": "b", "retry": print}
    with pytest.raises(RuntimeError, match="gateway"):
        asyncio.run(metered.chat_stream(["explode"]))
    assert (meter.calls, dict(meter.errors)) == (2, {"RuntimeError": 1})
    with pytest.raises(devlib.BudgetExhausted):
        asyncio.run(metered.chat_stream(["third"]))  # (never sent)
    assert len(client.seen) == 2 and meter.refused == 1
    metered.reconfigure({"a": 1})  # every other attribute is the real client's
    assert client.seen[-1] == {"reconfigured": {"a": 1}} and metered.reasoning_effort == "high"
    asyncio.run(metered.aclose())
    assert client.closed
    # the model port above it still finds out that retries can be reported (the signature is the real one's)
    from app.runtime.llm_gate import _accepts_keyword

    assert _accepts_keyword(metered.chat_stream, "on_retry")


def test_a_cancellation_is_not_counted_as_an_error() -> None:
    meter = devlib.Meter(5)

    class Hangs:
        async def chat_stream(self, messages, **kwargs):
            await asyncio.sleep(30)

    async def scenario() -> None:
        task = asyncio.ensure_future(meter.client(Hangs()).chat_stream(["x"]))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert meter.calls == 1 and not meter.errors


def test_the_port_behind_the_meter_is_counted_and_capped_like_the_client() -> None:
    class Port:
        def __init__(self) -> None:
            self.soft_gate = "gate"

        async def complete(self, agent_id, messages, tools, *, max_tokens=None):
            if agent_id == "bad":
                raise ValueError("no")
            return (agent_id, max_tokens)

    meter = devlib.Meter(2)
    port = meter.port(Port())
    assert asyncio.run(port.complete("N1", [], None, max_tokens=5)) == ("N1", 5) and port.soft_gate == "gate"
    with pytest.raises(ValueError):
        asyncio.run(port.complete("bad", [], None))
    with pytest.raises(devlib.BudgetExhausted):
        asyncio.run(port.complete("N2", [], None))
    assert (meter.calls, meter.refused, dict(meter.errors)) == (2, 1, {"ValueError": 1})


# ====================================================================================================== scratch directory
def test_the_environment_is_the_scratch_inside_the_block_and_as_it_was_after_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARENA_DATA_DIR", "the-previous-data-dir")
    monkeypatch.setenv("ARENA_MASTER_KEY", "a-master-key")
    monkeypatch.delenv("ARENA_MASTER_KEY_FILE", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with devlib.scratch_environment(scratch):
        assert os.environ["ARENA_DATA_DIR"] == str(scratch) and os.environ["PYTHONUTF8"] == "1"
        (scratch / "master.v1.dpapi").write_bytes(b"x")
        devlib.adopt_master_key(scratch)  # a wrapper next to the data IS the key: the environment's must not override it
        assert "ARENA_MASTER_KEY" not in os.environ
    assert os.environ["ARENA_DATA_DIR"] == "the-previous-data-dir" and os.environ["ARENA_MASTER_KEY"] == "a-master-key"
    assert "PYTHONUTF8" not in os.environ and "ARENA_MASTER_KEY_FILE" not in os.environ
    with pytest.raises(RuntimeError):  # (also when the block fails)
        with devlib.scratch_environment(scratch):
            raise RuntimeError("boom")
    assert os.environ["ARENA_DATA_DIR"] == "the-previous-data-dir"


def test_a_tool_refuses_to_go_on_when_the_data_directory_is_not_its_scratch(tmp_path: Path) -> None:
    mine, other = tmp_path / "mine", tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    with devlib.scratch_environment(mine):
        devlib.require_scratch(mine)  # (fine)
        with pytest.raises(SystemExit, match="refusing to go on: the data directory is .*mine, not the scratch directory .*other"):
            devlib.require_scratch(other)


def test_without_a_wrapper_the_master_key_of_the_environment_stays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARENA_MASTER_KEY", "a-master-key")
    with devlib.scratch_environment(tmp_path):
        devlib.adopt_master_key(tmp_path)
        assert os.environ["ARENA_MASTER_KEY"] == "a-master-key"


def test_only_the_registry_the_keys_and_the_wrapper_are_copied_and_the_source_is_left_alone(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    (source / "games" / "g1").mkdir(parents=True)
    (source / "games" / "g1" / "game.json").write_text("{}", encoding="utf-8")
    (source / "master.v1.dpapi").write_bytes(b"wrapped")
    before = tree(source)
    scratch = devlib.new_scratch(tmp_path / "root", "arena-x-")
    assert scratch.parent == (tmp_path / "root").resolve() and scratch.name.startswith("arena-x-")
    devlib.copy_provider_files(source, scratch)
    assert sorted(p.name for p in scratch.iterdir()) == ["master.v1.dpapi", "providers.json", "secrets.v1.enc"]
    assert (scratch / "master.v1.dpapi").read_bytes() == b"wrapped" and tree(source) == before


def test_a_source_without_a_registry_or_without_keys_is_refused_with_a_plain_message(tmp_path: Path) -> None:
    from app.providers.store import ProviderStore

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit, match="providers.json"):
        devlib.copy_provider_files(empty, scratch)
    keyless = tmp_path / "keyless"
    ProviderStore(keyless).create_provider("P", "openai_chat", "https://example.invalid", "")
    with pytest.raises(SystemExit, match="secrets.v1.enc"):
        devlib.copy_provider_files(keyless, scratch)
    assert not list(scratch.iterdir())


# ============================================================================================================== bindings
def test_the_bindings_of_a_v013_directory_are_read_by_seat(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    found = devlib.source_bindings(source, ["N1", "N2", "N3"])
    assert list(found) == ["moderator", "N1", "N2", "N3"]
    assert [found[a].model_id for a in found] == ["big", "small", "big", "big"]


def test_the_bindings_of_a_v014_directory_are_saved_by_bot_and_come_out_by_seat(tmp_path: Path) -> None:
    source = make_source(tmp_path, by_bot_id=True, seats=4)
    raw = json.loads((source / "settings.json").read_text(encoding="utf-8"))["default_bindings"]
    assert "N1" not in raw and sum(1 for key in raw if key.startswith("b-")) == 4  # (what v0.14 saves: by bot id)
    found = devlib.source_bindings(source, ["N1", "N2", "N3", "N4"])
    assert [found[a].model_id for a in found] == ["big", "small", "big", "big", "big"]  # ... and the seats get theirs by the line-up
    lineup = json.loads((source / "bots" / "lineup.json").read_text(encoding="utf-8"))["order"]
    assert raw[lineup[0]]["model_id"] == "small"


def test_a_seat_without_a_binding_gets_the_moderators_and_a_short_window_can_be_asked_for(tmp_path: Path) -> None:
    source = make_source(tmp_path, seats=2)  # (bindings for two seats only)
    found = devlib.source_bindings(source, ["N1", "N2", "N3", "N4"], window=16000)
    assert [found[a].model_id for a in found] == ["big", "small", "big", "big", "big"]  # N3 / N4: the moderator's
    assert {b.context_window_tokens for b in found.values()} == {16000}


def test_v012_seat_names_are_read_as_the_new_ones_and_no_binding_at_all_is_refused(tmp_path: Path) -> None:
    from app.providers.store import ProviderStore

    source = tmp_path / "old"
    provider = ProviderStore(source).create_provider("P", "openai_chat", "https://example.invalid", SOURCE_KEY)
    ProviderStore(source).add_model(provider.id, "m", 1.0, 2.0)
    both = ModelBinding(provider_id=provider.id, model_id="m")
    SettingsStore(source).save_default_bindings({"moderator": both, "p01": both})
    assert list(devlib.source_bindings(source, ["N1", "N2"])) == ["moderator", "N1", "N2"]
    nothing = tmp_path / "nothing"
    nothing.mkdir()
    with pytest.raises(SystemExit, match="no default model bindings"):
        devlib.source_bindings(nothing, ["N1"])


def test_the_bindings_of_the_seats_move_to_the_bots_that_sit_there_when_the_table_is_made(tmp_path: Path) -> None:
    from app.storage.bots import get_bot_store

    source = make_source(tmp_path)
    scratch = devlib.new_scratch(tmp_path / "root", "arena-x-")
    with devlib.scratch_environment(scratch):
        reset_stores_for_tests()
        devlib.copy_provider_files(source, scratch)
        devlib.write_bindings(scratch, devlib.source_bindings(source, ["N1", "N2", "N3"]))
        assert set(json.loads((scratch / "settings.json").read_text(encoding="utf-8"))["default_bindings"]) == {"moderator", "N1", "N2", "N3"}
        assert not (scratch / "last_session_id").exists()
        store = get_bot_store(3)
        settings = SettingsStore(scratch)
        lineup = store.lineup()
        assert [settings.get_bot_binding(bot).model_id for bot in lineup] == ["small", "big", "big"]  # (seat 1 had the small model)
        assert set(settings.get_default_bindings()) == {"moderator", "N1", "N2", "N3"}  # ... and the seat view is as before
        reset_stores_for_tests()


# ============================================================================================================= config.yml
def test_the_scenario_a_tool_asks_for_is_what_the_stores_install_and_afterwards_all_is_as_it_was(tmp_path: Path) -> None:
    import app.config as app_config
    import app.game.orchestrator as orchestrator_module
    from app.storage.rules import default_scenario_id

    real = app_config.load_config
    assert default_scenario_id() == "werewolf_12p"
    cfg = real()
    cfg["scenario"] = {"path": str(Path(app_config.BASE_DIR) / "scenarios" / "minimal_test.md")}
    with devlib.patched_config(cfg):
        assert default_scenario_id() == "minimal_test"  # the rules store reads the configuration itself
        assert orchestrator_module.load_config()["scenario"] == cfg["scenario"]
    assert app_config.load_config is real and orchestrator_module.load_config is real
    assert default_scenario_id() == "werewolf_12p"


# ================================================================================================================ traces
def test_the_rows_of_a_trace_are_read_in_order_and_what_is_not_json_is_skipped(tmp_path: Path) -> None:
    folder = tmp_path / "logs" / "game1"
    folder.mkdir(parents=True)
    (folder / "trace-0001.jsonl").write_text('{"event":"a","n":1}\nnot json\n{"event":"b","n":2}\n[1,2]\n', encoding="utf-8")
    (folder / "trace-0002.jsonl").write_text('{"event":"c","n":3}\n', encoding="utf-8")
    assert [row["n"] for row in devlib.trace_rows(tmp_path, "game1")] == [1, 2, 3]
    assert devlib.trace_rows(tmp_path, "no-such-game") == []
