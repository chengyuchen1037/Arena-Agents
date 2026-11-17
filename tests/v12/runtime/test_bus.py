from __future__ import annotations

import asyncio
import json

from app.runtime.bus import ORDINARY_TYPES, EventBus
from app.runtime.ports import EventSink
from app.world.types import UiEvent

from .fakes import async_test


def ev(type_: str = "message.committed", **data) -> UiEvent:
    return UiEvent(type=type_, data=data)


def ids(events) -> list[int]:
    return [e.id for e in events]


def test_publish_assigns_increasing_ids_and_satisfies_the_sink_protocol():
    bus = EventBus()
    sink: EventSink = bus
    assert bus.last_id == 0
    sink.publish(ev(n=1))
    assert bus.publish(ev(n=2)) == 2
    assert bus.last_id == 2


def test_first_connection_gets_no_replay_and_receives_what_follows():
    bus = EventBus()
    bus.publish(ev(n=1))
    replay, queue = bus.subscribe(None)
    assert replay is None  # the endpoint must send a snapshot
    bus.publish(ev(n=2))
    assert queue.get_nowait().data == {"n": 2}
    assert bus.subscriber_count == 1
    bus.unsubscribe(queue)
    bus.unsubscribe(queue)  # idempotent
    bus.publish(ev(n=3))
    assert queue.empty() and bus.subscriber_count == 0


def test_resume_replays_everything_after_last_event_id():
    bus = EventBus(ring=10)
    for n in range(1, 6):
        bus.publish(ev(n=n))
    replay, queue = bus.subscribe(2)
    assert ids(replay) == [3, 4, 5]
    assert [e.data["n"] for e in replay] == [3, 4, 5]
    bus.publish(ev(n=6))
    assert queue.get_nowait().id == 6  # no event is delivered twice, none is lost
    assert queue.empty()


def test_resume_when_nothing_was_missed_returns_an_empty_replay():
    bus = EventBus()
    bus.publish(ev())
    bus.publish(ev())
    replay, _ = bus.subscribe(2)
    assert replay == []
    replay, _ = bus.subscribe("2")  # Last-Event-ID arrives as a header string
    assert replay == []


def test_gap_larger_than_the_ring_needs_a_snapshot():
    bus = EventBus(ring=3)
    for n in range(1, 11):
        bus.publish(ev(n=n))
    replay, _ = bus.subscribe(5)  # ring holds 8, 9, 10: 6 and 7 are gone
    assert replay is None
    replay, _ = bus.subscribe(7)  # 8, 9, 10 are all still there
    assert ids(replay) == [8, 9, 10]
    replay, _ = bus.subscribe(6)  # exactly one missing event: 7
    assert replay is None


def test_unknown_or_malformed_ids_need_a_snapshot():
    bus = EventBus()
    for _ in range(3):
        bus.publish(ev())
    assert bus.subscribe(99)[0] is None  # from a previous server run
    assert bus.subscribe(-4)[0] is None
    assert bus.subscribe("garbage")[0] is None
    assert bus.subscribe("")[0] is None


def test_resume_on_an_empty_bus():
    bus = EventBus()
    assert bus.subscribe(0)[0] == []
    assert bus.subscribe(5)[0] is None


def test_full_queue_drops_the_oldest_ordinary_event_first():
    bus = EventBus(queue_size=4)
    _, queue = bus.subscribe()
    bus.publish(ev("message.committed", n=1))
    bus.publish(ev("agent.status", id="a1", state="thinking"))
    bus.publish(ev("message.committed", n=2))
    bus.publish(ev("agent.status", id="a2", state="thinking"))
    assert queue.full()
    bus.publish(ev("message.committed", n=3))  # sheds the oldest ordinary event (a1's status)
    bus.publish(ev("message.committed", n=4))  # sheds the next one (a2's status)
    got = []
    while not queue.empty():
        got.append(queue.get_nowait())
    assert [e.type for e in got] == ["message.committed"] * 4
    assert [e.data["n"] for e in got] == [1, 2, 3, 4]
    assert queue.dropped == 2 and queue.lost_important is False


def test_when_only_important_events_are_queued_the_oldest_goes_and_the_queue_is_flagged():
    bus = EventBus(queue_size=3)
    _, queue = bus.subscribe()
    for n in range(1, 6):
        bus.publish(ev("message.committed", n=n))
    assert [queue.get_nowait().data["n"] for _ in range(3)] == [3, 4, 5]
    assert queue.dropped == 2 and queue.lost_important is True  # the endpoint should resync this client


def test_a_slow_subscriber_does_not_affect_a_fast_one():
    bus = EventBus(queue_size=2)
    _, slow = bus.subscribe()
    _, fast = bus.subscribe()
    seen = []
    for n in range(1, 7):
        bus.publish(ev("message.committed", n=n))
        seen.append(fast.get_nowait().data["n"])  # the fast one keeps up
    assert seen == [1, 2, 3, 4, 5, 6]
    assert slow.dropped == 4 and slow.qsize() == 2


def test_ordinary_types_are_the_self_superseding_ones():
    assert {"agent.status", "agent.metrics", "request.progress"} <= ORDINARY_TYPES
    assert "message.committed" not in ORDINARY_TYPES and "game.status" not in ORDINARY_TYPES


def test_wire_format_is_sse_without_an_event_field():
    bus = EventBus()
    bus.publish(ev("message.committed", content="你好", seq=7))
    (event,) = bus.subscribe(0)[0]
    body = json.loads(event.json())
    assert body == {"id": 1, "type": "message.committed", "data": {"content": "你好", "seq": 7}}
    assert "你好" in event.json()  # not \u-escaped
    assert event.sse() == f"id: 1\ndata: {event.json()}\n\n"
    assert "event:" not in event.sse()
    assert event.json() is event.json()  # encoded once


def test_non_json_values_in_event_data_do_not_break_encoding():
    bus = EventBus()
    bus.publish(UiEvent(type="x", data={"s": {1, 2}}))
    (event,) = bus.subscribe(0)[0]
    json.loads(event.json())


@async_test()
async def test_async_consumer_receives_events_in_order():
    bus = EventBus()
    _, queue = bus.subscribe()
    received: list[int] = []

    async def consume() -> None:
        while len(received) < 5:
            received.append((await queue.get()).id)

    consumer = asyncio.create_task(consume())
    for n in range(5):
        bus.publish(ev(n=n))
        await asyncio.sleep(0)
    await asyncio.wait_for(consumer, 1)
    assert received == [1, 2, 3, 4, 5]
