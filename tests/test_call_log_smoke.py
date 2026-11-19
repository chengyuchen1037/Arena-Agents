"""``tools/real_smoke.py`` and the trace after v0.13.1 (review of the observe stream).

The trace's ``llm_call`` rows used to be one per SUCCESSFUL turn of an agent.  Since v0.13.1 it also holds a row for every
attempt that failed or was cancelled (``status``) and for the one-shot calls outside the game loop (``purpose`` = ``review`` /
``refine``).  The smoke test's "prompt cache: x/y calls after an agent's first one reported cached input" counts the first
kind only: a failed call reports no tokens, a review or a "complete it" starts a conversation of its own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace as ns
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import real_smoke  # noqa: E402


@pytest.mark.parametrize(
    "row, expected",
    [
        ({"event": "llm_call", "agent_id": "N1", "cache_read_tokens": 5}, True),  # what an agent writes for a success (older traces too)
        ({"event": "llm_call", "agent_id": "N1", "status": "ok", "purpose": "turn"}, True),
        ({"event": "llm_call", "status": None, "purpose": None}, True),
        ({"event": "llm_call", "status": "retry", "purpose": "turn"}, False),
        ({"event": "llm_call", "status": "timeout", "purpose": "turn"}, False),
        ({"event": "llm_call", "status": "error", "purpose": "turn"}, False),
        ({"event": "llm_call", "status": "cancelled", "purpose": "turn"}, False),
        ({"event": "llm_call", "status": "ok", "purpose": "review"}, False),  # a player's own review of the game
        ({"event": "llm_call", "status": "ok", "purpose": "refine"}, False),  # the moderator's model completing a text
        ({"event": "llm_call", "agent_id": "roster", "purpose": "refine"}, False),
        ({"event": "llm_call", "status": ["ok"]}, False),  # (a hand-made row: not a success)
        ({"event": "llm_call", "purpose": {"x": 1}}, False),
    ],
)
def test_only_the_successful_turns_of_a_player_or_the_moderator_count(row, expected):
    assert real_smoke.is_successful_turn(row) is expected


def stub_orch(session_id: str) -> Any:
    usage = ns(
        prompt_tokens=1, cache_read_tokens=2, cache_write_tokens=0, completion_tokens=3, total_tokens=6, cache_hit_rate=0.5,
        total_cost_usd=0.0, by_actor={},
    )
    world = ns(seq=0, state=ns(winner=None, phase="p"), ledger=[], display_name=lambda actor: actor)
    return ns(world=world, usage=usage, session=ns(status="paused", session_id=session_id))


def write_trace(root: Path, session_id: str, rows: list[dict[str, Any]]) -> None:
    directory = root / "logs" / session_id
    directory.mkdir(parents=True)
    (directory / "trace-0001.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def turn(agent: str, cached: int) -> dict[str, Any]:
    """The row an agent writes for a call that succeeded."""
    return {"event": "llm_call", "agent_id": agent, "gen": 1, "round": 0, "prompt_tokens": 100, "cache_read_tokens": cached, "tool_calls": ["speak"]}


def other(agent: str, status: str, purpose: str = "turn", **fields: Any) -> dict[str, Any]:
    """The row the call log writes for a failed / cancelled attempt or a one-shot call."""
    return {"event": "llm_call", "agent_id": agent, "status": status, "purpose": purpose, "attempt": 1, **fields}


def test_the_prompt_cache_line_of_the_report_counts_the_successful_turns_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARENA_DATA_DIR", str(tmp_path))
    write_trace(
        tmp_path,
        "game1",
        [
            turn("N1", 0),  # the first call of N1: not counted (nothing to hit yet)
            other("N1", "retry", http_status=429),
            other("N1", "error", http_status=500),
            turn("N1", 900),  # N1's second turn: cached input reported
            other("N1", "cancelled"),
            other("N1", "timeout"),
            other("N1", "ok", purpose="review", prompt_tokens=50, cache_read_tokens=0),
            turn("N2", 0),
            turn("N2", 0),  # N2's second turn: nothing cached
            other("N2", "error"),
            other("roster", "ok", purpose="refine", prompt_tokens=10),  # a "complete it" call
            turn("moderator", 0),
            turn("moderator", 4000),  # the moderator's second turn: cached
            {"event": "error", "agent_id": "N1", "message": "RuntimeError: x"},
        ],
    )
    real_smoke.report(stub_orch("game1"), {"n": 14, "errors": {}}, 12.0)
    output = capsys.readouterr().out
    # 3 turns after an agent's first one (N1, N2 and the moderator once each); two of them reported cached input
    assert "prompt cache: 2/3 calls after an agent's first one reported cached input (67%)" in output
    assert "runtime errors:" in output  # (the other trace events are reported as before)


def test_without_any_failed_or_one_shot_row_the_report_is_what_it_was(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARENA_DATA_DIR", str(tmp_path))
    write_trace(tmp_path, "game2", [turn("N1", 0), turn("N1", 500), turn("N1", 600), turn("N2", 0), turn("N2", 0)])
    real_smoke.report(stub_orch("game2"), {"n": 5, "errors": {}}, 1.0)
    assert "prompt cache: 2/3 calls after an agent's first one reported cached input (67%)" in capsys.readouterr().out


def test_a_trace_with_only_failed_attempts_has_no_cache_line_at_all(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARENA_DATA_DIR", str(tmp_path))
    write_trace(tmp_path, "game3", [other("N1", "error"), other("N1", "error"), other("N1", "cancelled"), other("N2", "ok", purpose="review")])
    real_smoke.report(stub_orch("game3"), {"n": 4, "errors": {"RuntimeError": 2}}, 1.0)
    assert "prompt cache:" not in capsys.readouterr().out  # nothing a conversation could have cached
