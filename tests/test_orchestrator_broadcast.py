"""SSE broadcast behaviour: one serialization, none without subscribers, bounded queues."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.engine import orchestrator as orchestrator_module
from app.engine.events import (
    PUBLIC_SESSION_EXCLUDE,
    SUBSCRIBER_QUEUE_SIZE,
    SubscriberQueue,
    encode_event,
)
from app.engine.orchestrator import GameOrchestrator
from app.engine.state import GameSession, Participant


def make_orch() -> GameOrchestrator:
    orch = GameOrchestrator.__new__(GameOrchestrator)
    orch.session = GameSession(
        status="running",
        participants=[Participant(id="p01", seat="S1", name="名字")],
        moderator_messages=[{"role": "user", "content": "SECRET"}],
        prompt_cache={"x": "SECRET"},
    )
    orch._subscribers = set()
    return orch


def drain(queue: asyncio.Queue[Any]) -> list[Any]:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def test_payload_format_is_unchanged_and_excludes_hidden_fields() -> None:
    orch = make_orch()
    payload = encode_event("state_update", orch.session)
    decoded = json.loads(payload)
    assert set(decoded) == {"event", "session"}
    assert decoded["event"] == "state_update"
    expected = json.loads(
        json.dumps(orch.session.model_dump(exclude=set(PUBLIC_SESSION_EXCLUDE)), ensure_ascii=False)
    )
    assert decoded["session"] == expected
    assert "SECRET" not in payload
    assert "名字" in payload  # not \u-escaped


def test_no_subscribers_means_no_serialization(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_orch()

    def boom(*_: Any, **__: Any) -> None:
        raise AssertionError("serialized without subscribers")

    monkeypatch.setattr(orchestrator_module, "encode_event", boom)
    asyncio.run(orch.broadcast_state("state_update"))


def test_one_broadcast_serializes_once_for_all_subscribers(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_orch()
    calls = 0
    real = orchestrator_module.encode_event

    def counting(event: str, session: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(event, session)

    monkeypatch.setattr(orchestrator_module, "encode_event", counting)
    queues = [orch.subscribe() for _ in range(3)]
    asyncio.run(orch.broadcast_state("tool_executed"))
    assert calls == 1
    items = [drain(q)[0] for q in queues]
    assert items[0] is items[1] is items[2]


def test_full_queue_drops_old_snapshots_but_never_a_terminal_event() -> None:
    orch = make_orch()
    queue = orch.subscribe()

    async def flood() -> None:
        await orch.broadcast_state("game_ended")
        for _ in range(SUBSCRIBER_QUEUE_SIZE * 3):
            await orch.broadcast_state("stream_chunk")
        await orch.broadcast_state("error")
        for _ in range(SUBSCRIBER_QUEUE_SIZE * 3):
            await orch.broadcast_state("state_update")

    asyncio.run(flood())
    events = [json.loads(p)["event"] for p in drain(queue)]
    assert len(events) == SUBSCRIBER_QUEUE_SIZE
    assert events[0] == "game_ended" and events[1] == "error"
    assert events[-1] == "state_update"  # the newest snapshot always survives


def test_queue_of_only_important_events_still_accepts_new_ones() -> None:
    orch = make_orch()
    queue = orch.subscribe()

    async def flood() -> None:
        for _ in range(SUBSCRIBER_QUEUE_SIZE + 5):
            await orch.broadcast_state("status_change")

    asyncio.run(flood())
    assert queue.qsize() == SUBSCRIBER_QUEUE_SIZE


def test_plain_asyncio_queues_keep_working_and_broken_ones_are_dropped() -> None:
    orch = make_orch()
    plain: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)

    class Broken(asyncio.Queue):
        def full(self) -> bool:
            raise RuntimeError("broken subscriber")

    orch._subscribers = {plain, Broken()}

    async def go() -> None:
        await orch.broadcast_state("a")
        await orch.broadcast_state("b")

    asyncio.run(go())
    assert plain in orch._subscribers and len(orch._subscribers) == 1
    assert json.loads(plain.get_nowait())["event"] == "b"


def test_stream_chunk_broadcasts_stay_throttled() -> None:
    orch = make_orch()
    queue = orch.subscribe()
    assert isinstance(queue, SubscriberQueue)

    async def go() -> None:
        for _ in range(20):
            await orch._throttled_stream_broadcast()

    asyncio.run(go())
    assert queue.qsize() == 1


def test_v010_state_events_are_never_evicted_by_ordinary_snapshots() -> None:
    orch = make_orch()
    queue = orch.subscribe()

    async def flood() -> None:
        await orch.broadcast_state("bindings_updated")
        await orch.broadcast_state("autosave_error")
        for _ in range(SUBSCRIBER_QUEUE_SIZE * 3):
            await orch.broadcast_state("stream_chunk")

    asyncio.run(flood())
    events = [json.loads(p)["event"] for p in drain(queue)]
    assert events[:2] == ["bindings_updated", "autosave_error"]


def test_session_snapshot_exposes_v010_fields_without_secrets() -> None:
    orch = make_orch()
    decoded = json.loads(encode_event("state_update", orch.session))["session"]
    for key in ("schema_version", "model_bindings", "binding_issues", "restored_from_save",
                "restore_notice", "autosave_error"):
        assert key in decoded
    assert "by_model" in decoded["usage_stats"] and "by_actor" in decoded["usage_stats"]
    assert decoded["schema_version"] == 2
