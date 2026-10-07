from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


BASE_NODE_WEIGHTS = {
    'RestSite': 10.0,
    'Elite': 8.0,
    'Unknown': 6.0,
    'Event': 6.0,
    'Treasure': 5.0,
    'Monster': 4.0,
    'Shop': 2.0,
    'Boss': 0.0,
    'Ancient': 0.0,
}


def _coord(row: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    value = row.get('coord') or row
    try:
        return int(value.get('col')), int(value.get('row'))
    except (AttributeError, TypeError, ValueError):
        return None


def _node_type(row: Dict[str, Any]) -> str:
    return str(row.get('type') or row.get('node_type') or '')


def _nodes(map_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = map_data.get('rows') or []
    if isinstance(rows, list) and all(isinstance(row, list) for row in rows):
        return [node for row in rows for node in (row or [])]
    return list(map_data.get('nodes') or [])


def _node_score(node_type: str, row: int, max_row: int) -> float:
    if node_type == 'Shop':
        progress = row / max(max_row, 1)
        return BASE_NODE_WEIGHTS['Shop'] + 6.0 * progress
    return BASE_NODE_WEIGHTS.get(node_type, 0.0)


def choose_weighted_route(
    choices: List[Dict[str, Any]], map_data: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    node_map = {coord: dict(node) for node in _nodes(map_data) if (coord := _coord(node)) is not None}
    boss = map_data.get('boss') or {}
    boss_coord = _coord(boss)
    if boss_coord is not None:
        node_map[boss_coord] = dict(boss)
    if not choices or not node_map:
        raise ValueError('weighted route requires choices and a complete map')

    max_row = max((row for _, row in node_map), default=1)
    memo: Dict[Tuple[int, int], Tuple[float, List[Dict[str, Any]]]] = {}

    def best_from(key: Tuple[int, int]) -> Tuple[float, List[Dict[str, Any]]]:
        if key in memo:
            score, route = memo[key]
            return score, list(route)
        node = node_map.get(key) or {}
        node_type = _node_type(node)
        own = _node_score(node_type, key[1], max_row)
        route_head = {'col': key[0], 'row': key[1], 'type': node_type, 'node_score': round(own, 3)}
        child_keys = [coord for child in (node.get('children') or []) if (coord := _coord(child)) is not None]
        candidates = [best_from(child) for child in child_keys]
        if candidates:
            child_score, child_route = max(
                candidates, key=lambda item: (item[0], len(item[1]), -item[1][0]['col'])
            )
            result = own + child_score, [route_head] + child_route
        else:
            result = own, [route_head]
        memo[key] = result
        return result[0], list(result[1])

    ranked = []
    for choice in choices:
        key = _coord(choice)
        if key is None or key not in node_map:
            continue
        score, route = best_from(key)
        ranked.append((score, len(route), -key[0], key, route))
    if not ranked:
        raise ValueError('none of the choices exists in the complete map')
    score, _, _, key, route = max(ranked)
    route[0]['route_score'] = round(score, 3)
    return {'col': key[0], 'row': key[1]}, route
