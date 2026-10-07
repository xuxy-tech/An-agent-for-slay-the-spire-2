import copy
import json

import pytest

from controller.combat_observation import (
    CombatObservationPair, MODEL_INFORMATION_POLICY, project_model_state,
)
from controller.combat_scoring import CombatScoring
from controller.combat_opportunity import opportunities
from controller.combat_abilities import ability_potential as potential, REALIZATION
from controller.preference_model import fit_pairs
from controller.search.state_cache import hash_search_state


def state(hand=('STRIKE_IRONCLAD', 'HEMOKINESIS'), draw=('STRIKE_IRONCLAD', 'HEMOKINESIS')):
    return {'success': True, 'combat': {
        'turn_number': 2, 'round_number': 2, 'is_player_turn': True,
        'player': {'hp': 80, 'max_hp': 80, 'energy': 3, 'block': 0, 'powers': []},
        'enemies': [{'monster_id': 'TEST', 'hp': 300, 'max_hp': 300, 'block': 0,
                     'powers': [], 'intent': {'intent_types': ['Attack'], 'total_damage': 10}}],
        'hand': [{'card_id': card, 'upgrade': 0} for card in hand],
        'draw_pile': [{'card_id': card, 'upgrade': 0} for card in draw],
        'discard_pile': [], 'exhaust_pile': [], 'play_pile': [],
        'available_actions': [{'action_type': 'play_card', 'card_index': index}
                              for index in range(len(hand))]},
        'leaf_settlement': {'phase': 'post_enemy_turn'}}


def powered(value, name, amount):
    result = copy.deepcopy(value)
    result['combat']['player']['powers'].append({'id': name + '_POWER', 'amount': amount})
    return result


def test_unordered_projection_is_idempotent_and_keeps_duplicates_not_positions():
    raw = state(draw=('STRIKE_IRONCLAD', 'HEMOKINESIS', 'STRIKE_IRONCLAD'))
    raw['combat']['draw_pile'][0]['index'] = 42
    projected = project_model_state(raw)
    assert project_model_state(projected) == projected
    entries = projected['combat']['draw_pile']['cards']
    assert sorted(row['count'] for row in entries) == [1, 2]
    assert all('index' not in row['card'] for row in entries)
    assert raw['combat']['draw_pile'][0]['index'] == 42
    trace = [{'before': raw, 'action': {'action_type': 'end_turn'}}]
    pair = CombatObservationPair(raw, raw, trace, 'mid', information_policy=MODEL_INFORMATION_POLICY)
    assert pair.trace[0]['before']['combat']['draw_pile']['kind'] == 'card_multiset'
    assert CombatObservationPair.from_record(json.loads(json.dumps(pair.to_record()))).to_record() == pair.to_record()


def test_reordering_piles_cannot_change_features_but_engine_hash_remains_ordered():
    scorer, root = CombatScoring(), state()
    leaf = powered(root, 'STRENGTH', 3)
    original = copy.deepcopy(leaf)
    expected = scorer.features(root, leaf, [])
    leaf['combat']['draw_pile'].reverse()
    assert hash_search_state(original) != hash_search_state(leaf)
    assert scorer.features(root, leaf, []) == expected
    assert scorer.score(root, leaf, []) == scorer.score(root, original, [])
    assert project_model_state(original) == project_model_state(leaf)


def test_simulated_hand_changes_are_permitted_and_affect_score():
    scorer, root = CombatScoring(), state()
    attack = state(hand=('STRIKE_IRONCLAD',))
    curse = state(hand=('UNKNOWN_CURSE',))
    assert scorer.score(root, attack, []) > scorer.score(root, curse, [])


def test_strength_uses_attack_segments_and_deck_density():
    scorer = CombatScoring()
    strike = state(hand=('STRIKE_IRONCLAD',), draw=('STRIKE_IRONCLAD',))
    twin = state(hand=('TWIN_STRIKE',), draw=('TWIN_STRIKE',))
    def gain(root):
        return scorer.features(root, powered(root, 'STRENGTH', 2), [])['ability_values']['enemy_hp_removed']
    assert gain(twin) > gain(strike) > 0
    diluted = state(hand=('STRIKE_IRONCLAD',), draw=('STRIKE_IRONCLAD',) + ('UNKNOWN_CURSE',) * 8)
    assert gain(diluted) < gain(strike)
    exhausted = copy.deepcopy(strike)
    exhausted['combat']['exhaust_pile'] = [{'card_id': 'TWIN_STRIKE'}] * 20
    assert gain(exhausted) == gain(strike)


def test_rupture_requires_both_triggers_and_following_attacks():
    scorer = CombatScoring()
    def gain(root):
        return scorer.features(root, powered(root, 'RUPTURE', 2), [])['ability_values']['enemy_hp_removed']
    assert gain(state()) > 0
    assert gain(state(hand=('STRIKE_IRONCLAD',), draw=('STRIKE_IRONCLAD',))) == 0
    assert gain(state(hand=('BLOODLETTING',), draw=('BLOODLETTING',))) == 0
    raw = state()
    assert scorer.features(raw, powered(raw, 'RUPTURE', 2), [])['ability_values']['enemy_hp_removed'] > (
        scorer.features(raw, powered(raw, 'RUPTURE', 1), [])['ability_values']['enemy_hp_removed'])


def test_growth_starts_after_current_turn_and_terminal_has_no_future():
    scorer, root = CombatScoring(), state()
    leaf = powered(root, 'DEMON_FORM', 2)
    op = opportunities(leaf, scorer.cards, 10)
    assert potential(leaf, op, 1, 10, 80)['values']['enemy_hp_removed'] == 0
    assert potential(leaf, op, 3, 10, 80)['values']['enemy_hp_removed'] > 0
    leaf['terminal_decision'] = 'victory'
    assert all(value == 0 for value in potential(leaf, op, 3, 10, 80)['values'].values())
    assert scorer.score(root, leaf, []) == 1_000_000


def test_damage_potentials_share_overkill_cap():
    scorer, raw = CombatScoring(), state(hand=('STRIKE_IRONCLAD',), draw=())
    leaf = powered(powered(raw, 'STRENGTH', 100), 'RUPTURE', 100)
    leaf['combat']['enemies'][0]['hp'] = 7
    op = opportunities(leaf, scorer.cards, 10)
    values = potential(leaf, op, 3, 10, 80)
    assert 0 < values['values']['enemy_hp_removed'] <= REALIZATION / 10


def test_shared_energy_cannot_buy_full_attack_and_full_defense():
    scorer, raw = CombatScoring(), state(hand=('STRIKE_IRONCLAD', 'DEFEND_IRONCLAD'))
    raw['combat']['player']['energy'] = 1
    op = opportunities(raw, scorer.cards, 10)['immediate']
    assert not (op['damage'] == 6 and op['block'] == 5)
    assert op['hits'] <= 1


def test_excess_block_and_plating_do_not_earn_unlimited_value():
    scorer, root = CombatScoring(), state(hand=(), draw=())
    enough, excess = copy.deepcopy(root), copy.deepcopy(root)
    enough['combat']['player']['block'] = 10
    excess['combat']['player']['block'] = 100
    assert scorer.score(root, enough, []) == scorer.score(root, excess, [])
    assert scorer.features(root, powered(enough, 'PLATING', 20), [])['opportunities']['leaf_potential']['values']['hp_change'] > 0
    no_attack = powered(root, 'PLATING', 20)
    no_attack['combat']['enemies'][0]['intent'] = {}
    assert scorer.features(root, no_attack, [])['opportunities']['leaf_potential']['values']['hp_change'] == 0


def test_missing_piles_are_not_fabricated_and_cannot_enter_training():
    scorer, root = CombatScoring(), state()
    leaf = powered(root, 'RUPTURE', 1)
    del leaf['combat']['draw_pile']
    incomplete = scorer.features(root, leaf, [])
    assert incomplete['training_eligible'] is False
    assert 'draw_pile' in incomplete['opportunities']['leaf']['missing_piles']
    complete = scorer.features(root, root, [])
    with pytest.raises(ValueError, match='complete'):
        fit_pairs([{'a': incomplete, 'b': complete, 'target': 1}])
    old = {**complete, 'version': 'combat-preference-2'}
    with pytest.raises(ValueError, match='version'):
        fit_pairs([{'a': old, 'b': complete, 'target': 1}])
    assert fit_pairs([{'a': complete, 'b': complete, 'target': 0.5}], steps=2)['trained']


def test_new_model_keeps_terminal_failure_priority():
    scorer, root = CombatScoring(), state()
    assert scorer.score(root, {'success': False, 'combat': {}}, []) == float('-inf')
    assert scorer.score(root, {**root, 'terminal_decision': 'defeat'}, []) == -1_000_000
