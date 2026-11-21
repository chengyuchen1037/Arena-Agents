"""A v0.13 archive (format 1) in v0.14: its roster becomes bots when it is imported (docs/v0.14-plan.md H45, H59; contract 13.9).

Until v0.13.1 an archive carried the roster files as opaque text and they came back byte for byte.  The roster is gone in v0.14: the ``roster/**`` of a
format 1 archive is unpacked as it came, and once ``settings.json`` and ``roster/`` have landed it is handed to ``BotStore.import_legacy_roster`` - the
names and avatars, the persona (L0), the impressions (by name), the experiences (low-confidence playbook entries) and the record become bots, nothing is
initialized and no model is called.  A memory written by v0.13.0 (``## 对局记忆`` / ``## 经验与印象``) keeps its record; its free text is not migrated.

Real stores, real orchestrator; nothing is faked except that no game is played.  The old roster files are written by hand (the format of
v0.13.1 / v0.13.0), so this does not depend on the code that wrote them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.archive import jobs as jobs_module
from app.archive.jobs import ArchiveJob, ArchiveManager
from app.archive.ports import OrchestratorAdapter
from app.game.orchestrator import GameOrchestrator
from app.storage import botfiles as bf
from app.storage import reset_stores_for_tests
from app.storage.bots import get_bot_store, legacy_persona_to_core
from app.storage.rules import default_scenario_id
from game_fakes import async_test
from tests.archive.helpers import write_zip

PERSONA = "【定位】冷静的逻辑派，喜欢用票型说话。\n【说话风格】\n- 句子短，先结论后理由。\n【口头禅】我们看票。"
#: a memory file as v0.13.1 wrote it: the record, one impression per other seat (by seat id and name), the lessons
MEMORY = (
    "## 战绩\n场次 2，胜 1，负 1，胜率 50%\n- 狼人：1 场，胜 1\n- 平民：1 场，胜 0\n\n"
    "## 印象\n- N1 Alice：发言短，站边快。\n- N5 Eve：拿到狼牌时爱悍跳。\n\n"
    "## 经验\n- 先听后说。\n- 票型比发言可靠。\n"
)
#: ... and as v0.13.0 wrote it
V0130 = "## 战绩\n场次 3，胜 2，负 1，胜率 67%\n- 狼人：2 场，胜 1\n\n## 对局记忆\n第一局：我太保守。\n\n## 经验与印象\nBob 爱撒谎。\n"
ROSTER = (
    b'{"memory_enabled": true, "seats": {"N1": {"name": "Alice"}, "N2": {"name": "Bob"}, "N3": {"name": "Carol"}, '
    b'"N5": {"name": "Eve"}, "N7": {"name": "Grace"}}}'
)


async def finished(job: ArchiveJob, timeout: float = 60.0) -> ArchiveJob:
    deadline = time.monotonic() + timeout
    while job.status == "running":
        assert time.monotonic() < deadline, f"stuck in {job.phase}"
        await asyncio.sleep(0.01)
    return job


async def pieces(data: bytes, size: int = 100_000) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]
        await asyncio.sleep(0)


async def booted() -> GameOrchestrator:
    orch = GameOrchestrator()
    report = await orch.startup()
    assert report["recovery_error"] is None
    orch._autosave_delay = 0.02
    return orch


@pytest.fixture()
def data(isolated_external_storage: Path) -> Path:
    reset_stores_for_tests()
    yield isolated_external_storage
    reset_stores_for_tests()
    jobs_module.reset_archive_manager()


async def import_roster(orch: GameOrchestrator, data: Path, **files: bytes) -> None:
    """Make a format 1 archive whose roster is ``roster.json`` and ``files`` (name -> bytes) and import it through the manager."""
    members = {"roster/roster.json": ROSTER, **{f"roster/{name}": content for name, content in files.items()}}
    archive = write_zip(data.parent / "v013.arenaagents", members, version=1)
    manager = ArchiveManager(engine=OrchestratorAdapter(orch))
    session = await manager.receive_upload(pieces(archive.read_bytes()), name="v013.arenaagents", max_bytes=10**9)
    assert session.summary.format_version == 1 and session.summary.roster_seats == 5 and session.summary.bots is None
    job = await finished(await manager.start_apply(session.id, password=None, skip_keys=True))
    assert job.status == "done", job.message


@async_test
async def test_the_memory_and_the_persona_of_a_v013_archive_become_bots(data: Path) -> None:
    orch = await booted()
    await orch.persist_session("t")
    await import_roster(orch, data, **{"N3.persona.md": PERSONA.encode("utf-8"), "N3.memory.md": MEMORY.encode("utf-8")})

    store = get_bot_store()
    lineup = store.lineup()
    assert len(lineup) == 12 and store.check_integrity() == []
    assert [store.name_of(bot_id) for bot_id in lineup][:7] == ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace"]
    carol = lineup[2]
    core = store.core(carol)
    assert core == legacy_persona_to_core(PERSONA), "the persona is the bot's L0"
    assert "冷静的逻辑派" in core.identity and "我们看票" in core.voice
    assert core.mind == "" and not core.complete, "(this persona has no way of thinking: the start check will ask for one - nothing is made up)"
    assert bf.parse_people(store.read_part(carol, "people")) == {"Alice": "发言短，站边快。", "Eve": "拿到狼牌时爱悍跳。"}, "the impressions, by name"
    entries = bf.parse_playbook(store.read_part(carol, "playbook", default_scenario_id()))
    assert [(entry.text, entry.confidence) for entry in entries] == [("先听后说。", "低"), ("票型比发言可靠。", "低")], "the experiences, the lowest confidence"
    record = bf.read_record(store.bot_dir(carol))
    assert (record.games, record.wins) == (2, 1)
    assert store.summary(carol)["init"]["status"] == "uninitialized" and store.is_frozen(carol), "nothing is initialized; a bot with a memory is frozen"
    assert (data / "roster" / "N3.memory.md").read_bytes() == MEMORY.encode("utf-8"), "the roster files themselves are not rewritten"
    assert orch.bots_payload()["bots"][2]["name"] == "Carol", "the running application reads the new table"
    await orch.new_game()
    assert [p.name for p in orch.session.participants][:3] == ["Alice", "Bob", "Carol"]
    await orch.shutdown()


@async_test
async def test_a_v0130_memory_in_an_archive_keeps_its_record_and_the_free_text_is_left_where_it_was(data: Path) -> None:
    orch = await booted()
    await orch.persist_session("t")
    await import_roster(orch, data, **{"N2.memory.md": V0130.encode("utf-8")})

    store = get_bot_store()
    bob = store.lineup()[1]
    assert store.name_of(bob) == "Bob"
    record = bf.read_record(store.bot_dir(bob))
    assert (record.games, record.wins) == (3, 2) and record.by_role == {"狼人": [2, 1]}, "the record is kept"
    assert store.read_part(bob, "people") == "" and store.read_part(bob, "playbook", default_scenario_id()) == "", (
        "what v0.13.0 wrote as free text is not migrated (nothing in it says who it is about or how sure it is)"
    )
    assert (data / "roster" / "N2.memory.md").read_bytes() == V0130.encode("utf-8"), "the archive does not rewrite what it carries"
    await orch.shutdown()


@async_test
async def test_an_archive_without_a_memory_gives_plain_bots_with_the_names_of_its_roster(data: Path) -> None:
    orch = await booted()
    await orch.persist_session("t")
    await import_roster(orch, data)
    store = get_bot_store()
    assert [store.name_of(bot_id) for bot_id in store.lineup()][:5] == ["Alice", "Bob", "Carol", "David", "Eve"]
    for bot_id in store.lineup():
        assert store.read_part(bot_id, "people") == "" and bf.read_record(store.bot_dir(bot_id)).games == 0
    assert store.check_integrity() == []
    await orch.shutdown()
