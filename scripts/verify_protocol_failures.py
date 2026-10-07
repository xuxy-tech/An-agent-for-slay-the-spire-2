"""Read-only historical regressions in isolated engines; never sends client inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.engine_parity import (
    client_map_checkpoint, headless_map_checkpoint, client_combat_checkpoint,
    headless_combat_checkpoint, compare_checkpoints, _client_run, _headless_run,
)
from controller.rng_parity import compare_rng_snapshots

ROOT = Path(__file__).resolve().parents[1]
MAP_SAMPLES = ['172444_821915', '180620_946386', '180834_370436', '195215_715264']
EVENT_SAMPLES = ['183637_194961', '190418_025553', '194142_678037']


def checked(value):
    if value.get('type') == 'error':
        raise RuntimeError(json.dumps(value, ensure_ascii=False))
    return value


def load_report(suffix):
    path = ROOT / 'logs/live_dashboard' / ('20260927_' + suffix) / 'run_report.json'
    return json.loads(path.read_text(encoding='utf-8'))


def run_projection(client, shadow):
    return compare_checkpoints(
        {'checkpoint': 'run_boundary', 'run': _client_run(client['run'])},
        {'checkpoint': 'run_boundary', 'run': _headless_run(
            shadow['player'], shadow['context'], shadow, str(client.get('run_id') or ''))},
    )


def historical_command(cli, state, row):
    """Translate recorded player choices into the new native offer lifecycle.

    Original reports remain unchanged. Opening an old auto-open card reward is
    recorded as an extra native action; no rewards, RNG or player data are set.
    """
    action = row['client_action']
    command = (row.get('transaction') or {}).get('shadow')
    if action == 'claim_reward' and state.get('decision') == 'combat_reward':
        visible = (row.get('client_before') or {}).get('reward') or {}
        items = visible.get('rewards') or []
        offer = [r for r in state['offered_rewards'] if not r['successfully_selected']]
        if [r['reward_type'] for r in items] != [r['reward_type'] for r in offer]:
            raise RuntimeError(f"Historical ordered reward list differs at {row['sequence']}")
        selected = next(i for i, r in enumerate(items) if r['index'] == row['client_params']['option_index'])
        return 'claim_combat_reward', {'reward_index': offer[selected]['index'], 'reward_set_id': state['reward_set_id']}
    if action in {'choose_reward_card', 'skip_reward_cards'} and state.get('decision') == 'combat_reward':
        options = (row['client_before']['reward'].get('card_options') or [])
        ids = [(r['card_id'], r.get('upgraded', False)) for r in options]
        matches = [r for r in state['rewards'] if r['reward_type'] == 'Card'
                   and [(c['id'], c['upgraded']) for c in r['cards']] == ids]
        if len(matches) != 1:
            raise RuntimeError('Cannot identify historical auto-open card reward')
        state = checked(cli.action('claim_combat_reward', {'reward_index': matches[0]['index'],
                            'reward_set_id': state['reward_set_id']}, timeout_s=20))
    if action in {'collect_rewards_and_proceed', 'proceed'} and state.get('decision') == 'combat_reward':
        return 'finish_combat_rewards', {}
    if action == 'choose_event_option' and state.get('decision') == 'event_choice':
        telemetry = row.get('decision_telemetry') or {}
        option_id = telemetry.get('option_id')
        if option_id:
            matches = [r for r in state['options'] if r.get('text_key') == option_id]
            if len(matches) == 1:
                return 'choose_option', {'option_index': matches[0]['index']}
    if command:
        if command['action'] == 'ack_event_reward':
            raise RuntimeError('Historical acknowledgement has no identified reward')
        return command['action'], command.get('params') or {}
    return None


def verify_map(suffix):
    report = load_report(suffix)
    anchor = max((r for r in report['reanchors'] if r['status'] == 'REANCHORED_PASS'),
                 key=lambda r: r['action_sequence'])
    path = Path(anchor['save_path'])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == anchor['save_sha256']
    row = next(r for r in report['actions'] if r['sequence'] > anchor['action_sequence'])
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        state = checked(cli.load_save(str(path), resume_room=False))
        before = compare_checkpoints(client_map_checkpoint(row['client_before']),
            headless_map_checkpoint(state, cli.get_map(), row['client_before']['run_id']))
        before_rng = compare_rng_snapshots(row['client_rng_before'], cli.get_rng_snapshot())
        command = row['transaction']['shadow']
        state = checked(cli.action(command['action'], command['params'], timeout_s=20))
        after_rng = compare_rng_snapshots(row['client_rng_after'], cli.get_rng_snapshot())
        client = row['client_after']
        if client.get('in_combat'):
            search = cli.get_search_state()['combat_state_for_search']
            after = compare_checkpoints(client_combat_checkpoint(client),
                    headless_combat_checkpoint(state, search, client['run_id']))
        elif client.get('screen') == 'MAP':
            after = compare_checkpoints(client_map_checkpoint(client),
                        headless_map_checkpoint(state, cli.get_map(), client['run_id']))
        else:
            after = run_projection(client, state)
        return {'sequence': row['sequence'], 'before_state': before.status,
                'before_rng': before_rng.status, 'after_state': after.status,
                'after_rng': after_rng.status, 'decision': state.get('decision'),
                'differences': after.differences + after_rng.differences,
                'stderr_tail': list(cli._stderr_tail)}
    finally:
        cli.stop()


def verify_replay(suffix):
    report = load_report(suffix)
    cli = Sts2CliAdapter(CliConfig(ROOT))
    count = 0
    intermediate_segments = None
    try:
        cli.start()
        state = checked(cli.load_save(report['anchor_save'], resume_room=report.get('anchor_room', False)))
        for row in report['actions']:
            if row.get('status') != 'completed':
                raise RuntimeError(f"Unknown client outcome at {row['sequence']}")
            # GLASS_EYE's old policy abandoned four remaining groups. Stop
            # before that abandoned action and validate them separately below.
            if suffix == '194142_678037' and row['sequence'] == 259:
                break
            command = historical_command(cli, state, row)
            if command:
                state = checked(cli.action(*command, timeout_s=20))
            count = row['sequence']
            rng = compare_rng_snapshots(row['client_rng_after'], cli.get_rng_snapshot())
            if not rng.passed:
                raise RuntimeError(f"First RNG difference at {count}: {rng.differences[:4]}")
            if suffix == '171219_961079' and row['sequence'] == 287:
                client = row['client_after']
                observed = [enemy for enemy in (client.get('combat') or {}).get('enemies') or []
                            if str(enemy.get('enemy_id') or '').startswith('DECIMILLIPEDE_SEGMENT')]
                if len(observed) != 3 or sum((enemy.get('current_hp') or 0) > 0 for enemy in observed) != 1:
                    raise RuntimeError('Historical intermediate segment evidence changed')
                parity = compare_checkpoints(client_combat_checkpoint(client),
                    headless_combat_checkpoint(state, cli.get_search_state()['combat_state_for_search'], client['run_id']))
                intermediate_segments = {'sequence': count, 'living_segments': 1,
                                         'state': parity.status, 'rng': rng.status,
                                         'differences': parity.differences}
                if parity.status != 'PASS':
                    raise RuntimeError(f'Intermediate segment state differs: {parity.differences[:4]}')
        player = run_projection(row['client_after'] if count == row['sequence'] else row['client_before'], state)
        result = {'replayed_through': count, 'decision': state.get('decision'), 'state': player.status,
                  'rng': rng.status, 'differences': player.differences, 'stderr_tail': list(cli._stderr_tail)}
        if intermediate_segments is not None:
            result['intermediate_segments'] = intermediate_segments
        if suffix == '183637_194961':
            before_potions = [r['id'] for r in state['player']['potions']]
            before_rng = cli.get_rng_snapshot()
            result['offered_types'] = [r['reward_type'] for r in state['rewards']]
            finished = checked(cli.action('finish_combat_rewards', timeout_s=20))
            result['abandon_decision'] = finished.get('decision')
            result['potions_preserved'] = [r['id'] for r in finished['player']['potions']] == before_potions
            result['abandon_rng_unchanged'] = cli.get_rng_snapshot() == before_rng
        if suffix == '190418_025553':
            next_items = [r for r in state.get('rewards') or [] if r['reward_type'] == 'Card']
            result['remaining_card_groups_after_skip'] = len(next_items)
            if next_items:
                opened = checked(cli.action('claim_combat_reward', {
                    'reward_index': next_items[0]['index'], 'reward_set_id': state['reward_set_id']}, timeout_s=20))
                result['second_group_opened'] = opened.get('decision') == 'card_reward'
                result['second_group_card_count'] = len(opened.get('cards') or [])
                resolved = checked(cli.action('select_card_reward', {'card_index': 0}, timeout_s=20))
                result['second_group_resolved'] = resolved.get('decision') == 'combat_reward'
        if suffix == '194142_678037':
            from controller.run_agent import choose_card_reward
            from controller.deck_profile import load_deck_profile
            profile = load_deck_profile(report['config'].get('deck_profile'))
            groups = 1
            group_receipts = []
            while state.get('decision') == 'combat_reward' and state['rewards']:
                item = next(r for r in state['rewards'] if r['reward_type'] == 'Card')
                before_count = len(state['rewards'])
                before_deck = len(state['player']['deck'])
                state = checked(cli.action('claim_combat_reward', {'reward_index': item['index'],
                        'reward_set_id': state['reward_set_id']}, timeout_s=20))
                choice = choose_card_reward(state, ROOT, deck_profile=profile)
                state = checked(cli.action('select_card_reward' if choice else 'skip_card_reward', choice or {}, timeout_s=20))
                group_receipts.append({'group': groups + 1, 'action': 'select' if choice else 'skip',
                    'remaining_before': before_count, 'remaining_after': len(state.get('rewards') or []),
                    'deck_delta': len(state['player']['deck']) - before_deck})
                if len(state.get('rewards') or []) != before_count - 1:
                    raise RuntimeError(f'Card group {groups + 1} did not resolve exactly one item')
                groups += 1
            result['processed_groups'] = groups
            result['group_receipts'] = group_receipts
            result['remaining_items'] = len(state.get('rewards') or [])
            result['continuation'] = checked(cli.action('finish_combat_rewards', timeout_s=20)).get('decision')
            result['additional_groups_evidence'] = 'native offline choices; no historical client choices exist'
        return result
    except Exception as exc:
        return {'replayed_through': count, 'error': str(exc), 'stderr_tail': list(cli._stderr_tail)}
    finally:
        cli.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'logs/protocol_regression_20260927/results.json')
    parser.add_argument('--sample', action='append')
    args = parser.parse_args()
    results: dict[str, Any] = {'runtime': str(CliConfig(ROOT).dll_relpath), 'samples': {}}
    for suffix in args.sample or [*MAP_SAMPLES, '171219_961079', *EVENT_SAMPLES]:
        try:
            value = verify_map(suffix) if suffix in MAP_SAMPLES else verify_replay(suffix)
        except Exception as exc:
            value = {'error': str(exc)}
        results['samples'][suffix] = value
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'sample': suffix, **{k: v for k, v in value.items() if k not in {'stderr_tail', 'differences'}}}, ensure_ascii=False), flush=True)
    return int(any('error' in r or any(r.get(k) == 'FAIL' for k in ['state','rng','before_state','after_state','before_rng','after_rng'])
                   for r in results['samples'].values()))


if __name__ == '__main__':
    raise SystemExit(main())
