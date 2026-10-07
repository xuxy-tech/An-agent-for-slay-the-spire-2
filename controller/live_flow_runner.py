from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from controller.interaction_state import FlowContext, interaction_at_boundary

from controller.interaction_flow import FlowBlocked, InteractionFlow, ProgressGuard, shop_inventories_match
from controller.interaction_state import InteractionKind as K
from controller.transaction_runtime import execute_transaction
from controller.search.combat_search import CombatWorkerPool
from controller.search.worker_scaling import AdaptiveWorkerController
from controller.deck_profile import load_deck_profile
from controller.live_client_bridge import StaleClientStateError, gameplay_observation


def _deck_identity(state):
    """Return the protocol-level deck identity for client/shadow comparison."""

    if not isinstance(state, dict):
        return None
    run = state.get('run') or {}
    player = state.get('player') or {}
    deck = run.get('deck') if isinstance(run, dict) else None
    if deck is None:
        deck = player.get('deck') if isinstance(player, dict) else None
    if not isinstance(deck, list):
        return None
    result = []
    for card in deck:
        if not isinstance(card, dict):
            return None
        result.append((card.get('card_id', card.get('id')), bool(card.get('upgraded'))))
    return tuple(result)


def _confirm_map_boundary(live, state, *, delay_s=0.35, timeout_s=3.0):
    """Let transient room overlays surface before authorizing map travel."""
    deadline = time.monotonic() + timeout_s
    stable_key = None
    while time.monotonic() < deadline:
        time.sleep(delay_s)
        observed = live.observe()
        if observed.get('screen') != 'MAP':
            return observed
        if 'choose_map_node' in (observed.get('available_actions') or []):
            key = json.dumps(gameplay_observation(observed), sort_keys=True,
                             ensure_ascii=False, separators=(',', ':'))
            if key == stable_key:
                return observed
            stable_key = key
        state = observed
    raise FlowBlocked('Map did not reach a stable client decision boundary')


def _authoritative_client_save(live):
    capture = getattr(getattr(live, 'mod', None), 'exact_save', None)
    if not callable(capture):
        raise FlowBlocked(
            'Visible client does not expose the authoritative exact-save endpoint; '
            'refusing to re-anchor from asynchronous current_run.save'
        )
    payload = capture()
    if not isinstance(payload, dict) or not isinstance(payload.get('save_json'), str):
        raise FlowBlocked('Visible client returned an incomplete authoritative exact save')
    rng_snapshot = getattr(getattr(live, 'mod', None), 'rng_snapshot', None)
    if not callable(rng_snapshot):
        raise FlowBlocked(
            'Visible client does not expose the authoritative RNG snapshot required '
            'to validate an exact-save re-anchor'
        )
    payload = dict(payload)
    payload['client_rng'] = rng_snapshot()
    return payload


def run_interactions(cli, cli_cfg, live, client_state, headless_state, args, repo_root, log, report, control):
    # Keep the existing tactical policy behind one boundary. Scene orchestration
    # never changes search configuration or scores.
    from scripts.live_run_demo import (
        _advance_shadow,
        _check_map,
        _play_combat,
        _reanchor_from_official_save,
        _write_report,
    )
    save_report = getattr(args, 'save_report', None)
    if save_report is None:
        save_report = lambda: _write_report(args.session_dir, report)
    saved_context = dict(report.get('flow_context') or {})
    if 'location' in saved_context:
        saved_context['location'] = tuple(saved_context['location'])
    deck_profile = load_deck_profile(getattr(args, 'deck_profile', None))
    flow = InteractionFlow(repo_root, FlowContext(**saved_context), deck_profile=deck_profile)
    guard = ProgressGuard()
    completed = int(report.get('completed_combat_count') or 0)
    seen_interactions = set()
    open_segments = [s for s in report.get('client_only_segments', [])
                     if s.get('status') in {'AWAITING_BOUNDARY', 'REANCHORING'}]
    if len(open_segments) > 1:
        raise FlowBlocked('Multiple unresolved client-authoritative segments')
    pending_reanchor = ({'save_path': Path(report['anchor_save']).resolve(),
                         'segment': open_segments[0]} if open_segments else None)
    while True:
        control.checkpoint('running', phase='observe', completed_combat_count=completed)
        if client_state.get('screen') == 'MAP':
            client_state = _confirm_map_boundary(live, client_state)
        current = flow.classify(client_state, headless_state)
        # A resumed/paced observation can reach the boundary before the next
        # action. Reconcile BEFORE comparing the stale shadow or choosing a move.
        if pending_reanchor is not None and interaction_at_boundary(
                current.kind, pending_reanchor['segment'].get('reanchor_boundary', 'map')):
            segment = pending_reanchor['segment']
            try:
                exact_save = _authoritative_client_save(live)
                headless_state, reanchor = _reanchor_from_official_save(
                    cli, client_state, pending_reanchor['save_path'],
                    (0, 0, exact_save['sha256']), log, report, segment['segment_id'],
                    authoritative_save_json=exact_save['save_json'],
                    authoritative_client_rng=exact_save['client_rng'])
            except Exception as exc:
                segment.update(status='REANCHOR_FAILED', error=str(exc))
                save_report()
                raise
            segment.update(status='BOUNDARY_VERIFIED', end_sequence=live.sequence,
                           reanchor_save_path=reanchor['save_path'],
                           reanchor_save_sha256=reanchor['save_sha256'])
            flow.invalidate_reward_mapping()
            flow.context.reanchor_required = False
            flow.context.shop_reanchor_required = False
            pending_reanchor = None
            report['flow_context'] = asdict(flow.context)
            log.write({'event': 'client_only_segment_verified', **segment})
            save_report()
            current = flow.classify(client_state, headless_state)
        signature = (current.scene, current.kind.value, current.stage)
        description = current.to_dict()
        description['stack'] = list(flow.context.stack)
        report['interaction'] = description
        report['flow_context'] = asdict(flow.context)
        control.publish('running', phase=current.kind.value, interaction=description,
                        client={'screen': client_state.get('screen'), **(client_state.get('run') or {})})
        if signature not in seen_interactions:
            seen_interactions.add(signature)
            report.setdefault('state_coverage', []).append(description)
        log.write({'event': 'interaction_state', **description,
                   'flow_context': asdict(flow.context)})
        if current.kind == K.TERMINAL:
            victory = (client_state.get('game_over') or {}).get('is_victory')
            if type(victory) is not bool:
                raise FlowBlocked('Game-over outcome is unavailable; refusing to infer defeat')
            report['status'] = 'VICTORY' if victory is True else 'DEFEAT'
            report['terminal_client'] = client_state
            control.publish('completed', phase='game_over', outcome=report['status'], pending_action=None)
            break
        if current.kind == K.MAP and args.target_combats > 0 and completed >= args.target_combats:
            report['status'] = 'COMPLETED'
            control.publish('completed', phase='target_reached', completed_combat_count=completed, pending_action=None)
            break
        if current.stage == 'waiting':
            guard.waiting(current)
            control.publish('running', phase='waiting', waiting_for=current.reason)
            time.sleep(0.1)
            client_state = live.observe()
            continue
        if current.stage == 'blocked':
            raise FlowBlocked(current.reason)
        # A map anchor cannot represent an open shop. Compare normalized
        # stock, and keep genuine stocked-item mismatches visible.
        if current.kind == K.SHOP_INVENTORY and (
            headless_state.get('decision') != 'shop'
            or not shop_inventories_match(client_state, headless_state)
        ):
            raise FlowBlocked('Shop state mismatch after normalized stock comparison; '
                              'map-only recovery cannot restore an open shop')
        if current.kind == K.COMBAT and pending_reanchor is not None:
            raise FlowBlocked(
                'Visible run reached combat before the client-authoritative segment could re-anchor at a map boundary'
            )
        if current.kind == K.COMBAT or (client_state.get('in_combat') and headless_state.get('decision') == 'card_select'):
            worker_pool = None
            pool_started = time.perf_counter()
            worker_controller = AdaptiveWorkerController(
                hardware_max=max(1, int(getattr(args, 'max_workers', 2))),
                mode=str(getattr(args, 'worker_mode', 'fixed')),
            )
            if cli_cfg is not None and not bool(getattr(args, 'disable_worker_pool', False)):
                worker_pool = CombatWorkerPool(cli_cfg)
                worker_pool.prewarm(worker_controller.current_workers)
            try:
                _, client_state, headless_state = _play_combat(
                    cli, cli_cfg, live, client_state, headless_state, str(client_state.get('run_id') or ''),
                    completed + 1, args, log, report, control, worker_pool=worker_pool,
                    worker_controller=worker_controller)
            finally:
                if worker_pool is not None:
                    pool_stats = worker_pool.stats()
                    pool_stats['combat_number'] = completed + 1
                    pool_stats['lifetime_ms'] = round((time.perf_counter() - pool_started) * 1000, 3)
                    report.setdefault('worker_pool_combats', []).append(pool_stats)
                    log.write({'event': 'worker_pool_closed', **pool_stats})
                    worker_pool.close()
            completed += 1
            report['completed_combat_count'] = completed
            log.write({'event': 'combat_count', 'count': completed})
            flow.context.operation = None
            flow.context.reward_card_resolved = False
            log.write({'event': 'flow_context', 'value': asdict(flow.context)})
            continue
        current, command = flow.choose(client_state, headless_state)
        if command is None:
            raise FlowBlocked(f'No action for {current.kind.value}')
        if current.kind == K.MAP and command.telemetry.get('requires_reanchor'):
            prior_pending = pending_reanchor
            exact_save = _authoritative_client_save(live)
            save_path = Path(report['anchor_save']).resolve()
            segment = {
                'segment_id': len(report.setdefault('client_only_segments', [])) + 1,
                'kind': 'pre_action_map_reconciliation',
                'status': 'REANCHORING',
                'start_sequence': live.sequence + 1,
                'source_save_path': 'client://exact-save',
                'reanchor_boundary': command.telemetry.get('reanchor_boundary', 'map'),
                'source_save_sha256': exact_save.get('sha256'),
                'reason': command.telemetry.get('client_authoritative_reason')
                          or 'headless_map_step_unavailable',
            }
            if prior_pending is not None:
                segment['completes_segment_id'] = prior_pending['segment'].get('segment_id')
            report['client_only_segments'].append(segment)
            log.write({'event': 'pre_action_map_reanchor_started', **segment})
            headless_state, reanchor = _reanchor_from_official_save(
                cli,
                client_state,
                save_path,
                (-1, -1, ''),
                log,
                report,
                segment['segment_id'],
                authoritative_save_json=exact_save['save_json'],
                authoritative_client_rng=exact_save['client_rng'],
            )
            segment.update(
                status='BOUNDARY_VERIFIED',
                reanchor_save_path=reanchor['save_path'],
                reanchor_save_sha256=reanchor['save_sha256'],
            )
            if prior_pending is not None:
                prior_segment = prior_pending['segment']
                prior_segment.update(
                    status='BOUNDARY_VERIFIED',
                    end_sequence=live.sequence,
                    completed_by_segment_id=segment['segment_id'],
                    reanchor_save_path=reanchor['save_path'],
                    reanchor_save_sha256=reanchor['save_sha256'],
                )
                pending_reanchor = None
            flow.invalidate_reward_mapping()
            flow.context.shop_reanchor_required = False
            flow.context.reanchor_required = False
            current, command = flow.choose(client_state, headless_state)
            if command is None or command.shadow is None or command.telemetry.get('requires_reanchor'):
                raise FlowBlocked('Map re-anchor did not produce a mirrored map decision')
            log.write({'event': 'pre_action_map_reanchor_verified', **segment})
            save_report()
        # A client-authoritative mutation makes the previous shadow stale
        # until the declared boundary. Never silently mirror against it.
        if pending_reanchor is not None and command.shadow is not None:
            raise FlowBlocked(
                'Mirrored action requested before client-authoritative boundary '
                f"(reason={pending_reanchor['segment'].get('reason')!r})"
            )
        mapping = flow.context.reward_set_mapping
        if (current.kind == K.REWARD and command.shadow is not None
                and mapping is not None and not mapping['verified']):
            from controller.engine_parity import compare_checkpoints, reward_lifecycle_checkpoint
            from controller.rng_parity import compare_rng_snapshots
            identity = (f"{mapping['run_id']}:{mapping['reanchor_generation']}:"
                        f"{mapping['occurrence']}")
            parity = compare_checkpoints(
                reward_lifecycle_checkpoint(client_state, client=True,
                                            mapped_set_identity=identity),
                reward_lifecycle_checkpoint(headless_state, client=False,
                                            mapped_set_identity=identity))
            rng_reader = getattr(getattr(live, 'mod', None), 'rng_snapshot', None)
            if not callable(rng_reader):
                raise FlowBlocked('Native reward mapping requires client RNG snapshot')
            rng = compare_rng_snapshots(rng_reader(), cli.get_rng_snapshot())
            if parity.status != 'PASS' or not rng.passed:
                raise FlowBlocked('Native reward mapping requires complete reward and RNG parity')
            mapping['verified'] = True
            log.write({'event': 'native_reward_set_mapping', **mapping,
                       'basis': 'complete_ordered_offer_run_and_rng_parity'})
            report['flow_context'] = asdict(flow.context)
            save_report()
        guard.before(client_state, command)
        before = client_state
        shadow_before = headless_state
        telemetry = dict(command.telemetry)
        telemetry.update(
            interaction=description,
            completion_condition=command.completion,
        )
        try:
            executed = execute_transaction(
                command,
                live=live,
                client_state=before,
                log=log,
                report=report,
                advance_shadow=lambda action, payload, tx_log, tx_report: _advance_shadow(
                    cli, action, payload, tx_log, tx_report
                ),
                operation_label=f'advance {current.kind.value}',
                telemetry=telemetry,
            )
        except StaleClientStateError as exc:
            # Nothing was submitted. The client may legitimately advance an
            # event while the operator viewing interval is active, so discard
            # the obsolete decision and classify the fresh state again.
            client_state = exc.observed_state
            guard = ProgressGuard()
            log.write({
                'event': 'stale_action_replanned',
                'sequence': (report.get('actions') or [{}])[-1].get('sequence'),
                'screen': client_state.get('screen'),
                'event_id': (client_state.get('event') or {}).get('event_id'),
            })
            continue
        client_state = executed.client_state
        if executed.shadow_state is not None:
            headless_state = executed.shadow_state
        # Some native event rewards are applied atomically when the visible
        # reward flow closes. The shadow may expose the same reward flags while
        # omitting the automatic card additions, so the divergence first
        # becomes observable at the following room boundary. Start the existing
        # authoritative reanchor segment immediately after a mirrored reward
        # close instead of carrying a stale shadow into the next map decision.
        if (command.shadow is not None
                and command.client.action in {'collect_rewards_and_proceed', 'proceed'}
                and client_state.get('screen') in {'MAP', 'EVENT', 'SHOP'}):
            client_deck = _deck_identity(client_state)
            shadow_deck = _deck_identity(headless_state)
            if client_deck is not None and shadow_deck is not None and client_deck != shadow_deck:
                command.telemetry['requires_reanchor'] = True
                command.telemetry['reanchor_boundary'] = 'map'
                command.telemetry['client_authoritative_reason'] = (
                    'native_reward_close_added_cards_missing_from_shadow')
        flow.verify_native_reward_transition(
            command, before, shadow_before, client_state, headless_state)
        flow.verify_crystal_sphere_transition(
            command, before, shadow_before, client_state, headless_state)
        if (command.shadow is not None and
                (before.get('screen') == 'CRYSTAL_SPHERE'
                 or client_state.get('screen') == 'CRYSTAL_SPHERE')):
            from controller.rng_parity import compare_rng_snapshots
            rng_reader = getattr(getattr(live, 'mod', None), 'rng_snapshot', None)
            if not callable(rng_reader):
                raise FlowBlocked('Crystal Sphere requires client RNG snapshot')
            rng = compare_rng_snapshots(rng_reader(), cli.get_rng_snapshot())
            if not rng.passed:
                raise FlowBlocked('Crystal Sphere RNG diverged between client and shadow')
        if client_state.get('in_combat') and headless_state.get('decision') not in {
            'combat_play', 'card_select',
        }:
            raise FlowBlocked(
                'Client entered combat but shadow did not reach a combat decision '
                f"(shadow_decision={headless_state.get('decision')!r})"
            )
        # A client-only reconciliation segment starts only after the visible
        # action completed.  Creating it before submission leaves a false
        # AWAITING_BOUNDARY segment when the stale-action gate cancels without
        # sending anything, which makes an otherwise intact run unrecoverable.
        if command.telemetry.get('requires_reanchor') and pending_reanchor is None:
            save_path = Path(report['anchor_save']).resolve()
            segment = {
                'segment_id': len(report.setdefault('client_only_segments', [])) + 1,
                'kind': 'client_authoritative_reconciliation',
                'status': 'AWAITING_BOUNDARY',
                'start_sequence': (report.get('actions') or [{}])[-1].get('sequence'),
                'source_save_path': 'client://exact-save',
                'reanchor_boundary': command.telemetry.get('reanchor_boundary', 'map'),
                'reason': command.telemetry.get('client_authoritative_reason')
                          or 'visible_and_headless_state_differ',
            }
            report['client_only_segments'].append(segment)
            pending_reanchor = {
                'save_path': save_path,
                'segment': segment,
            }
            log.write({'event': 'client_only_segment_started', **segment})
            save_report()
        flow.complete(command, before, client_state)
        report['flow_context'] = asdict(flow.context)
        log.write({'event': 'flow_context', 'value': report['flow_context']})
        after_kind = flow.classify(client_state, headless_state).kind
        if pending_reanchor is not None:
            boundary = pending_reanchor['segment'].get('reanchor_boundary', 'map')
            boundary_reached = interaction_at_boundary(after_kind, boundary)
        else:
            boundary_reached = False
        if pending_reanchor is not None and boundary_reached:
            segment = pending_reanchor['segment']
            control.publish(
                'running',
                phase='reanchor',
                completed_combat_count=completed,
                interaction=flow.classify(client_state, headless_state).to_dict(),
            )
            try:
                exact_save = _authoritative_client_save(live)
                headless_state, reanchor = _reanchor_from_official_save(
                    cli,
                    client_state,
                    pending_reanchor['save_path'],
                    (0, 0, exact_save['sha256']),
                    log,
                    report,
                    segment['segment_id'],
                    authoritative_save_json=exact_save['save_json'],
                    authoritative_client_rng=exact_save['client_rng'],
                )
            except Exception as exc:
                segment['status'] = 'REANCHOR_FAILED'
                segment['error'] = str(exc)
                log.write({'event': 'client_only_segment_failed', **segment})
                save_report()
                raise
            segment.update(
                status='BOUNDARY_VERIFIED',
                end_sequence=(report.get('actions') or [{}])[-1].get('sequence'),
                source_save_sha256=exact_save.get('sha256'),
                reanchor_save_path=reanchor['save_path'],
                reanchor_save_sha256=reanchor['save_sha256'],
            )
            flow.invalidate_reward_mapping()
            flow.context.shop_reanchor_required = False
            flow.context.reanchor_required = False
            report['flow_context'] = asdict(flow.context)
            log.write({'event': 'client_only_segment_verified', **segment})
            pending_reanchor = None
            save_report()
        if current.kind == K.MAP:
            report['map_choices'].append({'combat_number': completed + 1, **telemetry})
        if current.kind == K.REWARD_CARD:
            report['rewards'].append({'combat_number': completed, **telemetry,
                                      'action': command.shadow_action, 'payload': command.shadow_args or {}})
        if current.kind in {K.REWARD, K.REWARD_CARD}:
            before_run, after_run = before.get('run') or {}, client_state.get('run') or {}
            receipt = {'event': 'reward_receipt', 'sequence': live.sequence,
                       'action': command.client.action, 'item_type': telemetry.get('reward_type',
                           'Card' if current.kind == K.REWARD_CARD else 'Finish'),
                       'gold_before': before_run.get('gold'), 'gold_after': after_run.get('gold'),
                       'potions_before': before_run.get('potions'), 'potions_after': after_run.get('potions'),
                       'skipped': telemetry.get('skipped_rewards', [])}
            report['actions'][-1]['reward_receipt'] = receipt
            log.write(receipt)
        verified_kind = flow.classify(client_state, headless_state).kind
        if (command.shadow is not None and verified_kind in {K.REWARD, K.REWARD_CARD}
                and flow.context.reward_set_mapping is not None):
            from controller.engine_parity import compare_checkpoints, reward_lifecycle_checkpoint
            from scripts.live_run_demo import _record_comparison
            mapping = flow.context.reward_set_mapping
            mapped_identity = None
            if mapping is not None:
                client_set_id = (client_state.get('reward') or {}).get('reward_set_id')
                shadow_set_id = headless_state.get('reward_set_id')
                shadow_set_omitted_in_card_boundary = (
                    shadow_set_id is None
                    and headless_state.get('decision') == 'card_reward')
                if (client_set_id != mapping['client_set_id']
                        or (shadow_set_id != mapping['shadow_set_id']
                            and not shadow_set_omitted_in_card_boundary)):
                    raise FlowBlocked('Native reward set changed after mapped reward action')
                mapped_identity = (f"{mapping['run_id']}:"
                                   f"{mapping['reanchor_generation']}:"
                                   f"{mapping['occurrence']}")
            comparison = compare_checkpoints(
                reward_lifecycle_checkpoint(client_state, client=True,
                                            mapped_set_identity=mapped_identity),
                reward_lifecycle_checkpoint(headless_state, client=False,
                                            mapped_set_identity=mapped_identity))
            _record_comparison(comparison, f'flow_{live.sequence}_rewards', log, report)
        if verified_kind == K.MAP:
            _check_map(cli, client_state, headless_state, f'flow_{live.sequence}_map', log, report)
        action_record = (report.get('actions') or [None])[-1]
        if verified_kind == K.COMBAT:
            # The existing first combat checkpoint in _play_combat is the
            # verification boundary. Do not insert presentation pacing before it.
            if isinstance(action_record, dict):
                action_record['pacing_status'] = 'pending_combat_checkpoint'
        else:
            live.pace_after_verification(
                verification=(
                    f'{verified_kind.value}_checkpoint'
                    if verified_kind == K.MAP else 'settled_shadow_transaction'
                ),
                record=action_record,
            )
            # Pacing is deliberately outside settlement and verification. It
            # also makes the cached observation old, so never carry that state
            # into the next policy decision.
            client_state = live.observe()
        save_report()
    return client_state, headless_state
