import random
from pathlib import Path

from controller.archetype_strategy import choose_self_damage_reward, score_self_damage_card
from controller.route_strategy import choose_weighted_route
from controller.run_agent import choose_card_reward, choose_global_random, choose_shop_action


ROOT = Path(__file__).resolve().parents[1]
STARTER = [
    *[{'id': 'STRIKE_IRONCLAD'} for _ in range(5)],
    *[{'id': 'DEFEND_IRONCLAD'} for _ in range(4)],
    {'id': 'BASH'},
]


def test_reward_prioritizes_first_energy_enabler():
    cards = [
        {'index': 0, 'id': 'HAVOC'},
        {'index': 1, 'id': 'BLOOD_WALL'},
        {'index': 2, 'id': 'BLOODLETTING'},
    ]
    choice = choose_card_reward({'cards': cards, 'player': {'deck': STARTER}}, ROOT)
    assert choice == {'card_index': 2}


def test_rupture_waits_for_repeatable_self_damage_sources():
    without_trigger, _ = choose_self_damage_reward(
        [{'index': 0, 'id': 'RUPTURE'}, {'index': 1, 'id': 'SHRUG_IT_OFF'}], STARTER
    )
    assert without_trigger['card_id'] == 'SHRUG_IT_OFF'

    with_trigger, _ = choose_self_damage_reward(
        [{'index': 0, 'id': 'RUPTURE'}, {'index': 1, 'id': 'SHRUG_IT_OFF'}],
        STARTER + [{'id': 'BLOODLETTING'}],
    )
    assert with_trigger['card_id'] == 'SHRUG_IT_OFF'
    assert not score_self_damage_card('RUPTURE', ['BLOODLETTING'])['eligible']
    assert score_self_damage_card('RUPTURE', ['BLOODLETTING', 'HEMOKINESIS'])['eligible']


def test_copy_caps_reject_excess_duplicates():
    row = score_self_damage_card('BLOODLETTING', ['BLOODLETTING', 'BLOODLETTING'])
    assert not row['eligible']
    assert row['reason'] == 'copy_cap_reached'


def test_shop_reuses_archetype_and_skips_off_archetype_cards():
    state = {
        'player': {'gold': 100, 'deck': STARTER},
        'cards': [
            {'index': 0, 'id': 'HAVOC', 'cost': 20},
            {'index': 1, 'id': 'BLOODLETTING', 'cost': 70},
        ],
        'card_removal_cost': 120,
    }
    assert choose_shop_action(state, ROOT) == {'action': 'buy_card', 'card_index': 1}


def test_shop_does_not_repeat_sold_items_or_used_removal():
    state = {
        'player': {'gold': 100, 'deck': STARTER},
        'cards': [{'index': 0, 'id': 'BLOODLETTING', 'cost': 70, 'is_stocked': False}],
        'card_removal_cost': 50,
        'card_removal_available': False,
    }
    assert choose_shop_action(state, ROOT) is None


def test_random_global_policy_resolves_bundle_selection():
    state = {'bundles': [{'index': 4}, {'index': 7}]}
    action, payload = choose_global_random('bundle_select', state, random.Random(0))
    assert action == 'select_bundle'
    assert payload['bundle_index'] in {4, 7}


def test_random_global_policy_respects_required_selection_count():
    state = {
        'min_select': 2,
        'max_select': 2,
        'cards': [{'index': 3}, {'index': 5}, {'index': 8}],
    }
    action, payload = choose_global_random('card_select', state, random.Random(0))
    assert action == 'select_cards'
    assert len(payload['indices'].split(',')) == 2


def test_route_uses_additive_full_path_weights_without_hp_reaction():
    map_data = {
        'type': 'map',
        'rows': [
            [
                {'col': 0, 'row': 1, 'type': 'Monster', 'children': [{'col': 0, 'row': 2}]},
                {'col': 1, 'row': 1, 'type': 'RestSite', 'children': [{'col': 1, 'row': 2}]},
            ],
            [
                {'col': 0, 'row': 2, 'type': 'Elite', 'children': [{'col': 0, 'row': 3}]},
                {'col': 1, 'row': 2, 'type': 'Unknown', 'children': [{'col': 1, 'row': 3}]},
            ],
            [
                {'col': 0, 'row': 3, 'type': 'RestSite', 'children': []},
                {'col': 1, 'row': 3, 'type': 'Monster', 'children': []},
            ],
        ],
    }
    choices = [{'col': 0, 'row': 1}, {'col': 1, 'row': 1}]
    chosen, route = choose_weighted_route(choices, map_data)
    assert chosen == {'col': 0, 'row': 1}
    assert [node['type'] for node in route] == ['Monster', 'Elite', 'RestSite']
    assert route[0]['route_score'] == 22.0


def test_route_accepts_visible_mod_flat_map_shape():
    map_data = {
        'rows': 2,
        'nodes': [
            {'row': 1, 'col': 0, 'node_type': 'Monster',
             'children': [{'row': 2, 'col': 0}]},
            {'row': 1, 'col': 1, 'node_type': 'Unknown',
             'children': [{'row': 2, 'col': 1}]},
            {'row': 2, 'col': 0, 'node_type': 'RestSite', 'children': []},
            {'row': 2, 'col': 1, 'node_type': 'Monster', 'children': []},
        ],
    }
    chosen, route = choose_weighted_route(
        [{'row': 1, 'col': 0}, {'row': 1, 'col': 1}], map_data
    )
    assert chosen == {'col': 0, 'row': 1}
    assert [node['type'] for node in route] == ['Monster', 'RestSite']
