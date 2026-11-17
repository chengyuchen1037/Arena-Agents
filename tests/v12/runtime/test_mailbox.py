from __future__ import annotations

import asyncio

from app.runtime.mailbox import Mailbox

from .fakes import ambient, async_test, normal, settle, urgent


def labelled(priority: str, label: str):
    make = {"urgent": urgent, "normal": normal, "ambient": ambient}[priority]
    return make("x", label=label)


def labels(envelopes):
    return [e.payload["label"] for e in envelopes]


@async_test()
async def test_get_serves_urgent_before_normal_before_ambient_fifo_within_a_lane():
    box = Mailbox()
    for priority, label in [
        ("ambient", "a1"),
        ("normal", "n1"),
        ("urgent", "u1"),
        ("ambient", "a2"),
        ("urgent", "u2"),
        ("normal", "n2"),
    ]:
        box.put(labelled(priority, label))
    assert len(box) == 6 and not box.empty()
    got = [(await box.get()).payload["label"] for _ in range(6)]
    assert got == ["u1", "u2", "n1", "n2", "a1", "a2"]
    assert box.empty()


@async_test()
async def test_drain_is_priority_ordered_and_drain_arrival_keeps_arrival_order():
    box = Mailbox()
    seq = [("ambient", "a1"), ("urgent", "u1"), ("normal", "n1"), ("urgent", "u2")]
    for priority, label in seq:
        box.put(labelled(priority, label))
    assert labels(box.peek_all()) == ["a1", "u1", "n1", "u2"]
    assert labels(box.drain()) == ["u1", "u2", "n1", "a1"]
    assert len(box) == 0
    for priority, label in seq:
        box.put(labelled(priority, label))
    assert labels(box.drain_arrival()) == ["a1", "u1", "n1", "u2"]
    assert box.drain_arrival() == [] and box.drain() == []


@async_test()
async def test_get_waits_for_a_put_without_polling():
    box = Mailbox()
    getter = asyncio.create_task(box.get())
    await settle()
    assert not getter.done()
    box.put(labelled("normal", "n1"))
    assert (await asyncio.wait_for(getter, 1)).payload["label"] == "n1"


@async_test()
async def test_wait_does_not_consume_and_poke_wakes_it():
    box = Mailbox()
    waiter = asyncio.create_task(box.wait())
    await settle()
    assert not waiter.done()
    box.poke()
    await asyncio.wait_for(waiter, 1)
    assert len(box) == 0  # a poke carries no envelope
    box.put(labelled("urgent", "u1"))
    await asyncio.wait_for(box.wait(), 1)
    assert len(box) == 1  # wait() leaves the envelope in place


@async_test()
async def test_cancelled_getter_does_not_lose_later_envelopes():
    box = Mailbox()
    getter = asyncio.create_task(box.get())
    await settle()
    getter.cancel()
    await asyncio.gather(getter, return_exceptions=True)
    box.put(labelled("urgent", "u1"))
    assert (await asyncio.wait_for(box.get(), 1)).payload["label"] == "u1"


@async_test()
async def test_put_never_blocks_and_clear_drops_everything():
    box = Mailbox()
    for i in range(1000):
        box.put(labelled("ambient", f"a{i}"))
    assert len(box) == 1000
    assert box.clear() == 1000
    assert box.empty()
    box.poke()
    box.clear()  # clearing also forgets a stale poke
    waiter = asyncio.create_task(box.wait())
    await settle()
    assert not waiter.done()
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)


def test_get_nowait_on_empty_returns_none():
    assert Mailbox().get_nowait() is None
