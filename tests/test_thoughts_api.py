"""The thinking record of an agent (v0.13.1, G21-G26; contract docs/v0.12-contracts.md 12.4): ``GET /api/agents/{id}/thoughts`` over
HTTP on the real orchestrator.  What the agent thought (never its tool calls), newest page first, read from the game's trace;
closed for the others while a seat is played and the game is not over; for trusted clients only; never pushed anywhere."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.thoughts as thoughts_module
import app.game.observe_runtime as observe_module
import app.game.orchestrator as orchestrator_module
from app.game.orchestrator import GameOrchestrator
from app.main import app as real_app
from app.runtime.trace_log import TraceLog, trace_dir
from app.world.types import MODERATOR_ID
from game_fakes import CFG, async_test, make_table_orch
from v12.bots import BotLlm, Call, simple_player

IDS = ["N1", "N2", "N3", "N4"]
NAMES = ["Alice", "Bob", "Carol", "David"]
CFG4: dict[str, Any] = {
    **copy.deepcopy(CFG),
    "participants": [{"id": pid, "seat": str(i), "name": name} for i, (pid, name) in enumerate(zip(IDS, NAMES), start=1)],
}
ITEM_KEYS = {"id", "ts", "gt", "gen", "round", "kind", "text", "model"}
PAGE_KEYS = {"agent_id", "name", "items", "next_before", "has_more"}
CLOSED = "接管期间不能查看其他玩家的思考记录，对局结束后开放。"


# ------------------------------------------------------------------------------------------------ helpers
def make_orch(monkeypatch: pytest.MonkeyPatch) -> GameOrchestrator:
    return make_table_orch(monkeypatch)  # (v0.14: the bots of the table are initialized, so that a game can start)


@pytest.fixture(autouse=True)
def no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARENA_API_TOKEN", raising=False)


@contextlib.contextmanager
def make_client(orch: GameOrchestrator, monkeypatch: pytest.MonkeyPatch, *, remote: bool = False) -> Iterator[TestClient]:
    """A client of the real application (every router of ``app.main``).  It is not entered as a context - that would run
    the application's own start-up and build its own orchestrator - the endpoint under test talks to ``orch``."""
    monkeypatch.setattr(thoughts_module, "orchestrator", orch)
    yield TestClient(real_app, client=("203.0.113.5", 50000)) if remote else TestClient(real_app)


def open_trace(orch: GameOrchestrator) -> TraceLog:
    return TraceLog(orch._logs_root(), orch.session.session_id)


def write_thoughts(orch: GameOrchestrator, agent: str, count: int, *, first: int = 1) -> None:
    trace = open_trace(orch)
    for n in range(first, first + count):
        trace.write("wake", agent, gen=n)
        trace.write("llm_call", agent, gen=n, round=0, model="m1", tool_calls=["speak"], gt=float(n))
        trace.write("thought", agent, gen=n, round=0, text=f"{agent} thinks {n}", gt=float(n), model="m1")
        trace.write("tool_call", agent, gen=n, name="speak", args={"content": f"PUBLIC {n}"}, result="ok")
        trace.write("receipt", agent, gen=n, name="speak", ok=True)
    trace.close()


async def wait_for(predicate: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await asyncio.sleep(0.02)


def chatty(world: Any, me: str) -> list[Call]:
    if world.channel("public") is not None:
        return []
    return [
        ("create_channel", {"channel_id": "public", "name": "公共频道", "members": IDS, "type": "public"}),
        ("open_floor", {"channel_id": "public", "mode": "free", "limits": {"max_messages_per_member": 4}}),
    ]


def play(orch: GameOrchestrator) -> None:
    orch.ensure_bindings_ready = lambda: None  # type: ignore[method-assign]
    orch.llm_port_factory = lambda o: BotLlm(o.world, {MODERATOR_ID: chatty, **{pid: simple_player for pid in IDS}})


# ------------------------------------------------------------------------------------- the shape
def test_an_agent_with_thoughts_answers_with_the_contract_shape(monkeypatch):
    orch = make_orch(monkeypatch)
    write_thoughts(orch, "N1", 3)
    with make_client(orch, monkeypatch) as client:
        response = client.get("/api/agents/N1/thoughts")
    assert response.status_code == 200
    page = response.json()
    assert set(page) == PAGE_KEYS and page["agent_id"] == "N1" and page["name"] == "Alice"
    assert page["has_more"] is False and page["next_before"] is None
    assert [i["text"] for i in page["items"]] == ["N1 thinks 1", "N1 thinks 2", "N1 thinks 3"]  # oldest to newest
    first = page["items"][0]
    assert set(first) == ITEM_KEYS and first["kind"] == "thought" and first["model"] == "m1"
    assert (first["gt"], first["gen"], first["round"]) == (1.0, 1, 1) and first["ts"].endswith("Z")
    assert len({i["id"] for i in page["items"]}) == 3


def test_the_moderator_has_a_record_too_and_the_others_are_not_mixed_in(monkeypatch):
    orch = make_orch(monkeypatch)
    write_thoughts(orch, "moderator", 2)
    write_thoughts(orch, "N2", 4)
    with make_client(orch, monkeypatch) as client:
        moderator = client.get("/api/agents/moderator/thoughts").json()
        bob = client.get("/api/agents/N2/thoughts").json()
    assert (moderator["name"], [i["text"] for i in moderator["items"]]) == ("主持人", ["moderator thinks 1", "moderator thinks 2"])
    assert (bob["name"], len(bob["items"])) == ("Bob", 4) and all(i["text"].startswith("N2 ") for i in bob["items"])


def test_only_thoughts_and_the_monologue_of_a_call_without_tools_never_a_tool_call(monkeypatch):
    orch = make_orch(monkeypatch)
    trace = open_trace(orch)
    trace.write("wake", "N1", gen=1)
    trace.write("llm_call", "N1", gen=1, round=0, tool_calls=["speak"])
    trace.write("thought", "N1", gen=1, round=0, text="considering", gt=1.0)
    trace.write("content", "N1", gen=1, round=0, text="PUBLIC remark beside a tool call", gt=1.0, with_tools=True)
    trace.write("tool_call", "N1", gen=1, name="speak", args={"content": "PUBLIC statement"}, result="ok")
    trace.write("receipt", "N1", gen=1, name="speak", ok=True)
    trace.write("content", "N1", gen=2, round=0, text="talking to myself", gt=2.0, with_tools=False)
    trace.write("error", "N1", gen=3, message="RuntimeError: boom", traceback="...")
    trace.close()
    with make_client(orch, monkeypatch) as client:
        page = client.get("/api/agents/N1/thoughts").json()
    assert [(i["kind"], i["text"]) for i in page["items"]] == [("thought", "considering"), ("content", "talking to myself")]
    text = json.dumps(page, ensure_ascii=False)
    assert "PUBLIC" not in text and "speak" not in text and "boom" not in text and "receipt" not in text


def test_nothing_recorded_is_an_empty_page_not_an_error(monkeypatch):
    orch = make_orch(monkeypatch)  # no trace directory at all: a game that was never run, or imported without its logs
    with make_client(orch, monkeypatch) as client:
        for agent in ("N1", "moderator"):
            response = client.get(f"/api/agents/{agent}/thoughts")
            assert response.status_code == 200
            assert response.json()["items"] == [] and response.json()["has_more"] is False and response.json()["next_before"] is None
        write_thoughts(orch, "N3", 1)
        assert client.get("/api/agents/N1/thoughts").json()["items"] == []  # a trace without anything of this agent
        assert len(client.get("/api/agents/N3/thoughts").json()["items"]) == 1


# ------------------------------------------------------------------------------------------- paging
def test_pages_go_from_the_newest_back_through_the_cursor_and_cover_everything_once(monkeypatch):
    orch = make_orch(monkeypatch)
    write_thoughts(orch, "N1", 7)
    with make_client(orch, monkeypatch) as client:
        pages = []
        before = None
        while True:
            query = f"?limit=3" + (f"&before={before}" if before else "")
            page = client.get(f"/api/agents/N1/thoughts{query}").json()
            pages.append(page)
            if not page["has_more"]:
                break
            before = page["next_before"]
        assert [len(p["items"]) for p in pages] == [3, 3, 1]
        assert [i["text"] for i in pages[0]["items"]] == ["N1 thinks 5", "N1 thinks 6", "N1 thinks 7"]
        assert [i["text"] for p in reversed(pages) for i in p["items"]] == [f"N1 thinks {n}" for n in range(1, 8)]
        assert pages[-1]["next_before"] is None
        default = client.get("/api/agents/N1/thoughts").json()  # the default page holds 50
        assert len(default["items"]) == 7 and client.get("/api/agents/N1/thoughts?limit=200").status_code == 200


def test_new_entries_appear_on_top_of_what_was_read_so_a_poll_can_take_the_newest_page(monkeypatch):
    orch = make_orch(monkeypatch)
    write_thoughts(orch, "N1", 2)
    with make_client(orch, monkeypatch) as client:
        first = client.get("/api/agents/N1/thoughts").json()
        write_thoughts(orch, "N1", 2, first=3)  # the agent thought on while the window was open
        second = client.get("/api/agents/N1/thoughts").json()
    ids = [i["id"] for i in first["items"]]
    assert [i["id"] for i in second["items"]][: len(ids)] == ids  # the ids are stable: what was read keeps its key
    assert [i["text"] for i in second["items"]][-2:] == ["N1 thinks 3", "N1 thinks 4"]


# ---------------------------------------------------------------------------------------- bad input
def test_an_id_that_is_no_agent_of_the_game_is_a_404_and_bad_parameters_are_refused(monkeypatch):
    orch = make_orch(monkeypatch)
    write_thoughts(orch, "N1", 1)
    with make_client(orch, monkeypatch) as client:
        for agent in ("N9", "engine", "roster", "n1", "ghost"):
            gone = client.get(f"/api/agents/{agent}/thoughts")
            assert gone.status_code == 404 and gone.json()["detail"] == "没有这个玩家。", agent
        for query in ("limit=0", "limit=201", "limit=-5", "limit=many", f"before={'1' * 65}"):
            assert client.get(f"/api/agents/N1/thoughts?{query}").status_code == 422, query
        for cursor in ("nonsense", "1", "1:2:3", "-1:5", "a:b"):
            refused = client.get(f"/api/agents/N1/thoughts?before={cursor}")
            assert refused.status_code == 400 and "游标无效" in refused.json()["detail"], cursor
        assert client.get("/api/agents/N1/thoughts?before=1:0").json()["items"] == []  # (in front of the first line: nothing)


# --------------------------------------------------------------------------------- not while playing
@async_test
async def test_while_a_seat_is_played_the_others_are_closed_until_the_game_is_over(monkeypatch):
    orch = make_orch(monkeypatch)
    for agent in ("N1", "N2", "N3", "moderator"):
        write_thoughts(orch, agent, 2)

    with make_client(orch, monkeypatch) as client:
        def status(agent: str) -> int:
            return client.get(f"/api/agents/{agent}/thoughts").status_code

        assert [status(a) for a in ("N1", "N2", "moderator")] == [200, 200, 200]  # nobody plays: look at everybody

        await orch.takeover("N2")
        assert status("N2") == 200  # what the seat's own agent thought before the person took over
        for agent in ("N1", "N3", "moderator"):
            closed = client.get(f"/api/agents/{agent}/thoughts")
            assert closed.status_code == 409 and closed.json()["detail"] == CLOSED, agent
        assert status("N9") == 404  # (an id that does not exist is still not found)

        orch.session.status = "paused"  # the status the game has while a person plays and has paused
        assert status("N1") == 409 and status("N2") == 200
        orch.session.status = "running"
        assert status("moderator") == 409
        orch.session.status = "finished"  # the game is over: the post-mortem may look at everybody
        assert [status(a) for a in ("N1", "N2", "N3", "moderator")] == [200, 200, 200, 200]

        orch.session.status = "paused"
        await orch.release_takeover()
        assert [status(a) for a in ("N1", "N2", "moderator")] == [200, 200, 200]  # nobody plays any more


@async_test
async def test_the_closed_door_is_the_servers_not_just_the_buttons_the_rule_is_checked_before_any_file_is_read(monkeypatch):
    orch = make_orch(monkeypatch)
    write_thoughts(orch, "N1", 1)
    await orch.takeover("N2")
    calls: list[str] = []
    real = observe_module.read_thoughts
    monkeypatch.setattr(observe_module, "read_thoughts", lambda *a, **k: calls.append("read") or real(*a, **k))
    with pytest.raises(observe_module.ObserveError) as refused:
        await orch.agent_thoughts("N1")
    assert refused.value.status == 409 and calls == []  # nothing of N1 was even opened
    assert (await orch.agent_thoughts("N2"))["items"] == [] and calls == ["read"]


# ---------------------------------------------------------------------------------- trusted clients
def test_only_a_trusted_client_may_read_thoughts(monkeypatch):
    orch = make_orch(monkeypatch)
    write_thoughts(orch, "N1", 1)
    with make_client(orch, monkeypatch, remote=True) as remote:
        denied = remote.get("/api/agents/N1/thoughts")
        assert denied.status_code == 403
    monkeypatch.setenv("ARENA_API_TOKEN", "test-token")
    with make_client(orch, monkeypatch, remote=True) as remote:
        assert remote.get("/api/agents/N1/thoughts").status_code == 401
        assert remote.get("/api/agents/N1/thoughts", headers={"X-Arena-Token": "nope"}).status_code == 401
        assert remote.get("/api/agents/N1/thoughts", headers={"X-Arena-Token": "test-token"}).status_code == 200
    with make_client(orch, monkeypatch) as local:
        assert local.get("/api/agents/N1/thoughts").status_code == 401  # with a token, everybody needs it


# ------------------------------------------------------------------------- a game that was played
@async_test
async def test_a_played_game_leaves_a_record_that_is_never_pushed_to_the_spectators(monkeypatch):
    orch = make_orch(monkeypatch)
    play(orch)
    await orch.start_or_resume()
    await wait_for(lambda: orch.usage.by_actor.get("N1") is not None and orch.usage.by_actor["N1"].calls >= 2)
    await orch.pause()

    page = await orch.agent_thoughts("N1")
    assert page["items"], "the scripted model thinks on every call"
    assert {i["kind"] for i in page["items"]} == {"thought"} and {i["text"] for i in page["items"]} == {"（脚本）"}
    first = page["items"][0]
    assert first["model"] == "scripted" and isinstance(first["gt"], float) and first["gen"] >= 1 and first["round"] >= 1
    assert (await orch.agent_thoughts(MODERATOR_ID))["items"]  # the moderator thought, too
    everything = json.dumps(page, ensure_ascii=False)
    assert "speak" not in everything and "respond" not in everything and "sleep" not in everything  # no tool call

    # D5 still stands for everything that is pushed: no event, no snapshot, no PublicSession carries a thought
    pushed = " ".join(e.json() for e in orch.bus._ring) + json.dumps(orch.public_session(), ensure_ascii=False, default=str)
    assert "（脚本）" not in pushed and "thoughts" not in json.dumps(orch.public_session(), default=str)


@async_test
async def test_a_seat_that_was_played_keeps_the_record_of_its_agent_and_a_loaded_game_has_its_own(monkeypatch):
    orch = make_orch(monkeypatch)
    play(orch)
    await orch.start_or_resume()
    await wait_for(lambda: orch.usage.by_actor.get("N2") is not None and orch.usage.by_actor["N2"].calls >= 1)
    await orch.pause()
    first_id = orch.session.session_id
    before = await orch.agent_thoughts("N2")
    assert before["items"]
    await orch.takeover("N2")
    assert (await orch.agent_thoughts("N2"))["items"] == before["items"]  # the agent's record from before it was played
    await orch.release_takeover()
    await orch.persist_session("test")

    await orch.new_game()
    assert (await orch.agent_thoughts("N2"))["items"] == []  # a new game: nothing yet (and the other game's trace is not read)
    await orch.load_game(first_id)
    assert (await orch.agent_thoughts("N2"))["items"] == before["items"]


@async_test
async def test_the_file_is_read_on_a_worker_thread_so_a_big_trace_does_not_stall_the_game(monkeypatch):
    orch = make_orch(monkeypatch)
    write_thoughts(orch, "N1", 1)
    seen: list[int] = []
    real = observe_module.read_thoughts

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(observe_module, "read_thoughts", spy)
    await orch.agent_thoughts("N1")
    assert seen and seen[0] != threading.get_ident()


def test_the_trace_directory_of_the_game_is_the_one_that_is_read(monkeypatch):
    orch = make_orch(monkeypatch)
    write_thoughts(orch, "N1", 1)
    other = TraceLog(orch._logs_root(), "some-other-game")  # another game's log is somebody else's business
    other.write("thought", "N1", text="from another game")
    other.close()
    assert trace_dir(orch._logs_root(), orch.session.session_id).is_dir()
    with make_client(orch, monkeypatch) as client:
        assert [i["text"] for i in client.get("/api/agents/N1/thoughts").json()["items"]] == ["N1 thinks 1"]
