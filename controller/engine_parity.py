from __future__ import annotations

import hashlib
import json
from functools import wraps
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from controller.combat_intent import intent_deals_damage, intent_total_damage


@dataclass(frozen=True)
class ParityResult:
    status: str
    client_digest: str
    headless_digest: str
    differences: List[Dict[str, Any]]
    client: Dict[str, Any]
    headless: Dict[str, Any]


def compare_checkpoints(
    client: Dict[str, Any],
    headless: Dict[str, Any],
) -> ParityResult:
    client_digest = _digest(client)
    headless_digest = _digest(headless)
    differences: List[Dict[str, Any]] = []
    _diff("", client, headless, differences)
    missing = list(client.get('_missing_fields') or []) + list(headless.get('_missing_fields') or [])
    for side, checkpoint in [('client', client), ('headless', headless)]:
        for path in _incomplete_canonical(checkpoint):
            missing.append(side + '.' + path)
    if missing:
        differences.insert(0, {'path': 'required_fields', 'client': client.get('_missing_fields', []),
                               'headless': headless.get('_missing_fields', []), 'missing': sorted(set(missing))})
    return ParityResult(
        status="INCOMPLETE" if missing else ("PASS" if not differences else "FAIL"),
        client_digest=client_digest,
        headless_digest=headless_digest,
        differences=differences,
        client=client,
        headless=headless,
    )


def _paths_missing(value, paths):
    missing = []
    def visit(current, parts, prefix):
        if not parts:
            if current is None:
                missing.append(prefix)
            return
        key, *rest = parts
        if key == '*':
            if not isinstance(current, list):
                missing.append(prefix)
            else:
                for index, item in enumerate(current):
                    visit(item, rest, prefix + '[' + str(index) + ']')
        elif not isinstance(current, dict) or key not in current:
            missing.append((prefix + '.' + key).strip('.'))
        else:
            visit(current[key], rest, (prefix + '.' + key).strip('.'))
    for path in paths:
        visit(value, path.split('.'), '')
    return missing


def _incomplete_canonical(value):
    if value.get('checkpoint') == 'card_reward':
        return _paths_missing(value, ['cards.*.id', 'cards.*.index'])
    paths = ['checkpoint', 'run.character', 'run.ascension', 'run.act', 'run.floor', 'run.hp',
             'run.max_hp', 'run.gold', 'run.boss', 'run.deck.*.id',
             'run.deck.*.upgraded', 'run.relics.*', 'run.potions.*']
    if value.get('checkpoint') == 'combat_play':
        paths += ['turn', 'player.hp', 'player.max_hp', 'player.block', 'player.energy',
                  'player.powers.*', 'hand.*.id', 'hand.*.index', 'hand.*.cost',
                  'hand.*.playable', 'hand.*.upgraded', 'enemies.*.id', 'enemies.*.hp',
                  'enemies.*.max_hp', 'enemies.*.block', 'enemies.*.powers.*', 'enemies.*.intents.*.type']
    elif value.get('checkpoint') == 'map_select':
        paths += ['boss_coord', 'available.*', 'nodes.*.coord',
                  'nodes.*.type', 'nodes.*.children.*']
    elif value.get('checkpoint') in {'run_boundary', 'reward_lifecycle'}:
        if value.get('checkpoint') == 'reward_lifecycle':
            paths += ['reward_set_id', 'phase', 'offered.*.index', 'offered.*.reward_type',
                      'offered.*.successfully_selected', 'cards.*.id', 'cards.*.index']
    else:
        return ['checkpoint']
    missing = _paths_missing(value, paths)
    if value.get('checkpoint') == 'map_select' and 'current_coord' not in value:
        missing.append('current_coord')
    run = value.get('run') or {}
    for key in ('ascension', 'act', 'floor', 'hp', 'max_hp', 'gold'):
        if type(run.get(key)) is not int:
            missing.append('run.' + key)
    if value.get('checkpoint') == 'combat_play':
        for key in ('hp', 'max_hp', 'block', 'energy'):
            if type((value.get('player') or {}).get(key)) is not int:
                missing.append('player.' + key)
        for index, enemy in enumerate(value.get('enemies') or []):
            for intent in enemy.get('intents') or []:
                if intent_deals_damage(intent) and (type(intent.get('damage')) is not int or
                                                    type(intent.get('hits')) is not int):
                    missing.append(f'enemies[{index}].intents.attack')
    return missing


def _validate_source(kind):
    def decorate(function):
        @wraps(function)
        def checked(*args, **kwargs):
            state = args[0] if args else kwargs['state']
            if kind.startswith('client'):
                paths = ['run.character_id', 'run.ascension', 'run.act_id', 'run.floor', 'run.current_hp',
                         'run.max_hp', 'run.gold', 'run.boss_id', 'run.deck.*.card_id',
                         'run.deck.*.upgraded', 'run.relics.*.relic_id', 'run.potions.*.occupied']
                if kind == 'client_combat':
                    paths += ['combat.hand.*.card_id', 'combat.hand.*.upgraded',
                              'combat.hand.*.playable', 'combat.player.powers.*.amount',
                              'combat.enemies.*.powers.*.amount', 'combat.enemies.*.intents.*.intent_type']
                else:
                    paths += ['map.nodes.*.children.*', 'map.available_nodes.*']
            else:
                paths = ['player.name', 'context.act', 'player.deck.*.id', 'player.deck.*.upgraded',
                         'player.relics.*.id', 'player.potions.*.id']
            missing = _paths_missing(state, paths)
            if kind == 'headless_combat':
                search = args[1] if len(args) > 1 else kwargs['search_state']
                missing += _paths_missing(search, ['combat.available_actions.*', 'combat.hand.*.upgrade',
                    'combat.player.powers.*', 'combat.enemies.*.powers.*', 'combat.enemies.*.intent.intent_types.*'])
            value = function(*args, **kwargs)
            if missing:
                value['_missing_fields'] = missing
            return value
        return checked
    return decorate


@_validate_source('client_map')
def client_map_checkpoint(state: Dict[str, Any]) -> Dict[str, Any]:
    run = _required_dict(state, "run")
    map_state = _required_dict(state, "map")
    if state.get("screen") != "MAP":
        raise ValueError(f"Client is not at a map checkpoint: {state.get('screen')!r}")
    nodes = [
        {
            "coord": _coord(node),
            "type": _node_type(node.get("node_type")),
            "children": sorted(_coord(child) for child in node.get("children") or []),
        }
        for node in map_state.get("nodes") or []
        if _node_type(node.get("node_type")) not in {"Ancient", "Boss"}
    ]
    return {
        "run": _client_run(run),
        "checkpoint": "map_select",
        "current_coord": _optional_coord(map_state.get("current_node")),
        "boss_coord": _coord(map_state.get("boss_node")),
        "available": sorted(_coord(node) for node in map_state.get("available_nodes") or []),
        "nodes": sorted(nodes, key=lambda node: node["coord"]),
    }


@_validate_source('headless_map')
def headless_map_checkpoint(
    state: Dict[str, Any],
    map_state: Dict[str, Any],
    run_id: str,
) -> Dict[str, Any]:
    if state.get("decision") != "map_select" or map_state.get("type") != "map":
        raise ValueError("Headless runtime is not at a complete map checkpoint")
    player = _required_dict(state, "player")
    context = _required_dict(state, "context")
    nodes = []
    for row in map_state.get("rows") or []:
        for node in row or []:
            nodes.append(
                {
                    "coord": _coord(node),
                    "type": _node_type(node.get("type")),
                    "children": sorted(_coord(child) for child in node.get("children") or []),
                }
            )
    return {
        "run": _headless_run(player, context, state, run_id),
        "checkpoint": "map_select",
        "current_coord": _optional_coord(map_state.get("current_coord")),
        "boss_coord": _coord(map_state.get("boss")),
        "available": sorted(_coord(node) for node in state.get("choices") or []),
        "nodes": sorted(nodes, key=lambda node: node["coord"]),
    }


@_validate_source('client_combat')
def client_combat_checkpoint(state: Dict[str, Any]) -> Dict[str, Any]:
    run = _required_dict(state, "run")
    combat = _required_dict(state, "combat")
    player = _required_dict(combat, "player")
    if not state.get("in_combat"):
        raise ValueError("Client is not at a combat checkpoint")
    return {
        "run": _client_run(run),
        "checkpoint": "combat_play",
        "turn": state.get("turn"),
        "player": {
            "hp": player.get("current_hp"),
            "max_hp": player.get("max_hp"),
            "block": player.get("block"),
            "energy": player.get("energy"),
            "powers": _client_powers(player.get("powers") or []),
        },
        "hand": [
            {
                "index": card.get("index"),
                "id": _id(card.get("card_id")),
                "upgraded": bool(card.get("upgraded")),
                "cost": 'X' if card.get('costs_x') is True else card.get("energy_cost"),
                "playable": bool(card.get("playable")),
            }
            for card in combat.get("hand") or []
        ],
        "enemies": [
            {
                "index": index,
                "id": _id(enemy.get("enemy_id")),
                "hp": enemy.get("current_hp"),
                "max_hp": enemy.get("max_hp"),
                "block": enemy.get("block"),
                "intents": [
                    {
                        "type": intent.get("intent_type"),
                        "damage": (
                            intent.get("total_damage")
                            if intent.get("total_damage") is not None
                            else intent.get("damage")
                        ),
                        "hits": intent.get("hits"),
                    }
                    for intent in enemy.get("intents") or []
                ],
                "powers": _client_powers(enemy.get("powers") or []),
            }
            for index, enemy in enumerate(client_living_enemies(state))
        ],
    }


def client_living_enemies(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    enemies = (state.get('combat') or {}).get('enemies') or []
    living = []
    for enemy in enemies:
        hp = enemy.get('current_hp')
        alive = enemy.get('is_alive')
        if alive is False and isinstance(hp, (int, float)) and hp <= 0:
            continue
        if alive is False or (alive is True and isinstance(hp, (int, float)) and hp <= 0):
            raise ValueError('Client enemy alive flag conflicts with HP')
        living.append(enemy)
    return living


@_validate_source('headless_combat')
def headless_combat_checkpoint(
    state: Dict[str, Any],
    search_state: Dict[str, Any],
    run_id: str,
) -> Dict[str, Any]:
    combat = _required_dict(search_state, "combat")
    player = _required_dict(combat, "player")
    context = _required_dict(state, "context")
    run_player = _required_dict(state, "player")
    available = combat.get("available_actions") or []
    playable_indices = {
        action.get("card_index")
        for action in available
        if action.get("action_type") == "play_card"
    }
    return {
        "run": _headless_run(run_player, context, search_state, run_id),
        "checkpoint": "combat_play",
        "turn": combat.get("round_number"),
        "player": {
            "hp": player.get("hp"),
            "max_hp": player.get("max_hp"),
            "block": player.get("block"),
            "energy": player.get("energy"),
            "powers": _headless_powers(player.get("powers") or []),
        },
        "hand": [
            {
                "index": card.get("index"),
                "id": _id(card.get("id")),
                "upgraded": bool((combat.get("hand") or [])[index].get("upgrade")),
                "cost": 'X' if card.get('costs_x') is True else card.get('display_cost', card.get("cost")),
                "playable": index in playable_indices,
            }
            for index, card in enumerate(state.get("hand") or [])
        ],
        "enemies": [
            {
                "index": index,
                "id": _id(enemy.get("monster_id")),
                "hp": enemy.get("hp"),
                "max_hp": enemy.get("max_hp"),
                "block": enemy.get("block"),
                "intents": _headless_intents(enemy.get("intent") or {}),
                "powers": _headless_powers(enemy.get("powers") or []),
            }
            for index, enemy in enumerate(combat.get("enemies") or [])
        ],
    }


def _client_run(run: Dict[str, Any]) -> Dict[str, Any]:
    act = _client_act(run)
    return {
        "character": _id(run.get("character_id")),
        "ascension": run.get("ascension"),
        "act": act,
        "floor": _client_act_floor(run.get("floor"), act),
        "hp": run.get("current_hp"),
        "max_hp": run.get("max_hp"),
        "gold": run.get("gold"),
        "boss": _id(run.get("boss_id")),
        "deck": [
            {"id": _id(card.get("card_id")), "upgraded": bool(card.get("upgraded"))}
            for card in run.get("deck") or []
        ],
        "relics": [_id(relic.get("relic_id")) for relic in run.get("relics") or []],
        "potions": [
            _id(potion.get("potion_id"))
            for potion in run.get("potions") or []
            if potion.get("occupied")
        ],
    }


def is_card_reward_selection(state: Dict[str, Any]) -> bool:
    reward = state.get('reward') or {}
    return (state.get('screen') in {'REWARD', 'CARD_SELECTION'}
            and reward.get('pending_card_choice') is True
            and isinstance(reward.get('card_options'), list)
            and bool(reward['card_options'])
            and bool(set(state.get('available_actions') or []).intersection(
                {'resolve_rewards', 'choose_reward_card', 'skip_reward_cards'})))


def client_reward_checkpoint(state: Dict[str, Any]) -> Dict[str, Any]:
    reward = _required_dict(state, 'reward')
    if state.get('screen') not in {'REWARD', 'CARD_SELECTION'}:
        raise ValueError('Client is not at a reward checkpoint')
    missing = _paths_missing(reward, ['card_options.*.card_id', 'card_options.*.index'])
    if not is_card_reward_selection(state):
        missing.append('reward.selection_boundary')
    if reward.get('pending_card_choice') is not True:
        missing.append('reward.pending_card_choice')
    if not reward.get('card_options'):
        missing.append('reward.card_options')
    value = {'checkpoint': 'card_reward', 'cards': [
        {'id': _id(card.get('card_id')), 'index': card.get('index')}
        for card in reward.get('card_options') or []]}
    if missing:
        value['_missing_fields'] = missing
    return value


def headless_reward_checkpoint(state: Dict[str, Any]) -> Dict[str, Any]:
    if state.get('decision') != 'card_reward':
        raise ValueError('Headless is not at a reward checkpoint')
    missing = _paths_missing(state, ['cards.*.id', 'cards.*.index'])
    value = {'checkpoint': 'card_reward', 'cards': [
        {'id': _id(card.get('id')), 'index': card.get('index')}
        for card in state.get('cards') or []]}
    if missing:
        value['_missing_fields'] = missing
    return value


def reward_lifecycle_checkpoint(state: Dict[str, Any], *, client: bool,
                                mapped_set_identity: str | None = None) -> Dict[str, Any]:
    reward = state.get('reward') or {} if client else state
    offered = reward.get('offered_rewards')
    cards = reward.get('card_options') or [] if client else state.get('cards') or []
    value = {
        'checkpoint': 'reward_lifecycle',
        'run': _client_run(state.get('run') or {}) if client else _headless_run(
            state.get('player') or {}, state.get('context') or {}, state, ''),
        'reward_set_id': (mapped_set_identity if mapped_set_identity is not None
                          else reward.get('reward_set_id')),
        'phase': ('card' if reward.get('pending_card_choice') else 'overview') if client
                 else ('card' if state.get('decision') == 'card_reward' else 'overview'),
        'offered': [{**r, 'index': r.get('native_index', r.get('index'))}
                    for r in offered or []],
        'cards': [{'id': _id(r.get('card_id') if client else r.get('id')), 'index': r.get('index')}
                  for r in cards],
    }
    for row in value['offered']:
        row.pop('native_index', None)
    if not isinstance(offered, list):
        value['_missing_fields'] = ['offered_rewards']
    return value


def _headless_run(
    player: Dict[str, Any],
    context: Dict[str, Any],
    state: Dict[str, Any],
    run_id: str,
) -> Dict[str, Any]:
    return {
        "character": _character_id(player.get("name")),
        "ascension": state.get("ascension", 0),
        "act": context.get("act", state.get("act")),
        "floor": context.get("floor", state.get("floor")),
        "hp": player.get("hp"),
        "max_hp": player.get("max_hp"),
        "gold": player.get("gold"),
        "boss": _id((context.get("boss") or {}).get("id")),
        "deck": [
            {"id": _id(card.get("id")), "upgraded": bool(card.get("upgraded"))}
            for card in player.get("deck") or []
        ],
        "relics": [_id(relic.get("id")) for relic in player.get("relics") or []],
        "potions": [_id(potion.get("id")) for potion in player.get("potions") or []],
    }


def _client_powers(powers: Iterable[Dict[str, Any]]) -> List[Tuple[str, Any]]:
    return sorted((_id(power.get("power_id")), power.get("amount")) for power in powers)


def _headless_powers(powers: Iterable[Dict[str, Any]]) -> List[Tuple[str, Any]]:
    return sorted((_id(power.get("id") or power.get("power_id")), power.get("amount")) for power in powers)


def _headless_intents(intent: Dict[str, Any]) -> List[Dict[str, Any]]:
    types = intent.get("intent_types") or []
    rows = []
    for intent_type in types:
        deals_damage = intent_deals_damage({"type": intent_type})
        rows.append({
            "type": intent_type,
            "damage": int(intent_total_damage(intent)) if deals_damage else None,
            "hits": (intent.get("hits") or 1) if deals_damage else None,
        })
    return rows


def _required_dict(container: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Required checkpoint field {key!r} is missing")
    return value


def _coord(value: Any) -> Tuple[int, int]:
    if not isinstance(value, dict) or value.get("row") is None or value.get("col") is None:
        raise ValueError(f"Incomplete map coordinate: {value!r}")
    return int(value["row"]), int(value["col"])


def _optional_coord(value: Any) -> Optional[Tuple[int, int]]:
    return None if value is None else _coord(value)


def _client_act(run: Dict[str, Any]) -> Optional[int]:
    value = run.get("act_id")
    if value is None:
        return None
    return int(value) + 1


def _client_act_floor(global_floor: Any, act: Optional[int]) -> Any:
    if global_floor is None or act is None:
        return global_floor
    offsets = {1: 0, 2: 17, 3: 33}
    if act not in offsets:
        raise ValueError(f"Unsupported client act for floor normalization: {act!r}")
    return int(global_floor) - offsets[act]


def _id(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value).split(".")[-1].upper()


def _character_id(value: Any) -> Optional[str]:
    text = str(value or "").upper().replace(" ", "_")
    if text.endswith('.TITLE'):
        text = text[:-len('.TITLE')]
    aliases = {"THE_IRONCLAD": "IRONCLAD"}
    return aliases.get(text, text or None)


def _node_type(value: Any) -> str:
    text = str(value or "")
    aliases = {"Rest": "RestSite"}
    return aliases.get(text, text)


def _digest(value: Dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _diff(path: str, left: Any, right: Any, out: List[Dict[str, Any]]) -> None:
    if type(left) is not type(right):
        out.append({"path": path or "$", "client": left, "headless": right})
        return
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else key
            if key not in left or key not in right:
                out.append({"path": child, "client": left.get(key), "headless": right.get(key)})
            else:
                _diff(child, left[key], right[key], out)
        return
    if isinstance(left, (list, tuple)):
        if len(left) != len(right):
            out.append({"path": path, "client": left, "headless": right})
            return
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            _diff(f"{path}[{index}]", left_item, right_item, out)
        return
    if left != right:
        out.append({"path": path, "client": left, "headless": right})
