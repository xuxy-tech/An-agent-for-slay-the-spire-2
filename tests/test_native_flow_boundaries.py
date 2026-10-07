import json
from pathlib import Path

import pytest

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    not (ROOT / CliConfig(ROOT).dll_relpath).exists(), reason='Local game runtime is required')


def test_resume_room_preserves_serialized_room_progress(tmp_path):
    anchor = tmp_path / 'map_anchor.save'
    restored = tmp_path / 'restored_map_anchor.save'
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        state = cli.start_run(seed='71V3MQMUET')
        pomander = next(
            option for option in state['options']
            if str(option.get('text_key') or '').endswith('.POMANDER')
        )
        state = cli.action('choose_option', {'option_index': pomander['index']}, timeout_s=20)
        assert state['decision'] == 'card_select', state
        state = cli.action('select_cards', {'indices': '0'}, timeout_s=20)
        assert state['decision'] == 'map_select', state
        written = cli.write_exact_save(str(anchor))
        assert written['success'] is True, written
    finally:
        cli.stop()

    before = json.loads(anchor.read_text(encoding='utf-8'))
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        loaded = cli.load_save(str(anchor), resume_room=True)
        assert loaded.get('type') != 'error', loaded
        written = cli.write_exact_save(str(restored))
        assert written['success'] is True, written
    finally:
        cli.stop()

    after = json.loads(restored.read_text(encoding='utf-8'))
    counter_names = (
        'events_visited', 'normal_encounters_visited',
        'elite_encounters_visited', 'boss_encounters_visited',
    )
    assert [
        {name: act['rooms'][name] for name in counter_names}
        for act in after['acts']
    ] == [
        {name: act['rooms'][name] for name in counter_names}
        for act in before['acts']
    ]


def test_event_with_same_option_count_stays_on_next_page():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        cli.start_run(seed='flow-boundary-test')
        initial = cli.enter_room('event', event='TABLET_OF_TRUTH')
        assert initial['decision'] == 'event_choice'
        first = initial['options'][0]
        after = cli.action('choose_option', {'option_index': first['index']}, timeout_s=20)
        assert after['decision'] == 'event_choice'
        assert len(after['options']) == len(initial['options']) == 2
        assert after['options'][0]['text_key'] != first['text_key']
    finally:
        cli.stop()


def test_neow_card_reward_skip_settles_event():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        initial = cli.start_run(seed='H341ZRU6L2')
        lost_coffer = next(
            option for option in initial['options']
            if str(option.get('text_key') or '').endswith('.LOST_COFFER')
        )
        reward = cli.action('choose_option', {'option_index': lost_coffer['index']}, timeout_s=20)
        assert reward['decision'] == 'combat_reward'
        assert reward['from_event'] is True
        card = next(r for r in reward['rewards'] if r['reward_type'] == 'Card')
        opened = cli.action('claim_combat_reward', {'reward_index': card['index'],
                            'reward_set_id': reward['reward_set_id']}, timeout_s=20)
        assert opened['decision'] == 'card_reward'
        after_skip = cli.action('skip_card_reward', timeout_s=20)
        assert after_skip['decision'] == 'combat_reward'
        assert all(item['reward_type'] != 'Card' for item in after_skip['rewards'])
        after_skip = cli.action('finish_combat_rewards', timeout_s=20)
        assert after_skip.get('type') != 'error', after_skip.get('message')
        assert after_skip['decision'] in {'event_choice', 'map_select'}
        if after_skip['decision'] == 'event_choice':
            proceed = next(
                option for option in after_skip['options']
                if str(option.get('text_key') or '').split('.')[-1].upper() == 'PROCEED'
            )
            after_skip = cli.action('choose_option', {'option_index': proceed['index']}, timeout_s=20)
        assert after_skip['decision'] == 'map_select'
    finally:
        cli.stop()


def test_map_relic_reconciliation_returns_updated_map_state():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        initial = cli.start_run(seed='H341ZRU6L2')
        lost_coffer = next(
            option for option in initial['options']
            if str(option.get('text_key') or '').endswith('.LOST_COFFER')
        )
        state = cli.action('choose_option', {'option_index': lost_coffer['index']}, timeout_s=20)
        if state['decision'] == 'combat_reward':
            card = next(r for r in state['rewards'] if r['reward_type'] == 'Card')
            state = cli.action('claim_combat_reward', {'reward_index': card['index'],
                               'reward_set_id': state['reward_set_id']}, timeout_s=20)
        if state['decision'] == 'card_reward':
            state = cli.action('skip_card_reward', timeout_s=20)
        if state['decision'] == 'combat_reward':
            state = cli.action('finish_combat_rewards', timeout_s=20)
        if state['decision'] == 'event_choice':
            proceed = next(
                option for option in state['options']
                if str(option.get('text_key') or '').split('.')[-1].upper() == 'PROCEED'
            )
            state = cli.action('choose_option', {'option_index': proceed['index']}, timeout_s=20)
        assert state['decision'] == 'map_select'
        original = [str(row['id']).split('.')[-1] for row in state['player']['relics']]

        reconciled = cli.action(
            'reconcile_relics',
            {'relic_ids': ','.join([*original, 'ANCHOR'])},
            timeout_s=20,
        )

        assert reconciled['decision'] == 'map_select'
        assert [str(row['id']).split('.')[-1] for row in reconciled['player']['relics']] == [
            *original, 'ANCHOR',
        ]
        first = reconciled['choices'][0]
        combat = cli.action(
            'select_map_node', {'row': first['row'], 'col': first['col']}, timeout_s=20,
        )
        assert combat.get('decision') == 'combat_play', combat
        assert combat['player']['block'] >= 10
    finally:
        cli.stop()


def test_event_multi_card_selection_settles_event():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        cli.start_run(seed='room-full-cheese-regression')
        initial = cli.enter_room('event', event='ROOM_FULL_OF_CHEESE')
        gorge = next(
            option for option in initial['options']
            if str(option.get('text_key') or '').endswith('.GORGE')
        )
        selection = cli.action('choose_option', {'option_index': gorge['index']}, timeout_s=20)
        assert selection['decision'] == 'card_select'
        assert selection['min_select'] == selection['max_select'] == 2
        rejected_skip = cli.action('skip_select', timeout_s=20)
        assert rejected_skip['type'] == 'error'
        rejected_partial = cli.action('select_cards', {'indices': '0'}, timeout_s=20)
        assert rejected_partial['type'] == 'error'
        after_select = cli.action('select_cards', {'indices': '0,1'}, timeout_s=20)
        assert after_select['decision'] == 'map_select'
    finally:
        cli.stop()


def test_neow_bundle_selection_settles_event():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        initial = cli.start_run(seed='4J3TXZLWW8')
        scroll_boxes = next(
            option for option in initial['options']
            if str(option.get('text_key') or '').endswith('.SCROLL_BOXES')
        )
        bundles = cli.action(
            'choose_option', {'option_index': scroll_boxes['index']}, timeout_s=20)
        assert bundles['decision'] == 'bundle_select'
        assert len(bundles['bundles']) == 2
        rejected = cli.action('select_bundle', {'bundle_index': 99}, timeout_s=20)
        assert rejected['type'] == 'error'
        after_select = cli.action('select_bundle', {'bundle_index': 0}, timeout_s=20)
        assert after_select['decision'] == 'map_select'
    finally:
        cli.stop()


def test_rest_smith_selection_settles_to_map():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        cli.start_run(seed='noncombat-rest-audit')
        rest = cli.enter_room('rest')
        smith = next(option for option in rest['options'] if option['option_id'] == 'SMITH')
        selection = cli.action(
            'choose_option', {'option_index': smith['index']}, timeout_s=20)
        assert selection['decision'] == 'card_select'
        after_select = cli.action('select_cards', {'indices': '0'}, timeout_s=20)
        assert after_select['decision'] == 'map_select'
    finally:
        cli.stop()


def test_shop_removal_selection_returns_to_shop_and_removes_card():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        cli.start_run(seed='noncombat-shop-audit')
        cli.set_player(gold=999)
        shop = cli.enter_room('shop')
        initial_size = shop['player']['deck_size']
        selection = cli.action('remove_card', timeout_s=20)
        assert selection['decision'] == 'card_select'
        after_select = cli.action('select_cards', {'indices': '0'}, timeout_s=20)
        assert after_select['decision'] == 'shop'
        assert after_select['player']['deck_size'] == initial_size - 1
    finally:
        cli.stop()


def test_treasure_room_stops_at_each_visible_action_boundary():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        cli.start_run(seed='treasure-boundary-regression')
        before = cli.get_rng_snapshot()
        arrived = cli.enter_room('treasure')
        after_arrive = cli.get_rng_snapshot()
        player_stream = lambda snapshot, name: snapshot['players'][0]['streams'][name]
        run_stream = lambda snapshot, name: snapshot['run_streams'][name]
        assert arrived['decision'] == 'treasure'
        assert arrived['opened'] is False
        assert player_stream(after_arrive, 'Rewards')['counter'] == player_stream(before, 'Rewards')['counter']
        assert run_stream(after_arrive, 'TreasureRoomRelics')['counter'] == run_stream(before, 'TreasureRoomRelics')['counter'] + 1

        gold_before = arrived['player']['gold']
        opened = cli.action('open_chest', timeout_s=20)
        after_open = cli.get_rng_snapshot()
        assert opened['decision'] == 'treasure_relic'
        assert opened['player']['gold'] > gold_before
        assert player_stream(after_open, 'Rewards')['counter'] == player_stream(after_arrive, 'Rewards')['counter'] + 1
        assert len(opened['relics']) == 1

        relic_count = len(opened['player']['relics'])
        claimed = cli.action('choose_treasure_relic', {'relic_index': 0}, timeout_s=20)
        assert claimed['decision'] == 'treasure_complete'
        assert len(claimed['player']['relics']) == relic_count + 1

        left = cli.action('leave_room', timeout_s=20)
        assert left['decision'] == 'map_select'
    finally:
        cli.stop()


def test_silver_crucible_empty_treasure_is_a_valid_terminal_boundary():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        initial = cli.start_run(seed='silver-crucible-empty-chest-regression')
        for _ in range(8):
            if initial['decision'] == 'map_select':
                break
            if initial['decision'] == 'event_choice':
                option = next(option for option in initial['options'] if not option.get('is_locked'))
                initial = cli.action('choose_option', {'option_index': option['index']}, timeout_s=20)
            elif initial['decision'] == 'card_reward':
                initial = cli.action('skip_card_reward', timeout_s=20)
            elif initial['decision'] == 'bundle_select':
                initial = cli.action('select_bundle', {'bundle_index': 0}, timeout_s=20)
            elif initial['decision'] == 'card_select':
                cards = initial.get('cards') or []
                assert cards, initial
                initial = cli.action('select_cards', {'indices': str(cards[0]['index'])}, timeout_s=20)
            else:
                initial = cli.action('proceed', timeout_s=20)
        assert initial['decision'] == 'map_select'

        relics = [str(row['id']).split('.')[-1] for row in initial['player']['relics']]
        cli.action(
            'reconcile_relics',
            {'relic_ids': ','.join([*relics, 'SILVER_CRUCIBLE'])},
            timeout_s=20,
        )
        arrived = cli.enter_room('treasure')
        assert arrived['decision'] == 'treasure'

        opened = cli.action('open_chest', timeout_s=20)
        assert opened['decision'] == 'treasure_complete'
        assert opened['opened'] is True
        assert opened['claimed'] is False
        assert opened['empty'] is True

        left = cli.action('leave_room', timeout_s=20)
        assert left['decision'] == 'map_select'
    finally:
        cli.stop()


def test_winged_boots_expands_map_choices_and_consumes_native_charge():
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        state = cli.start_run(seed='HAPK2VHE49')
        for _ in range(20):
            if state['decision'] == 'map_select':
                break
            if state['decision'] == 'event_choice':
                option = next(
                    row for row in state['options']
                    if 'WINGED_BOOTS' in str(row.get('text_key'))
                )
                state = cli.action(
                    'choose_option', {'option_index': option['index']}, timeout_s=20)
            elif state['decision'] == 'card_reward':
                state = cli.action('skip_card_reward', timeout_s=20)
            elif state['decision'] == 'bundle_select':
                state = cli.action('select_bundle', {'bundle_index': 0}, timeout_s=20)
            elif state['decision'] == 'card_select':
                state = cli.action('skip_select', timeout_s=20)
            else:
                state = cli.action('proceed', timeout_s=20)
        assert state['decision'] == 'map_select'
        assert any(
            str(row['id']).split('.')[-1] == 'WINGED_BOOTS'
            for row in state['player']['relics']
        )
        first = state['choices'][0]
        combat = cli.action(
            'select_map_node', {'row': first['row'], 'col': first['col']}, timeout_s=20)
        assert combat['decision'] == 'combat_play'

        enemy_count = len(combat.get('enemies') or [])
        assert enemy_count > 0
        configured = cli.send({
            'cmd': 'configure_sandbox', 'hp': 80, 'energy': 10,
            'hand': ['STRIKE_IRONCLAD'] * min(enemy_count, 3),
            'enemy_hp': [1] * enemy_count,
        })
        assert configured['decision'] == 'combat_play'
        rewards = configured
        while rewards.get('decision') == 'combat_play':
            actions = cli.get_search_state()['combat_state_for_search']['combat']['available_actions']
            strike = next(action for action in actions
                          if (action.get('metadata') or {}).get('card_id') == 'STRIKE_IRONCLAD')
            rewards = cli.action('play_card', {
                'card_index': strike['card_index'], 'target_index': strike['target_index'],
            }, timeout_s=20)
        assert rewards['decision'] == 'combat_reward'
        state = cli.action('finish_combat_rewards', timeout_s=20)
        assert state['decision'] == 'map_select'

        full_map = cli.send({'cmd': 'get_map'})
        current = full_map['current_coord']
        current_node = next(
            node for row in full_map['rows'] for node in row
            if (node['row'], node['col']) == (current['row'], current['col'])
        )
        linked = {(row['row'], row['col']) for row in current_node['children']}
        next_row = {
            (node['row'], node['col'])
            for row in full_map['rows'] for node in row
            if node['row'] == current['row'] + 1
        }
        available = {(row['row'], row['col']) for row in state['choices']}
        assert available == next_row
        off_path = next(iter(next_row - linked))

        rejected = cli.action(
            'select_map_node', {'row': 15, 'col': 0}, timeout_s=20)
        assert rejected['type'] == 'error'
        assert 'is not legal' in rejected['message']
        wrapped = cli.action(
            'select_map_node', {'row': current['row'] + 1, 'col': 258}, timeout_s=20)
        assert wrapped['type'] == 'error'
        assert 'outside the supported coordinate range' in wrapped['message']

        before = cli.send({'cmd': 'inspect_relic_state'})
        winged_before = next(row for row in before['relics'] if row['id'] == 'WINGED_BOOTS')
        times_before = next(row for row in winged_before['fields'] if row['name'] == '_timesUsed')
        assert times_before['value'] == '0'

        entered = cli.action(
            'select_map_node', {'row': off_path[0], 'col': off_path[1]}, timeout_s=20)
        assert entered.get('type') != 'error', entered
        after = cli.send({'cmd': 'inspect_relic_state'})
        winged_after = next(row for row in after['relics'] if row['id'] == 'WINGED_BOOTS')
        times_after = next(row for row in winged_after['fields'] if row['name'] == '_timesUsed')
        assert times_after['value'] == '1'
    finally:
        cli.stop()
