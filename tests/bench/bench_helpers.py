"""Shared pieces of the bench tests: bots, situations, one call of the runner with a fake model.

Everything a test writes goes to pytest's ``tmp_path`` (``--basetemp``): nothing is made with ``tempfile`` (in some sandboxes the system's temporary
directory is the repository root) and nothing is left behind."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from app.runtime.clock import ManualClock
from app.storage import botfiles
from app.world.types import Intent
from app.world.world import World
from bench.bots import BenchBot, make_bots
from bench.fakes import FakeLlm, Policy, policy_named
from bench.play import Scene, intent_type, load_spec
from bench.runner import Job, RunConfig, RunReport, run_suite
from bench.situation import Situation, load_situations

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "bench" / "fixtures" / "situations"
SCENARIO = "minimal_test"
NAMES = ["Alice", "Bob", "Carol", "David"]
LIBRARY_VOICE = "你说话短。\n允许口头列举\n篇幅：30–90 字"


def examples() -> list[Situation]:
    """The two shipped examples: a vote (``respond``) and a turn to speak."""
    return load_situations(FIXTURES / "_example.yaml") + load_situations(FIXTURES / "_example_speak.yaml")


def example(name: str) -> Situation:
    return next(s for s in examples() if s.id == f"example-{name}")


def bots(*specs: str) -> list[BenchBot]:
    return make_bots(list(specs), scenario_id=SCENARIO)


def run_fake(
    situations: list[Situation],
    bot_list: list[BenchBot],
    policy: Policy | str = "prose",
    *,
    tmp_path: Path | None = None,
    samples: int = 1,
    **config: Any,
) -> tuple[RunReport, list[tuple[Job, Scene, FakeLlm]]]:
    """Run the suite with the fake model ``policy``; also returns what every sample's model port saw (job, scene, llm)."""
    chosen = policy_named(policy) if isinstance(policy, str) else policy
    seen: list[tuple[Job, Scene, FakeLlm]] = []

    def factory(job: Job, scene: Scene) -> FakeLlm:
        llm = FakeLlm(chosen, world=scene.world, seat=scene.seat, bot=job.bot.label, situation=job.situation.id, sample=job.sample)
        seen.append((job, scene, llm))
        return llm

    cfg = RunConfig(samples=samples, run_id=config.pop("run_id", "t"), **config)
    report = asyncio.run(run_suite(situations, bot_list, cfg, factory, out_dir=tmp_path))
    return report, seen


def rows_of(report: RunReport, *, bot: str | None = None, situation: str | None = None) -> list[dict[str, Any]]:
    return [r for r in report.rows if (bot is None or r["bot"] == bot) and (situation is None or r["situation"] == f"example-{situation}" or r["situation"] == situation)]


def one(report: RunReport, bot: str, situation: str) -> dict[str, Any]:
    found = rows_of(report, bot=bot, situation=situation)
    assert len(found) == 1, [(r["bot"], r["situation"]) for r in report.rows]
    return found[0]


def scene_of(seen: list[tuple[Job, Scene, FakeLlm]], bot: str, situation: str) -> Scene:
    return next(scene for job, scene, _llm in seen if job.bot.label == bot and job.situation.id == f"example-{situation}")


def spoken_by(scene: Scene) -> list[str]:
    """What the seat under test said in the world, in order (``message`` entries it wrote)."""
    return [str(e.data.get("content")) for e in scene.world.ledger if e.type == "message" and e.actor_id == scene.seat]


# ------------------------------------------------------------------------------------------------ a bot library
def make_library(root: Path) -> Path:
    """A library of two bots: Zed has everything (L0, notes, digest, playbook, questions, people, a record), Yan only an L0."""
    zed = root / "Zed"
    (zed / "core").mkdir(parents=True)
    (zed / "bot.json").write_text(json.dumps({"format": 1, "name": "Zed"}), encoding="utf-8")
    (zed / "core" / "identity.md").write_text("一个爱列点的老玩家。", encoding="utf-8")
    (zed / "core" / "voice.md").write_text(LIBRARY_VOICE, encoding="utf-8")
    (zed / "core" / "mind.md").write_text("先看事实。", encoding="utf-8")
    (zed / "arch").mkdir()
    (zed / "arch" / "notes.md").write_text("我的操作笔记：发言前先看状态板。", encoding="utf-8")
    rules = zed / "rules" / SCENARIO
    rules.mkdir(parents=True)
    (rules / "digest.md").write_text("我理解这个游戏要看事实。", encoding="utf-8")
    (rules / "playbook.md").write_text("## 假设\n- [把握:高|支持:3|反例:0|来源:复盘|#发言] 先看规则给出的结果。\n- [把握:低|支持:0|反例:0|来源:初始化] 不太确定的想法。\n", encoding="utf-8")
    (rules / "questions.md").write_text("平局怎么处理还不确定。", encoding="utf-8")
    (zed / "people.md").write_text("- Alice：话少，爱反问\n- Bob：话多\n", encoding="utf-8")
    botfiles.add_record(zed, "红队", True)
    yan = root / "Yan"
    (yan / "core").mkdir(parents=True)
    (yan / "core" / "identity.md").write_text("一个新手。", encoding="utf-8")
    (yan / "core" / "voice.md").write_text("你说话慢。篇幅：20–60 字", encoding="utf-8")
    (yan / "core" / "mind.md").write_text("你多听少说。", encoding="utf-8")
    return root


# ------------------------------------------------------------------------------------------------ a game played on the engine
class Game:
    """A game played on the real engine, step by step (the way an orchestrator's agents would), and saved in the shape of ``game.json``."""

    def __init__(self, *, seed: int = 21, human: list[tuple[str, int, int | None]] | None = None) -> None:
        self.spec = load_spec(SCENARIO)
        self.players = [{"id": f"N{i}", "seat": str(i), "name": n, "persona": ""} for i, n in enumerate(NAMES, start=1)]
        self.clock = ManualClock(0.0)
        self.world = World.new_game(self.spec, self.players, self.clock, seed=seed, deal=False)
        self.world.deal_identities()
        self.world.set_running(True, "started")
        self.human = human or []

    def do(self, actor: str, tool: str, at: float | None = None, **args: Any) -> Any:
        if at is not None:
            self.clock.set(at)
            self.world.tick()
        receipt = self.world.submit(Intent(actor_id=actor, based_on_seq=self.world.seq, type=intent_type(actor, tool), payload=args))
        assert receipt.ok, (tool, args, receipt)
        return receipt

    def mod(self, tool: str, at: float | None = None, **args: Any) -> Any:
        return self.do("moderator", tool, at, **args)

    def tick(self, at: float) -> None:
        self.clock.set(at)
        self.world.tick()

    def saved(self) -> dict[str, Any]:
        return {
            "session_id": "test-game", "version": "v0.13.1", "scenario_name": SCENARIO, "participants": copy.deepcopy(self.players),
            "scenario_spec": self.spec.model_dump(mode="json"), "world": json.loads(json.dumps(self.world.dump(), default=str)),
            "human_spans": [{"seat": s, "from_seq": a, "to_seq": b} for s, a, b in self.human],
        }


def play_long_game() -> dict[str, Any]:
    """Every kind of action the converter knows, in one small game."""
    g = Game()
    ids = ["N1", "N2", "N3", "N4"]
    g.mod("create_channel", 1, channel_id="public", name="公共频道", type="public", members=ids)
    g.mod("create_channel", channel_id="team", name="红队", type="group", members=["N1", "N2"], mode="free")
    g.mod("set_phase", text="第 1 轮 · 发言")
    g.mod("update_dashboard", factions=[{"name": "红队", "alive": 2, "total": 2, "variant": "danger"}], tag_stats=[{"name": "存活", "count": 4}])
    g.mod("send_message", channel_id="team", content="你们是红队。")
    g.mod("set_floor_queue", 2, channel_id="public", order=ids, limits={"max_messages": 1, "duration_s": 30, "max_chars_per_message": 200})
    g.do("N1", "speak", 3, channel_id="public", content="我先说两句：今天没有什么特别的怀疑。")
    g.do("N2", "speak", 6, channel_id="public", content="我听听后面的人怎么说。")
    g.do("N3", "speak_template", 9, channel_id="public", template_id="say_vote", params={"target": "N1"})
    g.tick(45)  # N4 never speaks: the floor times out and the queue completes
    g.mod("set_phase", text="第 1 轮 · 投票")
    g.mod("open_request", 46, to=ids, kind="choice", question="请投票", sealed=True, duration_s=20, default="N1",
          options=[{"id": i, "label": i} for i in ids])
    g.do("N1", "respond", 48, request_id="r1", value="N2", reason="先投一个")
    g.do("N2", "respond", 49, request_id="r1", value="N1")
    g.tick(70)  # N3 and N4 default to N1
    g.mod("announce", channel_id="public", template_id="out_notice", params={"players": ["N1"]})
    g.mod("update_player", player_id="N1", add_tags=["出局"], remove_tags=["存活"], set_attributes={"note": "x"})
    red = next(p.id for p in g.world.state.players if p.role == "red" and "出局" not in p.tags)  # (whoever the seed dealt the red team to)
    g.mod("enable_ability", ability_id="shout")
    g.do(red, "use_ability", 80, ability_id="shout", params={"declaration": "红队万岁"})
    g.mod("disable_ability", ability_id="shout")
    g.mod("update_channel", channel_id="team", add_members=["N3"], readonly_members=["N3"], name="红队（旁听）")
    g.do("N3", "challenge_moderator", 90, content="第 1 轮的投票规则有误。", ref_seq=10)
    g.mod("resolve_challenge", ticket_id="t1", verdict="upheld", reply="已更正。")
    g.mod("random", kind="int", lo=1, hi=6)
    g.mod("random", kind="choice", items=["N2", "N3", "N4"], k=2)
    g.mod("random", kind="shuffle", items=["N2", "N3", "N4"])
    g.mod("open_request", 100, to=["N3"], kind="text", question="说说你的想法", duration_s=30)
    g.mod("cancel_request", request_id="r2")
    g.mod("reopen_request", request_id="r2", to=["N3"], duration_s=15)
    g.do("N3", "respond", 104, request_id="r3", value="我没有意见。")
    g.mod("open_floor", 105, channel_id="public", mode="free", holders=["N2", "N3"], limits={"duration_s": 30, "max_messages_per_member": 2, "max_chars_per_message": 100})
    g.do("N2", "speak", 106, channel_id="public", content="自由讨论里的一句话。")
    g.mod("close_floor", channel_id="public")
    g.mod("update_channel", channel_id="public", mode="listen")
    g.mod("hold_game", 110, reason="等人工检查")
    g.world.set_running(True, "resumed")
    g.mod("end_game", 120, winner="红队", summary="一局结束")
    return g.saved()
