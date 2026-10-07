import copy
from pathlib import Path

import pytest

from controller.combat_abilities import ability_settings
from controller.combat_comparison import ModelLibrary, _cache_key
from controller.combat_scoring import CombatScoring, active_model
from controller.preference_model import fit_pairs
from scripts.train_human_leaf_preferences import _leave_one_session_out
from tests.test_combat_opportunity import state, powered


def scorer(**settings):
    model = active_model()
    model['ability_config'] = ability_settings(settings)
    return CombatScoring(model=model)


def test_legacy_v4_defaults_have_identical_values():
    old = active_model()
    old.pop('ability_config')
    root = state()
    leaf = powered(root, 'STRENGTH', 2)
    assert CombatScoring(model=old).features(root, leaf, []) == scorer().features(root, leaf, [])


def test_settings_change_estimate_not_base_values():
    root = state()
    leaf = powered(root, 'DEMON_FORM', 2)
    base = scorer().features(root, leaf, [])
    doubled = scorer(realization=0.7).features(root, leaf, [])
    assert doubled['base_values'] == base['base_values']
    for key, value in base['ability_values'].items():
        assert doubled['ability_values'][key] == pytest.approx(value * 2)
    assert not any(scorer(realization=0).features(root, leaf, [])['ability_values'].values())
    assert scorer(discount=0).features(root, leaf, [])['ability_values']['enemy_hp_removed'] == 0
    assert scorer(max_horizon=1).features(root, leaf, [])['opportunities']['horizon'] == 1
    root['combat']['enemies'][0]['hp'] = 60
    assert scorer(damage_per_turn=40).features(root, leaf, [])['opportunities']['horizon'] == 1.5


@pytest.mark.parametrize('settings', [
    {'realization': -1}, {'realization': 2}, {'discount': float('nan')},
    {'max_horizon': 2.5}, {'max_horizon': 7}, {'damage_per_turn': 0},
    {'discount': None}, {'discount': True}, {'unknown': 1}, [],
])
def test_invalid_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        ability_settings(settings)


def test_model_roundtrip_identity_and_ab_cache_include_settings(tmp_path):
    library = ModelLibrary(tmp_path)
    weights = active_model()['weights']
    a = library.save('default', weights)
    b = library.save('patient', weights, {'discount': 0.95, 'max_horizon': 4})
    assert a['id'] != b['id']
    restored = library.get(b['id'])
    assert restored['ability_config']['discount'] == 0.95
    assert CombatScoring(model=a['model']).identity['weights_sha256'] != CombatScoring(model=restored).identity['weights_sha256']
    root = Path(__file__).resolve().parents[1]
    metadata = {'snapshot_id': 'test', 'restore_sha256': 'digest', 'compatibility': {}}
    assert _cache_key(root, metadata, a['model'], {}) != _cache_key(root, metadata, restored, {})


def test_training_keeps_manual_settings_and_rejects_mixed_features():
    engine = scorer(realization=0.6, discount=0.9, max_horizon=4, damage_per_turn=25)
    root = state()
    a = engine.features(root, powered(root, 'STRENGTH', 3), [])
    b = engine.features(root, root, [])
    pair = {'a': a, 'b': b, 'target': 1}
    model = fit_pairs([pair], steps=20)
    assert model['ability_config'] == engine.payload['ability_config']
    assert set(model['weights']) == set(active_model()['weights'])
    CombatScoring(model=model).explain_features(a)
    validation = _leave_one_session_out({'a': [pair], 'b': [pair]}, 0.1)
    assert validation['held_out_pairs'] == 2
    with pytest.raises(ValueError, match='settings mismatch'):
        scorer().explain_features(a)
    mixed = copy.deepcopy(pair)
    mixed['b']['ability_config']['discount'] = 0.5
    with pytest.raises(ValueError, match='Mixed ability settings'):
        fit_pairs([mixed], steps=1)


def test_original_feature_records_without_settings_keep_default_meaning():
    root = state()
    row = scorer().features(root, root, [])
    expected = scorer().explain_features(row)['score']
    row.pop('ability_config')
    assert scorer().explain_features(row)['score'] == expected
    trained = fit_pairs([{'a': row, 'b': row, 'target': 0.5}], steps=1)
    assert trained['ability_config'] == ability_settings()
