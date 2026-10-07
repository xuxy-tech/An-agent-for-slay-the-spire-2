from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from cli.sts2_cli_adapter import CliConfig
from controller.search.actions import available_actions_from_search_state
from controller.search.combat_search import CombatSearcher, CombatSpec


def action_to_json(action) -> Dict[str, Any]:
    return {
        "action_type": action.action_type,
        "card_index": action.card_index,
        "target_index": action.target_index,
        "metadata": action.metadata,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit whether root search chose the highest-scoring first action")
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument("--encounter")
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
    parser.add_argument("--no-symmetry-dedup", action="store_true")
    parser.add_argument("--no-state-dedup", action="store_true")
    parser.add_argument("--snapshot-json-file")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    cli_cfg = CliConfig(repo_root=repo_root)
    snapshot_json = None
    snapshot_meta: Dict[str, Any] = {}
    if args.snapshot_json_file:
        raw = json.loads(Path(args.snapshot_json_file).read_text())
        snapshot_json = raw["snapshot_json"] if isinstance(raw, dict) and "snapshot_json" in raw else json.dumps(raw, ensure_ascii=False)
        snapshot_meta = raw if isinstance(raw, dict) else {}
    encounter_id = args.encounter or str(snapshot_meta.get("encounter_id") or "")
    if not encounter_id:
        raise SystemExit("Provide --encounter or --snapshot-json-file containing encounter_id")
    combat_spec = CombatSpec(character=args.character, encounter=encounter_id, seed=args.seed)
    def make_searcher() -> CombatSearcher:
        return CombatSearcher(
            cli_cfg,
            combat_spec,
            reuse_cli_processes=args.reuse_cli_processes,
            score_mode=args.score_mode,
            symmetry_dedup=not args.no_symmetry_dedup,
            state_dedup=not args.no_state_dedup,
            root_snapshot_id="audit_root_snapshot" if snapshot_json is not None else None,
            root_snapshot_json=snapshot_json,
        )

    searcher = make_searcher()
    try:
        captured_root = snapshot_meta.get("search_state")
        if isinstance(captured_root, dict) and captured_root.get("success") is not None:
            root = captured_root
        else:
            root_result = searcher.combat_to_state([])
            root = searcher._extract_search_state(root_result)
        searcher._root_summary = searcher._combat_summary(root)
        searcher.eval_cache.clear()
        searcher.subtree_cache.clear()
        action_budget = args.depth
        pre_chance_budget = searcher._initial_pre_chance_budget(args.depth, args.chance_depth)
        result = searcher._search_from_state(root, [], action_budget, args.chance_depth, pre_chance_budget)
        all_actions = available_actions_from_search_state(root)
        candidates = searcher._prepare_action_candidates(root, [], all_actions)
        audited: List[Dict[str, Any]] = []
        best_score = None
        for c in candidates:
            candidate_searcher = make_searcher()
            try:
                candidate_searcher._root_summary = searcher._combat_summary(root)
                child = candidate_searcher._evaluate_child_action(
                    c["action"],
                    root,
                    [],
                    action_budget,
                    args.chance_depth,
                    pre_chance_budget,
                    c["next_history"],
                    c["child_state"],
                )
            finally:
                candidate_searcher.close()
            item = {
                "action": action_to_json(c["action"]),
                "score": child.score,
                "effect_score": c["effect_score"],
                "sequence": [action_to_json(a) for a in child.sequence],
            }
            audited.append(item)
            if best_score is None or child.score > best_score:
                best_score = child.score
        chosen_first = action_to_json(result.sequence[0]) if result.sequence else None
        chosen_first_rows = [row for row in audited if row["action"] == chosen_first]
        chosen_first_action_score = chosen_first_rows[0]["score"] if chosen_first_rows else None
        argmax_actions = [row for row in audited if abs(float(row["score"]) - float(best_score or 0.0)) < 1e-9]
        chosen_is_argmax = any(row["action"] == chosen_first for row in argmax_actions)
        print(json.dumps({
            "encounter": args.encounter,
            "chosen_first_action": chosen_first,
            "chosen_first_action_score": chosen_first_action_score,
            "root_sequence_score": result.score,
            "best_first_action_score": best_score,
            "chosen_is_argmax": chosen_is_argmax,
            "num_argmax_actions": len(argmax_actions),
            "argmax_actions": argmax_actions,
            "all_first_actions": audited,
        }, ensure_ascii=False))
    finally:
        searcher.close()


if __name__ == "__main__":
    main()
