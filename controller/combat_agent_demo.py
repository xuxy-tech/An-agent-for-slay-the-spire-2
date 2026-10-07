from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.combat_intent import intent_deals_damage, intent_total_damage
from controller.search.actions import available_actions_from_search_state, cli_payload_for_action
from controller.search.enemy_chance_model import EnemyChanceLookupTable
from controller.search.combat_search import CombatSearcher, CombatSpec, RecordedAction


def _combat_summary(state_result: dict) -> dict:
    search_state = state_result.get("combat_state_for_search") or {}
    combat = search_state.get("combat") or {}
    player = combat.get("player") or {}
    enemies = combat.get("enemies") or []
    return {
        "schema": search_state.get("schema_version"),
        "decision": state_result.get("decision"),
        "player": {
            "hp": player.get("hp"),
            "max_hp": player.get("max_hp"),
            "block": player.get("block"),
            "energy": player.get("energy"),
        },
        "turn_number": combat.get("turn_number"),
        "round_number": combat.get("round_number"),
        "enemy_hp": [e.get("hp") for e in enemies],
        "available_actions": [a.get("action_type") for a in (combat.get("available_actions") or [])],
    }


def _should_parallelize(search_state: dict, user_requested_parallel: bool) -> bool:
    if user_requested_parallel:
        return True
    combat = search_state.get("combat") or {}
    enemies = combat.get("enemies") or []
    legal_actions = available_actions_from_search_state(search_state)
    incoming_damage = 0
    for enemy in enemies:
        intent = enemy.get("intent") or {}
        if not intent_deals_damage(intent):
            continue
        incoming_damage += int(intent_total_damage(intent))
    # Match the run-level agent heuristic: top-level parallelism only pays off on
    # dense states. Simpler combats are faster left serial.
    return len(enemies) >= 3 and len(legal_actions) >= 10 and incoming_damage >= 15


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a combat-only replanning agent")
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
    parser.add_argument("--max-actions", type=int, default=40)
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

    live_cli = Sts2CliAdapter(cli_cfg)
    live_cli.start()
    live_cli.start_test_combat(
        character=args.character,
        encounter=args.encounter,
        seed=args.seed,
        ascension=args.ascension,
        lang=args.lang,
    )

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

    history: list[RecordedAction] = []
    try:
        for step_id in range(1, args.max_actions + 1):
            current = live_cli.get_search_state()
            print(
                json.dumps(
                    {
                        "event": "state",
                        "step_id": step_id,
                        "summary": _combat_summary(current),
                    },
                    ensure_ascii=False,
                )
            )

            search_state = current.get("combat_state_for_search") or {}
            if not search_state.get("success"):
                print(json.dumps({"event": "stop", "reason": "search_state_not_success"}, ensure_ascii=False))
                break

            searcher.parallel_top_level = _should_parallelize(
                search_state,
                user_requested_parallel=args.parallel_top_level,
            )
            search_started = time.perf_counter()
            result = searcher.search_from_history(history, depth=args.depth, chance_depth=args.chance_depth)
            search_wall_ms = (time.perf_counter() - search_started) * 1000.0
            if not result.sequence:
                print(json.dumps({"event": "stop", "reason": "no_action", "score": result.score}, ensure_ascii=False))
                break

            chosen = result.sequence[0]
            action_name, payload = cli_payload_for_action(chosen)
            live_cli.action(action_name, args=payload, with_snapshot=False)
            history.append(RecordedAction(action_name, payload))

            after_result = live_cli.get_search_state()
            after = after_result.get("combat_state_for_search") or {}
            print(
                json.dumps(
                    {
                        "event": "step",
                        "step_id": step_id,
                        "search_score": result.score,
                        "search_wall_ms": round(search_wall_ms, 3),
                        "parallel_top_level": searcher.parallel_top_level,
                        "chosen_action": {
                            "action_type": chosen.action_type,
                            "card_index": chosen.card_index,
                            "target_index": chosen.target_index,
                            "metadata": chosen.metadata,
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
                        "after": _combat_summary(after_result),
                        "stats": result.stats,
                    },
                    ensure_ascii=False,
                )
            )

            if not after.get("success"):
                print(json.dumps({"event": "stop", "reason": "combat_ended"}, ensure_ascii=False))
                break
    finally:
        searcher.close()
        live_cli.stop()


if __name__ == "__main__":
    main()
