"""The replay projector (``app/game/replay.py``) against the real world.

A scripted game is played on the real ``World`` (kernel + chat + game).  After every step the spectator
events the world emitted *live* are compared with the events ``build_replay`` projects from the ledger of
the same moment: messages, requests, tickets, tags, phases, the dashboard and - folded the way the client
store does - every channel with its floor.  A replay is only as good as this equality.
"""

from __future__ import annotations

import copy
import json
import time
from collections import Counter
from typing import Any

from app.game.replay import REPLAY_VERSION, build_replay
from app.world.chat import channel_view
from app.world.types import UiEvent
from app.world.world import World

from .helpers import ManualClock, do_pass, flush, make_spec, mod, ok, speak, speak_template, submit

IDS = ["N1", "N2", "N3", "N4"]
SETUP = {
    "identity_pool": [{"role": "lead", "count": 1}, {"role": "crew", "count": 3}],
    "roles": {
        "lead": {"faction": "red", "tags": ["active", "red"], "attributes": {"lives": 2}, "brief": "你是领队。"},
        "crew": {"faction": "blue", "tags": ["active"], "attributes": {}, "brief": "你是队员。"},
    },
    "public_tags": ["active"],
}
ABILITIES = {
    "flare": {
        "holders": "${alive(by_role('lead'))}",
        "quota": 1,
        "freeze_channels": ["public"],
        "reveal": {"channel_id": "public", "text": "{{actor}}亮出了信号。"},
        "description": "亮出信号。",
    }
}
OPTIONS = [{"id": "a", "label": "甲"}, {"id": "b", "label": "乙"}]


def make_game_spec():
    return make_spec(setup=SETUP, abilities=ABILITIES, ui={"out_tags": ["out"]})


def players() -> list[dict[str, Any]]:
    return [{"id": pid, "seat": str(i), "name": f"玩家{i}", "persona": ""} for i, pid in enumerate(IDS, start=1)]


def payload_of(world: World, spec: Any) -> dict[str, Any]:
    """What ``GameStore.load`` would return for this world (the parts a replay reads)."""
    return {
        "session_id": "replay-test",
        "schema_version": 5,
        "version": "v0.13.0",
        "title": "重放测试",
        "scenario_name": spec.id,
        "scenario_title": "",
        "status": world.state.status,
        "participants": [{"id": p.id, "seat": p.seat, "name": p.name, "avatar": None} for p in world.state.players],
        "scenario_spec": spec.model_dump(mode="json"),
        "world": world.dump(),
        "usage_stats": {},
        "tag_styles": {},
        "human_spans": [],
    }


def fold_channels(base: dict[str, dict[str, Any]], events: list[tuple[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """The channels a client store ends up with after these events (what ``store.js`` does with them)."""
    out = copy.deepcopy(base)
    for type_, data in events:
        if type_ in ("channel.created", "channel.updated"):
            out[data["channel"]["id"]] = copy.deepcopy(data["channel"])
        elif type_ == "floor.changed" and data["channel_id"] in out:
            out[data["channel_id"]]["floor"] = copy.deepcopy(data["floor"])
    return out


def of_type(events: list[tuple[str, dict[str, Any]]], *types: str) -> list[dict[str, Any]]:
    return [data for type_, data in events if type_ in types]


class Run:
    """Plays a game on the real world and checks the replay after every step."""

    def __init__(self) -> None:
        self.clock = ManualClock()
        self.spec = make_game_spec()
        self.world = World.new_game(self.spec, players(), self.clock, seed=11)
        self.world.set_running(True, "start")
        flush(self.world)
        self.start_seq = self.world.seq
        self.live: list[tuple[str, dict[str, Any]]] = []
        self.base = {c.id: channel_view(self.world, c) for c in self.world.state.channels}
        self.steps = 0
        self.replay: dict[str, Any] = {}

    def lead(self) -> str:
        return next(p.id for p in self.world.state.players if p.role == "lead")

    def last_request(self) -> str:
        return list(self.world.state.requests)[-1]

    def step(self, label: str) -> None:
        _, events = flush(self.world)
        self.live.extend((e.type, e.data) for e in events)
        self.replay = replay = build_replay(payload_of(self.world, self.spec))
        self.steps += 1
        projected = [(ev["type"], ev["data"]) for ev in replay["events"]]
        after = [(ev["type"], ev["data"]) for ev in replay["events"] if ev["seq"] > self.start_seq]

        # the events are in ledger order and carry the game clock
        seqs = [ev["seq"] for ev in replay["events"]]
        assert seqs == sorted(seqs), label
        gts = [ev["gt"] for ev in replay["events"]]
        assert gts == sorted(gts) and replay["duration_gt"] >= (gts[-1] if gts else 0), label

        assert of_type(after, "message.committed") == of_type(self.live, "message.committed"), f"{label}: messages"
        assert fold_channels(self.base, self.live) == fold_channels({}, projected), f"{label}: channels"
        for type_ in ("request.progress", "request.answer", "request.closed", "player.updated", "phase.changed",
                      "dashboard.updated", "ability.used", "challenge.opened", "challenge.resolved", "game.status"):
            assert of_type(after, type_) == of_type(self.live, type_), f"{label}: {type_}"
        # request.opened: the replay's view also carries ``answer_changes`` (the client counts them itself)
        opened = [{**d["request"]} for d in of_type(after, "request.opened")]
        for view in opened:
            view.pop("answer_changes")
        assert opened == [d["request"] for d in of_type(self.live, "request.opened")], f"{label}: request.opened"


def play(run: Run) -> None:
    w, clock = run.world, run.clock
    lead = run.lead()
    others = [p for p in IDS if p != lead]

    # ---- channels, phase, a floor of two turns ------------------------------------------------------
    ok(mod(w, "create_channel", channel_id="public", name="公共", members=IDS, mode="closed", type="public"))
    run.step("create public")
    ok(mod(w, "create_channel", channel_id="wolves", name="小队", members=["N1", "N2"], mode="free", type="group"))
    run.step("create group")
    ok(mod(w, "set_phase", text="第 1 天 · 发言"))
    run.step("phase")
    ok(mod(w, "announce", channel_id="public", template_id="notice", params={"text": "开始了"}))
    run.step("announce")
    ok(mod(w, "open_floor", channel_id="public", mode="turn", holders=["N1", "N2"], limits={"max_messages": 2, "duration_s": 30}))
    run.step("open turn")
    ok(speak(w, "N1", "public", "大家好"))
    run.step("N1 speaks")
    clock.advance(5)
    ok(speak(w, "N2", "public", "你好"))
    run.step("N2 speaks")
    ok(speak(w, "N1", "public", "再说一句"))
    run.step("N1 uses up its messages")
    ok(mod(w, "pause_queue", channel_id="public"))
    run.step("pause")
    clock.advance(10)
    ok(mod(w, "resume_queue", channel_id="public"))
    run.step("resume")
    ok(do_pass(w, "N2", "public"))
    run.step("N2 passes, the floor completes")

    # ---- a queue: a timeout moves it on; a freeze; the floor is taken back ---------------------------
    ok(mod(w, "set_floor_queue", channel_id="public", order=["N3", "N4", "N1"], limits={"duration_s": 20, "max_messages": 3}))
    run.step("open queue")
    ok(speak(w, "N3", "public", "轮到我"))
    run.step("N3 speaks in the queue")
    clock.advance(25)
    w.tick()
    run.step("N3 times out, N4 is next")
    ok(speak(w, "N4", "public", "我也说"))
    run.step("N4 speaks")
    assert w.chat.freeze_channel("public", "test")
    run.step("freeze")
    clock.advance(7)
    ok(mod(w, "resume_queue", channel_id="public"))
    run.step("resume after the freeze")
    ok(mod(w, "close_floor", channel_id="public"))
    run.step("close the floor")

    # ---- free discussion in the group; members, read-only, rename, mode, limits ------------------------
    ok(mod(w, "open_floor", channel_id="wolves", mode="free", holders=[], limits={"max_messages_per_member": 3, "duration_s": 60}))
    run.step("open free")
    ok(speak(w, "N1", "wolves", "小队频道"))
    run.step("N1 speaks in the group")
    clock.advance(3)
    ok(speak(w, "N2", "wolves", "收到"))
    run.step("N2 speaks in the group")
    ok(mod(w, "send_message", channel_id="wolves", content="主持人插一句"))
    run.step("the moderator speaks in the group")
    ok(mod(w, "update_channel", channel_id="wolves", add_members=["N3"], readonly_members=["N2"], name="小队二"))
    run.step("members / read-only / rename")
    ok(speak_template(w, "N3", "wolves", "count", n=3))
    run.step("template speech")
    ok(mod(w, "update_channel", channel_id="wolves", remove_members=["N2"]))
    run.step("remove a member")
    ok(mod(w, "update_channel", channel_id="wolves", mode="closed"))
    run.step("mode closed")
    ok(mod(w, "update_channel", channel_id="wolves", mode="free", limits={"max_messages_per_member": 5}))
    run.step("mode free again with limits")
    ok(mod(w, "update_channel", channel_id="wolves", limits={"max_chars_total": 150}))
    run.step("new limits")

    # ---- requests: sealed with a default (expires), shared + unanimous, text, cancel, reopen -------------
    ok(mod(w, "open_request", to=["N1", "N2", "N3"], kind="choice", question="选谁？", options=OPTIONS, duration_s=20, sealed=True, default="a"))
    first = run.last_request()
    run.step("open a sealed request")
    ok(submit(w, "N1", "respond", {"request_id": first, "value": "b"}))
    run.step("N1 answers")
    ok(submit(w, "N2", "respond", {"request_id": first, "value": "a", "reason": "觉得对"}))
    run.step("N2 answers")
    ok(submit(w, "N1", "respond", {"request_id": first, "value": "a"}))
    run.step("N1 changes his answer")
    clock.advance(25)
    w.tick()
    run.step("the sealed request expires, N3 gets the default")
    ok(mod(w, "open_request", to=["N1", "N2"], kind="choice", question="刀谁？", options=OPTIONS, duration_s=30, share_answers=True, auto_close="unanimous"))
    shared = run.last_request()
    run.step("open a shared request")
    ok(submit(w, "N1", "respond", {"request_id": shared, "value": "a"}))
    run.step("shared: N1")
    ok(submit(w, "N2", "respond", {"request_id": shared, "value": "b"}))
    run.step("shared: N2 disagrees")
    ok(submit(w, "N2", "respond", {"request_id": shared, "value": "a"}))
    run.step("shared: N2 agrees, the request closes")
    ok(mod(w, "open_request", to=["N3"], kind="text", question="说点什么", duration_s=10))
    text = run.last_request()
    run.step("open a text request")
    ok(submit(w, "N3", "respond", {"request_id": text, "value": "好的"}))
    run.step("N3 answers the text request")
    ok(mod(w, "open_request", to=["N1", "N4"], kind="choice", question="要取消的", options=OPTIONS, duration_s=10))
    cancelled = run.last_request()
    run.step("open a request to cancel")
    ok(submit(w, "N4", "respond", {"request_id": cancelled, "value": "b"}))
    run.step("N4 answers it")
    ok(mod(w, "cancel_request", request_id=cancelled))
    run.step("cancel")
    ok(mod(w, "reopen_request", request_id=cancelled, to=["N1"]))
    run.step("reopen")

    # ---- players, tickets, abilities, the dashboard, the end --------------------------------------------
    ok(mod(w, "update_player", player_id="N4", add_tags=["out"]))
    run.step("N4 is out")
    ok(mod(w, "update_player", player_id="N4", remove_tags=["out"]))
    run.step("N4 is back")
    ok(submit(w, "N2", "challenge", {"content": "我要质询第三条发言，理由是它越过了字数的上限，请主持人核实。"}))
    run.step("challenge")
    ticket = next(iter(w.state.tickets))
    ok(mod(w, "resolve_challenge", ticket_id=ticket, verdict="rejected", reply="驳回。"))
    run.step("resolve the challenge")
    ok(mod(w, "enable_ability", ability_id="flare"))
    run.step("enable the ability")
    ok(submit(w, lead, "use_ability", {"ability_id": "flare", "params": {}}))
    run.step("use the ability (it freezes the public channel)")
    ok(mod(w, "update_dashboard", factions=[{"name": "红", "alive": 1, "total": 1, "variant": "danger"}], tag_stats=[]))
    run.step("dashboard")
    ok(mod(w, "end_game", winner="红", summary="领队获胜"))
    run.step("end")
    assert others  # (the script needs three other players)


def test_every_step_of_a_scripted_game_projects_to_what_the_live_stream_sent() -> None:
    run = Run()
    play(run)
    types = Counter(t for t, _ in run.live)
    # the script really exercised every kind of event the projector has to reproduce
    for needed in (
        "message.committed", "channel.created", "channel.updated", "floor.changed", "request.opened", "request.progress",
        "request.answer", "request.closed", "player.updated", "phase.changed", "dashboard.updated", "ability.used",
        "challenge.opened", "challenge.resolved", "game.status",
    ):
        assert types[needed] > 0, needed
    assert run.steps >= 50


def test_the_final_channel_views_equal_the_worlds_own_up_to_the_ticking_clock() -> None:
    run = Run()
    play(run)
    world = run.world
    projected = fold_channels({}, [(ev["type"], ev["data"]) for ev in run.replay["events"]])
    assert sorted(projected) == sorted(c.id for c in world.state.channels)

    def timeless(view: dict[str, Any]) -> dict[str, Any]:
        view = copy.deepcopy(view)
        view["floor"]["seconds_left"] = None
        free = view["floor"].get("free")
        if free is not None:
            for key in ("elapsed_s", "silent_s"):
                free.pop(key)
        return view

    for channel in world.state.channels:
        assert timeless(projected[channel.id]) == timeless(channel_view(world, channel)), channel.id


def test_the_replay_has_the_documented_shape_and_is_plain_json() -> None:
    run = Run()
    play(run)
    replay = run.replay
    assert replay["version"] == REPLAY_VERSION
    assert set(replay) == {"version", "game", "initial", "events", "duration_gt"}
    assert replay["game"]["session_id"] == "replay-test" and replay["game"]["winner"] == "红" and replay["game"]["human_involved"] is False
    assert all(set(ev) == {"seq", "gt", "type", "data"} for ev in replay["events"])
    assert replay["duration_gt"] == run.world.ledger[-1].gt > 0
    # the first events open the system DMs and seat the identities: the session before them is empty
    initial = replay["initial"]
    assert initial["status"] == "idle" and initial["channels"] == [] and initial["messages"] == [] and initial["requests"] == []
    assert [p["id"] for p in initial["participants"]] == IDS and all(p["tags"] == [] and p["out"] is False for p in initial["participants"])
    assert [ev["type"] for ev in replay["events"][:8]] == ["channel.created"] * 4 + ["player.updated"] * 4
    assert json.loads(json.dumps(replay)) == replay  # plain JSON all the way


def test_a_whole_game_projects_quickly() -> None:
    run = Run()
    play(run)
    payload = payload_of(run.world, run.spec)
    started = time.perf_counter()
    for _ in range(20):
        build_replay(payload)
    assert (time.perf_counter() - started) / 20 < 0.2


def test_the_replay_of_a_game_without_a_ledger_is_empty_not_an_error() -> None:
    world = World.new_game(make_game_spec(), players(), ManualClock(), seed=3)
    payload = payload_of(world, make_game_spec())
    payload["world"]["ledger"] = []
    replay = build_replay(payload)
    assert replay["events"] == [] and replay["duration_gt"] == 0 and replay["initial"]["status"] == "idle"
