from __future__ import annotations

import asyncio
import json

from app.runtime.bus import ORDINARY_TYPES, TRUSTED_ONLY_TYPES, EventBus
from app.runtime.ports import EventSink
from app.world.types import UiEvent

from .fakes import async_test


def ev(type_: str = "message.committed", **data) -> UiEvent:
    return UiEvent(type=type_, data=data)


def ids(events) -> list[int]:
    return [e.id for e in events]


def drain(queue) -> list:
    got = []
    while not queue.empty():
        got.append(queue.get_nowait())
    return got


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
    assert {"agent.status", "agent.metrics", "request.progress", "budget.updated"} <= ORDINARY_TYPES
    # the thresholds are announcements, not running totals: they must never be shed for a status update
    assert "message.committed" not in ORDINARY_TYPES and "game.status" not in ORDINARY_TYPES
    assert "budget.warning" not in ORDINARY_TYPES and "budget.reached" not in ORDINARY_TYPES


def test_an_error_log_line_is_an_ordinary_event_a_slow_client_may_lose():
    # v0.13.1 (G35): ``llm.call`` is one line of the error log; a client that misses some asks ``GET /api/llm/calls?after=``
    assert "llm.call" in ORDINARY_TYPES
    bus = EventBus(queue_size=4)
    _, queue = bus.subscribe()
    bus.publish(ev("message.committed", n=1))
    for n in range(30):
        bus.publish(ev("llm.call", call={"id": n}, errors=0))
    bus.publish(ev("message.committed", n=2))
    got = []
    while not queue.empty():
        got.append(queue.get_nowait())
    assert [e.data["n"] for e in got if e.type == "message.committed"] == [1, 2]  # the messages stayed
    assert queue.lost_important is False  # nothing the client cannot do without was shed: no resync is needed


# ------------------------------------------------- v0.13.1 (review): the error log is for trusted subscribers only
def test_the_error_log_event_is_the_only_trusted_only_type():
    assert TRUSTED_ONLY_TYPES == frozenset({"llm.call"})
    assert not TRUSTED_ONLY_TYPES & {"message.committed", "game.status", "agent.status", "error", "snapshot"}


def test_a_subscriber_without_a_type_never_gets_it_live_and_the_others_still_do():
    bus = EventBus()
    _, trusted = bus.subscribe()
    _, other = bus.subscribe(exclude=TRUSTED_ONLY_TYPES)
    bus.publish(ev("message.committed", n=1))
    bus.publish(ev("llm.call", call={"id": 1, "message": "provider said no"}, errors=1))
    bus.publish(ev("message.committed", n=2))
    assert [e.type for e in drain(trusted)] == ["message.committed", "llm.call", "message.committed"]
    got = drain(other)
    assert [e.type for e in got] == ["message.committed", "message.committed"] and [e.data["n"] for e in got] == [1, 2]
    assert "provider said no" not in " ".join(e.json() for e in got)
    assert bus.subscriber_count == 2 and other.dropped == 0 and other.lost_important is False
    assert bus.last_id == 3  # the ring still holds the event: ids are the bus's, not the subscriber's


def test_a_resume_replays_without_the_excluded_types_and_a_trusted_resume_gets_everything():
    bus = EventBus(ring=50)
    for n in range(1, 4):
        bus.publish(ev("message.committed", n=n))
        bus.publish(ev("llm.call", call={"id": n}, errors=0))
    replay, _ = bus.subscribe(2, exclude=TRUSTED_ONLY_TYPES)
    assert [(e.id, e.type) for e in replay] == [(3, "message.committed"), (5, "message.committed")]  # ids skip what is left out
    full, _ = bus.subscribe(2)
    assert [e.type for e in full] == ["message.committed", "llm.call", "message.committed", "llm.call"]
    caught_up, _ = bus.subscribe(6, exclude=TRUSTED_ONLY_TYPES)  # the newest event is an excluded one: nothing was missed
    assert caught_up == []
    gap, _ = bus.subscribe(0, exclude=TRUSTED_ONLY_TYPES)
    assert [e.id for e in gap] == [1, 3, 5]
    # a gap larger than the ring still needs a snapshot, whatever is left out
    small = EventBus(ring=2)
    for n in range(5):
        small.publish(ev("llm.call", call={"id": n}, errors=0))
    assert small.subscribe(1, exclude=TRUSTED_ONLY_TYPES)[0] is None


def test_what_a_subscriber_is_not_told_cannot_crowd_out_what_it_is():
    bus = EventBus(queue_size=4)
    _, other = bus.subscribe(exclude=TRUSTED_ONLY_TYPES)
    bus.publish(ev("message.committed", n=1))
    for n in range(500):
        bus.publish(ev("llm.call", call={"id": n}, errors=0))  # (a call log line for every model call of every agent)
    bus.publish(ev("message.committed", n=2))
    assert [e.data["n"] for e in drain(other)] == [1, 2] and other.dropped == 0 and other.lost_important is False
    _, trusted = bus.subscribe()
    for n in range(10):
        bus.publish(ev("llm.call", call={"id": n}, errors=0))
    assert trusted.qsize() == 4 and trusted.dropped == 6  # (the trusted one sheds the oldest ordinary event, as before)


def test_an_exclude_set_can_be_any_collection_and_empty_means_everybody():
    bus = EventBus()
    _, listed = bus.subscribe(exclude=["llm.call", "agent.metrics"])
    _, nobody = bus.subscribe(exclude=())
    _, default = bus.subscribe()
    for kind in ("llm.call", "agent.metrics", "agent.status"):
        bus.publish(ev(kind))
    assert [e.type for e in drain(listed)] == ["agent.status"]
    everything = ["llm.call", "agent.metrics", "agent.status"]
    assert [e.type for e in drain(nobody)] == everything and [e.type for e in drain(default)] == everything
    assert listed.exclude == frozenset({"llm.call", "agent.metrics"}) and nobody.exclude == frozenset()


def test_a_burst_of_agent_status_events_never_pushes_out_a_message():
    # v0.12.1: every message wakes every agent, so status chatter is heavy; it is shed first
    bus = EventBus(queue_size=6)
    _, queue = bus.subscribe()
    bus.publish(ev("message.committed", n=1))
    for n in range(50):
        bus.publish(ev("agent.status", id=f"a{n}", state="thinking", until=None))
    bus.publish(ev("message.committed", n=2))
    got = []
    while not queue.empty():
        got.append(queue.get_nowait())
    assert [e.data["n"] for e in got if e.type == "message.committed"] == [1, 2]
    assert queue.lost_important is False


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
