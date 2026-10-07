"""Frozen evidence, editable labels, offline scoring and snapshot search.

Never connects to the visible client. Historical replay is only needed once
to recover candidate leaves absent from old logs; later checks need only JSON.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / 'data' / 'combat_regressions'


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_json(path: Path, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding='utf-8', newline='\n')
    temporary.replace(path)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_id(value: str) -> str:
    if not re.fullmatch(r'[a-z][a-z0-9_-]*', value):
        raise ValueError('Case ID must use lowercase ASCII letters, digits, _ or -')
    return value


def import_case(report_path: Path, sequence: int, case_dir: Path,
                preferred_card: str | None = None, note: str = '') -> None:
    if case_dir.exists():
        raise ValueError(f'Refusing to overwrite existing case: {case_dir.name}')
    report = read_json(report_path)
    if report.get('resume_report') or report.get('room_replay') or report.get('reanchors'):
        raise ValueError('Replay chains/reanchors require explicit flattening before import')
    selected = next(row for row in report['actions'] if row['sequence'] == sequence)
    anchor = report_path.parent / 'official_map_anchor.save'
    if not anchor.is_file():
        raise ValueError('Missing original official_map_anchor.save beside the report')
    expected_hash = (report.get('identity') or {}).get('anchor_sha256')
    if expected_hash and digest(anchor) != expected_hash:
        raise ValueError('Anchor SHA256 does not match recorded identity')
    prefix = []
    for row in report['actions']:
        if row['sequence'] >= sequence:
            break
        if row.get('status') != 'completed':
            raise ValueError(f'Unknown/incomplete preceding action: {row["sequence"]}')
        tx = row.get('transaction')
        if not isinstance(tx, dict):
            raise ValueError(f'Missing explicit transaction: {row["sequence"]}')
        prefix.append({'sequence': row['sequence'], 'shadow': tx.get('shadow')})
    evidence = {
        'schema_version': 1, 'id': case_dir.name,
        'source': {'session': report_path.parent.name, 'sequence': sequence,
                   'report_sha256': digest(report_path), 'anchor_sha256': digest(anchor),
                   'identity': report.get('identity'), 'status': report.get('status'),
                   'error': report.get('error'), 'original_runtime_hash': None},
        'config': report.get('config', {}), 'anchor_room': bool(report.get('anchor_room')),
        'replay_prefix': prefix, 'action': selected,
        'following_actions': [row for row in report['actions']
                              if sequence < row['sequence'] <= sequence + 4],
        'limitations': ['Original candidate leaves and combat snapshot were not logged.',
                       'Hydrated leaves use the recorded current runtime.',
                       'Root validation covers exported fields, not all hidden state.'],
    }
    label = {'schema_version': 1, 'review_status': 'proposed', 'author': 'codex',
             'rationale': note or 'Imported evidence; review before policy acceptance.',
             'kind': 'policy' if preferred_card else 'diagnostic',
             'acceptable_first_cards': [],
             'required_cards': [preferred_card.upper()] if preferred_card else [],
             'forbidden_cards': [], 'minimum_margin': 0.01, 'feature_expectations': {}}
    write_json(case_dir / 'evidence.json', evidence)
    (case_dir / 'anchor.save').write_bytes(anchor.read_bytes())
    write_json(case_dir / 'label.json', label)
    write_json(case_dir / 'integrity.json', {
        name: digest(case_dir / name) for name in ('evidence.json', 'anchor.save')})


def verify_integrity(case_dir: Path) -> None:
    manifest = read_json(case_dir / 'integrity.json')
    required = {'evidence.json'} | {name for name in ('replay.json', 'anchor.save')
                                   if (case_dir / name).exists()}
    if not required.issubset(manifest):
        raise ValueError('Frozen artifact is missing from integrity manifest')
    for name, expected in manifest.items():
        if Path(name).name != name or digest(case_dir / name) != expected:
            raise ValueError(f'Frozen artifact integrity mismatch: {case_dir.name}/{name}')


def validate_label(label: dict) -> None:
    if label.get('schema_version') != 1:
        raise ValueError('Unsupported label schema_version')
    if label.get('review_status') not in {'proposed', 'confirmed', 'rejected'}:
        raise ValueError('Unknown label review_status')
    if label.get('kind') not in {'policy', 'feature', 'diagnostic'}:
        raise ValueError('Unknown label kind')
    if not label.get('author') or not label.get('rationale'):
        raise ValueError('Labels need an author and rationale')
    if label['kind'] == 'policy':
        for key in ('required_cards', 'forbidden_cards', 'acceptable_first_cards'):
            values = label.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                raise ValueError(f'{key} must be a list of card IDs')
        margin = float(label.get('minimum_margin', 0))
        if not math.isfinite(margin) or margin <= 0:
            raise ValueError('minimum_margin must be positive: ties are not improvements')


def runtime_identity() -> dict:
    from cli.sts2_cli_adapter import CliConfig
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    binary = ROOT / CliConfig(ROOT).dll_relpath
    return {'git_head': revision, 'headless_sha256': digest(binary),
            'assembly_sha256': {str(path.relative_to(ROOT)): digest(path)
                                for path in sorted(binary.parent.glob('*.dll'))},
            'source_sha256': {name: digest(ROOT / name) for name in (
                'controller/search/combat_search.py', 'controller/search/evaluator.py',
                'third_party/sts2-cli/src/Sts2Headless/RunSimulator.cs')}}


def require_ok(value: dict, operation: str) -> dict:
    if value.get('type') == 'error' or value.get('success') is False:
        raise ValueError(f'{operation}: {value.get("message", value)}')
    return value


def hydrate_case(case_dir: Path) -> dict:
    from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
    from controller.engine_parity import (client_combat_checkpoint, headless_combat_checkpoint,
                                         compare_checkpoints)
    from controller.run_agent import resolve_live_planned_action
    from controller.search.actions import SearchAction
    from controller.search.combat_search import CombatSearcher
    from controller.search.evaluator import explain_leaf_score
    from controller.deck_profile import load_deck_profile

    verify_integrity(case_dir)
    if (case_dir / 'replay.json').exists():
        raise ValueError('Already hydrated; preserve baseline and use search/check instead')
    evidence = read_json(case_dir / 'evidence.json')
    row = evidence['action']
    if not (row.get('client_before') or {}).get('in_combat'):
        return {'id': case_dir.name, 'status': 'EVIDENCE_ONLY', 'reason': 'Non-combat evidence'}
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        state = require_ok(cli.load_save(str((case_dir / 'anchor.save').resolve()),
                                        resume_room=evidence['anchor_room']), 'load anchor')
        for step in evidence['replay_prefix']:
            command = step['shadow']
            if command:
                state = require_ok(cli.action(command['action'], command.get('params') or {},
                                              timeout_s=20), f'replay {step["sequence"]}')
        search_state = require_ok(cli.get_search_state(timeout_s=20), 'get root')['combat_state_for_search']
        parity = compare_checkpoints(
            client_combat_checkpoint(row['client_before']),
            headless_combat_checkpoint(state, search_state, row['client_before']['run_id']))
        if parity.status != 'PASS':
            return {'id': case_dir.name, 'status': 'REPLAY_MISMATCH', 'differences': parity.differences}
        require_ok(cli.capture_combat_snapshot('regression_root'), 'capture')
        snapshot = require_ok(cli.export_combat_snapshot('regression_root'), 'export')['snapshot_json']
        root_summary = CombatSearcher._combat_summary(search_state)
        coefficients = dict(load_deck_profile().get('combat_coefficients') or {})
        mode = evidence['config'].get('score_mode', 'balanced')
        candidates = []
        for index, recorded in enumerate(row.get('decision_telemetry', {}).get('root_candidates') or []):
            require_ok(cli.restore_combat_snapshot('regression_root'), 'restore')
            current = require_ok(cli.get_search_state(timeout_s=20), 'get restored')['combat_state_for_search']
            policy_state = current
            terminal = None
            invalid = None
            for item in recorded['line']:
                resolved = resolve_live_planned_action(current, SearchAction(**item))
                if resolved is None:
                    invalid = {'reason': 'recorded_action_not_legal', 'action': item,
                               'state': current}
                    break
                result = require_ok(cli.action(*resolved, timeout_s=20), 'replay candidate')
                if result.get('decision') != 'combat_play':
                    terminal = result
                    break
                current = require_ok(cli.get_search_state(timeout_s=20), 'candidate state')['combat_state_for_search']
                policy_state = CombatSearcher._replace_enemy_intents_from_parent(policy_state, current)
            if terminal is not None:
                invalid = {'reason': 'terminal_not_materialized', 'state': terminal}
            if invalid:
                candidates.append({'id': f'candidate_{index}', 'line': recorded['line'],
                                   'historical_score': recorded['score'], 'replay_error': invalid})
                continue
            settled = bool(recorded['line'] and recorded['line'][-1]['action_type'] == 'end_turn')
            candidates.append({'id': f'candidate_{index}', 'line': recorded['line'],
                               'historical_score': recorded['score'], 'leaf_state': policy_state,
                               'engine_leaf_state': current,
                               'settled': settled,
                               'baseline_explanation': explain_leaf_score(policy_state, root_summary, mode, coefficients)})
        replay = {'schema_version': 1, 'origin': 'offline_reconstruction',
                  'runtime': runtime_identity(), 'root_checkpoint': asdict(parity),
                  'root_state': search_state, 'root_summary': root_summary,
                  'headless_root': state,
                  'score_mode': mode, 'coefficients': coefficients,
                  'coefficient_origin': 'profile frozen at hydration, not claimed historical',
                  'snapshot_json': snapshot, 'candidates': candidates,
                  'leaf_semantics': 'leaf_state follows current search semantics; engine_leaf_state is unmodified',
                  'candidate_scope': 'recorded top-k only; not all possible plans'}
        write_json(case_dir / 'replay.json', replay)
        integrity = read_json(case_dir / 'integrity.json')
        integrity['replay.json'] = digest(case_dir / 'replay.json')
        write_json(case_dir / 'integrity.json', integrity)
        return {'id': case_dir.name, 'status': 'HYDRATED', 'candidates': len(candidates),
                'invalid_candidates': sum('replay_error' in item for item in candidates)}
    finally:
        cli.stop()


def matches_label(line: list[dict], label: dict) -> bool:
    cards = [(item.get('metadata') or {}).get('card_id') for item in line]
    first = label.get('acceptable_first_cards') or []
    return (not first or bool(cards) and cards[0] in first) and all(
        card in cards for card in label.get('required_cards', [])
    ) and not any(card in cards for card in label.get('forbidden_cards', []))


def check_case(case_dir: Path, coefficients: dict | None = None, leaf_source: str = 'engine',
               score_mode: str = 'preference') -> dict:
    from controller.search.evaluator import explain_leaf_score
    verify_integrity(case_dir)
    label = read_json(case_dir / 'label.json')
    validate_label(label)
    result = {'id': case_dir.name, 'review_status': label['review_status']}
    if label['review_status'] == 'rejected' or label['kind'] == 'diagnostic':
        return {**result, 'status': 'DIAGNOSTIC', 'reason': 'No policy success claim'}
    path = case_dir / 'replay.json'
    if not path.exists():
        return {**result, 'status': 'BLOCKED', 'reason': 'No frozen evaluator inputs'}
    replay = read_json(path)
    rows = []
    for item in replay['candidates']:
        if item.get('replay_error'):
            continue
        leaf_key = 'engine_leaf_state' if leaf_source == 'engine' and 'engine_leaf_state' in item else 'leaf_state'
        if score_mode == 'preference' and label['kind'] != 'feature':
            from controller.combat_scoring import CombatScoring, stage_for_floor
            context = replay.get('headless_root', {}).get('context') or {}
            scorer = CombatScoring(stage_for_floor(context.get('floor')))
            if coefficients is not None:
                raise ValueError('Legacy coefficient overrides require --score-mode legacy')
            trace = [{'action': action, 'before': {}} for action in item['line']]
            explanation = scorer.explain(replay.get('root_state') or {'combat': replay['root_summary']},
                                         item[leaf_key], trace)
        else:
            explanation = explain_leaf_score(item[leaf_key], replay['root_summary'],
                                             replay['score_mode'], coefficients if coefficients is not None
                                             else replay['coefficients'])
        if not math.isfinite(explanation['total']):
            raise ValueError('Non-finite evaluator score')
        rows.append({'id': item['id'], 'score': explanation['total'], 'line': item['line'],
                     'leaf_source': leaf_key,
                     'settled': item['settled'], 'acceptable': matches_label(item['line'], label),
                     'historical_score': item.get('historical_score'),
                     'baseline_score': item['baseline_explanation']['total'], 'explanation': explanation})
    if label['kind'] == 'feature':
        if not rows or not label.get('feature_expectations'):
            raise ValueError('Feature labels need nonempty inputs and expectations')
        failures = []
        for row in rows:
            for key, expected in label['feature_expectations'].items():
                actual = row['explanation'].get('features', {}).get(key)
                if actual is None or not math.isclose(actual, expected, abs_tol=1e-8):
                    failures.append({'candidate': row['id'], 'feature': key, 'expected': expected, 'actual': actual})
        return {**result, 'status': 'FAIL' if failures else 'PASS', 'failures': failures, 'candidates': rows}
    if not any(label.get(key) for key in ('required_cards', 'forbidden_cards', 'acceptable_first_cards')):
        raise ValueError('Policy labels require at least one expectation')
    if any(item.get('replay_error') for item in replay['candidates']):
        return {**result, 'status': 'BLOCKED', 'reason': 'Candidate replay failed; ranking would be incomplete',
                'candidates': rows}
    eligible = [row for row in rows if row['settled']]
    good = [row['score'] for row in eligible if row['acceptable']]
    bad = [row['score'] for row in eligible if not row['acceptable']]
    if not good or not bad:
        return {**result, 'status': 'BLOCKED', 'reason': 'Need settled positive and negative candidates',
                'candidates': rows}
    margin = max(good) - max(bad)
    return {**result, 'status': 'PASS' if margin >= float(label['minimum_margin']) else 'FAIL',
            'margin': margin, 'candidates': rows, 'scope': 'frozen candidate ranking, not fresh search'}


def synthetic_case(case_dir: Path) -> None:
    from controller.search.evaluator import explain_leaf_score
    if case_dir.exists():
        raise ValueError('Synthetic case already exists')
    root = {'player': {'hp': 50, 'max_hp': 80, 'energy': 3, 'powers': []},
            'hand': [], 'draw_pile': [], 'discard_pile': [], 'enemies': [
                {'monster_id': 'TEST', 'hp': 50, 'max_hp': 50, 'powers': [],
                 'intent': {'intent_types': ['Attack'], 'display_damage': 8, 'hits': 1}}]}
    leaf = copy.deepcopy(root)
    leaf['enemies'][0]['intent'] = {'intent_types': ['Sleep']}
    state = {'success': True, 'combat': leaf}
    write_json(case_dir / 'evidence.json', {'schema_version': 1, 'id': case_dir.name,
        'source': {'kind': 'synthetic'}, 'description': 'Intent changes, all HP values stay constant.'})
    write_json(case_dir / 'label.json', {'schema_version': 1, 'kind': 'feature',
        'author': 'codex', 'review_status': 'proposed',
        'rationale': 'Damage features must not count intent changes as HP loss.',
        'feature_expectations': {'enemy_hp_loss': 0.0, 'weighted_enemy_hp_loss': 0.0,
                                 'focused_enemy_hp_loss': 0.0}})
    write_json(case_dir / 'replay.json', {'schema_version': 1, 'origin': 'synthetic',
        'root_summary': root, 'score_mode': 'balanced', 'coefficients': {}, 'candidates': [
            {'id': 'intent_only', 'line': [], 'settled': True, 'leaf_state': state,
             'baseline_explanation': explain_leaf_score(state, root, 'balanced')}]})
    write_json(case_dir / 'integrity.json', {name: digest(case_dir / name)
                                          for name in ('evidence.json', 'replay.json')})


def verify_search_leaf(replay: dict, line: list[dict], predicted_leaf: dict) -> dict:
    """Replay the chosen plan on a separate engine, outside benchmark timing."""
    from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
    from controller.run_agent import resolve_live_planned_action
    from controller.search.actions import SearchAction
    from controller.search.combat_search import CombatSearcher, CombatSpec, RecordedAction
    from controller.search.state_cache import canonicalize_search_state_for_plan_reuse, diff_plan_reuse_states
    cli = Sts2CliAdapter(CliConfig(ROOT))
    builder = CombatSearcher(CliConfig(ROOT), CombatSpec('Ironclad', 'VERIFY', 'frozen'))
    builder._root_summary = replay['root_summary']
    try:
        cli.start()
        require_ok(cli.import_combat_snapshot(replay['snapshot_json'], 'verify_root'), 'import verification root')
        require_ok(cli.restore_combat_snapshot('verify_root'), 'restore verification root')
        actual = require_ok(cli.get_search_state(timeout_s=20), 'verification root')['combat_state_for_search']
        for index, item in enumerate(line):
            resolved = resolve_live_planned_action(actual, SearchAction(**item))
            if resolved is None:
                return {'status': 'FAIL', 'reason': 'illegal_action', 'index': index, 'action': item}
            response = require_ok(cli.action(*resolved, timeout_s=20), 'verify action')
            if response.get('decision') != 'combat_play':
                if index != len(line) - 1:
                    return {'status': 'FAIL', 'reason': 'early_terminal', 'index': index}
                actual = builder._build_terminal_search_state(response, RecordedAction(*resolved))
            else:
                actual = require_ok(cli.get_search_state(timeout_s=20), 'verification state')['combat_state_for_search']
        differences = diff_plan_reuse_states(canonicalize_search_state_for_plan_reuse(predicted_leaf),
                                            canonicalize_search_state_for_plan_reuse(actual))
        return {'status': 'FAIL' if differences else 'PASS', 'differences': differences,
                'scope': 'semantic_exported_state', 'actual_leaf': actual}
    except (RuntimeError, ValueError, TimeoutError) as exc:
        return {'status': 'ERROR', 'reason': str(exc)}
    finally:
        cli.stop()
        builder.close()


def search_case(case_dir: Path, workers: int, budget_ms: float, verify_leaf: bool = False,
                score_mode: str = 'preference') -> dict:
    from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
    from controller.combat_step import CombatStepConfig, PlanState, decide_combat_action
    from controller.search.combat_search import CombatWorkerPool, CombatSpec
    from controller.search.state_cache import (canonicalize_search_state_for_plan_reuse,
                                               diff_plan_reuse_states)
    from controller.search.worker_scaling import CpuDecisionMonitor
    verify_integrity(case_dir)
    replay = read_json(case_dir / 'replay.json')
    if not replay.get('snapshot_json'):
        raise ValueError('This case has no engine snapshot')
    source = read_json(case_dir / 'evidence.json')
    identity = runtime_identity()
    runtime_changed = (identity['headless_sha256'] != replay['runtime']['headless_sha256']
                       or identity['assembly_sha256'] != replay['runtime']['assembly_sha256'])
    if runtime_changed and not verify_leaf:
        raise ValueError('Runtime changed; use --verify-leaf to check the root and independently replay the plan')
    label = read_json(case_dir / 'label.json')
    validate_label(label)
    if workers < 1 or not math.isfinite(budget_ms) or budget_ms < 0:
        raise ValueError('workers must be positive and budget-ms nonnegative')
    cli_cfg = CliConfig(ROOT)
    cli = Sts2CliAdapter(cli_cfg)
    pool = CombatWorkerPool(cli_cfg)
    try:
        cli.start()
        require_ok(cli.import_combat_snapshot(replay['snapshot_json'], 'regression_root'), 'import root')
        require_ok(cli.restore_combat_snapshot('regression_root'), 'restore root')
        state = require_ok(cli.get_search_state(timeout_s=20), 'search root')['combat_state_for_search']
        differences = diff_plan_reuse_states(canonicalize_search_state_for_plan_reuse(replay['root_state']),
                                            canonicalize_search_state_for_plan_reuse(state))
        if differences:
            raise ValueError(f'Frozen root restore mismatch: {differences}')
        warm_started = time.perf_counter()
        pool.prewarm(workers)
        warm_ms = (time.perf_counter() - warm_started) * 1000
        context = replay.get('headless_root', {}).get('context') or {}
        snapshot = json.loads(replay['snapshot_json'])
        cfg = CombatStepConfig(cli_cfg=cli_cfg,
            spec=CombatSpec(snapshot.get('CharacterName', 'Ironclad'), 'REGRESSION',
                            snapshot.get('Seed', 'frozen'), int(snapshot.get('AscensionLevel', 0))),
            depth=int(source['config'].get('depth', 12)),
            chance_depth=int(source['config'].get('chance_depth', 1)),
            score_mode=replay['score_mode'] if score_mode == 'legacy' else 'preference', evaluator_coefficients=replay['coefficients'],
            max_search_ms=budget_ms, max_workers=workers, user_parallel=workers > 1,
            reuse_cli_processes=True, worker_pool=pool, capture_root_topk=20,
            floor=context.get('floor'), room_type=context.get('room_type'))
        plan = PlanState()
        monitor = CpuDecisionMonitor()
        monitor.start()
        started = time.perf_counter()
        try:
            answer = decide_combat_action(cli, state, cfg, plan)
        finally:
            cpu = monitor.finish()
        decision_ms = (time.perf_counter() - started) * 1000
        line = [asdict(action) for action in ([answer.chosen] if answer.chosen else []) + plan.sequence]
        complete = bool(line and line[-1]['action_type'] == 'end_turn') or bool(
            (answer.leaf_state or {}).get('terminal_decision') in
            {'card_reward', 'map_select', 'victory', 'game_over', 'defeat'})
        valid = not answer.fell_back and not answer.search_failed and complete
        verification = verify_search_leaf(replay, line, answer.leaf_state) if verify_leaf and valid else None
        if verification is not None and verification['status'] != 'PASS':
            valid = False
        return {'id': case_dir.name, 'scope': 'fresh frozen-snapshot search',
                'review_status': label['review_status'],
                'matches_label': valid and matches_label(line, label) if label['kind'] == 'policy' else None,
                'comparable_plan': valid,
                'line': line,
                'score': answer.search_score if answer.search_score is not None and math.isfinite(answer.search_score) else None,
                'stats': answer.decision_audit,
                'leaf_state': answer.leaf_state, 'leaf_verification': verification,
                'timing': answer.searcher_timing_summary, 'runtime': identity,
                'runtime_changed': runtime_changed,
                'workers': workers, 'budget_ms': budget_ms, 'cpu': cpu,
                'prewarm_ms': warm_ms, 'decision_ms': decision_ms,
                'fell_back': answer.fell_back, 'search_failed': answer.search_failed,
                'root_candidates': answer.root_candidates}
    finally:
        pool.close()
        cli.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=DEFAULT_CORPUS)
    sub = parser.add_subparsers(dest='command', required=True)
    add = sub.add_parser('import')
    add.add_argument('--report', required=True, type=Path)
    add.add_argument('--sequence', required=True, type=int)
    add.add_argument('--id', required=True, type=checked_id)
    add.add_argument('--prefer-card')
    add.add_argument('--note', default='')
    sub.add_parser('seed-feature')
    hydrate = sub.add_parser('hydrate')
    hydrate.add_argument('--id', required=True, type=checked_id)
    search = sub.add_parser('search')
    search.add_argument('--id', required=True, type=checked_id)
    search.add_argument('--workers', type=int, default=1)
    search.add_argument('--budget-ms', type=float, default=5000)
    search.add_argument('--output', type=Path, required=True)
    search.add_argument('--verify-leaf', action='store_true')
    search.add_argument('--score-mode', choices=('preference', 'legacy'), default='preference')
    check = sub.add_parser('check')
    check.add_argument('--id', type=checked_id)
    check.add_argument('--output', type=Path)
    check.add_argument('--coefficients', type=Path)
    check.add_argument('--score-mode', choices=('preference', 'legacy'), default='preference')
    check.add_argument('--leaf-source', choices=('engine', 'historical'), default='engine')
    check.add_argument('--strict', action='store_true', help='Exit 1 for FAIL or BLOCKED labels')
    args = parser.parse_args()
    if args.command == 'import':
        import_case(args.report, args.sequence, args.corpus / args.id, args.prefer_card, args.note)
        print(f'Imported {args.id}')
    elif args.command == 'seed-feature':
        synthetic_case(args.corpus / 'intent_is_not_damage')
    elif args.command == 'hydrate':
        result = hydrate_case(args.corpus / args.id)
        print(json.dumps(result, ensure_ascii=True))
        if result['status'] == 'REPLAY_MISMATCH':
            raise SystemExit(2)
    elif args.command == 'search':
        result = search_case(args.corpus / args.id, args.workers, args.budget_ms, args.verify_leaf, args.score_mode)
        write_json(args.output, result)
        print(json.dumps({**{key: result[key] for key in ('id', 'matches_label', 'score')},
                          'leaf_verification': (result.get('leaf_verification') or {}).get('status')}, ensure_ascii=True))
    else:
        paths = [args.corpus / args.id] if args.id else sorted(
            path.parent for path in args.corpus.glob('*/label.json'))
        if not paths:
            raise ValueError('No cases found')
        overrides = read_json(args.coefficients) if args.coefficients else None
        results = [check_case(path, overrides, args.leaf_source, args.score_mode) for path in paths]
        for result in results:
            print(f'{result["status"]:10} {result["id"]} [{result["review_status"]}] '
                  f'{result.get("margin", result.get("reason", ""))}')
        if args.output:
            write_json(args.output, {'schema_version': 1, 'results': results})
        if args.strict and any(item['status'] in {'FAIL', 'BLOCKED'} for item in results):
            raise SystemExit(1)


if __name__ == '__main__':
    main()
