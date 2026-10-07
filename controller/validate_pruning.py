from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from cli.sts2_cli_adapter import CliConfig
from controller.search.combat_search import CombatSearcher, CombatSpec, SearchResult


def _sequence_signature(result: SearchResult) -> List[Dict[str, Any]]:
    return [
        {
            "action_type": a.action_type,
            "card_index": a.card_index,
            "target_index": a.target_index,
            "metadata": a.metadata,
        }
        for a in result.sequence
    ]


def _run_search(
    *,
    repo_root: Path,
    encounter: str,
    character: str,
    seed: str,
    depth: int,
    chance_depth: int,
    score_mode: str,
    reuse_cli_processes: bool,
    symmetry_dedup: bool,
    state_dedup: bool,
) -> Dict[str, Any]:
    cli_cfg = CliConfig(repo_root=repo_root)
    combat_spec = CombatSpec(character=character, encounter=encounter, seed=seed)
    searcher = CombatSearcher(
        cli_cfg,
        combat_spec,
        reuse_cli_processes=reuse_cli_processes,
        score_mode=score_mode,
        symmetry_dedup=symmetry_dedup,
        state_dedup=state_dedup,
    )
    try:
        result = searcher.search(depth=depth, chance_depth=chance_depth)
        return {
            "score": result.score,
            "sequence": _sequence_signature(result),
            "stats": result.stats,
            "timing": {
                "replay_calls": searcher.timing["replay_calls"],
                "replay_total_ms": round(searcher.timing["replay_total_ms"], 3),
                "replay_avg_ms": round(
                    searcher.timing["replay_total_ms"] / searcher.timing["replay_calls"]
                    if searcher.timing["replay_calls"]
                    else 0.0,
                    3,
                ),
                "snapshot_restore_hits": searcher.timing["snapshot_restore_hits"],
                "snapshot_capture_count": searcher.timing["snapshot_capture_count"],
                "symmetry_pruned_actions": searcher.timing["symmetry_pruned_actions"],
                "state_pruned_actions": searcher.timing["state_pruned_actions"],
                "budget_pruned_actions": searcher.timing["budget_pruned_actions"],
            },
        }
    finally:
        searcher.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate whether pruning changes root decisions")
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument("--seed", default="42")
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--chance-depth", type=int, default=1)
    parser.add_argument("--score-mode", default="balanced", choices=[
        "damage_first", "balanced", "balanced_action", "balanced_future",
        "balanced_nosquare", "balanced_unweighted", "balanced_power",
        "balanced_r0a", "balanced_r0b", "balanced_r0c", "balanced_r0d",
        "defense_first", "fallback",
    ])
    parser.add_argument("--reuse-cli-processes", action="store_true")
    parser.add_argument("encounters", nargs="+", help="Encounter ids to compare")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    reports = []
    for encounter in args.encounters:
        baseline = _run_search(
            repo_root=repo_root,
            encounter=encounter,
            character=args.character,
            seed=args.seed,
            depth=args.depth,
            chance_depth=args.chance_depth,
            score_mode=args.score_mode,
            reuse_cli_processes=args.reuse_cli_processes,
            symmetry_dedup=False,
            state_dedup=True,
        )
        candidate = _run_search(
            repo_root=repo_root,
            encounter=encounter,
            character=args.character,
            seed=args.seed,
            depth=args.depth,
            chance_depth=args.chance_depth,
            score_mode=args.score_mode,
            reuse_cli_processes=args.reuse_cli_processes,
            symmetry_dedup=True,
            state_dedup=True,
        )
        decision_same = baseline["sequence"] == candidate["sequence"]
        score_same = abs(float(baseline["score"]) - float(candidate["score"])) < 1e-9
        reports.append(
            {
                "encounter": encounter,
                "decision_same": decision_same,
                "score_same": score_same,
                "baseline": baseline,
                "candidate": candidate,
                "nodes_delta": int(candidate["stats"].get("nodes", 0)) - int(baseline["stats"].get("nodes", 0)),
                "replay_total_ms_delta": round(
                    float(candidate["timing"]["replay_total_ms"]) - float(baseline["timing"]["replay_total_ms"]),
                    3,
                ),
            }
        )

    summary = {
        "all_decisions_same": all(r["decision_same"] for r in reports),
        "all_scores_same": all(r["score_same"] for r in reports),
        "reports": reports,
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
