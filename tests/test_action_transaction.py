import re
from pathlib import Path

import pytest

from cli.sts2_mod_adapter import ModApiError
from controller.action_transaction import (
    ActionFamily,
    MirrorMode,
    RecoveryPolicy,
    SHADOW_ACTIONS,
    make_transaction,
    transaction_from_dict,
)
from controller.live_client_bridge import JsonlSessionLog, LiveClientBridge, translate_headless_action
from tests.test_live_observer import FakeMod


def test_mirrored_transaction_separates_intent_and_protocol_commands():
    transaction = make_transaction(
        'choose_event_option',
        {'option_index': 4},
        shadow_action='choose_option',
        shadow_args={'option_index': 1},
        operation='transform',
    )
    assert transaction.intent == 'event.choose_option'
    assert transaction.family == ActionFamily.EVENT
    assert transaction.mirror_mode == MirrorMode.MIRRORED
    assert transaction.recovery == RecoveryPolicy.REPLAY_SHADOW_IF_COMPLETED
    assert transaction.client.params == {'option_index': 4}
    assert transaction.shadow.params == {'option_index': 1}


def test_multiclick_selection_is_one_atomic_player_transaction():
    transaction = make_transaction(
        'select_deck_cards',
        {'indices': [2, 5]},
        shadow_action='select_cards',
        shadow_args={'indices': '0,1'},
        operation='transform',
    )
    assert transaction.intent == 'selection.select_cards'
    assert transaction.atomic is True
    assert transaction.client.action == 'select_deck_cards'
    assert transaction.shadow.action == 'select_cards'


def test_unregistered_actions_fail_closed():
    with pytest.raises(ValueError, match='Unregistered transaction action'):
        make_transaction('guess_and_click', {})


def test_client_only_actions_are_logged_as_transactions(tmp_path):
    mod = FakeMod()
    mod.value.update(screen='MAIN_MENU', available_actions=['open_character_select'])
    log = JsonlSessionLog(tmp_path / 'session.jsonl')
    bridge = LiveClientBridge(mod, log)
    bridge.execute_client_action('open_character_select', {}, mod.state())
    assert bridge.last_action['transaction']['intent'] == 'session.open_character_select'
    assert bridge.last_action['transaction']['mirror_mode'] == 'client_only'


def test_discard_potion_has_visible_client_translation():
    translated = translate_headless_action(
        'combat_play', 'discard_potion', {'potion_index': 2}, {}
    )
    assert translated.name == 'discard_potion'
    assert translated.params == {'option_index': 2}


def test_potion_transaction_resolves_visible_slot_by_identity():
    mod = FakeMod()
    bridge = LiveClientBridge(mod)
    before = {
        'run': {
            'potions': [
                {'index': 0, 'occupied': False, 'can_use': False},
                {'index': 1, 'occupied': True, 'can_use': True, 'potion_id': 'Potion.CURE_ALL'},
            ]
        }
    }
    transaction = bridge.prepare_headless_transaction(
        'combat_play', 'use_potion', {'potion_index': 0}, before,
        {'chosen': {'metadata': {'potion_id': 'CURE_ALL'}}},
    )
    assert transaction.client.params == {'option_index': 1}
    assert transaction.shadow.params == {'potion_index': 0}


def test_all_enemies_potion_does_not_invent_a_target():
    bridge = LiveClientBridge(FakeMod())
    before = {
        'run': {'potions': [
            {'index': 0, 'occupied': True, 'can_use': True,
             'potion_id': 'EXPLOSIVE_AMPOULE', 'requires_target': False,
             'target_type': 'AllEnemies', 'valid_target_indices': []},
        ]},
        'combat': {'enemies': [
            {'index': 3, 'is_alive': True, 'is_hittable': True},
        ]},
    }

    transaction = bridge.prepare_headless_transaction(
        'combat_play', 'use_potion', {'potion_index': 0}, before,
        {'chosen': {'metadata': {
            'potion_id': 'EXPLOSIVE_AMPOULE', 'target_type': 'AllEnemies',
        }}},
    )

    assert transaction.client.params == {'option_index': 0}
    assert transaction.shadow.params == {'potion_index': 0}


def test_targeted_enemy_potion_uses_visible_valid_target():
    before = {
        'run': {'potions': [
            {'index': 2, 'occupied': True, 'can_use': True, 'potion_id': 'FIRE_POTION',
             'requires_target': True, 'target_type': 'AnyEnemy',
             'target_index_space': 'enemies', 'valid_target_indices': [3]},
        ]},
        'combat': {'enemies': [
            {'index': 3, 'is_alive': True, 'is_hittable': True},
        ]},
    }
    translated = translate_headless_action(
        'combat_play', 'use_potion',
        {'potion_index': 2, 'potion_id': 'FIRE_POTION', 'target_index': 0}, before,
    )
    assert translated.params == {'option_index': 2, 'target_index': 3}


def test_targetless_potion_rejects_a_planned_target():
    before = {'run': {'potions': [
        {'index': 0, 'occupied': True, 'can_use': True, 'potion_id': 'EXPLOSIVE_AMPOULE',
         'requires_target': False, 'target_type': 'AllEnemies', 'valid_target_indices': []},
    ]}}
    with pytest.raises(ModApiError, match='targetless'):
        translate_headless_action(
            'combat_play', 'use_potion',
            {'potion_index': 0, 'target_index': 0}, before,
        )


def test_snapshot_restore_prefers_resolved_dynamic_energy_cost():
    source = (
        Path(__file__).resolve().parents[1]
        / 'third_party/sts2-cli/src/Sts2Headless/RunSimulator.cs'
    ).read_text(encoding='utf-8')
    assert (
        'GetMember(energyCost, "ResolvedValue") ?? '
        'GetMember(energyCost, "Value") ?? 0'
    ) in source


def test_potion_translation_fails_before_mod_call_when_identity_is_missing():
    before = {
        'run': {
            'potions': [
                {'index': 0, 'occupied': False, 'can_use': False},
                {'index': 1, 'occupied': True, 'can_use': True, 'potion_id': 'Potion.OTHER'},
            ]
        }
    }
    with pytest.raises(ModApiError, match='no unique visible counterpart'):
        translate_headless_action(
            'combat_play', 'use_potion',
            {'potion_index': 0, 'potion_id': 'CURE_ALL'}, before,
        )


def test_shadow_registry_matches_headless_execute_action_switch():
    source = (
        Path(__file__).resolve().parents[1]
        / 'third_party/sts2-cli/src/Sts2Headless/RunSimulator.cs'
    ).read_text(encoding='utf-8')
    block = source[
        source.index('public Dictionary<string, object?> ExecuteAction'):
        source.index('public Dictionary<string, object?> ExecuteActionWithEngineSnapshot')
    ]
    actual = set(re.findall(r'case "([a-z_]+)"', block))
    assert actual == set(SHADOW_ACTIONS)


def test_persisted_transaction_round_trip_is_validated():
    original = make_transaction(
        'choose_map_node',
        {'option_index': 3},
        shadow_action='select_map_node',
        shadow_args={'row': 1, 'col': 0},
        completion='room_change',
    )
    loaded = transaction_from_dict(original.to_dict())
    assert loaded == original
    invalid = original.to_dict()
    invalid['shadow']['action'] = 'invented_shadow_action'
    with pytest.raises(ValueError, match='Unregistered transaction shadow action'):
        transaction_from_dict(invalid)


def test_client_only_run_creation_records_rng_baseline_without_delta():
    mod = FakeMod()
    mod.value.update(screen='CHARACTER_SELECT', available_actions=['embark'])
    calls = 0

    def rng_snapshot():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModApiError('No run in progress')
        return {
            'schema_version': 1,
            'complete': True,
            'run_seed': 'NEW_RUN',
            'run_streams': {
                'Shuffle': {'counter': 0, 'seed': 1, 's0': 2, 's1': 3, 's2': 4, 's3': 5},
            },
            'players': [],
        }

    mod.rng_snapshot = rng_snapshot
    bridge = LiveClientBridge(mod)
    bridge.execute_client_action('embark', {}, mod.state())

    assert bridge.last_action['status'] == 'completed'
    assert bridge.last_action['client_rng_transition'] == 'baseline_created'
    assert bridge.last_action['client_rng_after']['run_seed'] == 'NEW_RUN'
    assert 'client_rng_before' not in bridge.last_action
    assert 'client_rng_delta' not in bridge.last_action


@pytest.mark.parametrize('client,shadow', [
    ('claim_reward', 'ack_event_reward'),
    ('proceed', 'finish_combat_rewards'),
    ('choose_event_option', 'reconcile_relics'),
    ('confirm_selection', 'select_cards'),
])
def test_registered_exception_pairs_share_construction_and_replay_contract(client, shadow):
    args = {'claim_reward': {'option_index': 0},
            'choose_event_option': {'option_index': 0}}.get(client, {})
    shadow_args = {'ack_event_reward': {'reward_index': 0, 'reward_set_id': 1},
                   'select_cards': {'indices': ''}}.get(shadow, {})
    tx = make_transaction(client, args, shadow_action=shadow, shadow_args=shadow_args)
    assert transaction_from_dict(tx.to_dict()) == tx


def test_persisted_client_only_boundary_cannot_bypass_contract():
    tx = make_transaction('buy_card', {'option_index': 0}).to_dict()
    tx['telemetry'] = {'requires_reanchor': True}
    with pytest.raises(ValueError, match='explicitly client-authoritative'):
        transaction_from_dict(tx)


def test_duplicate_objects_map_by_verified_offer_occurrence():
    from controller.action_transaction import map_object_indices
    left = [{'index': 5, 'id': 'A'}, {'index': 9, 'id': 'A'}]
    right = [{'index': 0, 'id': 'A'}, {'index': 1, 'id': 'A'}]
    assert map_object_indices(left, right, [9], lambda row: row['id']) == [1]
    with pytest.raises(ValueError, match='unambiguous'):
        map_object_indices(left, right + [{'index': 2, 'id': 'B'}], [9], lambda row: row['id'])


@pytest.mark.parametrize('client,shadow', [
    ('play_card', 'play_card'), ('claim_reward', 'ack_event_reward'),
])
def test_empty_gameplay_commands_fail_before_either_endpoint(client, shadow):
    with pytest.raises(ValueError, match='requires nonnegative integer'):
        make_transaction(client, shadow_action=shadow)
