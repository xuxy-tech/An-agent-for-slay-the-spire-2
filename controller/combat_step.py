#!/usr/bin/env python3
"""Shared per-step combat decision primitive.

ONE frontend-agnostic implementation of "given a combat_play state, decide the
next action". Visible and headless callers use the same snapshot preparation,
scheduling heuristic, action tree, plan reuse, retry, fallback, and action
resolution semantics. A visible client may provide input values such as seed
and state, and may verify the result, but it must not select another search
policy.

Helper functions (build_player_overrides, sanitize_snapshot_json_for_search,
should_parallelize_combat_search, is_failed_root_search, resolve_planned_action,
choose_combat_fallback, summarize_combat_action) are
imported lazily from controller.run_agent to avoid a module-load cycle — the
same pattern fight_record already uses.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from controller.search.combat_search import (
    CombatSearcher,
    CombatSpec,
    RootStateMismatchError,
    should_expand_potions_for_state,
)
from controller.search.actions import SearchAction, available_actions_from_search_state
from controller.combat_scoring import DEFAULT_SCORE_MODE, CombatScoring, active_model, stage_for_floor
from controller.search.state_cache import (
    AUTOMATIC_POTION_IDS,
    UNMODELED_SEARCH_POTION_IDS,
    canonicalize_search_state_for_plan_reuse,
    diff_plan_reuse_states,
    hash_search_state_for_plan_reuse,
)


DEFAULT_TURN_ACTION_CAP = 8
DEFAULT_LIVE_SEARCH_BUDGET_MS = 20000.0


@dataclass
class CombatStepConfig:
    """Inputs and resource limits for the canonical combat decision path."""
    cli_cfg: Any                           # CliConfig (searcher's first positional)
    spec: CombatSpec                       # character/encounter/seed/ascension/lang
    depth: int = DEFAULT_TURN_ACTION_CAP
    chance_depth: int = 1
    score_mode: str = DEFAULT_SCORE_MODE
    max_workers: int = 4
    reuse_cli_processes: bool = False
    # Resource-policy inputs. They come from run configuration, never from
    # whether a visible client happens to be attached.
    user_parallel: bool = False
    floor: Optional[int] = None
    room_type: Optional[str] = None        # "Elite"/"Boss"/... gates potion expansion
    # optional plumbing
    worker_pool: Any = None
    leaf_dump_sink: Any = None
    leaf_dump_rate: float = 0.0
    capture_root_topk: int = 0
    max_search_ms: float = 0.0
    search_mode: str = 'beam'
    beam_width: int = 8
    evaluator_coefficients: Dict[str, float] = field(default_factory=dict)
    scorer_stage: Optional[str] = None
    scorer_model: Optional[Dict[str, Any]] = None


@dataclass
class PlanState:
    """Carries cached-plan continuity across canonical combat steps."""
    sequence: List[SearchAction] = field(default_factory=list)
    expected_state_hashes: List[str] = field(default_factory=list)
    expected_state_keys: List[Dict[str, Any]] = field(default_factory=list)
    origin_reason: Optional[str] = None
    origin_audit: Dict[str, Any] = field(default_factory=dict)
    origin_score_explanation: Dict[str, Any] = field(default_factory=dict)
    scorer_identity: Optional[Dict[str, Any]] = None


@dataclass
class CombatStepResult:
    action: str
    payload: Dict[str, Any]
    chosen: Optional[SearchAction]
    chosen_summary: Dict[str, Any]
    reused_plan: bool = False
    search_failed: bool = False
    raw_retry_used: bool = False
    raw_retry_recovered: bool = False
    fell_back: bool = False
    search_empty: bool = False             # retained for trace-schema compatibility
    search_score: Optional[float] = None
    leaf_state: Optional[Dict[str, Any]] = None
    # search telemetry (set only when a fresh search ran; None/empty on plan reuse,
    # mirroring run_agent's `... if step_searcher is not None else None`).
    ran_search: bool = False
    parallel: bool = False
    nodes: Optional[int] = None
    root_candidates: List[Dict[str, Any]] = field(default_factory=list)
    searcher_timing: Dict[str, Any] = field(default_factory=dict)
    searcher_timing_summary: Dict[str, Any] = field(default_factory=dict)
    # exported root snapshot (set when a fresh search ran) — lets the caller's
    # apply path import/restore the exact searched state before committing.
    root_snapshot_id: Optional[str] = None
    root_snapshot_json: Optional[str] = None
    timing: Dict[str, float] = field(default_factory=dict)
    search_meta: Dict[str, Any] = field(default_factory=dict)
    decision_reason: str = 'highest_score'
    decision_audit: Dict[str, Any] = field(default_factory=dict)
    score_explanation: Dict[str, Any] = field(default_factory=dict)
    # Set when a cached plan no longer matches the observed intermediate state.
    plan_diverged: Optional[Dict[str, Any]] = None


def _helpers():
    """Lazy import of run_agent's combat helpers (avoids module-load cycle)."""
    from controller import run_agent as ra
    return ra


def _clear_plan(plan: PlanState) -> None:
    plan.sequence = []
    plan.expected_state_hashes = []
    plan.expected_state_keys = []
    plan.origin_reason = None
    plan.origin_audit = {}
    plan.origin_score_explanation = {}
    plan.scorer_identity = None


def _turn_space_coverage_ratio(expanded: int, known_unexpanded: int) -> float:
    known_total = expanded + known_unexpanded
    return expanded / known_total if known_total else 0.0


def _plan_origin_has_full_coverage(audit: Dict[str, Any]) -> bool:
    """Only reuse a continuation when its source search saw every candidate edge."""
    turn_space = (audit or {}).get('turn_space') or {}
    # Beam search is selective by design, so it cannot claim exhaustive tree
    # coverage.  A selected beam line is still reusable when the searcher
    # explicitly marked it safe; each reused action is re-resolved against the
    # live state and the plan is discarded on the first mismatch.
    if turn_space.get('beam_plan_reuse_safe'):
        return True
    if not turn_space:
        # Legacy/manual plans predate coverage telemetry; preserve compatibility.
        return True
    if turn_space.get('topology_exhaustive') is not None:
        return bool(turn_space.get('topology_exhaustive'))
    if turn_space.get('unique_known_unexpanded_action_edges') is not None:
        return int(turn_space.get('unique_known_unexpanded_action_edges') or 0) == 0
    return bool(turn_space.get('exhaustive'))


def _is_modeled_potion_action(action: SearchAction) -> bool:
    if action.action_type != 'use_potion':
        return True
    potion_id = str((action.metadata or {}).get('potion_id') or '').strip().upper()
    potion_id = potion_id.replace('-', '_').replace(' ', '_').removeprefix('POTION.')
    return potion_id not in UNMODELED_SEARCH_POTION_IDS and potion_id not in AUTOMATIC_POTION_IDS


def _build_searcher(cli, cfg, search_state, snapshot_id, snapshot_json,
                    parallel, expand_potions):
    ra = _helpers()
    return CombatSearcher(
        cfg.cli_cfg,
        cfg.spec,
        parallel_top_level=parallel,
        max_workers=cfg.max_workers,
        reuse_cli_processes=cfg.reuse_cli_processes,
        chance_table=None,
        score_mode=cfg.score_mode,
        player_overrides=ra.build_player_overrides(search_state),
        expand_potions=expand_potions,
        root_snapshot_id=snapshot_id,
        root_snapshot_json=snapshot_json,
        authoritative_root_state=search_state,
        worker_pool=cfg.worker_pool,
        leaf_dump_sink=cfg.leaf_dump_sink,
        leaf_dump_rate=cfg.leaf_dump_rate,
        capture_root_topk=cfg.capture_root_topk,
        max_search_ms=cfg.max_search_ms,
        evaluator_coefficients=cfg.evaluator_coefficients,
        scorer_stage=cfg.scorer_stage or stage_for_floor(cfg.floor),
        scorer_model=cfg.scorer_model,
        search_mode=cfg.search_mode,
        beam_width=cfg.beam_width,
    )


def _fresh_search(cli, search_state, cfg, plan, res, t, expand_potions):
    """Capture → (sanitize) → search → (raw-retry) → fallback. Mutates plan."""
    ra = _helpers()
    snap_id = "combat_step_root"
    t0 = time.perf_counter()
    cap = cli.capture_combat_snapshot(snap_id)
    t["capture_ms"] = (time.perf_counter() - t0) * 1000.0
    if not cap.get("success"):
        raise RuntimeError(f"capture_combat_snapshot failed: {cap}")
    t0 = time.perf_counter()
    exp = cli.export_combat_snapshot(snap_id)
    t["export_ms"] = (time.perf_counter() - t0) * 1000.0
    if not exp.get("success"):
        raise RuntimeError(f"export_combat_snapshot failed: {exp}")
    raw_json = exp["snapshot_json"]
    res.root_snapshot_id = snap_id
    res.root_snapshot_json = raw_json

    search_json, meta = ra.sanitize_snapshot_json_for_search(raw_json)
    res.search_meta.update(meta)

    parallel = ra.should_parallelize_combat_search(
        search_state, user_requested_parallel=cfg.user_parallel,
        floor=cfg.floor, depth=cfg.depth, chance_depth=cfg.chance_depth)
    res.parallel = parallel

    t0 = time.perf_counter()
    searcher = _build_searcher(cli, cfg, search_state, snap_id, search_json,
                               parallel, expand_potions)
    t["construct_ms"] = (time.perf_counter() - t0) * 1000.0
    final_searcher = searcher
    t0 = time.perf_counter()
    try:
        result = searcher.search_from_history([], depth=cfg.depth,
                                               chance_depth=cfg.chance_depth)
        t["search_ms"] = (time.perf_counter() - t0) * 1000.0
    except RootStateMismatchError as exc:
        t["search_ms"] = (time.perf_counter() - t0) * 1000.0
        res.search_failed = True
        res.decision_audit['root_state_mismatch'] = str(exc)
        _clear_plan(plan)
        fb_action, fb_payload = ra.choose_combat_fallback(search_state)
        res.fell_back = True
        res.decision_reason = 'fallback'
        return None, (fb_action, fb_payload), res
    finally:
        t0 = time.perf_counter()
        searcher.close()
        t["close_ms"] = (time.perf_counter() - t0) * 1000.0

    # raw-retry on a failed root (un-sanitized snapshot)
    if ra.is_failed_root_search(result, search_state):
        res.search_failed = True
        if raw_json != search_json:
            res.raw_retry_used = True
            retry = _build_searcher(cli, cfg, search_state, snap_id, raw_json,
                                    parallel, expand_potions)
            try:
                try:
                    retry_result = retry.search_from_history(
                        [], depth=cfg.depth, chance_depth=cfg.chance_depth)
                except RootStateMismatchError as exc:
                    res.decision_audit['root_state_mismatch'] = str(exc)
                    _clear_plan(plan)
                    fb_action, fb_payload = ra.choose_combat_fallback(search_state)
                    res.fell_back = True
                    res.decision_reason = 'fallback'
                    return None, (fb_action, fb_payload), res
            finally:
                retry.close()
            if not ra.is_failed_root_search(retry_result, search_state):
                res.raw_retry_recovered = True
                result = retry_result
                final_searcher = retry
    # Offline leaf collection also needs the root-level candidate set. Keep it
    # behind a duck-typed sink so normal searches retain zero collection
    # overhead and existing sinks remain valid.
    recorder = cfg.leaf_dump_sink
    record_search = getattr(recorder, 'record_search', None)
    if callable(record_search):
        try:
            record_search(
                final_searcher._preference_root or search_state,
                result,
                {
                    'depth': cfg.depth,
                    'chance_depth': cfg.chance_depth,
                    'score_mode': cfg.score_mode,
                    'time_budget_ms': cfg.max_search_ms,
                    'timing': final_searcher.timing_summary(),
                    'root_coverage': dict((result.stats or {}).get('root_coverage') or {}),
                    'search_failed': bool(res.search_failed),
                    'raw_retry_used': bool(res.raw_retry_used),
                },
            )
        except Exception:
            # Dataset collection must never change combat behavior. The leaf
            # rows remain usable even if a decision envelope cannot be written.
            pass

    # search telemetry from the searcher that produced `result` (timing dict and
    # timing_summary() both survive close()).
    res.ran_search = True
    res.nodes = (result.stats or {}).get("nodes")
    res.root_candidates = result.root_candidates or []
    res.searcher_timing = dict(final_searcher.timing)
    res.searcher_timing_summary = final_searcher.timing_summary()
    root_coverage = dict((result.stats or {}).get('root_coverage') or {})
    prune_counts = final_searcher._prune_counts()
    deduplicated_edges = (
        prune_counts.get('symmetry_pruned_actions', 0)
        + prune_counts.get('state_pruned_actions', 0)
    )
    filtered_edges = sum(
        value for key, value in prune_counts.items()
        if key not in {'symmetry_pruned_actions', 'state_pruned_actions'}
    )
    expanded_edges = int(res.searcher_timing_summary.get('expanded_action_edges') or 0)
    candidate_edges = int(res.searcher_timing_summary.get('candidate_action_edges') or 0)
    known_unexpanded = int(res.searcher_timing_summary.get('known_unexpanded_action_edges') or 0)
    lethal_skipped = int(res.searcher_timing_summary.get('lethal_skipped_action_edges') or 0)
    depth_cutoffs = int(res.searcher_timing_summary.get('depth_cutoff_leaves') or 0)
    time_exhausted = bool(res.searcher_timing_summary.get('time_budget_exhausted'))
    unique_expanded = int(res.searcher_timing_summary.get('unique_expanded_action_edges') or 0)
    unique_unexpanded = int(res.searcher_timing_summary.get('unique_known_unexpanded_action_edges') or 0)
    failed_settlements = int(res.searcher_timing_summary.get('failed_settlements') or 0)
    branch_budgets = list(res.searcher_timing_summary.get('root_branch_budgets') or [])
    branch_budget_exhaustions = sum(bool(row.get('budget_exhausted')) for row in branch_budgets)
    stage_b_budget_exhaustions = sum(
        bool(row.get('budget_exhausted')) and row.get('stage') == 'B'
        for row in branch_budgets
    )
    soft_budget_overrun_ms = max(
        0.0, float(t.get('search_ms') or 0.0) - float(cfg.max_search_ms or 0.0)
    )
    soft_budget_overrun_ratio = (
        soft_budget_overrun_ms / float(cfg.max_search_ms)
        if cfg.max_search_ms else 0.0
    )
    # The project target is the deduplicated, bounded action tree: every
    # remaining edge is expanded until energy reaches zero / the turn settles,
    # or the configured search horizon is reached. A verified lethal stop, an
    # equivalent edge, and a horizon leaf are valid boundaries rather than
    # uncovered work. Only a unique frontier edge left unexpanded, or a failed
    # settlement, makes this target incomplete.
    # Beam may discard continuations whose edges were never discovered.
    # A zero known-unexpanded count cannot certify its complete search tree.
    selective_search = bool((result.stats or {}).get('beam_search'))
    bounded_tree_complete = not unique_unexpanded and not failed_settlements and not selective_search
    topology_exhaustive = bounded_tree_complete
    all_attempts_complete = bounded_tree_complete and not branch_budget_exhaustions
    combat = search_state.get('combat') or {}
    res.decision_audit = {
        'root': root_coverage,
        'turn_space': {
            'turn_number': combat.get('turn_number', combat.get('round_number')),
            'decision_states': int(res.searcher_timing_summary.get('decision_states_prepared') or 0),
            'discovered_action_edges': int(res.searcher_timing_summary.get('available_action_edges') or 0),
            'candidate_action_edges': candidate_edges,
            'deduplicated_action_edges': deduplicated_edges,
            'filtered_action_edges': filtered_edges,
            'expanded_action_edges': expanded_edges,
            'known_unexpanded_action_edges': known_unexpanded,
            'lethal_skipped_action_edges': lethal_skipped,
            'coverage_schema_version': int(res.searcher_timing_summary.get('coverage_schema_version') or 1),
            'coverage_edge_identity': res.searcher_timing_summary.get('coverage_edge_identity'),
            'unique_decision_states': int(res.searcher_timing_summary.get('unique_decision_states') or 0),
            'unique_discovered_action_edges': int(res.searcher_timing_summary.get('unique_available_action_edges') or 0),
            'unique_candidate_action_edges': int(res.searcher_timing_summary.get('unique_candidate_action_edges') or 0),
            'unique_expanded_action_edges': unique_expanded,
            'unique_known_unexpanded_action_edges': unique_unexpanded,
            'repeated_decision_state_preparations': int(res.searcher_timing_summary.get('repeated_decision_state_preparations') or 0),
            'repeated_discovered_action_edges': int(res.searcher_timing_summary.get('repeated_available_action_edges') or 0),
            'repeated_candidate_action_edges': int(res.searcher_timing_summary.get('repeated_candidate_action_edges') or 0),
            'repeated_expanded_action_edges': int(res.searcher_timing_summary.get('repeated_expanded_action_edges') or 0),
            'completed_turn_lines': int(res.searcher_timing_summary.get('completed_turn_lines') or 0),
            'scored_leaves': int(res.searcher_timing_summary.get('unique_leaf_scores') or 0),
            'leaf_score_requests': int(res.searcher_timing_summary.get('leaf_score_requests') or 0),
            'unique_scored_leaf_states': int(res.searcher_timing_summary.get('unique_scored_leaf_states') or 0),
            'repeated_leaf_score_requests': int(res.searcher_timing_summary.get('repeated_leaf_score_requests') or 0),
            'repeated_leaf_evaluations': int(res.searcher_timing_summary.get('repeated_leaf_evaluations') or 0),
            'depth_cutoff_leaves': depth_cutoffs,
            'horizon_boundary_leaves': int(res.searcher_timing_summary.get('horizon_boundary_leaves') or 0),
            'candidate_coverage_ratio': _turn_space_coverage_ratio(
                expanded_edges, known_unexpanded
            ),
            'unique_candidate_coverage_ratio': _turn_space_coverage_ratio(
                unique_expanded, unique_unexpanded
            ),
            'bounded_tree_coverage_ratio': _turn_space_coverage_ratio(
                unique_expanded, unique_unexpanded
            ),
            'coverage_target': 'deduplicated_action_tree_to_turn_end_or_horizon',
            'selective_search': selective_search,
            'beam_plan_reuse_safe': bool(res.searcher_timing_summary.get('beam_plan_reuse_safe')),
            'beam_layers': (result.stats or {}).get('beam_layers'),
            'beam_width_pruned': (result.stats or {}).get('beam_width_pruned', 0),
            'terminated_by_lethal': bool(res.searcher_timing_summary.get('lethal_early_stops')),
            'failed_settlements': failed_settlements,
            'bounded_tree_complete': bounded_tree_complete,
            'topology_exhaustive': topology_exhaustive,
            'all_attempts_complete': all_attempts_complete,
            # Backward-compatible conservative flag. New consumers should show
            # topology_exhaustive and all_attempts_complete separately.
            'exhaustive': all_attempts_complete,
        },
        'tree': {
            'nodes_visited': res.nodes,
            'subtree_cache_hits': res.searcher_timing_summary.get('subtree_cache_hits', 0),
            'eval_cache_hits': res.searcher_timing_summary.get('eval_cache_hits', 0),
        },
        'limits': {
            'depth': cfg.depth,
            'effective_depth': int((res.searcher_timing_summary.get('horizon_policy') or {}).get('effective_depth') or cfg.depth),
            'horizon_policy': dict(res.searcher_timing_summary.get('horizon_policy') or {}),
            'chance_depth': cfg.chance_depth,
            'time_budget_ms': cfg.max_search_ms,
            'search_ms': float(t.get('search_ms') or 0.0),
            'time_budget_exhausted': time_exhausted,
            'soft_budget_overrun': soft_budget_overrun_ms > 0.0,
            'soft_budget_overrun_ms': soft_budget_overrun_ms,
            'soft_budget_overrun_ratio': soft_budget_overrun_ratio,
            'branch_budget_exhaustions': branch_budget_exhaustions,
            'stage_b_budget_exhaustions': stage_b_budget_exhaustions,
            'budget_pressure_detected': bool(
                time_exhausted or soft_budget_overrun_ms > 0.0 or branch_budget_exhaustions
            ),
            'pre_chance_budget_exhaustions': int(final_searcher.timing.get('pre_chance_budget_exhaustions') or 0),
        },
        'selection': {
            'rule': str((result.stats or {}).get('selection_rule') or 'score'),
            'lethal_candidates': int((result.stats or {}).get('lethal_candidates') or 0),
            'death_pruned_candidates': int((result.stats or {}).get('death_pruned_candidates') or 0),
            'dominated_candidates': int((result.stats or {}).get('dominated_candidates') or 0),
            'invalid_root_candidates': int((result.stats or {}).get('invalid_root_candidates') or 0),
            'pareto_frontier_size': int((result.stats or {}).get('pareto_frontier_size') or 0),
            'fair_root_budget_ms': float(res.searcher_timing_summary.get('root_fair_budget_ms') or 0.0),
            'root_branch_budget_ms': float(res.searcher_timing_summary.get('root_branch_budget_ms') or 0.0),
            'root_budget_policy': str((res.searcher_timing_summary.get('parallel_audit') or {}).get('root_budget_policy') or 'serial_or_legacy'),
            'root_branches_evaluated': int(res.searcher_timing_summary.get('root_branches_evaluated') or 0),
            'root_branch_budgets': branch_budgets,
        },
        'parallel': dict(res.searcher_timing_summary.get('parallel_audit') or {}),
    }
    res.score_explanation = dict((result.stats or {}).get('score_explanation') or {})

    return _resolve_search_result(ra, search_state, result, plan, res, t)


def _resolve_search_result(ra, search_state, result, plan, res, t):
    """Turn a SearchResult into (chosen, resolved, res), with fallback."""
    res.search_score = getattr(result, "score", None)
    res.leaf_state = getattr(result, "leaf_state", None)
    res.decision_audit['leaf_settlement'] = (res.leaf_state or {}).get('leaf_settlement')
    terminal_decision = str((res.leaf_state or {}).get('terminal_decision') or '')
    lethal_selected = bool(float(res.search_score or 0.0) >= 900_000.0 and terminal_decision)
    lethal_early_stop = bool(lethal_selected and (result.stats or {}).get('lethal_early_stop'))
    res.decision_reason = 'lethal_early_stop' if lethal_early_stop else 'highest_score'
    res.decision_audit['lethal_selected'] = lethal_selected
    res.decision_audit['lethal_early_stop'] = lethal_early_stop
    res.decision_audit['selection_rule'] = str((result.stats or {}).get('selection_rule') or 'score')
    if not result.sequence:
        _clear_plan(plan)
        fb_action, fb_payload = ra.choose_combat_fallback(search_state)
        res.fell_back = True
        res.decision_reason = 'fallback'
        return None, (fb_action, fb_payload), res

    chosen = result.sequence[0]
    plan_reuse_eligible = _plan_origin_has_full_coverage(res.decision_audit)
    res.decision_audit['plan_reuse_eligible'] = plan_reuse_eligible
    if plan_reuse_eligible:
        plan.sequence = list(result.sequence[1:])
        plan.expected_state_hashes = list(
            result.state_hashes_after_actions[:len(plan.sequence)]
        )
        plan.expected_state_keys = list(
            result.state_keys_after_actions[:len(plan.sequence)]
        )
        plan.origin_reason = res.decision_reason if plan.sequence else None
        plan.origin_audit = dict(res.decision_audit) if plan.sequence else {}
        plan.origin_score_explanation = dict(res.score_explanation) if plan.sequence else {}
    else:
        _clear_plan(plan)

    # Resolve the semantic action against the current backend state in every
    # mode. This keeps slot/index drift handling independent of client presence.
    t0 = time.perf_counter()
    resolved = ra.resolve_planned_action(search_state, chosen)
    t["resolve_ms"] = (time.perf_counter() - t0) * 1000.0
    if resolved is None:
        _clear_plan(plan)
        fb_action, fb_payload = ra.choose_combat_fallback(search_state)
        res.fell_back = True
        res.decision_reason = 'fallback'
        return None, (fb_action, fb_payload), res
    return chosen, resolved, res


def decide_combat_action(
    cli,
    search_state: Dict[str, Any],
    cfg: CombatStepConfig,
    plan: Optional[PlanState] = None,
) -> CombatStepResult:
    """Decide the next combat action on a live combat_play `search_state`.

    All callers use the same search and scheduling semantics. Client-facing
    adapters may supply inputs and perform correctness checks, but do not alter
    this decision path. Mutates `plan` in place when provided.
    """
    ra = _helpers()
    plan = plan if plan is not None else PlanState()
    t = {"capture_ms": 0.0, "export_ms": 0.0, "construct_ms": 0.0,
         "search_ms": 0.0, "close_ms": 0.0, "resolve_ms": 0.0}
    res = CombatStepResult(action="", payload={}, chosen=None, chosen_summary={})
    scorer_identity = None
    if getattr(cfg, 'score_mode', None) == DEFAULT_SCORE_MODE:
        cfg.scorer_model = cfg.scorer_model or active_model()
        scorer_identity = CombatScoring(cfg.scorer_stage or stage_for_floor(cfg.floor), cfg.scorer_model).identity
        if plan.scorer_identity is not None and plan.scorer_identity != scorer_identity:
            _clear_plan(plan)
        plan.scorer_identity = scorer_identity

    expand_potions = cfg.room_type in {"Elite", "Boss"}

    # --- 1. plan reuse ------------------------------------------------------
    chosen: Optional[SearchAction] = None
    resolved: Optional[Tuple[str, Dict[str, Any]]] = None
    if plan.sequence and plan.origin_audit and not _plan_origin_has_full_coverage(plan.origin_audit):
        origin_space = (plan.origin_audit.get('turn_space') or {})
        res.plan_diverged = {
            'reason': 'origin_search_incomplete',
            'discarded_plan_len': len(plan.sequence),
            'unique_unexpanded_action_edges': int(
                origin_space.get('unique_known_unexpanded_action_edges')
                if origin_space.get('unique_known_unexpanded_action_edges') is not None
                else origin_space.get('known_unexpanded_action_edges') or 0
            ),
        }
        _clear_plan(plan)
    if plan.sequence and plan.expected_state_hashes:
        live_hash = hash_search_state_for_plan_reuse(search_state)
        expected_hash = plan.expected_state_hashes[0]
        if live_hash != expected_hash:
            live_key = canonicalize_search_state_for_plan_reuse(search_state)
            expected_key = plan.expected_state_keys[0] if plan.expected_state_keys else None
            res.plan_diverged = {
                "reason": "state_mismatch",
                "live_state_hash": live_hash,
                "expected_state_hash": expected_hash,
                "discarded_plan_len": len(plan.sequence),
            }
            if expected_key is not None:
                res.plan_diverged["state_differences"] = diff_plan_reuse_states(
                    expected_key, live_key
                )
            _clear_plan(plan)
    while plan.sequence:
        candidate = plan.sequence[0]
        if not _is_modeled_potion_action(candidate):
            _clear_plan(plan)
            break
        origin_reason = plan.origin_reason or 'highest_score'
        origin_audit = dict(plan.origin_audit)
        origin_score_explanation = dict(plan.origin_score_explanation)
        resolved = ra.resolve_planned_action(search_state, candidate)
        if resolved is not None:
            chosen = candidate
            plan.sequence = plan.sequence[1:]
            if plan.expected_state_hashes:
                plan.expected_state_hashes = plan.expected_state_hashes[1:]
            if plan.expected_state_keys:
                plan.expected_state_keys = plan.expected_state_keys[1:]
            res.reused_plan = True
            res.decision_reason = origin_reason
            res.decision_audit = origin_audit
            res.score_explanation = origin_score_explanation
            if not plan.sequence:
                plan.origin_reason = None
                plan.origin_audit = {}
                plan.origin_score_explanation = {}
            break
        _clear_plan(plan)

    # --- 2. exact forced action ---------------------------------------------
    if chosen is None:
        expand_live_potions = should_expand_potions_for_state(
            search_state, expand_potions
        )
        strategic_actions = [
            action for action in available_actions_from_search_state(search_state)
            if action.action_type != 'discard_potion'
            and _is_modeled_potion_action(action)
            and (action.action_type != 'use_potion' or expand_live_potions)
        ]
        if len(strategic_actions) == 1 and strategic_actions[0].action_type == 'end_turn':
            _clear_plan(plan)
            chosen = strategic_actions[0]
            resolved = ('end_turn', {})
            res.decision_reason = 'forced_action'
            res.decision_audit = {'selection_rule': 'forced_action'}

    # --- 3. fresh search (when no reusable or forced action) ----------------
    if chosen is None:
        chosen, resolved, res = _fresh_search(
            cli, search_state, cfg, plan, res, t, expand_potions)

    action, payload = resolved
    res.action, res.payload = action, payload
    res.chosen = chosen
    res.chosen_summary = (
        {"action_type": chosen.action_type, "card_index": chosen.card_index,
         "target_index": chosen.target_index, "metadata": chosen.metadata}
        if chosen is not None
        else ra.summarize_combat_action(search_state, action, payload)
    )
    if scorer_identity:
        res.decision_audit['scorer'] = scorer_identity
        plan.scorer_identity = scorer_identity
    available_count = len(available_actions_from_search_state(search_state))
    if res.reused_plan:
        res.decision_audit = {
            'root': {
                'available_actions': available_count,
                'candidate_actions': 0,
                'deduplicated_actions': 0,
                'filtered_actions': 0,
                'evaluated_actions': 0,
                'unevaluated_actions': 0,
                'candidate_coverage_ratio': 0.0,
            },
            'reused_plan': True,
            'origin_search': res.decision_audit,
        }
    root_audit = res.decision_audit.setdefault('root', {})
    root_audit.setdefault('available_actions', available_count)
    root_audit.setdefault('candidate_actions', 0)
    root_audit.setdefault('deduplicated_actions', 0)
    root_audit.setdefault('filtered_actions', 0)
    root_audit.setdefault('evaluated_actions', 0)
    root_audit.setdefault('unevaluated_actions', 0)
    root_audit.setdefault('candidate_coverage_ratio', 0.0)
    res.decision_audit.update({
        'decision_reason': res.decision_reason,
        'ran_search': res.ran_search,
        'reused_plan': res.reused_plan,
        'fell_back': res.fell_back,
    })
    if scorer_identity:
        res.decision_audit['scorer'] = scorer_identity
    res.timing = t
    return res
