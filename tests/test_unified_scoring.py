import copy
from pathlib import Path

import pytest

from controller.combat_scoring import CombatScoring, active_model, history_trace
from controller.combat_step import CombatStepConfig
from controller.sandbox import SandboxManager
from controller.search.combat_search import CombatSearcher, CombatSpec, RecordedAction

ROOT = Path(__file__).resolve().parents[1]


def combat(hp=60, strength=2):
    return {'success': True, 'combat': {'turn_number': 1, 'is_player_turn': True,
            'player': {'hp': hp, 'max_hp': 80, 'energy': 3, 'block': 0,
                       'powers': [{'id': 'STRENGTH_POWER', 'amount': strength}]},
            'enemies': [{'monster_id': 'TEST', 'hp': 30, 'max_hp': 30, 'block': 0, 'intent': {}}],
            'hand': [{'card_id': 'STRIKE_IRONCLAD', 'current_cost': None, 'display_cost': 0}],
            'available_actions': [{'action_type': 'play_card', 'card_index': 0}]}}


def test_default_and_shared_score_preserve_full_root_and_potion_path():
    assert CombatStepConfig(None, CombatSpec('Ironclad', 'TEST', 'test')).score_mode == 'preference'
    root, leaf = combat(), combat(hp=55, strength=3)
    scorer = CombatScoring('late')
    searcher = CombatSearcher(None, CombatSpec('Ironclad', 'TEST', 'test'), score_mode='preference', scorer_stage='late')
    searcher._preference_root = root
    searcher._root_summary = searcher._combat_summary(root)
    history = [RecordedAction('use_potion', {'potion_index': 0})]
    try:
        actual = searcher._score_with_history(leaf, history)
        expected = scorer.score(root, leaf, history_trace(history))
        assert actual == expected
        assert searcher._score_with_history(leaf, []) - actual == pytest.approx(
            scorer.payload['weights']['potion_cost']
        )
        assert scorer.features(root, root, [])['ability_values']['enemy_hp_removed'] == 0
        assert scorer.features(root, root, [])['values']['hand_energy_opportunity'] == 0.6
        branch = searcher._new_branch_searcher(100)
        assert branch.preference_scorer.identity == scorer.identity
        branch.close()
    finally:
        searcher.close()


def test_model_validation_and_invalid_leaves():
    model = copy.deepcopy(active_model())
    model['feature_version'] = 'obsolete'
    with pytest.raises(ValueError, match='version'):
        CombatScoring(model=model)
    scorer = CombatScoring()
    assert scorer.score(combat(), {'success': False}, []) == float('-inf')
    assert scorer.score(combat(), {'terminal_decision': 'card_select'}, []) == float('-inf')


def test_enemy_phase_replacement_is_progress_not_enemy_healing():
    scorer = CombatScoring('mid')
    root = combat()
    root['combat']['enemies'] = [
        {'monster_id': 'PHASE_ONE', 'hp': 1, 'max_hp': 48, 'block': 0, 'intent': {}},
    ]
    replacement = combat()
    replacement['combat']['enemies'] = [
        {'monster_id': 'PHASE_TWO_LEFT', 'hp': 10, 'max_hp': 10, 'block': 0, 'intent': {}},
        {'monster_id': 'PHASE_TWO_RIGHT', 'hp': 17, 'max_hp': 17, 'block': 0, 'intent': {}},
    ]

    values = scorer.features(root, replacement, [])['base_values']

    assert values['enemy_hp_removed'] == pytest.approx(0.1)
    assert values['enemy_kills'] == 0.0


def test_enemy_transition_scoring_still_penalizes_real_summons_and_rewards_kills():
    scorer = CombatScoring('mid')
    root = combat()
    root['combat']['enemies'] = [
        {'monster_id': 'CORE', 'hp': 30, 'max_hp': 30, 'block': 0, 'intent': {}},
    ]
    summoned = combat()
    summoned['combat']['enemies'] = [
        {'monster_id': 'CORE', 'hp': 30, 'max_hp': 30, 'block': 0, 'intent': {}},
        {'monster_id': 'ADD', 'hp': 10, 'max_hp': 10, 'block': 0, 'intent': {}},
    ]
    summoned_values = scorer.features(root, summoned, [])['values']
    assert summoned_values['enemy_hp_removed'] == pytest.approx(-1.0)
    assert summoned_values['enemy_kills'] == -1.0

    two_enemies = combat()
    two_enemies['combat']['enemies'] = [
        {'monster_id': 'ADD', 'hp': 5, 'max_hp': 10, 'block': 0, 'intent': {}},
        {'monster_id': 'CORE', 'hp': 20, 'max_hp': 30, 'block': 0, 'intent': {}},
    ]
    after_kill = combat()
    after_kill['combat']['enemies'] = [
        {'monster_id': 'CORE', 'hp': 20, 'max_hp': 30, 'block': 0, 'intent': {}},
    ]
    kill_values = scorer.features(two_enemies, after_kill, [])['values']
    assert kill_values['enemy_hp_removed'] == pytest.approx(0.5)
    assert kill_values['enemy_kills'] == 1.0


def test_lock_phase_sentinel_counts_damage_once_and_never_as_healing():
    scorer = CombatScoring('mid')
    root = combat()
    root['combat']['enemies'] = [
        {'monster_id': 'LOCKING_BOSS', 'hp': 3, 'max_hp': 240, 'block': 0, 'intent': {}},
    ]
    locked = combat()
    locked['combat']['enemies'] = [
        {'monster_id': 'LOCKING_BOSS', 'hp': 999999999, 'max_hp': 999999999,
         'block': 0, 'hittable': True, 'intent': {'move_id': 'ABOUT_TO_BLOW_MOVE'}},
    ]
    values = scorer.features(root, locked, [])['base_values']
    assert values['enemy_hp_removed'] == pytest.approx(0.3)

    after_lock_attack = combat()
    after_lock_attack['combat']['enemies'] = [
        {'monster_id': 'LOCKING_BOSS', 'hp': 999999999, 'max_hp': 999999999,
         'block': 0, 'hittable': True, 'intent': {'move_id': 'ABOUT_TO_BLOW_MOVE'}},
    ]
    values_after = scorer.features(locked, after_lock_attack, [])['base_values']
    assert values_after['enemy_hp_removed'] == pytest.approx(0.0)


def test_same_model_population_split_does_not_look_like_enemy_healing():
    scorer = CombatScoring('mid')
    root = combat()
    root['combat']['enemies'] = [
        {'monster_id': 'SPLITTER', 'hp': 2, 'max_hp': 30, 'block': 0, 'intent': {}},
    ]
    split = combat()
    split['combat']['enemies'] = [
        {'monster_id': 'SPLITTER', 'hp': 12, 'max_hp': 15, 'block': 0, 'intent': {}},
        {'monster_id': 'SPLITTER', 'hp': 12, 'max_hp': 15, 'block': 0, 'intent': {}},
    ]
    values = scorer.features(root, split, [])['base_values']
    assert values['enemy_hp_removed'] >= 0.0


def test_dedup_does_not_merge_different_dynamic_costs():
    from controller.search.actions import SearchAction
    searcher = CombatSearcher(None, CombatSpec('Ironclad', 'TEST', 'fixed'))
    state = combat()
    state['combat']['hand'] = [
        {'card_id': 'STRIKE_IRONCLAD', 'upgrade': 0, 'display_cost': 0},
        {'card_id': 'STRIKE_IRONCLAD', 'upgrade': 0, 'display_cost': 1}]
    actions = [SearchAction('play_card', card_index=i, target_index=0) for i in range(2)]
    assert len(searcher._dedupe_symmetric_actions(actions, state)) == 2
    searcher.close()


def test_hint_uses_current_position_does_not_play_and_marks_assistance(tmp_path):
    manager = SandboxManager(ROOT, tmp_path, ROOT / 'data/deck_profiles/ironclad_self_damage.json')
    try:
        manager._cmd_generate({'seed': 'hint-unification', 'stage': 'mid', 'energy': 4, 'enemy_hp': 150,
                               'hand': ['RUPTURE', 'HEMOKINESIS', 'SHRUG_IT_OFF']})
        manager._publish()
        first = next(a for a in manager.status()['actions'] if (a.get('metadata') or {}).get('card_id') == 'RUPTURE')
        manager._cmd_action({'action_id': first['id']})
        manager._publish()
        before, history = copy.deepcopy(manager.runtime.state), copy.deepcopy(manager.history)
        manager._cmd_agent({'origin': 'current'})
        manager._publish()
        hint = manager.status()['agent_hint']
        assert manager.runtime.state == before and manager.history == history
        assert hint['prefix_length'] == 1 and hint['origin'] == 'current'
        assert hint['verification'] == 'PASS', hint
        assert hint['score'] == pytest.approx(hint['replay_score'])
        assert hint['explanation']['scorer'] == manager.status()['scorer']
        assert manager.status()['hint_current']
        end = next(a for a in manager.status()['actions'] if a['action_type'] == 'end_turn')
        manager._cmd_action({'action_id': end['id']})
        manager._publish()
        assert not manager.status()['hint_current']
        manager._cmd_demonstrate({})
        demo = manager.store.all('demonstrations')[0]
        assert demo['assisted']
        assert not demo['decisions'][0]['hint_seen_before']
        assert demo['decisions'][1]['hint_seen_before']
        manager._cmd_load({'id': manager.scene['id']})
        assert manager.hint_events
    finally:
        manager.close()
