import copy
import math

import pytest

from controller.search.actions import SearchAction
from controller.search.combat_search import CombatSearcher, CombatSpec, SearchResult
from controller.search.evaluator import evaluate_leaf, explain_leaf_score, extract_leaf_features
from controller.search.state_cache import hash_search_state_for_plan_reuse


def state(turn=1, hp=40, enemy_hp=20, intent='Attack'):
    return {'success': True, 'combat': {
        'turn_number': turn, 'round_number': turn, 'is_player_turn': True,
        'player': {'hp': hp, 'max_hp': 40, 'block': 0, 'energy': 3, 'powers': []},
        'enemies': [{'index': 0, 'monster_id': 'TEST', 'hp': enemy_hp, 'max_hp': 20,
                     'block': 0, 'powers': [],
                     'intent': {'intent_types': [intent], 'display_damage': 8 if intent == 'Attack' else None}}],
        'hand': [], 'draw_pile': [], 'discard_pile': [], 'exhaust_pile': [],
        'available_actions': [{'action_type': 'end_turn'}]}}


def searcher():
    result = CombatSearcher(None, CombatSpec('Ironclad', 'TEST', 'fixed'), score_mode='balanced')
    result._root_summary = result._combat_summary(state())
    return result


def test_end_turn_keeps_actual_next_intent_and_checkpoint(monkeypatch):
    subject = searcher()
    before = state()
    after = state(turn=2, hp=32, intent='Buff')
    original = copy.deepcopy(after)
    monkeypatch.setattr(subject, 'combat_to_state', lambda history: after)
    result = subject._evaluate_child_action(SearchAction('end_turn'), before, [], 3, 1, 3,
                                             next_history=[], child_state=before)
    assert result.leaf_state['combat']['enemies'][0]['intent']['intent_types'] == ['Buff']
    assert result.leaf_state['combat']['player']['hp'] == 32
    assert result.state_hashes_after_actions == [hash_search_state_for_plan_reuse(after)]
    assert result.score == evaluate_leaf(after, subject._root_summary, 'balanced')
    assert result.leaf_state['leaf_settlement']['phase'] == 'post_enemy_turn'
    assert after == original
    assert before['combat']['enemies'][0]['intent']['intent_types'] == ['Attack']


@pytest.mark.parametrize('bad', [
    {'success': False, 'error': 'restore failed'},
    {'success': True, 'terminal_decision': 'card_select'},
    {'success': True, 'terminal_decision': 'victory', 'terminal_surviving_enemy_hp': 5},
    state(turn=1),
    {'success': True, 'combat': {'is_player_turn': False, 'turn_number': 2}},
    {'success': True, 'combat': {'is_player_turn': True}},
])
def test_unresolved_failed_and_noop_turns_are_not_scored(monkeypatch, bad):
    subject = searcher()
    monkeypatch.setattr(subject, 'combat_to_state', lambda history: bad)
    result = subject._settled_leaf_result(state(), [])
    assert result.leaf_state['success'] is False
    assert result.leaf_state['leaf_settlement']['phase'] == 'failed'
    assert result.score == float('-inf')
    assert result.sequence == []
    assert subject.timing_summary()['failed_settlements'] == 1
    assert subject.timing_summary()['completed_turn_lines'] == 0
    explanation = explain_leaf_score(result.leaf_state, subject._root_summary, 'balanced')
    assert explanation['total'] is None
    assert explanation['comparable'] is False


def test_exception_does_not_return_unsettled_hp(monkeypatch):
    subject = searcher()
    def fail(history):
        raise TimeoutError('engine timeout')
    monkeypatch.setattr(subject, 'combat_to_state', fail)
    result = subject._budget_leaf_result(state(), [])
    assert result.score == float('-inf')
    assert 'engine timeout' in result.leaf_state['error']
    assert result.sequence == []


def test_budget_cutoff_returns_the_end_turn_it_actually_scored(monkeypatch):
    subject = searcher()
    after = state(turn=2, hp=32, intent='Buff')
    monkeypatch.setattr(subject, 'combat_to_state', lambda history: after)
    result = subject._budget_leaf_result(state(), [])
    assert [action.action_type for action in result.sequence] == ['end_turn']
    assert result.state_hashes_after_actions == [hash_search_state_for_plan_reuse(after)]
    assert result.score == evaluate_leaf(after, subject._root_summary, 'balanced')
    assert subject.timing_summary()['completed_turn_lines'] == 1


def test_empty_horizon_returns_an_explicit_settlement_plan(monkeypatch):
    subject = searcher()
    monkeypatch.setattr(subject, 'combat_to_state', lambda history: state(turn=2))
    monkeypatch.setattr(subject, '_prepare_action_candidates', lambda *args: [])
    result = subject._search_from_state(state(), [], 0, 0, 0, is_root=True)
    assert [action.action_type for action in result.sequence] == ['end_turn']
    assert result.leaf_state['combat']['turn_number'] == 2


def test_multiturn_score_leaf_and_path_have_the_same_horizon(monkeypatch):
    subject = searcher()
    after = state(turn=2, hp=32, intent='Buff')
    final = state(turn=3, hp=24, enemy_hp=5)
    continuation = SearchResult(score=42, sequence=[SearchAction('play_card', card_index=0), SearchAction('end_turn')],
                                leaf_state=final, stats={'nodes': 5},
                                state_hashes_after_actions=['after-card', 'after-second-turn'],
                                state_keys_after_actions=[{'stage': 'card'}, {'stage': 'turn'}])
    monkeypatch.setattr(subject, 'combat_to_state', lambda history: after)
    monkeypatch.setattr(subject, '_search_from_state', lambda *args: continuation)
    result = subject._evaluate_child_action(SearchAction('end_turn'), state(), [], 3, 2, 3,
                                             next_history=[], child_state=after)
    assert result.score == 42
    assert result.leaf_state is final
    assert [action.action_type for action in result.sequence] == ['end_turn', 'play_card', 'end_turn']
    assert len(result.state_hashes_after_actions) == len(result.state_keys_after_actions) == 3


def test_invalid_engine_result_cannot_beat_a_real_defeat():
    subject = searcher()
    dead = {'success': True, 'terminal_decision': 'defeat', 'terminal_result': {'player': {'hp': 0}}}
    failed = {'success': False, 'terminal_decision': 'error', 'combat': {'player': {'hp': 40}}}
    bad = ({'action': SearchAction('play_card', card_index=0)},
           SearchResult(score=12345, sequence=[], leaf_state=failed, stats={}))
    good = ({'action': SearchAction('end_turn')},
            SearchResult(score=evaluate_leaf(dead), sequence=[], leaf_state=dead, stats={}))
    selected, audit = subject._select_root_candidate([bad, good])
    assert selected is good
    assert audit['invalid_root_candidates'] == 1
    assert subject._select_root_candidate([bad])[0] is None


def test_end_turn_victory_is_allowed_but_living_enemy_victory_is_rejected():
    leaf = {'success': True, 'terminal_decision': 'victory', 'terminal_on_end_turn': True,
            'terminal_root_enemy_hp': 100, 'terminal_surviving_enemy_hp': 0,
            'terminal_result': {'player': {'hp': 10, 'max_hp': 40}}}
    assert evaluate_leaf(leaf) > 900000
    leaf['terminal_surviving_enemy_hp'] = 1
    assert evaluate_leaf(leaf) == float('-inf')


def test_intent_changes_alone_are_not_damage():
    root = state(enemy_hp=50)['combat']
    leaf = state(enemy_hp=50, intent='Sleep')
    features = extract_leaf_features(leaf, root)
    for name in ('enemy_hp_loss', 'weighted_enemy_hp_loss', 'focused_enemy_hp_loss'):
        assert features[name] == 0


def test_actual_damage_uses_root_weights_regardless_of_next_intent():
    root = state(enemy_hp=50)['combat']
    a = extract_leaf_features(state(enemy_hp=44, intent='Attack'), root)
    b = extract_leaf_features(state(enemy_hp=44, intent='Buff'), root)
    assert a['weighted_enemy_hp_loss'] == b['weighted_enemy_hp_loss'] == pytest.approx(6 * 1.64)
    assert a['focused_enemy_hp_loss'] == b['focused_enemy_hp_loss'] == pytest.approx((50**2 - 44**2) * 1.64)
    assert a['incoming_damage'] == 8
    assert b['incoming_damage'] == 0


def test_enemy_removal_does_not_assign_old_slot_threat_to_survivor():
    root = state(enemy_hp=10)['combat']
    other = copy.deepcopy(root['enemies'][0])
    other.update(index=1, monster_id='OTHER', hp=20, intent={'intent_types': ['Buff']})
    root['enemies'].append(other)
    survivor = copy.deepcopy(other)
    survivor.update(index=0, intent={'intent_types': ['Attack'], 'display_damage': 99})
    leaf = state()
    leaf['combat']['enemies'] = [survivor]
    features = extract_leaf_features(leaf, root)
    assert features['weighted_enemy_hp_loss'] == pytest.approx(10 * 1.64)
    assert features['focused_enemy_hp_loss'] == pytest.approx(100 * 1.64)


def test_spawned_population_uses_conservative_unweighted_damage():
    root = state(enemy_hp=50)['combat']
    leaf = state(enemy_hp=50, intent='Sleep')
    leaf['combat']['enemies'][0]['monster_id'] = 'NEW_MODEL'
    features = extract_leaf_features(leaf, root)
    assert features['weighted_enemy_hp_loss'] == features['focused_enemy_hp_loss'] == 0


def test_corpus_checks_use_raw_engine_leaves_without_rewriting_history():
    from controller.combat_regressions import DEFAULT_CORPUS, check_case, read_json
    case = DEFAULT_CORPUS / 'rupture_before_self_damage'
    replay = read_json(case / 'replay.json')
    result = check_case(case, score_mode='legacy')
    first = result['candidates'][0]
    expected = explain_leaf_score(replay['candidates'][0]['engine_leaf_state'], replay['root_summary'],
                                 replay['score_mode'], replay['coefficients'])
    assert first['leaf_source'] == 'engine_leaf_state'
    assert first['score'] == expected['total']
    assert first['historical_score'] == first['baseline_score'] == 199.0016
    assert math.isfinite(first['score'])


# Beam ranking must evaluate an end-turn probe while continuing from the
# untouched player node. These small transition graphs exercise the real beam.
def beam_harness(monkeypatch, transitions, depth=3):
    subject = searcher()
    subject.beam_width = 2
    subject.search_mode = 'beam'
    subject._preference_root = copy.deepcopy(transitions[()])
    calls, scored = [], []

    def replay(history):
        key = tuple('end' if a.action == 'end_turn' else a.args['card_index'] for a in history)
        calls.append(key)
        value = transitions[key]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def score(leaf, history, **kwargs):
        assert leaf.get('leaf_settlement', {}).get('phase') in {'post_enemy_turn', 'combat_terminal', 'failed'}
        scored.append((copy.deepcopy(leaf), list(history), kwargs.get('collect_leaf', True)))
        if leaf.get('success') is False:
            return float('-inf')
        if leaf.get('terminal_decision') == 'defeat':
            return -1000000.0
        if leaf.get('terminal_decision') == 'victory':
            return 1000000.0
        combat = leaf['combat']
        return combat['player']['hp'] + combat['player']['block'] - sum(e['hp'] for e in combat['enemies'])

    monkeypatch.setattr(subject, 'combat_to_state', replay)
    monkeypatch.setattr(subject, '_prepare_action_candidates', lambda st, h, actions: [{'action': a} for a in actions])
    monkeypatch.setattr(subject, '_score_with_history', score)
    return subject, calls, scored


def beam_node(cards=(), *, turn=1, hp=40, enemy_hp=20, block=0):
    node = state(turn=turn, hp=hp, enemy_hp=enemy_hp)
    node['combat']['player']['block'] = block
    node['combat']['available_actions'] = [{'action_type': 'end_turn'}] + [
        {'action_type': 'play_card', 'card_index': index} for index in cards]
    return node


def test_beam_ranks_settled_outcomes_and_keeps_original_expansion_nodes(monkeypatch):
    transitions = {
        (): beam_node([0, 1, 2]), ('end',): beam_node(turn=2, hp=20),
        (0,): beam_node([3], block=100),  # Misleading, non-retained excess block.
        (1,): beam_node([3], enemy_hp=10),
        (2,): beam_node([3], block=10),
        (0, 'end'): beam_node(turn=2, hp=30),
        (1, 'end'): beam_node(turn=2, hp=30, enemy_hp=10),
        (2, 'end'): beam_node(turn=2, hp=40),
        (1, 3): beam_node(enemy_hp=1), (2, 3): beam_node(enemy_hp=15),
        (1, 3, 'end'): beam_node(turn=2, hp=40, enemy_hp=1),
        (2, 3, 'end'): beam_node(turn=2, hp=40, enemy_hp=15),
    }
    original = copy.deepcopy(transitions)
    subject, calls, scored = beam_harness(monkeypatch, transitions)
    result = subject._search_beam(transitions[()], [], 3, 1)
    assert transitions == original
    assert (0, 3) not in calls
    assert (1, 3) in calls  # Probe end_turn was not appended to expansion history.
    assert [(a.action_type, a.card_index) for a in result.sequence] == [
        ('play_card', 1), ('play_card', 3), ('end_turn', None)]
    assert calls.count((1, 'end')) == 1  # Reused when end_turn becomes a candidate.
    assert calls.count((1, 3, 'end')) == 1
    assert result.stats['beam_settlement_cache_hits'] >= 2
    assert result.stats['beam_ranking_policy'] == 'settled_end_turn_v1'
    assert all(h[-1].action == 'end_turn' for leaf, h, collect in scored if not collect)


def test_beam_probe_death_can_continue_to_survival_but_failed_probe_is_excluded(monkeypatch):
    dead = {'success': True, 'terminal_decision': 'defeat',
            'terminal_result': {'player': {'hp': 0}}}
    win = {'success': True, 'terminal_decision': 'victory',
           'terminal_surviving_enemy_hp': 0, 'terminal_result': {'player': {'hp': 20}}}
    transitions = {(): beam_node([0, 1]), ('end',): dead,
                   (0,): beam_node([2]), (0, 'end'): dead,
                   (1,): beam_node([2]), (1, 'end'): TimeoutError('probe timeout'),
                   (0, 2): win}
    subject, calls, scored = beam_harness(monkeypatch, transitions)
    result = subject._search_beam(transitions[()], [], 3, 1)
    assert (0, 2) in calls and (1, 2) not in calls
    assert (0, 2, 'end') not in calls  # Terminal victory needs no extra end_turn.
    assert result.leaf_state['terminal_decision'] == 'victory'
    assert [a.action_type for a in result.sequence] == ['play_card', 'play_card']
    assert result.stats['beam_settlement_failures'] == 1
    assert all(leaf.get('success') for leaf, _, collect in scored if not collect)


def test_beam_budget_stops_probes_without_using_unsettled_scores(monkeypatch):
    transitions = {(): beam_node([0, 1]), ('end',): beam_node(turn=2, hp=30),
                   (0,): beam_node(), (1,): beam_node(),
                   (0, 'end'): beam_node(turn=2, hp=35)}
    subject, calls, scored = beam_harness(monkeypatch, transitions)
    monkeypatch.setattr(subject, '_time_budget_exhausted', lambda: (0, 'end') in calls)
    result = subject._search_beam(transitions[()], [], 3, 1)
    assert (1, 'end') not in calls
    assert result.stats['beam_unranked_budget_nodes'] == 1
    assert [a.action_type for a in result.sequence] == ['end_turn']
    assert result.leaf_state['combat']['player']['hp'] == 30


def test_beam_settlement_cache_is_local_to_each_search(monkeypatch):
    transitions = {(): beam_node([0]), ('end',): beam_node(turn=2, hp=30),
                   (0,): beam_node(), (0, 'end'): beam_node(turn=2, hp=35)}
    subject, calls, scored = beam_harness(monkeypatch, transitions)
    subject._search_beam(transitions[()], [], 2, 1)
    subject._search_beam(transitions[()], [], 2, 1)
    assert calls.count((0, 'end')) == 2


def test_beam_probe_cache_does_not_suppress_real_candidate_training_record():
    from controller.search.combat_search import RecordedAction
    samples = []
    subject = CombatSearcher(None, CombatSpec('Ironclad', 'TEST', 'fixed'),
                             score_mode='preference', leaf_dump_sink=samples.append)
    subject._preference_root = state()
    subject.timing['beam_ranking_policy'] = 'settled_end_turn_v1'
    leaf = {**state(turn=2, hp=32), 'leaf_settlement': {'phase': 'post_enemy_turn'}}
    history = [RecordedAction('play_card', {'card_index': 0}), RecordedAction('end_turn', {})]
    probe = subject._score_with_history(leaf, history, collect_leaf=False)
    assert samples == []
    actual = subject._score_with_history(leaf, history)
    assert probe == actual
    assert len(samples) == 1
    assert samples[0]['action_sequence'][-1]['action_type'] == 'end_turn'
    assert samples[0]['search_context']['beam_ranking_policy'] == 'settled_end_turn_v1'


def test_legacy_beam_probe_is_not_exported_as_a_training_leaf():
    samples = []
    subject = searcher()
    subject.leaf_dump_sink = samples.append
    leaf = {**state(turn=2), 'leaf_settlement': {'phase': 'post_enemy_turn'}}
    probe = subject._score_with_history(leaf, [], collect_leaf=False)
    assert samples == []
    assert subject._score_with_history(leaf, []) == probe
    assert len(samples) == 1


def test_beam_depth_cap_returns_the_settlement_action_it_scores(monkeypatch):
    transitions = {(): beam_node([0]), ('end',): beam_node(turn=2, hp=20),
                   (0,): beam_node(block=10), (0, 'end'): beam_node(turn=2, hp=40)}
    subject, calls, scored = beam_harness(monkeypatch, transitions)
    result = subject._search_beam(transitions[()], [], 1, 1)
    assert [a.action_type for a in result.sequence] == ['play_card', 'end_turn']
    assert result.leaf_state['combat']['turn_number'] == 2
    assert all(collect for _, _, collect in scored)  # No frontier probes at depth cap.
