from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List
from types import SimpleNamespace

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from cli.sts2_mod_adapter import ModClientConfig, Sts2ModAdapter
from controller.action_transaction import make_transaction, transaction_from_dict
from controller.combat_step import (
    CombatStepConfig,
    DEFAULT_LIVE_SEARCH_BUDGET_MS,
    DEFAULT_TURN_ACTION_CAP,
    PlanState,
    decide_combat_action,
)
from controller.engine_parity import (
    client_combat_checkpoint,
    client_map_checkpoint,
    compare_checkpoints,
    headless_combat_checkpoint,
    headless_map_checkpoint,
    client_reward_checkpoint,
    headless_reward_checkpoint,
    is_card_reward_selection,
)
from controller.live_client_bridge import (
    JsonlSessionLog,
    LiveClientBridge,
    StaleClientStateError,
    client_state_digest,
    event_resolution_equivalent,
    gameplay_observation,
)
from controller.live_session import ControlledStop, ManagedControl, SessionJournal, ThrottledReportWriter, recover_report_tail, write_json, RunDefeat
from controller.run_agent import choose_card_reward, choose_map_node, choose_map_route_global
from controller.search.combat_search import CombatSpec
from controller.search.worker_scaling import CpuDecisionMonitor, recommended_hardware_workers
from controller.deck_profile import load_deck_profile
from controller.interaction_flow import FlowBlocked, InteractionFlow
from controller.transaction_runtime import execute_transaction
from controller.combat_snapshot import CombatValidationSet
from controller.snapshot_evidence import seal_evidence


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Continue a visible client run while a persistent headless shadow verifies it"
    )
    parser.add_argument("--anchor-map-save", type=Path, required=True)
    parser.add_argument("--bootstrap-combat-report", type=Path)
    parser.add_argument("--bootstrap-reward-report", type=Path)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--target-combats", type=int, default=0, help="0 runs until game over")
    parser.add_argument("--completed-combats", type=int, default=0)
    parser.add_argument("--depth", type=int, default=DEFAULT_TURN_ACTION_CAP,
                        help="Maximum player actions expanded before the first enemy turn")
    parser.add_argument("--chance-depth", type=int, default=1)
    parser.add_argument("--max-search-ms", type=float, default=DEFAULT_LIVE_SEARCH_BUDGET_MS)
    parser.add_argument("--max-actions-per-combat", type=int, default=120)
    parser.add_argument("--visible-delay-ms", type=float, default=700.0)
    parser.add_argument("--control-file", type=Path)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--resume-report", type=Path)
    parser.add_argument('--recover-pending-selection', action='store_true')
    parser.add_argument('--verify-checkpoints', action='store_true', help='Optional legacy parity diagnostics')
    parser.add_argument('--anchor-room', action='store_true', help='Resume the room using the official load lifecycle')
    parser.add_argument('--replay-room-report', type=Path)
    parser.add_argument('--max-workers', type=int, default=recommended_hardware_workers())
    parser.add_argument('--worker-mode', choices=('fixed', 'adaptive'), default='adaptive')
    parser.add_argument('--deck-profile', type=Path)
    parser.add_argument('--scoring-model', type=Path)
    parser.add_argument('--scoring-model-id', default='active')
    parser.add_argument('--disable-worker-pool', action='store_true')
    args = parser.parse_args()
    if args.resume_report:
        source_metadata = json.loads(args.resume_report.read_text(encoding='utf-8'))
        args.anchor_room = bool(source_metadata.get('anchor_room', args.anchor_room))

    repo_root = Path(__file__).resolve().parents[1]
    from controller.combat_scoring import active_model, validate_model
    scoring_model = (json.loads(args.scoring_model.read_text(encoding='utf-8'))
                     if args.scoring_model else active_model())
    validate_model(scoring_model)
    args.scoring_model_payload = scoring_model
    args.session_dir.mkdir(parents=True, exist_ok=True)
    log = JsonlSessionLog(args.session_dir / "session.jsonl", compact_lifecycle=True)
    mod = Sts2ModAdapter(ModClientConfig(base_url=args.url))
    live = LiveClientBridge(mod, log, args.visible_delay_ms, rng_audit_required=True)
    cli_cfg = CliConfig(repo_root=repo_root)
    control = ManagedControl(args.control_file, args.status_file)
    cli = Sts2CliAdapter(cli_cfg)
    report: Dict[str, Any] = {
        "schema_version": 3,
        "transaction_schema_version": 1,
        "verification_enabled": args.verify_checkpoints,
        "created_at": time.time(),
        "anchor_save": str(args.anchor_map_save.resolve()),
        "anchor_room": args.anchor_room,
        "config": {"combat_policy": "bounded_combat_search", "draft_policy": "heuristic",
                   "depth": args.depth, "chance_depth": args.chance_depth,
                   "search_budget_ms": args.max_search_ms, "visible_delay_ms": args.visible_delay_ms,
                   "budget_type": "soft", "score_mode": "preference"},
        "verification_scope": "RNG_LOCKSTEP_AND_COVERED_FIELDS",
        "unverified_fields": ["progression_hash", "full_piles", "transient_object_identity"],
        "rng_bridge": None,
        "status": "RUNNING",
        "target_combat_count": args.target_combats,
        "bootstrap_combat_count": args.completed_combats,
        "combats": [],
        "map_choices": [],
        "rewards": [],
        "parity_checkpoints": [],
        "client_only_segments": [],
        "reanchors": [],
        "completed_combat_count": args.completed_combats,
    }
    report['config']['max_workers'] = max(1, int(args.max_workers))
    report['config']['worker_pool_enabled'] = not args.disable_worker_pool
    report['config']['worker_mode'] = args.worker_mode
    report['config']['deck_profile'] = str(args.deck_profile.resolve()) if args.deck_profile else 'default'
    report['config']['scoring_model_id'] = args.scoring_model_id
    report['config']['scoring_model'] = scoring_model
    report_writer = ThrottledReportWriter(args.session_dir, report, log.path, _write_report)
    save_report = report_writer.save
    args.save_report = save_report
    journal = SessionJournal(report, save_report, control)
    log.on_event = journal.record
    live.before_action = control.before_action
    live.verify_checkpoints = args.verify_checkpoints
    live.on_wait = lambda fields: control.publish('running', **fields)
    try:
        save_report(force=True)
        control.publish("running", phase="bootstrap")
        runtime_path = (repo_root / cli_cfg.dll_relpath).resolve()
        runtime_sha_before = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
        cli.start()
        cli.require_capabilities({"exact_save_json", "native_reward_items_v1"})
        runtime_sha_after = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
        if runtime_sha_after != runtime_sha_before:
            raise RuntimeError('Headless runtime changed while starting; restart with a stable build')
        report['headless_runtime'] = {
            'path': str(runtime_path),
            'sha256': runtime_sha_after,
            'protocol_capabilities': sorted(cli.protocol_capabilities),
        }
        log.write({'event': 'headless_runtime_loaded', **report['headless_runtime']})
        save_report()
        mod_health = mod.health()
        capture_health = mod.capture_health()
        if capture_health.get('reward_contract') != 'native-reward-items-v1':
            raise RuntimeError('Capture Mod lacks native reward identities; rebuild/install it and restart the game')
        if capture_health.get('potion_target_contract') != 'native-no-creature-v1':
            raise RuntimeError('Client Mod lacks native-no-creature-v1 potion target compatibility; install STS2HumanCapture 0.4.1 and restart the game')
        rng_health = mod.rng_health()
        rng_identity = mod.rng_identity()
        client_game_hash = ((rng_identity.get('game_assembly') or {}).get('sha256'))
        original_game_dll = repo_root / 'third_party/sts2-cli/lib/sts2.dll.original'
        if not original_game_dll.is_file():
            raise RuntimeError(f'Missing unpatched game assembly for identity check: {original_game_dll}')
        headless_source_hash = hashlib.sha256(original_game_dll.read_bytes()).hexdigest()
        if client_game_hash != headless_source_hash:
            raise RuntimeError(
                f'Client/headless source assembly mismatch: client={client_game_hash}, '
                f'headless_source={headless_source_hash}'
            )
        report['rng_bridge'] = {
            'health': rng_health,
            'identity': rng_identity,
            'headless_source_sha256': headless_source_hash,
        }
        report['capture_health'] = capture_health
        headless_state = _bootstrap_shadow(cli, args, log)
        client_state = live.observe()
        report["identity"] = {"run_id": client_state.get("run_id"),
                              "character": (client_state.get("run") or {}).get("character_id"),
                              "ascension": (client_state.get("run") or {}).get("ascension"),
                              "game_version": mod_health.get("game_version"),
                              "game_assembly_sha256": client_game_hash,
                              "anchor_sha256": hashlib.sha256(args.anchor_map_save.read_bytes()).hexdigest()}
        if args.resume_report:
            headless_state, completed = _resume_shadow(
                cli, args, client_state, report, log,
                bootstrap_state=headless_state,
                live=live,
            )
            report['completed_combat_count'] = completed
        if args.replay_room_report:
            source = json.loads(args.replay_room_report.read_text(encoding='utf-8'))
            if (source.get('identity') or {}).get('run_id') != client_state.get('run_id'):
                raise FlowBlocked('Room replay belongs to a different client run')
            completed_rows = [r for r in source.get('actions') or [] if r.get('status') == 'completed']
            if not completed_rows or gameplay_observation(completed_rows[-1].get('client_after') or {}) != gameplay_observation(client_state):
                raise FlowBlocked('Client changed after the source room trace')
            floor = (client_state.get('run') or {}).get('floor')
            replayed = []
            for row in completed_rows:
                before = row.get('client_before') or {}
                if not before.get('in_combat') or (before.get('run') or {}).get('floor') != floor:
                    continue
                command = row.get('client_action')
                payload = dict(row.get('client_params') or {})
                if command not in {'play_card', 'end_turn', 'use_potion'}:
                    raise FlowBlocked('Room recovery requires an explicit selection replay mapping')
                if payload.get('target_index') is not None:
                    living = [e for e in (before.get('combat') or {}).get('enemies') or [] if e.get('is_alive')]
                    matches = [i for i,e in enumerate(living) if e.get('index') == payload['target_index']]
                    if len(matches) != 1:
                        raise FlowBlocked('Cannot translate historical client target')
                    payload['target_index'] = matches[0]
                if command == 'use_potion':
                    payload['potion_index'] = payload.pop('option_index')
                headless_state = cli.action(command, payload, timeout_s=20)
                _raise_on_headless_error(headless_state, 'replay actual room input')
                replayed.append({
                    'transaction': make_transaction(
                        None,
                        shadow_action=command,
                        shadow_args=payload,
                        completion='historical_room_replay',
                    ).to_dict()
                })
            report['room_replay'] = replayed
            report['room_replay_source'] = str(args.replay_room_report.resolve())
            report['completed_combat_count'] = int(source.get('completed_combat_count') or 0)
        from controller.rng_parity import compare_rng_snapshots
        client_rng_root = mod.rng_snapshot()
        shadow_rng_root = cli.get_rng_snapshot()
        root_rng_parity = compare_rng_snapshots(client_rng_root, shadow_rng_root)
        report['root_rng_parity'] = {
            'status': root_rng_parity.status,
            'differences': root_rng_parity.differences,
            'client_digest': client_rng_root.get('digest_sha256'),
            'shadow_digest': shadow_rng_root.get('digest_sha256'),
        }
        log.write({'event': 'root_rng_parity', **report['root_rng_parity']})
        save_report(force=True)
        if not root_rng_parity.passed:
            raise FlowBlocked(
                f"Root RNG parity failed before agent input: {root_rng_parity.differences[:3]}"
            )

        from controller.live_flow_runner import run_interactions
        run_interactions(cli, cli_cfg, live, client_state, headless_state, args,
                         repo_root, log, report, control)
        save_report(force=True)
        print(json.dumps({'status': report['status'], 'completed_combat_count': report['completed_combat_count']},
                         ensure_ascii=False), flush=True)
    except FlowBlocked as exc:
        report['status'] = 'BLOCKED'
        report['error'] = str(exc)
        control.publish('blocked', phase='blocked', error=str(exc), pending_action=None)
    except RunDefeat as exc:
        report['status'] = 'DEFEAT'
        report['reason'] = str(exc)
        control.publish('completed', phase='defeat', outcome='DEFEAT', pending_action=None)
    except ControlledStop as exc:
        report["status"] = "STOPPED"
        report["reason"] = str(exc)
        save_report(force=True)
        control.publish("stopped", phase="safe_stop", reason=str(exc), pending_action=None)
    except Exception as exc:
        report["status"] = "FAIL"
        report["error"] = str(exc)
        save_report(force=True)
        control.publish("error", phase="failed", error=str(exc), pending_action=None)
        raise
    finally:
        for combat in report["combats"]:
            if combat.get("status") == "RUNNING":
                combat["status"] = report["status"]
        report["pending_action"] = None
        report["finished_at"] = time.time()
        try:
            save_report(force=True)
        finally:
            cli.stop()


def _resume_shadow(cli, args, client_state, report, log, bootstrap_state=None, live=None):
    source = recover_report_tail(
        json.loads(args.resume_report.read_text(encoding='utf-8')),
        args.resume_report.parent / 'session.jsonl',
    )
    identity = source.get('identity') or {}
    if identity.get('run_id') != client_state.get('run_id'):
        raise RuntimeError('Cannot resume: client run identity changed')
    if identity.get('anchor_sha256') != hashlib.sha256(args.anchor_map_save.read_bytes()).hexdigest():
        raise RuntimeError('Cannot resume: anchor hash differs')
    completed_actions = [row for row in source.get('actions') or [] if row.get('status') == 'completed']
    unknown = [row for row in source.get('actions') or [] if row.get('status') in {'executing', 'outcome_unknown'}]
    paused = [row for row in source.get('actions') or [] if row.get('status') == 'awaiting_input']
    if len(paused) > 1 or (paused and paused[0] is not source['actions'][-1]):
        raise RuntimeError('Cannot resume: native player-choice pause is not the latest action')
    native_parent = paused[0] if paused else None
    if native_parent is not None:
        if (unknown or live is None
                or native_parent.get('transaction_status') != 'both_awaiting_input'
                or native_parent.get('shadow_action_applied') is not True
                or native_parent.get('native_action_phase') != 'awaiting_input'
                or not client_state.get('in_combat')
                or client_state.get('screen') != 'CARD_SELECTION'
                or gameplay_observation(native_parent.get('client_after') or {})
                != gameplay_observation(client_state)):
            raise RuntimeError('Cannot resume: pending native choice lacks a verified client boundary')
        native = live.mod.action_lifecycle()
        matches = [item for item in native.get('actions') or []
                   if item.get('id') == native_parent.get('native_action_id')
                   and item.get('semantic_action') == 'play_card']
        before = native_parent.get('client_before') or {}
        selected_index = (native_parent.get('client_params') or {}).get('card_index')
        selected = [card for card in (before.get('combat') or {}).get('hand') or []
                    if card.get('index') == selected_index]
        if (native.get('epoch') != native_parent.get('native_action_epoch')
                or native.get('revision') != native_parent.get('native_end_revision')
                or len(matches) != 1 or len(selected) != 1
                or matches[0].get('card_id') != selected[0].get('card_id')
                or matches[0].get('status') != 'awaiting_input'
                or matches[0].get('pause_type') != 'player_choice'):
            raise RuntimeError('Cannot resume: native action identity or pause changed')
        from controller.rng_parity import compare_rng_snapshots
        expected_rng = native_parent.get('client_rng_after')
        if not isinstance(expected_rng, dict) or not compare_rng_snapshots(
                expected_rng, live.mod.rng_snapshot()).passed:
            raise RuntimeError('Cannot resume: pending native choice RNG changed')
    unknown_shadow = _explicit_shadow_command(unknown[0]) if len(unknown) == 1 else None
    recover_selection = native_parent is not None or (
                         getattr(args, 'recover_pending_selection', False) and len(unknown) == 1
                         and unknown[0] is source['actions'][-1] and unknown_shadow is not None
                         and unknown_shadow[0] == 'play_card'
                         and client_state.get('in_combat') and client_state.get('screen') == 'CARD_SELECTION')
    if unknown and not recover_selection:
        raise RuntimeError('Cannot resume an action with unknown outcome')
    if not recover_selection:
        recorded_client = completed_actions[-1].get('client_after') if completed_actions else None
        exact_client_match = bool(
            recorded_client
            and gameplay_observation(recorded_client) == gameplay_observation(client_state)
        )
        resolved_event_match = bool(
            recorded_client
            and not exact_client_match
            and event_resolution_equivalent(recorded_client, client_state)
        )
        if not exact_client_match and not resolved_event_match:
            raise RuntimeError('Cannot resume: client changed after the recorded action')
        if resolved_event_match:
            log.write({
                'event': 'resume_event_template_resolved',
                'event_id': (client_state.get('event') or {}).get('event_id'),
            })

    base_state = bootstrap_state

    def replay(path, visited):
        path = path.resolve()
        if path in visited:
            raise RuntimeError('Resume report chain contains a cycle')
        visited.add(path)
        data = recover_report_tail(
            json.loads(path.read_text(encoding='utf-8')),
            path.parent / 'session.jsonl',
        )
        if _blocking_client_only_segments(data):
            raise RuntimeError('Cannot replay client-only rooms without a verified anchor')

        anchors = [
            row for row in data.get('reanchors') or []
            if row.get('status') == 'REANCHORED_PASS' and row.get('save_path')
        ]
        replay_after_sequence = None
        state = None
        if anchors:
            anchor = anchors[-1]
            anchor_path = Path(anchor['save_path']).resolve()
            if not anchor_path.is_file():
                raise RuntimeError(f'Re-anchor save is missing: {anchor_path}')
            expected_hash = str(anchor.get('save_sha256') or '')
            actual_hash = hashlib.sha256(anchor_path.read_bytes()).hexdigest()
            if expected_hash and actual_hash != expected_hash:
                raise RuntimeError('Re-anchor save hash differs from the recorded boundary')
            state = cli.load_save(str(anchor_path), lang='en')
            _raise_on_headless_error(state, 'load verified replay anchor')
            replay_after_sequence = anchor.get('action_sequence')
            log.write({
                'event': 'resume_reanchor_loaded',
                'source': str(path),
                'save_path': str(anchor_path),
                'action_sequence': replay_after_sequence,
            })
        else:
            if data.get('resume_report'):
                state = replay(Path(data['resume_report']), visited)
            else:
                state = base_state
                if state is None:
                    raise RuntimeError('Resume replay has no opening anchor state')
            if data.get('recovered_pending_action'):
                recovered = data['recovered_pending_action']
                translated = _explicit_shadow_command(recovered)
                if translated is None:
                    raise RuntimeError('Recovered pending action has no shadow command')
                state = cli.action(*translated, timeout_s=20)
                _raise_on_headless_error(state, 'replay reconciled pending action')
            for recovered in data.get('room_replay') or []:
                translated = _explicit_shadow_command(recovered)
                if translated is None:
                    raise RuntimeError('Room replay record has no shadow command')
                state = cli.action(*translated, timeout_s=20)
                _raise_on_headless_error(state, 'replay room recovery input')

        require_transaction = int(data.get('schema_version') or 0) >= 3
        for row in data.get('actions') or []:
            if row.get('recovered_parent_reference'):
                continue
            sequence = row.get('sequence')
            if (replay_after_sequence is not None and type(sequence) is int
                    and sequence <= int(replay_after_sequence)):
                continue
            eligible = row.get('status') == 'completed' or (
                recover_selection and path == args.resume_report.resolve() and row == data['actions'][-1]
            )
            if not eligible:
                continue
            translated = _recorded_shadow_command(
                row,
                state or {},
                require_transaction=require_transaction,
            )
            if translated:
                state = cli.action(*translated, timeout_s=20)
                _raise_on_headless_error(state, 'replay logged transaction')
        return state

    for attempt in range(3):
        try:
            state = replay(args.resume_report, set())
            break
        except TimeoutError:
            log.write({'event': 'resume_replay_timeout', 'attempt': attempt + 1,
                       'stderr_tail': list(cli._stderr_tail)})
            if attempt == 2:
                raise
            cli.stop()
            cli.start()
            state = cli.load_save(str(args.anchor_map_save.resolve()), resume_room=getattr(args, 'anchor_room', False))
            _raise_on_headless_error(state, 'reload resume anchor')
            base_state = state
    if not state or not state.get('decision'):
        raise RuntimeError('Replay did not reach a supported decision boundary')
    if recover_selection:
        if state.get('decision') != 'card_select':
            raise RuntimeError('Pending action replay did not produce a card selection')
        report['recovered_pending_action'] = native_parent or unknown[0]
        if native_parent is not None:
            parent_reference = copy.deepcopy(native_parent)
            parent_reference['recovered_parent_reference'] = True
            report.setdefault('actions', []).append(parent_reference)
            live.sequence = max(live.sequence, native_parent['sequence'])
            live.pending_native_parent = {
                'epoch': native_parent['native_action_epoch'],
                'id': native_parent['native_action_id'],
                'parent_sequence': native_parent['sequence'],
                'expected_card_id': matches[0]['card_id'],
            }
            log.write({'event': 'resume_native_choice',
                       'sequence': native_parent['sequence'],
                       'native_action_id': native_parent['native_action_id'],
                       'native_action_epoch': native_parent['native_action_epoch']})
    report['resume_report'] = str(args.resume_report.resolve())
    if source.get('flow_context'):
        report['flow_context'] = source['flow_context']
    elif source.get('interaction'):
        interaction = source['interaction']
        report['flow_context'] = {'scene': interaction.get('scene') or '',
                                  'operation': (interaction.get('selection') or {}).get('operation')}
    log.write({'event': 'resume_replay', 'source': report['resume_report'], 'decision': state['decision']})
    return state, int(source.get('completed_combat_count') or 0)


def _blocking_client_only_segments(report: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Return client-only segments backed by an action that may have executed."""

    actions_by_sequence = {
        row.get('sequence'): row
        for row in report.get('actions') or []
        if type(row.get('sequence')) is int
    }
    blockers = []
    for segment in report.get('client_only_segments') or []:
        if segment.get('status') == 'BOUNDARY_VERIFIED':
            continue
        action = actions_by_sequence.get(segment.get('start_sequence'))
        # Older runners opened the segment before the stale-action preflight.
        # A cancelled/NOT_EXECUTED row proves that no client-only transition
        # happened, so there is no missing re-anchor to recover.
        if (action is not None
                and action.get('status') == 'cancelled'
                and action.get('verification') == 'NOT_EXECUTED'):
            continue
        blockers.append(segment)
    return blockers




def _bootstrap_shadow(
    cli: Sts2CliAdapter,
    args: argparse.Namespace,
    log: JsonlSessionLog,
) -> Dict[str, Any]:
    state = cli.load_save(str(args.anchor_map_save.resolve()), lang="en", resume_room=getattr(args, 'anchor_room', False))
    _raise_on_headless_error(state, "load anchor save")
    if args.bootstrap_combat_report is None and args.bootstrap_reward_report is None:
        log.write(
            {
                "event": "shadow_bootstrap",
                "anchor_save": str(args.anchor_map_save.resolve()),
                "replayed_combat_actions": 0,
            }
        )
        return state
    if args.bootstrap_combat_report is None or args.bootstrap_reward_report is None:
        raise RuntimeError(
            "bootstrap-combat-report and bootstrap-reward-report must be provided together"
        )
    first_node, _ = choose_map_route_global(state, cli.get_map())
    state = cli.action("select_map_node", first_node, timeout_s=20.0)
    _raise_on_headless_error(state, "replay first map choice")
    combat_report = json.loads(args.bootstrap_combat_report.read_text(encoding="utf-8"))
    rows = list(combat_report.get("actions") or [])
    for row in rows:
        state = cli.action(str(row["action"]), dict(row.get("args") or {}), timeout_s=20.0)
        _raise_on_headless_error(state, "replay first combat")
    reward_report = json.loads(args.bootstrap_reward_report.read_text(encoding="utf-8"))
    state = cli.action(
        str(reward_report["action"]), dict(reward_report.get("payload") or {}), timeout_s=20.0
    )
    _raise_on_headless_error(state, "replay first reward")
    log.write(
        {
            "event": "shadow_bootstrap",
            "anchor_save": str(args.anchor_map_save.resolve()),
            "replayed_combat_actions": len(rows),
            "reward_action": reward_report["action"],
            "reward_payload": reward_report.get("payload") or {},
        }
    )
    return state


def _play_combat(
    cli: Sts2CliAdapter,
    cli_cfg: CliConfig,
    live: LiveClientBridge,
    client_state: Dict[str, Any],
    headless_state: Dict[str, Any],
    run_id: str,
    combat_number: int,
    args: argparse.Namespace,
    log: JsonlSessionLog,
    report: Dict[str, Any],
    control: "ManagedControl",
    worker_pool: Any = None,
    worker_controller: Any = None,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    plan = PlanState()
    deck_profile = load_deck_profile(getattr(args, 'deck_profile', None))
    selection_flow = InteractionFlow(Path(__file__).resolve().parents[1])
    selection_flow.context.scene = 'COMBAT'
    action_rows: List[Dict[str, Any]] = []
    started = time.perf_counter()
    combat_row = {"combat_number": combat_number, "status": "RUNNING",
                  "action_count": 0, "actions": action_rows}
    report["combats"].append(combat_row)
    validation_set = (CombatValidationSet(
        Path(args.session_dir).parent / 'combat_validation_set', cli_config=cli_cfg)
        if cli_cfg is not None and getattr(args, 'session_dir', None) else None)
    captured_for_this_combat = False
    evidence_artifact = None
    def seal_live_trace() -> None:
        if evidence_artifact is not None:
            try:
                if not seal_evidence(evidence_artifact, report, getattr(args, 'scoring_model_payload', None),
                                     dict(deck_profile.get('combat_coefficients') or {})):
                    log.write({'event': 'combat_evidence_incomplete', 'combat_number': combat_number})
            except (OSError, ValueError, TypeError) as exc:
                log.write({'event': 'combat_evidence_failed', 'combat_number': combat_number,
                           'error': str(exc)})
    for sequence in range(1, args.max_actions_per_combat + 1):
        control.checkpoint(
            "running",
            phase="combat_decision",
            client=_client_status(client_state),
            combat_number=combat_number,
            combat_action_sequence=sequence,
        )
        if headless_state.get('decision') == 'card_select':
            selection_flow.context.operation = 'remove' if _last_card_requires_exhaust(report) else None
            current, selected = selection_flow.choose(client_state, headless_state)
            if selected is None:
                raise FlowBlocked('Combat selection is not ready')
            decision_telemetry = dict(selected.telemetry)
            control.publish('running', interaction=current.to_dict(), phase='card_selection')
            report['interaction'] = current.to_dict()
            log.write({'event': 'interaction_state', **current.to_dict()})
            try:
                executed = execute_transaction(
                    selected,
                    live=live,
                    client_state=client_state,
                    log=log,
                    report=report,
                    advance_shadow=lambda action, payload, tx_log, tx_report: _advance_shadow(
                        cli, action, payload, tx_log, tx_report
                    ),
                    operation_label='resolve combat selection',
                    telemetry=decision_telemetry,
                )
            except StaleClientStateError as exc:
                client_state = exc.observed_state
                plan = PlanState()
                log.write({
                    'event': 'stale_action_replanned',
                    'sequence': (report.get('actions') or [{}])[-1].get('sequence'),
                    'screen': client_state.get('screen'),
                    'combat_number': combat_number,
                })
                continue
            client_state = executed.client_state
            if executed.shadow_state is None:
                raise RuntimeError('Combat selection transaction has no shadow command')
            headless_state = executed.shadow_state
            plan = PlanState()
            parent_sequence = (report.get('actions') or [{}])[-1].get('parent_sequence')
            parent_row = None
            if parent_sequence is not None:
                parent_row = _settle_parent_choice(report, client_state)
                log.write({'event': 'parent_action_settled',
                           'sequence': parent_row['sequence'], 'parent_record': parent_row})
            if headless_state.get('decision') == 'combat_play':
                # Native card selections can return as soon as the choice is
                # accepted while the visible client is still rendering the
                # resulting enemy turn.  Do not compare that stale frame to
                # the already-advanced shadow; wait only while the client
                # explicitly reports an older combat turn, then fail closed.
                shadow_search = cli.get_search_state(timeout_s=10.0).get('combat_state_for_search') or {}
                shadow_turn = (shadow_search.get('combat') or {}).get('round_number')
                if type(shadow_turn) is not int or shadow_turn < 1:
                    raise RuntimeError('Combat selection has no authoritative shadow round')
                client_turn = (client_state.get('turn') or 0)
                if client_state.get('in_combat') and client_turn < shadow_turn:
                    settle_deadline = time.monotonic() + max(
                        float(live.mod.config.timeout_s), 20.0)
                    while client_turn < shadow_turn and time.monotonic() < settle_deadline:
                        time.sleep(0.05)
                        client_state = live.observe()
                        client_turn = (client_state.get('turn') or 0)
                    if client_turn < shadow_turn:
                        raise FlowBlocked(
                            f'Combat selection did not reach shadow round {shadow_turn}; '
                            f'client turn={client_turn}, actions={client_state.get("available_actions")!r}'
                        )
                _check_combat(cli, client_state, headless_state, run_id,
                              f'combat_{combat_number}_selection_{sequence}', log, report,
                              search_state=shadow_search)
                if parent_row is not None:
                    parent_row.update(boundary_verified=True, verification='COVERED_FIELDS_MATCH',
                                      settlement_phase='VERIFIED',
                                      transaction_status='completed')
                    log.write({'event': 'parent_action_verified',
                               'sequence': parent_row['sequence'], 'parent_record': parent_row})
            elif headless_state.get('decision') != 'card_select':
                if client_state.get('in_combat'):
                    raise RuntimeError('Combat selection terminal states disagree')
                if _confirmed_defeat(client_state, headless_state):
                    combat_row['status'] = 'DEFEAT'
                    seal_live_trace()
                    live.pace_after_verification(
                        verification='terminal_defeat_match',
                        record=(report.get('actions') or [None])[-1],
                    )
                    raise RunDefeat(f'Agent defeated during selection in combat {combat_number}')
                combat_row['status'] = 'COMPLETED'
                seal_live_trace()
                live.pace_after_verification(
                    verification='terminal_boundary_match',
                    record=(report.get('actions') or [None])[-1],
                )
                client_state = live.observe()
                return combat_row, client_state, headless_state
            live.pace_after_verification(
                verification='combat_selection_checkpoint',
                record=(report.get('actions') or [None])[-1],
            )
            client_state = live.observe()
            continue
        search_result = cli.get_search_state(timeout_s=10.0)
        search_state = search_result.get("combat_state_for_search") or {}
        if not search_state:
            raise RuntimeError(f"Incomplete search state in combat {combat_number}")
        if report.get('verification_enabled', True):
            comparison = compare_checkpoints(
                client_combat_checkpoint(client_state),
                headless_combat_checkpoint(headless_state, search_state, run_id),
            )
            if sequence == 1 and comparison.status != 'PASS' and not _enemy_intents_only(comparison):
                _record_combat_entry_diagnostic(
                    live, client_state, headless_state, search_state,
                    comparison, combat_number, log, report,
                )
            if sequence == 1 and _enemy_intents_only(comparison):
                headless_state, search_state = _reconcile_combat_entry(
                    cli, live, client_state, comparison, combat_number,
                    args.session_dir, log, report,
                )
                comparison = compare_checkpoints(
                    client_combat_checkpoint(client_state),
                    headless_combat_checkpoint(headless_state, search_state, run_id),
                )
            _record_comparison(
                comparison,
                f"combat_{combat_number}_before_action_{sequence}",
                log,
                report,
                attach_pending_action=False,
            )
        if sequence == 1:
            pending_pacing = (report.get('actions') or [None])[-1]
            if (isinstance(pending_pacing, dict)
                    and pending_pacing.get('pacing_status') == 'pending_combat_checkpoint'):
                live.pace_after_verification(
                    verification='combat_entry_checkpoint',
                    record=pending_pacing,
                )
                refreshed = live.observe()
                if gameplay_observation(refreshed) != gameplay_observation(client_state):
                    client_state = refreshed
                    plan = PlanState()
                    log.write({
                        'event': 'post_pacing_state_changed',
                        'combat_number': combat_number,
                        'screen': client_state.get('screen'),
                    })
                    continue
                client_state = refreshed
        context = headless_state.get("context") or {}
        # Encounter identity belongs to the canonical search snapshot returned
        # by get_search_state().  The lighter decision state is intentionally
        # allowed to omit it, so reading that object here makes every live
        # combat fail even when the search snapshot carries the native id.
        combat_payload = search_state.get("combat") or {}
        encounter_id = str(combat_payload.get("encounter_id") or "").strip()
        if not encounter_id:
            raise RuntimeError(
                f"Headless search state has no encounter_id at combat {combat_number}, "
                "refusing to search with an inferred encounter"
            )
        if validation_set is not None and not captured_for_this_combat:
            try:
                captured = validation_set.capture(
                    live.mod, client_state=client_state, encounter=encounter_id,
                    room_type=str((headless_state.get('context') or {}).get('room_type') or ''),
                    session_dir=Path(args.session_dir), combat_number=combat_number)
                if captured is not None:
                    captured_for_this_combat = True
                    if captured.get('status') == 'RESTORE_VERIFIED':
                        evidence_artifact = Path(captured['artifact_dir'])
                    log.write({'event': 'combat_validation_capture',
                               'snapshot_id': captured.get('snapshot_id'),
                               'status': captured.get('status'),
                               'error': captured.get('error'),
                               'coverage': validation_set.summary()})
            except Exception as capture_error:
                captured_for_this_combat = True
                log.write({'event': 'combat_validation_capture_failed',
                           'error': str(capture_error), 'combat_number': combat_number})
        active_workers = (
            worker_controller.current_workers
            if worker_controller is not None
            else max(1, int(getattr(args, 'max_workers', 2)))
        )
        if worker_pool is not None:
            worker_pool.prewarm(active_workers)
        pool_discards_before = int(
            (worker_pool.stats() if worker_pool is not None else {}).get('discards') or 0
        )
        cfg = CombatStepConfig(
            cli_cfg=cli_cfg,
            spec=CombatSpec(
                character="Ironclad",
                encounter=encounter_id,
                seed=run_id,
                ascension=int(headless_state.get("ascension") or 0),
                lang="en",
            ),
            depth=args.depth,
            chance_depth=args.chance_depth,
            score_mode="preference",
            max_workers=active_workers,
            user_parallel=active_workers > 1,
            reuse_cli_processes=worker_pool is not None,
            floor=context.get("floor"),
            room_type=context.get("room_type"),
            capture_root_topk=5,
            max_search_ms=args.max_search_ms,
            worker_pool=worker_pool,
            evaluator_coefficients=dict(deck_profile.get('combat_coefficients') or {}),
            scorer_model=getattr(args, 'scoring_model_payload', None),
        )
        decision_started = time.perf_counter()
        cpu_monitor = CpuDecisionMonitor()
        cpu_monitor.start()
        try:
            decision = decide_combat_action(cli, search_state, cfg, plan)
        finally:
            cpu_metrics = cpu_monitor.finish()
        decision_ms = (time.perf_counter() - decision_started) * 1000.0
        parallel_audit = dict(decision.searcher_timing_summary.get('parallel_audit') or {})
        worker_adjustment = None
        if worker_controller is not None:
            root_candidates = int(
                ((decision.decision_audit.get('root') or {}).get('candidate_actions')) or 0
            )
            turn_space = decision.decision_audit.get('turn_space') or {}
            known_unexpanded = int(
                turn_space.get('unique_known_unexpanded_action_edges')
                or turn_space.get('known_unexpanded_action_edges')
                or 0
            )
            queued_root_work = (
                max(
                    int(parallel_audit.get('frontier_jobs_queued') or 0),
                    int(parallel_audit.get('root_jobs_queued') or 0),
                    root_candidates,
                ) > int(parallel_audit.get('effective_worker_slots') or active_workers)
                or known_unexpanded > 0
            )
            worker_adjustment = worker_controller.observe(
                cpu_metrics['cpu_avg_percent'],
                cpu_metrics['cpu_peak_percent'],
                queued_root_work=queued_root_work,
                deadline_overrun=bool(args.max_search_ms and decision_ms > args.max_search_ms + 350.0),
                deadline_overrun_ratio=(decision_ms / args.max_search_ms) if args.max_search_ms else 0.0,
                worker_failures=max(
                    0,
                    int((worker_pool.stats() if worker_pool is not None else {}).get('discards') or 0)
                    - pool_discards_before,
                ),
            )
        telemetry = {
            "decision_ms": round((time.perf_counter() - decision_started) * 1000, 3),
            "timing_breakdown": decision.timing,
            "fell_back": decision.fell_back,
            "raw_retry_used": decision.raw_retry_used,
            "search_failed": decision.search_failed,
            "budget_type": "soft",
            "policy": "bounded_combat_search",
            "search_ms": round(float(decision.timing.get("search_ms") or 0.0), 3),
            "nodes": decision.nodes,
            "score": decision.search_score,
            "reused_plan": decision.reused_plan,
            "time_budget_ms": args.max_search_ms,
            "time_budget_exhausted": bool(
                decision.searcher_timing_summary.get("time_budget_exhausted")
            ),
            "engine_rpc_failures": int(decision.searcher_timing_summary.get("engine_rpc_failures") or 0),
            "engine_rpc_failure_samples": list(
                decision.searcher_timing_summary.get("engine_rpc_failure_samples") or []
            ),
            "chosen": decision.chosen_summary,
            "root_candidates": decision.root_candidates,
        }
        telemetry['decision_ms'] = round(decision_ms, 3)
        telemetry['worker_pool'] = worker_pool.stats() if worker_pool is not None else {'enabled': False}
        telemetry['worker_runtime'] = {
            'mode': getattr(args, 'worker_mode', 'fixed'),
            'active_workers': active_workers,
            **cpu_metrics,
            'adjustment': worker_adjustment,
        }
        telemetry['parallel_audit'] = parallel_audit
        telemetry['decision_reason'] = getattr(decision, 'decision_reason', 'fallback' if decision.fell_back else 'highest_score')
        telemetry['plan_diverged'] = getattr(decision, 'plan_diverged', None)
        telemetry['decision_audit'] = getattr(decision, 'decision_audit', {})
        limits_audit = telemetry['decision_audit'].get('limits') or {}
        telemetry['soft_budget_overrun'] = bool(limits_audit.get('soft_budget_overrun'))
        telemetry['soft_budget_overrun_ms'] = float(limits_audit.get('soft_budget_overrun_ms') or 0.0)
        telemetry['branch_budget_exhaustions'] = int(limits_audit.get('branch_budget_exhaustions') or 0)
        telemetry['stage_b_budget_exhaustions'] = int(limits_audit.get('stage_b_budget_exhaustions') or 0)
        telemetry['budget_pressure_detected'] = bool(limits_audit.get('budget_pressure_detected'))
        telemetry['score_explanation'] = getattr(decision, 'score_explanation', {})
        print(
            json.dumps(
                {
                    "event": "combat_action",
                    "combat_number": combat_number,
                    "sequence": sequence,
                    "action": decision.action,
                    "args": decision.payload,
                    **telemetry,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        control.checkpoint(
            "running",
            phase="before_combat_action",
            client=_client_status(client_state),
            combat_number=combat_number,
            combat_action_sequence=sequence,
            pending_action={"action": decision.action, "payload": decision.payload},
            search_ms=telemetry["search_ms"],
            nodes=telemetry["nodes"],
            reused_plan=telemetry["reused_plan"],
        )
        transaction_telemetry = {"combat_number": combat_number, **telemetry}
        transaction = live.prepare_headless_transaction(
            "combat_play",
            decision.action,
            decision.payload,
            client_state,
            transaction_telemetry,
        )
        try:
            executed = execute_transaction(
                transaction,
                live=live,
                client_state=client_state,
                log=log,
                report=report,
                advance_shadow=lambda action, payload, tx_log, tx_report: _advance_shadow(
                    cli, action, payload, tx_log, tx_report
                ),
                operation_label='combat action',
                telemetry=transaction_telemetry,
            )
        except StaleClientStateError as exc:
            client_state = exc.observed_state
            plan = PlanState()
            log.write({
                'event': 'stale_action_replanned',
                'sequence': (report.get('actions') or [{}])[-1].get('sequence'),
                'screen': client_state.get('screen'),
                'combat_number': combat_number,
            })
            continue
        client_after = executed.client_state
        if executed.shadow_state is None:
            raise RuntimeError('Combat transaction has no shadow command')
        headless_after = executed.shadow_state
        client_action_ms = live.last_action.get("action_wall_ms")
        action_rows.append(
            {
                "sequence": sequence,
                "action": decision.action,
                "args": decision.payload,
                **telemetry,
                "client_action_ms": client_action_ms,
            }
        )
        combat_row["action_count"] = len(action_rows)
        combat_row["wall_ms"] = round((time.perf_counter() - started) * 1000, 3)
        client_state, headless_state = client_after, headless_after
        client_in_combat = bool(client_state.get("in_combat"))
        headless_in_combat = headless_state.get("decision") in {"combat_play", "card_select"}
        if client_in_combat != headless_in_combat:
            raise RuntimeError(
                f"Combat {combat_number} terminal state diverged: "
                f"client={client_in_combat}, headless={headless_state.get('decision')!r}"
            )
        if not client_in_combat:
            if _confirmed_defeat(client_state, headless_state):
                combat_row['status'] = 'DEFEAT'
                seal_live_trace()
                terminal_sequence = None
                if report.get('actions'):
                    report['actions'][-1]['verification'] = 'TERMINAL_DEFEAT_MATCH'
                    terminal_sequence = report['actions'][-1].get('sequence')
                log.write({'event': 'terminal_outcome', 'status': 'DEFEAT',
                           'verification': 'TERMINAL_DEFEAT_MATCH',
                           'sequence': terminal_sequence,
                           'combat_number': combat_number, 'client': client_state,
                           'headless': headless_state})
                live.pace_after_verification(
                    verification='terminal_defeat_match',
                    record=(report.get('actions') or [None])[-1],
                )
                raise RunDefeat(f'Agent defeated in combat {combat_number}')
            terminal_sequence = None
            if report.get('actions'):
                terminal_action = report['actions'][-1]
                if terminal_action.get('verification') == 'PENDING':
                    terminal_action['verification'] = 'TERMINAL_BOUNDARY_MATCH'
                terminal_sequence = terminal_action.get('sequence')
            log.write({
                'event': 'terminal_outcome',
                'status': 'COMPLETED',
                'verification': 'TERMINAL_BOUNDARY_MATCH',
                'sequence': terminal_sequence,
                'combat_number': combat_number,
                'client_screen': client_state.get('screen'),
                'headless_decision': headless_state.get('decision'),
            })
            combat_row["status"] = "COMPLETED"
            seal_live_trace()
            live.pace_after_verification(
                verification='terminal_boundary_match',
                record=(report.get('actions') or [None])[-1],
            )
            client_state = live.observe()
            return (
                combat_row,
                client_state,
                headless_state,
            )
        if headless_state.get('decision') == 'combat_play':
            _check_combat(cli, client_state, headless_state, run_id,
                          f"combat_{combat_number}_after_action_{sequence}", log, report)
        elif headless_state.get('decision') == 'card_select':
            parent = report['actions'][-1]
            if parent.get('native_action_phase') == 'awaiting_input':
                # Both sides are paused at the same choice. No final combat
                # projection or pacing claim is valid until the parent resumes.
                continue
        live.pace_after_verification(
            verification='combat_action_checkpoint',
            record=(report.get('actions') or [None])[-1],
        )
        client_state = live.observe()
    raise RuntimeError(
        f"Combat {combat_number} exceeded {args.max_actions_per_combat} actions"
    )


def _confirmed_defeat(client, headless):
    headless_defeat = headless.get('decision') in {'game_over', 'defeat'} and headless.get('victory') is not True
    hp = (client.get('run') or {}).get('current_hp')
    client_defeat = isinstance(hp, (int, float)) and hp <= 0
    if bool(client_defeat) != bool(headless_defeat):
        raise RuntimeError('Client and shadow disagree on combat defeat')
    return bool(client_defeat and headless_defeat)


def _settle_parent_choice(report, client_state):
    """Close one native action only after its selection resumed and completed."""
    child = report['actions'][-1]
    parent = next((item for item in report['actions']
                   if item.get('sequence') == child.get('parent_sequence')), None)
    if parent is None or parent.get('transaction_status') != 'both_awaiting_input':
        raise RuntimeError('Combat selection has no pending native parent transaction')
    if (child.get('native_action_phase') != 'completed'
            or child.get('native_action_id') != parent.get('native_action_id')
            or child.get('native_action_epoch') != parent.get('native_action_epoch')
            or child.get('shadow_completed') is not True):
        raise RuntimeError('Combat selection did not complete the same native parent action')
    from controller.rng_parity import compare_rng_transitions
    parent_rng = compare_rng_transitions(
        parent['client_rng_before'], child['client_rng_after'],
        parent['shadow_rng_before'], child['shadow_rng_after'])
    parent['rng_parity'] = parent_rng
    parent['rng_verified'] = parent_rng['status'] == 'PASS'
    if not parent['rng_verified']:
        raise RuntimeError(f'Parent combat action RNG diverged: {parent_rng["differences"][:3]}')
    parent.update(client_completed=True, shadow_completed=True,
                  status='completed', event='live_action',
                  client_after=client_state,
                  client_digest_after=client_state_digest(client_state),
                  screen_after=client_state.get('screen'),
                  client_rng_after=child['client_rng_after'],
                  shadow_rng_after=child['shadow_rng_after'],
                  native_action_phase='completed',
                  completion_evidence=child.get('completion_evidence'),
                  settlement_phase='BOTH_SETTLED',
                  transaction_status='both_settled_awaiting_checkpoint')
    return parent


def _last_card_requires_exhaust(report):
    rows = report.get('actions') or []
    for row in reversed(rows):
        chosen = (row.get('decision_telemetry') or {}).get('chosen') or {}
        card_id = (chosen.get('metadata') or {}).get('card_id')
        if card_id:
            return card_id in {'BURNING_PACT', 'TRUE_GRIT'}
    return False


def _client_map_option_index(
    state: Dict[str, Any], selected_node: Dict[str, Any]
) -> int:
    matches = []
    for node in (state.get("map") or {}).get("available_nodes") or []:
        coord = node.get("coord") or node
        if (
            coord.get("row") == selected_node.get("row")
            and coord.get("col") == selected_node.get("col")
        ):
            matches.append(node)
    if len(matches) != 1 or matches[0].get("index") is None:
        raise RuntimeError(
            "Cannot uniquely resolve the selected headless map node in the visible client"
        )
    return int(matches[0]["index"])


def _explicit_shadow_command(record):
    transaction_data = record.get('transaction') if isinstance(record, dict) else None
    if transaction_data is not None:
        transaction = transaction_from_dict(transaction_data)
        if transaction.shadow is None:
            return None
        return transaction.shadow.action, dict(transaction.shadow.params)
    action = record.get('headless_action') if isinstance(record, dict) else None
    if action:
        return action, dict(record.get('headless_args') or {})
    return None


def _recorded_shadow_command(row, headless, *, require_transaction=False):
    if row.get('transaction') is not None:
        return _explicit_shadow_command(row)
    if require_transaction:
        raise RuntimeError(
            f"Schema 3 action {row.get('sequence')!r} is missing its transaction"
        )
    explicit = _explicit_shadow_command(row)
    if explicit is not None:
        return explicit
    if row.get('flow_managed') or row.get('client_action') == 'claim_reward':
        return None
    decision = SimpleNamespace(
        action=row.get('client_action'),
        params=row.get('client_params') or {},
        telemetry=row.get('decision_telemetry') or {},
    )
    return _noncombat_shadow_action(
        decision,
        row.get('client_before') or {},
        headless,
    )




def _noncombat_shadow_action(decision, client, headless):
    """Translate schema-2 history only; new reports persist transaction.shadow."""
    phase = headless.get('decision')
    action = decision.action
    if action == 'choose_event_option':
        if phase == 'map_select' and decision.telemetry.get('policy') == 'explicit_proceed':
            return None
        if phase != 'event_choice':
            raise RuntimeError(f'Event choice differs from shadow phase {phase}')
        key = decision.telemetry.get('option_id')
        matches = [row for row in headless.get('options') or [] if row.get('text_key') == key]
        if len(matches) != 1:
            raise RuntimeError(f'Cannot match event option {key!r} in shadow')
        return 'choose_option', {'option_index': matches[0]['index']}
    if action in {'select_deck_card', 'select_deck_cards'}:
        if phase != 'card_select':
            raise RuntimeError(f'Card selection differs from shadow phase {phase}')
        visible = (client.get('selection') or {}).get('cards') or []
        candidates = headless.get('cards') or []
        norm = lambda value: str(value or '').split('.')[-1].upper()
        if [(norm(c.get('card_id')), c.get('upgraded', False)) for c in visible] != [
            (norm(c.get('id')), c.get('upgraded', False)) for c in candidates]:
            raise RuntimeError('Non-combat card choices differ from shadow')
        indices = decision.params.get('indices', [decision.params.get('option_index')])
        positions = []
        for index in indices:
            matches = [i for i, card in enumerate(visible) if card.get('index') == index]
            if len(matches) != 1:
                raise RuntimeError('Cannot match selected card index')
            positions.append(candidates[matches[0]]['index'])
        return 'select_cards', {'indices': ','.join(map(str, positions))}
    if action == 'choose_rest_option' and phase == 'rest_site':
        options = headless.get('options') or []
        key = decision.telemetry.get('option_id')
        matches = [row for row in options if (row.get('option_id') or row.get('id')) == key]
        if len(matches) != 1:
            raise RuntimeError(f'Cannot match rest option {key!r}')
        return 'choose_option', {'option_index': matches[0]['index']}
    if action == 'open_shop_inventory' and phase == 'shop':
        return None
    if action == 'close_shop_inventory' and phase == 'shop':
        return 'leave_room', {}
    if action == 'proceed':
        if phase == 'map_select':
            return None
        if phase in {'treasure', 'rest_site', 'shop'}:
            return 'leave_room', {}
    if action in {'open_chest', 'choose_treasure_relic'} and phase in {'treasure', 'map_select'}:
        return None  # The headless treasure entry collects the relic automatically.
    raise RuntimeError(f'No audited shadow translation for {action!r} at {phase!r}')


def _latest_official_save() -> Path:
    appdata = Path(os.environ.get("APPDATA") or "")
    root = appdata / "SlayTheSpire2" / "steam"
    candidates = [
        path
        for path in root.glob("*/modded/profile*/saves/current_run.save")
        if path.is_file()
    ]
    if not candidates:
        raise FileNotFoundError("No Modded current_run.save was found for re-anchoring")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _file_fingerprint(path: Path) -> tuple[int, int, str]:
    raw = path.read_bytes()
    stat = path.stat()
    return stat.st_mtime_ns, len(raw), hashlib.sha256(raw).hexdigest()


def _write_authoritative_anchor(path: Path, save_json: str) -> str:
    """Hash and atomically persist the exact same UTF-8 bytes on every OS."""
    raw = save_json.encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    temporary = path.with_suffix('.save.tmp')
    temporary.write_bytes(raw)
    if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
        raise RuntimeError('Authoritative anchor bytes changed while writing')
    os.replace(temporary, path)
    return digest


def _reanchor_from_official_save(
    cli: Sts2CliAdapter,
    client_state: Dict[str, Any],
    save_path: Path,
    baseline_fingerprint: tuple[int, int, str],
    log: JsonlSessionLog,
    report: Dict[str, Any],
    segment_id: int,
    *,
    room_replay_before_sequence: int | None = None,
    authoritative_save_json: str | None = None,
    authoritative_client_rng: Dict[str, Any] | None = None,
    max_wait_s: float = 20.0,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    if authoritative_save_json is not None:
        session_dir = Path(report["anchor_save"]).resolve().parent
        snapshot_path = session_dir / f"reanchor_segment_{segment_id}.save"
        digest = _write_authoritative_anchor(snapshot_path, authoritative_save_json)
        # A discarded event can still own a blocked reward task in the old
        # process. Never let that continuation mutate the authoritative reload.
        cli.stop()
        cli.start()
        candidate = cli.load_save_json(authoritative_save_json, lang="en")
        if candidate.get("type") == "error":
            raise RuntimeError(f"Authoritative client save could not be loaded: {candidate}")
        if candidate.get("decision") != "map_select":
            raise RuntimeError(
                "Authoritative client save did not restore a map boundary: "
                f"decision={candidate.get('decision')!r}"
            )
        result = compare_checkpoints(
            client_map_checkpoint(client_state),
            headless_map_checkpoint(
                candidate,
                cli.get_map(),
                run_id=str(client_state.get("run_id") or ""),
            ),
        )
        if result.status != "PASS":
            raise RuntimeError(
                "Authoritative client save parity failed: "
                f"{result.differences[:5]}"
            )
        rng_result = None
        if authoritative_client_rng is not None:
            from controller.rng_parity import compare_rng_snapshots

            rng_result = compare_rng_snapshots(
                authoritative_client_rng,
                cli.get_rng_snapshot(),
            )
            if not rng_result.passed:
                raise RuntimeError(
                    "Authoritative client save RNG parity failed: "
                    f"{rng_result.differences[:5]}"
                )
        row = {
            "segment_id": segment_id,
            "status": "REANCHORED_PASS",
            "scope": "COVERED_FIELDS_ONLY",
            "client": result.client,
            "headless": result.headless,
            "differences": [],
            "save_path": str(snapshot_path),
            "source_save_path": "client://exact-save",
            "save_sha256": digest,
            "action_sequence": (report.get("actions") or [{}])[-1].get("sequence"),
            "client_digest": result.client_digest,
            "headless_digest": result.headless_digest,
            "difference_count": 0,
            "reanchor_mode": "CLIENT_EXACT_SAVE",
            "source_save_fresh": True,
            "rng_parity": (
                {"status": rng_result.status, "differences": rng_result.differences}
                if rng_result is not None else None
            ),
        }
        report["reanchors"].append(row)
        report["latest_checkpoint"] = row
        report["parity_checkpoints"].append(
            {"label": f"reanchor_segment_{segment_id}", **row, "differences": []}
        )
        log.write({"event": "reanchor", **row})
        print(json.dumps({"event": "reanchor", **row}, ensure_ascii=False), flush=True)
        return candidate, row

    if room_replay_before_sequence is not None:
        raise RuntimeError(
            "Historical room replay without an authoritative client exact save "
            "is disabled; the save time point cannot be proven"
        )

    deadline = time.monotonic() + max(0.0, float(max_wait_s))
    last_attempted: tuple[int, int, str] | None = None
    last_differences: List[Dict[str, Any]] = []
    last_candidate: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        if not save_path.exists():
            last_candidate = {"status": "missing_save"}
            time.sleep(0.2)
            continue
        fingerprint = _file_fingerprint(save_path)
        if fingerprint == baseline_fingerprint or fingerprint == last_attempted:
            last_candidate = {
                "status": "not_fresh",
                "save_sha256": fingerprint[2],
                "baseline_sha256": baseline_fingerprint[2],
            }
            time.sleep(0.2)
            continue
        # Require two identical reads so we never load a partially written save.
        time.sleep(0.15)
        if _file_fingerprint(save_path) != fingerprint:
            continue
        last_attempted = fingerprint
        last_candidate = {"status": "loading", "save_sha256": fingerprint[2]}
        session_dir = Path(report["anchor_save"]).resolve().parent
        snapshot_path = session_dir / f"reanchor_segment_{segment_id}.save"
        snapshot_tmp = snapshot_path.with_suffix(".save.tmp")
        snapshot_tmp.write_bytes(save_path.read_bytes())
        os.replace(snapshot_tmp, snapshot_path)
        if _file_fingerprint(snapshot_path)[2] != fingerprint[2]:
            time.sleep(0.2)
            continue
        candidate = cli.load_save(str(snapshot_path), lang="en")
        if candidate.get("type") == "error" or candidate.get("decision") != "map_select":
            last_candidate = {
                "status": "wrong_boundary",
                "save_sha256": fingerprint[2],
                "decision": candidate.get("decision"),
                "error": candidate.get("message"),
            }
            time.sleep(0.2)
            continue
        result = compare_checkpoints(
            client_map_checkpoint(client_state),
            headless_map_checkpoint(
                candidate,
                cli.get_map(),
                run_id=str(client_state.get("run_id") or ""),
            ),
        )
        last_differences = result.differences
        if result.status != "PASS":
            last_candidate = {
                "status": result.status,
                "save_sha256": fingerprint[2],
                "differences": result.differences[:5],
            }
            time.sleep(0.2)
            continue
        row = {
            "segment_id": segment_id,
            "status": "REANCHORED_PASS",
            "scope": "COVERED_FIELDS_ONLY",
            "client": result.client,
            "headless": result.headless,
            "differences": [],
            "save_path": str(snapshot_path),
            "source_save_path": str(save_path),
            "save_sha256": fingerprint[2],
            "action_sequence": (report.get("actions") or [{}])[-1].get("sequence"),
            "client_digest": result.client_digest,
            "headless_digest": result.headless_digest,
            "difference_count": 0,
            "reanchor_mode": "FRESH_OFFICIAL_SAVE",
            "source_save_fresh": True,
        }
        report["reanchors"].append(row)
        report['latest_checkpoint'] = row
        report["parity_checkpoints"].append(
            {"label": f"reanchor_segment_{segment_id}", **row, "differences": []}
        )
        log.write({"event": "reanchor", **row})
        print(json.dumps({"event": "reanchor", **row}, ensure_ascii=False), flush=True)
        return candidate, row

    # The game may leave current_run.save at the beginning of a room while the
    # visible client has already consumed that room. Never accept this stale
    # state directly. Resume the saved room and replay the last mirrored
    # transaction before the client-only segment, then verify map and RNG.
    if room_replay_before_sequence is not None and save_path.exists():
        replay_rows = [
            row for row in report.get("actions") or []
            if row.get("status") == "completed"
            and isinstance(row.get("sequence"), int)
            and row["sequence"] < int(room_replay_before_sequence)
            and _explicit_shadow_command(row) is not None
        ]
        replay_rows.sort(key=lambda row: int(row["sequence"]), reverse=True)
        source_fingerprint = _file_fingerprint(save_path)
        session_dir = Path(report["anchor_save"]).resolve().parent
        snapshot_path = session_dir / f"reanchor_segment_{segment_id}.save"
        snapshot_tmp = snapshot_path.with_suffix(".save.tmp")
        snapshot_tmp.write_bytes(save_path.read_bytes())
        os.replace(snapshot_tmp, snapshot_path)
        if _file_fingerprint(snapshot_path)[2] != source_fingerprint[2]:
            raise RuntimeError("Official save changed while preparing room replay")

        for replay_row in replay_rows:
            replay_sequence = int(replay_row["sequence"])
            try:
                candidate = cli.load_save(
                    str(snapshot_path), lang="en", resume_room=True
                )
                if candidate.get("type") == "error":
                    last_candidate = {
                        "status": "room_resume_error",
                        "replay_sequence": replay_sequence,
                        "error": candidate.get("message"),
                    }
                    continue

                if candidate.get("decision") != "map_select":
                    shadow_command = _explicit_shadow_command(replay_row)
                    if shadow_command is None:
                        continue
                    rng_before = None
                    capture_rng = getattr(cli, "get_rng_snapshot", None)
                    if callable(capture_rng):
                        rng_before = capture_rng()
                    candidate = cli.action(*shadow_command, timeout_s=20.0)
                    if candidate.get("type") == "error":
                        last_candidate = {
                            "status": "room_replay_error",
                            "replay_sequence": replay_sequence,
                            "error": candidate.get("message"),
                        }
                        continue
                    if rng_before is not None and callable(capture_rng):
                        from controller.rng_parity import compare_rng_transitions

                        rng_after = capture_rng()
                        client_before = replay_row.get("client_rng_before")
                        client_after = replay_row.get("client_rng_after")
                        if isinstance(client_before, dict) and isinstance(client_after, dict):
                            rng_result = compare_rng_transitions(
                                client_before, client_after, rng_before, rng_after
                            )
                            if rng_result["status"] != "PASS":
                                last_candidate = {
                                    "status": "room_replay_rng_mismatch",
                                    "replay_sequence": replay_sequence,
                                    "differences": rng_result["differences"][:5],
                                }
                                continue

                if candidate.get("decision") != "map_select":
                    last_candidate = {
                        "status": "room_replay_wrong_boundary",
                        "replay_sequence": replay_sequence,
                        "decision": candidate.get("decision"),
                    }
                    continue
                result = compare_checkpoints(
                    client_map_checkpoint(client_state),
                    headless_map_checkpoint(
                        candidate,
                        cli.get_map(),
                        run_id=str(client_state.get("run_id") or ""),
                    ),
                )
                last_differences = result.differences
                if result.status != "PASS":
                    last_candidate = {
                        "status": result.status,
                        "replay_sequence": replay_sequence,
                        "differences": result.differences[:5],
                    }
                    continue
                row = {
                    "segment_id": segment_id,
                    "status": "REANCHORED_PASS",
                    "scope": "COVERED_FIELDS_ONLY",
                    "client": result.client,
                    "headless": result.headless,
                    "differences": [],
                    "save_path": str(snapshot_path),
                    "source_save_path": str(save_path),
                    "save_sha256": source_fingerprint[2],
                    "action_sequence": (report.get("actions") or [{}])[-1].get("sequence"),
                    "client_digest": result.client_digest,
                    "headless_digest": result.headless_digest,
                    "difference_count": 0,
                    "reanchor_mode": "STALE_SAVE_ROOM_REPLAY",
                    "source_save_fresh": False,
                    "replayed_sequence": replay_sequence,
                }
                report["reanchors"].append(row)
                report["latest_checkpoint"] = row
                report["parity_checkpoints"].append(
                    {"label": f"reanchor_segment_{segment_id}", **row, "differences": []}
                )
                log.write({"event": "reanchor", **row})
                print(json.dumps({"event": "reanchor", **row}, ensure_ascii=False), flush=True)
                return candidate, row
            except Exception as exc:
                last_candidate = {
                    "status": "room_replay_exception",
                    "replay_sequence": replay_sequence,
                    "error": str(exc),
                }

    raise RuntimeError(
        "No fresh official map save passed the complete re-anchor comparison; "
        f"last_differences={last_differences[:5]}; last_candidate={last_candidate}"
    )


def _check_map(
    cli: Sts2CliAdapter,
    client_state: Dict[str, Any],
    headless_state: Dict[str, Any],
    label: str,
    log: JsonlSessionLog,
    report: Dict[str, Any],
) -> None:
    if not report.get('verification_enabled', True):
        return
    started = time.perf_counter()
    result = compare_checkpoints(
        client_map_checkpoint(client_state),
        headless_map_checkpoint(
            headless_state,
            cli.get_map(),
            run_id=str(client_state.get("run_id") or ""),
        ),
    )
    _record_comparison(result, label, log, report, (time.perf_counter() - started) * 1000)


def _enemy_intents_only(comparison: Any) -> bool:
    return (comparison.status == 'FAIL' and bool(comparison.differences)
            and all(str(row.get('path') or '').startswith('enemies[')
                    and '.intents[' in str(row.get('path') or '')
            for row in comparison.differences))


def _record_combat_entry_diagnostic(
    live: LiveClientBridge,
    client_state: Dict[str, Any],
    headless_state: Dict[str, Any],
    search_state: Dict[str, Any],
    mismatch: Any,
    combat_number: int,
    log: JsonlSessionLog,
    report: Dict[str, Any],
) -> None:
    """Capture decisive entry metadata while preserving the existing fail-closed path."""
    from controller.rng_parity import compare_rng_snapshots

    record: Dict[str, Any] = {
        'event': 'combat_entry_diagnostic',
        'combat_number': combat_number,
        'status': 'CAPTURED',
        'differences': mismatch.differences,
        'client_digest': mismatch.client_digest,
        'headless_digest': mismatch.headless_digest,
        'headless_encounter_id': (headless_state.get('combat') or {}).get('encounter_id'),
        'search_encounter_id': (search_state.get('combat') or {}).get('encounter_id'),
    }
    try:
        before_rng = live.mod.rng_snapshot()
        raw_snapshot = getattr(live.mod, 'current_combat_snapshot_raw', None)
        payload = raw_snapshot() if callable(raw_snapshot) else live.mod.current_combat_snapshot()
        response: Dict[str, Any] = {
            'schema': payload.get('schema'),
            'capture_protocol': payload.get('capture_protocol'),
            'snapshot_id': payload.get('snapshot_id'),
            'keys': sorted(payload.keys()),
        }
        snapshot_json = payload.get('snapshot_json')
        if isinstance(snapshot_json, str):
            try:
                room = json.loads(snapshot_json).get('RoomJson')
                room_obj = json.loads(room) if isinstance(room, str) else room
                if isinstance(room_obj, dict):
                    response['room_encounter_id'] = room_obj.get('encounter_id')
                    creatures = room_obj.get('NetState', {}).get('Creatures', [])
                    response['room_enemies'] = [
                        {
                            'id': ((creature.get('monsterId') or {}).get('Entry')
                                  if isinstance(creature, dict) else None),
                            'hp': creature.get('currentHp') if isinstance(creature, dict) else None,
                            'max_hp': creature.get('maxHp') if isinstance(creature, dict) else None,
                        }
                        for creature in creatures
                        if isinstance(creature, dict) and creature.get('playerId') is None
                    ]
            except (TypeError, ValueError, json.JSONDecodeError):
                response['room_parse_error'] = True
        record['authoritative_snapshot_response'] = response
        after_rng = live.mod.rng_snapshot()
        rng_result = compare_rng_snapshots(before_rng, after_rng)
        record['capture_rng_status'] = 'PASS' if rng_result.passed else 'FAIL'
        if not rng_result.passed:
            record['capture_rng_differences'] = rng_result.differences
    except Exception as exc:
        record['status'] = 'CAPTURE_FAILED'
        record['error'] = str(exc)
    report.setdefault('combat_entry_diagnostics', []).append(record)
    log.write(record)


def _reconcile_combat_entry(
    cli: Sts2CliAdapter,
    live: LiveClientBridge,
    client_state: Dict[str, Any],
    mismatch: Any,
    combat_number: int,
    session_dir: Path,
    log: JsonlSessionLog,
    report: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    from controller.human_capture import validate_authoritative_snapshot
    from controller.rng_parity import compare_rng_snapshots

    record = {
        'event': 'combat_entry_intent_mismatch',
        'combat_number': combat_number,
        'initial_status': mismatch.status,
        'status': 'REANCHORING',
        'differences': mismatch.differences,
        'client_digest': mismatch.client_digest,
        'headless_digest': mismatch.headless_digest,
    }
    report.setdefault('combat_entry_reanchors', []).append(record)
    log.write(record)
    try:
        before_rng = live.mod.rng_snapshot()
        raw_snapshot = getattr(live.mod, 'current_combat_snapshot_raw', None)
        payload = raw_snapshot() if callable(raw_snapshot) else live.mod.current_combat_snapshot()
        record['authoritative_snapshot_response'] = {
            'schema': payload.get('schema'),
            'capture_protocol': payload.get('capture_protocol'),
            'snapshot_id': payload.get('snapshot_id'),
            'keys': sorted(payload.keys()),
        }
        if isinstance(payload.get('snapshot_json'), str):
            try:
                room = json.loads(payload['snapshot_json']).get('RoomJson')
                room_obj = json.loads(room) if isinstance(room, str) else room
                encounter = room_obj.get('encounter_id') if isinstance(room_obj, dict) else None
                record['authoritative_snapshot_response']['encounter_id'] = encounter
            except (TypeError, ValueError, json.JSONDecodeError):
                record['authoritative_snapshot_response']['encounter_id'] = None
        snapshot, raw, digest = validate_authoritative_snapshot(payload)
        observed = live.observe()
        after_rng = live.mod.rng_snapshot()
        if gameplay_observation(observed) != gameplay_observation(client_state):
            raise FlowBlocked('Client changed while capturing the combat-entry snapshot')
        capture_rng = compare_rng_snapshots(before_rng, after_rng)
        if not capture_rng.passed:
            raise FlowBlocked(f'Combat snapshot capture changed client RNG: {capture_rng.differences[:3]}')
        if snapshot.get('Seed') != client_state.get('run_id'):
            raise FlowBlocked('Combat snapshot belongs to a different visible run')
        save_path = session_dir / f'combat_entry_{combat_number}.json'
        save_path.write_bytes(raw)
        if hashlib.sha256(save_path.read_bytes()).hexdigest() != digest:
            raise FlowBlocked('Combat snapshot changed while being archived')
        imported = cli.import_combat_snapshot(payload['snapshot_json'], f'live_combat_{combat_number}')
        if imported.get('success') is not True:
            raise FlowBlocked(f'Cannot import native combat snapshot: {imported}')
        restored = cli.restore_combat_snapshot(f'live_combat_{combat_number}', allow_full=False)
        if restored.get('type') == 'error' or restored.get('restore_mode') != 'in_place':
            raise FlowBlocked(f'Combat snapshot could not restore in the current run: {restored}')
        search = cli.get_search_state(timeout_s=10.0).get('combat_state_for_search') or {}
        if not search or restored.get('decision') != 'combat_play':
            raise FlowBlocked('Restored combat has no player decision and search state')
        result = compare_checkpoints(
            client_combat_checkpoint(observed),
            headless_combat_checkpoint(restored, search, str(observed['run_id'])),
        )
        rng_result = compare_rng_snapshots(after_rng, cli.get_rng_snapshot())
        if result.status != 'PASS' or not rng_result.passed:
            raise FlowBlocked(f'Combat entry recheck failed: fields={result.differences[:5]}, '
                              f'rng={rng_result.differences[:3]}')
        record.update(status='REANCHORED_PASS', snapshot_path=str(save_path),
                      snapshot_sha256=digest, rng_status='PASS',
                      rechecked_client_digest=result.client_digest,
                      rechecked_headless_digest=result.headless_digest)
        log.write({**record, 'event': 'combat_entry_reanchored'})
        return restored, search
    except Exception as exc:
        record.update(status='REANCHOR_FAILED', error=str(exc))
        log.write({**record, 'event': 'combat_entry_reanchor_failed'})
        raise


def _check_combat(
    cli: Sts2CliAdapter,
    client_state: Dict[str, Any],
    headless_state: Dict[str, Any],
    run_id: str,
    label: str,
    log: JsonlSessionLog,
    report: Dict[str, Any],
    *,
    search_state: Dict[str, Any] | None = None,
) -> None:
    if not report.get('verification_enabled', True):
        return
    started = time.perf_counter()
    search = search_state or cli.get_search_state(timeout_s=10.0).get("combat_state_for_search") or {}
    if not search:
        raise RuntimeError(f"Incomplete search state at {label}")
    result = compare_checkpoints(
        client_combat_checkpoint(client_state),
        headless_combat_checkpoint(headless_state, search, run_id),
    )
    _record_comparison(result, label, log, report, (time.perf_counter() - started) * 1000)


def _record_comparison(
    result: Any,
    label: str,
    log: JsonlSessionLog,
    report: Dict[str, Any],
    compare_ms: float | None = None,
    *,
    attach_pending_action: bool = True,
) -> None:
    row = {
        "scope": "COVERED_FIELDS_ONLY",
        "checkpoint_type": result.client.get('checkpoint'),
        "compare_ms": compare_ms,
        "label": label,
        "status": result.status,
        "client_digest": result.client_digest,
        "headless_digest": result.headless_digest,
        "difference_count": len(result.differences),
        "differences": result.differences,
        "client": result.client,
        "headless": result.headless,
    }
    latest = None
    if attach_pending_action:
        latest = next((item for item in reversed(report.get("actions", []))
                       if item.get("status") == "completed" and item.get("verification") == "PENDING"), None)
    if latest is not None:
        row['sequence'] = latest['sequence']
        latest["verification"] = "COVERED_FIELDS_MATCH" if result.status == "PASS" else result.status
        latest["checkpoint"] = row
        latest["compare_ms"] = compare_ms
        latest['boundary_verified'] = (result.status == 'PASS'
                                       and latest.get('shadow_completed') is True
                                       and latest.get('rng_verified') is True)
        if latest.get('completion_evidence') == 'native_game_action_and_queue':
            latest['settlement_phase'] = ('VERIFIED' if latest['boundary_verified']
                                          else 'DIVERGED')
        if result.status == 'PASS' and latest.get('shadow_completed') and latest.get('rng_verified'):
            latest['transaction_status'] = 'completed'
        for recovery in report.get('shadow_recoveries') or []:
            if (recovery.get('sequence') == latest['sequence']
                    and recovery.get('status') == 'REPLAYED_RNG_VERIFIED_AWAITING_CHECKPOINT'):
                recovery.update(status='BOUNDARY_VERIFIED' if result.status == 'PASS' else 'CHECKPOINT_FAILED',
                                checkpoint=label)
                log.write({'event': 'shadow_recovery', **recovery})
    report["latest_checkpoint"] = row
    report["parity_checkpoints"].append(row)
    log.write({"event": "parity_checkpoint", **row})
    print(json.dumps({"event": "parity", **row}, ensure_ascii=False), flush=True)
    if result.status != "PASS":
        raise RuntimeError(f"Parity failed at {label}: {result.differences[:5]}")


def _raise_on_headless_error(state: Dict[str, Any], operation: str) -> None:
    if state.get("type") == "error":
        raise RuntimeError(f"Headless failed to {operation}: {state}")


def _write_report(session_dir: Path, report: Dict[str, Any]) -> Dict[str, Any]:
    timing: Dict[str, Any] = {}
    write_json(session_dir / "run_report.json", report, compact=True, timing=timing)
    return timing


def _advance_shadow(cli, action, payload, log, report):
    """Keep recovery failures closed and preserve their first execution evidence."""
    try:
        return _advance_shadow_impl(cli, action, payload, log, report)
    except Exception as exc:
        row = (report.get('actions') or [{}])[-1]
        for recovery in report.get('shadow_recoveries') or []:
            if recovery.get('sequence') == row.get('sequence') and recovery.get('status') == 'REPLAYING':
                recovery.update(status='REPLAY_FAILED', error=str(exc),
                                stderr_tail=list(getattr(cli, '_stderr_tail', [])))
                log.write({'event': 'shadow_recovery', **recovery})
        raise


def _advance_shadow_impl(cli, action, payload, log, report):
    started = time.perf_counter()
    row = (report.get('actions') or [{}])[-1]
    timing = row.setdefault('timing_breakdown', {})
    transaction = row.get('transaction') if isinstance(row.get('transaction'), dict) else {}
    telemetry = transaction.get('telemetry') if isinstance(transaction.get('telemetry'), dict) else {}
    try:
        shadow_timeout_s = float(telemetry.get('shadow_timeout_ms', 20000.0)) / 1000.0
    except (TypeError, ValueError):
        shadow_timeout_s = 20.0
    if shadow_timeout_s <= 0:
        raise ValueError('shadow_timeout_ms must be positive')
    timing['shadow_timeout_ms'] = round(shadow_timeout_s * 1000.0, 3)
    capture_rng = getattr(cli, 'get_rng_snapshot', None)
    rng_started = time.perf_counter()
    shadow_rng_before = capture_rng() if callable(capture_rng) else None
    timing['shadow_rng_before_ms'] = round((time.perf_counter() - rng_started) * 1000.0, 3)
    if shadow_rng_before is not None:
        row['shadow_rng_before'] = shadow_rng_before
    try:
        try:
            action_started = time.perf_counter()
            try:
                result = cli.action(action, payload, timeout_s=shadow_timeout_s)
            finally:
                timing['shadow_action_ms'] = round(
                    (time.perf_counter() - action_started) * 1000.0, 3
                )
            backend_error = result.get('type') == 'error'
            row['shadow_completed'] = not backend_error
            if backend_error:
                row['shadow_response'] = result
                row['shadow_stderr_tail'] = list(getattr(cli, '_stderr_tail', []))
                row['shadow_execution_error'] = result.get('message') or repr(result)
                log.write({'event': 'shadow_execution_error', 'sequence': row.get('sequence'),
                           'transaction': row.get('transaction'), 'response': result,
                           'stderr_tail': row['shadow_stderr_tail']})
            if shadow_rng_before is not None:
                rng_started = time.perf_counter()
                shadow_rng_after = capture_rng()
                timing['shadow_rng_after_ms'] = round((time.perf_counter() - rng_started) * 1000.0, 3)
                row['shadow_rng_after'] = shadow_rng_after
                from controller.rng_parity import compare_rng_transitions, rng_counter_delta
                parity_started = time.perf_counter()
                row['shadow_rng_delta'] = rng_counter_delta(shadow_rng_before, shadow_rng_after)
                client_before = row.get('client_rng_before')
                client_after = row.get('client_rng_after')
                if (isinstance(client_before, dict) and isinstance(client_after, dict)
                        and row.get('native_action_phase') != 'awaiting_input'):
                    audit = compare_rng_transitions(
                        client_before, client_after, shadow_rng_before, shadow_rng_after
                    )
                    timing['rng_parity_compare_ms'] = round(
                        (time.perf_counter() - parity_started) * 1000.0, 3
                    )
                    row['rng_parity'] = audit
                    log.write({
                        'event': 'rng_parity', 'sequence': row.get('sequence'),
                        'action': action, **audit,
                    })
                    row['rng_verified'] = audit['status'] == 'PASS'
                    if audit['status'] != 'PASS' and not backend_error:
                        raise RuntimeError(
                            f"RNG parity failed at sequence {row.get('sequence')} action {action}: "
                            f"{audit['differences'][:3]}"
                        )
            _raise_on_headless_error(result, f'execute shadow {action}')
            return result
        except TimeoutError:
            recovery_started = time.perf_counter()
            recoveries = report.setdefault('shadow_recoveries', [])
            if len(recoveries) >= 2 or not report.get('anchor_save'):
                raise
            record = {'action': action, 'sequence': (report.get('actions') or [{}])[-1].get('sequence'),
                      'status': 'REPLAYING', 'stderr_tail': list(cli._stderr_tail)}
            recoveries.append(record)
            log.write({'event': 'shadow_recovery', **record})
            # Prefer the newest verified client-authoritative anchor in this
            # session. The opening anchor predates rewards, purchases and
            # client-only rooms, so replaying from it can silently lose cards
            # or potions after a timeout.
            current_sequence = int(record.get('sequence') or 0)
            verified = [row for row in report.get('reanchors') or []
                        if row.get('status') == 'REANCHORED_PASS'
                        and row.get('save_path')
                        and int(row.get('action_sequence') or 0) < current_sequence]
            recovery_anchor = report['anchor_save']
            recovery_anchor_sequence = 0
            if verified:
                anchor = max(verified, key=lambda row: int(row.get('action_sequence') or 0))
                recovery_anchor = anchor['save_path']
                recovery_anchor_sequence = int(anchor.get('action_sequence') or 0)
            record['recovery_anchor'] = recovery_anchor
            record['recovery_anchor_sequence'] = recovery_anchor_sequence
            cli.stop()
            cli.start()
            anchor_bytes = Path(recovery_anchor).read_bytes()
            if verified and hashlib.sha256(anchor_bytes).hexdigest() != anchor.get('save_sha256'):
                raise RuntimeError('Recovery anchor content no longer matches the verified checkpoint')
            # Map anchors are already settled; reopening their previous room
            # can repeat event/reward RNG. Opening anchors keep their own mode.
            state = cli.load_save(recovery_anchor, resume_room=(
                False if verified else bool(report.get('anchor_room'))
            ))
            _raise_on_headless_error(state, 'reload recovery anchor')

            def replay(data):
                nonlocal state
                if data.get('resume_report'):
                    replay(json.loads(Path(data['resume_report']).read_text(encoding='utf-8')))
                if data.get('recovered_pending_action'):
                    recovered = data['recovered_pending_action']
                    translated = _explicit_shadow_command(recovered)
                    if translated is None:
                        raise RuntimeError('Recovered pending action has no shadow command')
                    state = cli.action(*translated, timeout_s=20)
                    _raise_on_headless_error(state, 'replay reconciled pending action')
                for recovered in data.get('room_replay') or []:
                    translated = _explicit_shadow_command(recovered)
                    if translated is None:
                        raise RuntimeError('Room replay record has no shadow command')
                    state = cli.action(*translated, timeout_s=20)
                    _raise_on_headless_error(state, 'replay room recovery input')
                require_transaction = int(data.get('schema_version') or 0) >= 3
                for row in data.get('actions') or []:
                    if row.get('recovered_parent_reference'):
                        continue
                    if int(row.get('sequence') or 0) <= recovery_anchor_sequence:
                        continue
                    if row.get('status') != 'completed':
                        continue
                    translated = _recorded_shadow_command(
                        row,
                        state,
                        require_transaction=require_transaction,
                    )
                    if translated:
                        state = cli.action(*translated, timeout_s=20)
                        _raise_on_headless_error(state, 'replay recovery transaction')
            replay(report)
            # A recovered map action can complete in the engine after its
            # response was lost. The replay response may still describe the
            # old map boundary, while the native combat is already active.
            # Refresh the authoritative search boundary before returning to
            # the flow runner; never let client COMBAT pair with shadow MAP.
            if action == 'select_map_node':
                refreshed = cli.get_search_state(timeout_s=10.0)
                if refreshed.get('type') != 'error':
                    recovered_search = refreshed.get('combat_state_for_search') or {}
                    if recovered_search.get('decision') in {'combat_play', 'card_select'}:
                        state = recovered_search
            timing['shadow_recovery_ms'] = round(
                (time.perf_counter() - recovery_started) * 1000.0, 3
            )
            from controller.rng_parity import compare_rng_snapshots
            expected_rng = row.get('client_rng_after')
            if not isinstance(expected_rng, dict):
                raise RuntimeError('Recovery has no authoritative post-action RNG snapshot')
            recovered_rng = cli.get_rng_snapshot()
            recovered_audit = compare_rng_snapshots(expected_rng, recovered_rng)
            row['recovery_rng_parity'] = {
                'status': recovered_audit.status, 'differences': recovered_audit.differences,
            }
            row['shadow_completed'] = True
            row['rng_verified'] = recovered_audit.passed
            if not recovered_audit.passed:
                record['status'] = 'REPLAY_FAILED'
                raise RuntimeError(f'Recovered shadow RNG differs before continuation: {recovered_audit.differences[:3]}')
            record['status'] = 'REPLAYED_RNG_VERIFIED_AWAITING_CHECKPOINT'
            log.write({'event': 'shadow_recovery', **record})
            return state
    finally:
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        timing['shadow_total_ms'] = elapsed
        if report.get("actions"):
            report["actions"][-1]["shadow_ms"] = elapsed
        log.write({"event": "shadow_advance", "action": action, "shadow_ms": elapsed,
                   "sequence": (report.get('actions') or [{}])[-1].get('sequence')})


def _client_status(state: Dict[str, Any]) -> Dict[str, Any]:
    run = state.get("run") or {}
    return {
        "screen": state.get("screen"),
        "run_id": state.get("run_id"),
        "floor": run.get("floor"),
        "hp": run.get("current_hp"),
        "max_hp": run.get("max_hp"),
        "gold": run.get("gold"),
        "turn": state.get("turn"),
    }


if __name__ == "__main__":
    main()
