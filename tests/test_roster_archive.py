"""The v0.13.1 memory and persona in an ``.arenaagents`` archive (contract 12.8, memory).

The archive carries the roster files as opaque text (``roster/N<k>.persona.md`` / ``.memory.md``), so a memory of the new
format comes back byte for byte, and one written by v0.13.0 is simply read - migrated - after the import: the archive needs no
migration of its own (G41).  Real stores, real orchestrator; nothing is faked except that no game is played."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.archive import jobs as jobs_module
from app.archive.export import ExportOptions
from app.archive.jobs import ArchiveJob, ArchiveManager
from app.archive.ports import OrchestratorAdapter
from app.game.orchestrator import GameOrchestrator
from app.storage import reset_stores_for_tests
from app.storage.roster import RosterStore
from game_fakes import async_test

PERSONA = "【定位】冷静的逻辑派，喜欢用票型说话。\n【说话风格】\n- 句子短，先结论后理由。\n【口头禅】我们看票。"
V0130 = "## 战绩\n场次 3，胜 2，负 1，胜率 67%\n- 狼人：2 场，胜 1\n\n## 对局记忆\n第一局：我太保守。\n\n## 经验与印象\nBob 爱撒谎。\n"


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


def roster_texts(data: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted((data / "roster").glob("N*.md"))}


async def export_then_import(orch: GameOrchestrator, data: Path, between) -> None:
    """Export the data, let ``between()`` change it, import the archive again."""
    manager = ArchiveManager(engine=OrchestratorAdapter(orch))
    exported = Path((await finished(await manager.start_export(ExportOptions()))).result["path"])  # type: ignore[index]
    between()
    session = await manager.receive_upload(pieces(exported.read_bytes()), name="mine.arenaagents", max_bytes=10**9)
    job = await finished(await manager.start_apply(session.id, password=None, skip_keys=True))
    assert job.status == "done", job.message


@async_test
async def test_a_memory_of_the_new_format_and_a_persona_come_back_from_an_archive_byte_for_byte(data: Path) -> None:
    orch = await booted()
    store = RosterStore(data)
    store.apply_review("N3", role="狼人", won=True, impressions={"N1": "发言短，站边快。", "N5": "拿到狼牌时爱悍跳。"}, experience=["先听后说。"])
    store.apply_review("N3", role="平民", won=False, impressions={"N1": "改观了：其实很可靠。"})
    store.set_memory_parts("N7", impressions={"N2": "印象里有冒号：和 N5 的 “引号”"}, experience=["第一条", "第二条"])
    store.set_persona("N3", PERSONA)
    original = roster_texts(data)
    assert set(original) == {"N3.memory.md", "N3.persona.md", "N7.memory.md"}
    assert original["N3.memory.md"].decode("utf-8").startswith("## 战绩\n场次 2，胜 1，负 1，胜率 50%")
    await orch.persist_session("t")

    def change_everything() -> None:
        store.clear(include_persona=True)
        store.set_memory_parts("N3", experience=["完全不同的经验"])
        store.set_persona("N4", "后来写的性格")
        assert roster_texts(data) != original

    await export_then_import(orch, data, change_everything)
    assert roster_texts(data) == original  # the files are as they were, byte for byte
    entry = RosterStore(data).entry("N3")
    assert entry["persona"] == PERSONA and entry["memory_parts"]["impressions"]["N1"] == "改观了：其实很可靠。"
    assert entry["memory_parts"]["impressions"]["N5"] == "拿到狼牌时爱悍跳。" and entry["memory_parts"]["experience"] == ["先听后说。"]
    assert orch.roster_payload()["seats"][2]["memory_parts"] == entry["memory_parts"]  # the running application reads the restored files
    await orch.shutdown()


@async_test
async def test_a_v0130_memory_in_an_archive_is_read_migrated_after_the_import_and_not_touched_by_it(data: Path) -> None:
    orch = await booted()
    (data / "roster" / "N2.memory.md").write_bytes(V0130.encode("utf-8"))
    await orch.persist_session("t")
    await export_then_import(orch, data, lambda: RosterStore(data).clear())
    assert (data / "roster" / "N2.memory.md").read_bytes() == V0130.encode("utf-8")  # the archive does not rewrite what it carries
    parts = orch.roster_payload()["seats"][1]["memory_parts"]  # ... it is migrated when it is read
    assert parts["record"] == "场次 3，胜 2，负 1，胜率 67%\n- 狼人：2 场，胜 1"
    assert parts["legacy"] == "第一局：我太保守。\n\nBob 爱撒谎。" and parts["experience"] == [] and not any(parts["impressions"].values())
    # and the game that is created from it carries the old notes until a review tidies them up
    await orch.new_game()
    assert "场次 3，胜 2" in orch.session.participants[1].memory and "Bob 爱撒谎。" in orch.session.participants[1].memory
    await orch.shutdown()
