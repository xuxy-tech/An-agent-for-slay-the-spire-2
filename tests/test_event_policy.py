from controller.run_agent import (
    _event_option_effects,
    _event_option_text,
    choose_event_option,
)


def event_state(hp=73, max_hp=80, loss=3):
    return {
        'event_id': 'TABLET_OF_TRUTH',
        'player': {'hp': hp, 'max_hp': max_hp, 'gold': 117, 'deck': []},
        'options': [
            {
                'index': 0,
                'title': '\u89e3\u8bfb',
                'description': f'\u5931\u53bb[red]{loss}[/red]\u70b9\u6700\u5927\u751f\u547d\u3002\u968f\u673a[gold]\u5347\u7ea7[/gold]\u4e00\u5f20\u724c\u3002',
                'option_id': 'TABLET_OF_TRUTH.pages.INITIAL.options.DECIPHER',
                'will_kill_player': False,
            },
            {
                'index': 1,
                'title': '\u653e\u5f03',
                'description': '\u79bb\u5f00\u3002',
                'option_id': 'TABLET_OF_TRUTH.pages.DECIPHER.options.GIVE_UP',
                'will_kill_player': False,
            },
        ],
    }


def test_event_markup_is_not_scored_as_gold_and_cost_is_structured():
    option = event_state()['options'][0]
    assert 'GOLD' not in _event_option_text(option)
    assert _event_option_effects(option)['max_hp_loss'] == 3.0


def test_event_max_hp_loss_cap_rejects_tablet_escalation():
    assert choose_event_option(event_state(loss=12)) == {'option_index': 1}
    assert choose_event_option(event_state(max_hp=35, loss=6)) == {'option_index': 1}


def test_event_never_selects_explicitly_lethal_option_when_alternative_exists():
    state = event_state()
    state['options'][0].update(
        description='Gain a relic.',
        will_kill_player=True,
    )
    assert choose_event_option(state) == {'option_index': 1}


def test_event_direct_hp_loss_cannot_cross_safety_floor():
    state = event_state(hp=12, max_hp=80)
    state['options'][0].update(
        description='Lose 12 HP. Gain a relic.',
        will_kill_player=False,
    )
    assert choose_event_option(state) == {'option_index': 1}


def test_gold_loss_scoring_remains_defined_after_effect_parsing():
    state = event_state()
    state['options'][0].update(
        description='Lose 25 gold. Gain a relic.',
        will_kill_player=False,
    )
    assert choose_event_option(state)['option_index'] in {0, 1}
