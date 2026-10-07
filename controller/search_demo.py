from __future__ import annotations

import argparse
import json
from pathlib import Path

from cli.sts2_cli_adapter import CliConfig
from controller.search.enemy_chance_model import EnemyChanceLookupTable
from controller.search.combat_search import CombatSearcher, CombatSpec


def main() -> None:
    parser = argparse.ArgumentParser(description="Run combat-only shallow search")
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument("--encounter", required=True)
    parser.add_argument("--seed", default="42")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--chance-depth", type=int, default=1)
    parser.add_argument("--parallel-top-level", action="store_true")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--reuse-cli-processes", action="store_true")
    parser.add_argument("--chance-table", default=None)
    parser.add_argument("--score-mode", default="preference", choices=[
        "preference",
        "damage_first", "balanced", "balanced_action", "balanced_future",
        "balanced_nosquare", "balanced_unweighted", "balanced_power",
        "balanced_r0a", "balanced_r0b", "balanced_r0c", "balanced_r0d",
        "defense_first", "fallback",
    ])
    parser.add_argument("--no-symmetry-dedup", action="store_true")
    parser.add_argument("--no-state-dedup", action="store_true")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    cli_cfg = CliConfig(repo_root=repo_root)
    combat_spec = CombatSpec(
        character=args.character,
        encounter=args.encounter,
        seed=args.seed,
        ascension=args.ascension,
        lang=args.lang,
    )

    chance_table = None
    if args.chance_table:
        chance_table = EnemyChanceLookupTable.load(args.chance_table)

    searcher = CombatSearcher(
        cli_cfg,
        combat_spec,
        parallel_top_level=args.parallel_top_level,
        max_workers=args.max_workers,
        reuse_cli_processes=args.reuse_cli_processes,
        chance_table=chance_table,
        score_mode=args.score_mode,
        symmetry_dedup=not args.no_symmetry_dedup,
        state_dedup=not args.no_state_dedup,
    )
    try:
        result = searcher.search(depth=args.depth, chance_depth=args.chance_depth)
    finally:
        searcher.close()

    print(
        json.dumps(
            {
                "encounter": args.encounter,
                "depth": args.depth,
                "chance_depth": args.chance_depth,
                "parallel_top_level": args.parallel_top_level,
                "max_workers": args.max_workers,
                "reuse_cli_processes": args.reuse_cli_processes,
                "chance_table": args.chance_table,
                "score_mode": args.score_mode,
                "symmetry_dedup": not args.no_symmetry_dedup,
                "state_dedup": not args.no_state_dedup,
                "score": result.score,
                "stats": result.stats,
                "cache": {
                    "eval_cache_size": len(searcher.eval_cache),
                    "subtree_cache_size": len(searcher.subtree_cache),
                },
                "timing": {
                    "replay_calls": searcher.timing["replay_calls"],
                    "replay_total_ms": round(searcher.timing["replay_total_ms"], 3),
                    "replay_avg_ms": round(
                        (
                            searcher.timing["replay_total_ms"] / searcher.timing["replay_calls"]
                            if searcher.timing["replay_calls"]
                            else 0.0
                        ),
                        3,
                    ),
                    "replay_ms_samples": [round(x, 3) for x in searcher.timing["replay_ms_samples"]],
                    "branch_ms_samples": [
                        {
                            **sample,
                            "elapsed_ms": round(float(sample["elapsed_ms"]), 3),
                        }
                        for sample in searcher.timing["branch_ms_samples"]
                    ],
                    "chance_lookup_hits": searcher.timing["chance_lookup_hits"],
                    "chance_lookup_misses": searcher.timing["chance_lookup_misses"],
                    "chance_expected_threat_total": round(searcher.timing["chance_expected_threat_total"], 3),
                    "symmetry_pruned_actions": searcher.timing["symmetry_pruned_actions"],
                "state_pruned_actions": searcher.timing["state_pruned_actions"],
                "budget_pruned_actions": searcher.timing["budget_pruned_actions"],
                    "subtree_cache_hits": searcher.timing["subtree_cache_hits"],
                    "eval_cache_hits": searcher.timing["eval_cache_hits"],
                },
                "sequence": [
                    {
                        "action_type": a.action_type,
                        "card_index": a.card_index,
                        "target_index": a.target_index,
                        "metadata": a.metadata,
                    }
                    for a in result.sequence
                ],
                "leaf_state_summary": {
                    "player": (result.leaf_state.get("combat") or {}).get("player"),
                    "enemies": [
                        {
                            "monster_id": e.get("monster_id"),
                            "hp": e.get("hp"),
                            "block": e.get("block"),
                            "intent": e.get("intent"),
                        }
                        for e in ((result.leaf_state.get("combat") or {}).get("enemies") or [])
                    ],
                },
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
