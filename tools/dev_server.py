"""Start an isolated development instance of Arena-Agents (never touches your real workspace).

Two modes:

``--demo``
    Scripted bots play the moderator and the players (no model, no API key, no cost).

``--copy-providers-from <dir>``
    Real models.  The encrypted provider registry, the DPAPI key wrapper and the default model
    bindings are COPIED from ``<dir>`` (a workspace's ``.arena`` directory) into a private data
    directory, so the live workspace — which another running instance may hold locked — is never
    opened or modified.  Games started here call the bound models and **cost API credits**.

``--price-source`` (with ``--copy-providers-from``)
    Before starting, download the LiteLLM price table and price the bound models with it - in the private
    copy only - so the cost counter shows what the provider really bills (input / cache read / cache write /
    output) instead of the prices typed into the registry by hand.  Offline: the copied prices stay.

Examples::

    python tools/dev_server.py --demo --port 8123
    python tools/dev_server.py --copy-providers-from "C:/Users/me/Arena-Agents/Default/.arena" --price-source --port 8124

The private data directory is ``.demo-data/<mode>`` (git-ignored); delete it to forget everything.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", action="store_true", help="scripted bots instead of language models")
    parser.add_argument("--copy-providers-from", metavar="DIR", help="a workspace .arena directory to copy providers/bindings from")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--data-dir", help="private data directory (default: .demo-data/<mode>)")
    parser.add_argument("--fresh", action="store_true", help="wipe the private data directory first")
    parser.add_argument("--price-source", action="store_true", help="price the bound models with the LiteLLM table (private copy only)")
    args = parser.parse_args()
    if bool(args.demo) == bool(args.copy_providers_from):
        parser.error("choose exactly one of --demo or --copy-providers-from")
    if args.price_source and not args.copy_providers_from:
        parser.error("--price-source needs --copy-providers-from (the demo has no models)")

    mode = "demo" if args.demo else "real"
    data_dir = Path(args.data_dir) if args.data_dir else ROOT / ".demo-data" / mode
    if args.fresh and data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    os.environ["ARENA_DISABLE_WORKSPACE"] = "1"
    os.environ["ARENA_DISABLE_LEGACY_MIGRATION"] = "1"
    os.environ["ARENA_DATA_DIR"] = str(data_dir.resolve())
    os.environ["PYTHONUTF8"] = "1"
    if args.demo:
        os.environ["ARENA_DEMO"] = "1"
        # A throw-away key is enough: the demo data holds no secrets.
        os.environ.setdefault("ARENA_MASTER_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode())
    else:
        source = Path(args.copy_providers_from).expanduser().resolve()
        for name in ("providers.v1.enc", "master.v1.dpapi"):
            src = source / name
            if not src.is_file():
                raise SystemExit(f"missing {name} in {source}")
            shutil.copy2(src, data_dir / name)
        settings = {}
        if (source / "settings.json").is_file():
            settings = json.loads((source / "settings.json").read_text(encoding="utf-8"))
        # Only the model bindings (ids, no secrets); no last-session pointer.
        (data_dir / "settings.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "default_bindings": settings.get("default_bindings") or {},
                    "last_session_id": None,
                    "ui": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.environ.pop("ARENA_MASTER_KEY", None)
        os.environ.pop("ARENA_MASTER_KEY_FILE", None)
        if args.price_source:
            import asyncio

            from devlib import apply_price_source
            from app.storage.settings import get_default_bindings

            try:
                for line in asyncio.run(apply_price_source(get_default_bindings().values())):
                    print(line, flush=True)
            except Exception as exc:  # noqa: BLE001 - never stop a dev instance from starting
                print(f"price source: skipped ({type(exc).__name__}: {exc})", flush=True)

    print(f"Arena-Agents dev instance ({mode}) on http://{args.host}:{args.port}  data: {data_dir}", flush=True)
    if not args.demo:
        print("REAL MODELS: starting a game calls the bound models and costs API credits.", flush=True)

    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
