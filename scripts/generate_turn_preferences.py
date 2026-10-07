"""Current offline turn collector, independent of the live search policy."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

from cli.sts2_cli_adapter import CliConfig
from controller.combat_scoring import CombatScoring, stage_for_floor
from controller.combat_observation import project_model_state
from controller.search.combat_search import CombatSearcher, CombatSpec
from controller.turn_learning import SCHEMA, turn_groups, enumerate_turn, demonstration_leaf


class OfflineReplay(CombatSearcher):
    """Add native modal payload forwarding only to the offline replay helper."""
    @staticmethod
    def _strip_cli_payload(args, action_name):
        if action_name == 'select_cards':
            return {'indices': args['indices']}
        return CombatSearcher._strip_cli_payload(args, action_name)


def generate(args):
    from scripts.generate_human_counterfactuals import _sessions, _jsonl, _load_snapshot
    if args.workers < 1 or min(args.max_nodes, args.max_seconds, args.max_actions) < 0:
        raise ValueError('Workers must be positive and budgets nonnegative; zero disables a budget')
    excluded = []
    sessions = _sessions(args.human_input, excluded)
    if not sessions:
        result = {'schema': SCHEMA, 'sessions': 0, 'success': False,
                  'excluded_sessions': excluded, 'error': 'No eligible capture sessions; no replay attempted'}
        if getattr(args, 'report', None):
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
    jobs = []
    for session in sessions:
        for rows in turn_groups(_jsonl(session / 'events.jsonl')):
            jobs.append((session, rows))
    if args.limit is not None:
        jobs = jobs[:args.limit]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    completed = set()
    previous = {'leaves': 0, 'exhaustive_turns': 0, 'demonstrations': 0}
    if args.output.exists() and not args.overwrite:
        if not args.resume:
            raise ValueError('Output exists; use --resume or --overwrite')
        for row in _jsonl(args.output):
            if row.get('schema') != SCHEMA:
                raise ValueError('Old action-level data cannot be resumed as turn data')
            completed.add(row['root_id'])
            previous['leaves'] += len(row['leaves'])
            previous['exhaustive_turns'] += int(row['coverage']['exhaustive'])
            previous['demonstrations'] += int('demonstration' in row)
    report_path = args.report or args.output.with_suffix('.report.json')
    stats = {'schema': SCHEMA, 'sessions': len(sessions), 'turns': len(jobs),
             'completed_turns': len(completed), 'resumed_turns': len(completed), 'failed_turns': 0,
             **previous,
             'excluded_sessions': excluded, 'errors': [], 'success': False,
             'policy': 'all_engine_legal_actions_no_score_pruning',
             'limits': {'nodes': args.max_nodes, 'seconds': args.max_seconds, 'actions': args.max_actions}}

    def publish():
        report_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = report_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(report_path)

    def collect(session, rows):
        def engine(row):
            snapshot_id, snapshot, _ = _load_snapshot(session, row)
            return OfflineReplay(CliConfig(Path.cwd(), dll_relpath=args.dll),
                CombatSpec(snapshot['CharacterName'], 'ImportedSnapshot', str(snapshot['Seed']),
                           ascension=int(snapshot['AscensionLevel']), lang='en'),
                root_snapshot_id=snapshot_id, root_snapshot_json=json.dumps(snapshot),
                symmetry_dedup=False, state_dedup=False)
        scorer = CombatScoring(stage_for_floor(((rows[0].get('observation_before') or {}).get('run') or {}).get('floor')))
        builder = engine(rows[0])
        try:
            root, leaves, coverage = enumerate_turn(builder, scorer, max_nodes=args.max_nodes,
                max_seconds=args.max_seconds, max_actions=args.max_actions)
        finally:
            builder.close()
        result = {'schema': SCHEMA, 'root_id': rows[0]['decision_id'],
                  'session_id': session.name, 'scorer': scorer.identity,
                  'root_snapshot': rows[0].get('authoritative_snapshot'),
                  'root_scope': 'first_captured_decision_of_turn',
                  'root_state': project_model_state(root),
                  'leaves': leaves, 'coverage': coverage}
        try:
            if rows[-1]['action']['type'] != 'end_turn':
                raise ValueError('No human end_turn snapshot; terminal/card-choice turns retained without labels')
            end = engine(rows[-1])
            try:
                result['demonstration'] = demonstration_leaf(root, rows, end, scorer)
            finally:
                end.close()
        except Exception as exc:
            result['demonstration_error'] = str(exc)
        return result

    publish()
    with args.output.open('w' if args.overwrite else 'a', encoding='utf-8') as out:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(collect, session, rows): rows[0]['decision_id']
                       for session, rows in jobs if rows[0]['decision_id'] not in completed}
            for future in as_completed(futures):
                try:
                    turn = future.result()
                    out.write(json.dumps(turn, ensure_ascii=False) + '\n')
                    out.flush()
                    stats['completed_turns'] += 1
                    stats['leaves'] += len(turn['leaves'])
                    stats['exhaustive_turns'] += int(turn['coverage']['exhaustive'])
                    stats['demonstrations'] += int('demonstration' in turn)
                except Exception as exc:
                    stats['failed_turns'] += 1
                    stats['errors'].append({'root': futures[future], 'error': str(exc)})
                publish()
                print(json.dumps({k: stats[k] for k in ['turns', 'completed_turns', 'failed_turns', 'leaves', 'exhaustive_turns', 'demonstrations']}), flush=True)
    stats['success'] = bool((stats['completed_turns'] or completed) and not stats['failed_turns'])
    publish()
    return stats
