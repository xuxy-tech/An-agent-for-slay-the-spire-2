from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    not (ROOT / CliConfig(ROOT).dll_relpath).exists(),
    reason='Local game runtime is required',
)


def test_lethal_headbutt_waits_for_native_reward_generation():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        cli.start_run(seed='terminal-headbutt-reward-boundary')
        configured_player = cli.send({'cmd': 'set_player', 'deck': ['HEADBUTT']})
        assert configured_player['type'] == 'ok', configured_player
        entered = cli.enter_room('combat', encounter='SHRINKER_BEETLE_WEAK')
        assert entered['decision'] == 'combat_play', entered
        configured = cli.send({
            'cmd': 'configure_sandbox', 'hp': 80, 'energy': 3,
            'hand': ['HEADBUTT'], 'enemy_hp': [1],
        })
        assert configured['decision'] == 'combat_play', configured
        actions = cli.get_search_state()['combat_state_for_search']['combat']['available_actions']
        headbutt = next(action for action in actions
                        if (action.get('metadata') or {}).get('card_id') == 'HEADBUTT')
        rewards_before = cli.get_rng_snapshot()['players'][0]['streams']['Rewards']['counter']

        offered = cli.action('play_card', {
            'card_index': headbutt['card_index'],
            'target_index': headbutt['target_index'],
        }, timeout_s=20)

        rewards_after = cli.get_rng_snapshot()['players'][0]['streams']['Rewards']['counter']
        assert offered['decision'] == 'combat_reward', offered
        assert offered['rewards'], offered
        assert rewards_after > rewards_before
    finally:
        cli.stop()


@pytest.mark.parametrize('take_card', [False, True])
def test_combat_rewards_use_native_choice_and_settle_once(take_card):
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        initial = cli.start_test_combat(
            encounter='SHRINKER_BEETLE_WEAK', seed=f'native-reward-{take_card}',
        )
        configured = cli.send({
            'cmd': 'configure_sandbox', 'hp': 80, 'energy': 3,
            'hand': ['STRIKE_IRONCLAD'], 'enemy_hp': [1],
        })
        assert configured['decision'] == 'combat_play'
        actions = cli.get_search_state()['combat_state_for_search']['combat']['available_actions']
        strike = next(action for action in actions
                      if (action.get('metadata') or {}).get('card_id') == 'STRIKE_IRONCLAD')

        offered = cli.action('play_card', {
            'card_index': strike['card_index'],
            'target_index': strike['target_index'],
        }, timeout_s=20)
        assert offered['decision'] == 'combat_reward', offered
        assert offered['player']['gold'] == initial['player']['gold']
        gold_item = next(row for row in offered['rewards'] if row['reward_type'] == 'Gold')
        collected = cli.action('claim_combat_reward', {'reward_index': gold_item['index']}, timeout_s=20)
        assert collected['decision'] == 'combat_reward', collected
        card_item = next(row for row in collected['rewards'] if row['reward_type'] == 'Card')
        reward = cli.action('claim_combat_reward', {'reward_index': card_item['index']}, timeout_s=20)
        assert reward['decision'] == 'card_reward', reward
        assert reward['from_event'] is False
        assert reward['player']['gold'] > initial['player']['gold']
        deck_size = reward['player']['deck_size']
        gold = reward['player']['gold']
        rng_after_offer = cli.get_rng_snapshot()

        resolved = cli.action(
            'select_card_reward' if take_card else 'skip_card_reward',
            {'card_index': 0} if take_card else None,
            timeout_s=20,
        )
        assert resolved['decision'] == 'combat_reward', resolved
        assert resolved['player']['gold'] == gold
        assert resolved['player']['deck_size'] == deck_size + int(take_card)
        assert cli.get_rng_snapshot() == rng_after_offer
        finished = cli.action('finish_combat_rewards', timeout_s=20)
        assert finished['decision'] == 'map_select', finished
    finally:
        cli.stop()


def test_unclaimed_combat_rewards_do_not_change_player_state():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        initial = cli.start_test_combat(encounter='SHRINKER_BEETLE_WEAK', seed='skip-native-rewards')
        cli.send({'cmd': 'configure_sandbox', 'hp': 80, 'energy': 3,
                  'hand': ['STRIKE_IRONCLAD'], 'enemy_hp': [1]})
        actions = cli.get_search_state()['combat_state_for_search']['combat']['available_actions']
        strike = next(action for action in actions
                      if (action.get('metadata') or {}).get('card_id') == 'STRIKE_IRONCLAD')
        offered = cli.action('play_card', {
            'card_index': strike['card_index'], 'target_index': strike['target_index'],
        }, timeout_s=20)
        assert offered['decision'] == 'combat_reward', offered
        assert offered['player']['gold'] == initial['player']['gold']
        assert offered['player']['deck_size'] == initial['player']['deck_size']
        finished = cli.action('finish_combat_rewards', timeout_s=20)
        assert finished['decision'] == 'map_select', finished
        assert finished['player']['gold'] == initial['player']['gold']
        assert finished['player']['deck_size'] == initial['player']['deck_size']
    finally:
        cli.stop()


def test_search_protocol_marks_unclaimed_combat_rewards_as_victory():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        cli.start_test_combat(encounter='SHRINKER_BEETLE_WEAK', seed='search-reward-boundary')
        cli.send({'cmd': 'configure_sandbox', 'hp': 80, 'energy': 3,
                  'hand': ['STRIKE_IRONCLAD'], 'enemy_hp': [1]})
        actions = cli.get_search_state()['combat_state_for_search']['combat']['available_actions']
        strike = next(action for action in actions
                      if (action.get('metadata') or {}).get('card_id') == 'STRIKE_IRONCLAD')
        result = cli.action('play_card', {
            'card_index': strike['card_index'], 'target_index': strike['target_index'],
        }, compact=True, timeout_s=20)
        assert result['decision'] == 'victory', result
        assert result['player']['gold'] >= 0
    finally:
        cli.stop()


def test_unclaimed_potion_remains_unclaimed_after_taking_card():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        cli.start_test_combat(encounter='SHRINKER_BEETLE_WEAK', seed='potion-reward-1')
        cli.send({'cmd': 'configure_sandbox', 'hp': 80, 'energy': 3,
                  'hand': ['STRIKE_IRONCLAD'], 'enemy_hp': [1]})
        actions = cli.get_search_state()['combat_state_for_search']['combat']['available_actions']
        strike = next(action for action in actions
                      if (action.get('metadata') or {}).get('card_id') == 'STRIKE_IRONCLAD')
        offered = cli.action('play_card', {
            'card_index': strike['card_index'], 'target_index': strike['target_index'],
        }, timeout_s=20)
        assert [row['reward_type'] for row in offered['rewards']] == ['Gold', 'Potion', 'Card']
        potion_count = len(offered['player']['potions'])
        gold = offered['rewards'][0]
        after_gold = cli.action('claim_combat_reward', {'reward_index': gold['index']}, timeout_s=20)
        card = next(row for row in after_gold['rewards'] if row['reward_type'] == 'Card')
        choice = cli.action('claim_combat_reward', {'reward_index': card['index']}, timeout_s=20)
        assert choice['decision'] == 'card_reward', choice
        after_card = cli.action('select_card_reward', {'card_index': 0}, timeout_s=20)
        assert after_card['decision'] == 'combat_reward', after_card
        assert [row['reward_type'] for row in after_card['rewards']] == ['Potion']
        finished = cli.action('finish_combat_rewards', timeout_s=20)
        assert finished['decision'] == 'map_select', finished
        assert len(finished['player']['potions']) == potion_count
    finally:
        cli.stop()
