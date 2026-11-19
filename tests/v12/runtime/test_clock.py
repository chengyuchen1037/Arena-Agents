from __future__ import annotations

import pytest

from app.runtime.clock import GameClock, ManualClock


class WallClock:
    """A hand-driven stand-in for ``time.monotonic``."""

    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_game_clock_starts_paused_and_frozen():
    wall = WallClock()
    clock = GameClock(5.0, time_source=wall)
    assert clock.paused is True
    assert clock.now() == 5.0
    wall.t += 50
    assert clock.now() == 5.0  # a new game does not run until it is resumed


def test_game_clock_runs_after_resume_and_stops_on_pause():
    wall = WallClock()
    clock = GameClock(0.0, time_source=wall)
    clock.resume()
    assert clock.paused is False
    wall.t += 3
    assert clock.now() == pytest.approx(3.0)
    clock.pause()
    wall.t += 100  # paused: game time does not move (D18)
    assert clock.now() == pytest.approx(3.0)
    clock.resume()
    wall.t += 2
    assert clock.now() == pytest.approx(5.0)


def test_pause_and_resume_are_idempotent():
    wall = WallClock()
    clock = GameClock(time_source=wall, paused=False)
    wall.t += 1
    clock.resume()  # already running: must not re-anchor and lose the elapsed second
    assert clock.now() == pytest.approx(1.0)
    clock.pause()
    clock.pause()
    wall.t += 9
    assert clock.now() == pytest.approx(1.0)


def test_a_soft_pause_followed_by_a_hard_pause_freezes_once_and_one_resume_runs_the_clock_again():
    # v0.13.1: the click of a soft pause freezes the clock, the hard pause that may follow freezes it "again"
    # (the orchestrator's ``_halt``), and the continue after that resumes it - both pairs may be doubled up
    wall = WallClock()
    clock = GameClock(0.0, time_source=wall, paused=False)
    wall.t += 2
    clock.pause()  # the soft pause
    wall.t += 30
    clock.pause()  # the hard pause on top of it
    wall.t += 30
    assert clock.now() == pytest.approx(2.0) and clock.paused
    clock.resume()
    clock.resume()  # (a soft resume followed by the start of the runtime)
    wall.t += 1
    assert clock.now() == pytest.approx(3.0) and not clock.paused


def test_manual_clock_pause_and_resume_are_idempotent_too():
    clock = ManualClock()
    clock.advance(5)
    clock.pause()
    clock.pause()
    clock.advance(50)
    assert clock.now() == pytest.approx(5.0)
    clock.resume()
    clock.resume()
    clock.advance(1)
    assert clock.now() == pytest.approx(6.0)


def test_set_jumps_and_keeps_the_paused_state():
    wall = WallClock()
    clock = GameClock(time_source=wall)  # paused
    clock.set(42.5)
    assert clock.now() == 42.5 and clock.paused
    clock.resume()
    wall.t += 1.5
    assert clock.now() == pytest.approx(44.0)
    clock.set(10.0)  # running: continues from the new value
    wall.t += 1.0
    assert clock.now() == pytest.approx(11.0)


def test_real_game_clock_uses_monotonic_time():
    clock = GameClock(paused=False)
    first = clock.now()
    assert clock.now() >= first


def test_manual_clock_only_moves_on_advance():
    clock = ManualClock()
    assert clock.now() == 0.0 and not clock.paused
    assert clock.advance(2.5) == 2.5
    assert clock.now() == 2.5
    clock.advance(0)
    assert clock.now() == 2.5


def test_manual_clock_honours_pause_like_the_real_clock():
    clock = ManualClock(10.0)
    clock.advance(1)
    clock.pause()
    clock.advance(100)  # wall time passes, game time must not
    assert clock.now() == pytest.approx(11.0)
    clock.resume()
    clock.advance(4)
    assert clock.now() == pytest.approx(15.0)


def test_manual_clock_set_and_start_paused():
    clock = ManualClock(3.0, paused=True)
    clock.advance(5)
    assert clock.now() == 3.0
    clock.set(7.0)
    assert clock.now() == 7.0
    clock.resume()
    clock.advance(1)
    assert clock.now() == pytest.approx(8.0)


def test_manual_clock_cannot_go_backwards():
    with pytest.raises(ValueError):
        ManualClock().advance(-1)
