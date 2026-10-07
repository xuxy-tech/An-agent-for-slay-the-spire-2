import copy
import json

import pytest

from controller.combat_observation import CombatObservation, CombatObservationPair
from controller.combat_scoring import CombatScoring


def state():
    return {'success': True, 'character': 'UNKNOWN_CHARACTER', 'combat': {
        'encounter_id': 'TEST', 'turn_number': 2, 'round_number': 2,
        'is_player_turn': True,
        'player': {'hp': 40, 'max_hp': 80, 'block': 0, 'energy': 0,
                   'powers': [{'id': 'UNKNOWN_POWER', 'amount': -2, 'extra': {'x': 7}}],
                   'relics': [], 'stars': 0},
        'enemies': [
            {'index': 0, 'monster_id': 'SAME', 'hp': 10, 'intent': None},
            {'index': 1, 'monster_id': 'SAME', 'hp': 20, 'intent': {'hits': 0}}],
        'hand': [{'card_id': 'UNKNOWN_CARD', 'display_cost': 0, 'current_cost': None,
                  'affliction': 'UNKNOWN_AFFLICTION', 'extension': {'a': [1, None]}}],
        'draw_pile': [{'card_id': 'HIDDEN_A'}, {'card_id': 'HIDDEN_B'}],
        'discard_pile': [], 'exhaust_pile': [], 'play_pile': [],
        'available_actions': [{'action_type': 'end_turn', 'metadata': {'future': 1}}],
        'future_mechanic': {'opaque_id': 'NEW', 'charges': 0}},
        'leaf_settlement': {'phase': 'post_enemy_turn'}}


def test_lossless_roundtrip_unknown_entities_order_and_nested_extensions():
    original = state()
    observation = CombatObservation(original)
    record = json.loads(json.dumps(observation.to_record()))
    restored = CombatObservation.from_record(record)
    assert restored.to_record()['state'] == original
    assert [row['hp'] for row in restored.enemies.value] == [10, 20]
    assert [row['card_id'] for row in restored.piles['draw_pile'].value] == ['HIDDEN_A', 'HIDDEN_B']
    assert restored.at('combat', 'future_mechanic', 'charges').value == 0
    assert restored.context['settlement'].value['phase'] == 'post_enemy_turn'


def test_missing_null_zero_empty_and_unavailable_are_distinct():
    observation = CombatObservation(state())
    assert observation.at('combat', 'player', 'potions').status == 'missing'
    assert observation.at('combat', 'hand', 0, 'current_cost').status == 'null'
    assert observation.at('combat', 'player', 'energy').to_dict()['value'] == 0
    assert observation.at('combat', 'player', 'relics').to_dict()['value'] == []
    assert observation.at('combat', 'enemies', 0, 'intent', 'hits').status == 'unavailable'
    assert observation.at('combat', 'enemies', 1, 'intent', 'hits').value == 0
    assert observation.at('combat', 'enemies', -1).status == 'missing'


def test_observations_are_detached_and_recursively_immutable():
    original = state()
    pair = CombatObservationPair(original, original, [{'before': original}], 'mid')
    original['combat']['player']['hp'] = 1
    assert pair.root.player.value['hp'] == 40
    assert pair.trace[0]['before']['combat']['player']['hp'] == 40
    with pytest.raises(TypeError):
        pair.root.player.value['hp'] = 1
    with pytest.raises(TypeError):
        pair.root.piles['hand'].value[0]['extension']['a'][0] = 3
    exported = pair.to_record()
    exported['root']['state']['combat']['player']['hp'] = 5
    assert pair.root.player.value['hp'] == 40


def test_model_policy_excludes_piles_counts_and_trace_copies():
    original = state()
    original['combat']['draw_pile_count'] = 2
    pair = CombatObservationPair(original, original, [{'before': original}], 'late')
    encoded = json.dumps(pair.to_record())
    assert 'HIDDEN_A' not in encoded
    assert 'draw_pile' not in encoded
    assert pair.root.piles['draw_pile'].status == 'excluded'
    assert pair.root.at('combat', 'draw_pile_count').status == 'excluded'
    assert pair.root.piles['hand'].value[0]['card_id'] == 'UNKNOWN_CARD'
    assert CombatObservationPair.from_record(pair.to_record()).to_record() == pair.to_record()


def test_terminal_views_never_use_synthetic_combat_placeholders():
    terminal = state()
    terminal.update(terminal_decision='victory', terminal_result={'player': {'hp': 35}})
    terminal['combat']['player']['powers'] = []
    terminal['combat']['enemies'] = []
    view = CombatObservation(terminal)
    assert view.player.path == ('terminal_result', 'player')
    assert view.player.value == {'hp': 35}
    assert view.enemies.status == 'missing'
    assert view.piles['hand'].status == 'missing'
    assert view.context['turn'].status == 'missing'
    # Audit retains the original placeholders, without presenting them as native.
    assert view.to_record()['state'] == terminal


def test_terminal_null_player_is_not_replaced_by_stale_combat_player():
    terminal = state()
    terminal.update(terminal_decision='death', terminal_result={'player': None, 'enemies': []})
    view = CombatObservation(terminal)
    assert view.player.status == 'null'
    assert view.enemies.to_dict()['value'] == []


def test_failed_and_unsettled_states_are_not_promoted_to_settled():
    view = CombatObservation({'success': False, 'terminal_decision': 'unknown', 'combat': {}})
    assert view.context['success'].value is False
    assert view.context['settlement'].status == 'missing'
    midturn = state()
    del midturn['leaf_settlement']
    assert CombatObservation(midturn).context['settlement'].status == 'missing'


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), object(), {1: 'invalid key'}])
def test_invalid_json_is_rejected_without_coercion(bad):
    original = state()
    original['future'] = bad
    with pytest.raises(TypeError):
        CombatObservation(original)


def test_schema_and_information_policy_are_explicit_contracts():
    with pytest.raises(ValueError, match='search-state'):
        CombatObservation({'Players': []})  # A restore snapshot is not search input.
    with pytest.raises(ValueError, match='policy'):
        CombatObservation(state(), information_policy='guess')
    record = CombatObservation(state()).to_record()
    record['schema'] = 'future'
    with pytest.raises(ValueError, match='schema'):
        CombatObservation.from_record(record)
    pair = CombatObservationPair(state(), state(), [], 'mid').to_record()
    pair['outcome']['information_policy'] = 'supplied_state'
    with pytest.raises(ValueError, match='policies differ'):
        CombatObservationPair.from_record(pair)


def test_shared_scorer_additive_api_preserves_score_features_and_inputs(monkeypatch):
    scorer = CombatScoring('mid')
    root, outcome = state(), state()
    outcome['combat']['player']['hp'] = 35
    trace = [{'action': {'action_type': 'end_turn'}, 'before': root}]
    original = copy.deepcopy((root, outcome, trace))
    expected_features = scorer.features(root, outcome, trace)
    expected_score = scorer.score(root, outcome, trace)
    pair = scorer.observation(root, outcome, trace)
    assert pair.stage == 'mid'
    assert pair.root.information_policy == 'simulated_hand_unordered_piles_v1'
    assert (root, outcome, trace) == original
    assert scorer.features(root, outcome, trace) == expected_features
    # Representation stays off the existing score/explain hot path.
    monkeypatch.setattr(scorer, 'observation', lambda *args: pytest.fail('unexpected migration'))
    assert scorer.score(root, outcome, trace) == expected_score
    assert scorer.explain(root, outcome, trace)['score'] == expected_score
