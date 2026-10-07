"""Small mechanistic opportunity estimates, never an engine or action planner.

Known hand: a shared energy budget. Later turns: unordered cycling-card averages.
Unsupported mechanics remain visible in diagnostics rather than invented effects.
"""
from __future__ import annotations

import math

DISCOUNT = 0.85
MAX_HORIZON = 3.0
FUTURE_DRAW = 5.0
FUTURE_ENERGY = 3.0
DIMENSIONS = ('damage', 'block', 'hits', 'self_events', 'self_loss',
              'attack_cards', 'block_cards', 'block_events', 'exhausts', 'draw', 'energy', 'plays')


def power(player: dict, name: str) -> float:
    return sum(float(p.get('amount') or 0) for p in player.get('powers', [])
               if str(p.get('id') or p.get('power_id') or '').split('.')[-1]
               in {name, name + '_POWER'})


def pile_cards(combat: dict, name: str) -> list[dict] | None:
    value = combat.get(name)
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and value.get('kind') == 'card_multiset':
        return [row['card'] for row in value['cards'] for _ in range(row['count'])]
    return None


def _effect(card: dict, catalog: dict, energy: float, *, future: bool = False,
            visible_cost=None) -> dict | None:
    info = catalog.get(card.get('card_id'), {})
    stats = {str(k).lower(): v for k, v in
             (info.get('stats_upgraded' if card.get('upgrade') else 'stats') or {}).items()}
    # Future averages use printed costs, not a temporary hand cost reduction.
    cost = info.get('cost_upgraded' if card.get('upgrade') else 'cost') if future else card.get('display_cost')
    if cost is None and not future:
        cost = visible_cost
    if cost is None and not future:
        cost = card.get('current_cost')
    if cost is None:
        cost = info.get('cost_upgraded' if card.get('upgrade') else 'cost')
    x_cost = cost == 'X' or (not future and card.get('display_costs_x') is True)
    if x_cost:
        cost = energy
    if not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        return None
    def number(key):
        value = stats.get(key)
        return max(0.0, float(value)) if isinstance(value, (int, float)) and math.isfinite(value) else 0.0
    kind = str(info.get('type', '')).lower()
    # Only resolved direct Attack/Skill stats are supported. Power stats do not
    # describe an immediate effect; arbitrary conditionals must not be guessed.
    if kind not in {'attack', 'skill'}:
        return None
    repeats = number('repeat') or (2.0 if card.get('card_id') == 'TWIN_STRIKE' else 1.0)
    if x_cost:
        if card.get('card_id') != 'WHIRLWIND':
            return None
        repeats = energy
    if card.get('card_id') in {'FIEND_FIRE', 'SECOND_WIND'}:
        return None  # Consumes other playable hand cards; aggregate estimates are unsafe.
    damage = number('damage') if kind == 'attack' else 0.0
    loss = number('hploss')
    draw = number('cards') if card.get('card_id') in {
        'SHRUG_IT_OFF', 'OFFERING', 'BURNING_PACT', 'BATTLE_TRANCE', 'POMMEL_STRIKE'} else 0.0
    gained_energy = number('energy') if card.get('card_id') in {'OFFERING', 'BLOODLETTING'} else 0.0
    keywords = {str(k).split('.')[-1].lower() for k in card.get('keywords') or []}
    exhausts = float('exhaust' in keywords) + float(card.get('card_id') in {'TRUE_GRIT', 'BURNING_PACT'})
    if damage == 0 and number('block') == 0 and loss == 0 and draw == 0 and gained_energy == 0 and exhausts == 0:
        return None
    return {'cost': float(cost), 'damage': damage * repeats,
            'hits': repeats if damage > 0 else 0.0, 'block': number('block'),
            'self_events': 1.0 if loss > 0 else 0.0, 'self_loss': loss,
            'attack_cards': float(damage > 0), 'block_cards': float(number('block') > 0),
            'block_events': float(number('block') > 0), 'exhausts': exhausts,
            'draw': draw, 'energy': gained_energy, 'plays': 1.0}


def opportunities(state: dict, catalog: dict, incoming: float, visible_costs=None) -> dict:
    combat = state.get('combat') or {}
    player = combat.get('player') or {}
    energy = max(0.0, min(20.0, float(player.get('energy') or 0)))
    strength = power(player, 'STRENGTH')
    legal = {a.get('card_index') for a in combat.get('available_actions', [])
             if a.get('action_type') == 'play_card'}
    enemy_hp = sum(max(0, float(e.get('hp') or 0)) for e in combat.get('enemies', []))
    gap = max(0.0, incoming - float(player.get('block') or 0))
    empty = dict.fromkeys(DIMENSIONS, 0.0)
    # Half-energy units conservatively round costs up. Each row is considered
    # once. This estimates shared opportunities, never returns an executable plan.
    budget = int(energy * 2)
    choices = {0: empty}
    unsupported = set()
    def utility(row):
        return (min(enemy_hp, max(0, row['damage'] + strength * row['hits']))
                + min(gap, row['block']) - 2.0 * row['self_loss'])
    for index, card in enumerate(combat.get('hand') or []):
        if index not in legal:
            continue
        override = visible_costs[index] if visible_costs is not None and index < len(visible_costs) else None
        effect = _effect(card, catalog, energy, visible_cost=override)
        if effect is None:
            unsupported.add(str(card.get('card_id')))
            continue
        cost = math.ceil(effect['cost'] * 2)
        for used, row in list(choices.items()):
            if used + cost > budget:
                continue
            candidate = {key: row[key] + effect[key] for key in DIMENSIONS}
            if candidate['self_loss'] >= float(player.get('hp') or 0):
                continue
            old = choices.get(used + cost)
            if old is None or utility(candidate) > utility(old):
                choices[used + cost] = candidate
    immediate = max(choices.values(), key=utility)
    names = ('hand', 'draw_pile', 'discard_pile', 'play_pile')
    missing = [name for name in names if pile_cards(combat, name) is None]
    cycling = [card for name in names for card in (pile_cards(combat, name) or [])]
    future = dict(empty)
    effects = []
    # Missing piles are unknown, never an empty deck or a fabricated composition.
    if not missing and cycling:
        effects = []
        for card in cycling:
            effect = _effect(card, catalog, FUTURE_ENERGY, future=True)
            if effect is not None:
                effects.append(effect)
            else:
                unsupported.add(str(card.get('card_id')))
        future = cycle_opportunity({'effects': effects, 'count': len(cycling)}, FUTURE_DRAW, FUTURE_ENERGY)
    return {'immediate': immediate, 'future': future, 'missing_piles': missing,
            'unsupported_cards': sorted(unsupported), 'cycling_count': len(cycling) if not missing else None,
            'cycle_profile': {'effects': effects, 'count': len(cycling) if not missing else 0},
            'hand_count': len(combat.get('hand') or []), 'hand_energy': energy}



def cycle_opportunity(profile: dict, draws: float, energy: float) -> dict:
    result = dict.fromkeys(DIMENSIONS, 0.0)
    count = profile['count']
    if count <= 0:
        return result
    draws = min(max(0.0, draws), float(count))
    effects = profile['effects']
    cost = sum(row['cost'] for row in effects) * draws / count
    paid = min(1.0, max(0.0, energy) / max(cost, 1e-9))
    for row in effects:
        fraction = draws / count * (paid if row['cost'] > 0 else 1.0)
        for key in DIMENSIONS:
            result[key] += fraction * row[key]
    return result


def resource_opportunity(op: dict, *, turn: int, draw: float, energy: float) -> dict:
    """One bounded marginal expansion; resources are never scored on their own."""
    seen = op['hand_count'] if turn == 0 else FUTURE_DRAW
    budget = op['hand_energy'] if turn == 0 else FUTURE_ENERGY
    before = cycle_opportunity(op['cycle_profile'], seen, budget)
    after = cycle_opportunity(op['cycle_profile'], seen + max(0, draw), budget + max(0, energy))
    delta = {key: max(0.0, after[key] - before[key]) for key in DIMENSIONS}
    # First-turn opportunities already spent some cards; cap any continuation.
    selected = op['immediate' if turn == 0 else 'future']['plays']
    room = max(0.0, min(op['cycle_profile']['count'], seen + max(0, draw)) - selected)
    factor = min(1.0, room / max(delta['plays'], 1e-9))
    return {key: value * factor for key, value in delta.items()}
