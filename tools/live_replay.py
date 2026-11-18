"""Contract check of a RUNNING instance: do the SSE events add up to the session the server reports?

It plays a whole game on a dev instance (``tools/dev_server.py --demo``) and records

* the first ``snapshot`` event of the stream (the idle session),
* every event that follows,
* ``GET /api/state`` once the game is over (and, optionally, after the post-game review),

then replays the events with the REAL client store (``static/js/store.js``, through
``tests/frontend/replay_check.js --live``) and compares the result with the server's own final session: names,
avatars, the three agent states, channels, messages, requests with their answers / results, the budget, the
review, the token counters incl. the cache kinds.  It is the same check the mock server's log goes through in
``tests/frontend/test_mock_server.py``, but against the real backend - a drift between what the engine sends and
what the client understands shows up here.

The game is driven through the public API only: a token limit with "force stop" is set first (so the log
contains ``budget.updated|warning|reached`` and the pause), raised later, and the post-game review is started
(``--no-review`` skips it).  Demo bots cost nothing.

Usage::

    python tools/dev_server.py --demo --port 8135 --data-dir .demo-data/demo-live --fresh
    python tools/live_replay.py --base http://127.0.0.1:8135

Exit code 0 = the client store reproduces the server's session exactly.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]


class Recorder(threading.Thread):
    """Reads the SSE stream; the first event is the snapshot (= the initial session)."""

    def __init__(self, base: str) -> None:
        super().__init__(daemon=True)
        self.base = base
        self.initial: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.ready = threading.Event()
        self.stop_flag = threading.Event()
        self.error: str | None = None

    def run(self) -> None:
        try:
            with httpx.Client(timeout=httpx.Timeout(None, connect=10.0)) as client:
                with client.stream("GET", self.base + "/api/events") as response:
                    data_lines: list[str] = []
                    for line in response.iter_lines():
                        if self.stop_flag.is_set():
                            return
                        if line.startswith("data:"):
                            data_lines.append(line[5:].strip())
                        elif line == "" and data_lines:
                            event = json.loads("\n".join(data_lines))
                            data_lines = []
                            if self.initial is None:
                                if event.get("type") != "snapshot":
                                    raise RuntimeError("the first event of the stream is not a snapshot")
                                self.initial = event["data"]["session"]
                                self.ready.set()
                            else:
                                self.events.append(event)
        except Exception as exc:  # noqa: BLE001 - reported by the main thread
            self.error = f"{type(exc).__name__}: {exc}"
            self.ready.set()


def call(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise SystemExit(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
    return response.json() if response.content else None


def session_of(client: httpx.Client) -> dict[str, Any]:
    return call(client, "GET", "/api/state")["session"]


def wait_until(what: str, predicate: Any, timeout: float, client: httpx.Client) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = session_of(client)
        if predicate(session):
            return session
        time.sleep(0.5)
    raise SystemExit(f"timed out waiting for: {what}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8135")
    parser.add_argument("--out", default=str(ROOT / ".demo-data" / "live-replay.json"))
    parser.add_argument("--timeout", type=float, default=420.0, help="seconds to wait for the game to finish")
    parser.add_argument("--no-review", action="store_true")
    parser.add_argument("--no-budget", action="store_true", help="do not exercise the token limit")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    with httpx.Client(base_url=base, timeout=60.0) as client:
        call(client, "POST", "/api/control/new")
        if not args.no_budget:
            call(client, "PUT", "/api/budget", json={"max_tokens": 500_000, "max_cost_usd": None, "force_stop": True})
        recorder = Recorder(base)
        recorder.start()
        recorder.ready.wait(15)
        if recorder.error or recorder.initial is None:
            raise SystemExit(f"could not read the event stream: {recorder.error}")
        print(f"recording from event {recorder.initial.get('event_id')} ({recorder.initial['status']})", flush=True)

        call(client, "POST", "/api/control/start")
        deadline = time.monotonic() + args.timeout
        raised = False
        while time.monotonic() < deadline:
            session = session_of(client)
            if session["status"] == "finished":
                break
            if session["status"] == "paused" and session["pause_reason"] == "budget" and not raised:
                print("paused by the token limit: raising it", flush=True)
                call(client, "PUT", "/api/budget", json={"max_tokens": None, "max_cost_usd": None, "force_stop": False})
                call(client, "POST", "/api/control/start")  # (start also resumes a paused game)
                raised = True
            time.sleep(1.0)
        else:
            raise SystemExit("the game did not finish in time")
        if not args.no_budget and not raised:
            print("note: the token limit was never reached (nothing paused)", flush=True)

        if not args.no_review:
            started = call(client, "POST", "/api/game/review")
            print("review started for", started.get("started"), flush=True)
            wait_until("the review of every player", lambda s: len(s["review"]["done"]) >= len(s["participants"]) and not s["review"]["running"], 240, client)
        time.sleep(3.0)  # let the last events arrive
        final = session_of(client)
        recorder.stop_flag.set()

    payload = {"initial": recorder.initial, "events": recorder.events, "final": final}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    kinds: dict[str, int] = {}
    for event in recorder.events:
        kinds[event["type"]] = kinds.get(event["type"], 0) + 1
    print(f"{len(recorder.events)} events recorded -> {out}", flush=True)
    print("event types:", dict(sorted(kinds.items(), key=lambda kv: -kv[1])), flush=True)

    result = subprocess.run(["node", str(ROOT / "tests" / "frontend" / "replay_check.js"), str(out), "--live"], cwd=str(ROOT), capture_output=True, text=True)
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip()[-3000:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
