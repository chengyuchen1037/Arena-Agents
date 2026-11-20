"""Initialize the default bot library for real, before a release (costs API credits - it has a hard call budget).

The twelve default bots in bots/<name>/ ship with an L0 only (who they are, how they talk, how they think).  Every installation copies them to
its data directory as "not initialized"; the user has to initialize them before the first game.  This tool does that run ONCE, with the model
YOU choose, on the official contract (prompts/contract.md) and the default scenario of this checkout (no study materials), and - only with
--write-library - puts what the bots wrote (their operating notes, the rules digest, the playbook, the open questions) and the initialization
record (the model, the time, the four hashes) into bots/<name>/, so that the shipped bots are ready (✔) from the first start.  Review the
result like any other change (git diff bots/) before it is committed; docs/real-model-runs.md says what to look for.

What it does:
  1. makes a private scratch data directory and COPIES providers.json, secrets.v1.enc and (Windows) master.v1.dpapi into it from --from - the
     live data directory is only read, never opened by a store or modified; the scratch is deleted at the end (--keep keeps it);
  2. binds the model to all twelve seats and the moderator - --model PROVIDER/MODEL (the provider's id or its name as shown in the settings,
     then the model id), or the model bound to the moderator in --from;
  3. starts the application on the scratch, which makes the table from the default library (L0 only) and runs the application's own
     initialization (start_init) for the bots that need it: every bot asks the model in parallel, at most 120 s each, all or nothing - from
     scratch, the memory a library bot may already have is not revised;
  4. prints the report: per bot the state, the time, the calls and tokens, the files it wrote and their size, why it failed; then the total;
  5. with --write-library: copies arch/notes.md and rules/<scenario>/{digest,playbook,questions}.md of every bot that SUCCEEDED, and its
     "init" record (model = the model id, time, the four hashes), into bots/<name>/; a bot that failed is not touched.

Which bots: by default those whose initialization in the library is not up to date (never initialized, or the L0, the contract or the scenario
changed since); --all takes all twelve, --only Alice,Bob takes the ones named.  --max-calls (hard) and --timeout cap the cost; --dry-run prints
the plan without calling a model; --check only checks that the initialized bots of the library are up to date (no model, no --from).

Exit code: 0 everything asked for was done; 1 a bot failed, or the library is out of date (--check); 2 bad arguments (the model, the names).

Examples::

    python tools/init_default_bots.py --from "C:/Users/me/Arena-Agent/data" --dry-run
    python tools/init_default_bots.py --from ... --model MyProvider/my-model --max-calls 200 --write-library
    python tools/init_default_bots.py --from ... --only Kate --write-library
    python tools/init_default_bots.py --check
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools/ (devlib), whatever the working directory is

import devlib  # noqa: E402

LIBRARY = ROOT / "bots"
DEFAULT_MAX_CALLS = 200
DEFAULT_TIMEOUT_S = 400.0
#: Seconds between two looks at the running job (a test makes it short).
POLL_S = 0.5
SEATS = 12
RULE_FILES = ("digest.md", "playbook.md", "questions.md")
FIX_COMMAND = "tools/init_default_bots.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="init_default_bots.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--from", dest="source", help="a data directory (the data/ of an installation) holding providers.json and secrets.v1.enc; only read (needed unless --check)"
    )
    parser.add_argument(
        "--model",
        metavar="PROVIDER/MODEL",
        help="the model every bot uses: the provider (its id, or its name as shown in the settings), a slash, the model id; default: the model bound to the moderator in --from",
    )
    parser.add_argument("--only", metavar="NAMES", help="only these bots (names separated by commas, e.g. Alice,Bob); default: every bot whose initialization is not up to date")
    parser.add_argument("--all", action="store_true", help="every bot of the library, also those whose initialization is up to date")
    parser.add_argument("--write-library", action="store_true", help="put the result of the bots that succeeded into the library (bots/<name>/); without it the library is not touched")
    parser.add_argument("--library", metavar="DIR", help="the default bot library to start from and to write into (default: bots/ of this checkout)")
    parser.add_argument("--dry-run", action="store_true", help="print what would be done - the model, the bots, the budget - and call no model")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only check that the initialized bots of the library are up to date with the contract, the default scenario and their L0; no model, no --from; exit 1 if one is not",
    )
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS, help=f"hard budget of model calls (default {DEFAULT_MAX_CALLS}: about 5-8 per bot); a call over it is never sent")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help=f"wall-clock limit in seconds for the whole run (default {DEFAULT_TIMEOUT_S:.0f}); the run is cancelled when it is reached")
    parser.add_argument("--concurrency", type=int, default=0, help="limit of concurrent model calls (0 = unlimited: all bots at once)")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory (it holds an encrypted copy of the providers and the bots as they were initialized)")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.check and not args.source:
        parser.error("--from is required (except with --check)")
    if args.max_calls < 1:
        parser.error("--max-calls must be at least 1")
    if args.only and args.all:
        parser.error("--only and --all exclude each other")
    return args


# ================================================================================================= the library and its hashes
def library_folders(library: Path) -> dict[str, Path]:
    """``{bot name (case-folded): folder}`` of the default library (the name is the one in ``bot.json``, else the folder's)."""
    found: dict[str, Path] = {}
    folders = sorted(p for p in Path(library).iterdir() if p.is_dir() and not p.name.startswith(".")) if Path(library).is_dir() else []
    for folder in folders:
        name = folder.name
        try:
            raw = json.loads((folder / "bot.json").read_text(encoding="utf-8"))
            if isinstance(raw, dict) and str(raw.get("name") or "").strip():
                name = str(raw["name"]).strip()
        except (OSError, ValueError):
            pass
        found[name.casefold()] = folder
    return found


def require_library(library: Path) -> None:
    """``SystemExit`` when ``library`` is not a default bot library (a folder with bot folders in it) - the tool would make a table of blank bots."""
    if not library_folders(library):
        raise SystemExit(f"no default bot library at {library}: it must hold one folder per bot (bots/<name>/bot.json and core/)")


def fresh_install_hashes() -> Any:
    """What the contract, the default scenario and the (absent) study materials hash to on a FRESH installation: the stores of the data directory
    the process is configured with (``ARENA_DATA_DIR``) copy ``prompts/contract.md`` and the scenario ``config.yml`` names on their first use,
    and the application hashes what they hold - not the bytes of the files in the checkout (a store writes LF line ends, for one thing).  Call it
    with a scratch data directory.  Returns :class:`app.bots.types.SourceHashes`."""
    from app.config import load_config
    from app.game.sources import Sources
    from app.storage.paths import get_data_dir

    if Path(get_data_dir()).resolve() == (ROOT / "data").resolve():
        raise RuntimeError("fresh_install_hashes() makes the stores of the data directory it runs in: use a scratch ARENA_DATA_DIR, never data/")
    return Sources(load_config, lambda: True).hashes()


def library_problems(library: Path, hashes: Any) -> list[str]:
    """Why the INITIALIZED bots of ``library`` are not what a fresh installation would find ready, in Chinese - ``[]`` when every bot whose
    ``bot.json`` says ``ready`` matches the L0 on disk and ``hashes`` (the contract, the scenario, the materials of
    :func:`fresh_install_hashes`) and has the memory files its record promises.  Bots that are not initialized are not looked at (a library
    may ship without any: then everything is fine)."""
    from app.bots.types import core_hash
    from app.storage import botfiles
    from app.storage.bots import SOURCE_LABELS, derive_init, normalize_init

    problems: list[str] = []
    folders = sorted(p for p in Path(library).iterdir() if p.is_dir() and not p.name.startswith(".")) if Path(library).is_dir() else []
    for folder in folders:
        label = f"bots/{folder.name}"
        try:
            raw = json.loads((folder / "bot.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            problems.append(f"{label}/bot.json 读不出来，请检查这个文件。")
            continue
        init = normalize_init(raw.get("init") if isinstance(raw, dict) else None)
        if init["status"] != "ready":
            continue
        derived = derive_init(init, core_hash(botfiles.read_core(folder / "core")), hashes)
        if derived["stale"]:
            what = "、".join(SOURCE_LABELS.get(key, key) for key in derived["stale"])
            problems.append(f"{label} 的初始化已经过期（{what}在初始化之后改过）：请重新运行 {FIX_COMMAND}（再用 --write-library 写回）。")
            continue
        scenario_id = init.get("scenario_id") or getattr(hashes, "scenario_id", "")
        wanted = [folder / "arch" / "notes.md", *(folder / "rules" / scenario_id / name for name in ("digest.md", "playbook.md"))]
        missing = [str(path.relative_to(folder)).replace("\\", "/") for path in wanted if not _has_text(path)]
        if missing:
            problems.append(f"{label} 的初始化记录是“已就绪”，但缺少它的记忆文件：{'、'.join(missing)}。请重新运行 {FIX_COMMAND}（再用 --write-library 写回）。")
    return problems


def _has_text(path: Path) -> bool:
    try:
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def copy_bot_to_library(bot_dir: Path, folder: Path, init: dict[str, Any], model: str) -> list[str]:
    """Put what an initialization produced into the library folder of the bot: ``arch/notes.md``, ``rules/<scenario>/{digest,playbook,questions}.md``
    (the old ones are replaced: the library has exactly this run's memory) and the ``init`` record in ``bot.json`` with ``model`` as given.  The
    name, the avatar and the L0 of the library are not touched.  Returns the relative paths written."""
    scenario_id = str(init.get("scenario_id") or "")
    written: list[str] = []
    for part in ("arch", "rules"):
        target = folder / part
        if target.is_dir():
            shutil.rmtree(target)
    notes = bot_dir / "arch" / "notes.md"
    if _has_text(notes):
        (folder / "arch").mkdir(parents=True, exist_ok=True)
        (folder / "arch" / "notes.md").write_bytes(notes.read_bytes())
        written.append("arch/notes.md")
    for name in RULE_FILES:
        source = bot_dir / "rules" / scenario_id / name
        if _has_text(source):
            (folder / "rules" / scenario_id).mkdir(parents=True, exist_ok=True)
            (folder / "rules" / scenario_id / name).write_bytes(source.read_bytes())
            written.append(f"rules/{scenario_id}/{name}")
    meta = json.loads((folder / "bot.json").read_text(encoding="utf-8"))
    meta["init"] = {**init, "model": model}
    (folder / "bot.json").write_bytes((json.dumps(meta, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    written.append("bot.json")
    return written


# ============================================================================================================== the model
def resolve_model(source: Path, scratch: Path, text: str | None) -> Any:
    """The binding every bot is initialized with: ``text`` is ``PROVIDER/MODEL`` (the provider's id, or its name as the settings show it, then the
    id of one of its models); without it the model bound to the moderator in ``source``.  Looked up in the scratch COPY of the registry (``source``
    is only read); ``SystemExit`` says what is wrong and what there is to choose from."""
    from app.game.bindings import normalized_binding
    from app.providers.models import ModelBinding, ProviderError
    from app.providers.store import ProviderStore

    store = ProviderStore(scratch)
    providers = store.list_providers()
    listing = "; ".join(f"{p.name} ({p.id}): {', '.join(m.id for m in p.models) or 'no models'}" for p in providers) or "none"
    if text:
        provider_text, slash, model_id = text.partition("/")
        if not slash or not provider_text.strip() or not model_id.strip():
            raise SystemExit("--model must be PROVIDER/MODEL: the provider's id or name, a slash, the model id")
        found = [p for p in providers if p.id == provider_text.strip()] or [p for p in providers if p.name.casefold() == provider_text.strip().casefold()]
        if len(found) != 1:
            raise SystemExit(f"no (single) provider {provider_text!r} in {source}; the providers there: {listing}")
        binding = ModelBinding(provider_id=found[0].id, model_id=model_id.strip())
    else:
        binding = devlib.source_bindings(source, ["N1"])["moderator"]
    try:
        provider, entry = store.resolve(binding)
    except ProviderError as exc:
        raise SystemExit(f"the model {binding.model_id!r} cannot be used ({type(exc).__name__}); the providers in {source}: {listing}") from exc
    if not str(provider.api_key or "").strip():
        raise SystemExit(f"the provider {provider.name!r} has no API key stored in {source}")
    return normalized_binding(binding, entry)


def describe_model(scratch: Path, binding: Any) -> str:
    from app.providers.store import ProviderStore

    provider, entry = ProviderStore(scratch).resolve(binding)
    price = entry.price_snapshot()  # (an empty cache price is billed like an input token)
    return (
        f"{provider.name} / {entry.id}  (provider {provider.id}; prices in ${price['input_price_per_m']}/M, cache read ${price['cache_read_price_per_m']}/M,"
        f" out ${price['output_price_per_m']}/M; context window {binding.context_window_tokens}, reasoning {binding.reasoning_depth})"
    )


# ================================================================================================================== run
async def run(
    args: argparse.Namespace,
    *,
    port_factory: Callable[[Any], Any] | None = None,
    scratch_root: Path | None = None,
) -> int:
    """The whole run.  ``port_factory`` (a test): a fake model port for the learning runs instead of the real models; the budget still counts."""
    library = Path(args.library).expanduser().resolve() if args.library else LIBRARY
    require_library(library)
    if args.check:
        return check(library, scratch_root)
    source = Path(args.source).expanduser().resolve()
    scratch = devlib.new_scratch(scratch_root if scratch_root is not None else ROOT / ".demo-data", "arena-init-bots-")
    try:
        with devlib.scratch_environment(scratch):  # (the data directory is the scratch BEFORE anything of the application is imported)
            devlib.copy_provider_files(source, scratch)
            devlib.adopt_master_key(scratch)
            binding = resolve_model(source, scratch, args.model)
            devlib.write_bindings(scratch, {"moderator": binding, **{f"N{i}": binding for i in range(1, SEATS + 1)}})
            return await initialize_library(args, scratch, library, binding, port_factory)
    finally:
        if args.keep:
            print("scratch kept at", scratch)
        else:
            shutil.rmtree(scratch, ignore_errors=True)


def check(library: Path, scratch_root: Path | None = None) -> int:
    """``--check``: no model, no providers - only the stores of a scratch data directory, to hash what a fresh installation would hash."""
    from app.storage import reset_stores_for_tests

    scratch = devlib.new_scratch(scratch_root if scratch_root is not None else ROOT / ".demo-data", "arena-init-bots-check-")
    try:
        with devlib.scratch_environment(scratch):
            devlib.require_scratch(scratch)
            reset_stores_for_tests()
            try:
                hashes = fresh_install_hashes()
            finally:
                reset_stores_for_tests()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    problems = library_problems(library, hashes)
    initialized = sum(1 for _name, folder in library_folders(library).items() if _is_ready(folder))
    if problems:
        print(f"the default bot library is OUT OF DATE ({len(problems)} problem(s)):")
        for problem in problems:
            print("  -", problem)
        return 1
    print(f"the default bot library is up to date ({initialized} initialized bot(s) of {len(library_folders(library))}; contract {hashes.contract[:12]}, scenario {hashes.scenario_id} {hashes.scenario[:12]})")
    return 0


def _is_ready(folder: Path) -> bool:
    from app.storage.bots import normalize_init

    try:
        raw = json.loads((folder / "bot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and normalize_init(raw.get("init"))["status"] == "ready"


def plan_rows(orch: Any) -> list[dict[str, Any]]:
    """Every bot of the table with what its initialization is now (``ready`` / ``stale`` and what changed / ``uninitialized`` / ``failed``)."""
    store = orch._bots()
    sources = orch.source_hashes()
    rows = []
    for bot_id in store.lineup():
        summary = store.summary(bot_id, sources=sources)
        init = summary["init"]
        rows.append(
            {
                "id": bot_id,
                "name": summary["name"],
                "status": init["status"],
                "stale": list(init.get("stale") or []),
                "core_complete": bool(summary["core"]["complete"]),
            }
        )
    return rows


def select_bots(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    """The bots to initialize: ``--only`` (by name), ``--all``, else every bot that is not ``ready``."""
    if args.only:
        wanted = [n.strip() for n in str(args.only).split(",") if n.strip()]
        by_name = {row["name"].casefold(): row for row in rows}
        unknown = [n for n in wanted if n.casefold() not in by_name]
        if unknown:
            raise SystemExit(f"no such bot in the library: {', '.join(unknown)}; the bots: {', '.join(row['name'] for row in rows)}")
        chosen: list[dict[str, Any]] = []
        for name in wanted:
            row = by_name[name.casefold()]
            if row not in chosen:
                chosen.append(row)
        return chosen
    if args.all:
        return list(rows)
    return [row for row in rows if row["status"] != "ready"]


def print_plan(orch: Any, scratch: Path, binding: Any, rows: list[dict[str, Any]], chosen: list[dict[str, Any]], args: argparse.Namespace, library: Path) -> None:
    hashes = orch.source_hashes()
    states = ", ".join(row["name"] + " [" + row["status"] + ("/" + ",".join(row["stale"]) if row["stale"] else "") + "]" for row in rows)
    materials = "none" if not hashes.materials else hashes.materials[:12] + "…"
    which = "--only" if args.only else "--all" if args.all else "not up to date"
    print("initialize the default bot library")
    print(f"  model: {describe_model(scratch, binding)}")
    print(f"  library: {library} - {states}")
    print(f"  contract {hashes.contract[:12]}…  scenario {hashes.scenario_id} {hashes.scenario[:12]}…  study materials: {materials}")
    print(f"  to initialize ({which}): {', '.join(row['name'] for row in chosen) or 'nobody'}")
    print(f"  budget: at most {args.max_calls} model calls (about 5-8 per bot), 120 s per bot, {args.timeout:.0f} s in all, concurrency {args.concurrency or 'unlimited'}")
    if args.write_library:
        print("  --write-library: the result of every bot that succeeds is copied into the library; the others are not touched")
    elif not args.keep and not args.dry_run:
        print("  NOTE: without --write-library (and --keep) the result is only reported and then deleted")


async def initialize_library(args: argparse.Namespace, scratch: Path, library: Path, binding: Any, port_factory: Callable[[Any], Any] | None) -> int:
    from app.game.orchestrator import GameOrchestrator
    from app.learning.jobs import LearningRefused
    from app.storage import reset_stores_for_tests
    from app.storage.bots import BotStore

    devlib.require_scratch(scratch)
    reset_stores_for_tests()  # (the stores are made for the scratch directory now)
    BotStore(scratch, seats=SEATS, library_dir=library).bootstrap()  # the table = the bots of THIS library, L0 only
    meter = devlib.Meter(args.max_calls)
    orch = GameOrchestrator()
    if port_factory is not None:
        orch.llm_port_factory = lambda o: meter.port(port_factory(o))
    else:  # every real call passes the client
        orch.llm = meter.client(orch.llm)
    started = time.monotonic()
    try:
        await orch.startup()
        orch.cfg.setdefault("runtime", {})["llm_concurrency"] = int(args.concurrency)  # (after startup: it reloads the configuration)
        orch.cfg["runtime"]["llm_concurrency_per_provider"] = 0
        rows = plan_rows(orch)
        chosen = select_bots(rows, args)
        print_plan(orch, scratch, binding, rows, chosen, args, library)
        if args.dry_run:
            print("dry run: no model was called, nothing was written")
            return 0
        if not chosen:
            print("nothing to do: every bot of the library is up to date (--all initializes them again)")
            return 0
        store = orch._bots()
        store.clear_memory([row["id"] for row in chosen])  # from scratch: the result must not depend on what the library had
        try:
            job = await orch.start_init(ids=[row["id"] for row in chosen])
        except LearningRefused as exc:
            print("INITIALIZATION REFUSED:", exc)
            for problem in exc.problems:
                print("  -", problem.get("name") or problem.get("scope"), [item["message"] for item in problem["items"]])
            return 1
        print(f"\njob {job['job_id']}: {len(job['ids'])} bots at the same time, at most {job['deadline_s']}s each")
        deadline = started + args.timeout
        while orch.init_job_view() is not None:
            if time.monotonic() > deadline:
                print("time limit reached; cancelling the initialization (the bots that were not done are not written)")
                await orch.cancel_init()
                break
            await asyncio.sleep(POLL_S)
        await asyncio.sleep(0.2)
        print("\n=== initialization ===")
        result = devlib.init_report(orch, [row["id"] for row in chosen])
        print(f"  model calls: {meter.calls} of {meter.max_calls} (refused over the budget: {meter.refused}); errors {dict(meter.errors) or 'none'}; {time.monotonic() - started:.0f}s")
        done = [row for row in result if row["status"] == "ready"]
        failed = [row for row in result if row["status"] != "ready"]
        code = 0 if not failed else 1
        if args.write_library:
            code = max(code, write_library(orch, library, done))
        elif done:
            print("\nlibrary NOT changed (no --write-library)" + ("; the scratch directory is kept" if args.keep else ""))
        if failed:
            print("\nFAILED (not written; run again with --only to try just these): " + ", ".join(f"{row['name']} ({row['error'] or row['status']})" for row in failed))
        return code
    finally:
        try:
            await orch.shutdown()
        except Exception as exc:  # noqa: BLE001
            print("shutdown error:", type(exc).__name__)
        reset_stores_for_tests()


def write_library(orch: Any, library: Path, done: list[dict[str, Any]]) -> int:
    """``--write-library``: copy what every bot that succeeded produced into its folder of the library; then check the library again."""
    store = orch._bots()
    model = store_model_id(orch)
    folders = library_folders(library)
    print(f"\n=== writing {library} ===")
    for row in done:
        folder = folders.get(str(row["name"]).casefold())
        if folder is None or not (folder / "bot.json").is_file():
            print(f"  {row['name']}: no folder with a bot.json of that name in the library - skipped")
            continue
        written = copy_bot_to_library(store.bot_dir(row["id"]), folder, store.init_record(row["id"]), model)
        print(f"  {row['name']}: {', '.join(written)}")
    hashes = orch.source_hashes()
    problems = library_problems(library, hashes)
    if problems:
        print("the library is NOT up to date after writing:")
        for problem in problems:
            print("  -", problem)
        return 1
    print("library check: every initialized bot is up to date (git diff bots/ shows what changed)")
    return 0


def store_model_id(orch: Any) -> str:
    """The model id the bots were initialized with (the moderator's binding - every seat has the same one)."""
    binding = orch.session.model_bindings.get("moderator") or next(iter(orch.session.model_bindings.values()))
    return str(binding.model_id)


def main(argv: list[str] | None = None) -> int:
    stdout = getattr(sys.stdout, "reconfigure", None)
    if callable(stdout):  # (a Windows console may not be UTF-8; the report has Chinese in it)
        stdout(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="LOG %(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
