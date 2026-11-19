"""Random action sequences against the chat module: structural invariants hold, nothing crashes.

Every receipt may be a rejection (most random moves are illegal); what must never happen is an
``INTERNAL_ERROR``, an inconsistent channel / floor state, or a save that does not load back identically.
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from app.world.chat import FLOOR_MODES, floor_view, token_untimed
from app.world.types import ENGINE_ID, MODERATOR_ID, Code, Receipt

from .helpers import (
    PLAYERS,
    ChatWorld,
    ManualClock,
    do_pass,
    kill,
    make_world,
    mod,
    new_channel,
    revive,
    speak,
    speak_template,
)

CHANNELS = ["c1", "c2", "c3"]
ALL_IDS = [*PLAYERS, MODERATOR_ID, "ghost"]


def check_invariants(world: ChatWorld) -> None:
    seqs = [e.seq for e in world.ledger]
    assert seqs == list(range(1, len(seqs) + 1)), "the ledger must be gap-free"
    everyone = set(world.everyone())
    for entry in world.ledger:
        assert set(entry.visible_to_ids) <= everyone
        if entry.type == "message":
            assert entry.actor_id in entry.visible_to_ids, "an author always sees its own message"
    now = world.clock.now()
    for channel in world.state.channels:
        assert channel.members[0] == MODERATOR_ID and len(set(channel.members)) == len(channel.members)
        assert set(channel.readonly) <= set(channel.members) and MODERATOR_ID not in channel.readonly
        if channel.system:
            assert channel.type == "dm" and channel.owner and channel.members == [MODERATOR_ID, channel.owner]
            assert channel.free is None and channel.group is None and channel.mode in ("free", "closed", "listen")
        if channel.mode in FLOOR_MODES:
            group = channel.group
            assert group is not None and not group.completed and group.mode == channel.mode
            assert channel.free is None
            assert not all(t.status == "done" for t in group.tokens), "a finished group is dropped"
            statuses = [t.status for t in group.tokens]
            if group.mode == "queue":
                assert statuses.count("active") == 1
                at = statuses.index("active")
                assert set(statuses[:at]) <= {"done"}  # whoever came before the speaker is finished
                assert set(statuses[at + 1 :]) <= {"pending", "done"}  # (a waiting member may have been removed)
            for token in group.tokens:
                if token.status == "active":
                    if group.paused or token_untimed(world, channel, token):
                        # frozen, or (v0.13.1) held by the seat a person plays in a public channel: no deadline,
                        # the nominal time left is kept
                        assert token.deadline is None and token.remaining is not None
                    else:
                        assert token.deadline is not None and token.remaining is None
                    assert token.messages_used < token.max_messages and token.chars_used < token.max_chars_total
                else:
                    assert token.deadline is None and token.remaining is None
                if token.status != "done":
                    assert token.holder in channel.members
            if channel.frozen:
                assert group.paused, "a frozen floor never keeps running"
        else:
            assert channel.group is None
        if channel.mode == "free" and not channel.system:
            assert channel.free is not None
            assert set(channel.free.counts) <= set(PLAYERS)
            if channel.free.writers is not None:
                assert set(channel.free.writers) <= set(channel.members)
        elif channel.mode != "free":
            assert channel.free is None
    deadline = world.next_deadline()
    assert deadline is None or isinstance(deadline, float)
    assert now >= 0


DIRECT_EVENTS = frozenset({"floor.granted", "floor.revoked", "floor.paused", "floor.frozen"})


def check_deliveries(world: ChatWorld, deliveries: list, where: str) -> None:
    """The delivery rules of contracts §10.1 that hold for every state of the world."""
    known = set(world.everyone()) | {ENGINE_ID}
    for delivery in deliveries:
        env = delivery.envelope
        assert delivery.target in known, where
        assert env.priority in ("urgent", "normal"), where  # there is no ambient level
        assert "debounce_s" not in env.payload, where
        if env.sender is not None:
            assert env.sender != delivery.target, f"{where}: {env.payload['event']} went back to its sender"
        player = world.player(delivery.target)
        if player is not None and world.is_out(player):
            event = env.payload["event"]
            channel = world.channel(env.channel_id) if env.channel_id else None
            direct_message = event == "message.new" and channel is not None and channel.system
            assert event in DIRECT_EVENTS or direct_message, f"{where}: {event} woke {delivery.target}, who is out"


def check_after_tick(world: ChatWorld) -> None:
    now = world.clock.now()
    for channel in world.state.channels:
        group = channel.group
        if group is not None and not group.paused:
            for token in group.tokens:
                if token.status == "active" and not token_untimed(world, channel, token):
                    assert token.deadline > now, "tick() must have expired every overdue token"


def reload(world: ChatWorld) -> ChatWorld:
    world.set_running(False, "pause")
    data = json.loads(json.dumps(world.dump(), ensure_ascii=False))
    restored = ChatWorld.restore(data, world.spec, ManualClock(data["clock_now"]))
    assert restored.state.model_dump(exclude={"rng", "status"}) == world.state.model_dump(exclude={"rng", "status"})
    restored.set_running(True, "resume")
    restored.redeliver()
    return restored


def pick(rng: random.Random, items: list[str], low: int = 0, high: int | None = None) -> list[str]:
    return rng.sample(items, rng.randint(low, min(len(items), high if high is not None else len(items))))


def random_free_limits(rng: random.Random) -> dict[str, Any] | None:
    if rng.random() < 0.5:
        return None
    options: dict[str, Any] = {
        "duration_s": rng.choice([5, 50, 300]),
        "max_messages_per_member": rng.choice([1, 2, 6]),
        "max_chars_per_message": rng.choice([5, 30]),
        "max_chars_total": rng.choice([10, 100, 1000]),
        "silence_s": rng.choice([5, 40]),
    }
    limits = {k: v for k, v in options.items() if rng.random() < 0.4}
    if rng.random() < 0.1:
        limits["max_messages"] = 1  # a key of the other mode: must be refused, not applied
    return limits


def random_token_limits(rng: random.Random) -> dict[str, Any] | None:
    if rng.random() < 0.5:
        return None
    limits: dict[str, Any] = {"max_messages": rng.randint(1, 3), "duration_s": rng.choice([5, 30, 90])}
    if rng.random() < 0.1:
        limits["silence_s"] = 5  # a key of the other mode
    return limits


def random_move(rng: random.Random, world: ChatWorld) -> Receipt | None:
    channel_id = rng.choice([*CHANNELS, "dm-N1", "dm-N2", "nowhere"])
    actor = rng.choice(PLAYERS)
    kind = rng.choices(
        [
            "speak", "template", "pass", "send", "announce", "turn", "free", "queue", "pause", "resume", "close",
            "update", "freeze", "tick", "reload", "kill", "revive",
        ],
        weights=[22, 6, 8, 3, 2, 6, 4, 8, 3, 4, 3, 10, 2, 12, 1, 3, 2],
    )[0]
    if kind == "speak":
        return speak(world, actor, channel_id, "字" * rng.randint(1, 45))
    if kind == "template":
        return speak_template(world, actor, channel_id, rng.choice(["count", "long", "vote", "shout", "nope"]), n=rng.randint(0, 11), target="N3", who=["N1"], word="yes")
    if kind == "pass":
        return do_pass(world, actor, channel_id)
    if kind == "send":
        return mod(world, "send_message", channel_id=channel_id, content="话" * rng.randint(1, 60))
    if kind == "announce":
        return mod(world, "announce", channel_id=channel_id, template_id="notice", params={"text": "公告"})
    if kind == "turn":
        holders = pick(rng, [*PLAYERS, "ghost"], 1, 4)
        limits = random_token_limits(rng)
        return mod(world, "open_floor", channel_id=channel_id, mode="turn", holders=holders, **({"limits": limits} if limits else {}))
    if kind == "free":
        limits = random_free_limits(rng)
        return mod(world, "open_floor", channel_id=channel_id, mode="free", holders=pick(rng, PLAYERS, 0, 3), **({"limits": limits} if limits else {}))
    if kind == "queue":
        order = [rng.choice(PLAYERS) for _ in range(rng.randint(1, 5))]
        limits = {"max_messages": rng.randint(1, 3), "duration_s": rng.choice([5, 30, 90])} if rng.random() < 0.5 else None
        return mod(world, "set_floor_queue", channel_id=channel_id, order=order, **({"limits": limits} if limits else {}))
    if kind == "kill":
        kill(world, rng.choice(PLAYERS))
        return None
    if kind == "revive":
        revive(world, rng.choice(PLAYERS))
        return None
    if kind == "pause":
        return mod(world, "pause_queue", channel_id=channel_id)
    if kind == "resume":
        return mod(world, "resume_queue", channel_id=channel_id)
    if kind == "close":
        return mod(world, "close_floor", channel_id=channel_id)
    if kind == "update":
        payload: dict[str, Any] = {"channel_id": channel_id}
        if rng.random() < 0.4:
            payload["add_members"] = pick(rng, ALL_IDS, 0, 2)
        if rng.random() < 0.4:
            payload["remove_members"] = pick(rng, ALL_IDS, 0, 2)
        if rng.random() < 0.3:
            payload["readonly_members"] = pick(rng, ALL_IDS, 0, 2)
        if rng.random() < 0.5:
            payload["mode"] = rng.choice(["closed", "free", "listen"])
        limits = random_free_limits(rng)
        if limits:
            payload["limits"] = limits
        if rng.random() < 0.2:
            payload["name"] = "新名字"
        return mod(world, "update_channel", **payload)
    if kind == "freeze":
        world.chat.freeze_channel(channel_id, "fuzz")
        return None
    if kind == "tick":
        world.clock.advance(rng.choice([0, 1, 4, 10, 31, 61, 200]))
        world.tick()
        check_after_tick(world)
        return None
    return None


@pytest.mark.parametrize("seed", range(40))
def test_random_sequences_keep_the_world_consistent(seed: int) -> None:
    rng = random.Random(seed)
    world = make_world()
    for cid in CHANNELS:
        members = pick(rng, PLAYERS, 2, 4)
        assert new_channel(world, cid, members=members, mode=rng.choice(["closed", "free", "listen"])).ok
    codes: set[str] = set()
    for step in range(120):
        if rng.random() < 0.01:
            world = reload(world)
        receipt = random_move(rng, world)
        if receipt is not None:
            assert receipt.code != Code.INTERNAL_ERROR, f"seed {seed} step {step}: {receipt.message}"
            codes.add(receipt.code or "OK")
        check_invariants(world)
        check_deliveries(world, world.drain_outbox(), f"seed {seed} step {step}")
        world.drain_events()
    assert world.ledger  # something happened


def random_move_with_a_person(rng: random.Random, world: ChatWorld) -> Receipt | None:
    """Like :func:`random_move`, and now and then a person takes a seat over / gives it back (v0.13.1)."""
    if rng.random() < 0.06:
        world.set_untimed_holders(rng.choice([[], ["N1"], ["N2"], ["N1", "N3"], ["ghost"]]))
        return None
    return random_move(rng, world)


@pytest.mark.parametrize("seed", range(40))
def test_random_sequences_with_a_played_seat_keep_the_world_consistent(seed: int) -> None:
    """The seat a person plays holds public floors without a deadline; taking it over and giving it back in the
    middle of anything must leave every floor in a state the invariants accept (an untimed token has no deadline
    and its nominal time, every other active token of a running group has a deadline and tick() expires it)."""
    rng = random.Random(1000 + seed)
    world = make_world()
    world.set_untimed_holders([rng.choice(PLAYERS)])
    for cid in CHANNELS:
        members = pick(rng, PLAYERS, 2, 4)
        mode = rng.choice(["closed", "free", "listen"])
        assert new_channel(world, cid, members=members, mode=mode, type_=rng.choice(["public", "group"])).ok
    for step in range(120):
        if rng.random() < 0.01:
            holders = world.untimed_holders
            world = reload(world)
            world.set_untimed_holders(holders)  # (what the orchestrator does after a load: the save does not know)
        receipt = random_move_with_a_person(rng, world)
        if receipt is not None:
            assert receipt.code != Code.INTERNAL_ERROR, f"seed {seed} step {step}: {receipt.message}"
        check_invariants(world)
        for channel in world.state.channels:  # the view agrees with the tokens
            view = floor_view(world, channel)
            group = channel.group if channel.mode in FLOOR_MODES else None
            active = [t for t in group.tokens if t.status == "active"] if group is not None else []
            timed = [t for t in active if not token_untimed(world, channel, t)]
            assert view["untimed"] is (bool(active) and not timed), f"seed {seed} step {step}"
            assert view["untimed"] is False or view["seconds_left"] is None
        check_deliveries(world, world.drain_outbox(), f"seed {seed} step {step}")
        world.drain_events()
    assert world.ledger


def test_the_fuzz_reaches_most_rejection_codes() -> None:
    """A smoke test of the fuzzer itself: across the seeds it must hit the interesting receipts."""
    seen: set[str] = set()
    for seed in range(120):
        rng = random.Random(seed)
        world = make_world()
        for cid in CHANNELS:
            new_channel(world, cid, members=pick(rng, PLAYERS, 2, 4), mode=rng.choice(["closed", "free", "listen"]))
        for _ in range(120):
            receipt = random_move(rng, world)
            if receipt is not None:
                seen.add(receipt.code or "OK")
            world.drain_outbox()
            world.drain_events()
    expected = {
        "OK", Code.NO_FLOOR, Code.NOT_MEMBER, Code.CHANNEL_CLOSED, Code.QUOTA_EXCEEDED, Code.UNKNOWN_CHANNEL,
        Code.UNKNOWN_PLAYER, Code.INVALID_TARGET, Code.INVALID_PARAMS, Code.FORBIDDEN, Code.UNKNOWN_TEMPLATE,
    }
    assert expected <= seen, f"never produced: {sorted(expected - seen)}"
