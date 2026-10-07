from pathlib import Path

from controller.deck_profile import (
    choose_profile_bundle,
    choose_profile_reward,
    deck_card_ids,
    load_deck_profile,
    save_deck_profile,
    score_profile_card,
)
from controller.search.evaluator import evaluate_leaf, extract_leaf_features


def test_deck_card_ids_normalize_visible_rows():
    assert deck_card_ids([{'card_id': 'CARD.Blood-Wall'}, {'id': 'BASH'}]) == ['BLOOD_WALL', 'BASH']


def test_custom_profile_round_trip_and_port_cap(tmp_path):
    profile = {
        'id': 'test_profile',
        'take_threshold': 5,
        'ports': {'draw': {'target': 1, 'weight': 4}},
        'cards': {'TEST_CARD': {'base_score': 3, 'max_copies': 1, 'ports': ['draw']}},
    }
    path = tmp_path / 'profile.json'
    save_deck_profile(path, profile)
    loaded = load_deck_profile(path)
    first = score_profile_card(loaded, 'TEST_CARD', [])
    capped = score_profile_card(loaded, 'TEST_CARD', ['TEST_CARD'])
    assert first['eligible'] and first['assigned_port'] == 'draw'
    assert capped['rejection_reason'] == 'copy_cap_reached'


def test_ordered_profile_prefers_earlier_deficit_and_rejects_outside_cards(tmp_path):
    profile = {
        'schema_version': 2,
        'id': 'ordered',
        'ports': [
            {'id': 'energy', 'name': 'Energy', 'target': 1, 'maximum': 2},
            {'id': 'defense', 'name': 'Defense', 'target': 1, 'maximum': 2},
        ],
        'cards': [
            {'id': 'OFFERING', 'max_copies': 1, 'ports': ['energy']},
            {'id': 'FLAME_BARRIER', 'max_copies': 2, 'ports': ['defense']},
        ],
    }
    path = tmp_path / 'profile.json'
    save_deck_profile(path, profile)
    loaded = load_deck_profile(path)
    assert score_profile_card(loaded, 'OFFERING', [])['priority_rank'] < score_profile_card(loaded, 'FLAME_BARRIER', [])['priority_rank']
    assert score_profile_card(loaded, 'HAVOC', [])['rejection_reason'] == 'not_in_profile'


def test_bundle_uses_best_whitelist_card_instead_of_first_bundle():
    profile = load_deck_profile()
    choice = choose_profile_bundle(profile, [
        {'index': 0, 'cards': [{'id': 'HAVOC'}]},
        {'index': 1, 'cards': [{'id': 'BLOODLETTING'}, {'id': 'HAVOC'}]},
    ], [])
    assert choice['index'] == 1
    assert choice['outside_cards'] == 1


def test_saved_profile_preserves_user_whitelist_and_card_roles():
    profile = load_deck_profile()
    cards = {row['id']: row for row in profile['cards']}
    assert len(cards) == 23
    assert cards['BREAKTHROUGH']['max_copies'] == 2
    assert cards['NOT_YET']['ports'] == ['sustain']
    assert 'attack' not in cards['FINESSE']['ports']
    assert 'attack' not in cards['MASTER_OF_STRATEGY']['ports']
    assert 'defense' in cards['CRIMSON_MANTLE']['ports']


def test_selection_rules_survive_dashboard_style_save(tmp_path):
    profile = load_deck_profile()
    path = tmp_path / 'edited_profile.json'
    save_deck_profile(path, profile)
    restored = load_deck_profile(path)
    assert restored['selection_rules']['max_early_fillers'] == 2
    assert restored['deck_size']['optional_max_by_act']['2'] == 20
    cards = {row['id']: row for row in restored['cards']}
    assert cards['NOT_YET']['selection']['always_take']
    assert cards['POMMEL_STRIKE']['selection']['second_copy_max_strikes'] == 3


def test_second_pommel_requires_two_starter_strikes_removed():
    profile = load_deck_profile()
    full_starters = ['POMMEL_STRIKE'] + ['STRIKE_IRONCLAD'] * 5
    reduced_starters = ['POMMEL_STRIKE'] + ['STRIKE_IRONCLAD'] * 3
    assert score_profile_card(profile, 'POMMEL_STRIKE', full_starters, act=1)['rejection_reason'] == 'starter_strikes_not_removed'
    assert score_profile_card(profile, 'POMMEL_STRIKE', reduced_starters, act=1)['eligible']


def test_breakthrough_can_be_taken_twice_and_not_yet_is_must_take():
    profile = load_deck_profile()
    deck = ['STRIKE_IRONCLAD'] * 5 + ['DEFEND_IRONCLAD'] * 4 + ['BASH']
    assert score_profile_card(profile, 'BREAKTHROUGH', deck + ['BREAKTHROUGH'], act=1)['eligible']
    assert score_profile_card(profile, 'BREAKTHROUGH', deck + ['BREAKTHROUGH'] * 2, act=1)['rejection_reason'] == 'copy_cap_reached'
    oversized_deck = deck + ['HAVOC'] * 15
    assert score_profile_card(profile, 'POMMEL_STRIKE', oversized_deck, act=3)['rejection_reason'] == 'deck_size_cap_reached'
    assert score_profile_card(profile, 'NOT_YET', oversized_deck, act=3)['eligible']
    best, _ = choose_profile_reward(profile, [{'id': 'OFFERING'}, {'id': 'NOT_YET'}],
                                    [{'id': card_id} for card_id in deck], act=1)
    assert best['card_id'] == 'NOT_YET'


def test_early_fillers_close_after_two_picks_or_act_one():
    profile = load_deck_profile()
    assert score_profile_card(profile, 'IRON_WAVE', ['TREMBLE', 'ARMAMENTS'], act=1)['rejection_reason'] == 'early_filler_cap_reached'
    assert score_profile_card(profile, 'IRON_WAVE', [], act=2)['rejection_reason'] == 'outside_act_window'


def test_rage_requires_nonstarter_attack_density():
    profile = load_deck_profile()
    assert score_profile_card(profile, 'RAGE', ['STRIKE_IRONCLAD'] * 5, act=1)['rejection_reason'] == 'attack_density_missing'
    assert score_profile_card(profile, 'RAGE', ['POMMEL_STRIKE', 'HEMOKINESIS', 'BREAKTHROUGH'], act=2)['eligible']


def test_stage_rules_reach_live_reward_and_shop_paths():
    from controller.run_agent import choose_card_reward, choose_shop_action

    root = Path(__file__).resolve().parents[1]
    starter = ([{'id': 'STRIKE_IRONCLAD'}] * 5
               + [{'id': 'DEFEND_IRONCLAD'}] * 4 + [{'id': 'BASH'}])
    state = {'context': {'act': 2}, 'player': {'deck': starter},
             'cards': [{'index': 0, 'id': 'IRON_WAVE'}, {'index': 1, 'id': 'NOT_YET'}]}
    assert choose_card_reward(state, root) == {'card_index': 1}
    shop = {'run': {'act_id': '1'}, 'player': {'deck': starter, 'gold': 100},
            'cards': [{'index': 0, 'id': 'IRON_WAVE', 'cost': 50}],
            'card_removal_available': False}
    assert choose_shop_action(shop, root) is None


def test_profile_combat_coefficients_adjust_generic_state_features():
    root = {'player': {'hp': 50, 'block': 0}, 'enemies': [{'hp': 20, 'intent': {}}]}
    leaf = {'success': True, 'combat': {
        'player': {'hp': 45, 'block': 10, 'energy': 0, 'powers': []},
        'enemies': [{'hp': 15, 'intent': {}}], 'hand': [],
    }}
    baseline = evaluate_leaf(leaf, root, 'balanced')
    adjusted = evaluate_leaf(leaf, root, 'balanced', {
        'player_hp_loss_delta': 0.75,
        'player_block_delta': 0.35,
    })
    assert round(adjusted - baseline, 3) == 7.25


def test_strength_future_value_scales_with_expected_attack_plays():
    root = {'player': {'hp': 50, 'block': 0, 'powers': []},
            'enemies': [{'hp': 36, 'intent': {}}]}
    cards = [{'card_id': 'ASHEN_STRIKE'} for _ in range(5)]
    leaf = {'success': True, 'combat': {
        'player': {'hp': 50, 'block': 0, 'energy': 0,
                   'powers': [{'id': 'STRENGTH_POWER', 'amount': 2}]},
        'enemies': [{'hp': 36, 'intent': {}}], 'hand': cards,
        'draw_pile': [], 'discard_pile': [],
    }}
    features = extract_leaf_features(leaf, root)
    assert features['strength_gain'] == 2
    assert features['future_attack_plays'] == 10
    baseline = evaluate_leaf(leaf, root, 'balanced')
    adjusted = evaluate_leaf(leaf, root, 'balanced', {'strength_future_delta': 1.0})
    assert adjusted - baseline == 20
