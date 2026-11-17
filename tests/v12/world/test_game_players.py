"""update_player / set_phase / update_dashboard (contracts §2.3, §2.5, §2.6, §8.2)."""

from __future__ import annotations

from app.world.types import MODERATOR_ID, Code, Intent

from .test_game_kit import PLAYER_IDS, ask, deliveries, entries, events, make_world, mod, player, to


def update(world, player_id="u1", **payload):
    return mod(world, "update_player", player_id=player_id, **payload)


# --------------------------------------------------------------------------- update_player


def test_update_player_changes_tags_and_merges_attributes():
    world = make_world()
    me = player(world, "u1")
    me.attributes.update({"keep": 1, "lives": 5})
    start_tags = list(me.tags)

    receipt = update(world, add_tags=["out", "secret"], remove_tags=[start_tags[0]], set_attributes={"lives": 0, "new": "x"})
    assert receipt.ok and receipt.data["changed"] is True
    assert me.tags == [*start_tags[1:], "out", "secret"]
    assert me.attributes["keep"] == 1 and me.attributes["lives"] == 0 and me.attributes["new"] == "x"

    entry = entries(world, "player.updated")[-1]
    assert entry.actor_id == MODERATOR_ID and entry.visible_to_ids == ["u1", MODERATOR_ID]
    assert entry.data == {
        "player_id": "u1",
        "add_tags": ["out", "secret"],
        "remove_tags": [start_tags[0]],
        "set_attributes": {"lives": 0, "new": "x"},
        "tags_after": me.tags,
    }
    assert receipt.seq == entry.seq


def test_public_change_entry_only_carries_public_tags_and_hides_the_rest():
    world = make_world()
    me = player(world, "u1")
    assert "active" in me.tags  # public tag
    update(world, add_tags=["out", "secret"], remove_tags=["active"])

    public = entries(world, "player.public_changed")
    assert len(public) == 1
    entry = public[0]
    assert entry.data == {"player_id": "u1", "added": ["out"], "removed": ["active"]}
    assert entry.visible_to_ids == [MODERATOR_ID, "u2", "u3", "u4"]  # everybody except the player
    assert "secret" not in str(entry.data)

    private = entries(world, "player.updated")[-1]
    assert "u2" not in private.visible_to_ids
    assert [e.type for e in world.entries_visible("u2") if e.seq > 0 and e.type.startswith("player.")] == [
        "player.updated",  # u2's own deal entry
        "player.public_changed",
    ]
    own = [e.type for e in world.entries_visible("u1") if e.type.startswith("player.")]
    assert own == ["player.updated", "player.updated"]  # deal + this update, never the public record
    assert sorted({e.type for e in world.entries_visible(MODERATOR_ID) if e.type.startswith("player.")}) == [
        "player.public_changed",
        "player.updated",
    ]


def test_private_only_changes_write_no_public_entry():
    world = make_world()
    update(world, add_tags=["secret"], set_attributes={"mood": "calm"})
    assert entries(world, "player.public_changed") == []
    items = deliveries(world)
    assert [d.target for d in items] == ["u1"]  # nobody else is told


def test_update_player_ui_event_carries_all_tags_and_the_out_flag():
    world = make_world()
    update(world, add_tags=["out", "secret"])
    ui = events(world, "player.updated")
    assert len(ui) == 1
    assert ui[0].data["player_id"] == "u1" and ui[0].data["out"] is True
    assert ui[0].data["tags"] == player(world, "u1").tags and "secret" in ui[0].data["tags"]

    update(world, remove_tags=["out"])
    assert events(world, "player.updated")[0].data["out"] is False


def test_update_player_notices_are_ambient_and_public_changes_reach_the_others():
    world = make_world()
    update(world, add_tags=["secret", "leader"])
    items = deliveries(world)

    own = to(items, "u1", "player.updated")[0].envelope
    assert (own.kind, own.priority, own.interrupt) == ("notice", "ambient", False)
    assert own.payload["player_id"] == "u1" and own.payload["add_tags"] == ["secret", "leader"]

    others = [d for d in items if d.target != "u1"]
    assert sorted(d.target for d in others) == ["u2", "u3", "u4"]
    for d in others:
        assert (d.envelope.kind, d.envelope.priority) == ("notice", "ambient")
        assert d.envelope.payload == {
            "event": "player.updated",
            "player_id": "u1",
            "added": ["leader"],
            "removed": [],
        }
    assert to(items, MODERATOR_ID) == []


def test_update_player_without_effective_change_is_a_silent_success():
    world = make_world()
    me = player(world, "u1")
    existing = me.tags[0]
    seq = world.seq
    receipt = update(world, add_tags=[existing], remove_tags=["never-had"])
    assert receipt.ok and receipt.data["changed"] is False
    assert world.seq == seq and deliveries(world) == [] and events(world) == []


def test_tag_in_both_lists_is_removed_then_added_again():
    world = make_world()
    me = player(world, "u1")
    before = list(me.tags)
    assert update(world, add_tags=[before[0]], remove_tags=[before[0]]).data["changed"] is False
    assert me.tags == before
    assert update(world, add_tags=["fresh"], remove_tags=["fresh"]).ok
    assert me.tags == [*before, "fresh"]


def test_set_attributes_can_change_role_and_faction():
    world = make_world()
    me = player(world, "u1")
    receipt = update(world, set_attributes={"role": "delta", "faction": "green", "note": 1})
    assert receipt.ok
    assert (me.role, me.faction) == ("delta", "green") and me.attributes["note"] == 1
    assert "role" not in me.attributes and "faction" not in me.attributes
    entry = entries(world, "player.updated")[-1]
    assert entry.data["role"] == "delta" and entry.data["faction"] == "green"
    assert entry.data["set_attributes"] == {"note": 1}
    # expressions see the new role
    assert world.to_plain(world.eval_expr("${by_role('delta')}")) == ["u1"]

    # setting the values a player already has changes nothing
    assert update(world, set_attributes={"role": "delta", "faction": "green"}).data["changed"] is False


def test_update_player_unknown_player_changes_nothing():
    world = make_world()
    seq = world.seq
    assert update(world, player_id="ghost", add_tags=["x"]).code == Code.UNKNOWN_PLAYER
    assert world.seq == seq
    assert ask(world, "u1", "mod.update_player", player_id="u1", add_tags=["x"]).code == Code.FORBIDDEN


def test_update_player_never_changes_channels_or_floors():
    world = make_world()
    members = {c.id: list(c.members) for c in world.state.channels}
    modes = {c.id: c.mode for c in world.state.channels}
    update(world, add_tags=["out"])
    assert {c.id: list(c.members) for c in world.state.channels} == members
    assert {c.id: c.mode for c in world.state.channels} == modes
    assert world.chat.frozen == []


def test_update_player_invalidates_drafts_only_when_it_changes_something():
    world = make_world()  # the scenario lists ``player.updated`` in ``invalidates_drafts``

    def draft_code(based_on_seq: int) -> str | None:
        # there is no chat module here, so a draft that survives the STALE check ends in INTERNAL_ERROR
        intent = Intent(
            actor_id="u3", type="speak", payload={"channel_id": "public", "content": "hi"}, based_on_seq=based_on_seq
        )
        return world.submit(intent).code

    base = world.seq
    update(world, add_tags=[player(world, "u1").tags[0]])  # no effective change: nothing is written
    assert draft_code(base) != Code.STALE

    assert update(world, player_id="u2", add_tags=["leader"]).ok
    assert draft_code(base) == Code.STALE
    assert draft_code(world.seq) != Code.STALE  # a draft written after the change is fine


# --------------------------------------------------------------------------- phase / dashboard


def test_set_phase_is_public_and_notifies_every_player_quietly():
    world = make_world()
    receipt = mod(world, "set_phase", text="第一阶段")
    assert receipt.ok and world.state.phase == "第一阶段"

    entry = entries(world, "phase.changed")[0]
    assert entry.actor_id == MODERATOR_ID and entry.data == {"text": "第一阶段"}
    assert sorted(entry.visible_to_ids) == sorted([MODERATOR_ID, *PLAYER_IDS])

    items = deliveries(world)
    assert sorted(d.target for d in items) == sorted(PLAYER_IDS)
    for d in items:
        assert (d.envelope.kind, d.envelope.priority) == ("notice", "ambient")
        assert d.envelope.payload == {"event": "phase.changed", "text": "第一阶段"}
    assert [e.data for e in events(world, "phase.changed")] == [{"text": "第一阶段"}]


def test_set_phase_rejects_blank_text():
    world = make_world()
    seq = world.seq
    assert mod(world, "set_phase", text="   ").code == Code.INVALID_PARAMS
    assert world.seq == seq and world.state.phase == ""


def test_update_dashboard_replaces_the_dashboard_and_is_public():
    world = make_world()
    receipt = mod(
        world,
        "update_dashboard",
        factions=[{"name": "红队", "alive": 1, "total": 1, "variant": "danger"}],
        tag_stats=[{"name": "存活", "count": 3}],
    )
    assert receipt.ok
    expected = {
        "factions": [{"name": "红队", "alive": 1, "total": 1, "variant": "danger"}],
        "tag_stats": [{"name": "存活", "count": 3, "variant": "neutral"}],
    }
    assert world.state.dashboard.factions == expected["factions"]
    assert world.state.dashboard.tag_stats == expected["tag_stats"]
    entry = entries(world, "dashboard.updated")[0]
    assert entry.data == expected and sorted(entry.visible_to_ids) == sorted([MODERATOR_ID, *PLAYER_IDS])
    assert [e.data for e in events(world, "dashboard.updated")] == [{"dashboard": expected}]

    mod(world, "update_dashboard", factions=[], tag_stats=[])
    assert world.state.dashboard.factions == [] and world.state.dashboard.tag_stats == []
