from controller.turn_learning import enumerate_turn, preference_record, turn_groups
from controller.search.combat_search import RecordedAction


class Engine:
    def combat_to_state(self, history):
        last = history[-1].action if history else None
        return {'success': True, 'combat': {'turn_number': 2 if last == 'end_turn' else 1,
                'player': {'hp': 10}, 'hand': [], 'enemies': [],
                'available_actions': [{'action_type': 'end_turn'}] +
                ([] if history else [{'action_type': 'play_card', 'card_index': 0}])}}

    def _extract_search_state(self, value):
        return value

    def _recorded_action_from_search_action(self, action):
        return RecordedAction(action.action_type, {})

    def _validate_settled_transition(self, before, after):
        assert before['combat']['turn_number'] == 1
        assert after['combat']['turn_number'] == 2
        return after


class Scorer:
    def valid_leaf(self, state):
        return True

    def features(self, root, leaf, trace):
        assert leaf['combat']['turn_number'] == 2
        return {'training_eligible': True, 'values': {'length': len(trace)}}

    def score(self, *args):
        raise AssertionError('Offline collector must never rank by the old scorer')


def test_all_routes_end_at_real_boundary_without_scoring():
    root, leaves, coverage = enumerate_turn(Engine(), Scorer())
    assert coverage['exhaustive'] is True
    assert [len(row['path']) for row in leaves] == [1, 2]
    assert coverage['expanded_edges'] == 3


def test_cutoff_is_not_a_fabricated_settled_leaf():
    root, leaves, coverage = enumerate_turn(Engine(), Scorer(), max_actions=1)
    assert coverage['exhaustive'] is False
    assert coverage['depth_cutoffs'] == 1
    assert len(leaves) == 1


def test_modal_choices_are_enumerated_before_settlement():
    class ModalEngine(Engine):
        def combat_to_state(self, history):
            if history and history[-1].action == 'play_card':
                return {'success': True, 'terminal_decision': 'card_select',
                        'terminal_result': {'cards': [{'index': 0}, {'index': 1}],
                                            'min_select': 1, 'max_select': 1}}
            return super().combat_to_state(history)

    _, leaves, coverage = enumerate_turn(ModalEngine(), Scorer(),
                                        max_nodes=0, max_seconds=0, max_actions=0)
    assert coverage['exhaustive'] is True
    paths = [leaf['path'] for leaf in leaves if len(leaf['path']) == 3]
    assert {path[1]['args']['indices'] for path in paths} == {'0', '1'}
    assert all(path[-1]['action_type'] == 'end_turn' for path in paths)


def test_offline_modal_payload_keeps_selected_indices():
    from scripts.generate_turn_preferences import OfflineReplay
    assert OfflineReplay._strip_cli_payload({'indices': '0,2'}, 'select_cards') == {'indices': '0,2'}


def test_human_completed_outcome_is_used_for_every_observed_comparison():
    root, leaves, coverage = enumerate_turn(Engine(), Scorer())
    human = leaves[-1]
    row = preference_record({'demonstration': human, 'leaves': leaves,
        'root_id': 'r', 'session_id': 's', 'coverage': coverage})
    assert row['fit_ready']
    assert len(row['pairwise_examples']) == 1
    assert row['pairwise_examples'][0]['chosen'] == human['features']


def test_grouping_keeps_potions_and_overlap_evidence():
    rows = [{'record_type': 'decision', 'combat_id': 'a', 'turn': 1,
             'observation_before': {'in_combat': True},
             'action': {'type': kind}, 'settlement': 'ambiguous_overlap'}
            for kind in ['play_card', 'use_potion', 'end_turn']]
    assert turn_groups(rows) == [rows]
