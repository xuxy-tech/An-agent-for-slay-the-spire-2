from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[1] / 'data' / 'deck_profiles' / 'ironclad_self_damage.json'


def normalize_card_id(value: Any) -> str:
    card_id = str(value or '').strip().upper()
    if card_id.startswith('CARD.'):
        card_id = card_id[5:]
    return card_id.replace('-', '_').replace(' ', '_')


def card_id_from_row(card: Dict[str, Any]) -> str:
    return normalize_card_id(card.get('card_id') or card.get('id') or card.get('cardEnName') or card.get('name'))


def deck_card_ids(deck: Iterable[Dict[str, Any]]) -> list[str]:
    return [card_id_from_row(card) for card in deck]


def _migrate_legacy_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    legacy_ports = profile.get('ports') or {}
    legacy_cards = profile.get('cards') or {}
    ports = [
        {
            'id': str(port_id),
            'name': str(port.get('name') or port_id),
            'target': max(0, int(port.get('target') or 0)),
            'maximum': max(0, int(port.get('maximum') or port.get('target') or 0)),
        }
        for port_id, port in legacy_ports.items()
        if isinstance(port, dict)
    ]
    cards = [
        {
            'id': normalize_card_id(card_id),
            'max_copies': max(1, int(spec.get('max_copies') or 1)),
            'ports': [str(port_id) for port_id in spec.get('ports') or []],
        }
        for card_id, spec in sorted(
            legacy_cards.items(),
            key=lambda row: -float((row[1] or {}).get('base_score') or 0.0),
        )
        if isinstance(spec, dict)
    ]
    return {
        'schema_version': 2,
        'id': profile.get('id'),
        'name': profile.get('name'),
        'character': profile.get('character'),
        'combat_coefficients': dict(profile.get('combat_coefficients') or {}),
        'deck_size': dict(profile.get('deck_size') or {}),
        'ports': ports,
        'cards': cards,
    }


def normalize_deck_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(profile, dict):
        raise ValueError('Deck profile must be an object')
    if int(profile.get('schema_version') or 1) < 2 or isinstance(profile.get('ports'), dict):
        profile = _migrate_legacy_profile(profile)
    normalized = dict(profile)
    normalized['schema_version'] = 2
    normalized['id'] = str(profile.get('id') or '').strip()
    normalized['name'] = str(profile.get('name') or normalized['id']).strip()
    normalized['character'] = str(profile.get('character') or 'IRONCLAD').strip().upper()

    ports = []
    seen_ports = set()
    for raw in profile.get('ports') or []:
        if not isinstance(raw, dict):
            raise ValueError('Every deck profile port must be an object')
        port_id = str(raw.get('id') or '').strip().lower().replace(' ', '_')
        if not port_id or port_id in seen_ports:
            raise ValueError(f'Invalid or duplicate deck profile port: {port_id!r}')
        target = max(0, int(raw.get('target') or 0))
        maximum = max(target, int(raw.get('maximum') if raw.get('maximum') is not None else target))
        ports.append({'id': port_id, 'name': str(raw.get('name') or port_id).strip(),
                      'target': target, 'maximum': maximum})
        seen_ports.add(port_id)
    normalized['ports'] = ports

    cards = []
    seen_cards = set()
    for raw in profile.get('cards') or []:
        if not isinstance(raw, dict):
            raise ValueError('Every deck profile card must be an object')
        card_id = normalize_card_id(raw.get('id'))
        if not card_id or card_id in seen_cards:
            raise ValueError(f'Invalid or duplicate deck profile card: {card_id!r}')
        card_ports = [str(value).strip().lower() for value in raw.get('ports') or []]
        unknown = [value for value in card_ports if value not in seen_ports]
        if unknown:
            raise ValueError(f'{card_id} references unknown ports: {unknown}')
        if not card_ports:
            raise ValueError(f'{card_id} must belong to at least one port')
        card = {'id': card_id, 'max_copies': max(1, int(raw.get('max_copies') or 1)),
                'ports': list(dict.fromkeys(card_ports))}
        selection = raw.get('selection') or {}
        if not isinstance(selection, dict):
            raise ValueError(f'{card_id} selection rules must be an object')
        if selection:
            card['selection'] = {
                key: (bool(value) if key in {'always_take', 'early_filler', 'repeatable_trigger'}
                      else int(value))
                for key, value in selection.items()
                if key in {'priority', 'act_min', 'act_max', 'always_take', 'early_filler',
                           'repeatable_trigger', 'second_copy_max_strikes',
                           'min_repeatable_triggers', 'min_nonstarter_attacks'}
            }
        cards.append(card)
        seen_cards.add(card_id)
    normalized['cards'] = cards
    normalized['combat_coefficients'] = dict(profile.get('combat_coefficients') or {})
    normalized['deck_size'] = dict(profile.get('deck_size') or {})
    normalized['selection_rules'] = dict(profile.get('selection_rules') or {})
    return normalized


def load_deck_profile(path: Optional[Path | str] = None) -> Dict[str, Any]:
    source = Path(path) if path else DEFAULT_PROFILE_PATH
    profile = normalize_deck_profile(json.loads(source.read_text(encoding='utf-8-sig')))
    if not profile.get('id'):
        raise ValueError(f'Invalid deck profile: {source}')
    profile['_source'] = str(source.resolve())
    return profile


def save_deck_profile(path: Path, profile: Dict[str, Any]) -> None:
    validated = normalize_deck_profile(profile)
    validated.pop('_source', None)
    if not validated.get('id'):
        raise ValueError('Deck profile requires an id')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(validated, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def _card_specs(profile: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {normalize_card_id(row.get('id')): row for row in profile.get('cards') or []}


def profile_card_priority(profile: Dict[str, Any]) -> Dict[str, int]:
    profile = normalize_deck_profile(profile)
    return {row['id']: index for index, row in enumerate(profile.get('cards') or [])}


def _port_counts(profile: Dict[str, Any], deck_ids: Iterable[str]) -> Dict[str, int]:
    cards = _card_specs(profile)
    counts = Counter(normalize_card_id(value) for value in deck_ids)
    totals = {str(port.get('id')): 0 for port in profile.get('ports') or []}
    for card_id, copies in counts.items():
        for port in (cards.get(card_id) or {}).get('ports') or []:
            if port in totals:
                totals[port] += copies
    return totals


def score_profile_card(profile: Dict[str, Any], card_id: str, deck_ids: Iterable[str],
                       act: Optional[int] = None) -> Dict[str, Any]:
    profile = normalize_deck_profile(profile)
    card_id = normalize_card_id(card_id)
    deck_ids = [normalize_card_id(value) for value in deck_ids]
    counts = Counter(deck_ids)
    card_rows = profile.get('cards') or []
    card_index = next((index for index, row in enumerate(card_rows) if row['id'] == card_id), None)
    spec = card_rows[card_index] if card_index is not None else None
    port_rows = profile.get('ports') or []
    port_index = {row['id']: index for index, row in enumerate(port_rows)}
    port_counts = _port_counts(profile, deck_ids)
    deficits = {row['id']: max(0, row['target'] - port_counts.get(row['id'], 0)) for row in port_rows}
    common = {'profile_id': profile.get('id'), 'card_id': card_id, 'copies': counts[card_id],
              'port_counts': port_counts, 'deficits': deficits}
    if spec is None:
        return {**common, 'eligible': False, 'score': -1_000_000.0,
                'reason': 'not_in_profile', 'rejection_reason': 'not_in_profile'}
    cap = int(spec.get('max_copies') or 1)
    if counts[card_id] >= cap:
        return {**common, 'eligible': False, 'score': -1_000_000.0, 'copy_cap': cap,
                'reason': 'copy_cap_reached', 'rejection_reason': 'copy_cap_reached'}

    selection = spec.get('selection') or {}
    always_take = bool(selection.get('always_take'))
    if act is not None and ((selection.get('act_min') and act < selection['act_min'])
                            or (selection.get('act_max') and act > selection['act_max'])):
        return {**common, 'eligible': False, 'score': -1_000_000.0,
                'reason': 'outside_act_window', 'rejection_reason': 'outside_act_window'}
    rules = profile.get('selection_rules') or {}
    if selection.get('early_filler'):
        filler_count = sum(
            counts[row['id']] for row in card_rows
            if (row.get('selection') or {}).get('early_filler')
        )
        if filler_count >= int(rules.get('max_early_fillers', 999)):
            return {**common, 'eligible': False, 'score': -1_000_000.0,
                    'reason': 'early_filler_cap_reached', 'rejection_reason': 'early_filler_cap_reached'}
    strikes_limit = selection.get('second_copy_max_strikes')
    if strikes_limit is not None and counts[card_id] >= 1 and counts['STRIKE_IRONCLAD'] > strikes_limit:
        return {**common, 'eligible': False, 'score': -1_000_000.0,
                'reason': 'starter_strikes_not_removed', 'rejection_reason': 'starter_strikes_not_removed'}
    required_triggers = int(selection.get('min_repeatable_triggers') or 0)
    if required_triggers:
        triggers = sum(
            counts[row['id']] for row in card_rows
            if (row.get('selection') or {}).get('repeatable_trigger')
        )
        if triggers < required_triggers:
            return {**common, 'eligible': False, 'score': -1_000_000.0,
                    'reason': 'repeatable_triggers_missing', 'rejection_reason': 'repeatable_triggers_missing'}
    required_attacks = int(selection.get('min_nonstarter_attacks') or 0)
    if required_attacks:
        attacks = sum(
            counts[row['id']] for row in card_rows
            if row['id'] not in {'STRIKE_IRONCLAD', 'BASH'} and 'attack' in row['ports']
        )
        if attacks < required_attacks:
            return {**common, 'eligible': False, 'score': -1_000_000.0,
                    'reason': 'attack_density_missing', 'rejection_reason': 'attack_density_missing'}
    limits = profile.get('deck_size') or {}
    by_act = limits.get('optional_max_by_act') or {}
    if by_act and not always_take:
        limit = int(by_act.get(str(act), limits.get('maximum', 999)))
        if len(deck_ids) >= limit:
            return {**common, 'eligible': False, 'score': -1_000_000.0,
                    'reason': 'deck_size_cap_reached', 'rejection_reason': 'deck_size_cap_reached'}

    available_ports = [
        row for row in port_rows
        if row['id'] in spec.get('ports', []) and port_counts.get(row['id'], 0) < row['maximum']
    ]
    if not available_ports and not always_take:
        return {**common, 'eligible': False, 'score': -1_000_000.0, 'copy_cap': cap,
                'reason': 'port_cap_reached', 'rejection_reason': 'port_cap_reached'}
    assigned = next((row for row in available_ports if deficits.get(row['id'], 0) > 0),
                    available_ports[0] if available_ports else next(
                        row for row in port_rows if row['id'] in spec['ports']))
    filling_target = deficits.get(assigned['id'], 0) > 0
    priority = -1 if always_take else int(selection.get('priority', 0))
    rank = [priority, 0 if filling_target else 1,
            port_index[assigned['id']], int(card_index), counts[card_id]]
    score = 1_000_000.0 - rank[0] * 100_000.0 - rank[1] * 10_000.0 - rank[2] * 1_000.0 - rank[3] * 10.0 - rank[4]
    return {**common, 'eligible': True, 'score': score, 'copy_cap': cap,
            'assigned_port': assigned['id'], 'assigned_port_name': assigned['name'],
            'priority_rank': rank, 'filling_target': filling_target,
            'reason': 'ordered_priority', 'rejection_reason': None}


def choose_profile_reward(profile: Dict[str, Any], cards: Iterable[Dict[str, Any]],
                          deck: Iterable[Dict[str, Any]], act: Optional[int] = None):
    deck_ids = [card_id_from_row(card) for card in deck]
    scored = []
    best = None
    for position, card in enumerate(cards):
        row = score_profile_card(profile, card_id_from_row(card), deck_ids, act=act)
        row['index'] = int(card.get('index', position))
        scored.append(row)
        key = tuple(row.get('priority_rank') or [999, 999, 999, 999]) + (row['index'],)
        if row['eligible'] and (best is None or key < best['_selection_key']):
            best = {**row, '_selection_key': key}
    if best is not None:
        best.pop('_selection_key', None)
    return best, scored


def choose_profile_bundle(profile: Dict[str, Any], bundles: Iterable[Dict[str, Any]],
                          deck: Iterable[Dict[str, Any]], act: Optional[int] = None):
    deck_ids = [card_id_from_row(card) for card in deck]
    candidates = []
    for position, bundle in enumerate(bundles):
        cards = list(bundle.get('cards') or [])
        scored = [score_profile_card(profile, card_id_from_row(card), deck_ids, act=act) for card in cards]
        eligible = [row for row in scored if row['eligible']]
        if not eligible:
            continue
        best_rank = min(tuple(row['priority_rank']) for row in eligible)
        outside = sum(1 for row in scored if row['reason'] == 'not_in_profile')
        capped = len(scored) - len(eligible) - outside
        candidates.append({
            'index': int(bundle.get('index', position)),
            'cards': scored,
            'eligible_cards': len(eligible),
            'outside_cards': outside,
            'capped_cards': capped,
            '_selection_key': best_rank + (outside, capped, -len(eligible), position),
        })
    if not candidates:
        return None
    chosen = min(candidates, key=lambda row: row['_selection_key'])
    chosen.pop('_selection_key', None)
    return chosen
