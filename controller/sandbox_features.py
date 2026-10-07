"""Versioned features using simulated hands and unordered pile composition."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from controller.combat_intent import intent_deals_damage, intent_total_damage
from controller.combat_observation import MODEL_INFORMATION_POLICY, project_model_state
from controller.combat_opportunity import DISCOUNT, MAX_HORIZON, opportunities
from controller.combat_abilities import ability_potential, rule_schema, ability_settings

FEATURE_VERSION = 'combat-preference-4'
OBSERVATION_MODE = MODEL_INFORMATION_POLICY
STAGES = ('early', 'mid', 'late')


@dataclass(frozen=True)
class FeatureSpec:
    key: str
    label: str
    definition: str
    initial_weight: float


FEATURES = (
    FeatureSpec('hp_change', '生命变化', '(leaf HP - root HP) / 10 + discounted marginal ability HP value', 42.5),
    FeatureSpec('hp_risk', '非线性失血风险', '-max(0, root HP - leaf HP)^2 / max(root HP, 1) / 10 + marginal ability risk value', 30.0),
    FeatureSpec('enemy_hp_removed', '敌方生命减少', 'identity-matched HP removed / 10 + discounted marginal ability damage value', 60.0),
    FeatureSpec('enemy_kills', '敌人数减少', 'identity-matched kill progress; pure summons are negative', 8.0),
    FeatureSpec('next_threat', '下一意图威胁', '-max(0, incoming - current block - shared-budget hand block) / 10', 30.0),
    FeatureSpec('hand_energy_opportunity', '手牌与能量机会', 'shared-budget base hand damage minus self-HP cost / 10; defense enters next_threat', 2.0),
    FeatureSpec('potion_cost', '药水消耗', '-number of potion uses in this trajectory', 8.0),
)


def schema() -> dict:
    return {'version': FEATURE_VERSION, 'observation_mode': OBSERVATION_MODE,
            'features': [asdict(item) for item in FEATURES],
            'ability_model': rule_schema(),
            'opportunity_model': {'discount': DISCOUNT, 'max_horizon': MAX_HORIZON,
                                  'simulated_draws_allowed': True, 'pile_order_allowed': False}}


def _player(state: dict) -> dict:
    return ((state.get('terminal_result') or {}).get('player') or
            (state.get('combat') or {}).get('player') or {})


def _enemies(state: dict) -> list[dict]:
    return [enemy for enemy in (state.get('combat') or {}).get('enemies', [])
            if float(enemy.get('hp') or 0) > 0]


def power(player: dict, name: str) -> float:
    return sum(float(item.get('amount') or 0) for item in player.get('powers', [])
               if str(item.get('id') or item.get('power_id') or '').split('.')[-1] in {name, name + '_POWER'})


def _enemy_identity(enemy: dict) -> str:
    return str(enemy.get('monster_id') or enemy.get('enemy_id') or enemy.get('id') or '__unknown__')


def _enemy_hp(enemy: dict) -> float:
    return max(0.0, float(enemy.get('hp') or enemy.get('current_hp') or 0.0))


def _enemy_max_hp(enemy: dict) -> float:
    return max(_enemy_hp(enemy), float(enemy.get('max_hp') or 0.0))


def _is_lock_or_phase_transition(before: dict, after: dict) -> bool:
    """Recognize a same-instance HP sentinel/phase transition numerically.

    Some encounters keep the same enemy identity but replace a nearly-dead
    normal HP pool with a very large phase HP pool.  That value is a protocol
    representation of a lock/special phase, not healing.  The relative check
    avoids naming individual monsters while leaving ordinary heals unchanged.
    """
    old_max = _enemy_max_hp(before)
    new_hp = _enemy_hp(after)
    new_max = _enemy_max_hp(after)
    if old_max <= 0 or new_hp <= old_max:
        return False
    if after.get('hittable') is False or after.get('is_hittable') is False:
        return True
    return new_max > old_max * 2 and new_hp >= new_max


def enemy_transition_progress(old_enemies: list[dict], new_enemies: list[dict]) -> tuple[float, float]:
    """Return HP removal and kill progress without treating phase replacements as healing.

    Enemies with the same model id are matched greedily. When old identities disappear and
    new identities appear, the vanished enemies' max HP forms a replacement budget. New HP
    inside that budget is the next phase of the same encounter; HP beyond it remains a real
    summon penalty. Pure summons, where every old enemy remains, keep the previous penalty.
    """
    old_by_id: dict[str, list[dict]] = {}
    new_by_id: dict[str, list[dict]] = {}
    for enemy in old_enemies:
        old_by_id.setdefault(_enemy_identity(enemy), []).append(enemy)
    for enemy in new_enemies:
        new_by_id.setdefault(_enemy_identity(enemy), []).append(enemy)

    matched: list[tuple[dict, dict]] = []
    vanished: list[dict] = []
    appeared: list[dict] = []
    for identity in old_by_id.keys() | new_by_id.keys():
        old_rows = list(old_by_id.get(identity, []))
        new_rows = list(new_by_id.get(identity, []))
        while old_rows and new_rows:
            old_index, new_index = min(
                ((oi, ni) for oi in range(len(old_rows)) for ni in range(len(new_rows))),
                key=lambda pair: (
                    abs(_enemy_max_hp(old_rows[pair[0]]) - _enemy_max_hp(new_rows[pair[1]])),
                    abs(_enemy_hp(old_rows[pair[0]]) - _enemy_hp(new_rows[pair[1]])),
                ),
            )
            matched.append((old_rows.pop(old_index), new_rows.pop(new_index)))
        vanished.extend(old_rows)
        appeared.extend(new_rows)

    matched_hp_removed = sum(
        _enemy_hp(before) if _is_lock_or_phase_transition(before, after)
        else max(0.0, _enemy_hp(before) - _enemy_hp(after))
        for before, after in matched
    )
    vanished_hp = sum(_enemy_hp(enemy) for enemy in vanished)
    replacement_budget = sum(_enemy_max_hp(enemy) for enemy in vanished)
    appeared_hp = sum(_enemy_hp(enemy) for enemy in appeared)
    # A same-model population increase is commonly a split.  Its newly
    # exposed HP is a new population state, not healing of the old target.
    # Keep the existing penalty for explicit new-model summons.
    same_identity_split = any(
        old_by_id.get(identity)
        and len(new_by_id.get(identity, [])) > len(old_by_id.get(identity, []))
        for identity in old_by_id.keys()
    )
    excess_summoned_hp = 0.0 if same_identity_split else max(0.0, appeared_hp - replacement_budget)
    hp_removed = matched_hp_removed + vanished_hp - excess_summoned_hp

    if vanished and appeared and appeared_hp <= replacement_budget:
        kill_progress = float(max(0, len(vanished) - len(appeared)))
    else:
        kill_progress = float(len(vanished) - len(appeared))
    return hp_removed, kill_progress


def extract_features(root: dict, leaf: dict, trace: list[dict], stage: str,
                     cards: dict[str, dict], visible_costs: list | None = None,
                     ability_config: dict | None = None) -> dict[str, Any]:
    config = ability_settings(ability_config)
    if stage not in STAGES:
        raise ValueError('Unknown stage')
    root, leaf = project_model_state(root), project_model_state(leaf)
    before, after = _player(root), _player(leaf)
    initial_hp, final_hp = float(before.get('hp') or 0), float(after.get('hp') or 0)
    loss = max(0.0, initial_hp - final_hp)
    old_enemies, new_enemies = _enemies(root), _enemies(leaf)
    initial_enemy_hp = sum(_enemy_hp(enemy) for enemy in old_enemies)
    enemy_hp_removed, enemy_kills = enemy_transition_progress(old_enemies, new_enemies)
    incoming = sum(intent_total_damage(enemy.get('intent'))
                   for enemy in new_enemies if intent_deals_damage(enemy.get('intent')))
    block = float(after.get('block') or 0)
    horizon = min(config['max_horizon'], max(1.0, initial_enemy_hp / config['damage_per_turn']))
    root_incoming = sum(intent_total_damage(e.get('intent')) for e in old_enemies
                        if intent_deals_damage(e.get('intent')))
    root_op = opportunities(root, cards, root_incoming)
    leaf_op = opportunities(leaf, cards, incoming, visible_costs)
    root_potential = ability_potential(root, root_op, horizon, root_incoming, initial_hp, config)
    leaf_potential = ability_potential(leaf, leaf_op, horizon, incoming, initial_hp, config)
    hand = leaf_op['immediate']
    effective_damage = min(sum(_enemy_hp(e) for e in new_enemies),
                           max(0.0, hand['damage']))
    useful_block = min(max(0.0, incoming - block), hand['block'])
    training_eligible = not root_op['missing_piles'] and (bool(leaf.get('terminal_decision'))
                                                         or not leaf_op['missing_piles'])
    values = {
        'hp_change': (final_hp - initial_hp) / 10,
        'hp_risk': -loss * loss / max(1.0, initial_hp) / 10,
        'enemy_hp_removed': enemy_hp_removed / 10,
        'enemy_kills': enemy_kills,
        'next_threat': -max(0.0, incoming - block - useful_block) / 10,
        'hand_energy_opportunity': (effective_damage - 2.0 * hand['self_loss']) / 10,
        'potion_cost': -float(sum(row['action']['action_type'] == 'use_potion' for row in trace)),
    }
    base_values = dict(values)
    ability_values = {key: leaf_potential['values'].get(key, 0.0)
                     - root_potential['values'].get(key, 0.0) for key in values}
    if leaf.get('terminal_decision'):
        ability_values = dict.fromkeys(values, 0.0)
    values = {key: value + ability_values[key] for key, value in values.items()}
    last_player_state = next((row['before'] for row in reversed(trace)
                              if row['action']['action_type'] == 'end_turn'), leaf)
    return {'version': FEATURE_VERSION, 'observation_mode': OBSERVATION_MODE,
            'stage': stage, 'values': values, 'ability_config': config,
            'base_values': base_values, 'ability_values': ability_values,
            'training_eligible': training_eligible,
            'opportunities': {'root': root_op, 'leaf': leaf_op,
                              'root_potential': root_potential, 'leaf_potential': leaf_potential,
                              'horizon': horizon, 'discount': config['discount']},
            'vector': [values[item.key] for item in FEATURES],
            'terminal': leaf.get('terminal_decision'),
            'process': {'actions': len(trace),
                        'energy_before_end_turn': (_player(last_player_state)).get('energy'),
                        'hand_before_end_turn': len((last_player_state.get('combat') or {}).get('hand') or [])}}
