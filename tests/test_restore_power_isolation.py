import json
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.rng_parity import compare_rng_snapshots


ROOT = Path(__file__).resolve().parents[1]
ARCHIVED_STUN_SNAPSHOT = (
    ROOT
    / 'data' / 'human_play' / 'raw'
    / 'human_b561826d7f994592a1564785bb95363f'
    / 'snapshots' / 'combat_df7045f13d4b4396af20a5802c96a23e.json'
)
INTERMEDIATE_RESTORE_SNAPSHOT = (
    ROOT
    / 'data' / 'human_play' / 'raw'
    / 'human_b561826d7f994592a1564785bb95363f'
    / 'snapshots' / 'combat_d043a0010a2042ccac1b1646205f60d9.json'
)


def _player_power(cli, power_id):
    inspected = cli.send({'cmd': 'inspect_power_state'})
    return next(
        row for row in inspected['powers']
        if row['owner'] == 'player' and row['id'] == power_id
    )


def _enemy_ai_summary(cli):
    inspected = cli.send({'cmd': 'inspect_enemy_ai'})
    assert inspected.get('success') is True
    return [
        {
            'monster_id': enemy['monster_id'],
            'current_state_id': enemy['current_state_id'],
            'next_move_id': enemy['next_move_id'],
        }
        for enemy in inspected['enemies']
    ]


def test_archived_stunned_enemy_snapshot_restores_and_replays_without_stall():
    snapshot_json = ARCHIVED_STUN_SNAPSHOT.read_text(encoding='utf-8')
    cli = Sts2CliAdapter(CliConfig(repo_root=ROOT))
    cli.start()
    try:
        assert cli.import_combat_snapshot(
            snapshot_json, 'archived_stun_root'
        ).get('success')

        cold = cli.restore_combat_snapshot('archived_stun_root', compact=True)
        assert cold.get('restore_mode') == 'full'
        expected_ai = _enemy_ai_summary(cli)
        assert expected_ai[0] == {
            'monster_id': 'BOWLBUG_ROCK',
            'current_state_id': 'STUNNED',
            'next_move_id': 'STUNNED',
        }

        for _ in range(3):
            restored = cli.restore_combat_snapshot(
                'archived_stun_root', compact=True
            )
            assert restored.get('restore_mode') == 'in_place'
            assert _enemy_ai_summary(cli) == expected_ai

            ended = cli.action('end_turn', compact=True)
            assert ended.get('decision') == 'combat_play'
            wait_profile = ended.get('headless_wait_profile') or {}
            assert wait_profile.get('end_turn_stalls') == 0
            assert wait_profile.get('end_turn_pump_iterations') == 0
    finally:
        cli.stop()


def test_enemy_move_callbacks_rebind_after_full_then_warm_restore():
    snapshot_json = ARCHIVED_STUN_SNAPSHOT.read_text(encoding='utf-8')
    cli = Sts2CliAdapter(CliConfig(repo_root=ROOT))
    cli.start()
    try:
        assert cli.import_combat_snapshot(
            snapshot_json, 'callback_archive_root'
        ).get('success')
        assert cli.restore_combat_snapshot(
            'callback_archive_root', compact=True
        ).get('restore_mode') == 'full'
        assert cli.capture_combat_snapshot('callback_live_root').get('success')

        state = cli.get_search_state()['combat_state_for_search']
        hemokinesis = next(
            action for action in state['combat']['available_actions']
            if (action.get('metadata') or {}).get('card_id') == 'HEMOKINESIS'
            and action.get('target_index') == 1
        )
        cli.action('play_card', {
            'card_index': hemokinesis['card_index'],
            'target_index': hemokinesis['target_index'],
        })
        assert cli.capture_combat_snapshot('callback_child').get('success')

        # Reintroduce the killed enemy, then restoring the child must rebuild
        # the combat fully. The child's captured static move callbacks still
        # point at the pre-rebuild monsters and must not be reused afterwards.
        assert cli.restore_combat_snapshot(
            'callback_live_root', compact=True
        ).get('restore_mode') == 'in_place'
        cold = cli.restore_combat_snapshot('callback_child', compact=True)
        assert cold.get('restore_mode') == 'full'
        cold_end = cli.action('end_turn', compact=True)
        assert cold_end['headless_wait_profile']['end_turn_stalls'] == 0
        cold_state = cli.get_search_state()['combat_state_for_search']

        hot = cli.restore_combat_snapshot('callback_child', compact=True)
        assert hot.get('restore_mode') == 'in_place'
        hot_end = cli.action('end_turn', compact=True)
        assert hot_end['headless_wait_profile']['end_turn_stalls'] == 0
        hot_state = cli.get_search_state()['combat_state_for_search']

        assert hot_state == cold_state
    finally:
        cli.stop()


def test_in_place_restore_rebuilds_power_objects_and_preserves_vigor_semantics():
    cli = Sts2CliAdapter(CliConfig(repo_root=ROOT))
    cli.start()
    try:
        started = cli.start_test_combat(
            character='Ironclad', encounter='CORPSE_SLUGS_WEAK',
            seed='restore-power-isolation', ascension=0, lang='en',
        )
        assert started.get('decision') == 'combat_play'
        state = cli.get_search_state()['combat_state_for_search']
        enemy_count = len(state['combat']['enemies'])
        configured = cli.send({
            'cmd': 'configure_sandbox', 'hp': 80, 'energy': 3,
            'hand': ['STRIKE_IRONCLAD', 'DEFEND_IRONCLAD'],
            'enemy_hp': [100] * enemy_count,
        })
        assert configured.get('decision') == 'combat_play'

        assert cli.capture_combat_snapshot('power_isolation_base').get('success')
        exported = cli.export_combat_snapshot('power_isolation_base')
        envelope = json.loads(exported['snapshot_json'])
        player_creature = next(
            creature for creature in envelope['NetState']['Creatures']
            if creature.get('playerId') is not None
        )
        player_creature['powers'] = [{
            'id': {'Category': 'POWER', 'Entry': 'VIGOR_POWER'},
            'amount': 8,
        }]
        assert cli.import_combat_snapshot(
            json.dumps(envelope), 'power_isolation_root'
        ).get('success')

        first_restore = cli.restore_combat_snapshot('power_isolation_root', compact=True)
        assert first_restore.get('restore_mode') == 'in_place'
        first_power = _player_power(cli, 'VIGOR_POWER')

        current = cli.get_search_state()['combat_state_for_search']
        defend = next(
            action for action in current['combat']['available_actions']
            if (action.get('metadata') or {}).get('card_id') == 'DEFEND_IRONCLAD'
        )
        cli.action('play_card', {'card_index': defend['card_index']})
        assert _player_power(cli, 'VIGOR_POWER')['object_id'] == first_power['object_id']

        second_restore = cli.restore_combat_snapshot('power_isolation_root', compact=True)
        assert second_restore.get('restore_mode') == 'in_place'
        second_power = _player_power(cli, 'VIGOR_POWER')
        assert second_power['object_id'] != first_power['object_id']
        assert second_power['amount'] == 8

        current = cli.get_search_state()['combat_state_for_search']
        before_hp = current['combat']['enemies'][0]['hp']
        strike = next(
            action for action in current['combat']['available_actions']
            if (action.get('metadata') or {}).get('card_id') == 'STRIKE_IRONCLAD'
        )
        cli.action('play_card', {
            'card_index': strike['card_index'],
            'target_index': strike['target_index'],
        })
        after = cli.get_search_state()['combat_state_for_search']
        assert before_hp - after['combat']['enemies'][0]['hp'] == 14
    finally:
        cli.stop()


def test_in_place_restore_reinstates_complete_rng_state_after_perturbation():
    cli = Sts2CliAdapter(CliConfig(repo_root=ROOT))
    cli.start()
    try:
        started = cli.start_test_combat(
            character='Ironclad', encounter='CORPSE_SLUGS_WEAK',
            seed='restore-rng-isolation', ascension=0, lang='en',
        )
        assert started.get('decision') == 'combat_play'
        assert cli.capture_combat_snapshot('rng_isolation_root').get('success')
        expected = cli.get_rng_snapshot()
        assert expected['complete'] is True

        stream_name = sorted(expected['run_streams'])[0]
        original_seed = expected['run_streams'][stream_name]['seed']
        replacement_seed = (int(original_seed) ^ 0x5A17C9E3) & 0x7FFFFFFF
        changed = cli.reseed_rng_stream({stream_name: replacement_seed})
        assert stream_name in changed['run_streams_changed']
        assert not compare_rng_snapshots(expected, cli.get_rng_snapshot()).passed

        for _ in range(2):
            restored = cli.restore_combat_snapshot('rng_isolation_root', compact=True)
            assert restored.get('restore_mode') == 'in_place'
            actual = cli.get_rng_snapshot()
            assert compare_rng_snapshots(expected, actual).passed
            assert actual['digest_sha256'] == expected['digest_sha256']
            cli.reseed_rng_stream({stream_name: replacement_seed})
    finally:
        cli.stop()


def test_intermediate_snapshot_restores_history_hooks_and_subscriptions_after_sibling_churn():
    snapshot_json = INTERMEDIATE_RESTORE_SNAPSHOT.read_text(encoding='utf-8')
    cli = Sts2CliAdapter(CliConfig(repo_root=ROOT))
    cli.start()
    try:
        assert cli.import_combat_snapshot(snapshot_json, 'intermediate_root').get('success')
        assert cli.restore_combat_snapshot('intermediate_root', compact=True).get('restore_mode') == 'full'

        def play(card_id, target_index=None):
            state = cli.get_search_state()['combat_state_for_search']
            action = next(
                row for row in state['combat']['available_actions']
                if row.get('action_type') == 'play_card'
                and (row.get('metadata') or {}).get('card_id') == card_id
                and (target_index is None or row.get('target_index') == target_index)
            )
            args = {'card_index': action['card_index']}
            if action.get('target_index') is not None:
                args['target_index'] = action['target_index']
            result = cli.action('play_card', args, compact=True)
            assert result.get('type') != 'error'

        play('STRIKE_IRONCLAD', 2)
        assert cli.capture_combat_snapshot('intermediate_parent').get('success')
        play('DEFEND_IRONCLAD')
        expected = cli.capture_combat_snapshot('intermediate_expected')
        expected_state = cli.get_search_state()['combat_state_for_search']
        expected_runtime = cli.send({'cmd': 'inspect_combat_runtime', 'depth': 5})['leaves']

        assert cli.restore_combat_snapshot('intermediate_root', compact=True).get('restore_mode') == 'in_place'
        play('HEMOKINESIS', 0)
        play('DEFEND_IRONCLAD')
        restored = cli.restore_combat_snapshot('intermediate_parent', compact=True)
        assert restored.get('restore_mode') == 'in_place'
        play('DEFEND_IRONCLAD')
        actual = cli.capture_combat_snapshot('intermediate_actual')
        actual_state = cli.get_search_state()['combat_state_for_search']
        actual_runtime = cli.send({'cmd': 'inspect_combat_runtime', 'depth': 5})['leaves']

        assert actual['state_fingerprint'] == expected['state_fingerprint']
        assert actual_state == expected_state
        keys = [
            key for key in expected_runtime
            if '_cardsPlayedThisTurn' in key
            or 'History.Entries.Count' in key
            or 'ContentsChanged' in key
            or 'cardDb._subscriptions.Count' in key
        ]
        assert {key: actual_runtime.get(key) for key in keys} == {
            key: expected_runtime.get(key) for key in keys
        }
    finally:
        cli.stop()


def test_intermediate_snapshot_preserves_resolved_enemy_follow_up_after_end_turn():
    snapshot_json = INTERMEDIATE_RESTORE_SNAPSHOT.read_text(encoding='utf-8')
    cli = Sts2CliAdapter(CliConfig(repo_root=ROOT))
    cli.start()
    try:
        assert cli.import_combat_snapshot(snapshot_json, 'follow_up_root').get('success')
        assert cli.restore_combat_snapshot(
            'follow_up_root', compact=True
        ).get('restore_mode') == 'full'
        # The archived fixture predates the complete runtime-state envelope.
        # Re-capture it once after the full load so both comparison paths use
        # the current snapshot contract rather than testing missing old fields.
        assert cli.capture_combat_snapshot('follow_up_live_root').get('success')

        def play(card_id, target_index=None):
            state = cli.get_search_state()['combat_state_for_search']
            action = next(
                row for row in state['combat']['available_actions']
                if row.get('action_type') == 'play_card'
                and (row.get('metadata') or {}).get('card_id') == card_id
                and (target_index is None or row.get('target_index') == target_index)
            )
            args = {'card_index': action['card_index']}
            if action.get('target_index') is not None:
                args['target_index'] = action['target_index']
            result = cli.action('play_card', args, compact=True)
            assert result.get('type') != 'error'

        def finish_line():
            ended = cli.action('end_turn', compact=True)
            assert ended.get('type') != 'error'
            captured = cli.capture_combat_snapshot('follow_up_leaf')
            return (
                cli.get_search_state()['combat_state_for_search'],
                _enemy_ai_summary(cli),
                captured['state_fingerprint'],
            )

        play('STRIKE_IRONCLAD', 2)
        play('STRIKE_IRONCLAD', 2)
        play('HEMOKINESIS', 2)
        expected = finish_line()

        assert cli.restore_combat_snapshot(
            'follow_up_live_root', compact=True
        ).get('restore_mode') == 'in_place'
        play('STRIKE_IRONCLAD', 2)
        assert cli.capture_combat_snapshot('follow_up_parent_1').get('success')
        play('DEFEND_IRONCLAD')
        assert cli.restore_combat_snapshot(
            'follow_up_parent_1', compact=True
        ).get('restore_mode') == 'in_place'

        play('STRIKE_IRONCLAD', 2)
        assert cli.capture_combat_snapshot('follow_up_parent_2').get('success')
        assert cli.restore_combat_snapshot(
            'follow_up_live_root', compact=True
        ).get('restore_mode') == 'in_place'
        play('HEMOKINESIS', 0)
        assert cli.restore_combat_snapshot(
            'follow_up_parent_2', compact=True
        ).get('restore_mode') == 'in_place'

        play('HEMOKINESIS', 2)
        assert cli.capture_combat_snapshot('follow_up_parent_3').get('success')
        assert cli.restore_combat_snapshot(
            'follow_up_parent_1', compact=True
        ).get('restore_mode') == 'in_place'
        play('DEFEND_IRONCLAD')
        assert cli.restore_combat_snapshot(
            'follow_up_parent_3', compact=True
        ).get('restore_mode') == 'in_place'

        assert finish_line() == expected
    finally:
        cli.stop()


@pytest.mark.parametrize('force_full', [False, True])
def test_combat_snapshot_round_trips_card_enchantment_for_full_and_warm_restore(
        monkeypatch, force_full):
    if force_full:
        monkeypatch.setenv('STS2_FORCE_FULL_RESTORE', '1')
    else:
        monkeypatch.delenv('STS2_FORCE_FULL_RESTORE', raising=False)
    cli = Sts2CliAdapter(CliConfig(repo_root=ROOT))
    cli.start()
    try:
        started = cli.start_test_combat(
            character='Ironclad', encounter='CORPSE_SLUGS_WEAK',
            seed='restore-card-enchantment', ascension=0, lang='en',
        )
        assert started.get('decision') == 'combat_play'
        state = cli.get_search_state()['combat_state_for_search']
        enemy_count = len(state['combat']['enemies'])
        configured = cli.send({
            'cmd': 'configure_sandbox', 'hp': 80, 'energy': 3,
            'hand': ['STRIKE_IRONCLAD'], 'enemy_hp': [100] * enemy_count,
        })
        assert configured.get('decision') == 'combat_play'

        assert cli.capture_combat_snapshot('enchantment_unmodified').get('success')
        exported = cli.export_combat_snapshot('enchantment_unmodified')
        envelope = json.loads(exported['snapshot_json'])
        card_state = next(
            card_state
            for player in envelope['NetState']['Players']
            for pile in player['piles']
            for card_state in pile['cards']
            if card_state['card']['id']['Entry'] == 'STRIKE_IRONCLAD'
        )
        card_state['card']['enchantment'] = {
            'id': {'Category': 'ENCHANTMENT', 'Entry': 'SHARP'},
            'amount': 2,
            'props': None,
        }
        assert cli.import_combat_snapshot(
            json.dumps(envelope), 'enchantment_root'
        ).get('success')

        round_trip = json.loads(cli.export_combat_snapshot('enchantment_root')['snapshot_json'])
        restored_card = next(
            card_state
            for player in round_trip['NetState']['Players']
            for pile in player['piles']
            for card_state in pile['cards']
            if card_state['card']['id']['Entry'] == 'STRIKE_IRONCLAD'
        )
        assert restored_card['card']['enchantment']['id']['Entry'] == 'SHARP'
        assert restored_card['card']['enchantment']['amount'] == 2

        expected_mode = 'full' if force_full else 'in_place'
        for attempt in range(2):
            restored = cli.restore_combat_snapshot('enchantment_root', compact=True)
            assert restored.get('restore_mode') == expected_mode
            if attempt == 0:
                assert cli.capture_combat_snapshot('enchantment_recaptured').get('success')
                recaptured = json.loads(
                    cli.export_combat_snapshot('enchantment_recaptured')['snapshot_json']
                )
                recaptured_card = next(
                    card_state
                    for player in recaptured['NetState']['Players']
                    for pile in player['piles']
                    for card_state in pile['cards']
                    if card_state['card']['id']['Entry'] == 'STRIKE_IRONCLAD'
                    and card_state['card'].get('enchantment')
                )
                assert recaptured_card['card']['enchantment']['id']['Entry'] == 'SHARP'
                assert recaptured_card['card']['enchantment']['amount'] == 2
            before = cli.get_search_state()['combat_state_for_search']
            before_hp = [enemy['hp'] for enemy in before['combat']['enemies']]
            action = next(
                action for action in before['combat']['available_actions']
                if (action.get('metadata') or {}).get('card_id') == 'STRIKE_IRONCLAD'
            )
            cli.action('play_card', {'card_index': action['card_index']})
            after = cli.get_search_state()['combat_state_for_search']
            after_hp = [enemy['hp'] for enemy in after['combat']['enemies']]
            damage = [left - right for left, right in zip(before_hp, after_hp)]
            assert sum(damage) == 8
            assert sum(value > 0 for value in damage) == 1
    finally:
        cli.stop()
