"""Abilities: enable / disable / use, quotas, parameters, reveal message, channel freeze, moderator wake
(contracts §2.3, §2.4, §2.6)."""

from __future__ import annotations

from app.world.types import MODERATOR_ID, Code

from .test_game_kit import (
    PLAYER_NAMES,
    ask,
    deliveries,
    entries,
    events,
    make_spec,
    make_world,
    mod,
    role_holder,
    to,
)


def setup_strike():
    """A world in which the ability ``strike`` is enabled; returns ``(world, holder_id, other_id)``."""
    world = make_world()
    holder = role_holder(world, "alpha")
    other = next(p.id for p in world.state.players if p.id != holder)
    assert mod(world, "enable_ability", ability_id="strike").ok
    world.drain_outbox()
    world.drain_events()
    return world, holder, other


def use_strike(world, actor, target, **extra):
    return ask(world, actor, "use_ability", ability_id="strike", params={"target": target, **extra})


# --------------------------------------------------------------------------- enable / disable


def test_abilities_start_disabled_unless_the_scenario_enables_them():
    world = make_world()
    assert {a.id: a.enabled for a in world.state.abilities.values()} == {"strike": False, "ping": False}

    spec = make_spec()
    spec.abilities["ping"].enabled = True
    world = make_world(spec=spec)
    assert world.state.abilities["ping"].enabled is True and world.state.abilities["strike"].enabled is False


def test_enable_and_disable_are_moderator_private_ledger_entries():
    world = make_world()
    receipt = mod(world, "enable_ability", ability_id="strike")
    assert receipt.ok
    assert world.state.abilities["strike"].enabled is True and world.state.abilities["strike"].allowed is None
    entry = entries(world, "ability.enabled")[0]
    assert entry.visible_to_ids == [MODERATOR_ID] and entry.data == {"ability_id": "strike"}
    assert world.drain_outbox() == []  # the engine does not tell players; the moderator announces it

    assert mod(world, "disable_ability", ability_id="strike").ok
    assert world.state.abilities["strike"].enabled is False
    disabled = entries(world, "ability.disabled")[0]
    assert disabled.visible_to_ids == [MODERATOR_ID] and disabled.data == {"ability_id": "strike"}


def test_enable_can_limit_holders_and_a_plain_enable_resets_the_limit():
    world = make_world()
    assert mod(world, "enable_ability", ability_id="ping", holders=["u1"]).ok
    assert world.state.abilities["ping"].allowed == ["u1"]
    assert entries(world, "ability.enabled")[0].data == {"ability_id": "ping", "holders": ["u1"]}
    assert ask(world, "u1", "use_ability", ability_id="ping").ok
    assert ask(world, "u2", "use_ability", ability_id="ping").code == Code.ABILITY_DISABLED

    assert mod(world, "enable_ability", ability_id="ping").ok
    assert world.state.abilities["ping"].allowed is None
    assert ask(world, "u2", "use_ability", ability_id="ping").ok


def test_enable_disable_errors_change_nothing():
    world = make_world()
    seq = world.seq
    assert mod(world, "enable_ability", ability_id="nope").code == Code.UNKNOWN_ABILITY
    assert mod(world, "disable_ability", ability_id="nope").code == Code.UNKNOWN_ABILITY
    assert mod(world, "enable_ability", ability_id="ping", holders=["ghost"]).code == Code.UNKNOWN_PLAYER
    assert mod(world, "disable_ability", ability_id="ping", holders=["u1"]).code == Code.INVALID_PARAMS
    assert world.seq == seq and world.state.abilities["ping"].enabled is False


# --------------------------------------------------------------------------- rejections


def test_use_ability_rejections_in_gate_order():
    world = make_world()
    holder = role_holder(world, "alpha")
    other = next(p.id for p in world.state.players if p.id != holder)
    seq = world.seq

    unknown = ask(world, holder, "use_ability", ability_id="nope")
    assert unknown.code == Code.UNKNOWN_ABILITY and "strike" in unknown.message
    assert use_strike(world, holder, other).code == Code.ABILITY_DISABLED  # not enabled yet
    assert use_strike(world, other, holder).code == Code.ABILITY_DISABLED  # disabled wins over "not a holder"
    assert world.seq == seq

    mod(world, "enable_ability", ability_id="strike")
    seq = world.seq
    assert use_strike(world, other, holder).code == Code.FORBIDDEN
    assert world.seq == seq and world.state.abilities["strike"].uses == {}


def test_holders_expression_is_evaluated_at_use_time():
    world, holder, other = setup_strike()
    # the holder gets an "out" tag: alive(...) no longer selects them
    assert mod(world, "update_player", player_id=holder, add_tags=["out"]).ok
    receipt = use_strike(world, holder, other)
    assert receipt.code == Code.FORBIDDEN
    assert mod(world, "update_player", player_id=holder, remove_tags=["out"]).ok
    assert use_strike(world, holder, other).ok


def test_quota_is_per_holder_and_enforced():
    world, holder, other = setup_strike()
    assert use_strike(world, holder, other).ok
    seq = world.seq
    again = use_strike(world, holder, other)
    assert again.code == Code.QUOTA_EXCEEDED
    assert world.seq == seq and world.state.abilities["strike"].uses == {holder: 1}

    mod(world, "enable_ability", ability_id="ping")
    for _ in range(2):
        assert ask(world, "u1", "use_ability", ability_id="ping").ok
    assert ask(world, "u1", "use_ability", ability_id="ping").code == Code.QUOTA_EXCEEDED
    assert ask(world, "u2", "use_ability", ability_id="ping").ok  # another holder, own quota


def test_parameter_validation_never_consumes_quota_or_freezes():
    world, holder, other = setup_strike()
    seq = world.seq
    missing = ask(world, holder, "use_ability", ability_id="strike", params={})
    assert missing.code == Code.INVALID_PARAMS
    bad_target = ask(world, holder, "use_ability", ability_id="strike", params={"target": "ghost"})
    assert bad_target.code == Code.INVALID_TARGET
    extra = ask(world, holder, "use_ability", ability_id="strike", params={"target": other, "oops": 1})
    assert extra.code == Code.INVALID_PARAMS
    assert world.seq == seq
    assert world.state.abilities["strike"].uses == {}
    assert world.chat.frozen == [] and world.drain_outbox() == []

    assert use_strike(world, holder, other).ok  # still has its single use


def test_text_param_is_truncated_by_the_param_spec():
    world, holder, other = setup_strike()
    assert use_strike(world, holder, other, note="x" * 30).ok
    assert entries(world, "ability.used")[0].data["params"] == {"target": other, "note": "x" * 10}


# --------------------------------------------------------------------------- success


def test_use_ability_writes_entry_reveal_message_freezes_and_wakes_the_moderator():
    world, holder, other = setup_strike()
    receipt = use_strike(world, holder, other)
    assert receipt.ok and receipt.data["uses_left"] == 0 and receipt.data["ability_id"] == "strike"
    assert world.state.abilities["strike"].uses == {holder: 1}

    used = entries(world, "ability.used")
    assert len(used) == 1
    assert used[0].actor_id == holder and used[0].visible_to_ids == [holder, MODERATOR_ID]
    assert used[0].data == {"ability_id": "strike", "actor_id": holder, "params": {"target": other}}
    assert receipt.seq == used[0].seq

    message = entries(world, "message")[0]
    assert message.channel_id == "public" and message.actor_id == holder and message.seq > used[0].seq
    assert message.data["kind"] == "ability"
    assert message.data["content"] == (
        f"{PLAYER_NAMES[holder]}（{holder}）发动了 strike，目标是 {PLAYER_NAMES[other]}（{other}）。"
    )
    assert message.visible_to_ids == world.channel("public").members

    assert world.chat.frozen == [("public", "ability:strike")]

    items = deliveries(world)
    woken = to(items, MODERATOR_ID, "ability.used")
    assert len(woken) == 1
    env = woken[0].envelope
    assert (env.kind, env.priority, env.interrupt) == ("ability", "urgent", False)
    assert env.payload == {
        "event": "ability.used",
        "ability_id": "strike",
        "actor_id": holder,
        "params": {"target": other},
    }
    ui = events(world)
    assert [e.data for e in ui if e.type == "ability.used"] == [{"ability_id": "strike", "actor_id": holder}]
    assert [e.data["kind"] for e in ui if e.type == "message.committed"] == ["ability"]


def test_the_reveal_message_ignores_floor_state_of_the_channel():
    world, holder, other = setup_strike()
    channel = world.channel("public")
    channel.mode = "closed"
    channel.frozen = True
    assert use_strike(world, holder, other).ok
    message = entries(world, "message")[0]
    assert message.data["kind"] == "ability" and message.actor_id == holder


def test_ability_without_reveal_or_freeze_is_just_a_ledger_entry_and_a_wake():
    world = make_world()
    mod(world, "enable_ability", ability_id="ping")
    world.drain_outbox()
    receipt = ask(world, "u3", "use_ability", ability_id="ping")
    assert receipt.ok and receipt.data["uses_left"] == 1
    assert entries(world, "message") == [] and world.chat.frozen == []
    woken = to(deliveries(world), MODERATOR_ID, "ability.used")
    assert [d.envelope.priority for d in woken] == ["urgent"]


def test_ability_works_without_a_chat_module_and_with_unknown_channels():
    world, holder, other = setup_strike()
    del world.chat  # no chat module attached
    assert use_strike(world, holder, other).ok  # nothing to freeze with, still succeeds

    spec = make_spec()
    spec.abilities["strike"].reveal.channel_id = "nowhere"
    spec.abilities["strike"].freeze_channels = ["nowhere"]
    world = make_world(spec=spec)
    holder = role_holder(world, "alpha")
    other = next(p.id for p in world.state.players if p.id != holder)
    mod(world, "enable_ability", ability_id="strike")
    assert use_strike(world, holder, other).ok
    assert entries(world, "message") == [] and world.chat.frozen == []
