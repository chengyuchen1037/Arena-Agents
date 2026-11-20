"""``python -m bench``: the command line of the benchmark.

    python -m bench audit    --game <game.json> [--trace <trace.jsonl>] [--scope public|all]
    python -m bench baseline --data-dir <dir> [--data-dir <dir> ...] [--out <report.json>]
    python -m bench situations [--check] | run --out <dir> ... | summarize --results <results.jsonl> | replay --game <game.json> ...   (bench.cli)

``audit`` prints the R / S1 / S3 numbers of ONE saved game; ``baseline`` does it for every saved game below the given data directories
(``<dir>/games/<id>/game.json`` + ``<dir>/logs/<id>/trace-*.jsonl``) and writes a pooled report: how the bots of an older version
(v0.13.1) really talked, the numbers the v0.14 gates are measured against (``bench/gates.yml``).  Only agents' speeches count; the
speeches of a person who played a seat are left out (and counted).  The report holds numbers and short shared phrases, never a game.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from bench.loaders import Speech, ledger_of, load_game, names_of, read_trace, speeches_of
from bench.metrics import cliche_counts, form_metrics, homogeneity, runtime_health


def audit_game(game: dict[str, Any], trace: list[dict[str, Any]] | None = None, *, scope: str = "public") -> dict[str, Any]:
    """Every number of one game: R, S1, S3."""
    speeches = speeches_of(game, scope=scope)
    agents = [s for s in speeches if not s.human]
    names = list(names_of(game).values())
    return {
        "version": game.get("version"),
        "scope": scope,
        "agent_speeches": len(agents),
        "human_speeches": len(speeches) - len(agents),
        "form": form_metrics(agents),
        "homogeneity": homogeneity(agents, names=names),  # strict: 6-grams shared by >= 4 seats
        "homogeneity_loose": homogeneity(agents, n=4, min_seats=4, names=names),  # looser: 4-grams shared by >= 4 seats
        "cliches": cliche_counts(agents),
        "runtime": runtime_health(ledger_of(game), trace or (), speeches=speeches),
    }


def _find_trace(data_dir: Path, session_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((data_dir / "logs" / session_id).glob("trace-*.jsonl")):
        rows.extend(read_trace(path))
    return rows


def _pooled(per_game: list[dict[str, Any]], all_agents: list[Speech]) -> dict[str, Any]:
    weights = [g["homogeneity"]["ngram_occurrences"] for g in per_game]
    shares = [g["homogeneity"]["shared_occurrence_share"] for g in per_game]
    loose_weights = [g["homogeneity_loose"]["ngram_occurrences"] for g in per_game]
    loose_shares = [g["homogeneity_loose"]["shared_occurrence_share"] for g in per_game]
    speeches = [g["agent_speeches"] for g in per_game]
    hits = [g["cliches"]["hits_per_speech"] for g in per_game]
    cut = sum(g["runtime"]["truncated_speeches"] for g in per_game)
    return {
        "games": len(per_game),
        "agent_speeches": sum(speeches),
        "form": form_metrics(all_agents),
        "shared_6gram_occurrence_share": round(sum(w * s for w, s in zip(weights, shares)) / sum(weights), 4) if sum(weights) else 0.0,
        "shared_4gram_occurrence_share": round(sum(w * s for w, s in zip(loose_weights, loose_shares)) / sum(loose_weights), 4) if sum(loose_weights) else 0.0,
        "cliche_hits_per_speech": round(sum(n * h for n, h in zip(speeches, hits)) / sum(speeches), 3) if sum(speeches) else 0.0,
        "truncated_speeches": cut,
    }


def baseline(data_dirs: list[Path]) -> dict[str, Any]:
    per_game: list[dict[str, Any]] = []
    pooled_speeches: list[Speech] = []
    for data_dir in data_dirs:
        for path in sorted((data_dir / "games").glob("*/game.json")):
            try:
                game = load_game(path)
            except ValueError:
                continue
            speeches = speeches_of(game)
            agents = [s for s in speeches if not s.human]
            if not agents:
                continue  # a game nobody spoke in (a demo, a game that was never started)
            report = audit_game(game, _find_trace(data_dir, path.parent.name))
            report["id"] = path.parent.name
            per_game.append(report)
            pooled_speeches.extend(agents)
    return {"format": "arena-bench-baseline", "games": per_game, "pooled": _pooled(per_game, pooled_speeches) if per_game else {}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench", description="The v0.14 benchmark (metrics over saved games).")
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit", help="the R / S1 / S3 numbers of one saved game")
    audit.add_argument("--game", required=True, type=Path)
    audit.add_argument("--trace", type=Path)
    audit.add_argument("--scope", default="public")
    base = sub.add_parser("baseline", help="pool every saved game below the data directories")
    base.add_argument("--data-dir", action="append", required=True, type=Path)
    base.add_argument("--out", type=Path)
    from bench import cli  # (the commands that run things: situations, run, summarize, replay)

    cli.add_commands(sub)
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):  # (a captured stdout of a test)
        pass
    handlers = {"situations": cli.cmd_situations, "run": cli.cmd_run, "summarize": cli.cmd_summarize, "replay": cli.cmd_replay}
    if args.command in handlers:
        return handlers[args.command](args)

    if args.command == "audit":
        trace = read_trace(args.trace) if args.trace else None
        report = audit_game(load_game(args.game), trace, scope=args.scope)
    else:
        report = baseline(args.data_dir)
    text = json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True)
    if getattr(args, "out", None):
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"written: {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
