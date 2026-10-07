"""Visible-only, deterministic decisions for the Crystal Sphere event."""

from typing import Any, Mapping


def board_identity(state: Mapping[str, Any]) -> tuple:
    """The information both the native UI and the headless event expose."""
    width, height, remaining = (state.get(key) for key in ('width', 'height', 'remaining'))
    if any(type(value) is not int for value in (width, height, remaining)):
        raise ValueError('Crystal Sphere dimensions or remaining count are unavailable')
    if width <= 0 or height <= 0 or remaining < 0:
        raise ValueError('Crystal Sphere dimensions or remaining count are invalid')
    rows = state.get('visible_cells')
    if not isinstance(rows, list):
        raise ValueError('Crystal Sphere visible cells are unavailable')
    cells = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Crystal Sphere visible cell is invalid')
        x, y = row.get('x'), row.get('y')
        if type(x) is not int or type(y) is not int or not (0 <= x < width and 0 <= y < height):
            raise ValueError('Crystal Sphere visible cell coordinates are invalid')
        cells.append((x, y, row.get('item_kind')))
    if len({(x, y) for x, y, _ in cells}) != len(cells):
        raise ValueError('Crystal Sphere visible cells contain duplicate coordinates')
    return width, height, remaining, tuple(sorted(cells))


def choose_divination(state: Mapping[str, Any]) -> dict[str, Any]:
    width, height, remaining, cells = board_identity(state)
    if remaining <= 0:
        raise ValueError('Crystal Sphere has no divinations remaining')
    shown = {(x, y) for x, y, _ in cells}
    cursed = {(x, y) for x, y, kind in cells if kind and 'Curse' in kind}
    best = None
    for y in range(height):
        for x in range(width):
            if (x, y) in shown:
                continue
            for tool in ('big', 'small'):
                radius = 1 if tool == 'big' else 0
                newly_seen = {(xx, yy)
                              for xx in range(max(0, x-radius), min(width, x+radius+1))
                              for yy in range(max(0, y-radius), min(height, y+radius+1))
                              if (xx, yy) not in shown}
                # A revealed curse is evidence of nearby hidden curse cells.
                # Prefer broad coverage, but avoid exposing its neighborhood.
                curse_exposure = sum(
                    any(abs(xx-cx) <= 1 and abs(yy-cy) <= 1 for cx, cy in cursed)
                    for xx, yy in newly_seen)
                score = (len(newly_seen) - 3 * curse_exposure,
                         len(newly_seen), tool == 'big', -y, -x)
                if best is None or score > best[0]:
                    best = (score, {'x': x, 'y': y, 'tool': tool,
                                    'expected_remaining': remaining})
    if best is None:
        raise ValueError('Crystal Sphere has no hidden cells')
    return best[1]
