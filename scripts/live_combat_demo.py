from __future__ import annotations

import argparse
import json
from pathlib import Path

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from cli.sts2_mod_adapter import ModClientConfig, Sts2ModAdapter
from controller.combat_step import (
    CombatStepConfig,
    DEFAULT_LIVE_SEARCH_BUDGET_MS,
    DEFAULT_TURN_ACTION_CAP,
    PlanState,
    decide_combat_action,
)
from controller.engine_parity import (
    client_combat_checkpoint,
    compare_checkpoints,
    headless_combat_checkpoint,
)
from controller.live_client_bridge import JsonlSessionLog, LiveClientBridge
from controller.search.combat_search import CombatSpec
from controller.run_agent import choose_map_route_global


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drive the visible client with the existing combat search from a map save"
    )
    parser.add_argument("--map-save", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--encounter", default="TOADPOLES_WEAK")
    parser.add_argument("--depth", type=int, default=DEFAULT_TURN_ACTION_CAP,
                        help="Maximum player actions expanded before the first enemy turn")
    parser.add_argument("--chance-depth", type=int, default=1)
    parser.add_argument("--max-search-ms", type=float, default=DEFAULT_LIVE_SEARCH_BUDGET_MS)
    parser.add_argument("--max-actions", type=int, default=30)
    parser.add_argument("--visible-delay-ms", type=float, default=700.0)
    parser.add_argument(
        "--resume-report",
        type=Path,
        help="Replay already-visible actions into the shadow runtime before continuing",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    args.session_dir.mkdir(parents=True, exist_ok=True)
    log = JsonlSessionLog(args.session_dir / "session.jsonl")
    mod = Sts2ModAdapter(ModClientConfig(base_url=args.url))
    live = LiveClientBridge(mod, log, args.visible_delay_ms)
    client_state = live.observe()
    if client_state.get("screen") != "COMBAT":
        raise RuntimeError(
            f"Visible client must be at the first combat decision; got {client_state.get('screen')!r}"
        )

    cli = Sts2CliAdapter(CliConfig(repo_root=repo_root))
    cli.start()
    plan = PlanState()
    action_rows = []
    try:
        headless_state = cli.load_save(str(args.map_save.resolve()), lang="en")
        headless_map = cli.get_map()
        selected_node, _ = choose_map_route_global(headless_state, headless_map)
        headless_state = cli.action("select_map_node", selected_node)
        if headless_state.get("type") == "error":
            raise RuntimeError(f"Failed to enter headless combat: {headless_state}")

        if args.resume_report:
            previous = json.loads(args.resume_report.read_text(encoding="utf-8"))
            prior_actions = [dict(row) for row in previous.get("actions") or []]
            for row in prior_actions:
                headless_state = cli.action(
                    str(row["action"]), dict(row.get("args") or {}), timeout_s=15.0
                )
                if headless_state.get("type") == "error":
                    raise RuntimeError(f"Failed to replay prior action: {headless_state}")
            log.write(
                {
                    "event": "shadow_resume",
                    "replayed_actions": len(previous.get("actions") or []),
                }
            )
            action_rows.extend(prior_actions)

        run_id = str(client_state.get("run_id") or "")
        first_sequence = len(action_rows) + 1
        for sequence in range(first_sequence, args.max_actions + 1):
            search_result = cli.get_search_state(timeout_s=10.0)
            search_state = search_result.get("combat_state_for_search") or {}
            if not search_state:
                raise RuntimeError(f"Incomplete headless search state: {search_result}")
            before = compare_checkpoints(
                client_combat_checkpoint(client_state),
                headless_combat_checkpoint(headless_state, search_state, run_id),
            )
            _log_parity(log, sequence, "before_action", before)
            if before.status != "PASS":
                raise RuntimeError(f"Pre-action parity failed: {before.differences[:5]}")

            context = headless_state.get("context") or {}
            cfg = CombatStepConfig(
                cli_cfg=CliConfig(repo_root=repo_root),
                spec=CombatSpec(
                    character="Ironclad",
                    encounter=args.encounter,
                    seed=run_id,
                    ascension=0,
                    lang="en",
                ),
                depth=args.depth,
                chance_depth=args.chance_depth,
                score_mode="preference",
                max_workers=2,
                reuse_cli_processes=False,
                floor=context.get("floor"),
                room_type=context.get("room_type"),
                capture_root_topk=5,
                max_search_ms=args.max_search_ms,
            )
            decision = decide_combat_action(cli, search_state, cfg, plan)
            telemetry = {
                "policy": "bounded_combat_search",
                "search_ms": round(float(decision.timing.get("search_ms") or 0.0), 3),
                "nodes": decision.nodes,
                "score": decision.search_score,
                "reused_plan": decision.reused_plan,
                "time_budget_ms": args.max_search_ms,
                "time_budget_exhausted": bool(
                    decision.searcher_timing_summary.get("time_budget_exhausted")
                ),
                "chosen": decision.chosen_summary,
                "root_candidates": decision.root_candidates,
            }
            telemetry['decision_reason'] = getattr(decision, 'decision_reason', 'fallback' if getattr(decision, 'fell_back', False) else 'highest_score')
            telemetry['decision_audit'] = getattr(decision, 'decision_audit', {})
            telemetry['score_explanation'] = getattr(decision, 'score_explanation', {})
            print(
                json.dumps(
                    {
                        "sequence": sequence,
                        "action": decision.action,
                        "args": decision.payload,
                        **telemetry,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

            client_after = live.execute_headless_action(
                "combat_play",
                decision.action,
                decision.payload,
                expected_client_state=client_state,
                decision_telemetry=telemetry,
            )
            headless_after = cli.action(decision.action, decision.payload, timeout_s=15.0)
            if headless_after.get("type") == "error":
                raise RuntimeError(f"Headless action failed: {headless_after}")

            action_rows.append(
                {
                    "sequence": sequence,
                    "action": decision.action,
                    "args": decision.payload,
                    **telemetry,
                    "client_action_ms": round(mod.last_call_ms, 3),
                }
            )
            client_state = client_after
            headless_state = headless_after

            client_in_combat = bool(client_state.get("in_combat"))
            headless_in_combat = headless_state.get("decision") == "combat_play"
            if client_in_combat != headless_in_combat:
                raise RuntimeError(
                    "Combat terminal state diverged: "
                    f"client_in_combat={client_in_combat}, "
                    f"headless_decision={headless_state.get('decision')!r}"
                )
            if client_in_combat:
                verified_search = cli.get_search_state(timeout_s=10.0).get(
                    "combat_state_for_search"
                ) or {}
                if not verified_search:
                    raise RuntimeError("Incomplete headless search state after action")
                after_comparison = compare_checkpoints(
                    client_combat_checkpoint(client_state),
                    headless_combat_checkpoint(headless_state, verified_search, run_id),
                )
                _log_parity(log, sequence, "after_action", after_comparison)
                if after_comparison.status != "PASS":
                    raise RuntimeError(
                        f"Post-action parity failed: {after_comparison.differences[:5]}"
                    )
                live.pace_after_verification(verification='combat_action_checkpoint')
            if not client_in_combat:
                live.pace_after_verification(verification='terminal_boundary_match')
                report = {
                    "status": "PASS",
                    "combat_completed": True,
                    "actions": action_rows,
                    "client_screen": client_state.get("screen"),
                    "headless_decision": headless_state.get("decision"),
                }
                _write_report(args.session_dir, report)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return

        report = {
            "status": "INCONCLUSIVE",
            "combat_completed": False,
            "reason": "max_actions reached",
            "actions": action_rows,
        }
        _write_report(args.session_dir, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(3)
    except Exception as exc:
        report = {
            "status": "FAIL",
            "combat_completed": False,
            "error": str(exc),
            "actions": action_rows,
        }
        _write_report(args.session_dir, report)
        raise
    finally:
        cli.stop()


def _log_parity(log: JsonlSessionLog, sequence: int, phase: str, result: object) -> None:
    log.write(
        {
            "event": "parity_checkpoint",
            "sequence": sequence,
            "phase": phase,
            "status": result.status,
            "client_digest": result.client_digest,
            "headless_digest": result.headless_digest,
            "difference_count": len(result.differences),
            "differences": result.differences,
        }
    )


def _write_report(session_dir: Path, report: dict) -> None:
    (session_dir / "combat_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
