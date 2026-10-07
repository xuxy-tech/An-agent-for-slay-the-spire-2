from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, List

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.search.actions import cli_payload_for_action
from controller.search.combat_search import CombatSearcher, CombatSpec, RecordedAction


def make_cli(repo_root: Path) -> Sts2CliAdapter:
    return Sts2CliAdapter(
        CliConfig(repo_root=repo_root)
    )


def generate_history(
    repo_root: Path,
    combat_spec: CombatSpec,
    depth: int,
    chance_depth: int,
    max_steps: int,
) -> List[RecordedAction]:
    searcher = CombatSearcher(
        CliConfig(repo_root=repo_root),
        combat_spec,
        reuse_cli_processes=True,
        score_mode="balanced",
        symmetry_dedup=True,
    )
    history: List[RecordedAction] = []
    try:
        for _ in range(max_steps):
            result = searcher.search_from_history(history, depth=depth, chance_depth=chance_depth) if history else searcher.search(depth=depth, chance_depth=chance_depth)
            if not result.sequence:
                break
            action_name, payload = cli_payload_for_action(result.sequence[0])
            history.append(RecordedAction(action_name, payload))
    finally:
        searcher.close()
    return history


def prefix_rounds(repo_root: Path, combat_spec: CombatSpec, history: List[RecordedAction]) -> List[int]:
    cli = make_cli(repo_root)
    cli.start()
    rounds = [1]
    try:
        cli.start_test_combat(
            character=combat_spec.character,
            encounter=combat_spec.encounter,
            seed=combat_spec.seed,
            ascension=combat_spec.ascension,
            lang=combat_spec.lang,
        )
        for step in history:
            cli.action(step.action, step.args)
            state = cli.get_search_state()
            rounds.append((state.get("combat_state_for_search") or {}).get("combat", {}).get("round_number") or rounds[-1])
    finally:
        cli.stop()
    return rounds


def benchmark_prefixes(
    repo_root: Path,
    combat_spec: CombatSpec,
    history: List[RecordedAction],
    rounds: List[int],
    depth: int,
    chance_depth: int,
    reuse_cli_processes: bool,
    symmetry_dedup: bool,
) -> Dict[str, Any]:
    searcher = CombatSearcher(
        CliConfig(repo_root=repo_root),
        combat_spec,
        reuse_cli_processes=reuse_cli_processes,
        score_mode="balanced",
        symmetry_dedup=symmetry_dedup,
    )
    prefixes: List[Dict[str, Any]] = []
    try:
        for prefix_len in range(len(history) + 1):
            prefix = history[:prefix_len]
            started = time.perf_counter()
            result = searcher.search_from_history(prefix, depth=depth, chance_depth=chance_depth) if prefix else searcher.search(depth=depth, chance_depth=chance_depth)
            wall_ms = (time.perf_counter() - started) * 1000.0
            timing = searcher.timing
            prefixes.append(
                {
                    "prefix_len": prefix_len,
                    "round_number": rounds[prefix_len],
                    "wall_ms": round(wall_ms, 3),
                    "score": result.score,
                    "sequence_len": len(result.sequence),
                    "nodes": result.stats.get("nodes"),
                    "replay_calls": timing["replay_calls"],
                    "replay_total_ms": round(timing["replay_total_ms"], 3),
                    "snapshot_restore_hits": timing["snapshot_restore_hits"],
                    "snapshot_capture_count": timing["snapshot_capture_count"],
                }
            )
            searcher.timing = {
                "replay_calls": 0,
                "replay_total_ms": 0.0,
                "replay_ms_samples": [],
                "branch_ms_samples": [],
                "replay_breakdown_samples": [],
                "snapshot_restore_hits": 0,
                "snapshot_capture_count": 0,
                "chance_lookup_hits": 0,
                "chance_lookup_misses": 0,
                "chance_expected_threat_total": 0.0,
                "symmetry_pruned_actions": 0,
                "state_pruned_actions": 0,
                "subtree_cache_hits": 0,
                "eval_cache_hits": 0,
            }
        return {
            "reuse_cli_processes": reuse_cli_processes,
            "symmetry_dedup": symmetry_dedup,
            "prefixes": prefixes,
        }
    finally:
        searcher.close()


def summarize_improvement(cold: Dict[str, Any], hot: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    by_prefix_hot = {row["prefix_len"]: row for row in hot["prefixes"]}
    for cold_row in cold["prefixes"]:
        hot_row = by_prefix_hot[cold_row["prefix_len"]]
        cold_ms = float(cold_row["wall_ms"])
        hot_ms = float(hot_row["wall_ms"])
        rows.append(
            {
                "prefix_len": cold_row["prefix_len"],
                "round_number": cold_row["round_number"],
                "cold_wall_ms": cold_ms,
                "hot_wall_ms": hot_ms,
                "speedup_x": round((cold_ms / hot_ms), 3) if hot_ms > 0 else None,
                "hot_snapshot_restore_hits": hot_row["snapshot_restore_hits"],
                "hot_snapshot_capture_count": hot_row["snapshot_capture_count"],
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark multi-turn search with and without snapshot restore")
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument("--encounter", required=True)
    parser.add_argument("--seed", default="42")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--chance-depth", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--no-symmetry-dedup", action="store_true")
    parser.add_argument("--no-state-dedup", action="store_true")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    combat_spec = CombatSpec(
        character=args.character,
        encounter=args.encounter,
        seed=args.seed,
        ascension=args.ascension,
        lang=args.lang,
    )

    history = generate_history(
        repo_root=repo_root,
        combat_spec=combat_spec,
        depth=args.depth,
        chance_depth=args.chance_depth,
        max_steps=args.max_steps,
    )
    rounds = prefix_rounds(repo_root, combat_spec, history)
    cold = benchmark_prefixes(
        repo_root,
        combat_spec,
        history,
        rounds,
        depth=args.depth,
        chance_depth=args.chance_depth,
        reuse_cli_processes=False,
        symmetry_dedup=not args.no_symmetry_dedup,
    )
    hot = benchmark_prefixes(
        repo_root,
        combat_spec,
        history,
        rounds,
        depth=args.depth,
        chance_depth=args.chance_depth,
        reuse_cli_processes=True,
        symmetry_dedup=not args.no_symmetry_dedup,
    )
    print(
        json.dumps(
            {
                "encounter": args.encounter,
                "depth": args.depth,
                "chance_depth": args.chance_depth,
                "symmetry_dedup": not args.no_symmetry_dedup,
                "history_len": len(history),
                "history": [{"action": h.action, "args": h.args} for h in history],
                "cold": cold,
                "hot": hot,
                "comparison": summarize_improvement(cold, hot),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
