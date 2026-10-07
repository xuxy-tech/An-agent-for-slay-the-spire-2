from __future__ import annotations

import argparse
import json
import time
import hashlib
from pathlib import Path

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from cli.sts2_mod_adapter import ModClientConfig, Sts2ModAdapter
from controller.engine_parity import (
    client_map_checkpoint,
    compare_checkpoints,
    headless_map_checkpoint,
    client_reward_checkpoint,
    headless_reward_checkpoint,
    is_card_reward_selection,
)
from controller.live_client_bridge import JsonlSessionLog, LiveClientBridge, gameplay_observation
from controller.run_agent import choose_card_reward, choose_map_route_global


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve one visible reward from a replayed shadow combat"
    )
    parser.add_argument("--map-save", type=Path, required=True)
    parser.add_argument("--combat-report", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--visible-delay-ms", type=float, default=700.0)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    args.session_dir.mkdir(parents=True, exist_ok=True)
    log = JsonlSessionLog(args.session_dir / "session.jsonl")
    mod = Sts2ModAdapter(ModClientConfig(base_url=args.url))
    live = LiveClientBridge(mod, log, args.visible_delay_ms)
    client_before = live.observe()
    if client_before.get("screen") != "REWARD" and not is_card_reward_selection(client_before):
        raise RuntimeError(f"Visible client is not at REWARD: {client_before.get('screen')!r}")

    cli = Sts2CliAdapter(CliConfig(repo_root=repo_root))
    cli.start()
    try:
        state = cli.load_save(str(args.map_save.resolve()), lang="en")
        source = json.loads(args.combat_report.read_text(encoding="utf-8"))
        rows = source.get('actions') or []
        dashboard = source.get('schema_version') == 2
        if dashboard:
            identity = source.get('identity') or {}
            if identity.get('run_id') != client_before.get('run_id'):
                raise RuntimeError('Live run identity differs from replay report')
            if identity.get('anchor_sha256') != hashlib.sha256(args.map_save.read_bytes()).hexdigest():
                raise RuntimeError('Anchor does not match replay report')
            if source.get('client_only_segments'):
                raise RuntimeError('Client-only segments require their own re-anchor replay')
            completed = [row for row in rows if row.get('status') == 'completed']
            if not completed or gameplay_observation(completed[-1].get('client_after') or {}) != gameplay_observation(client_before):
                raise RuntimeError('Client has changed since the last recorded action')
            rows = [row for row in completed if row.get('headless_action')]
        else:
            node, _ = choose_map_route_global(state, cli.get_map())
            state = cli.action('select_map_node', node)
        for row in rows:
            state = cli.action(str(row['headless_action' if dashboard else 'action']),
                               dict(row.get('headless_args' if dashboard else 'args') or {}), timeout_s=15.0)
            if state.get("type") == "error":
                raise RuntimeError(f"Shadow replay failed: {state}")
        if state.get("decision") != "card_reward":
            raise RuntimeError(f"Shadow is not at card_reward: {state.get('decision')!r}")

        client_before = live.reveal_card_reward(client_before)
        reward_comparison = compare_checkpoints(client_reward_checkpoint(client_before), headless_reward_checkpoint(state))
        log.write({'event': 'parity_checkpoint', 'phase': 'reward_options',
                   'status': reward_comparison.status, 'differences': reward_comparison.differences})
        if reward_comparison.status != 'PASS':
            raise RuntimeError(f'Reward options differ: {reward_comparison.differences}')

        started = time.perf_counter()
        choice = choose_card_reward(state, repo_root)
        decision_ms = (time.perf_counter() - started) * 1000.0
        if choice is None:
            action, payload = "skip_card_reward", {}
        else:
            action, payload = "select_card_reward", choice
        telemetry = {
            "policy": "existing_card_reward_heuristic",
            "decision_ms": round(decision_ms, 3),
            "offered": [
                {"index": card.get("index"), "id": card.get("id")}
                for card in state.get("cards") or []
            ],
            "chosen": payload,
        }
        client_after = live.execute_headless_action(
            "card_reward",
            action,
            payload,
            expected_client_state=client_before,
            decision_telemetry=telemetry,
        )
        headless_after = cli.action(action, payload, timeout_s=20.0)
        if headless_after.get("type") == "error":
            raise RuntimeError(f"Headless reward action failed: {headless_after}")
        headless_map = cli.get_map()
        comparison = compare_checkpoints(
            client_map_checkpoint(client_after),
            headless_map_checkpoint(
                headless_after,
                headless_map,
                run_id=str(client_after.get("run_id") or ""),
            ),
        )
        log.write(
            {
                "event": "parity_checkpoint",
                "phase": "post_reward_map",
                "status": comparison.status,
                "client_digest": comparison.client_digest,
                "headless_digest": comparison.headless_digest,
                "differences": comparison.differences,
            }
        )
        if comparison.status != "PASS":
            raise RuntimeError(f"Post-reward map parity failed: {comparison.differences[:5]}")
        live.pace_after_verification(verification='post_reward_map_checkpoint')

        report = {
            "status": "PASS",
            "action": action,
            "payload": payload,
            "decision_ms": round(decision_ms, 3),
            "offered": telemetry["offered"],
            "map_digest": comparison.client_digest,
            "run_id": client_after.get('run_id'),
            "client_after": client_after,
            "verification_scope": "COVERED_FIELDS_ONLY",
            "checkpoint_save": None,
            "checkpoint_note": (
                "The official client can delay current_run.save writes after rewards; "
                "continue with one persistent shadow process instead of copying a stale file."
            ),
        }
        (args.session_dir / "reward_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        cli.stop()


if __name__ == "__main__":
    main()
