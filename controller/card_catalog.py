from __future__ import annotations

import csv
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from controller.deck_profile import normalize_card_id


_FORMAT_TAG = re.compile(r'\[/?[A-Za-z][^\]]*\]')
_INNER_FIELD = re.compile(r'\{([^{}]*)\}')


@lru_cache(maxsize=4)
def _card_localization(repo_root: Path, language: str) -> Dict[str, str]:
    path = repo_root / 'third_party' / 'sts2-cli' / f'localization_{language}' / 'cards.json'
    if not path.is_file():
        return {}
    with path.open('r', encoding='utf-8') as stream:
        value = json.load(stream)
    return value if isinstance(value, dict) else {}


@lru_cache(maxsize=2)
def _card_values(repo_root: Path) -> Dict[str, Dict[str, Any]]:
    path = repo_root / 'data' / 'card_stats' / 'sts2_card_values.json'
    if not path.is_file():
        return {}
    with path.open('r', encoding='utf-8') as stream:
        value = json.load(stream)
    cards = value.get('cards') if isinstance(value, dict) else None
    return cards if isinstance(cards, dict) else {}


def _number(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def resolve_card_description(template: str, stats: Dict[str, Any] | None, *,
                             upgraded: bool = False, language: str = 'en') -> str:
    """Resolve the game's SmartFormat card text with exported DynamicVar values."""
    values = {str(key).lower(): value for key, value in (stats or {}).items()}
    dynamic = '动态值' if language == 'zh' else 'dynamic value'

    def lookup(name: str) -> Any:
        return values.get(name.strip().lower())

    def replace(field: re.Match[str]) -> str:
        expression = field.group(1)
        if expression == 'singleStarIcon':
            return '★'
        if expression.startswith('IfUpgraded:show:'):
            choices = expression[len('IfUpgraded:show:'):].split('|', 1)
            return choices[0] if upgraded else (choices[1] if len(choices) > 1 else '')
        if expression.startswith(('InCombat:', 'IsTargeting:', 'IsMultiplayer:')):
            choices = expression.split(':', 1)[1].split('|', 1)
            return choices[1] if len(choices) > 1 else ''

        plural = re.fullmatch(r'([^:]+):plural:(.*)', expression, re.DOTALL)
        if plural:
            value = lookup(plural.group(1))
            choices = plural.group(2).split('|', 1)
            if value is None:
                return choices[-1]
            return choices[0] if float(value) == 1 else choices[-1]

        conditional = re.fullmatch(r'([^:]+):cond:>0\?(.*)', expression, re.DOTALL)
        if conditional:
            choices = conditional.group(2).split('|', 1)
            value = lookup(conditional.group(1))
            return choices[0] if value is not None and float(value) > 0 else (choices[1] if len(choices) > 1 else '')

        choose = re.fullmatch(r'([^:]+):choose\(([^)]*)\):(.*)', expression, re.DOTALL)
        if choose:
            options = choose.group(3).split('|')
            selected = lookup(choose.group(1))
            labels = choose.group(2).split('|')
            try:
                return options[labels.index(str(selected))]
            except (ValueError, IndexError):
                return dynamic

        icon = re.fullmatch(r'([^:]+):(energyIcons|starIcons)\(([^)]*)\)', expression)
        if icon:
            name, kind, explicit = icon.groups()
            if name.lower() == 'energyprefix':
                return '⚡'
            value = lookup(name)
            if value is None and explicit:
                value = explicit
            symbol = '⚡' if kind == 'energyIcons' else '★'
            return f'{_number(value)}{symbol}' if value is not None else dynamic

        simple = re.fullmatch(r'([^:]+)(?::(?:diff|inverseDiff)\(\))?', expression)
        if simple:
            value = lookup(simple.group(1))
            return _number(value) if value is not None else dynamic

        branch = expression.split(':', 1)
        if len(branch) == 2 and '|' in branch[1]:
            choices = branch[1].split('|', 1)
            return choices[0] if lookup(branch[0]) else choices[1]
        return dynamic

    result = str(template or '')
    for _ in range(12):
        resolved, count = _INNER_FIELD.subn(replace, result)
        result = resolved
        if count == 0:
            break
    result = _INNER_FIELD.sub(dynamic, result)
    result = _FORMAT_TAG.sub('', result)
    result = re.sub(r'[ \t]+\n', '\n', result)
    result = re.sub(r'[ \t]{2,}', ' ', result)
    return result.strip()


def _apply_runtime_values(row: Dict[str, Any], runtime: Dict[str, Any] | None) -> Dict[str, Any]:
    if not runtime:
        row['description_en'] = resolve_card_description(row['description_en'], None, language='en')
        row['description_zh'] = resolve_card_description(row['description_zh'], None, language='zh')
        return row

    base_stats = runtime.get('stats') or {}
    upgraded = runtime.get('after_upgrade') or {}
    upgraded_stats = upgraded.get('stats') or base_stats
    row['description_en_template'] = row['description_en']
    row['description_zh_template'] = row['description_zh']
    row['description_en'] = resolve_card_description(row['description_en'], base_stats, language='en')
    row['description_zh'] = resolve_card_description(row['description_zh'], base_stats, language='zh')
    row['description_en_upgraded'] = resolve_card_description(
        row['description_en_template'], upgraded_stats, upgraded=True, language='en')
    row['description_zh_upgraded'] = resolve_card_description(
        row['description_zh_template'], upgraded_stats, upgraded=True, language='zh')
    row['cost'] = 'X' if runtime.get('costs_x') else runtime.get('cost', row['cost'])
    row['cost_upgraded'] = 'X' if runtime.get('costs_x') else upgraded.get('cost', row['cost'])
    row['stats'] = base_stats
    row['stats_upgraded'] = upgraded_stats
    row['upgradable'] = (
        row['cost_upgraded'] != row['cost']
        or {str(k).lower(): v for k, v in upgraded_stats.items()}
        != {str(k).lower(): v for k, v in base_stats.items()}
        or row['description_en_upgraded'] != row['description_en']
        or row['description_zh_upgraded'] != row['description_zh']
        or bool(upgraded.get('added_keywords') or upgraded.get('removed_keywords'))
    )
    return row


@lru_cache(maxsize=4)
def load_card_catalog(repo_root: Path, character: str = 'IRONCLAD') -> List[Dict[str, Any]]:
    path = repo_root / 'data' / 'card_stats' / 'sts2_linear_metadata_dataset.csv'
    wanted = str(character or '').strip().upper()
    rows: List[Dict[str, Any]] = []
    known = set()
    english = _card_localization(repo_root, 'eng')
    chinese = _card_localization(repo_root, 'zhs')
    runtime_values = _card_values(repo_root)
    with path.open('r', encoding='utf-8-sig', newline='') as stream:
        for raw in csv.DictReader(stream):
            pool = str(raw.get('primary_pool') or '').strip().upper()
            if wanted and pool not in {wanted, 'COLORLESS'}:
                continue
            card_id = normalize_card_id(raw.get('card_id'))
            if not card_id:
                continue
            try:
                cost: int | str = int(raw.get('cost') or 0)
            except ValueError:
                cost = str(raw.get('cost') or '?')
            rows.append(_apply_runtime_values({
                'id': card_id,
                'name_en': str(english.get(f'{card_id}.title') or raw.get('name_en') or raw.get('class_name') or card_id),
                'name_zh': str(chinese.get(f'{card_id}.title') or raw.get('name_zh') or ''),
                'description_en': str(english.get(f'{card_id}.description') or raw.get('description_en') or ''),
                'description_zh': str(chinese.get(f'{card_id}.description') or ''),
                'type': str(raw.get('type') or ''),
                'rarity': str(raw.get('rarity') or ''),
                'cost': cost,
                'pool': str(raw.get('primary_pool') or ''),
            }, runtime_values.get(card_id)))
            known.add(card_id)
    fallback_path = repo_root / 'data' / 'card_stats' / 'gamersky_sts2_card_stats.csv'
    character_names = {'IRONCLAD': '铁甲战士', 'SILENT': '静默猎手', 'DEFECT': '故障机器人',
                       'NECROBINDER': '亡灵契约师', 'REGENT': '储君'}
    type_names = {'攻击': 'Attack', '技能': 'Skill', '能力': 'Power', '诅咒': 'Curse', '状态': 'Status'}
    rarity_names = {'普通': 'Common', '罕见': 'Uncommon', '稀有': 'Rare', '基础': 'Basic', '特殊': 'Special'}
    with fallback_path.open('r', encoding='utf-8-sig', newline='') as stream:
        for raw in csv.DictReader(stream):
            if str(raw.get('character_query') or '') != character_names.get(wanted, wanted):
                continue
            card_id = normalize_card_id(raw.get('cardEnName'))
            if not card_id or card_id in known:
                continue
            name_en = str(raw.get('enName') or card_id).replace('_', ' ').title()
            rows.append(_apply_runtime_values({
                'id': card_id,
                'name_en': str(english.get(f'{card_id}.title') or name_en),
                'name_zh': str(chinese.get(f'{card_id}.title') or raw.get('zhName') or ''),
                'description_en': str(english.get(f'{card_id}.description') or ''),
                'description_zh': str(chinese.get(f'{card_id}.description') or ''),
                'type': type_names.get(str(raw.get('category') or ''), str(raw.get('category') or '')),
                'rarity': rarity_names.get(str(raw.get('rarity') or ''), str(raw.get('rarity') or '')),
                'cost': '?',
                'pool': character.title(),
            }, runtime_values.get(card_id)))
            known.add(card_id)
    return sorted(rows, key=lambda row: (row['pool'] != character.title(), row['name_en'], row['id']))
