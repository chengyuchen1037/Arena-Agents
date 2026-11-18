"""A whole scripted game, replayed through the REAL client store (static/js/store.js), ends in the session the
server reports for it.  This is the end-to-end contract of the replay (``app/game/replay.py``): the same events
a live spectator received, rebuilt from the saved ledger alone.  Skipped when node is missing."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import app.game.orchestrator as orchestrator_module
from app.config import load_config
from app.game.demo import install_demo
from app.game.orchestrator import GameOrchestrator
from app.game.replay import build_replay

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "tests" / "frontend" / "replay_projection_check.js"
_WINDOWS_NODE = Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"


def _find_node() -> str | None:
    env = os.environ.get("NODE_BIN")
    if env and Path(env).exists():
        return env
    return shutil.which("node") or (str(_WINDOWS_NODE) if _WINDOWS_NODE.exists() else None)


NODE = _find_node()


def _play_demo_game(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the saved game, the session the server reports) of a finished scripted werewolf game."""
    cfg = load_config()
    cfg["runtime"] = {**cfg["runtime"], "tick_interval": 0.05, "watchdog": 60}
    monkeypatch.setattr(orchestrator_module, "load_config", lambda *a, **k: copy.deepcopy(cfg))

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        orch = GameOrchestrator()
        install_demo(orch, delay=0.0, seed=7)
        await orch.start_or_resume()
        deadline = asyncio.get_running_loop().time() + 150
        while orch.session.status not in ("finished", "paused"):
            assert asyncio.get_running_loop().time() < deadline, "the demo game did not finish in time"
            await asyncio.sleep(0.1)
        assert orch.session.status == "finished"
        payload = json.loads(json.dumps(orch.session_payload()))
        final = json.loads(json.dumps(orch.public_session()))
        await orch.shutdown()
        return payload, final

    return asyncio.run(scenario())


def test_the_replay_of_a_whole_werewolf_game_rebuilds_the_servers_final_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload, final = _play_demo_game(monkeypatch)
    replay = build_replay(payload)

    assert replay["game"]["winner"] == payload["world"]["state"]["winner"] and replay["game"]["status"] == "finished"
    assert len(replay["events"]) > 100 and replay["duration_gt"] > 0
    types = {ev["type"] for ev in replay["events"]}
    assert {"message.committed", "channel.created", "floor.changed", "request.opened", "request.closed", "player.updated"} <= types

    if NODE is None:
        pytest.skip("node executable not found")
    events = [{"id": i, "gt": ev["gt"], "type": ev["type"], "data": ev["data"]} for i, ev in enumerate(replay["events"], start=1)]
    check = tmp_path / "replay-check.json"
    check.write_text(json.dumps({"initial": replay["initial"], "events": events, "final": final}, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run([NODE, str(CHECK), str(check)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
