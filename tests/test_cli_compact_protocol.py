from pathlib import Path

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_search_compact_protocol_preserves_full_default_responses():
    cli = Sts2CliAdapter(CliConfig(repo_root=REPO_ROOT))
    cli.start()
    try:
        started = cli.start_test_combat(
            encounter="SHRINKER_BEETLE_WEAK", seed="compact-protocol-test"
        )
        assert started.get("decision") == "combat_play"
        captured = cli.capture_combat_snapshot("compact_protocol_root")
        assert captured.get("success") is True

        full_restore = cli.restore_combat_snapshot("compact_protocol_root")
        full_restore_bytes = cli.last_response_bytes
        assert "player" in full_restore
        assert "hand" in full_restore

        compact_restore = cli.restore_combat_snapshot(
            "compact_protocol_root", compact=True
        )
        compact_restore_bytes = cli.last_response_bytes
        assert compact_restore.get("compact") is True
        assert compact_restore.get("restore_mode") in {"in_place", "full"}
        assert "restore_timing_ms" in compact_restore
        assert "player" not in compact_restore
        assert "hand" not in compact_restore
        assert compact_restore_bytes < full_restore_bytes / 4

        compact_action = cli.action("end_turn", compact=True)
        compact_action_bytes = cli.last_response_bytes
        assert compact_action.get("compact") is True
        assert compact_action.get("decision") == "combat_play"
        assert "headless_execute_ms" in compact_action
        assert "headless_wait_profile" in compact_action
        assert compact_action["headless_wait_profile"]["wait_calls"] >= 1
        assert compact_action["headless_wait_profile"]["wait_iterations"] >= 0
        assert "player" not in compact_action
        assert "hand" not in compact_action

        cli.restore_combat_snapshot("compact_protocol_root", compact=True)
        full_action = cli.action("end_turn")
        full_action_bytes = cli.last_response_bytes
        assert full_action.get("decision") == "combat_play"
        assert "player" in full_action
        assert "hand" in full_action
        assert compact_action_bytes < full_action_bytes / 4

        # Compact mode must fall back to the complete payload when an action
        # leaves combat_play; terminal construction consumes player/enemy data.
        cli.restore_combat_snapshot("compact_protocol_root", compact=True)
        configured = cli.send({
            "cmd": "configure_sandbox",
            "hp": 80,
            "energy": 3,
            "hand": ["STRIKE_IRONCLAD"],
            "enemy_hp": [1],
        })
        assert configured.get("decision") == "combat_play"
        state = cli.get_search_state()["combat_state_for_search"]
        strike = next(
            action for action in state["combat"]["available_actions"]
            if (action.get("metadata") or {}).get("card_id") == "STRIKE_IRONCLAD"
        )
        terminal = cli.action(
            "play_card",
            {
                "card_index": strike["card_index"],
                "target_index": strike["target_index"],
            },
            compact=True,
        )
        assert terminal.get("decision") != "combat_play"
        assert terminal.get("compact") is not True
        assert "player" in terminal
    finally:
        cli.stop()
