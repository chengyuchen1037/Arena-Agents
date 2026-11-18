"""Dealing (contracts §2.3 "发牌")."""

from __future__ import annotations

from collections import Counter

import pytest

from app.world.types import ENGINE_ID, MODERATOR_ID

from .test_game_kit import PLAYER_IDS, entries, make_spec, make_world


def _assignment(world) -> dict[str, str]:
    return {p.id: p.role for p in world.state.players}


def test_deal_expands_the_pool_and_sets_every_field():
    world = make_world(deal=False, running=False)
    dealt = world.game.deal()

    assert [d["player_id"] for d in dealt] == PLAYER_IDS
    assert Counter(p.role for p in world.state.players) == Counter({"alpha": 1, "beta": 1, "gamma": 2})
    roles = world.spec.setup.roles
    for p in world.state.players:
        assert p.faction == roles[p.role].faction
        assert p.tags == roles[p.role].tags
        assert p.attributes == roles[p.role].attributes
    assert [d["role"] for d in dealt] == [p.role for p in world.state.players]


def test_deal_copies_role_attributes_instead_of_sharing_them():
    world = make_world()
    gammas = [p for p in world.state.players if p.role == "gamma"]
    assert len(gammas) == 2
    gammas[0].attributes["group"] = "changed"
    assert gammas[1].attributes["group"] == "plain"
    assert world.spec.setup.roles["gamma"].attributes["group"] == "plain"


def test_deal_writes_one_private_player_updated_entry_per_player():
    world = make_world(deal=False, running=False)
    world.game.deal()
    updates = entries(world, "player.updated")

    assert len(updates) == 4
    for entry, p in zip(updates, world.state.players):
        assert entry.actor_id == ENGINE_ID
        assert entry.visible_to_ids == [p.id, MODERATOR_ID]
        assert entry.data["player_id"] == p.id
        assert entry.data["reason"] == "deal"
        assert entry.data["role"] == p.role
        assert entry.data["faction"] == p.faction
        assert entry.data["tags_after"] == p.tags
        assert entry.data["add_tags"] == p.tags
        assert entry.data["set_attributes"] == p.attributes
    # each entry is readable by its owner only (besides the moderator)
    for p in world.state.players:
        mine = [e.data["player_id"] for e in world.entries_visible(p.id)]
        assert mine == [p.id]


def test_deal_is_reproducible_for_the_same_seed_and_differs_between_seeds():
    first = _assignment(make_world(seed=11))
    again = _assignment(make_world(seed=11))
    assert first == again

    variants = {tuple(_assignment(make_world(seed=s)).items()) for s in range(1, 12)}
    assert len(variants) > 1


def test_deal_without_shuffle_follows_pool_order():
    spec = make_spec()
    spec.setup.shuffle = False
    world = make_world(spec=spec)
    assert [p.role for p in world.state.players] == ["alpha", "beta", "gamma", "gamma"]


def test_deal_consumes_the_world_rng_so_later_draws_stay_reproducible():
    one = make_world(seed=5)
    two = make_world(seed=5)
    assert one.rng.random() == two.rng.random()


@pytest.mark.parametrize("ids", [["u1", "u2", "u3"], ["u1", "u2", "u3", "u4", "u5"]])
def test_deal_rejects_a_player_count_that_differs_from_the_pool(ids):
    world = make_world(deal=False, running=False, players=ids)
    before = len(world.ledger)
    with pytest.raises(ValueError):
        world.game.deal()
    assert len(world.ledger) == before
    assert all(p.role == "" for p in world.state.players)


def test_deal_does_not_create_channels():
    world = make_world(deal=False, running=False)
    channels_before = [c.id for c in world.state.channels]
    world.game.deal()
    assert [c.id for c in world.state.channels] == channels_before
    assert entries(world, "channel.created") == []


def test_scenario_without_an_identity_pool_deals_nothing():
    spec = make_spec(setup={"public_tags": ["active"]})
    world = make_world(spec=spec, deal=False, running=False)
    assert world.game.deal() == []
    assert world.ledger == []
