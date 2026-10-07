from controller.combat_intent import intent_deals_damage, intent_total_damage
from controller.engine_parity import _headless_intents
from controller.sandbox_features import extract_features
from controller.search.state_cache import canonicalize_search_state_for_plan_reuse
from controller.combat_step import _is_modeled_potion_action
from controller.search.actions import SearchAction


def test_explicit_total_damage_is_not_multiplied_by_hits_again():
    assert intent_total_damage({
        'display_damage': 12,
        'hits': 3,
        'total_damage': 12,
    }) == 12


def test_legacy_per_hit_damage_keeps_backward_compatible_fallback():
    assert intent_total_damage({'display_damage': 4, 'hits': 3}) == 12


def test_preference_next_threat_uses_explicit_total_damage():
    root = {
        'combat': {
            'player': {'hp': 25, 'max_hp': 80, 'block': 0, 'energy': 2, 'powers': []},
            'enemies': [{'hp': 18, 'intent': {'intent_types': ['Attack'], 'total_damage': 33, 'hits': 1}}],
        }
    }
    leaf = {
        'combat': {
            'player': {'hp': 16, 'max_hp': 80, 'block': 0, 'energy': 3, 'powers': []},
            'enemies': [{'hp': 12, 'intent': {
                'intent_types': ['Attack'], 'display_damage': 12, 'hits': 3, 'total_damage': 12,
            }}],
            'available_actions': [],
            'hand': [],
        }
    }

    features = extract_features(root, leaf, [], 'late', {})

    assert features['values']['next_threat'] == -1.2


def test_parity_uses_total_damage_for_multi_hit_intent():
    intents = _headless_intents({
        'intent_types': ['Attack'],
        'display_damage': 12,
        'hits': 3,
        'total_damage': 12,
    })

    assert intents == [{'type': 'Attack', 'damage': 12, 'hits': 3}]


def test_death_blow_is_preserved_as_a_damage_intent():
    intent = {
        'intent_types': ['DeathBlow'],
        'display_damage': 8,
        'total_damage': 8,
    }

    assert intent_deals_damage(intent) is True
    assert _headless_intents(intent) == [
        {'type': 'DeathBlow', 'damage': 8, 'hits': 1},
    ]


def test_preference_next_threat_counts_death_blow_damage():
    root = {
        'combat': {
            'player': {'hp': 25, 'max_hp': 80, 'block': 0, 'energy': 2, 'powers': []},
            'enemies': [],
        }
    }
    leaf = {
        'combat': {
            'player': {'hp': 25, 'max_hp': 80, 'block': 0, 'energy': 3, 'powers': []},
            'enemies': [{'hp': 7, 'intent': {
                'intent_types': ['DeathBlow'], 'total_damage': 8,
            }}],
            'available_actions': [],
            'hand': [],
        }
    }

    features = extract_features(root, leaf, [], 'early', {})

    assert features['values']['next_threat'] == -0.8


def test_plan_semantics_normalize_legacy_and_explicit_total_damage():
    legacy = {'combat': {'enemies': [{'intent': {
        'intent_types': ['Attack'], 'display_damage': 4, 'hits': 3,
    }}]}}
    explicit = {'combat': {'enemies': [{'intent': {
        'intent_types': ['Attack'], 'display_damage': 12, 'hits': 3, 'total_damage': 12,
    }}]}}

    assert canonicalize_search_state_for_plan_reuse(legacy) == canonicalize_search_state_for_plan_reuse(explicit)


def test_automatic_fairy_potion_is_not_a_search_use_action():
    assert not _is_modeled_potion_action(SearchAction(
        'use_potion', metadata={'potion_id': 'FAIRY_IN_A_BOTTLE', 'potion_index': 0}
    ))
    assert _is_modeled_potion_action(SearchAction(
        'use_potion', metadata={'potion_id': 'STRENGTH_POTION', 'potion_index': 0}
    ))
