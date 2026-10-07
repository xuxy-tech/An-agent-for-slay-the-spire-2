import copy
from pathlib import Path

import pytest

from controller.combat_abilities import RULES, ability_potential
from controller.combat_comparison import _cache_key
from controller.combat_opportunity import opportunities
from controller.combat_scoring import CombatScoring, active_model
from controller.preference_model import fit_pairs
from tests.test_combat_opportunity import state, powered


def forecast(raw, turns=3, incoming=10):
    scorer = CombatScoring()
    return ability_potential(raw, opportunities(raw, scorer.cards, incoming), turns, incoming,
                             raw['combat']['player']['hp'])


def test_rules_have_no_independent_prices_and_cover_more_than_four_mechanisms():
    assert len({rule.power for rule in RULES}) >= 12
    forbidden = {'strength_potential', 'self_damage_scaling', 'strength_growth', 'recurring_block'}
    assert not (forbidden & active_model()['weights'].keys())
    assert all(not hasattr(rule, 'weight') for rule in RULES)


def test_damage_weight_prices_both_realized_damage_and_strength():
    root = state(hand=('STRIKE_IRONCLAD',), draw=('STRIKE_IRONCLAD',))
    root['combat']['enemies'][0]['intent'] = {}
    leaf = powered(root, 'STRENGTH', 2)
    model = active_model()
    before = CombatScoring(model=model).explain(root, leaf, [])
    model = copy.deepcopy(model)
    model['weights']['enemy_hp_removed'] *= 2
    after = CombatScoring(model=model).explain(root, leaf, [])
    assert after['ability_score'] == pytest.approx(before['ability_score'] * 2)
    assert after['base_score'] == pytest.approx(before['base_score'])
    assert before['feature_record']['base_values']['enemy_hp_removed'] == 0


def test_defense_uses_health_marginal_risk_and_preserves_saturation():
    healthy = state(hand=(), draw=())
    healthy['combat']['enemies'][0]['hp'] = 18  # One-turn marginal risk, no inevitable death.
    healthy['combat']['enemies'][0]['intent']['total_damage'] = 4
    fragile = copy.deepcopy(healthy)
    fragile['combat']['player']['hp'] = 6
    def explanation(root):
        return CombatScoring().explain(root, powered(root, 'PLATING', 3), [])
    a, b = explanation(healthy), explanation(fragile)
    assert b['feature_record']['ability_values']['hp_risk'] > a['feature_record']['ability_values']['hp_risk']
    row = state(hand=(), draw=())
    row['combat']['player']['block'] = 100
    assert forecast(powered(row, 'PLATING', 100), turns=1)['values']['hp_change'] == 0


def test_exhaust_block_damage_chain_and_event_units():
    root = state(hand=('TRUE_GRIT',), draw=('TRUE_GRIT',))
    juggernaut = powered(root, 'JUGGERNAUT', 6)
    combined = powered(juggernaut, 'FEEL_NO_PAIN', 3)
    alone = forecast(juggernaut, turns=1)
    joint = forecast(combined, turns=1)
    assert joint['values']['enemy_hp_removed'] > alone['values']['enemy_hp_removed'] > 0
    triggered = [e for e in joint['turns'][0]['powered']['events'] if e['power'] == 'JUGGERNAUT']
    assert sum(e['events'] for e in triggered) == 2  # card block + exhaust-trigger block
    bigger = powered(juggernaut, 'DEXTERITY', 10)
    assert forecast(bigger, turns=1)['values']['enemy_hp_removed'] == alone['values']['enemy_hp_removed']


def test_draw_resources_become_effects_without_independent_draw_reward():
    root = state(hand=('TRUE_GRIT',), draw=('ANGER',) * 8)
    root['combat']['player']['energy'] = 1
    raw = powered(root, 'DARK_EMBRACE', 2)
    result = forecast(raw, turns=1)
    assert result['values']['enemy_hp_removed'] > 0
    assert set(result['values']) == {'hp_change', 'hp_risk', 'enemy_hp_removed'}
    blocked = powered(raw, 'NO_DRAW', 1)
    assert forecast(blocked, turns=1)['values']['enemy_hp_removed'] == 0


def test_extra_energy_is_bounded_by_available_cards_and_turn_timing():
    raw = powered(state(hand=('STRIKE_IRONCLAD',) * 5, draw=('STRIKE_IRONCLAD',) * 5), 'PYRE', 2)
    assert forecast(raw, turns=1)['values']['enemy_hp_removed'] == 0
    assert forecast(raw, turns=3)['values']['enemy_hp_removed'] > 0
    empty = powered(state(hand=(), draw=()), 'PYRE', 20)
    assert forecast(empty)['values']['enemy_hp_removed'] == 0


def test_temporary_strength_does_not_become_permanent_or_count_twice():
    permanent = powered(state(), 'STRENGTH', 3)
    temporary = powered(permanent, 'TEMPORARY_STRENGTH', 3)
    assert forecast(temporary, turns=1)['values']['enemy_hp_removed'] == forecast(permanent, turns=1)['values']['enemy_hp_removed']
    assert forecast(temporary)['values']['enemy_hp_removed'] < forecast(permanent)['values']['enemy_hp_removed']


def test_periodic_self_damage_cost_is_priced_and_lethal_start_stops_effects():
    raw = powered(state(hand=(), draw=()), 'CRIMSON_MANTLE', 8)
    assert forecast(raw, turns=2, incoming=0)['values']['hp_change'] < 0
    raw['combat']['player']['hp'] = 1
    result = forecast(powered(raw, 'INFERNO', 100), turns=3, incoming=0)
    assert result['turns'][1]['powered']['stopped'] == 'lethal_start_turn_cost'
    assert result['values']['enemy_hp_removed'] == 0


def test_regen_is_capped_at_max_hp_and_reuses_health_features():
    root = state(hand=(), draw=())
    assert forecast(powered(root, 'REGEN', 10), incoming=0)['values']['hp_change'] == 0
    root['combat']['player']['hp'] = 75
    value = forecast(powered(root, 'REGEN', 10), incoming=0)['values']['hp_change']
    assert 0 < value <= 0.5


def test_unknown_power_is_reported_without_inventing_effects():
    result = forecast(powered(state(), 'UNKNOWN_ABILITY', 1000))
    assert result['unsupported_powers'] == ['UNKNOWN_ABILITY']
    assert not any(result['values'].values())


def test_end_turn_healing_is_not_credited_after_attack_lethal():
    root = state(hand=('STRIKE_IRONCLAD',), draw=())
    root['combat']['player']['hp'] = 75
    root['combat']['enemies'][0]['hp'] = 1
    result = forecast(powered(root, 'REGEN', 10), incoming=0)
    assert result['values']['hp_change'] == 0
    assert result['turns'][0]['powered']['events'] == []


def test_training_updates_shared_parameters_and_explanation_totals():
    scorer, root = CombatScoring(), state()
    a = scorer.features(root, powered(root, 'STRENGTH', 3), [])
    b = scorer.features(root, root, [])
    trained = fit_pairs([{'a': a, 'b': b, 'target': 1}], steps=20)
    assert set(trained['weights']) == set(active_model()['weights'])
    explained = CombatScoring(model=trained).explain_features(a)
    assert explained['score'] == pytest.approx(explained['base_score'] + explained['ability_score'])
    assert explained['score'] == pytest.approx(sum(c['score'] for c in explained['contributions']))


def test_ab_cache_includes_ability_and_observation_sources(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    meta = {'snapshot_id': 'test', 'restore_sha256': 'digest', 'compatibility': {}}
    original = Path.read_bytes
    first = _cache_key(root, meta, active_model(), {})
    for source in ('combat_abilities.py', 'combat_opportunity.py', 'combat_observation.py'):
        monkeypatch.setattr(Path, 'read_bytes', lambda p, name=source: original(p) + (b'\n' if p.name == name else b''))
        assert _cache_key(root, meta, active_model(), {}) != first
    monkeypatch.setattr(Path, 'read_bytes', original)
