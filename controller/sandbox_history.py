"""History-preserving sandbox reconstruction without mutating archived snapshots."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from controller.combat_regressions import verify_integrity
from controller.live_session import read_json
from controller.search.actions import SearchAction, cli_payload_for_action


def action_command(action: dict) -> dict:
    if action['action_type'] == 'select_cards':
        return {'action': 'select_cards', 'params': {'indices': ','.join(map(str, action['indices']))}}
    name, params = cli_payload_for_action(SearchAction(**action))
    return {'action': name, 'params': params}


def with_restore_recipe(scene: dict, root: Path) -> dict:
    if scene.get('restore_recipe'):
        return scene
    value = copy.deepcopy(scene)
    scene_id = value['id']
    if scene_id.startswith('case_'):
        directory = root / 'data/combat_regressions' / scene_id[5:]
        verify_integrity(directory)
        evidence = read_json(directory / 'evidence.json')
        commands = [row['shadow'] for row in evidence['replay_prefix'] if row['shadow']]
        value['restore_recipe'] = {
            'version': 1, 'kind': 'recorded_prefix',
            'save_json': (directory / 'anchor.save').read_text(encoding='utf-8-sig'),
            'anchor_room': evidence['anchor_room'], 'commands': commands}
        played = ((evidence['action'].get('client_before') or {}).get('combat') or {}).get('player', {}).get('cards_played_this_turn')
        value['turn_context'] = {'position': 'mid' if played else 'start' if played == 0 else 'unknown',
                                 'cards_played_before_root': played}
    elif value.get('source') == 'generated' and value.get('parameters', {}).get('deck'):
        params = value['parameters']
        snapshot = json.loads(value['snapshot_json'])
        encounter = json.loads(snapshot['RoomJson'])['encounter_id']['Entry']
        configure = {'cmd': 'configure_sandbox', 'hp': int(params.get('hp', 60)),
                     'energy': int(params.get('energy', 3)), 'upgrade_hand': bool(params.get('upgrade_hand'))}
        if params.get('hand'):
            configure['hand'] = params['hand']
        if params.get('enemy_hp'):
            configure['enemy_hp_value'] = int(params['enemy_hp'])
        value['restore_recipe'] = {'version': 1, 'kind': 'generated_prefix',
                                   'seed': params['seed'], 'deck': params['deck'],
                                   'encounter': encounter, 'configure': configure, 'commands': []}
        value['turn_context'] = {'position': 'start', 'cards_played_before_root': 0}
    else:
        value['turn_context'] = {'position': 'unknown', 'history_complete': False}
    value.setdefault('turn_context', {})['root_turn'] = (value['root_state'].get('combat') or {}).get('turn_number')
    value['turn_context']['history_complete'] = bool(value.get('restore_recipe'))
    return value


def recipe_with_prefix(scene: dict, trace: list[dict]) -> dict:
    recipe = copy.deepcopy(scene.get('restore_recipe'))
    if not recipe:
        raise ValueError('This scene has no history-preserving reconstruction recipe')
    recipe['commands'].extend(action_command(row['action']) for row in trace)
    return recipe
