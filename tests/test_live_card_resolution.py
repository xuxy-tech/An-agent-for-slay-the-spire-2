from controller.run_agent import resolve_live_planned_action
from controller.search.actions import SearchAction


def _action(index, *, upgrade=0, affliction=None, affliction_count=None):
    return {
        'action_type': 'play_card',
        'card_index': index,
        'target_index': None,
        'metadata': {
            'card_id': 'DEFEND_IRONCLAD',
            'target_type': 'Self',
            'upgrade': upgrade,
            'current_cost': 1,
            'display_cost': 1,
            'display_costs_x': False,
            'keywords': None,
            'affliction': affliction,
            'affliction_count': affliction_count,
        },
    }


def _state(*actions):
    return {'combat': {'available_actions': [{'action_type': 'end_turn'} , *actions]}}


def test_resolver_follows_upgraded_instance_when_duplicate_order_changes():
    current = _state(_action(0, upgrade=0), _action(1, upgrade=1))
    chosen = SearchAction(
        action_type='play_card',
        card_index=3,
        metadata=_action(3, upgrade=1)['metadata'],
    )

    assert resolve_live_planned_action(current, chosen) == ('play_card', {'card_index': 1})


def test_resolver_distinguishes_afflicted_duplicate_instance():
    current = _state(
        _action(0, affliction='REPLAY', affliction_count=1),
        _action(1, affliction=None, affliction_count=None),
    )
    chosen = SearchAction(
        action_type='play_card',
        card_index=4,
        metadata=_action(4, affliction='REPLAY', affliction_count=1)['metadata'],
    )

    assert resolve_live_planned_action(current, chosen) == ('play_card', {'card_index': 0})


def test_resolver_fails_closed_when_semantic_instance_is_missing():
    current = _state(_action(0, upgrade=0))
    chosen = SearchAction(
        action_type='play_card',
        card_index=3,
        metadata=_action(3, upgrade=1)['metadata'],
    )

    assert resolve_live_planned_action(current, chosen) is None
