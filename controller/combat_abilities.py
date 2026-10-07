"""Trigger/effect rules with shared pricing, not another combat simulator.

Rules predict bounded marginal outcomes relative to the same opportunity schedule
without these powers. Only the base feature weights price those outcomes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math

from controller.combat_opportunity import DISCOUNT, DIMENSIONS, resource_opportunity

REALIZATION = 0.35
MAX_TRIGGER_WAVES = 2

ABILITY_PARAMETERS = (
    {'key': 'realization', 'label': '整体兑现系数', 'default': REALIZATION,
     'min': 0, 'max': 1, 'step': 0.01, 'advanced': False,
     'description': '预计能力收益的整体缩减比例；越高越重视能力投资。'},
    {'key': 'discount', 'label': '逐回合折扣', 'default': DISCOUNT,
     'min': 0, 'max': 1, 'step': 0.01, 'advanced': False,
     'description': '每经过一个未来回合乘以此系数；越低越偏向短期收益。'},
    {'key': 'max_horizon', 'label': '最多估计回合数', 'default': 3,
     'min': 1, 'max': 6, 'step': 1, 'advanced': True,
     'description': '未来收益估计上限，整数 1–6；并非搜索深度。'},
    {'key': 'damage_per_turn', 'label': '战斗长度估计基准', 'default': 18,
     'min': 1, 'max': 200, 'step': 1, 'advanced': True,
     'description': '用根节点敌方总生命除以此值估计回合数；越高预计战斗越短。'},
)


def ability_settings(value: dict | None = None) -> dict:
    """Missing v4 settings mean the original fixed defaults, never active settings."""
    defaults = {spec['key']: spec['default'] for spec in ABILITY_PARAMETERS}
    if value is None:
        return defaults
    if not isinstance(value, dict) or set(value) - defaults.keys():
        raise ValueError('Unknown ability settings')
    result = {**defaults, **value}
    for spec in ABILITY_PARAMETERS:
        number = result[spec['key']]
        if (type(number) not in (int, float) or not math.isfinite(number)
                or not spec['min'] <= number <= spec['max']
                or (spec['key'] == 'max_horizon' and number != int(number))):
            raise ValueError(f"Invalid ability setting: {spec['key']}")
        result[spec['key']] = int(number) if spec['key'] == 'max_horizon' else float(number)
    return result


@dataclass(frozen=True)
class AbilityRule:
    power: str
    trigger: str
    effect: str
    scale: float = 1.0
    per_stack: bool = True
    decay: float = 0.0
    first_turn_only: bool = False


RULES = (
    AbilityRule('STRENGTH', 'attack_hit', 'attack_damage'),
    AbilityRule('DEXTERITY', 'block_card', 'card_block'),
    AbilityRule('DEMON_FORM', 'turn_start', 'strength'),
    AbilityRule('RITUAL', 'turn_end', 'strength'),
    AbilityRule('RUPTURE', 'self_damage', 'strength'),
    AbilityRule('PLATING', 'turn_end', 'block', decay=1),
    AbilityRule('REGEN', 'turn_end', 'heal', decay=1),
    AbilityRule('FEEL_NO_PAIN', 'exhaust', 'block'),
    AbilityRule('DARK_EMBRACE', 'exhaust', 'draw'),
    AbilityRule('JUGGERNAUT', 'gain_block', 'damage'),
    AbilityRule('PYRE', 'turn_start', 'energy'),
    AbilityRule('CRIMSON_MANTLE', 'turn_start', 'self_loss', per_stack=False),
    AbilityRule('CRIMSON_MANTLE', 'turn_start', 'block'),
    AbilityRule('INFERNO', 'turn_start', 'self_loss', per_stack=False),
    AbilityRule('INFERNO', 'self_damage', 'area_damage'),
    AbilityRule('BARRICADE', 'turn_end', 'retain_block', per_stack=False),
    # Temporary amounts already exist in the current Strength/Dexterity totals.
    AbilityRule('TEMPORARY_STRENGTH', 'turn_end', 'strength', scale=-1, first_turn_only=True),
    AbilityRule('TEMPORARY_DEXTERITY', 'turn_end', 'dexterity', scale=-1, first_turn_only=True),
    AbilityRule('DEXTERITY_DOWN', 'turn_end', 'dexterity', scale=-1, first_turn_only=True),
    AbilityRule('TEMPORARY_STRENGTH_DOWN', 'turn_end', 'strength', first_turn_only=True),
    AbilityRule('TEMPORARY_DEXTERITY_DOWN', 'turn_end', 'dexterity', first_turn_only=True),
)


def rule_schema() -> dict:
    return {'realization': REALIZATION, 'discount': DISCOUNT,
            'max_trigger_waves': MAX_TRIGGER_WAVES,
            'parameters': [dict(spec) for spec in ABILITY_PARAMETERS],
            'rules': [asdict(rule) for rule in RULES]}


def _levels(player: dict) -> dict:
    result = {}
    for row in player.get('powers') or []:
        name = str(row.get('id') or row.get('power_id') or '').split('.')[-1].removesuffix('_POWER')
        result[name] = result.get(name, 0.0) + float(row.get('amount') or 0)
    return result


def _risk(hp: float, root_hp: float) -> float:
    return -max(0.0, root_hp - hp) ** 2 / max(root_hp, 1.0) / 10.0


def _forecast(state: dict, op: dict, horizon: float, incoming: float,
              root_hp: float, enabled: bool) -> list[dict]:
    combat = state.get('combat') or {}
    player = combat.get('player') or {}
    levels = _levels(player) if enabled else {}
    remaining = sum(max(0.0, float(e.get('hp') or 0)) for e in combat.get('enemies', []))
    enemy_count = sum(float(e.get('hp') or 0) > 0 for e in combat.get('enemies', []))
    hp = max(0.0, float(player.get('hp') or 0))
    stock = max(0.0, float(player.get('block') or 0))
    rows = []
    for turn in range(math.ceil(horizon)):
        if remaining <= 0 or hp <= 0:
            break
        row = dict(op['immediate' if turn == 0 else 'future'])
        effects = dict.fromkeys(('strength', 'dexterity', 'block', 'damage', 'area_damage', 'heal',
                                 'self_loss', 'draw', 'energy', 'retain_block'), 0.0)
        events_log = []

        def trigger(events):
            generated = {'gain_block': 0.0, 'self_damage': 0.0}
            for rule in RULES:
                if rule.trigger not in events or rule.trigger in {'attack_hit', 'block_card'}:
                    continue
                amount = levels.get(rule.power, 0.0)
                if amount <= 0 or (rule.first_turn_only and turn != 0):
                    continue
                amount = max(0.0, amount - rule.decay * turn)
                count = max(0.0, events[rule.trigger])
                if amount == 0 or count == 0:
                    continue
                value = count * (amount if rule.per_stack else 1.0) * rule.scale
                effects[rule.effect] += value
                events_log.append({'power': rule.power, 'trigger': rule.trigger,
                                   'effect': rule.effect, 'events': count, 'amount': value})
                if rule.effect == 'block' and value > 0:
                    generated['gain_block'] += count
                if rule.effect == 'self_loss' and value > 0:
                    generated['self_damage'] += count
            return generated

        # The first forecast turn starts at an already observed player boundary.
        start = trigger({'turn_start': 1.0 if turn > 0 else 0.0})
        if effects['self_loss'] >= hp:
            rows.append({'turn': turn, 'hp_change': -hp / 10,
                         'hp_risk': _risk(0, root_hp) - _risk(hp, root_hp),
                         'enemy_hp_removed': 0.0, 'events': events_log,
                         'stopped': 'lethal_start_turn_cost'})
            break
        levels['STRENGTH'] = levels.get('STRENGTH', 0.0) + effects['strength']
        levels['DEXTERITY'] = levels.get('DEXTERITY', 0.0) + effects['dexterity']
        effects['strength'] = effects['dexterity'] = 0.0
        safe_fraction = min(1.0, max(0, hp - effects['self_loss'] - 1) / max(row['self_loss'], 1e-9))
        row = {key: value * safe_fraction for key, value in row.items()}

        # Resolve direct card resources once, then a maximum of two trigger waves.
        draws = row['draw']
        if turn == 0 and 'NO_DRAW' in _levels(player):
            draws = 0.0
        expanded = resource_opportunity(op, turn=turn, draw=draws, energy=row['energy'] + effects['energy'])
        def affordable(extra):
            room = max(0.0, hp - 1 - effects['self_loss'] - row['self_loss'])
            scale = min(1.0, room / max(extra['self_loss'], 1e-9))
            return {key: value * scale for key, value in extra.items()}
        expanded = affordable(expanded)
        for key in DIMENSIONS:
            row[key] += expanded[key]
        paid_draw = draws
        # Avoid treating a lethal estimated HP cost as a reliable trigger farm.
        trigger_fraction = min(1.0, max(0, hp - 1) / max(row['self_loss'] + effects['self_loss'], 1e-9))
        pending = {'exhaust': row['exhausts'], 'self_damage': (row['self_events'] + start['self_damage']) * trigger_fraction,
                   'gain_block': row['block_events'] + start['gain_block']}
        truncated_draw = truncated_energy = 0.0
        for wave in range(MAX_TRIGGER_WAVES):
            previous_draw, previous_energy = effects['draw'], effects['energy']
            generated = trigger(pending)
            draw_gain = effects['draw']
            if turn == 0 and 'NO_DRAW' in _levels(player):
                draw_gain = 0.0
            # Resource conversion only in the first wave; no recursive draw loops.
            if wave == 0:
                total = resource_opportunity(op, turn=turn, draw=paid_draw + draw_gain,
                                             energy=op['immediate' if turn == 0 else 'future']['energy'] + effects['energy'])
                extra = {key: max(0.0, total[key] - expanded[key]) for key in DIMENSIONS}
                extra = affordable(extra)
                for key in DIMENSIONS:
                    row[key] += extra[key]
                generated['exhaust'] = extra['exhausts']
                generated['self_damage'] += extra['self_events'] * trigger_fraction
                generated['gain_block'] += extra['block_events']
            else:
                truncated_draw = effects['draw'] - previous_draw
                truncated_energy = effects['energy'] - previous_energy
            pending = generated

        # Direct modifiers use the final shared opportunity counts exactly once.
        attack = row['damage']
        card_block = row['block']
        for rule in RULES:
            if rule.trigger == 'attack_hit':
                attack += levels.get(rule.power, 0.0) * row['hits'] * rule.scale
            elif rule.trigger == 'block_card':
                card_block += levels.get(rule.power, 0.0) * row['block_cards'] * rule.scale
        attack += 0.5 * effects['strength'] * row['hits']
        card_block += 0.5 * effects['dexterity'] * row['block_cards']
        # Current Weak/Frail are observed conditions, not removable bonuses in
        # the counterfactual; they scale both paths and expire with their duration.
        conditions = _levels(player)
        attack *= 0.75 if conditions.get('WEAK', 0) > turn else 1.0
        card_block *= 0.75 if conditions.get('FRAIL', 0) > turn else 1.0
        # End-turn growth cannot strengthen attacks that already occurred.
        before_end_damage = (max(0.0, attack) + max(0.0, effects['damage'])
                             + max(0.0, effects['area_damage']) * enemy_count)
        end = trigger({'turn_end': float(before_end_damage < remaining)})
        # End-turn block may trigger damage once; no recursive end-turn chains.
        trigger({'gain_block': end['gain_block']})
        dealt = min(remaining, max(0.0, attack) + max(0.0, effects['damage'])
                    + max(0.0, effects['area_damage']) * enemy_count)
        remaining -= dealt
        defense = stock + max(0.0, card_block) + max(0.0, effects['block'])
        threat = incoming if remaining > 0 else 0.0
        self_loss = row['self_loss'] + effects['self_loss']
        after_cost = max(0.0, hp - self_loss)
        after_heal = (min(max(hp, float(player.get('max_hp') or hp)), after_cost + effects['heal'])
                      if after_cost > 0 else 0.0)
        next_hp = max(0.0, after_heal - max(0.0, threat - defense))
        rows.append({'turn': turn, 'hp_change': (next_hp - hp) / 10,
                     'hp_risk': _risk(next_hp, root_hp) - _risk(hp, root_hp),
                     'enemy_hp_removed': dealt / 10,
                     'events': events_log, 'opportunities': row,
                     'truncated_resource_effects': {'draw': truncated_draw, 'energy': truncated_energy}})
        hp = next_hp
        stock = max(0.0, defense - threat) if effects['retain_block'] else 0.0
        levels['STRENGTH'] = levels.get('STRENGTH', 0.0) + effects['strength']
        levels['DEXTERITY'] = levels.get('DEXTERITY', 0.0) + effects['dexterity']
    return rows


def ability_potential(state: dict, op: dict, horizon: float, incoming: float, root_hp: float,
                      settings: dict | None = None) -> dict:
    settings = ability_settings(settings)
    keys = ('hp_change', 'hp_risk', 'enemy_hp_removed')
    result = dict.fromkeys(keys, 0.0)
    levels = _levels((state.get('combat') or {}).get('player') or {})
    supported = {rule.power for rule in RULES} | {'WEAK', 'FRAIL', 'NO_DRAW'}
    unknown = sorted(name for name in levels if name not in supported)
    if (state.get('terminal_decision') or state.get('success') is False
            or not any(levels.get(rule.power, 0) != 0 for rule in RULES)):
        return {'values': result, 'turns': [], 'unsupported_powers': unknown}
    baseline = _forecast(state, op, horizon, incoming, root_hp, False)
    powered = _forecast(state, op, horizon, incoming, root_hp, True)
    turns = []
    for turn in range(max(len(baseline), len(powered))):
        before = baseline[turn] if turn < len(baseline) else {}
        after = powered[turn] if turn < len(powered) else {}
        factor = settings['realization'] * settings['discount'] ** turn * min(1.0, horizon - turn)
        delta = {key: factor * (after.get(key, 0.0) - before.get(key, 0.0)) for key in keys}
        for key in keys:
            result[key] += delta[key]
        turns.append({'turn': turn, 'adjustments': delta, 'baseline': before, 'powered': after})
    return {'values': result, 'turns': turns, 'unsupported_powers': unknown}
