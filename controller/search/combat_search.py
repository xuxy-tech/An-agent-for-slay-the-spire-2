from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from collections import Counter
from dataclasses import dataclass, field
import csv
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.combat_intent import intent_deals_damage, intent_total_damage
from controller.search.actions import (
    SearchAction,
    available_actions_from_search_state,
    cli_payload_for_action,
)
from controller.search.evaluator import evaluate_leaf, explain_leaf_score, extract_leaf_features
from controller.combat_scoring import CombatScoring, history_trace
from controller.search.enemy_chance_model import (
    EnemyChanceLookupTable,
    encounter_intent_state_from_search_state,
)
from controller.search.card_strength import (
    expected_visible_draw_pool_strength,
)
from controller.search.boss_residual import residual_features as boss_residual_features
from controller.search.state_cache import (
    AUTOMATIC_POTION_IDS,
    canonicalize_search_state_for_plan_reuse,
    hash_search_state,
    hash_search_state_for_dedup,
    hash_search_state_for_plan_reuse,
    hash_search_state_for_subtree_cache,
)


_BASE_CARD_COSTS: Optional[Dict[str, int]] = None
_BASE_CARD_NUMERIC_STATS: Optional[Dict[str, Dict[str, float]]] = None
_BASE_CARD_RULES_TEXT: Optional[Dict[str, str]] = None
_DISABLED_NAMED_ROOT_MODES = {
    'balanced_action', 'balanced_r0a', 'balanced_r0b', 'balanced_r0c', 'balanced_r0d',
}

# Potions whose effect opens an in-combat card-select modal. The headless CLI's
# action executor cannot answer that modal mid-search; once one is used during
# candidate expansion, the worker process is left with a pending modal that the
# in_place restore does not fully clear, after which EVERY subsequent action on
# that worker returns an engine error -> empty/enemy-less states that surface as
# phantom +1e6 victories or empty searches (root-caused 2026-06-14). Until the
# engine can resolve the modal headlessly, these potions are excluded from
# search expansion so they never poison a worker. They are NOT removed from the
# real game — the run layer can still play them; the search just won't branch on
# them.
_MODAL_POTION_IDS = {
    "LIQUID_MEMORIES",
}
DEFAULT_POTION_EMERGENCY_HP_RATIO = 0.4


class RootStateMismatchError(RuntimeError):
    """The imported search root disagrees with the authoritative engine."""


def _root_card_contract(state: Dict[str, Any]) -> Dict[str, Any]:
    combat = state.get('combat') or {}
    card_fields = ('card_id', 'upgrade', 'current_cost', 'display_cost',
                   'display_costs_x', 'keywords', 'affliction', 'affliction_count')
    hand = []
    for card in combat.get('hand') or []:
        hand.append(tuple(
            tuple(sorted(str(v) for v in (card.get(field) or [])))
            if field == 'keywords' else card.get(field)
            for field in card_fields
        ))
    actions = []
    for action in combat.get('available_actions') or []:
        if action.get('action_type') != 'play_card':
            continue
        metadata = action.get('metadata') or {}
        actions.append((action.get('card_index'), action.get('target_index'),
                        metadata.get('card_id'), metadata.get('target_monster_id'),
                        metadata.get('target_type')))
    return {'hand': hand, 'play_card_actions': sorted(actions, key=str)}


def _require_matching_root_cards(expected: Dict[str, Any], actual: Dict[str, Any]) -> None:
    expected_contract = _root_card_contract(expected)
    actual_contract = _root_card_contract(actual)
    if expected_contract == actual_contract:
        return
    for section in ('hand', 'play_card_actions'):
        for index, (left, right) in enumerate(zip(expected_contract[section], actual_contract[section])):
            if left != right:
                raise RootStateMismatchError(
                    f'{section}[{index}] authoritative={left!r} restored={right!r}')
        if len(expected_contract[section]) != len(actual_contract[section]):
            raise RootStateMismatchError(
                f'{section} length authoritative={len(expected_contract[section])} '
                f'restored={len(actual_contract[section])}')


def should_expand_potions_for_state(
    search_state: Dict[str, Any],
    expand_potions: bool,
    emergency_hp_ratio: float = DEFAULT_POTION_EMERGENCY_HP_RATIO,
) -> bool:
    if expand_potions:
        return True
    combat = search_state.get("combat") or {}
    player = combat.get("player") or {}
    try:
        hp = float(player.get("hp") or 0.0)
        max_hp = float(player.get("max_hp") or 1.0)
    except (TypeError, ValueError):
        return False
    return max_hp > 0.0 and hp / max_hp <= emergency_hp_ratio



def _base_card_costs() -> Dict[str, int]:
    global _BASE_CARD_COSTS
    if _BASE_CARD_COSTS is not None:
        return _BASE_CARD_COSTS
    repo_root = Path(__file__).resolve().parents[2]
    costs: Dict[str, int] = {}
    metadata_path = repo_root / "data" / "card_stats" / "sts2_linear_metadata_dataset.csv"
    try:
        with metadata_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                card_id = str(row.get("card_id") or "").strip().upper()
                if not card_id or card_id in costs:
                    continue
                try:
                    costs[card_id] = int(row.get("cost") or 0)
                except Exception:
                    continue
    except Exception:
        costs = {}
    cards_json_path = repo_root / "third_party" / "STS2-Agent" / "mcp_server" / "data" / "eng" / "cards.json"
    try:
        fallback_cards = json.loads(cards_json_path.read_text())
        if isinstance(fallback_cards, list):
            for card in fallback_cards:
                if not isinstance(card, dict):
                    continue
                card_id = str(card.get("id") or "").strip().upper()
                if not card_id or card_id in costs:
                    continue
                try:
                    cost = card.get("cost")
                    if cost is None:
                        continue
                    costs[card_id] = int(cost)
                except Exception:
                    continue
    except Exception:
        pass
    _BASE_CARD_COSTS = costs
    return costs


def _base_card_numeric_stats() -> Dict[str, Dict[str, float]]:
    global _BASE_CARD_NUMERIC_STATS
    if _BASE_CARD_NUMERIC_STATS is not None:
        return _BASE_CARD_NUMERIC_STATS
    repo_root = Path(__file__).resolve().parents[2]
    cards_json_path = repo_root / "third_party" / "STS2-Agent" / "mcp_server" / "data" / "eng" / "cards.json"
    stats_by_id: Dict[str, Dict[str, float]] = {}
    try:
        payload = json.loads(cards_json_path.read_text())
        if isinstance(payload, list):
            for card in payload:
                if not isinstance(card, dict):
                    continue
                card_id = str(card.get("id") or "").strip().upper()
                if not card_id:
                    continue
                stats_by_id[card_id] = {
                    "damage": float(card.get("damage") or 0.0),
                    "block": float(card.get("block") or 0.0),
                }
    except Exception:
        stats_by_id = {}
    _BASE_CARD_NUMERIC_STATS = stats_by_id
    return stats_by_id


def _base_card_rules_text() -> Dict[str, str]:
    global _BASE_CARD_RULES_TEXT
    if _BASE_CARD_RULES_TEXT is not None:
        return _BASE_CARD_RULES_TEXT
    repo_root = Path(__file__).resolve().parents[2]
    cards_json_path = repo_root / "third_party" / "STS2-Agent" / "mcp_server" / "data" / "eng" / "cards.json"
    text_by_id: Dict[str, str] = {}
    try:
        payload = json.loads(cards_json_path.read_text())
        if isinstance(payload, list):
            for card in payload:
                if not isinstance(card, dict):
                    continue
                card_id = str(card.get("id") or "").strip().upper()
                if not card_id:
                    continue
                text_by_id[card_id] = str(
                    card.get("description")
                    or card.get("description_raw")
                    or card.get("raw_description")
                    or ""
                ).lower()
    except Exception:
        text_by_id = {}
    _BASE_CARD_RULES_TEXT = text_by_id
    return text_by_id


def _card_energy_cost(card: Optional[Dict[str, Any]], fallback_card_id: Optional[str] = None) -> int:
    if not isinstance(card, dict):
        card = {}
    raw_cost = card.get('display_cost', card.get('current_cost'))
    try:
        if raw_cost is not None:
            return int(raw_cost)
    except Exception:
        pass
    card_id = str(card.get("card_id") or fallback_card_id or "").strip().upper()
    if not card_id:
        return 0
    return int(_base_card_costs().get(card_id, 0))


def _card_numeric_stat(
    card: Optional[Dict[str, Any]],
    stat_name: str,
    fallback_card_id: Optional[str] = None,
) -> float:
    if not isinstance(card, dict):
        card = {}
    stats = card.get("stats") or {}
    try:
        value = stats.get(stat_name)
        if value is not None:
            return float(value)
    except Exception:
        pass
    card_id = str(card.get("card_id") or fallback_card_id or "").strip().upper()
    if not card_id:
        return 0.0
    base = _base_card_numeric_stats().get(card_id) or {}
    try:
        return float(base.get(stat_name) or 0.0)
    except Exception:
        return 0.0


def _card_rules_text(
    card: Optional[Dict[str, Any]],
    fallback_card_id: Optional[str] = None,
) -> str:
    if not isinstance(card, dict):
        card = {}
    card_id = str(card.get("card_id") or fallback_card_id or "").strip().upper()
    if not card_id:
        return ""
    return _base_card_rules_text().get(card_id, "")


@dataclass(frozen=True)
class CombatSpec:
    character: str
    encounter: str
    seed: str
    ascension: int = 0
    lang: str = "en"


@dataclass(frozen=True)
class RecordedAction:
    action: str
    args: Dict[str, Any]


@dataclass
class SearchResult:
    score: float
    sequence: List[SearchAction]
    leaf_state: Dict[str, Any]
    stats: Dict[str, Any]
    state_hashes_after_actions: List[str] = field(default_factory=list)
    state_keys_after_actions: List[Dict[str, Any]] = field(default_factory=list)
    # Optional decision-review payload: the root candidate actions the
    # top-level search scored (each: action summary, score, and the line it
    # leads to). Empty unless capture_root_topk != 0. A negative value captures
    # every evaluated root candidate. None on deeper nodes.
    root_candidates: Optional[List[Dict[str, Any]]] = None


@dataclass
class _CliWorkerContext:
    cli: Sts2CliAdapter
    snapshot_ids_by_history: Dict[Tuple[Tuple[str, Tuple[Tuple[str, Any], ...]], ...], str]
    owned: bool = True
    snapshot_fingerprints_by_history: Dict[
        Tuple[Tuple[str, Tuple[Tuple[str, Any], ...]], ...], str
    ] = field(default_factory=dict)
    snapshot_semantic_fingerprints_by_history: Dict[
        Tuple[Tuple[str, Tuple[Tuple[str, Any], ...]], ...], str
    ] = field(default_factory=dict)


@dataclass
class _StrictDagCandidate:
    """One path reaching a cheap projected state before strict equivalence."""

    history: List[RecordedAction]
    search_state: Dict[str, Any]
    worker_ctx: Optional[_CliWorkerContext] = None
    strict_fingerprint: str = ""
    result: Optional[SearchResult] = None


class _SharedDagCache:
    """Thread-safe state cache with duplicate-work coalescing."""

    def __init__(self, hit_stat: str = "dag_cache_hit") -> None:
        self._lock = threading.Lock()
        self._results: Dict[Tuple[Any, ...], SearchResult] = {}
        self._inflight: Dict[Tuple[Any, ...], Tuple[int, threading.Event]] = {}
        self._hit_stat = hit_stat

    def _clone(self, result: SearchResult, *, hit: bool = False) -> SearchResult:
        stats = dict(result.stats)
        if hit:
            stats[self._hit_stat] = True
        return SearchResult(
            score=result.score,
            sequence=list(result.sequence),
            leaf_state=result.leaf_state,
            state_hashes_after_actions=list(result.state_hashes_after_actions),
            state_keys_after_actions=copy.deepcopy(result.state_keys_after_actions),
            stats=stats,
            root_candidates=copy.deepcopy(result.root_candidates),
        )

    def reserve(self, key: Tuple[Any, ...]) -> Tuple[str, Any]:
        owner = threading.get_ident()
        with self._lock:
            cached = self._results.get(key)
            if cached is not None:
                return "cached", self._clone(cached, hit=True)
            pending = self._inflight.get(key)
            if pending is not None:
                if pending[0] == owner:
                    return "recursive", None
                return "wait", pending[1]
            event = threading.Event()
            self._inflight[key] = (owner, event)
            return "owner", event

    def completed(self, key: Tuple[Any, ...]) -> Optional[SearchResult]:
        with self._lock:
            result = self._results.get(key)
            return self._clone(result, hit=True) if result is not None else None

    def publish(self, key: Tuple[Any, ...], result: Optional[SearchResult]) -> None:
        with self._lock:
            if result is not None:
                self._results[key] = self._clone(result)
            pending = self._inflight.pop(key, None)
            if pending is not None:
                pending[1].set()

    def invalidate(self, key: Tuple[Any, ...]) -> None:
        with self._lock:
            self._results.pop(key, None)


class CombatWorkerPool:
    """Persistent pool of warm CLI worker processes that survives across
    decision steps within (and optionally across) a combat.

    Why this exists: a fresh CLI process pays ~241ms `EnsureModelDbInitialized`
    on its first snapshot restore, plus a ~93ms run bootstrap — together the
    340ms `full` restore that dominates search wall time (97.8% of restore ms,
    measured). That cost is process-level one-time: a process that has already
    initialized ModelDB and holds a live run serves a freshly imported root via
    the ~2ms in_place path instead (verified). The old code threw the worker
    away at the end of every decision step (`CombatSearcher.close()`), so every
    step re-paid 241ms per worker. This pool keeps the processes warm so the
    init is paid once per process, not once per step.

    Borrow/return model: processes are NOT keyed by thread id. Thread ids are
    not stable across the separate ThreadPoolExecutor instances each search
    creates (verified: they drift), so a thread-keyed pool would spawn cold
    processes every step. Instead a borrower checks out a warm process from a
    shared free-list and returns it when done; any warm process serves any
    thread. Each borrowing CombatSearcher re-imports its own root into the
    process and tracks snapshots in its own per-step dict, so no state leaks
    between steps beyond the warm ModelDB + live run we intend to reuse.
    """

    def __init__(self, cli_cfg: CliConfig):
        self.cli_cfg = cli_cfg
        self._lock = threading.Lock()
        self._free: List[Sts2CliAdapter] = []
        self._all: List[Sts2CliAdapter] = []
        self._started = 0
        self._acquires = 0
        self._warm_reuses = 0
        self._releases = 0
        self._discards = 0
        self._discard_reasons: Dict[str, int] = {}

    def prewarm(self, count: int = 1) -> None:
        target = max(0, int(count))
        with self._lock:
            missing = max(0, target - len(self._all))
        for _ in range(missing):
            cli = Sts2CliAdapter(self.cli_cfg)
            cli.start()
            with self._lock:
                self._all.append(cli)
                self._free.append(cli)
                self._started += 1

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'started': self._started,
                'acquires': self._acquires,
                'warm_reuses': self._warm_reuses,
                'releases': self._releases,
                'discards': self._discards,
                'discard_reasons': dict(sorted(self._discard_reasons.items())),
                'alive': len(self._all),
                'free': len(self._free),
                'borrowed': max(0, len(self._all) - len(self._free)),
            }

    def acquire(self) -> Sts2CliAdapter:
        """Check out a warm process from the free-list, or start a new one if
        none are free. The returned process must be handed back via release()."""
        with self._lock:
            self._acquires += 1
            while self._free:
                cli = self._free.pop()
                if cli.is_alive():
                    self._warm_reuses += 1
                    return cli
                # Crashed while idle; drop it.
                if cli in self._all:
                    self._all.remove(cli)
                self._discards += 1
                reason = 'idle_worker_dead'
                self._discard_reasons[reason] = self._discard_reasons.get(reason, 0) + 1
            cli = Sts2CliAdapter(self.cli_cfg)
            cli.start()
            self._all.append(cli)
            self._started += 1
            return cli

    def release(self, cli: Sts2CliAdapter) -> None:
        """Return a process to the free-list for reuse. Processes that have been
        discarded (no longer tracked) or have died are not re-pooled."""
        with self._lock:
            if cli not in self._all:
                return
            if cli.is_alive():
                if cli not in self._free:
                    self._free.append(cli)
                    self._releases += 1
            else:
                self._all.remove(cli)

    def discard(self, cli: Sts2CliAdapter, *, reason: str = 'unspecified') -> None:
        """Permanently evict a suspect process (failed import / mid-search
        corruption) and stop it, so it never serves another step."""
        with self._lock:
            self._discards += 1
            normalized_reason = str(reason or 'unspecified')
            self._discard_reasons[normalized_reason] = (
                self._discard_reasons.get(normalized_reason, 0) + 1
            )
            if cli in self._all:
                self._all.remove(cli)
            if cli in self._free:
                self._free.remove(cli)
        try:
            cli.stop()
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            clis = list(self._all)
            self._all.clear()
            self._free.clear()
        for cli in clis:
            try:
                cli.stop()
            except Exception:
                pass


class CombatSearcher:
    def __init__(
        self,
        cli_cfg: CliConfig,
        combat_spec: CombatSpec,
        parallel_top_level: bool = False,
        max_workers: int = 4,
        reuse_cli_processes: bool = False,
        chance_table: Optional[EnemyChanceLookupTable] = None,
        score_mode: str = "preference",
        player_overrides: Optional[Dict[str, Any]] = None,
        symmetry_dedup: bool = True,
        state_dedup: bool = True,
        expand_potions: bool = False,
        potion_emergency_hp_ratio: float = DEFAULT_POTION_EMERGENCY_HP_RATIO,
        root_cli: Optional[Sts2CliAdapter] = None,
        root_snapshot_id: Optional[str] = None,
        root_snapshot_json: Optional[str] = None,
        authoritative_root_state: Optional[Dict[str, Any]] = None,
        worker_pool: Optional["CombatWorkerPool"] = None,
        leaf_dump_sink: Optional[Any] = None,
        leaf_dump_rate: int = 1,
        capture_root_topk: int = 0,
        max_search_ms: float = 0.0,
        evaluator_coefficients: Optional[Dict[str, float]] = None,
        scorer_stage: str = 'mid',
        scorer_model: Optional[Dict[str, Any]] = None,
        parallel_frontier: bool = False,
        search_mode: str = "dfs",
        beam_width: int = 8,
        beam_dominance: bool = False,
    ):
        self.cli_cfg = cli_cfg
        self.combat_spec = combat_spec
        self.parallel_top_level = parallel_top_level
        self.max_workers = max(1, int(max_workers))
        # A root branch may split its first continuation frontier across
        # additional workers. Child frontier searchers disable this flag, so
        # the fan-out is bounded to one level and cannot recurse explosively.
        self.parallel_frontier = bool(parallel_frontier)
        self.search_mode = str(search_mode or "dfs").lower()
        if self.search_mode not in {"dfs", "beam"}:
            raise ValueError(f"Unknown search mode: {self.search_mode}")
        self.beam_width = max(2, int(beam_width or 8))
        self.beam_dominance = bool(beam_dominance)
        # Imported live-root snapshots are expensive to restore into a fresh CLI
        # process because each replay pays process-local runtime initialization.
        # Reusing a worker process preserves semantics while unlocking the
        # headless in-place restore fast path.
        self.reuse_cli_processes = reuse_cli_processes or (root_snapshot_json is not None)
        self.chance_table = chance_table
        self.score_mode = score_mode
        self.preference_scorer = CombatScoring(scorer_stage, scorer_model) if score_mode == 'preference' else None
        self._preference_root = None
        self._scoring_history_offset = 0
        if score_mode in _DISABLED_NAMED_ROOT_MODES:
            raise ValueError(
                f'Score mode {score_mode!r} is disabled because named-card root '
                'adjustments violate the combat-policy boundary'
            )
        self.leaf_score_mode = score_mode
        self.player_overrides = dict(player_overrides or {})
        self.symmetry_dedup = symmetry_dedup
        self.state_dedup = state_dedup
        # Potion expansion is a declarative horizon switch, not a runtime
        # heuristic: when on (set by the run layer for Elite/Boss rooms), every
        # potion is a fully-expanded candidate; when off, no potion enters the
        # tree. A low-HP ratio forces it on as a human-like survival override.
        self.expand_potions = expand_potions
        self.potion_emergency_hp_ratio = potion_emergency_hp_ratio
        self.root_cli = root_cli
        self.root_snapshot_id = root_snapshot_id
        self.root_snapshot_json = root_snapshot_json
        self.authoritative_root_state = authoritative_root_state
        # Cross-step warm-process pool. When provided (and we have a root
        # snapshot to import), workers are borrowed from the pool instead of
        # being started and stopped per decision step, so the ~241ms ModelDB
        # init is paid once per process rather than once per step.
        self.worker_pool = worker_pool
        # Optional offline data-collection hook. When leaf_dump_sink is set, the
        # searcher samples the leaves it actually evaluates (the exact states the
        # learned value model must score at inference time) and hands their
        # feature vectors to the sink. Default None -> zero overhead, so combat
        # search speed in real / comparison runs is unchanged.
        self.leaf_dump_sink = leaf_dump_sink
        self.leaf_dump_rate = max(1, int(leaf_dump_rate))
        self._leaf_dump_counter = 0
        self._leaf_dump_lock = threading.Lock()
        # Decision-review observability. When > 0, the top-level search keeps the
        # best `capture_root_topk` ROOT candidate actions it already scored
        # (action + score + the line each one leads to) instead of discarding all
        # but the winner. Default 0 -> zero overhead, so real/comparison runs are
        # unchanged. Filled into SearchResult.root_candidates by the top-level
        # search only (deeper recursion never populates it).
        # 0 disables root-candidate capture. A negative value captures every
        # evaluated root edge; offline counterfactual collection uses -1 so it
        # does not silently turn a large legal-action set into top-k labels.
        self.capture_root_topk = int(capture_root_topk)
        self.max_search_ms = max(0.0, float(max_search_ms or 0.0))
        self.evaluator_coefficients = dict(evaluator_coefficients or {})
        self._search_deadline: Optional[float] = None
        self.chance_depth = 0
        self._root_snapshot_enemy_count = self._infer_root_snapshot_enemy_count(root_snapshot_json)
        # Mid-turn checkpoints are safe only when the imported root carries the
        # complete engine RNG state. Older archived snapshots contain only
        # Seed/Counter and must continue using root/turn-root replay.
        self._root_snapshot_rng_state_complete = self._has_complete_root_rng_state(root_snapshot_json)
        self.eval_cache: Dict[str, float] = {}
        self.subtree_cache: Dict[Tuple[str, int, int], SearchResult] = {}
        self._dag_cache = _SharedDagCache()
        self._strict_dag_index_lock = threading.Lock()
        self._strict_dag_candidates: Dict[
            Tuple[Any, ...], List[_StrictDagCandidate]
        ] = {}
        self.strict_dag_enabled = (
            self.state_dedup
            and self.leaf_dump_sink is None
            and self.max_search_ms <= 0.0
            and os.environ.get("STS2_ENABLE_STRICT_DAG") == "1"
            and os.environ.get("STS2_DISABLE_STRICT_DAG") != "1"
        )
        self._semantic_dag_cache = _SharedDagCache("semantic_dag_cache_hit")
        self.semantic_dag_enabled = (
            self.state_dedup and os.environ.get("STS2_DISABLE_SEMANTIC_DAG") != "1"
        )
        self._cli_lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self._cli_by_thread: Dict[int, _CliWorkerContext] = {}
        # CLI processes borrowed from worker_pool this step; released (not
        # stopped) back to the pool on close() so the next step reuses them warm.
        self._pooled_borrowed: List[Sts2CliAdapter] = []
        self._root_summary: Dict[str, Any] = {}
        # Coverage telemetry keeps exact semantic edge identities in private
        # sets. The existing counters remain raw event counts for compatibility;
        # the sets expose how much of those counts came from repeated states or
        # repeated root or continuation expansions.
        self._coverage_sets: Dict[str, set[str]] = {
            "decision_states": set(),
            "available_edges": set(),
            "candidate_edges": set(),
            "expanded_edges": set(),
            "leaf_states": set(),
        }
        self.timing: Dict[str, Any] = {
            "replay_calls": 0,
            "replay_total_ms": 0.0,
            "replay_ms_samples": [],
            "branch_ms_samples": [],
            "replay_breakdown_samples": [],
            "worker_context_reuse_hits": 0,
            "worker_pool_acquire_total_ms": 0.0,
            "worker_process_start_total_ms": 0.0,
            "root_snapshot_import_total_ms": 0.0,
            "root_cli_reuse_hits": 0,
            "headless_action_total_ms": 0.0,
            "headless_get_state_total_ms": 0.0,
            "headless_capture_total_ms": 0.0,
            "headless_fingerprint_total_ms": 0.0,
            "headless_wait_profile": {},
            "action_type_timing": {},
            "slow_action_samples": [],
            # CLI transport probes, grouped by command. queue_wait includes the
            # engine work plus C# serialization and stdout transfer; subtracting
            # the engine-side timing exposes the serialization/pipe remainder.
            **{
                f"transport_{category}_{metric}": 0.0
                for category in (
                    "action", "get_state", "capture", "fingerprint",
                    "restore", "import", "expand",
                )
                for metric in ("response_bytes", "queue_wait_total_ms", "json_parse_total_ms")
            },
            "snapshot_restore_hits": 0,
            "snapshot_capture_count": 0,
            "snapshot_capture_total_ms": 0.0,
            "snapshot_fingerprint_count": 0,
            "snapshot_fingerprint_total_ms": 0.0,
            "state_dedup_collision_samples": [],
            # Restore-mode observability (A). The C# RestoreCombatSnapshot
            # returns restore_mode (in_place|full) + restore_timing_ms but the
            # run layer discarded it. A worker's FIRST restore after an import
            # is forced to `full` (~386ms) because the in-place fast path needs
            # an in-progress run; later restores are in_place (~1ms). Counting
            # full vs in_place hits + their total time exposes how much the
            # full-restore bootstrap actually costs in real fights.
            "restore_mode_full_hits": 0,
            "restore_mode_in_place_hits": 0,
            "restore_mode_other_hits": 0,
            "restore_full_total_ms": 0.0,
            "restore_in_place_total_ms": 0.0,
            "in_place_fail_reasons": {},
            "chance_lookup_hits": 0,
            "chance_lookup_misses": 0,
            "chance_expected_threat_total": 0.0,
            "symmetry_pruned_actions": 0,
            "state_pruned_actions": 0,
            "unsupported_pruned_actions": 0,
            "discard_potion_pruned_actions": 0,
            "noop_potion_pruned_actions": 0,
            "budget_pruned_actions": 0,
            "subtree_cache_hits": 0,
            "dag_cache_hits": 0,
            "dag_singleflight_waits": 0,
            "strict_dag_probe_collisions": 0,
            "strict_dag_exact_matches": 0,
            "strict_dag_fingerprint_mismatches": 0,
            "strict_dag_fingerprint_unavailable": 0,
            "strict_dag_nodes_avoided": 0,
            "semantic_dag_hits": 0,
            "semantic_dag_singleflight_waits": 0,
            "semantic_dag_rejects": 0,
            "semantic_dag_replay_failures": 0,
            "semantic_dag_nodes_avoided": 0,
            "semantic_dag_reject_reasons": {},
            "batch_expand_calls": 0,
            "batch_expand_children": 0,
            "batch_expand_fallbacks": 0,
            "batch_expand_total_ms": 0.0,
            "batch_expand_rpc_failures": 0,
            "batch_expand_rpc_timeouts": 0,
            "engine_rpc_failures": 0,
            "engine_rpc_timeouts": 0,
            "engine_rpc_failure_samples": [],
            "eval_cache_hits": 0,
            "pre_chance_budget_exhaustions": 0,
            "time_budget_ms": self.max_search_ms,
            "time_budget_exhausted": 0,
            "decision_states_prepared": 0,
            "available_action_edges": 0,
            "candidate_action_edges": 0,
            "expanded_action_edges": 0,
            "known_unexpanded_action_edges": 0,
            "lethal_skipped_action_edges": 0,
            "completed_turn_lines": 0,
            "depth_cutoff_leaves": 0,
            "horizon_boundary_leaves": 0,
            "leaf_score_requests": 0,
            "unique_leaf_scores": 0,
            "lethal_early_stops": 0,
            "root_fair_budget_ms": 0.0,
            "root_branches_evaluated": 0,
            "root_branch_budgets": [],
            "dominated_root_candidates": 0,
            "death_pruned_root_candidates": 0,
            "pareto_frontier_size": 0,
            "frontier_workers": 0,
            "root_prepare_workers": 0,
            "root_prepare_jobs_queued": 0,
            "root_prepare_jobs_completed": 0,
            "root_prepare_wall_ms": 0.0,
            "frontier_budget_ms": 0.0,
            "frontier_jobs_queued": 0,
            "frontier_jobs_started": 0,
            "frontier_jobs_completed": 0,
            "frontier_jobs_timed_out": 0,
            "frontier_jobs_cancelled": 0,
        }

    def _ensure_coverage_sets(self) -> None:
        # Some unit tests construct a lightweight CombatSearcher via
        # object.__new__, so telemetry must remain lazy and backwards-compatible.
        if not hasattr(self, "_coverage_sets"):
            self._coverage_sets = {}
        for key in (
            "decision_states", "available_edges", "candidate_edges",
            "expanded_edges", "leaf_states",
        ):
            self._coverage_sets.setdefault(key, set())

    @staticmethod
    def _beam_state_dominates(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        """Experimental metric heuristic, not strict dominance; disabled by default."""
        lc, rc = left.get("combat") or {}, right.get("combat") or {}
        lp, rp = lc.get("player") or {}, rc.get("player") or {}
        l_enemies, r_enemies = lc.get("enemies") or [], rc.get("enemies") or []
        l_hp = sum(float(e.get("hp") or 0.0) for e in l_enemies)
        r_hp = sum(float(e.get("hp") or 0.0) for e in r_enemies)
        l_hand, r_hand = len(lc.get("hand") or []), len(rc.get("hand") or [])
        greater_or_equal = (
            float(lp.get("hp") or 0.0) >= float(rp.get("hp") or 0.0)
            and float(lp.get("block") or 0.0) >= float(rp.get("block") or 0.0)
            and float(lp.get("energy") or 0.0) >= float(rp.get("energy") or 0.0)
            and l_hp <= r_hp and l_hand >= r_hand
        )
        strict = (
            float(lp.get("hp") or 0.0) > float(rp.get("hp") or 0.0)
            or float(lp.get("block") or 0.0) > float(rp.get("block") or 0.0)
            or float(lp.get("energy") or 0.0) > float(rp.get("energy") or 0.0)
            or l_hp < r_hp or l_hand > r_hand
        )
        return greater_or_equal and strict

    def _search_beam(
        self,
        root_state: Dict[str, Any],
        root_history: Sequence[RecordedAction],
        depth: int,
        chance_depth: int,
    ) -> SearchResult:
        """Bounded progressive search over player actions.

        Beam pruning is intentionally state based: it does not prescribe card
        names or action order. Enemy-turn expansion remains the existing
        settled boundary and chance depth is not increased by this path.
        """
        frontier = [(list(root_history), root_state, [])]
        best: Optional[SearchResult] = None
        nodes = 1
        root_actions = available_actions_from_search_state(root_state)
        root_infos: Dict[str, Dict[str, Any]] = {}
        root_results: Dict[str, SearchResult] = {}
        root_pruned: Dict[str, int] = {}
        width = self.beam_width
        self.timing["beam_width"] = width
        self.timing["beam_layers"] = 0
        self.timing["beam_dominance_pruned"] = 0
        self.timing["beam_width_pruned"] = 0
        self.timing["beam_ranking_policy"] = "settled_end_turn_v1"
        for key in ("beam_settlement_probes", "beam_settlement_cache_hits",
                    "beam_settlement_failures", "beam_unranked_budget_nodes"):
            self.timing[key] = 0
        self.timing["beam_settlement_ms"] = 0.0
        # Local to this root/search. Full histories, never projected state hashes,
        # identify speculative end-turn results. Expansion retains the old node.
        settled_by_history = {}

        def settle_for_ranking(state, history):
            if state.get('terminal_decision'):
                return self._settle_leaf_state(state, history)
            end_history = list(history) + [RecordedAction('end_turn', {})]
            key = self._history_key(end_history)
            if key in settled_by_history:
                self._audit_increment('beam_settlement_cache_hits')
                return copy.deepcopy(settled_by_history[key]), True
            if self._time_budget_exhausted():
                return None
            started = time.perf_counter()
            self._audit_increment('beam_settlement_probes')
            leaf, appended = self._settle_leaf_state(state, history)
            self.timing['beam_settlement_ms'] += (time.perf_counter() - started) * 1000.0
            if leaf.get('success') is True:
                settled_by_history[key] = copy.deepcopy(leaf)
            else:
                self._audit_increment('beam_settlement_failures')
            return leaf, appended

        for layer in range(max(1, int(depth))):
            if self._time_budget_exhausted():
                break
            expanded = []
            for history, state, sequence in frontier:
                prune_before = self._prune_counts()
                actions = self._prepare_action_candidates(state, history, available_actions_from_search_state(state))
                if not sequence:
                    root_infos = {self._coverage_action_token(info['action']): info for info in actions}
                    root_pruned = self._prune_delta(prune_before, self._prune_counts())
                for info in actions:
                    if self._time_budget_exhausted():
                        break
                    action = info["action"]
                    self._audit_increment('expanded_action_edges')
                    self._ensure_coverage_sets()
                    self._coverage_sets['expanded_edges'].add(self._coverage_edge_key(state, action))
                    nodes += 1
                    next_history = list(history) + [self._recorded_action_from_search_action(action)]
                    cached_end = settled_by_history.get(self._history_key(next_history)) if action.action_type == 'end_turn' else None
                    if cached_end is not None:
                        self._audit_increment('beam_settlement_cache_hits')
                        child = copy.deepcopy(cached_end)
                    else:
                        child = info.get("child_state") or self._extract_search_state(self.combat_to_state(next_history))
                    if not child.get("success"):
                        continue
                    child_sequence = sequence + [action]
                    # Combat can finish on a card, without an end_turn action.
                    # Such a child must compete as a completed line rather than
                    # entering a frontier with no legal continuation and vanishing.
                    at_cap = layer + 1 >= max(1, int(depth))
                    if action.action_type == "end_turn" or child.get('terminal_decision') or at_cap:
                        appended = False
                        if action.action_type == 'end_turn':
                            child = self._validate_settled_transition(state, child)
                        else:
                            if at_cap and not child.get('terminal_decision'):
                                self._audit_increment('depth_cutoff_leaves')
                                if self._time_budget_exhausted():
                                    continue
                            settled = settle_for_ranking(child, next_history)
                            if settled is None:
                                self._audit_increment('beam_unranked_budget_nodes')
                                continue
                            child, appended = settled
                        if child.get("success") is not True:
                            continue
                        if appended:
                            child_sequence = child_sequence + [SearchAction('end_turn')]
                            next_history = next_history + [RecordedAction('end_turn', {})]
                        self._audit_increment('completed_turn_lines')
                        score = self._score_with_history(child, next_history)
                        candidate = SearchResult(score=score, sequence=child_sequence,
                            leaf_state=child, stats={"nodes": 1, "beam_layer": layer})
                        root_token = self._coverage_action_token(child_sequence[0])
                        previous = root_results.get(root_token)
                        if previous is None or score > previous.score:
                            root_results[root_token] = candidate
                        if best is None or candidate.score > best.score:
                            best = candidate
                        continue
                    expanded.append((next_history, child, child_sequence))
            if not expanded:
                break
            ranked = []
            for index, row in enumerate(expanded):
                settled = settle_for_ranking(row[1], row[0])
                if settled is None:
                    self._audit_increment('beam_unranked_budget_nodes', len(expanded) - index)
                    break
                leaf, appended = settled
                if leaf.get('success') is not True:
                    continue
                score_history = list(row[0]) + ([RecordedAction('end_turn', {})] if appended else [])
                # Probes are ranking evidence, not selected candidate trajectories.
                # A real candidate later emits its own complete, settled path.
                score = self._score_with_history(leaf, score_history, collect_leaf=False)
                if math.isfinite(score):
                    ranked.append((score, row, leaf))
            kept = []
            for item in ranked:
                if self.beam_dominance and any(self._beam_state_dominates(other[2], item[2]) for other in ranked if other is not item):
                    self.timing["beam_dominance_pruned"] += 1
                    continue
                kept.append(item)
            kept.sort(key=lambda item: item[0], reverse=True)
            if len(kept) > width:
                self.timing["beam_width_pruned"] += len(kept) - width
                kept = kept[:width]
            frontier = [item[1] for item in kept]
            self.timing["beam_layers"] = layer + 1
        # Use the same root selection semantics as DFS.  A beam can discover
        # several verified lethal lines; selecting the first terminal line
        # would discard resource-preserving outcomes (for example, killing a
        # thief before it escapes).  Reapply the common Pareto/lethal selector
        # after beam expansion instead of relying on traversal order.
        if root_results:
            adjusted_pairs = []
            for token, candidate in root_results.items():
                info = dict(root_infos.get(token) or {})
                info.setdefault('adjusted_score', candidate.score)
                info.setdefault('base_score', candidate.score)
                adjusted_pairs.append((info, candidate))
            selected, selection_audit = self._select_root_candidate(adjusted_pairs)
            if selected is not None:
                best = selected[1]
                best.stats.update(selection_audit)
                best.stats['selection_rule'] = selection_audit.get('selection_rule', 'score')
        if best is None:
            best = self._settled_leaf_result(root_state, root_history)
        best.stats.update({
            "nodes": nodes,
            "root_coverage": self._root_coverage(len(root_actions), len(root_infos), len(root_results), root_pruned),
            "semantic_reuse_safe": False,
            "beam_search": True,
            "beam_width": width,
            "beam_ranking_policy": self.timing['beam_ranking_policy'],
            "beam_settlement_probes": self.timing['beam_settlement_probes'],
            "beam_settlement_cache_hits": self.timing['beam_settlement_cache_hits'],
            "beam_settlement_failures": self.timing['beam_settlement_failures'],
            "beam_settlement_ms": self.timing['beam_settlement_ms'],
            "beam_unranked_budget_nodes": self.timing['beam_unranked_budget_nodes'],
            "beam_layers": int(self.timing.get("beam_layers") or 0),
            "beam_dominance_pruned": int(self.timing.get("beam_dominance_pruned") or 0),
            "beam_width_pruned": int(self.timing.get("beam_width_pruned") or 0),
            "beam_plan_reuse_safe": bool(best.sequence),
            "time_budget_exhausted": bool(self.timing.get("time_budget_exhausted") or 0),
        })
        self.timing['beam_plan_reuse_safe'] = bool(best.sequence)
        self.timing['root_branches_evaluated'] = len(root_results)
        best.stats['score_explanation'] = self._explain_result(best, root_history)
        if self.capture_root_topk != 0:
            best.root_candidates = self._build_root_candidates([
                (root_infos[token], result) for token, result in root_results.items()
                if token in root_infos
            ])
        return best

    @staticmethod
    def _coverage_action_token(action: SearchAction) -> str:
        payload = {
            "action_type": action.action_type,
            "card_index": action.card_index,
            "target_index": action.target_index,
            "metadata": action.metadata or {},
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    def _coverage_edge_key(self, search_state: Dict[str, Any], action: SearchAction) -> str:
        self._ensure_coverage_sets()
        state_key = hash_search_state(search_state)
        return f"{state_key}:{self._coverage_action_token(action)}"

    def _coverage_snapshot(self) -> Dict[str, List[str]]:
        self._ensure_coverage_sets()
        return {key: sorted(values) for key, values in self._coverage_sets.items()}

    def _merge_coverage(self, coverage: Optional[Dict[str, Any]]) -> None:
        self._ensure_coverage_sets()
        for key in self._coverage_sets:
            values = (coverage or {}).get(key) or []
            self._coverage_sets[key].update(str(value) for value in values)

    def _audit_increment(self, key: str, amount: int = 1) -> None:
        with self._audit_lock:
            self.timing[key] = int(self.timing.get(key) or 0) + amount

    def _record_engine_rpc_failure(self, category: str, exc: Exception) -> None:
        self._audit_increment("engine_rpc_failures")
        if isinstance(exc, TimeoutError):
            self._audit_increment("engine_rpc_timeouts")
        with self._audit_lock:
            samples = self.timing.setdefault("engine_rpc_failure_samples", [])
            if len(samples) < 10:
                message = str(exc)
                if len(message) > 2400:
                    message = message[:1600] + " ... " + message[-800:]
                samples.append({
                    "category": category,
                    "exception": type(exc).__name__,
                    "message": message,
                })

    @staticmethod
    def _is_lethal_result(result: SearchResult) -> bool:
        terminal = str((result.leaf_state or {}).get('terminal_decision') or '')
        return result.score >= 900_000.0 and terminal in {
            'card_reward', 'treasure', 'map_select', 'rest_site', 'shop', 'victory'
        }

    @staticmethod
    def _leaf_player(result: SearchResult) -> Dict[str, Any]:
        leaf = result.leaf_state or {}
        terminal_player = ((leaf.get('terminal_result') or {}).get('player') or {})
        return terminal_player or (((leaf.get('combat') or {}).get('player')) or {})

    def _lethal_classification(self, result: SearchResult) -> str:
        """Classify verified victory lines for early-stop safety.

        Only a deterministic, same-turn, no-resource-cost lethal may stop the
        remaining search. Other lethal lines still outrank non-lethal lines,
        but continue through root comparison so a cheaper lethal can replace
        them.
        """
        if not self._is_lethal_result(result):
            return 'none'
        leaf = result.leaf_state or {}
        root_hp = int(((((getattr(self, '_root_summary', {}) or {}).get('player') or {}).get('hp'))) or 0)
        leaf_hp = int(self._leaf_player(result).get('hp') or 0)
        uses_potion = any(action.action_type == 'use_potion' for action in result.sequence)
        ends_turn = bool(leaf.get('terminal_on_end_turn')) or any(
            action.action_type == 'end_turn' for action in result.sequence
        )
        uncertain = any(
            'RANDOM' in json.dumps(action.metadata or {}, sort_keys=True, default=str).upper()
            for action in result.sequence
        )
        return 'clean' if leaf_hp >= root_hp and not uses_potion and not ends_turn and not uncertain else 'costly'

    def _mark_lethal_early_stop(self, result: SearchResult, remaining_siblings: int) -> bool:
        lethal_class = self._lethal_classification(result)
        result.stats['lethal_class'] = lethal_class
        if remaining_siblings <= 0 or lethal_class != 'clean':
            return False
        self._audit_increment('lethal_skipped_action_edges', remaining_siblings)
        self._audit_increment('lethal_early_stops')
        result.stats['lethal_early_stop'] = True
        result.stats['lethal_skipped_siblings'] = remaining_siblings
        return True

    def _fair_root_budget_ms(self, candidate_count: int) -> float:
        if self.max_search_ms <= 0.0 or candidate_count <= 0:
            return 0.0
        return self.max_search_ms / float(candidate_count)

    def _coverage_first_horizon(
        self, action_budget: int, pre_chance_budget: int, candidate_count: int
    ) -> Tuple[int, int, Dict[str, Any]]:
        """Preserve the requested horizon; coverage never buys a shallow search.

        The old implementation capped wide roots at depth 2/3. That made the
        coverage chart look healthier by removing the deeper tree from the
        search, which changes the agent's strength. A live bounded search must
        keep at least depth 10; configured shallower values are raised and
        surfaced in telemetry instead of being silently accepted.
        """
        configured = max(int(action_budget), int(pre_chance_budget))
        if self.max_search_ms <= 0.0:
            return action_budget, pre_chance_budget, {
                'configured_depth': configured,
                'effective_depth': configured,
                'minimum_depth': 10,
                'policy': 'configured_no_time_budget',
                'root_candidate_count': int(candidate_count),
            }
        effective = max(10, configured)
        return max(action_budget, effective), max(pre_chance_budget, effective), {
            'configured_depth': configured,
            'effective_depth': effective,
            'minimum_depth': 10,
            'policy': 'depth_floor_10' if effective != configured else 'configured',
            'root_candidate_count': int(candidate_count),
        }

    def _new_branch_searcher(
        self,
        budget_ms: float,
        *,
        max_workers: int = 1,
        parallel_frontier: bool = False,
    ) -> 'CombatSearcher':
        branch = CombatSearcher(
            self.cli_cfg,
            self.combat_spec,
            parallel_top_level=False,
            max_workers=max_workers,
            reuse_cli_processes=self.reuse_cli_processes,
            chance_table=self.chance_table,
            score_mode=self.score_mode,
            player_overrides=self.player_overrides,
            symmetry_dedup=self.symmetry_dedup,
            state_dedup=self.state_dedup,
            expand_potions=self.expand_potions,
            potion_emergency_hp_ratio=self.potion_emergency_hp_ratio,
            root_snapshot_id=self.root_snapshot_id,
            root_snapshot_json=self.root_snapshot_json,
            authoritative_root_state=self.authoritative_root_state,
            worker_pool=self.worker_pool,
            leaf_dump_sink=self.leaf_dump_sink,
            leaf_dump_rate=self.leaf_dump_rate,
            capture_root_topk=0,
            max_search_ms=budget_ms,
            evaluator_coefficients=self.evaluator_coefficients,
            scorer_stage=self.preference_scorer.stage if self.preference_scorer else 'mid',
            scorer_model=self.preference_scorer.payload if self.preference_scorer else None,
            parallel_frontier=parallel_frontier,
        )
        # Root branches are independent engine sessions, but their subtree and
        # leaf caches are keyed by semantic state plus remaining horizon. Share
        # them so root fan-out does not recompute equivalent continuation states.
        # This preserves the serial cache semantics and only affects work reuse.
        branch.subtree_cache = self.subtree_cache
        branch.eval_cache = self.eval_cache
        branch._dag_cache = self._dag_cache
        branch._strict_dag_index_lock = self._strict_dag_index_lock
        branch._strict_dag_candidates = self._strict_dag_candidates
        branch.strict_dag_enabled = self.strict_dag_enabled
        branch._semantic_dag_cache = self._semantic_dag_cache
        branch.semantic_dag_enabled = self.semantic_dag_enabled
        return branch

    def _merge_branch_timing(
        self, branch_timing: Dict[str, Any], coverage: Optional[Dict[str, Any]] = None
    ) -> None:
        self._merge_coverage(coverage)
        sample_keys = {
            'replay_ms_samples', 'branch_ms_samples', 'replay_breakdown_samples',
            'root_branch_budgets', 'engine_rpc_failure_samples',
        }
        ignored = {'time_budget_ms', 'time_budget_exhausted', 'root_fair_budget_ms'}
        with self._audit_lock:
            for key, value in branch_timing.items():
                if key in sample_keys:
                    self.timing.setdefault(key, []).extend(list(value or []))
                elif key == 'in_place_fail_reasons':
                    target = self.timing.setdefault(key, {})
                    for reason, count in (value or {}).items():
                        target[reason] = int(target.get(reason) or 0) + int(count or 0)
                elif key == 'headless_wait_profile':
                    target = self.timing.setdefault(key, {})
                    for field, amount in (value or {}).items():
                        if field == 'max_wait_iterations':
                            target[field] = max(int(target.get(field) or 0), int(amount or 0))
                        else:
                            target[field] = float(target.get(field) or 0.0) + float(amount or 0.0)
                elif key == 'action_type_timing':
                    target = self.timing.setdefault(key, {})
                    for action_key, row in (value or {}).items():
                        merged = target.setdefault(action_key, {})
                        for field, amount in (row or {}).items():
                            if field == 'max_cli_ms':
                                merged[field] = max(float(merged.get(field) or 0.0), float(amount or 0.0))
                            else:
                                merged[field] = float(merged.get(field) or 0.0) + float(amount or 0.0)
                elif key == 'slow_action_samples':
                    target = self.timing.setdefault(key, [])
                    target.extend(list(value or []))
                    target.sort(key=lambda item: float(item.get('cli_ms') or 0.0), reverse=True)
                    del target[20:]
                elif key not in ignored and isinstance(value, (int, float)):
                    self.timing[key] = self.timing.get(key, 0) + value

    def _evaluate_isolated_root_branch(
        self,
        action_info: Dict[str, Any],
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        action_budget: int,
        chance_depth: int,
        pre_chance_budget: int,
        budget_ms: float,
        frontier_workers: int = 1,
        queued_at: Optional[float] = None,
    ) -> Tuple[SearchResult, Dict[str, Any]]:
        entered_at = time.perf_counter()
        queue_wait_ms = max(0.0, (entered_at - queued_at) * 1000.0) if queued_at else 0.0
        construct_started = time.perf_counter()
        branch = self._new_branch_searcher(
            budget_ms,
            max_workers=max(1, int(frontier_workers)),
            parallel_frontier=int(frontier_workers) > 1,
        )
        construct_ms = (time.perf_counter() - construct_started) * 1000.0
        branch._root_summary = self._root_summary
        branch._preference_root = self._preference_root
        branch._scoring_history_offset = self._scoring_history_offset
        branch.chance_depth = chance_depth
        branch._reset_search_deadline()
        search_started = time.perf_counter()
        closed = False
        try:
            child = branch._evaluate_child_action(
                action_info['action'],
                search_state,
                history,
                action_budget,
                chance_depth,
                pre_chance_budget,
                action_info['next_history'],
                action_info['child_state'],
            )
            search_ms = (time.perf_counter() - search_started) * 1000.0
            branch_summary = branch.timing_summary()
            replay_cost = dict(branch_summary.get('replay_cost_breakdown') or {})
            audit = {
                **self._action_audit_label(action_info),
                'budget_ms': round(budget_ms, 3),
                'queue_wait_ms': round(queue_wait_ms, 3),
                'construct_ms': round(construct_ms, 3),
                'search_ms': round(search_ms, 3),
                'elapsed_ms': round(construct_ms + search_ms, 3),
                'budget_exhausted': bool(branch.timing.get('time_budget_exhausted')),
                'nodes': int(child.stats.get('nodes', 1)),
                'completed_turn_lines': int(branch.timing.get('completed_turn_lines') or 0),
                'replay_calls': int(branch_summary.get('replay_calls') or 0),
                'replay_total_ms': round(float(branch_summary.get('replay_total_ms') or 0.0), 3),
                'worker_context_reuse_hits': int(branch_summary.get('worker_context_reuse_hits') or 0),
                'worker_pool_acquire_ms': round(float(branch_summary.get('worker_pool_acquire_total_ms') or 0.0), 3),
                'worker_process_start_ms': round(float(branch_summary.get('worker_process_start_total_ms') or 0.0), 3),
                'root_snapshot_import_ms': round(float(branch_summary.get('root_snapshot_import_total_ms') or 0.0), 3),
                'snapshot_capture_ms': round(float(branch_summary.get('snapshot_capture_total_ms') or 0.0), 3),
                'headless_action_ms': round(float(branch_summary.get('headless_action_total_ms') or 0.0), 3),
                'headless_get_state_ms': round(float(branch_summary.get('headless_get_state_total_ms') or 0.0), 3),
                'headless_capture_ms': round(float(branch_summary.get('headless_capture_total_ms') or 0.0), 3),
                'cli_transport': dict(branch_summary.get('cli_transport_breakdown') or {}),
                'restore_snapshot_ms': round(float(replay_cost.get('restore_snapshot_total_ms') or 0.0), 3),
                'replay_actions_ms': round(float(replay_cost.get('replay_actions_total_ms') or 0.0), 3),
                'get_search_state_ms': round(float(replay_cost.get('get_search_state_total_ms') or 0.0), 3),
                'frontier_workers': int(frontier_workers),
                'frontier_budget_ms': round(float(branch.timing.get('frontier_budget_ms') or 0.0), 3),
                'frontier_jobs_queued': int(branch.timing.get('frontier_jobs_queued') or 0),
                'frontier_jobs_started': int(branch.timing.get('frontier_jobs_started') or 0),
                'frontier_jobs_completed': int(branch.timing.get('frontier_jobs_completed') or 0),
                'frontier_jobs_timed_out': int(branch.timing.get('frontier_jobs_timed_out') or 0),
                'frontier_jobs_cancelled': int(branch.timing.get('frontier_jobs_cancelled') or 0),
            }
            close_started = time.perf_counter()
            branch.close()
            closed = True
            audit['close_ms'] = round((time.perf_counter() - close_started) * 1000.0, 3)
            audit['total_worker_ms'] = round(
                queue_wait_ms + construct_ms + search_ms + float(audit['close_ms']), 3
            )
            return child, {'audit': audit, 'timing': dict(branch.timing), 'coverage': branch._coverage_snapshot()}
        finally:
            if not closed:
                branch.close()
    @staticmethod
    def _canonical_powers(powers: Sequence[Dict[str, Any]]) -> Tuple[Tuple[Any, ...], ...]:
        return tuple(sorted(
            (
                str(power.get('id') or power.get('power_id') or ''),
                float(power.get('amount') or 0.0),
                str(power.get('extra') or ''),
            )
            for power in powers
            if isinstance(power, dict)
        ))

    @staticmethod
    def _canonical_cards(cards: Sequence[Dict[str, Any]]) -> Tuple[Tuple[Any, ...], ...]:
        return tuple(sorted(
            (
                str(card.get('card_id') or card.get('id') or ''),
                int(card.get('upgrade') or card.get('upgrade_count') or 0),
                str(card.get('display_cost', card.get('current_cost'))),
                bool(card.get('display_costs_x')),
            )
            for card in cards
            if isinstance(card, dict)
        ))

    @staticmethod
    def _canonical_objects(values: Sequence[Any]) -> Tuple[str, ...]:
        return tuple(sorted(
            json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
            for value in values
        ))

    @classmethod
    def _enemy_shape(cls, enemy: Dict[str, Any]) -> Tuple[Any, ...]:
        intent = enemy.get('intent') or {}
        return (
            str(enemy.get('monster_id') or enemy.get('id') or ''),
            cls._canonical_powers(enemy.get('powers') or []),
            tuple(str(value) for value in (intent.get('intent_types') or [])),
            intent_total_damage(intent),
            int(intent.get('hits') or 1),
        )

    def _outcome_profile(self, result: SearchResult) -> Dict[str, Any]:
        leaf = result.leaf_state or {}
        combat = leaf.get('combat') or {}
        player = self._leaf_player(result)
        enemies = [
            enemy for enemy in (combat.get('enemies') or [])
            if isinstance(enemy, dict) and float(enemy.get('hp') or 0.0) > 0.0
        ]
        terminal = str(leaf.get('terminal_decision') or '')
        defeat = terminal in {'defeat', 'game_over'}
        hp = float(player.get('hp') or 0.0)
        block = float(player.get('block') or 0.0)
        energy = float(player.get('energy') or 0.0)
        incoming = 0.0
        for enemy in enemies:
            intent = enemy.get('intent') or {}
            types = {str(value).lower() for value in (intent.get('intent_types') or [])}
            if any('attack' in value for value in types):
                incoming += intent_total_damage(intent)
        piles = tuple(
            self._canonical_cards(combat.get(key) or [])
            for key in ('hand', 'draw_pile', 'discard_pile', 'exhaust_pile')
        )
        return {
            'victory': self._is_lethal_result(result),
            'alive': hp > 0.0 and not defeat,
            'hp': hp,
            'block': block,
            'energy': energy,
            'potion_uses': sum(action.action_type == 'use_potion' for action in result.sequence),
            'enemy_count': len(enemies),
            'enemy_effective_hp': sum(
                float(enemy.get('hp') or 0.0) + float(enemy.get('block') or 0.0)
                for enemy in enemies
            ),
            'unblocked_threat': max(0.0, incoming - block),
            'strategic_signature': (
                self._canonical_powers(player.get('powers') or []),
                self._canonical_objects(player.get('relics') or []),
                self._canonical_objects(player.get('potions') or combat.get('potions') or []),
                piles,
                combat.get('turn_number', combat.get('round_number')),
                bool(combat.get('is_player_turn')),
            ),
            'enemy_shapes': Counter(self._enemy_shape(enemy) for enemy in enemies),
        }

    @staticmethod
    def _counter_subset(left: Counter, right: Counter) -> bool:
        return all(count <= right.get(key, 0) for key, count in left.items())

    def _dominance_reasons(self, better: SearchResult, worse: SearchResult) -> Optional[List[str]]:
        a = self._outcome_profile(better)
        b = self._outcome_profile(worse)
        if a['victory'] != b['victory']:
            return ['verified_lethal'] if a['victory'] else None
        if a['alive'] != b['alive']:
            return ['survival'] if a['alive'] else None
        if not a['alive'] and not b['alive']:
            return None
        if not a['victory']:
            if a['strategic_signature'] != b['strategic_signature']:
                return None
            if not self._counter_subset(a['enemy_shapes'], b['enemy_shapes']):
                return None
        maximize = ('hp', 'block', 'energy')
        minimize = ('potion_uses', 'enemy_count', 'enemy_effective_hp', 'unblocked_threat')
        if any(a[key] < b[key] for key in maximize):
            return None
        if any(a[key] > b[key] for key in minimize):
            return None
        reasons = [key for key in maximize if a[key] > b[key]]
        reasons.extend(key for key in minimize if a[key] < b[key])
        return reasons or None

    @staticmethod
    def _action_audit_label(action_info: Dict[str, Any]) -> Dict[str, Any]:
        action = action_info['action']
        return {
            'action_type': action.action_type,
            'card_index': action.card_index,
            'target_index': action.target_index,
            'card_id': (action.metadata or {}).get('card_id'),
        }

    def _select_root_candidate(
        self, adjusted_pairs: List[Tuple[Dict[str, Any], SearchResult]]
    ) -> Tuple[Optional[Tuple[Dict[str, Any], SearchResult]], Dict[str, Any]]:
        """Apply hard combat rules, then score only the Pareto frontier."""
        if not adjusted_pairs:
            return None, {'selection_rule': 'none', 'pareto_frontier_size': 0}
        for action_info, child in adjusted_pairs:
            action_info['lethal_class'] = self._lethal_classification(child)
            action_info['hard_rule_rank'] = 0

        valid_pairs = [pair for pair in adjusted_pairs if self._is_comparable_result(pair[1])]
        invalid = len(adjusted_pairs) - len(valid_pairs)
        if not valid_pairs:
            return None, {'selection_rule': 'no_comparable_result', 'pareto_frontier_size': 0,
                          'invalid_root_candidates': invalid}
        lethal = [pair for pair in valid_pairs if self._is_lethal_result(pair[1])]
        pool = lethal or valid_pairs
        death_pruned = 0
        if not lethal:
            living = [pair for pair in pool if self._outcome_profile(pair[1])['alive']]
            if living:
                death_pruned = len(pool) - len(living)
                pool = living

        frontier: List[Tuple[Dict[str, Any], SearchResult]] = []
        dominated = 0
        for index, candidate in enumerate(pool):
            candidate_info, candidate_child = candidate
            for other_index, other in enumerate(pool):
                if index == other_index:
                    continue
                other_info, other_child = other
                reasons = self._dominance_reasons(other_child, candidate_child)
                if reasons:
                    candidate_info['dominated_by'] = self._action_audit_label(other_info)
                    candidate_info['dominance_reasons'] = reasons
                    dominated += 1
                    break
            else:
                frontier.append(candidate)

        for action_info, _child in pool:
            action_info['hard_rule_rank'] = 1
        for action_info, _child in frontier:
            action_info['hard_rule_rank'] = 2
        best_pair = max(
            frontier,
            key=lambda pair: (
                float(pair[0].get('adjusted_score', pair[1].score)),
                float(pair[0].get('effect_score') or 0.0),
            ),
            default=None,
        )
        if lethal:
            rule = 'clean_lethal' if best_pair and best_pair[0].get('lethal_class') == 'clean' else 'lower_cost_lethal'
        elif death_pruned:
            rule = 'survival'
        elif dominated:
            rule = 'dominance'
        else:
            rule = 'score'
        audit = {
            'selection_rule': rule,
            'invalid_root_candidates': invalid,
            'lethal_candidates': len(lethal),
            'death_pruned_candidates': death_pruned,
            'dominated_candidates': dominated,
            'pareto_frontier_size': len(frontier),
        }
        self.timing['death_pruned_root_candidates'] = death_pruned
        self.timing['dominated_root_candidates'] = dominated
        self.timing['pareto_frontier_size'] = len(frontier)
        return best_pair, audit

    @staticmethod
    def _is_comparable_result(result: SearchResult) -> bool:
        return (math.isfinite(result.score) and result.leaf_state.get('success') is not False
                and result.leaf_state.get('terminal_decision') not in
                {'error', 'failed', 'unknown', 'card_select', 'bundle_select', 'search_state_result'})

    def _prune_counts(self) -> Dict[str, int]:
        keys = (
            'symmetry_pruned_actions', 'state_pruned_actions',
            'unsupported_pruned_actions', 'discard_potion_pruned_actions',
            'noop_potion_pruned_actions', 'budget_pruned_actions',
            'modal_potion_pruned_actions',
        )
        return {key: int(self.timing.get(key) or 0) for key in keys}

    @staticmethod
    def _prune_delta(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
        return {key: max(0, after.get(key, 0) - before.get(key, 0)) for key in after}

    def _root_coverage(
        self,
        available: int,
        candidates: int,
        evaluated: int,
        pruned: Dict[str, int],
    ) -> Dict[str, Any]:
        deduplicated = pruned.get('symmetry_pruned_actions', 0) + pruned.get('state_pruned_actions', 0)
        filtered = max(0, available - candidates - deduplicated)
        return {
            'available_actions': available,
            'candidate_actions': candidates,
            'deduplicated_actions': deduplicated,
            'filtered_actions': filtered,
            'evaluated_actions': evaluated,
            'unevaluated_actions': max(0, candidates - evaluated),
            'candidate_coverage_ratio': (evaluated / candidates) if candidates else 0.0,
            'prune_reasons': pruned,
        }

    def _reset_search_deadline(self) -> None:
        self._search_deadline = (
            time.perf_counter() + (self.max_search_ms / 1000.0)
            if self.max_search_ms > 0.0
            else None
        )

    def _time_budget_exhausted(self) -> bool:
        if self._search_deadline is None:
            return False
        if time.perf_counter() < self._search_deadline:
            return False
        self.timing["time_budget_exhausted"] = 1
        return True

    def _remaining_search_budget_s(self) -> Optional[float]:
        if self._search_deadline is None:
            return None
        return max(0.0, self._search_deadline - time.perf_counter())

    def _engine_call_timeout_s(self) -> Optional[float]:
        """Return a bounded RPC timeout derived from the current search budget.

        The search deadline is cooperative: it can stop Python expansion loops,
        but it cannot interrupt a blocking read from a headless worker. Keep a
        small grace period for a response already in flight, while ensuring an
        engine call cannot outlive the decision by an unbounded amount. A search
        with no deadline keeps the historical blocking behavior for profiling
        and diagnostic runs.
        """
        remaining = self._remaining_search_budget_s()
        if remaining is None:
            return None
        try:
            grace_s = max(
                0.05,
                float(os.environ.get("STS2_ENGINE_RPC_GRACE_MS", "500")) / 1000.0,
            )
        except (TypeError, ValueError):
            grace_s = 0.5
        try:
            minimum_s = max(
                0.05, float(os.environ.get("STS2_ENGINE_RPC_MIN_TIMEOUT_S", "0.25"))
            )
        except (TypeError, ValueError):
            minimum_s = 0.25
        return max(minimum_s, remaining + grace_s)

    def _budget_leaf_result(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        all_actions: Optional[Sequence[SearchAction]] = None,
        legal_actions: Optional[Sequence[Dict[str, Any]]] = None,
        parallel_top_level: bool = False,
    ) -> SearchResult:
        self.timing["time_budget_exhausted"] = 1
        result = self._settled_leaf_result(search_state, history)
        stats = {
            "nodes": 1,
            "available_actions": len(all_actions or []),
            "considered_actions": len(legal_actions or []),
            "symmetry_pruned_actions": self.timing["symmetry_pruned_actions"],
            "time_budget_exhausted": True,
        }
        if parallel_top_level:
            stats["parallel_top_level"] = True
        result.stats.update(stats)
        result.stats["semantic_reuse_safe"] = False
        return result

    def timing_summary(self) -> Dict[str, Any]:
        self._ensure_coverage_sets()
        replay_samples = [float(x) for x in (self.timing.get("replay_ms_samples") or [])]
        branch_samples = self.timing.get("branch_ms_samples") or []
        replay_breakdowns = self.timing.get("replay_breakdown_samples") or []

        def _avg(values: Sequence[float]) -> float:
            return float(sum(values) / len(values)) if values else 0.0

        def _max(values: Sequence[float]) -> float:
            return float(max(values)) if values else 0.0

        def _sum_field(samples: Sequence[Dict[str, Any]], key: str) -> float:
            return float(sum(float(sample.get(key) or 0.0) for sample in samples))

        def _avg_field(samples: Sequence[Dict[str, Any]], key: str) -> float:
            return _sum_field(samples, key) / len(samples) if samples else 0.0

        unique_decision_states = len(self._coverage_sets["decision_states"])
        unique_available_edges = len(self._coverage_sets["available_edges"])
        unique_candidate_edges = len(self._coverage_sets["candidate_edges"])
        unique_expanded_edges = len(self._coverage_sets["expanded_edges"])
        unique_known_unexpanded_edges = len(
            self._coverage_sets["candidate_edges"] - self._coverage_sets["expanded_edges"]
        )
        unique_scored_leaf_states = len(self._coverage_sets["leaf_states"])
        summary = {
            "coverage_schema_version": 3,
            "coverage_edge_identity": "hash_search_state + canonical_action_json",
            "replay_calls": int(self.timing.get("replay_calls") or 0),
            "replay_total_ms": float(self.timing.get("replay_total_ms") or 0.0),
            "replay_avg_ms": _avg(replay_samples),
            "replay_max_ms": _max(replay_samples),
            "worker_context_reuse_hits": int(self.timing.get("worker_context_reuse_hits") or 0),
            "worker_pool_acquire_total_ms": round(float(self.timing.get("worker_pool_acquire_total_ms") or 0.0), 3),
            "worker_process_start_total_ms": round(float(self.timing.get("worker_process_start_total_ms") or 0.0), 3),
            "root_snapshot_import_total_ms": round(float(self.timing.get("root_snapshot_import_total_ms") or 0.0), 3),
            "root_cli_reuse_hits": int(self.timing.get("root_cli_reuse_hits") or 0),
            "headless_action_total_ms": round(float(self.timing.get("headless_action_total_ms") or 0.0), 3),
            "headless_get_state_total_ms": round(float(self.timing.get("headless_get_state_total_ms") or 0.0), 3),
            "headless_capture_total_ms": round(float(self.timing.get("headless_capture_total_ms") or 0.0), 3),
            "headless_fingerprint_total_ms": round(float(self.timing.get("headless_fingerprint_total_ms") or 0.0), 3),
            "headless_wait_profile": {
                key: round(float(value), 3)
                for key, value in (self.timing.get("headless_wait_profile") or {}).items()
            },
            "action_type_timing": {
                key: {
                    field: (int(value) if field == "count" else round(float(value), 3))
                    for field, value in row.items()
                }
                for key, row in sorted((self.timing.get("action_type_timing") or {}).items())
            },
            "slow_action_samples": list(self.timing.get("slow_action_samples") or []),
            "cli_transport_breakdown": {
                category: {
                    "response_bytes": int(self.timing.get(f"transport_{category}_response_bytes") or 0),
                    "queue_wait_total_ms": round(float(self.timing.get(f"transport_{category}_queue_wait_total_ms") or 0.0), 3),
                    "json_parse_total_ms": round(float(self.timing.get(f"transport_{category}_json_parse_total_ms") or 0.0), 3),
                }
                for category in (
                    "action", "get_state", "capture", "fingerprint",
                    "restore", "import", "expand",
                )
            },
            "snapshot_capture_total_ms": round(float(self.timing.get("snapshot_capture_total_ms") or 0.0), 3),
            "snapshot_fingerprint_count": int(self.timing.get("snapshot_fingerprint_count") or 0),
            "snapshot_fingerprint_total_ms": round(float(self.timing.get("snapshot_fingerprint_total_ms") or 0.0), 3),
            "state_dedup_collision_samples": list(
                self.timing.get("state_dedup_collision_samples") or []
            ),
            "branch_samples": len(branch_samples),
            "branch_avg_ms": _avg([float(sample.get("elapsed_ms") or 0.0) for sample in branch_samples]),
            "branch_max_ms": _max([float(sample.get("elapsed_ms") or 0.0) for sample in branch_samples]),
            "snapshot_restore_hits": int(self.timing.get("snapshot_restore_hits") or 0),
            "snapshot_capture_count": int(self.timing.get("snapshot_capture_count") or 0),
            "restore_mode_full_hits": int(self.timing.get("restore_mode_full_hits") or 0),
            "restore_mode_in_place_hits": int(self.timing.get("restore_mode_in_place_hits") or 0),
            "restore_mode_other_hits": int(self.timing.get("restore_mode_other_hits") or 0),
            "restore_full_total_ms": round(float(self.timing.get("restore_full_total_ms") or 0.0), 3),
            "restore_in_place_total_ms": round(float(self.timing.get("restore_in_place_total_ms") or 0.0), 3),
            "in_place_fail_reasons": dict(self.timing.get("in_place_fail_reasons") or {}),
            "subtree_cache_hits": int(self.timing.get("subtree_cache_hits") or 0),
            "dag_cache_hits": int(self.timing.get("dag_cache_hits") or 0),
            "dag_singleflight_waits": int(self.timing.get("dag_singleflight_waits") or 0),
            "strict_dag_enabled": bool(
                getattr(self, "strict_dag_enabled", False)
            ),
            "strict_dag_probe_collisions": int(
                self.timing.get("strict_dag_probe_collisions") or 0
            ),
            "strict_dag_exact_matches": int(
                self.timing.get("strict_dag_exact_matches") or 0
            ),
            "strict_dag_fingerprint_mismatches": int(
                self.timing.get("strict_dag_fingerprint_mismatches") or 0
            ),
            "strict_dag_fingerprint_unavailable": int(
                self.timing.get("strict_dag_fingerprint_unavailable") or 0
            ),
            "strict_dag_nodes_avoided": int(
                self.timing.get("strict_dag_nodes_avoided") or 0
            ),
            "semantic_dag_hits": int(self.timing.get("semantic_dag_hits") or 0),
            "semantic_dag_singleflight_waits": int(
                self.timing.get("semantic_dag_singleflight_waits") or 0
            ),
            "semantic_dag_rejects": int(self.timing.get("semantic_dag_rejects") or 0),
            "semantic_dag_replay_failures": int(
                self.timing.get("semantic_dag_replay_failures") or 0
            ),
            "semantic_dag_nodes_avoided": int(
                self.timing.get("semantic_dag_nodes_avoided") or 0
            ),
            "semantic_dag_reject_reasons": dict(
                self.timing.get("semantic_dag_reject_reasons") or {}
            ),
            "batch_expand_calls": int(self.timing.get("batch_expand_calls") or 0),
            "batch_expand_children": int(self.timing.get("batch_expand_children") or 0),
            "batch_expand_fallbacks": int(self.timing.get("batch_expand_fallbacks") or 0),
            "batch_expand_total_ms": round(float(self.timing.get("batch_expand_total_ms") or 0.0), 3),
            "batch_expand_rpc_failures": int(self.timing.get("batch_expand_rpc_failures") or 0),
            "batch_expand_rpc_timeouts": int(self.timing.get("batch_expand_rpc_timeouts") or 0),
            "engine_rpc_failures": int(self.timing.get("engine_rpc_failures") or 0),
            "engine_rpc_timeouts": int(self.timing.get("engine_rpc_timeouts") or 0),
            "engine_rpc_failure_samples": list(self.timing.get("engine_rpc_failure_samples") or []),
            "eval_cache_hits": int(self.timing.get("eval_cache_hits") or 0),
            "time_budget_ms": float(self.timing.get("time_budget_ms") or 0.0),
            "time_budget_exhausted": bool(self.timing.get("time_budget_exhausted") or 0),
            "horizon_policy": dict(self.timing.get("horizon_policy") or {}),
            "decision_states_prepared": int(self.timing.get("decision_states_prepared") or 0),
            "available_action_edges": int(self.timing.get("available_action_edges") or 0),
            "candidate_action_edges": int(self.timing.get("candidate_action_edges") or 0),
            "expanded_action_edges": int(self.timing.get("expanded_action_edges") or 0),
            "known_unexpanded_action_edges": int(self.timing.get("known_unexpanded_action_edges") or 0),
            "lethal_skipped_action_edges": int(self.timing.get("lethal_skipped_action_edges") or 0),
            "completed_turn_lines": int(self.timing.get("completed_turn_lines") or 0),
            "failed_settlements": int(self.timing.get("failed_settlements") or 0),
            "depth_cutoff_leaves": int(self.timing.get("depth_cutoff_leaves") or 0),
            "horizon_boundary_leaves": int(self.timing.get("horizon_boundary_leaves") or 0),
            "leaf_score_requests": int(self.timing.get("leaf_score_requests") or 0),
            "unique_leaf_scores": int(self.timing.get("unique_leaf_scores") or 0),
            "lethal_early_stops": int(self.timing.get("lethal_early_stops") or 0),
            "root_fair_budget_ms": round(float(self.timing.get("root_fair_budget_ms") or 0.0), 3),
            "root_branch_budget_ms": round(float(self.timing.get("root_branch_budget_ms") or 0.0), 3),
            "frontier_workers": int(self.timing.get("frontier_workers") or 0),
            "root_prepare_workers": int(self.timing.get("root_prepare_workers") or 0),
            "root_prepare_jobs_queued": int(self.timing.get("root_prepare_jobs_queued") or 0),
            "root_prepare_jobs_completed": int(self.timing.get("root_prepare_jobs_completed") or 0),
            "root_prepare_wall_ms": round(float(self.timing.get("root_prepare_wall_ms") or 0.0), 3),
            "frontier_budget_ms": round(float(self.timing.get("frontier_budget_ms") or 0.0), 3),
            "frontier_jobs_queued": int(self.timing.get("frontier_jobs_queued") or 0),
            "frontier_jobs_started": int(self.timing.get("frontier_jobs_started") or 0),
            "frontier_jobs_completed": int(self.timing.get("frontier_jobs_completed") or 0),
            "frontier_jobs_timed_out": int(self.timing.get("frontier_jobs_timed_out") or 0),
            "frontier_jobs_cancelled": int(self.timing.get("frontier_jobs_cancelled") or 0),
            "root_branches_evaluated": int(self.timing.get("root_branches_evaluated") or 0),
            "root_branch_budgets": list(self.timing.get("root_branch_budgets") or []),
            "dominated_root_candidates": int(self.timing.get("dominated_root_candidates") or 0),
            "death_pruned_root_candidates": int(self.timing.get("death_pruned_root_candidates") or 0),
            "pareto_frontier_size": int(self.timing.get("pareto_frontier_size") or 0),
            # Raw counters above count events. These unique counters count a
            # semantic (state, action) edge once, even if a repeated root or state
            # visit reaches it again.
            "unique_decision_states": unique_decision_states,
            "unique_available_action_edges": unique_available_edges,
            "unique_candidate_action_edges": unique_candidate_edges,
            "unique_expanded_action_edges": unique_expanded_edges,
            "unique_known_unexpanded_action_edges": unique_known_unexpanded_edges,
            "unique_scored_leaf_states": unique_scored_leaf_states,
            "repeated_leaf_score_requests": max(
                0, int(self.timing.get("leaf_score_requests") or 0) - unique_scored_leaf_states
            ),
            "repeated_leaf_evaluations": max(
                0, int(self.timing.get("unique_leaf_scores") or 0) - unique_scored_leaf_states
            ),
            "repeated_decision_state_preparations": max(
                0, int(self.timing.get("decision_states_prepared") or 0) - unique_decision_states
            ),
            "repeated_available_action_edges": max(
                0, int(self.timing.get("available_action_edges") or 0) - unique_available_edges
            ),
            "repeated_candidate_action_edges": max(
                0, int(self.timing.get("candidate_action_edges") or 0) - unique_candidate_edges
            ),
            "repeated_expanded_action_edges": max(
                0, int(self.timing.get("expanded_action_edges") or 0) - unique_expanded_edges
            ),
        }

        if getattr(self, 'search_mode', None) == 'beam':
            for key in ('beam_ranking_policy', 'beam_settlement_probes',
                        'beam_settlement_cache_hits', 'beam_settlement_failures',
                        'beam_settlement_ms', 'beam_unranked_budget_nodes'):
                summary[key] = self.timing.get(key)
        summary['parallel_audit'] = dict(self.timing.get('parallel_audit') or {})
        if replay_breakdowns:
            summary["replay_cost_breakdown"] = {
                "start_cli_total_ms": _sum_field(replay_breakdowns, "start_ms"),
                "start_cli_avg_ms": _avg_field(replay_breakdowns, "start_ms"),
                "start_test_combat_total_ms": _sum_field(replay_breakdowns, "start_test_combat_ms"),
                "start_test_combat_avg_ms": _avg_field(replay_breakdowns, "start_test_combat_ms"),
                "restore_snapshot_total_ms": _sum_field(replay_breakdowns, "restore_snapshot_ms"),
                "restore_snapshot_avg_ms": _avg_field(replay_breakdowns, "restore_snapshot_ms"),
                "replay_actions_total_ms": _sum_field(replay_breakdowns, "replay_actions_ms"),
                "replay_actions_avg_ms": _avg_field(replay_breakdowns, "replay_actions_ms"),
                "get_search_state_total_ms": _sum_field(replay_breakdowns, "get_search_state_ms"),
                "get_search_state_avg_ms": _avg_field(replay_breakdowns, "get_search_state_ms"),
            }
        parallel_audit = dict(self.timing.get("parallel_audit") or {})
        branch_audits = list(self.timing.get("root_branch_budgets") or [])
        if parallel_audit or branch_audits:
            def _branch_total(key: str) -> float:
                return float(sum(float(row.get(key) or 0.0) for row in branch_audits))

            def _branch_max(key: str) -> float:
                return float(max((float(row.get(key) or 0.0) for row in branch_audits), default=0.0))

            summary["parallel_phase_breakdown"] = {
                "root_candidate_prepare_ms": float(parallel_audit.get("root_prepare_wall_ms") or 0.0),
                "root_candidate_prepare_replay_ms": float(parallel_audit.get("root_prepare_replay_ms") or 0.0),
                "coordinator_release_ms": float(parallel_audit.get("coordinator_release_ms") or 0.0),
                "executor_wall_ms": float(parallel_audit.get("stage_a_wall_ms") or 0.0),
                "branch_queue_wait_total_ms": _branch_total("queue_wait_ms"),
                "branch_queue_wait_max_ms": _branch_max("queue_wait_ms"),
                "branch_construct_total_ms": _branch_total("construct_ms"),
                "branch_construct_max_ms": _branch_max("construct_ms"),
                "branch_search_total_ms": _branch_total("search_ms"),
                "branch_search_max_ms": _branch_max("search_ms"),
                "branch_close_total_ms": _branch_total("close_ms"),
                "branch_close_max_ms": _branch_max("close_ms"),
                "worker_pool_acquire_total_ms": _branch_total("worker_pool_acquire_ms"),
                "worker_process_start_total_ms": _branch_total("worker_process_start_ms"),
                "root_snapshot_import_total_ms": _branch_total("root_snapshot_import_ms"),
                "snapshot_capture_total_ms": _branch_total("snapshot_capture_ms"),
                "headless_action_total_ms": _branch_total("headless_action_ms"),
                "headless_get_state_total_ms": _branch_total("headless_get_state_ms"),
                "headless_capture_total_ms": _branch_total("headless_capture_ms"),
                "restore_snapshot_total_ms": _branch_total("restore_snapshot_ms"),
                "replay_actions_total_ms": _branch_total("replay_actions_ms"),
                "get_search_state_total_ms": _branch_total("get_search_state_ms"),
                "branch_replay_total_ms": _branch_total("replay_total_ms"),
                "cli_transport": {
                    category: {
                        "response_bytes": int(sum(
                            int(((row.get("cli_transport") or {}).get(category) or {}).get("response_bytes") or 0)
                            for row in branch_audits
                        )),
                        "queue_wait_total_ms": float(sum(
                            float(((row.get("cli_transport") or {}).get(category) or {}).get("queue_wait_total_ms") or 0.0)
                            for row in branch_audits
                        )),
                        "json_parse_total_ms": float(sum(
                            float(((row.get("cli_transport") or {}).get(category) or {}).get("json_parse_total_ms") or 0.0)
                            for row in branch_audits
                        )),
                    }
                    for category in (
                        "action", "get_state", "capture", "fingerprint",
                        "restore", "import", "expand",
                    )
                },
            }
        return summary

    def close(self) -> None:
        with self._cli_lock:
            adapters = [ctx.cli for ctx in self._cli_by_thread.values() if ctx.owned]
            self._cli_by_thread.clear()
            borrowed = self._pooled_borrowed
            self._pooled_borrowed = []
        for cli in adapters:
            try:
                cli.stop()
            except Exception:
                pass
        # Release pooled processes back to the pool (warm) rather than stopping
        # them, so the next decision step reuses them and skips ModelDB init.
        # discard() already removed any suspect process from the pool, so only
        # release ones the pool still owns.
        if self.worker_pool is not None:
            for cli in borrowed:
                self.worker_pool.release(cli)

    def _discard_worker_ctx(
        self,
        worker_ctx: Optional[_CliWorkerContext],
        *,
        reason: str = 'worker_context_failure',
    ) -> None:
        if worker_ctx is None:
            return
        thread_id = threading.get_ident()
        with self._cli_lock:
            if self._cli_by_thread.get(thread_id) is worker_ctx:
                self._cli_by_thread.pop(thread_id, None)
        if worker_ctx.owned:
            try:
                worker_ctx.cli.stop()
            except Exception:
                pass
        elif self.worker_pool is not None:
            # Pooled (owned=False) worker: a discard means the process is
            # suspect (mid-search corruption / failed restore), so evict it from
            # the pool too. Leaving it would let the next step reuse a bad
            # process — exactly the corruption the discard path guards against.
            with self._cli_lock:
                if worker_ctx.cli in self._pooled_borrowed:
                    self._pooled_borrowed.remove(worker_ctx.cli)
            self.worker_pool.discard(worker_ctx.cli, reason=reason)

    def _reset_current_thread_worker(self) -> None:
        thread_id = threading.get_ident()
        with self._cli_lock:
            worker_ctx = self._cli_by_thread.get(thread_id)
        # Recycle pooled workers warm: the next _get_reusable_cli re-imports the
        # root and the subsequent restore is forced full (see pooled flag), so
        # this is bit-equivalent to the baseline's discard-then-fresh-full reset
        # while still skipping the ModelDB init. Owned (non-pooled) workers are
        # stopped, exactly reproducing the prior reuse_cli_processes behavior.
        self._recycle_worker_ctx(worker_ctx)

    def _recycle_worker_ctx(self, worker_ctx: Optional[_CliWorkerContext]) -> None:
        """Routine reset: drop the worker's INTERMEDIATE snapshots so the next
        sibling rebuilds cleanly from the root instead of reusing drifty
        mid-replay snapshots (the actual correctness mechanism of the per-sibling
        reset — see callsite). It does NOT require killing the process.

        - POOLED process: released back to the pool WARM (ModelDB stays
          initialized); the next acquire re-imports the root.
        - OWNED process: kept WARM and rebound, with snapshot bookkeeping reset to
          the root only. The next sibling restores the still-pristine root snapshot
          (root in_place restore is bit-identical to force_full even after churn —
          verified by churn_diff_oracle + the 1368-1370 note) and replays the full
          suffix. This is the same clean-root-rebuild the old stop()+respawn gave,
          minus the ~840ms cold respawn + full-warmup restore per sibling that
          showed up as the owned-CLI `no_run` thrash storm (Task A: 49 vs pooled 5).
        """
        if worker_ctx is None:
            return
        thread_id = threading.get_ident()
        with self._cli_lock:
            pooled = (not worker_ctx.owned) and worker_ctx.cli in self._pooled_borrowed
            if worker_ctx.owned:
                # Warm-reset an owned worker: clear intermediate snapshots, keep
                # the root mapping, leave it bound for the next sibling. Only fall
                # through to stop() if there is no root snapshot to rebuild from.
                root_id = worker_ctx.snapshot_ids_by_history.get(tuple())
                if root_id is not None:
                    worker_ctx.snapshot_ids_by_history = {tuple(): root_id}
                    root_fingerprint = worker_ctx.snapshot_fingerprints_by_history.get(tuple())
                    worker_ctx.snapshot_fingerprints_by_history = (
                        {tuple(): root_fingerprint} if root_fingerprint else {}
                    )
                    root_semantic_fingerprint = (
                        worker_ctx.snapshot_semantic_fingerprints_by_history.get(tuple())
                    )
                    worker_ctx.snapshot_semantic_fingerprints_by_history = (
                        {tuple(): root_semantic_fingerprint}
                        if root_semantic_fingerprint else {}
                    )
                    return
            if self._cli_by_thread.get(thread_id) is worker_ctx:
                self._cli_by_thread.pop(thread_id, None)
            if pooled:
                self._pooled_borrowed.remove(worker_ctx.cli)
        if worker_ctx.owned:
            try:
                worker_ctx.cli.stop()
            except Exception:
                pass
        elif pooled and self.worker_pool is not None:
            self.worker_pool.release(worker_ctx.cli)

    def _forget_intermediate_snapshots(self) -> None:
        """Keep the live worker and root snapshot, but force full root replay.

        This is a correctness oracle for intermediate restoration. Releasing a
        pooled worker would also re-import the serialized root and mix snapshot
        import behavior into the comparison, so the diagnostic path only drops
        process-local descendant indexes.
        """
        thread_id = threading.get_ident()
        with self._cli_lock:
            worker_ctx = self._cli_by_thread.get(thread_id)
            if worker_ctx is None:
                return
            root_key = tuple()
            root_id = worker_ctx.snapshot_ids_by_history.get(root_key)
            if root_id is None:
                return
            worker_ctx.snapshot_ids_by_history = {root_key: root_id}
            root_fingerprint = worker_ctx.snapshot_fingerprints_by_history.get(root_key)
            worker_ctx.snapshot_fingerprints_by_history = (
                {root_key: root_fingerprint} if root_fingerprint else {}
            )
            root_semantic_fingerprint = (
                worker_ctx.snapshot_semantic_fingerprints_by_history.get(root_key)
            )
            worker_ctx.snapshot_semantic_fingerprints_by_history = (
                {root_key: root_semantic_fingerprint}
                if root_semantic_fingerprint else {}
            )

    def _get_reusable_cli(self) -> tuple[_CliWorkerContext, float]:
        thread_id = threading.get_ident()
        with self._cli_lock:
            ctx = self._cli_by_thread.get(thread_id)
            if ctx is not None:
                self.timing["worker_context_reuse_hits"] += 1
                return ctx, 0.0
            # Cross-step warm-process path: borrow a process from the pool and
            # import this step's root into it. A warm process serves the import
            # via the ~2ms in_place restore instead of the ~340ms full restore,
            # because ModelDB is already initialized and a live run exists.
            if self.worker_pool is not None and self.root_snapshot_json is not None:
                acquire_started = time.perf_counter()
                cli = self.worker_pool.acquire()
                self.timing["worker_pool_acquire_total_ms"] += (
                    time.perf_counter() - acquire_started
                ) * 1000.0
                snapshot_id = self.root_snapshot_id or "imported_root_snapshot"
                import_started = time.perf_counter()
                try:
                    import_result = cli.import_combat_snapshot(
                        self.root_snapshot_json,
                        snapshot_id,
                        timeout_s=self._engine_call_timeout_s(),
                    )
                    self._record_cli_transport(cli, "import")
                    self.timing["root_snapshot_import_total_ms"] += (
                        time.perf_counter() - import_started
                    ) * 1000.0
                except Exception as exc:
                    self.worker_pool.discard(
                        cli, reason=f'root_snapshot_import_exception:{type(exc).__name__}'
                    )
                    raise
                if not import_result.get("success"):
                    # A pooled process that cannot import is unusable; drop it and
                    # let the next acquire start a fresh one.
                    self.worker_pool.discard(cli, reason='root_snapshot_import_failed')
                    raise RuntimeError(f"Failed to import root combat snapshot (pooled): {import_result}")
                # owned=False: the pool owns the process lifecycle, so this
                # searcher's close() releases it back to the pool rather than
                # stopping it. Track it for release.
                ctx = _CliWorkerContext(cli=cli, snapshot_ids_by_history={}, owned=False)
                ctx.snapshot_ids_by_history[tuple()] = snapshot_id
                root_fingerprint = str(import_result.get("state_fingerprint") or "")
                if root_fingerprint:
                    ctx.snapshot_fingerprints_by_history[tuple()] = root_fingerprint
                root_semantic_fingerprint = str(
                    import_result.get("semantic_state_fingerprint") or ""
                )
                if root_semantic_fingerprint:
                    ctx.snapshot_semantic_fingerprints_by_history[tuple()] = (
                        root_semantic_fingerprint
                    )
                self._cli_by_thread[thread_id] = ctx
                self._pooled_borrowed.append(cli)
                return ctx, cli.last_call_ms
            if self.root_snapshot_json is not None:
                cli = Sts2CliAdapter(self.cli_cfg)
                process_started = time.perf_counter()
                cli.start()
                self.timing["worker_process_start_total_ms"] += (
                    time.perf_counter() - process_started
                ) * 1000.0
                ctx = _CliWorkerContext(cli=cli, snapshot_ids_by_history={}, owned=True)
                snapshot_id = self.root_snapshot_id or "imported_root_snapshot"
                import_started = time.perf_counter()
                import_result = cli.import_combat_snapshot(
                    self.root_snapshot_json,
                    snapshot_id,
                    timeout_s=self._engine_call_timeout_s(),
                )
                self._record_cli_transport(cli, "import")
                self.timing["root_snapshot_import_total_ms"] += (
                    time.perf_counter() - import_started
                ) * 1000.0
                if not import_result.get("success"):
                    raise RuntimeError(f"Failed to import root combat snapshot: {import_result}")
                ctx.snapshot_ids_by_history[tuple()] = snapshot_id
                root_fingerprint = str(import_result.get("state_fingerprint") or "")
                if root_fingerprint:
                    ctx.snapshot_fingerprints_by_history[tuple()] = root_fingerprint
                root_semantic_fingerprint = str(
                    import_result.get("semantic_state_fingerprint") or ""
                )
                if root_semantic_fingerprint:
                    ctx.snapshot_semantic_fingerprints_by_history[tuple()] = (
                        root_semantic_fingerprint
                    )
                self._cli_by_thread[thread_id] = ctx
                return ctx, cli.last_call_ms
            if self.root_cli is not None:
                self.timing["root_cli_reuse_hits"] += 1
                ctx = _CliWorkerContext(
                    cli=self.root_cli,
                    snapshot_ids_by_history={},
                    owned=False,
                )
                if self.root_snapshot_id is not None:
                    ctx.snapshot_ids_by_history[tuple()] = self.root_snapshot_id
                self._cli_by_thread[thread_id] = ctx
                return ctx, 0.0
            cli = Sts2CliAdapter(self.cli_cfg)
            process_started = time.perf_counter()
            cli.start()
            self.timing["worker_process_start_total_ms"] += (
                time.perf_counter() - process_started
            ) * 1000.0
            ctx = _CliWorkerContext(cli=cli, snapshot_ids_by_history={}, owned=True)
            self._cli_by_thread[thread_id] = ctx
            return ctx, cli.last_call_ms

    @staticmethod
    def _infer_root_snapshot_enemy_count(root_snapshot_json: Optional[str]) -> Optional[int]:
        if not root_snapshot_json:
            return None
        try:
            payload = json.loads(root_snapshot_json)
            enemy_states = payload.get("EnemyCreatureStates")
            if isinstance(enemy_states, list):
                return len(enemy_states)
        except Exception:
            return None
        return None

    @staticmethod
    def _has_complete_root_rng_state(root_snapshot_json: Optional[str]) -> bool:
        if not root_snapshot_json:
            return True
        try:
            payload = json.loads(root_snapshot_json)
            streams = list(payload.get("RunRngStates") or []) + list(payload.get("PlayerRngStates") or [])
            return bool(streams) and all(
                all(stream.get(key) is not None for key in ("S0", "S1", "S2", "S3"))
                for stream in streams if isinstance(stream, dict)
            )
        except Exception:
            return False

    def _should_disable_imported_root_cli_reuse(self) -> bool:
        return False

    def _should_use_semantic_replay_resolution(self) -> bool:
        # Imported live-root multi-enemy combats can reshuffle enemy ordering even
        # across fresh snapshot imports. Replay by stable semantic identity rather
        # than target_index in those cases.
        return self.root_snapshot_json is not None and (self._root_snapshot_enemy_count or 0) > 1

    @staticmethod
    def _recorded_action_from_search_action(action: SearchAction) -> RecordedAction:
        action_name, args = cli_payload_for_action(action)
        recorded_args = dict(args)
        metadata = action.metadata or {}
        for key in ("card_id", "target_monster_id", "target_type", "potion_id"):
            value = metadata.get(key)
            if value is not None:
                recorded_args[key] = value
        return RecordedAction(action_name, recorded_args)

    @staticmethod
    def _strip_cli_payload(args: Mapping[str, Any], action_name: str) -> Dict[str, Any]:
        if action_name == "play_card":
            payload: Dict[str, Any] = {"card_index": int(args["card_index"])}
            if args.get("target_index") is not None:
                payload["target_index"] = int(args["target_index"])
            return payload
        if action_name == "use_potion":
            payload = {"potion_index": int(args["potion_index"])}
            if args.get("target_index") is not None:
                payload["target_index"] = int(args["target_index"])
            return payload
        if action_name == "discard_potion":
            return {"potion_index": int(args["potion_index"])}
        return {}

    def _resolve_replay_payload(self, current_search_state: Dict[str, Any], step: RecordedAction) -> Dict[str, Any]:
        payload = self._strip_cli_payload(step.args, step.action)
        available = ((current_search_state.get("combat") or {}).get("available_actions") or [])

        if step.action in {"use_potion", "discard_potion"}:
            chosen_potion_id = str(step.args.get("potion_id") or "")
            chosen_potion_index = step.args.get("potion_index")
            chosen_target_monster_id = str(step.args.get("target_monster_id") or "")
            chosen_target_type = str(step.args.get("target_type") or "")

            def matches_potion(av: Dict[str, Any]) -> bool:
                if str(av.get("action_type") or "") != step.action:
                    return False
                metadata = av.get("metadata") or {}
                if chosen_potion_id and str(metadata.get("potion_id") or "") != chosen_potion_id:
                    return False
                if chosen_target_monster_id:
                    return str(metadata.get("target_monster_id") or "") == chosen_target_monster_id
                if chosen_target_type in {"AnyEnemy", "SingleEnemy"}:
                    return av.get("target_index") is not None
                return True

            exact = [av for av in available if matches_potion(av)]
            if chosen_potion_index is not None:
                same_potion_index = [
                    av for av in exact
                    if int((av.get("metadata") or {}).get(
                        "potion_index", av.get("potion_index", -1)
                    )) == int(chosen_potion_index)
                ]
                if same_potion_index:
                    exact = same_potion_index
            if chosen_target_monster_id and step.args.get("target_index") is not None:
                same_index = [
                    av for av in exact
                    if av.get("target_index") == step.args.get("target_index")
                ]
                if same_index:
                    exact = same_index
            if exact:
                resolved = {
                    "potion_index": int((exact[0].get("metadata") or {}).get("potion_index", exact[0].get("potion_index", 0)))
                }
                if exact[0].get("target_index") is not None:
                    resolved["target_index"] = int(exact[0]["target_index"])
                return resolved
            return payload

        if step.action != "play_card":
            return payload

        chosen_card_id = str(step.args.get("card_id") or "")
        chosen_card_index = step.args.get("card_index")
        chosen_target_monster_id = str(step.args.get("target_monster_id") or "")
        chosen_target_type = str(step.args.get("target_type") or "")
        target_index = step.args.get("target_index")

        def matches_card(av: Dict[str, Any]) -> bool:
            if str(av.get("action_type") or "") != "play_card":
                return False
            metadata = av.get("metadata") or {}
            if chosen_card_id and str(metadata.get("card_id") or "") != chosen_card_id:
                return False
            if chosen_target_monster_id:
                return str(metadata.get("target_monster_id") or "") == chosen_target_monster_id
            av_target = av.get("target_index")
            if target_index is not None and av_target != target_index:
                return False
            if target_index is None and chosen_target_type in {"AnyEnemy", "SingleEnemy"}:
                return False
            return True

        exact = [av for av in available if matches_card(av)]
        if chosen_card_index is not None:
            # Duplicate cards with the same id can carry distinct upgrades,
            # enchantments, runtime ids, or history identity. Preserve the
            # recorded hand slot whenever it still names a legal matching edge;
            # semantic relocation is only a fallback for a genuinely reordered
            # imported snapshot.
            same_card_index = [
                av for av in exact
                if av.get("card_index") == chosen_card_index
            ]
            if same_card_index:
                exact = same_card_index
        if chosen_target_monster_id and target_index is not None:
            # monster_id is not unique: encounters may contain several copies
            # of the same model. Prefer the original target slot when it still
            # exists; only use id-only relocation after an actual reorder.
            same_index = [av for av in exact if av.get("target_index") == target_index]
            if same_index:
                exact = same_index
        if exact:
            resolved = {"card_index": int(exact[0]["card_index"])}
            if exact[0].get("target_index") is not None:
                resolved["target_index"] = int(exact[0]["target_index"])
            return resolved
        return payload

    def _record_headless_timing(
        self, result: Optional[Dict[str, Any]], field: str, counter: str
    ) -> None:
        if not isinstance(result, dict):
            return
        try:
            self.timing[counter] = float(self.timing.get(counter) or 0.0) + float(
                result.get(field) or 0.0
            )
        except (TypeError, ValueError):
            return

    def _record_action_execution_profile(
        self,
        result: Optional[Dict[str, Any]],
        step: RecordedAction,
        *,
        history_len: int,
        suffix_len: int,
        cli_ms: float,
    ) -> None:
        """Retain bounded diagnostics for slow native actions.

        This reads telemetry returned by the headless runtime and never changes
        replay, settlement, caching, or action ordering.
        """
        if not isinstance(result, dict):
            return
        profile = result.get("headless_wait_profile")
        if not isinstance(profile, dict):
            profile = {}

        def number(key: str) -> float:
            try:
                return float(profile.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        try:
            engine_ms = float(result.get("headless_execute_ms") or 0.0)
        except (TypeError, ValueError):
            engine_ms = 0.0
        card_id = str(step.args.get("card_id") or "") if isinstance(step.args, dict) else ""
        action_key = step.action if not card_id else f"{step.action}:{card_id}"
        sample = {
            "action": step.action,
            "card_id": card_id or None,
            "target_index": step.args.get("target_index") if isinstance(step.args, dict) else None,
            "result_type": result.get("type"),
            "result_message": result.get("message"),
            "end_turn_stalled": bool(result.get("end_turn_stalled")),
            "end_turn_control_state": result.get("end_turn_control_state"),
            "history_len": int(history_len),
            "suffix_len": int(suffix_len),
            "cli_ms": round(float(cli_ms or 0.0), 3),
            "engine_ms": round(engine_ms, 3),
            "wait_total_ms": round(number("wait_total_ms"), 3),
            "sleep_total_ms": round(number("sleep_total_ms"), 3),
            "wait_calls": int(number("wait_calls")),
            "wait_iterations": int(number("wait_iterations")),
            "max_wait_iterations": int(number("max_wait_iterations")),
            "sleep_calls": int(number("sleep_calls")),
            "end_turn_pump_iterations": int(number("end_turn_pump_iterations")),
            "end_turn_pump_sleep_ms": round(number("end_turn_pump_sleep_ms"), 3),
            "end_turn_stalls": int(number("end_turn_stalls")),
        }
        lock = getattr(self, "_audit_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._audit_lock = lock
        with lock:
            aggregate = self.timing.setdefault("headless_wait_profile", {})
            for key in (
                "wait_total_ms", "sleep_total_ms", "wait_calls",
                "wait_iterations", "sleep_calls", "end_turn_pump_iterations",
                "end_turn_pump_sleep_ms", "end_turn_stalls",
            ):
                aggregate[key] = float(aggregate.get(key) or 0.0) + number(key)
            aggregate["max_wait_iterations"] = max(
                int(aggregate.get("max_wait_iterations") or 0),
                int(number("max_wait_iterations")),
            )

            by_type = self.timing.setdefault("action_type_timing", {})
            row = by_type.setdefault(action_key, {
                "count": 0,
                "cli_total_ms": 0.0,
                "engine_total_ms": 0.0,
                "wait_total_ms": 0.0,
                "sleep_total_ms": 0.0,
                "max_cli_ms": 0.0,
            })
            row["count"] += 1
            row["cli_total_ms"] += float(cli_ms or 0.0)
            row["engine_total_ms"] += engine_ms
            row["wait_total_ms"] += number("wait_total_ms")
            row["sleep_total_ms"] += number("sleep_total_ms")
            row["max_cli_ms"] = max(
                float(row.get("max_cli_ms") or 0.0), float(cli_ms or 0.0)
            )

            slow = self.timing.setdefault("slow_action_samples", [])
            slow.append(sample)
            slow.sort(key=lambda item: float(item.get("cli_ms") or 0.0), reverse=True)
            del slow[20:]

    def _record_cli_transport(self, cli: Sts2CliAdapter, category: str) -> None:
        if category not in {
            "action", "get_state", "capture", "fingerprint",
            "restore", "import", "expand",
        }:
            return
        self.timing[f"transport_{category}_response_bytes"] += int(
            getattr(cli, "last_response_bytes", 0) or 0
        )
        self.timing[f"transport_{category}_queue_wait_total_ms"] += float(
            getattr(cli, "last_response_queue_wait_ms", 0.0) or 0.0
        )
        self.timing[f"transport_{category}_json_parse_total_ms"] += float(
            getattr(cli, "last_json_parse_ms", 0.0) or 0.0
        )

    def _record_restore_mode(self, restore_result: Optional[Dict[str, Any]]) -> None:
        """Accumulate restore_mode + restore_timing from a restore response.

        Pure observability: reads fields the C# RestoreCombatSnapshot already
        returns (restore_mode, restore_timing_ms.total_ms) and never alters the
        restore itself. Lets timing_summary report how many restores took the
        slow `full` bootstrap path vs the fast `in_place` path, and their total
        cost — the data that decides where the restore-path fix should go.
        """
        if not isinstance(restore_result, dict):
            return
        mode = restore_result.get("restore_mode")
        timing = restore_result.get("restore_timing_ms") or {}
        try:
            total_ms = float(timing.get("total_ms") or 0.0)
        except (TypeError, ValueError):
            total_ms = 0.0
        if mode == "full":
            self.timing["restore_mode_full_hits"] += 1
            self.timing["restore_full_total_ms"] += total_ms
            reason = timing.get("in_place_fail_reason")
            if reason:
                tally = self.timing.setdefault("in_place_fail_reasons", {})
                tally[reason] = tally.get(reason, 0) + 1
        elif mode == "in_place":
            self.timing["restore_mode_in_place_hits"] += 1
            self.timing["restore_in_place_total_ms"] += total_ms
        else:
            self.timing["restore_mode_other_hits"] += 1

    def combat_to_state(self, history: Sequence[RecordedAction], prefer_cold: bool = False) -> Dict[str, Any]:
        # Retry around the core build. A pooled worker can silently drop out of
        # combat after import/restore (the "Not in combat" failure);
        # _combat_to_state_once discards that suspect worker and returns an
        # error state. A single retry is not enough: the search loop can poison
        # MORE than one pooled worker, so re-acquiring from the pool may hand
        # back another bad one. The retry therefore forces a FRESH cold process
        # (force_cold=True), bypassing the pool free-list entirely — the cold
        # import+restore path is the one verified to always restore these
        # snapshots correctly. This recovers the turn instead of dropping it to
        # the no-op fallback. Only retry the worker-failure error
        # (terminal_type=="error"), never a legitimate terminal/victory state.
        #
        # prefer_cold: callers for which CORRECTNESS dominates (the leaf
        # death-resolution end_turn — the lethal verdict) force the cold path
        # outright. A churned pooled worker can fail to resolve the enemy turn on
        # an end_turn replay (returns the unresolved pre-enemy-turn state, hp
        # unchanged), making a lethal line look survivable. The cold rebuild
        # resolves it correctly. Leaf resolutions are far rarer than total
        # restores, so paying the cold cost only there keeps search fast while
        # making the most decision-critical step exact.
        if prefer_cold and self.reuse_cli_processes:
            return self._combat_to_state_once(history, force_cold=True)
        result = self._combat_to_state_once(history)
        if (
            self.reuse_cli_processes
            and isinstance(result, dict)
            and result.get("success") is False
            and result.get("terminal_type") == "error"
        ):
            self.timing["worker_error_retries"] = self.timing.get("worker_error_retries", 0) + 1
            result = self._combat_to_state_once(history, force_cold=True)
        elif self.reuse_cli_processes and self._is_suspicious_victory(result):
            # A victory terminal reporting ZERO surviving enemies while the root
            # still had living enemy HP was, historically, the signature of a
            # poisoned worker (a card/modal potion left in_place restore silently
            # corrupting the enemy list -> phantom combat-clear). The original fix
            # re-adjudicated every such victory on a FRESH cold process.
            #
            # That cold re-adjudication is now REDUNDANT and was the dominant cost
            # of the B-class full-restore storm on low-HP multi-enemy fights
            # (SLIMES/NIBBITS): every genuine kill there looks "suspicious" (root
            # had HP, line cleared it), so the heuristic fired on nearly every
            # winning leaf -> a fresh ~620ms cold process spawn each time
            # (seed586918 SLIMES: 120 spawns, 113s serial). The phantom-victory
            # corruption it guarded against was root-caused and fixed at the engine
            # level (the zero-drift work: reactivate combat phase / ready-set reset
            # / IsActiveForHooks / per-combat relic reset / IsPlayPhase re-arm), and
            # in_place is now bit-identical to force_full (70/71-unit parity, 0
            # DIFF). Direct proof the re-adjudication no longer changes any verdict:
            # STS2_DIAG_SUSPVIC on the SLIMES storm shows 120/120 in_place==cold,
            # 0 disagreements. So trust the in_place verdict and skip the cold spawn
            # (storm collapses 64s->3.6s, same chosen action + score).
            #
            # STS2_FORCE_SUSPVIC_READJUDICATE=1 restores the old always-cold
            # behavior as an escape hatch if a future encounter is found where
            # in_place victory detection regresses. STS2_DIAG_SUSPVIC=1 logs the
            # in_place-vs-cold agreement tally without changing behavior.
            self.timing["suspicious_victory_retries"] = (
                self.timing.get("suspicious_victory_retries", 0) + 1
            )
            diag = os.environ.get("STS2_DIAG_SUSPVIC") == "1"
            readjudicate = os.environ.get("STS2_FORCE_SUSPVIC_READJUDICATE") == "1"
            if diag or readjudicate:
                cold = self._combat_to_state_once(history, force_cold=True)
                if diag:
                    def _vk(r):
                        if not isinstance(r, dict):
                            return ("non_dict",)
                        return (
                            str(r.get("terminal_decision") or ""),
                            round(float(r.get("terminal_surviving_enemy_hp") or 0.0), 1),
                            bool(r.get("success")),
                        )
                    ip_k = _vk(result)
                    cold_k = _vk(cold)
                    tally = self.timing.setdefault(
                        "suspvic_agree", {"agree": 0, "disagree": 0, "examples": []}
                    )
                    if ip_k == cold_k:
                        tally["agree"] += 1
                    else:
                        tally["disagree"] += 1
                        if len(tally["examples"]) < 10:
                            tally["examples"].append({"in_place": ip_k, "cold": cold_k})
                if readjudicate:
                    result = cold
        return result

    @staticmethod
    def _is_suspicious_victory(result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        if str(result.get("terminal_decision") or "") not in {
            "card_reward", "victory", "map_select", "treasure", "rest_site", "shop",
        }:
            return False
        # Zero surviving enemies is required for a victory claim; the suspicious
        # case is a claimed clear where the ROOT still had living enemy HP (you
        # cannot have removed it within this short search line if the worker
        # state is sound — and if you genuinely did, the cold re-adjudication
        # will confirm the same victory, so this never discards a real kill).
        surviving = float(result.get("terminal_surviving_enemy_hp") or 0.0)
        root_enemy_hp = float(result.get("terminal_root_enemy_hp") or 0.0)
        return surviving <= 0.0 and root_enemy_hp > 0.0


    def _combat_to_state_once(
        self, history: Sequence[RecordedAction], force_cold: bool = False
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        start_ms = 0.0
        start_combat_ms = 0.0
        restore_snapshot_ms = 0.0
        replay_actions_ms = 0.0
        get_state_ms = 0.0
        last_action_result: Optional[Dict[str, Any]] = None
        cli: Optional[Sts2CliAdapter] = None
        worker_ctx: Optional[_CliWorkerContext] = None
        should_stop = False
        imported_root_loaded = False
        try:
            if self.reuse_cli_processes and not force_cold:
                worker_ctx, start_ms = self._get_reusable_cli()
                cli = worker_ctx.cli
            else:
                cli = Sts2CliAdapter(self.cli_cfg)
                cli.start()
                start_ms = cli.last_call_ms
                should_stop = True
                if self.root_snapshot_json is not None:
                    snapshot_id = self.root_snapshot_id or "imported_root_snapshot"
                    import_result = cli.import_combat_snapshot(
                        self.root_snapshot_json,
                        snapshot_id,
                        timeout_s=self._engine_call_timeout_s(),
                    )
                    self._record_cli_transport(cli, "import")
                    if not import_result.get("success"):
                        raise RuntimeError(f"Failed to import root combat snapshot: {import_result}")
                    restore_snapshot_ms = cli.last_call_ms
                    restore_result = cli.restore_combat_snapshot(
                        snapshot_id,
                        lang=self.combat_spec.lang,
                        compact=True,
                        timeout_s=self._engine_call_timeout_s(),
                    )
                    self._record_cli_transport(cli, "restore")
                    if restore_result.get("type") == "error":
                        raise RuntimeError(f"Failed to restore imported root combat snapshot: {restore_result}")
                    restore_snapshot_ms += cli.last_call_ms
                    self.timing["snapshot_restore_hits"] += 1
                    self._record_restore_mode(restore_result)
                    imported_root_loaded = True

            suffix_history = list(history)
            restored_from_snapshot = False
            restored_prefix_len = 0
            if worker_ctx is not None and history:
                snapshot_key, snapshot_id = self._find_best_snapshot(worker_ctx, history)
                if snapshot_id is not None:
                    restore_result = cli.restore_combat_snapshot(
                        snapshot_id,
                        lang=self.combat_spec.lang,
                        compact=True,
                        timeout_s=self._engine_call_timeout_s(),
                    )
                    self._record_cli_transport(cli, "restore")
                    restore_snapshot_ms = cli.last_call_ms
                    self._record_restore_mode(restore_result)
                    if isinstance(restore_result, dict) and restore_result.get("type") == "error":
                        raise RuntimeError(
                            f"Failed to restore intermediate combat snapshot: {restore_result}"
                        )
                    restored_from_snapshot = True
                    self.timing["snapshot_restore_hits"] += 1
                    restored_prefix_len = len(snapshot_key)
                    suffix_history = list(history[len(snapshot_key):])
            elif imported_root_loaded:
                restored_from_snapshot = True

            if not restored_from_snapshot:
                root_snapshot_id = worker_ctx.snapshot_ids_by_history.get(tuple()) if worker_ctx is not None else None
                if root_snapshot_id is not None:
                    restore_result = cli.restore_combat_snapshot(
                        root_snapshot_id,
                        lang=self.combat_spec.lang,
                        compact=True,
                        timeout_s=self._engine_call_timeout_s(),
                    )
                    self._record_cli_transport(cli, "restore")
                    restore_snapshot_ms = cli.last_call_ms
                    # Mirror the non-pooled import path (which raises on a failed
                    # restore): a silently-failed restore here is the upstream of
                    # the "Not in combat" empty-state below. Fail loudly so the
                    # suspect worker is discarded and the retry path engages.
                    if isinstance(restore_result, dict) and restore_result.get("type") == "error":
                        raise RuntimeError(
                            f"Failed to restore pooled root snapshot: {restore_result}"
                        )
                    self.timing["snapshot_restore_hits"] += 1
                    self._record_restore_mode(restore_result)
                else:
                    cli.start_test_combat(
                        character=self.combat_spec.character,
                        encounter=self.combat_spec.encounter,
                        seed=self.combat_spec.seed,
                        ascension=self.combat_spec.ascension,
                        lang=self.combat_spec.lang,
                        timeout_s=self._engine_call_timeout_s(),
                    )
                    start_combat_ms = cli.last_call_ms
                    if self.player_overrides:
                        cli.send(
                            {"cmd": "set_player", **self.player_overrides},
                            timeout_s=self._engine_call_timeout_s(),
                        )
                    if worker_ctx is not None:
                        self._capture_snapshot(worker_ctx, tuple())

            current_search_state: Optional[Dict[str, Any]] = None
            if suffix_history and self._should_use_semantic_replay_resolution():
                current_state_result = cli.get_search_state(
                    timeout_s=self._engine_call_timeout_s()
                )
                self._record_cli_transport(cli, "get_state")
                get_state_ms += cli.last_call_ms
                self._record_headless_timing(
                    current_state_result, "headless_build_state_ms", "headless_get_state_total_ms"
                )
                current_search_state = self._extract_search_state(current_state_result)

            for offset, step in enumerate(suffix_history, start=1):
                replay_payload = (
                    self._resolve_replay_payload(current_search_state, step)
                    if current_search_state is not None
                    else self._strip_cli_payload(step.args, step.action)
                )
                last_action_result = cli.action(
                    step.action,
                    args=replay_payload,
                    with_snapshot=False,
                    compact=True,
                    timeout_s=self._engine_call_timeout_s(),
                )
                self._record_cli_transport(cli, "action")
                replay_actions_ms += cli.last_call_ms
                self._record_headless_timing(
                    last_action_result, "headless_execute_ms", "headless_action_total_ms"
                )
                self._record_action_execution_profile(
                    last_action_result,
                    step,
                    history_len=len(history),
                    suffix_len=len(suffix_history),
                    cli_ms=float(cli.last_call_ms or 0.0),
                )
                if worker_ctx is not None:
                    prefix_len = restored_prefix_len + offset
                    self._capture_snapshot(worker_ctx, history[:prefix_len])
                if current_search_state is not None and offset < len(suffix_history):
                    current_state_result = cli.get_search_state(
                        timeout_s=self._engine_call_timeout_s()
                    )
                    self._record_cli_transport(cli, "get_state")
                    get_state_ms += cli.last_call_ms
                    self._record_headless_timing(
                        current_state_result, "headless_build_state_ms", "headless_get_state_total_ms"
                    )
                    current_search_state = self._extract_search_state(current_state_result)
            if last_action_result is not None and last_action_result.get("type") == "error":
                # The terminating action came back type='error' (churn-induced
                # "Current state is not combat_play" / "Not in combat" on a warm
                # worker), NOT a real combat-end decision. Building a terminal
                # from it stamps terminal_decision='error' with an empty enemy
                # list, which evaluate_leaf would read as "all enemies dead" and
                # score as a 1800-3600 phantom victory (seed42 THE_KIN s14+).
                # Treat it as a worker failure: discard the suspect process and
                # surface a failed state, mirroring the get_search_state error
                # path below. Checked BEFORE the final _capture_snapshot so a
                # corrupt terminal frame is never stored as a reusable snapshot.
                if self.reuse_cli_processes:
                    self._discard_worker_ctx(worker_ctx, reason='worker_action_error')
                return self._build_failed_search_state(
                    f"worker action error: {last_action_result.get('message')}"
                )
            captured_fingerprint = None
            if worker_ctx is not None:
                captured_fingerprint = self._capture_snapshot(worker_ctx, history)
            if last_action_result is not None and last_action_result.get("decision") != "combat_play":
                return self._build_terminal_search_state(
                    last_action_result, last_action=suffix_history[-1] if suffix_history else None
                )
            result = cli.get_search_state(timeout_s=self._engine_call_timeout_s())
            self._record_cli_transport(cli, "get_state")
            get_state_ms = cli.last_call_ms
            self._record_headless_timing(
                result, "headless_build_state_ms", "headless_get_state_total_ms"
            )
            # A warm pooled worker can silently drop out of combat after an
            # import/restore (observed: get_search_state -> {"type":"error",
            # "message":"Not in combat"} on a still-live fight). Returning that
            # error verbatim makes the searcher see an empty combat with zero
            # actions and emit sequence=[] — a FAKE search_empty that poisons
            # rollout labels and, in production, drops the turn to the no-op
            # fallback. Treat it as a worker failure instead: discard the
            # suspect process (so it never serves another step) and surface an
            # explicit failed state, which is_failed_root_search already catches
            # and the raw-retry path can recover on a fresh worker.
            if isinstance(result, dict) and result.get("type") == "error":
                if self.reuse_cli_processes:
                    self._discard_worker_ctx(
                        worker_ctx, reason='worker_get_search_state_error'
                    )
                return self._build_failed_search_state(
                    f"worker get_search_state error: {result.get('message')}"
                )
            if isinstance(result, dict):
                state = result.get("combat_state_for_search")
                if isinstance(state, dict):
                    history_key = self._history_key(history)
                    snapshot_id = (
                        worker_ctx.snapshot_ids_by_history.get(history_key)
                        if worker_ctx is not None else None
                    )
                    if snapshot_id:
                        state["engine_snapshot_id"] = snapshot_id
                    if captured_fingerprint:
                        state["engine_state_fingerprint"] = captured_fingerprint
                        state["engine_state_fingerprint_schema"] = (
                            "sts2-combat-snapshot-v1"
                        )
                    semantic_fingerprint = (
                        worker_ctx.snapshot_semantic_fingerprints_by_history.get(
                            history_key
                        )
                        if worker_ctx is not None else None
                    )
                    if semantic_fingerprint:
                        state["engine_semantic_state_fingerprint"] = semantic_fingerprint
                        state["engine_semantic_state_fingerprint_schema"] = (
                            "sts2-combat-semantic-v1"
                        )
            return result
        except Exception as exc:
            self._record_engine_rpc_failure("replay", exc)
            if self.reuse_cli_processes:
                self._discard_worker_ctx(
                    worker_ctx, reason=f'replay_exception:{type(exc).__name__}'
                )
            return self._build_failed_search_state(str(exc))
        finally:
            if should_stop and cli is not None:
                cli.stop()
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self.timing["replay_calls"] += 1
            self.timing["replay_total_ms"] += elapsed_ms
            self.timing["replay_ms_samples"].append(elapsed_ms)
            self.timing["replay_breakdown_samples"].append(
                {
                    "combat_history_len": len(history),
                    "start_ms": start_ms,
                    "start_test_combat_ms": start_combat_ms,
                    "restore_snapshot_ms": restore_snapshot_ms,
                    "replay_actions_ms": replay_actions_ms,
                    "get_search_state_ms": get_state_ms,
                    "total_ms": elapsed_ms,
                }
            )

    def _build_terminal_search_state(
        self, result: Dict[str, Any], last_action: Optional[RecordedAction] = None
    ) -> Dict[str, Any]:
        player = result.get("player") or {}
        # Preserve any enemies the engine still reports at this non-combat_play
        # decision. A genuine combat-end (card_reward/victory/...) has no living
        # enemies; if enemies survive here, the "terminal" decision is spurious
        # (e.g. a corrupt restore that dropped the enemy list) and the leaf
        # evaluator must NOT award the +1e6 victory bonus. Carry their summed HP
        # so the evaluator can sanity-check the victory claim.
        raw_enemies = result.get("enemies") or []
        surviving_hp = 0.0
        for enemy in raw_enemies:
            if not isinstance(enemy, dict):
                continue
            hp = enemy.get("hp")
            try:
                hp_val = float(hp) if hp is not None else 0.0
            except (TypeError, ValueError):
                hp_val = 0.0
            if hp_val > 0:
                surviving_hp += hp_val
        # Retain terminal action provenance; end-turn effects can also win.
        root_combat = self._root_summary or {}
        root_enemy_hp = float(
            sum(int(e.get("hp") or 0) for e in (root_combat.get("enemies") or []))
        )
        terminated_on_end_turn = bool(
            last_action is not None and str(getattr(last_action, "action", "")) == "end_turn"
        )
        return {
            "type": "terminal_search_state",
            "success": True,
            "terminal_decision": result.get("decision") or result.get("type"),
            "terminal_type": result.get("type"),
            "terminal_result": result,
            "terminal_surviving_enemy_hp": surviving_hp,
            "terminal_root_enemy_hp": root_enemy_hp,
            "terminal_on_end_turn": terminated_on_end_turn,
            "combat": {
                "round_number": None,
                "turn_number": None,
                "is_player_turn": False,
                "player": {
                    "hp": player.get("hp"),
                    "max_hp": player.get("max_hp"),
                    "block": player.get("block"),
                    "energy": None,
                    "powers": [],
                    "relics": player.get("relics") or [],
                },
                "enemies": [],
                "hand": [],
                "draw_pile": [],
                "discard_pile": [],
                "exhaust_pile": [],
                "play_pile": [],
                "available_actions": [],
            },
        }

    @staticmethod
    def _build_failed_search_state(error: str) -> Dict[str, Any]:
        return {
            "type": "terminal_search_state",
            "success": False,
            "terminal_decision": "unknown",
            "terminal_type": "error",
            "error": error,
            "combat": {},
        }

    def _find_best_snapshot(
        self,
        worker_ctx: _CliWorkerContext,
        history: Sequence[RecordedAction],
    ) -> tuple[Tuple[Tuple[str, Tuple[Tuple[str, Any], ...]], ...], Optional[str]]:
        history_key = self._history_key(history)
        for prefix_len in range(len(history_key), -1, -1):
            prefix = history_key[:prefix_len]
            snapshot_id = worker_ctx.snapshot_ids_by_history.get(prefix)
            if snapshot_id is not None:
                return prefix, snapshot_id
        return tuple(), None

    def _capture_snapshot(
        self, worker_ctx: _CliWorkerContext, history: Sequence[RecordedAction]
    ) -> Optional[str]:
        if not self._is_safe_snapshot_checkpoint(history):
            return None
        history_key = self._history_key(history)
        if history_key in worker_ctx.snapshot_ids_by_history:
            return worker_ctx.snapshot_fingerprints_by_history.get(history_key)
        snapshot_id = self._snapshot_id_for_history(history_key)
        fingerprint_mode = str(
            os.environ.get("STS2_SEARCH_SNAPSHOT_FINGERPRINT_MODE") or "none"
        ).strip().lower()
        if fingerprint_mode not in {"none", "strict", "all"}:
            raise ValueError(
                "STS2_SEARCH_SNAPSHOT_FINGERPRINT_MODE must be none, strict, or all"
            )
        result = worker_ctx.cli.capture_combat_snapshot(
            snapshot_id,
            fingerprint_mode=fingerprint_mode,
            timeout_s=self._engine_call_timeout_s(),
        )
        self._record_cli_transport(worker_ctx.cli, "capture")
        self.timing["snapshot_capture_total_ms"] += float(worker_ctx.cli.last_call_ms or 0.0)
        self._record_headless_timing(
            result, "headless_capture_ms", "headless_capture_total_ms"
        )
        if result.get("type") == "combat_snapshot_captured" and result.get("success"):
            worker_ctx.snapshot_ids_by_history[history_key] = snapshot_id
            fingerprint = str(result.get("state_fingerprint") or "")
            if fingerprint:
                worker_ctx.snapshot_fingerprints_by_history[history_key] = fingerprint
            semantic_fingerprint = str(result.get("semantic_state_fingerprint") or "")
            if semantic_fingerprint:
                worker_ctx.snapshot_semantic_fingerprints_by_history[history_key] = (
                    semantic_fingerprint
                )
            self.timing["snapshot_capture_count"] += 1
            return fingerprint or None
        return None

    def _ensure_snapshot_fingerprints(
        self,
        worker_ctx: _CliWorkerContext,
        history: Sequence[RecordedAction],
        *,
        include_semantic: bool = False,
        search_state: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        history_key = self._history_key(history)
        strict = worker_ctx.snapshot_fingerprints_by_history.get(history_key, "")
        semantic = worker_ctx.snapshot_semantic_fingerprints_by_history.get(
            history_key, ""
        )
        if strict and (semantic or not include_semantic):
            return strict, semantic
        snapshot_id = worker_ctx.snapshot_ids_by_history.get(history_key)
        if not snapshot_id:
            return strict, semantic

        started = time.perf_counter()
        try:
            result = worker_ctx.cli.fingerprint_combat_snapshot(
                snapshot_id,
                fingerprint_mode="all" if include_semantic else "strict",
                timeout_s=self._engine_call_timeout_s(),
            )
        except Exception as exc:
            self._record_engine_rpc_failure("fingerprint", exc)
            self._discard_worker_ctx(
                worker_ctx, reason=f"fingerprint_exception:{type(exc).__name__}"
            )
            # No strict identity means no safe deduplication.
            return "", ""
        self._record_cli_transport(worker_ctx.cli, "fingerprint")
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.timing["snapshot_fingerprint_count"] += 1
        self.timing["snapshot_fingerprint_total_ms"] += elapsed_ms
        self._record_headless_timing(
            result, "headless_fingerprint_ms", "headless_fingerprint_total_ms"
        )
        if result.get("success") is not True:
            return strict, semantic

        strict = str(result.get("state_fingerprint") or strict)
        semantic = str(result.get("semantic_state_fingerprint") or semantic)
        if strict:
            worker_ctx.snapshot_fingerprints_by_history[history_key] = strict
        if semantic:
            worker_ctx.snapshot_semantic_fingerprints_by_history[history_key] = semantic
        if isinstance(search_state, dict):
            search_state["engine_snapshot_id"] = snapshot_id
            if strict:
                search_state["engine_state_fingerprint"] = strict
                search_state["engine_state_fingerprint_schema"] = (
                    "sts2-combat-snapshot-v1"
                )
            if semantic:
                search_state["engine_semantic_state_fingerprint"] = semantic
                search_state["engine_semantic_state_fingerprint_schema"] = (
                    "sts2-combat-semantic-v1"
                )
        return strict, semantic

    @staticmethod
    def _is_turn_root_checkpoint(history: Sequence[RecordedAction]) -> bool:
        if not history:
            return True
        return str(history[-1].action) == "end_turn"

    def _is_safe_snapshot_checkpoint(self, history: Sequence[RecordedAction]) -> bool:
        if self._is_turn_root_checkpoint(history):
            return True
        # Imported mid-turn checkpoints used to drift because the snapshot
        # captured only Seed/Counter while STS2's MegaRandom advances its hidden
        # _s0.._s3 state. Current snapshots persist that complete state and have
        # been checked against clean full restores across card and enemy-turn
        # transitions. Archived snapshots without those fields stay on the
        # conservative root/turn-root path.
        if self.root_snapshot_json is None:
            return True
        return bool(self._root_snapshot_rng_state_complete)

    @staticmethod
    def _history_key(history: Sequence[RecordedAction]) -> Tuple[Tuple[str, Tuple[Tuple[str, Any], ...]], ...]:
        def freeze_value(value: Any) -> Any:
            if isinstance(value, dict):
                return tuple(sorted((str(k), freeze_value(v)) for k, v in value.items()))
            if isinstance(value, list):
                return tuple(freeze_value(v) for v in value)
            return value

        return tuple(
            (step.action, tuple(sorted((str(k), freeze_value(v)) for k, v in step.args.items())))
            for step in history
        )

    def _snapshot_id_for_history(self, history_key: Tuple[Tuple[str, Tuple[Tuple[str, Any], ...]], ...]) -> str:
        raw = repr(history_key).encode("utf-8")
        return f"hist_{hashlib.sha1(raw).hexdigest()[:16]}"

    @staticmethod
    def _extract_search_state(result: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(result, dict) and result.get("type") == "search_state_result":
            return result.get("combat_state_for_search") or {}
        return result or {}

    def search(self, depth: int, chance_depth: int = 1) -> SearchResult:
        self._scoring_history_offset = 0
        self._reset_search_deadline()
        history: List[RecordedAction] = []
        state_result = self.combat_to_state(history)
        search_state = self._extract_search_state(state_result)
        # Root-failure recovery (the missing "raw-retry path" the worker-error
        # comment promised). A pooled worker can hand back a success=False root
        # state whose terminal_type is NOT "error" (so combat_to_state's own
        # retry does not fire) — observed as a mid-combat EMPTY search on a still
        # live fight (THE_KIN, worker-pool + parallel). That empty poisons rollout
        # labels and, in production, drops the turn to the no-op fallback. If the
        # root came back not-successful while we still expect a live combat,
        # rebuild it ONCE on a fresh cold process (the verified-correct path) and
        # discard the suspect pooled worker. A genuine terminal/victory root is
        # success=True and never lands here, so this only recovers true failures.
        if (
            self.reuse_cli_processes
            and isinstance(search_state, dict)
            and search_state.get("success") is False
        ):
            self.timing["root_failure_cold_retries"] = (
                self.timing.get("root_failure_cold_retries", 0) + 1
            )
            state_result = self.combat_to_state(history, prefer_cold=True)
            search_state = self._extract_search_state(state_result)
        if (self.authoritative_root_state is not None and self.root_snapshot_json is not None
                and 'hand' in (search_state.get('combat') or {})):
            _require_matching_root_cards(self.authoritative_root_state, search_state)
        self._root_summary = self._combat_summary(search_state)
        self._preference_root = copy.deepcopy(search_state)
        self.eval_cache.clear()
        self.subtree_cache.clear()
        action_budget = depth
        pre_chance_budget = self._initial_pre_chance_budget(depth, chance_depth)
        if self.search_mode == "beam":
            return self._search_beam(search_state, history, action_budget, chance_depth)
        if self.parallel_top_level and self.max_workers > 1:
            return self._search_top_level_parallel(search_state, history, action_budget, chance_depth, pre_chance_budget)
        return self._search_from_state(search_state, history, action_budget, chance_depth, pre_chance_budget, is_root=True)

    def search_from_history(self, history: Sequence[RecordedAction], depth: int, chance_depth: int = 1) -> SearchResult:
        self._scoring_history_offset = len(history)
        self._reset_search_deadline()
        self.chance_depth = chance_depth
        state_result = self.combat_to_state(history)
        search_state = self._extract_search_state(state_result)
        if (not history and self.authoritative_root_state is not None
                and self.root_snapshot_json is not None
                and 'hand' in (search_state.get('combat') or {})):
            _require_matching_root_cards(self.authoritative_root_state, search_state)
        self._root_summary = self._combat_summary(search_state)
        self._preference_root = copy.deepcopy(search_state)
        self.eval_cache.clear()
        self.subtree_cache.clear()
        action_budget = depth
        pre_chance_budget = self._initial_pre_chance_budget(depth, chance_depth)
        if self.search_mode == "beam":
            return self._search_beam(search_state, history, action_budget, chance_depth)
        if self.parallel_top_level and self.max_workers > 1:
            return self._search_top_level_parallel(search_state, history, action_budget, chance_depth, pre_chance_budget)
        return self._search_from_state(search_state, history, action_budget, chance_depth, pre_chance_budget, is_root=True)

    @staticmethod
    def _initial_pre_chance_budget(depth: int, chance_depth: int) -> int:
        # When chance search is enabled, `depth` is the caller-provided safe
        # bound for how far to expand before the first explicit enemy-turn
        # chance node. Do not silently replace it with a much larger same-turn
        # budget, or the tree can spill far beyond the intended combat horizon
        # and turn a "first chance only" search into an effectively unbounded
        # same-turn rollout.
        if chance_depth <= 0:
            return 0
        return max(1, depth)

    def _search_top_level_parallel(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        action_budget: int,
        chance_depth: int,
        pre_chance_budget: int,
    ) -> SearchResult:
        if not search_state.get('success'):
            return SearchResult(score=float('-inf'), sequence=[], leaf_state=search_state, stats={'nodes': 1})

        all_actions = available_actions_from_search_state(search_state)
        root_prune_before = self._prune_counts()
        root_prepare_started = time.perf_counter()
        # Prepare root successors once on the coordinator. The previous parallel
        # prepass performed a full engine replay for every candidate and then the
        # branch workers replayed the same root again; that increased latency and
        # consumed worker slots before any tree expansion happened.
        legal_actions = self._prepare_action_candidates(search_state, history, all_actions)
        root_prepare_ms = (time.perf_counter() - root_prepare_started) * 1000.0
        root_prepare_replay_ms = float(self.timing.get("replay_total_ms") or 0.0)
        if self._time_budget_exhausted():
            return self._budget_leaf_result(
                search_state, history, all_actions, legal_actions, parallel_top_level=True
            )
        root_pruned = self._prune_delta(root_prune_before, self._prune_counts())
        action_budget, pre_chance_budget, horizon_policy = self._coverage_first_horizon(
            action_budget, pre_chance_budget, len(legal_actions)
        )
        self.timing['horizon_policy'] = horizon_policy
        horizon_empty = (
            chance_depth <= 0 and action_budget <= 0
        ) or (
            chance_depth > 0 and pre_chance_budget <= 0
        )
        if not legal_actions or horizon_empty:
            if horizon_empty and legal_actions:
                self._audit_increment('horizon_boundary_leaves')
            result = self._settled_leaf_result(search_state, history)
            result.stats.update({
                    'nodes': 1,
                    'available_actions': len(all_actions),
                    'considered_actions': len(legal_actions),
                    'parallel_top_level': True,
                })
            return result

        # Candidate preparation uses one engine process on the coordinator
        # thread. It has already materialized every child state, so retaining
        # that process while the executor runs only steals a slot from actual
        # branch work. Release it before starting root or continuation workers.
        coordinator_release_ms = 0.0
        if self.reuse_cli_processes and self.root_snapshot_json is not None:
            release_started = time.perf_counter()
            self._reset_current_thread_worker()
            coordinator_release_ms = (time.perf_counter() - release_started) * 1000.0

        workers = max(1, min(self.max_workers, len(legal_actions)))
        frontier_workers = 1
        # A root branch is evaluated once. max_search_ms is a per-root soft
        # ceiling while roots run concurrently; it is no longer divided among
        # roots and no second fairness/refinement pass is scheduled.
        branch_budget_ms = float(self.max_search_ms or 0.0)
        self.timing['root_fair_budget_ms'] = 0.0
        self.timing['root_branch_budget_ms'] = branch_budget_ms
        stage_started = time.perf_counter()
        completed: List[Tuple[int, Dict[str, Any], SearchResult]] = []
        branch_audits: Dict[int, Dict[str, Any]] = {}
        timed_out = 0
        started_jobs = len(legal_actions)
        # Queue every candidate. The executor still limits active engine
        # processes, but the coordinator no longer drops late root edges when
        # the decision-wide wall clock expires. Any incomplete subtree is then
        # visible in the bounded-tree coverage counters.
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_rows = {
                executor.submit(
                    self._evaluate_isolated_root_branch,
                    action_info,
                    search_state,
                    history,
                    action_budget,
                    chance_depth,
                    pre_chance_budget,
                    branch_budget_ms,
                    frontier_workers,
                    time.perf_counter(),
                ): (index, action_info)
                for index, action_info in enumerate(legal_actions)
            }
            for future in as_completed(future_rows):
                index, action_info = future_rows[future]
                child, branch_meta = future.result()
                completed.append((index, action_info, child))
                audit = branch_meta['audit']
                branch_audits[index] = audit
                self.timing['root_branch_budgets'].append(audit)
                self._merge_branch_timing(branch_meta['timing'], branch_meta.get('coverage'))
                self._audit_increment('root_branches_evaluated')
                if audit['budget_exhausted']:
                    timed_out += 1

        cancelled_jobs = 0
        completed.sort(key=lambda row: row[0])
        children = [(action_info, child) for _, action_info, child in completed]
        stage_wall_ms = (time.perf_counter() - stage_started) * 1000.0
        stage_b_wall_ms = 0.0
        stage_b_jobs = 0
        if timed_out:
            self.timing['time_budget_exhausted'] = 1
        self.timing['parallel_audit'] = {
            'configured_workers': int(self.max_workers),
            'active_workers': workers,
            'frontier_workers_per_root': frontier_workers,
            'frontier_budget_ms': round(float(self.timing.get('frontier_budget_ms') or 0.0), 3),
            'root_prepare_workers': int(self.timing.get('root_prepare_workers') or 0),
            'root_prepare_jobs_queued': int(self.timing.get('root_prepare_jobs_queued') or 0),
            'root_prepare_jobs_completed': int(self.timing.get('root_prepare_jobs_completed') or 0),
            'root_prepare_wall_ms': round(root_prepare_ms, 3),
            'root_prepare_replay_ms': round(root_prepare_replay_ms, 3),
            'coordinator_release_ms': round(coordinator_release_ms, 3),
            'frontier_jobs_queued': int(self.timing.get('frontier_jobs_queued') or 0),
            'frontier_jobs_started': int(self.timing.get('frontier_jobs_started') or 0),
            'frontier_jobs_completed': int(self.timing.get('frontier_jobs_completed') or 0),
            'frontier_jobs_timed_out': int(self.timing.get('frontier_jobs_timed_out') or 0),
            'frontier_jobs_cancelled': int(self.timing.get('frontier_jobs_cancelled') or 0),
            'root_jobs_queued': len(legal_actions),
            'root_jobs_started': started_jobs,
            'root_jobs_completed': len(children),
            'root_jobs_timed_out': timed_out,
            'root_jobs_cancelled': cancelled_jobs,
            'per_root_budget_ms': round(branch_budget_ms, 3),
            'root_budget_policy': 'one_pass_per_root',
            'stage_a_wall_ms': round(stage_wall_ms, 3),
            'stage_b_jobs': stage_b_jobs,
            'stage_b_wall_ms': round(stage_b_wall_ms, 3),
        }

        top_base_score = max((float(child.score) for _, child in children), default=float('-inf'))
        adjusted_pairs: List[Tuple[Dict[str, Any], SearchResult]] = []
        for action_info, child in children:
            adjustments = self._root_adjustments(search_state, action_info, child, top_base_score)
            action_info.update(adjustments)
            action_info['base_score'] = float(child.score)
            action_info['adjusted_score'] = float(child.score) + float(action_info.get('root_adjustment', 0.0))
            adjusted_pairs.append((action_info, child))
        best_pair, selection_audit = self._select_root_candidate(adjusted_pairs)
        best = self._with_root_adjusted_score(best_pair[1], best_pair[0]) if best_pair is not None else None
        if best is None:
            return SearchResult(score=float('-inf'), sequence=[],
                                leaf_state=self._build_failed_search_state('No comparable root result'),
                                stats={'nodes': 1, **selection_audit})

        nodes = 1 + sum(int(child.stats.get('nodes', 1)) for _, child in children)
        best.stats = {
            **best.stats,
            'nodes': nodes,
            'available_actions': len(all_actions),
            'considered_actions': len(legal_actions),
            'symmetry_pruned_actions': self.timing['symmetry_pruned_actions'],
            'parallel_top_level': True,
            'max_workers': workers,
            'time_budget_exhausted': bool(self.timing.get('time_budget_exhausted') or 0),
            **selection_audit,
        }
        best.stats['root_coverage'] = self._root_coverage(
            len(all_actions), len(legal_actions), len(children), root_pruned
        )
        best.stats['score_explanation'] = self._explain_result(best, history)
        if self.capture_root_topk != 0:
            best.root_candidates = self._build_root_candidates(adjusted_pairs)
        return best

    def _evaluate_isolated_frontier_child(
        self,
        action_info: Dict[str, Any],
        parent_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        action_budget: int,
        chance_depth: int,
        pre_chance_budget: int,
        deadline: Optional[float],
        budget_ms: float,
    ) -> Tuple[SearchResult, Dict[str, Any]]:
        """Evaluate one continuation edge in a fresh, non-nested searcher.

        Each child gets its own CLI/searcher so engine state is never shared
        across threads. The caller supplies a bounded deadline slice derived from
        the parent root branch budget, allowing sibling edges to run together
        without letting one child consume the whole branch.
        """
        child_searcher = self._new_branch_searcher(
            budget_ms,
            max_workers=1,
            parallel_frontier=False,
        )
        child_searcher._root_summary = self._root_summary
        child_searcher._preference_root = self._preference_root
        child_searcher._scoring_history_offset = self._scoring_history_offset
        child_searcher.chance_depth = chance_depth
        child_searcher._search_deadline = deadline
        started = time.perf_counter()
        try:
            result = child_searcher._evaluate_child_action(
                action_info['action'],
                parent_state,
                history,
                action_budget,
                chance_depth,
                pre_chance_budget,
                action_info['next_history'],
                action_info['child_state'],
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return result, {
                'elapsed_ms': elapsed_ms,
                'budget_exhausted': bool(
                    result.stats.get('time_budget_exhausted')
                    or child_searcher.timing.get('time_budget_exhausted')
                ),
                'timing': dict(child_searcher.timing),
                'coverage': child_searcher._coverage_snapshot(),
            }
        finally:
            child_searcher.close()

    def _search_frontier_parallel(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        action_budget: int,
        chance_depth: int,
        pre_chance_budget: int,
        all_actions: Sequence[SearchAction],
        legal_actions: Sequence[Dict[str, Any]],
    ) -> Optional[SearchResult]:
        """Search one non-root frontier in parallel and merge it like serial search.

        This is deliberately one level deep. The top-level search already fans
        out root actions; each root branch may get a bounded number of continuation
        workers, and those workers recurse serially. No depth or candidate is
        removed by this method. Each frontier task receives a fair slice of its
        root branch budget, and edges left unscheduled are explicitly counted.
        """
        if not self.parallel_frontier or self.max_workers <= 1 or len(legal_actions) <= 1:
            return None

        workers = min(self.max_workers, len(legal_actions))
        self.timing['frontier_workers'] = max(int(self.timing.get('frontier_workers') or 0), workers)
        parent_deadline = self._search_deadline
        frontier_budget_ms = (
            self.max_search_ms / float(len(legal_actions))
            if self.max_search_ms > 0.0 else 0.0
        )
        self.timing['frontier_budget_ms'] = max(
            float(self.timing.get('frontier_budget_ms') or 0.0), frontier_budget_ms
        )
        pending: Dict[Any, int] = {}
        completed: List[Tuple[int, Dict[str, Any], SearchResult, Dict[str, Any]]] = []
        next_index = 0
        started_jobs = 0
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            while next_index < len(legal_actions) and len(pending) < workers:
                action_info = legal_actions[next_index]
                future = executor.submit(
                    self._evaluate_isolated_frontier_child,
                    action_info,
                    search_state,
                    history,
                    action_budget,
                    chance_depth,
                    pre_chance_budget,
                    (
                        min(parent_deadline, time.perf_counter() + frontier_budget_ms / 1000.0)
                        if parent_deadline is not None and frontier_budget_ms > 0.0
                        else parent_deadline
                    ),
                    frontier_budget_ms,
                )
                pending[future] = next_index
                next_index += 1
                started_jobs += 1
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    index = pending.pop(future)
                    result, meta = future.result()
                    completed.append((index, legal_actions[index], result, meta))
                    self._merge_branch_timing(meta.get('timing'), meta.get('coverage'))
                    self._audit_increment('frontier_jobs_completed')
                    if meta.get('budget_exhausted'):
                        self._audit_increment('frontier_jobs_timed_out')
                if self._time_budget_exhausted():
                    break
                while next_index < len(legal_actions) and len(pending) < workers:
                    action_info = legal_actions[next_index]
                    future = executor.submit(
                        self._evaluate_isolated_frontier_child,
                        action_info,
                        search_state,
                        history,
                        action_budget,
                        chance_depth,
                        pre_chance_budget,
                        (
                            min(parent_deadline, time.perf_counter() + frontier_budget_ms / 1000.0)
                            if parent_deadline is not None and frontier_budget_ms > 0.0
                            else parent_deadline
                        ),
                        frontier_budget_ms,
                    )
                    pending[future] = next_index
                    next_index += 1
                    started_jobs += 1
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        self._audit_increment('frontier_jobs_queued', len(legal_actions))
        self._audit_increment('frontier_jobs_started', started_jobs)
        cancelled_jobs = max(0, len(legal_actions) - started_jobs)
        if cancelled_jobs:
            self._audit_increment('frontier_jobs_cancelled', cancelled_jobs)
            self._audit_increment('known_unexpanded_action_edges', cancelled_jobs)
            self.timing['time_budget_exhausted'] = 1

        completed.sort(key=lambda row: row[0])
        best: Optional[SearchResult] = None
        best_effect_score = float('-inf')
        nodes = 1
        for _index, action_info, child, _meta in completed:
            nodes += int(child.stats.get('nodes', 1))
            effect_score = float(action_info.get('effect_score') or 0.0)
            if (
                self._is_comparable_result(child)
                and (best is None
                     or child.score > best.score
                     or (child.score == best.score and effect_score > best_effect_score))
            ):
                best = child
                best_effect_score = effect_score

        if best is None:
            if self.timing.get('time_budget_exhausted'):
                return self._budget_leaf_result(
                    search_state, history, all_actions, legal_actions
                )
            return SearchResult(
                score=float('-inf'),
                sequence=[],
                leaf_state=self._build_failed_search_state('No comparable frontier result'),
                stats={'nodes': nodes},
            )

        best.stats = {
            **best.stats,
            'nodes': nodes,
            'available_actions': len(all_actions),
            'considered_actions': len(legal_actions),
            'symmetry_pruned_actions': self.timing['symmetry_pruned_actions'],
            'time_budget_exhausted': bool(self.timing.get('time_budget_exhausted') or 0),
            'parallel_frontier': True,
            'frontier_workers': workers,
            'frontier_budget_ms': round(frontier_budget_ms, 3),
            'frontier_jobs_completed': len(completed),
            'frontier_jobs_cancelled': cancelled_jobs,
            # Parallel frontier may stop on a shared deadline. Until it carries
            # a complete per-edge pile-order proof, do not publish it into the
            # semantic DAG.
            'semantic_reuse_safe': False,
        }
        return best

    @staticmethod
    def _engine_state_fingerprint(search_state: Dict[str, Any]) -> str:
        fingerprint = str(search_state.get("engine_state_fingerprint") or "")
        return fingerprint if fingerprint.startswith("sts2-combat-snapshot-v1:") else ""

    def _exact_state_identity(self, search_state: Dict[str, Any]) -> str:
        fingerprint = self._engine_state_fingerprint(search_state)
        if fingerprint:
            return fingerprint
        snapshot_id = str(search_state.get("engine_snapshot_id") or "")
        if snapshot_id:
            # A process-local snapshot handle is unique but does not assert that
            # two visible states are equivalent. It is therefore a safe fallback
            # while strict fingerprints remain lazy.
            return f"process-snapshot:{snapshot_id}"
        return hash_search_state(search_state)

    @staticmethod
    def _engine_semantic_state_fingerprint(search_state: Dict[str, Any]) -> str:
        fingerprint = str(search_state.get("engine_semantic_state_fingerprint") or "")
        return fingerprint if fingerprint.startswith("sts2-combat-semantic-v1:") else ""

    def _subtree_cache_key(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        action_budget: int,
        chance_depth: int,
        pre_chance_budget: int,
    ) -> Tuple[Any, ...]:
        fingerprint = self._engine_state_fingerprint(search_state)
        if fingerprint:
            state_hash = fingerprint
        else:
            combat = search_state.get("combat") or {}
            enemies = combat.get("enemies") or []
            state_hash = (
                hash_search_state_for_subtree_cache(search_state)
                if len(enemies) >= 2
                else hash_search_state(search_state)
            )
        key: Tuple[Any, ...] = (
            state_hash,
            self._history_key(history),
            action_budget,
            chance_depth,
            pre_chance_budget,
        )
        if self.preference_scorer:
            key += (sum(row.action == "use_potion" for row in history),)
        return key

    def _strict_dag_path_context(
        self, history: Sequence[RecordedAction]
    ) -> Tuple[Any, ...]:
        if self.preference_scorer is None:
            return ("state_only_score",)
        relative_history = history[self._scoring_history_offset:]
        return (
            "preference_score",
            sum(row.action == "use_potion" for row in relative_history),
        )

    def _strict_dag_key(
        self,
        fingerprint: str,
        history: Sequence[RecordedAction],
        action_budget: int,
        chance_depth: int,
        pre_chance_budget: int,
    ) -> Tuple[Any, ...]:
        return (
            "strict-subtree-v2",
            fingerprint,
            action_budget,
            chance_depth,
            pre_chance_budget,
            self._strict_dag_path_context(history),
        )

    def _strict_dag_probe_key(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        action_budget: int,
        chance_depth: int,
        pre_chance_budget: int,
    ) -> Tuple[Any, ...]:
        return (
            "strict-probe-v1",
            hash_search_state(search_state),
            action_budget,
            chance_depth,
            pre_chance_budget,
            self._strict_dag_path_context(history),
        )

    def _strict_dag_result_reusable(self, result: SearchResult) -> bool:
        return (
            self._is_comparable_result(result)
            and not bool(result.stats.get("time_budget_exhausted"))
        )

    def _probe_strict_dag_candidate(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        action_budget: int,
        chance_depth: int,
        pre_chance_budget: int,
    ) -> Tuple[str, Optional[_StrictDagCandidate]]:
        fingerprint = self._engine_state_fingerprint(search_state)
        if not getattr(self, "strict_dag_enabled", False) or fingerprint:
            return fingerprint, None

        probe_key = self._strict_dag_probe_key(
            search_state, history, action_budget, chance_depth, pre_chance_budget
        )
        with self._cli_lock:
            worker_ctx = self._cli_by_thread.get(threading.get_ident())
        candidate = _StrictDagCandidate(
            list(history), search_state, worker_ctx=worker_ctx
        )
        with self._strict_dag_index_lock:
            prior_candidates = list(self._strict_dag_candidates.get(probe_key) or [])
            self._strict_dag_candidates.setdefault(probe_key, []).append(candidate)
        if not prior_candidates:
            return "", candidate

        self._audit_increment("strict_dag_probe_collisions")
        if worker_ctx is None:
            self._audit_increment("strict_dag_fingerprint_unavailable")
            return "", candidate

        fingerprint, _ = self._ensure_snapshot_fingerprints(
            worker_ctx, history, search_state=search_state
        )
        candidate.strict_fingerprint = fingerprint
        if not fingerprint:
            self._audit_increment("strict_dag_fingerprint_unavailable")
            return "", candidate

        mismatch_count = 0
        for prior in prior_candidates:
            prior_fingerprint = prior.strict_fingerprint
            if not prior_fingerprint and prior.worker_ctx is worker_ctx:
                prior_fingerprint, _ = self._ensure_snapshot_fingerprints(
                    worker_ctx,
                    prior.history,
                    search_state=prior.search_state,
                )
                prior.strict_fingerprint = prior_fingerprint
            if not prior_fingerprint:
                continue
            if prior_fingerprint != fingerprint:
                mismatch_count += 1
                continue
            self._audit_increment("strict_dag_exact_matches")
            if prior.result is not None and self._strict_dag_result_reusable(prior.result):
                key = self._strict_dag_key(
                    fingerprint,
                    history,
                    action_budget,
                    chance_depth,
                    pre_chance_budget,
                )
                self._dag_cache.publish(key, prior.result)
            return fingerprint, candidate

        if mismatch_count:
            self._audit_increment("strict_dag_fingerprint_mismatches", mismatch_count)
        else:
            self._audit_increment("strict_dag_fingerprint_unavailable")
        return fingerprint, candidate

    def _complete_strict_dag_candidate(
        self,
        candidate: Optional[_StrictDagCandidate],
        result: SearchResult,
        history: Sequence[RecordedAction],
        action_budget: int,
        chance_depth: int,
        pre_chance_budget: int,
    ) -> None:
        if candidate is None:
            return
        candidate.result = result
        if not candidate.strict_fingerprint or not self._strict_dag_result_reusable(result):
            return
        key = self._strict_dag_key(
            candidate.strict_fingerprint,
            history,
            action_budget,
            chance_depth,
            pre_chance_budget,
        )
        self._dag_cache.publish(key, result)

    def _semantic_dag_key(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        action_budget: int,
        chance_depth: int,
        pre_chance_budget: int,
    ) -> Optional[Tuple[Any, ...]]:
        semantic_fingerprint = self._engine_semantic_state_fingerprint(search_state)
        if not semantic_fingerprint:
            return None
        key: Tuple[Any, ...] = (
            "semantic-subtree-v1",
            semantic_fingerprint,
            self._history_key(history),
            action_budget,
            chance_depth,
            pre_chance_budget,
        )
        if self.preference_scorer:
            key += (sum(row.action == "use_potion" for row in history),)
        return key

    @staticmethod
    def _pile_card_tokens(search_state: Dict[str, Any], pile: str) -> List[str]:
        combat = search_state.get("combat") or {}
        return [
            json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for card in (combat.get(pile) or [])
        ]

    @staticmethod
    def _is_ordered_subsequence(smaller: Sequence[str], larger: Sequence[str]) -> bool:
        if not smaller:
            return True
        cursor = 0
        for value in larger:
            if value == smaller[cursor]:
                cursor += 1
                if cursor == len(smaller):
                    return True
        return False

    @classmethod
    def _semantic_transition_preserves_pile_order(
        cls, parent_state: Dict[str, Any], child_state: Dict[str, Any]
    ) -> bool:
        """Prove that ignored pile ordering was not observed by one transition.

        The semantic key only erases ordering inside discard/exhaust/play piles.
        Reuse is allowed only when every pre-existing card in those piles remains
        an unchanged prefix and the draw pile only loses cards while preserving
        order. A shuffle, retrieval, upgrade, reorder, or draw-pile insertion
        fails this proof and keeps the branch on the strict fingerprint path.
        """
        if child_state.get("success") is not True:
            return False
        if child_state.get("terminal_decision"):
            return True
        if parent_state.get("terminal_decision"):
            return False
        parent_combat = parent_state.get("combat")
        child_combat = child_state.get("combat")
        if not isinstance(parent_combat, dict) or not isinstance(child_combat, dict):
            return False

        for pile in ("discard_pile", "exhaust_pile", "play_pile"):
            parent_cards = cls._pile_card_tokens(parent_state, pile)
            child_cards = cls._pile_card_tokens(child_state, pile)
            if len(child_cards) < len(parent_cards):
                return False
            if child_cards[:len(parent_cards)] != parent_cards:
                return False

        parent_draw = cls._pile_card_tokens(parent_state, "draw_pile")
        child_draw = cls._pile_card_tokens(child_state, "draw_pile")
        if len(child_draw) > len(parent_draw):
            return False
        return cls._is_ordered_subsequence(child_draw, parent_draw)

    @staticmethod
    def _semantic_result_is_reusable(result: SearchResult) -> bool:
        return bool(result.stats.get("semantic_reuse_safe"))

    def _replay_semantic_result(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        cached: SearchResult,
    ) -> Optional[SearchResult]:
        """Rebase a semantic cache hit onto the exact current engine history."""
        def reject(reason: str) -> None:
            with self._audit_lock:
                reasons = self.timing.setdefault("semantic_dag_reject_reasons", {})
                reasons[reason] = int(reasons.get(reason) or 0) + 1

        replay_history = list(history)
        current_state = search_state
        state_hashes: List[str] = []
        state_keys: List[Dict[str, Any]] = []
        try:
            for action in cached.sequence:
                replay_history.append(self._recorded_action_from_search_action(action))
                replayed = self._extract_search_state(self.combat_to_state(replay_history))
                if action.action_type == "end_turn":
                    replayed = self._validate_settled_transition(current_state, replayed)
                if replayed.get("success") is not True:
                    reject("replay_state_failed")
                    return None
                current_state = replayed
                state_hashes.append(hash_search_state_for_plan_reuse(current_state))
                state_keys.append(canonicalize_search_state_for_plan_reuse(current_state))
        except Exception:
            reject("replay_exception")
            return None

        current_leaf_identity = (
            self._engine_semantic_state_fingerprint(current_state)
            or hash_search_state_for_subtree_cache(current_state)
        )
        cached_leaf_identity = (
            self._engine_semantic_state_fingerprint(cached.leaf_state)
            or hash_search_state_for_subtree_cache(cached.leaf_state)
        )
        if current_leaf_identity != cached_leaf_identity:
            reject("leaf_semantic_mismatch")
            return None
        score = self._score_with_history(current_state, replay_history)
        if not math.isclose(score, cached.score, rel_tol=0.0, abs_tol=1e-9):
            reject("score_mismatch")
            return None
        self._audit_increment(
            "semantic_dag_nodes_avoided",
            max(0, int(cached.stats.get("nodes") or 1) - max(1, len(cached.sequence))),
        )
        return SearchResult(
            score=score,
            sequence=list(cached.sequence),
            leaf_state=current_state,
            state_hashes_after_actions=state_hashes,
            state_keys_after_actions=state_keys,
            stats={
                **cached.stats,
                "semantic_dag_cache_hit": True,
                "semantic_reuse_safe": True,
                "semantic_replay_actions": len(cached.sequence),
            },
        )

    def _search_from_state(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        action_budget: int,
        chance_depth: int,
        pre_chance_budget: int,
        is_root: bool = False,
    ) -> SearchResult:
        strict_dag_enabled = bool(getattr(self, "strict_dag_enabled", False))
        semantic_dag_enabled = bool(getattr(self, "semantic_dag_enabled", False))
        if is_root or not (strict_dag_enabled or semantic_dag_enabled):
            return self._search_from_state_impl(
                search_state,
                history,
                action_budget,
                chance_depth,
                pre_chance_budget,
                is_root=is_root,
            )
        dag_candidate: Optional[_StrictDagCandidate] = None
        fingerprint = self._engine_state_fingerprint(search_state)
        if not fingerprint and strict_dag_enabled:
            fingerprint, dag_candidate = self._probe_strict_dag_candidate(
                search_state,
                history,
                action_budget,
                chance_depth,
                pre_chance_budget,
            )
        if not fingerprint:
            result = self._search_from_state_impl(
                search_state, history, action_budget, chance_depth,
                pre_chance_budget,
            )
            self._complete_strict_dag_candidate(
                dag_candidate,
                result,
                history,
                action_budget,
                chance_depth,
                pre_chance_budget,
            )
            return result
        if not hasattr(self, "_dag_cache"):
            self._dag_cache = _SharedDagCache()
        key = (
            self._strict_dag_key(
                fingerprint, history, action_budget, chance_depth, pre_chance_budget
            )
            if strict_dag_enabled
            else self._subtree_cache_key(
                search_state, history, action_budget, chance_depth, pre_chance_budget
            )
        )
        status, payload = self._dag_cache.reserve(key)
        if status == "cached":
            self._audit_increment("dag_cache_hits")
            if strict_dag_enabled:
                self._audit_increment(
                    "strict_dag_nodes_avoided",
                    max(0, int(payload.stats.get("nodes") or 1) - 1),
                )
            self._complete_strict_dag_candidate(
                dag_candidate,
                payload,
                history,
                action_budget,
                chance_depth,
                pre_chance_budget,
            )
            return payload
        if status == "wait":
            self._audit_increment("dag_singleflight_waits")
            timeout = None
            if self._search_deadline is not None:
                timeout = max(0.0, self._search_deadline - time.perf_counter())
            payload.wait(timeout=timeout)
            cached = self._dag_cache.completed(key)
            if cached is not None:
                self._audit_increment("dag_cache_hits")
                if strict_dag_enabled:
                    self._audit_increment(
                        "strict_dag_nodes_avoided",
                        max(0, int(cached.stats.get("nodes") or 1) - 1),
                    )
                self._complete_strict_dag_candidate(
                    dag_candidate,
                    cached,
                    history,
                    action_budget,
                    chance_depth,
                    pre_chance_budget,
                )
                return cached
            if self._time_budget_exhausted():
                return self._budget_leaf_result(search_state, history)
            result = self._search_from_state_impl(
                search_state, history, action_budget, chance_depth, pre_chance_budget
            )
            self._complete_strict_dag_candidate(
                dag_candidate,
                result,
                history,
                action_budget,
                chance_depth,
                pre_chance_budget,
            )
            return result
        if status == "recursive":
            result = self._search_from_state_impl(
                search_state, history, action_budget, chance_depth, pre_chance_budget
            )
            self._complete_strict_dag_candidate(
                dag_candidate,
                result,
                history,
                action_budget,
                chance_depth,
                pre_chance_budget,
            )
            return result

        semantic_key: Optional[Tuple[Any, ...]] = None
        semantic_owner = False
        if getattr(self, "semantic_dag_enabled", False):
            if not hasattr(self, "_semantic_dag_cache"):
                self._semantic_dag_cache = _SharedDagCache("semantic_dag_cache_hit")
            semantic_key = self._semantic_dag_key(
                search_state, history, action_budget, chance_depth, pre_chance_budget
            )
            semantic_status = "disabled"
            semantic_payload = None
            if semantic_key is not None:
                semantic_status, semantic_payload = self._semantic_dag_cache.reserve(semantic_key)
            if semantic_status == "wait":
                self._audit_increment("semantic_dag_singleflight_waits")
                timeout = None
                if self._search_deadline is not None:
                    timeout = max(0.0, self._search_deadline - time.perf_counter())
                semantic_payload.wait(timeout=timeout)
                semantic_payload = self._semantic_dag_cache.completed(semantic_key)
                semantic_status = "cached" if semantic_payload is not None else "miss"
            if semantic_status == "cached":
                replayed = self._replay_semantic_result(search_state, history, semantic_payload)
                if replayed is not None:
                    self._audit_increment("semantic_dag_hits")
                    self._dag_cache.publish(key, replayed)
                    return replayed
                self._semantic_dag_cache.invalidate(semantic_key)
                self._audit_increment("semantic_dag_rejects")
                self._audit_increment("semantic_dag_replay_failures")
                semantic_status, _semantic_payload = self._semantic_dag_cache.reserve(semantic_key)
            semantic_owner = semantic_status == "owner"

        try:
            result = self._search_from_state_impl(
                search_state, history, action_budget, chance_depth, pre_chance_budget
            )
        except Exception:
            self._dag_cache.publish(key, None)
            if semantic_owner and semantic_key is not None:
                self._semantic_dag_cache.publish(semantic_key, None)
            raise
        if semantic_owner and semantic_key is not None:
            reusable = self._is_comparable_result(result) and self._semantic_result_is_reusable(result)
            self._semantic_dag_cache.publish(semantic_key, result if reusable else None)
            if not reusable:
                self._audit_increment("semantic_dag_rejects")
        self._dag_cache.publish(
            key, result if self._is_comparable_result(result) else None
        )
        self._complete_strict_dag_candidate(
            dag_candidate,
            result,
            history,
            action_budget,
            chance_depth,
            pre_chance_budget,
        )
        return result

    def _search_from_state_impl(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        action_budget: int,
        chance_depth: int,
        pre_chance_budget: int,
        is_root: bool = False,
    ) -> SearchResult:
        if not search_state.get("success"):
            return SearchResult(score=float("-inf"), sequence=[], leaf_state=search_state, stats={"nodes": 1})
        if self._time_budget_exhausted():
            return self._budget_leaf_result(search_state, history)

        combat = search_state.get("combat") or {}
        enemies = combat.get("enemies") or []
        cache_key = self._subtree_cache_key(
            search_state, history, action_budget, chance_depth, pre_chance_budget
        )
        cached = None if is_root else self.subtree_cache.get(cache_key)
        if cached is not None:
            self.timing["subtree_cache_hits"] += 1
            return SearchResult(
                score=cached.score,
                sequence=list(cached.sequence),
                leaf_state=cached.leaf_state,
                state_hashes_after_actions=list(cached.state_hashes_after_actions),
                state_keys_after_actions=copy.deepcopy(cached.state_keys_after_actions),
                stats={**cached.stats, "subtree_cache_hit": True},
            )

        all_actions = available_actions_from_search_state(search_state)
        root_prune_before = self._prune_counts() if is_root else {}
        if self._time_budget_exhausted():
            return self._budget_leaf_result(search_state, history, all_actions)
        legal_actions = self._prepare_action_candidates(search_state, history, all_actions)
        if self._time_budget_exhausted():
            return self._budget_leaf_result(search_state, history, all_actions, legal_actions)
        root_pruned = self._prune_delta(root_prune_before, self._prune_counts()) if is_root else {}
        if is_root:
            action_budget, pre_chance_budget, horizon_policy = self._coverage_first_horizon(
                action_budget, pre_chance_budget, len(legal_actions)
            )
            self.timing['horizon_policy'] = horizon_policy
        horizon_boundary = (
            (chance_depth <= 0 and action_budget <= 0)
            or (chance_depth > 0 and pre_chance_budget <= 0)
        )
        if not legal_actions or horizon_boundary:
            # Reaching the configured action/chance horizon is a valid leaf.
            # Its currently playable actions are outside the bounded tree and
            # must not be reported as uncovered work.
            if horizon_boundary and legal_actions:
                self._audit_increment('horizon_boundary_leaves')
            if legal_actions and chance_depth > 0 and pre_chance_budget <= 0:
                self.timing["pre_chance_budget_exhaustions"] += 1
            result = self._settled_leaf_result(search_state, history)
            result.stats.update({
                    "nodes": 1,
                    "available_actions": len(all_actions),
                    "considered_actions": len(legal_actions),
                    "symmetry_pruned_actions": self.timing["symmetry_pruned_actions"],
                })
            if self._is_comparable_result(result):
                self.subtree_cache[cache_key] = result
            return result

        frontier_result = self._search_frontier_parallel(
            search_state,
            history,
            action_budget,
            chance_depth,
            pre_chance_budget,
            all_actions,
            legal_actions,
        )
        if frontier_result is not None:
            if self._is_comparable_result(frontier_result):
                self.subtree_cache[cache_key] = SearchResult(
                    score=frontier_result.score,
                    sequence=list(frontier_result.sequence),
                    leaf_state=frontier_result.leaf_state,
                    state_hashes_after_actions=list(frontier_result.state_hashes_after_actions),
                    state_keys_after_actions=copy.deepcopy(frontier_result.state_keys_after_actions),
                    stats=dict(frontier_result.stats),
                )
            return frontier_result

        best: Optional[SearchResult] = None
        best_effect_score = float("-inf")
        nodes = 1
        semantic_subtree_safe = True
        # Decision-review: gather (action_info, child) per root sibling when
        # capture is on, so we can retain the evaluated root candidates.
        root_cand_collect: List[Tuple[Dict[str, Any], SearchResult]] = []
        current_enemy_count = len((search_state.get("combat") or {}).get("enemies") or [])
        evaluated_children: List[Tuple[int, Dict[str, Any], SearchResult, float]] = []
        fair_root_budget_ms = self._fair_root_budget_ms(len(legal_actions)) if is_root else 0.0
        if fair_root_budget_ms > 0.0:
            self.timing['root_fair_budget_ms'] = fair_root_budget_ms
        for _idx, action_info in enumerate(legal_actions):
            if self._time_budget_exhausted():
                self._audit_increment('known_unexpanded_action_edges', len(legal_actions) - _idx)
                semantic_subtree_safe = False
                break
            previous_deadline = self._search_deadline
            branch_started = time.perf_counter()
            branch_deadline = None
            if fair_root_budget_ms > 0.0:
                branch_deadline = branch_started + fair_root_budget_ms / 1000.0
                self._search_deadline = branch_deadline
            try:
                child = self._evaluate_child_action(
                    action_info["action"],
                    search_state,
                    history,
                    action_budget,
                    chance_depth,
                    pre_chance_budget,
                    action_info["next_history"],
                    action_info["child_state"],
                )
            finally:
                self._search_deadline = previous_deadline
            if is_root:
                branch_elapsed_ms = (time.perf_counter() - branch_started) * 1000.0
                branch_audit = {
                    **self._action_audit_label(action_info),
                    'budget_ms': round(fair_root_budget_ms, 3),
                    'elapsed_ms': round(branch_elapsed_ms, 3),
                    'budget_exhausted': bool(
                        branch_deadline is not None and time.perf_counter() >= branch_deadline
                    ),
                    'nodes': int(child.stats.get('nodes', 1)),
                }
                self.timing['root_branch_budgets'].append(branch_audit)
                self._audit_increment('root_branches_evaluated')
            nodes += int(child.stats.get("nodes", 1))
            semantic_subtree_safe = (
                semantic_subtree_safe and self._semantic_result_is_reusable(child)
            )
            effect_score = float(action_info.get("effect_score") or 0.0)
            if is_root:
                evaluated_children.append((_idx, action_info, child, effect_score))
            else:
                if (
                    self._is_comparable_result(child) and (best is None
                    or child.score > best.score
                    or (child.score == best.score and effect_score > best_effect_score))
                ):
                    best = child
                    best_effect_score = effect_score
            # Intermediate snapshots now restore combat history, mutable hook
            # listener state, player runtime counters, and card-db subscriptions.
            # Keep them by default so every child restores its direct parent and
            # executes one edge. The escape hatch preserves the former clean-root
            # rebuild for diagnosing a future restore regression without silently
            # changing depth, legal actions, or search scoring.
            if (
                self.reuse_cli_processes
                and self.root_snapshot_json is not None
                and os.environ.get("STS2_FORCE_ROOT_REPLAY") == "1"
                and (not history or current_enemy_count >= 3)
            ):
                self._forget_intermediate_snapshots()
            remaining_siblings = len(legal_actions) - _idx - 1
            if is_root and self._mark_lethal_early_stop(child, remaining_siblings):
                semantic_subtree_safe = False
                break

        if is_root:
            top_base_score = max((float(child.score) for _, _, child, _ in evaluated_children), default=float("-inf"))
            adjusted_pairs: List[Tuple[Dict[str, Any], SearchResult]] = []
            for _idx, action_info, child, effect_score in evaluated_children:
                adjustments = self._root_adjustments(search_state, action_info, child, top_base_score)
                action_info.update(adjustments)
                adjusted_score = float(child.score) + float(action_info.get("root_adjustment", 0.0))
                action_info["base_score"] = float(child.score)
                action_info["adjusted_score"] = adjusted_score
                adjusted_pairs.append((action_info, child))
            best_pair, selection_audit = self._select_root_candidate(adjusted_pairs)
            if best_pair is not None:
                best = self._with_root_adjusted_score(best_pair[1], best_pair[0])
                best_effect_score = float(best_pair[0].get('effect_score') or 0.0)
            if self.capture_root_topk != 0:
                root_cand_collect.extend(adjusted_pairs)
        else:
            selection_audit = {}

        if best is None:
            if self.timing.get("time_budget_exhausted"):
                return self._budget_leaf_result(search_state, history, all_actions, legal_actions)
            return SearchResult(score=float('-inf'), sequence=[],
                                leaf_state=self._build_failed_search_state('No comparable branch result'),
                                stats={'nodes': nodes, **selection_audit})
        best.stats = {
            **best.stats,
            "nodes": nodes,
            "available_actions": len(all_actions),
            "considered_actions": len(legal_actions),
            "symmetry_pruned_actions": self.timing["symmetry_pruned_actions"],
            "time_budget_exhausted": bool(self.timing.get("time_budget_exhausted") or 0),
            "semantic_reuse_safe": semantic_subtree_safe,
            **selection_audit,
        }
        if is_root:
            best.stats['root_coverage'] = self._root_coverage(
                len(all_actions), len(legal_actions), len(evaluated_children), root_pruned
            )
            best.stats['score_explanation'] = self._explain_result(best, history)
        if not is_root:
            self.subtree_cache[cache_key] = SearchResult(
                score=best.score,
                sequence=list(best.sequence),
                leaf_state=best.leaf_state,
                state_hashes_after_actions=list(best.state_hashes_after_actions),
                state_keys_after_actions=copy.deepcopy(best.state_keys_after_actions),
                stats=dict(best.stats),
            )
        if self.capture_root_topk != 0 and is_root and root_cand_collect:
            best.root_candidates = self._build_root_candidates(root_cand_collect)
        return best

    def _turn_cap_result(
        self,
        action: SearchAction,
        child_state: Dict[str, Any],
        child_state_hash: str,
        next_history: Sequence[RecordedAction],
    ) -> SearchResult:
        self._audit_increment('depth_cutoff_leaves')
        leaf_state, appended_end_turn = self._settle_leaf_state(child_state, next_history)
        sequence = [action]
        state_hashes = [child_state_hash]
        state_keys = [canonicalize_search_state_for_plan_reuse(child_state)]
        if appended_end_turn:
            self._audit_increment('completed_turn_lines')
            sequence.append(SearchAction('end_turn'))
            state_hashes.append(hash_search_state_for_plan_reuse(leaf_state))
            state_keys.append(canonicalize_search_state_for_plan_reuse(leaf_state))
        semantic_safe = self._semantic_transition_preserves_pile_order(
            child_state, leaf_state
        )
        return SearchResult(
            score=self._score_with_history(leaf_state, next_history),
            sequence=sequence,
            leaf_state=leaf_state,
            state_hashes_after_actions=state_hashes,
            state_keys_after_actions=state_keys,
            stats={'nodes': 1, 'semantic_reuse_safe': semantic_safe},
        )

    def _evaluate_child_action(
        self,
        action: SearchAction,
        parent_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        action_budget: int,
        chance_depth: int,
        pre_chance_budget: int,
        next_history: Optional[Sequence[RecordedAction]] = None,
        child_state: Optional[Dict[str, Any]] = None,
    ) -> SearchResult:
        started = time.perf_counter()
        self._audit_increment('expanded_action_edges')
        self._ensure_coverage_sets()
        self._coverage_sets["expanded_edges"].add(self._coverage_edge_key(parent_state, action))
        if next_history is None or child_state is None:
            next_history = list(history) + [self._recorded_action_from_search_action(action)]
            child_state_result = self.combat_to_state(next_history)
            child_state = self._extract_search_state(child_state_result)
        # Rebuild end_turn and verify a real phase transition before scoring it.
        if action.action_type == "end_turn":
            if next_history is None:
                next_history = list(history) + [self._recorded_action_from_search_action(action)]
            try:
                child_state = self._validate_settled_transition(parent_state, self._extract_search_state(
                    self.combat_to_state(next_history)))
            except Exception as exc:
                child_state = self._settlement_failure(str(exc))
        child_state_hash = hash_search_state_for_plan_reuse(child_state)
        child_state_key = canonicalize_search_state_for_plan_reuse(child_state)
        if not child_state.get("success"):
            result = SearchResult(
                score=float("-inf"),
                sequence=[action],
                leaf_state=child_state,
                state_hashes_after_actions=[child_state_hash],
                state_keys_after_actions=[child_state_key],
                stats={"nodes": 1, "semantic_reuse_safe": False},
            )
            self._record_branch_timing(action, started, result)
            return result
        if action.action_type != 'end_turn' and child_state.get('terminal_decision'):
            self._audit_increment('completed_turn_lines')
        if action.action_type == "end_turn":
            self._audit_increment('completed_turn_lines')
            continuation = self._evaluate_enemy_chance_node(
                parent_state, child_state, next_history, action_budget, chance_depth - 1)
            result = SearchResult(
                score=continuation.score,
                sequence=[action] + continuation.sequence,
                leaf_state=continuation.leaf_state,
                state_hashes_after_actions=[child_state_hash] + continuation.state_hashes_after_actions,
                state_keys_after_actions=[child_state_key] + continuation.state_keys_after_actions,
                stats={
                    **continuation.stats,
                    "has_chance_node": True,
                    "semantic_reuse_safe": (
                        self._semantic_transition_preserves_pile_order(
                            parent_state, child_state
                        )
                        and self._semantic_result_is_reusable(continuation)
                    ),
                },
            )
            self._record_branch_timing(action, started, result)
            return result
        if chance_depth <= 0 and action_budget <= 1:
            result = self._turn_cap_result(action, child_state, child_state_hash, next_history)
            result.stats["semantic_reuse_safe"] = (
                self._semantic_transition_preserves_pile_order(parent_state, child_state)
                and self._semantic_result_is_reusable(result)
            )
            self._record_branch_timing(action, started, result)
            return result
        if chance_depth > 0 and pre_chance_budget <= 1:
            result = self._turn_cap_result(action, child_state, child_state_hash, next_history)
            result.stats["semantic_reuse_safe"] = (
                self._semantic_transition_preserves_pile_order(parent_state, child_state)
                and self._semantic_result_is_reusable(result)
            )
            self._record_branch_timing(action, started, result)
            return result

        next_action_budget = action_budget - 1 if chance_depth <= 0 else action_budget
        next_pre_chance_budget = pre_chance_budget - 1 if chance_depth > 0 else 0
        deeper = self._search_from_state(
            child_state,
            next_history,
            next_action_budget,
            chance_depth,
            next_pre_chance_budget,
        )
        result = SearchResult(
            score=deeper.score,
            sequence=[action] + deeper.sequence,
            leaf_state=deeper.leaf_state,
            state_hashes_after_actions=[child_state_hash] + deeper.state_hashes_after_actions,
            state_keys_after_actions=[child_state_key] + deeper.state_keys_after_actions,
            stats={
                **deeper.stats,
                "semantic_reuse_safe": (
                    self._semantic_transition_preserves_pile_order(
                        parent_state, child_state
                    )
                    and self._semantic_result_is_reusable(deeper)
                ),
            },
        )
        self._record_branch_timing(action, started, result)
        return result

    def _materialize_isolated_root_candidate(
        self,
        action: SearchAction,
        next_history: Sequence[RecordedAction],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Build one root successor on an isolated engine worker."""
        branch = self._new_branch_searcher(0.0, max_workers=1, parallel_frontier=False)
        branch._root_summary = self._root_summary
        branch._preference_root = self._preference_root
        branch._scoring_history_offset = self._scoring_history_offset
        started = time.perf_counter()
        try:
            child_state = branch._extract_search_state(branch.combat_to_state(next_history))
            if self._is_unsupported_terminal_state(child_state) and action.action_type == 'end_turn':
                child_state = branch._extract_search_state(
                    branch.combat_to_state(next_history, prefer_cold=True)
                )
            return child_state, {
                'elapsed_ms': (time.perf_counter() - started) * 1000.0,
                'timing': dict(branch.timing),
                'coverage': branch._coverage_snapshot(),
            }
        finally:
            branch.close()

    def _prepare_action_candidates_parallel(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        actions: Sequence[SearchAction],
    ) -> List[Dict[str, Any]]:
        """Parallelize only root successor generation, then merge deterministically.

        Candidate filtering, exact ordering, and state dedup remain on the
        coordinator. Only the mutable engine calls move to isolated workers, so
        root branches keep the same continuation depth and budget as serial
        search. This path is used for imported live roots; synthetic/unit roots
        retain the existing single-engine implementation.
        """
        self._audit_increment('decision_states_prepared')
        self._audit_increment('available_action_edges', len(actions))
        self._ensure_coverage_sets()
        source_state_key = hash_search_state(search_state)
        self._coverage_sets['decision_states'].add(source_state_key)
        self._coverage_sets['available_edges'].update(
            f"{source_state_key}:{self._coverage_action_token(action)}" for action in actions
        )
        candidate_actions = self._dedupe_symmetric_actions(actions, search_state) if self.symmetry_dedup else list(actions)
        candidate_actions = self._prioritize_actions(candidate_actions, search_state)
        expand_potions = self._should_expand_potions(search_state)
        eligible: List[Tuple[SearchAction, List[RecordedAction]]] = []
        for action in candidate_actions:
            if action.action_type == 'discard_potion':
                self.timing['discard_potion_pruned_actions'] += 1
                continue
            if action.action_type == 'use_potion' and not expand_potions:
                self.timing['budget_pruned_actions'] += 1
                continue
            if action.action_type == 'use_potion':
                potion_id = str((action.metadata or {}).get('potion_id') or '').strip().upper()
                potion_id = potion_id.replace('-', '_').replace(' ', '_').removeprefix('POTION.')
                if potion_id in AUTOMATIC_POTION_IDS:
                    self.timing['automatic_potion_pruned_actions'] = (
                        self.timing.get('automatic_potion_pruned_actions', 0) + 1
                    )
                    continue
                if potion_id in _MODAL_POTION_IDS:
                    self.timing['modal_potion_pruned_actions'] = (
                        self.timing.get('modal_potion_pruned_actions', 0) + 1
                    )
                    continue
            eligible.append((action, list(history) + [self._recorded_action_from_search_action(action)]))

        if not eligible:
            self._audit_increment('candidate_action_edges', 0)
            return []

        started = time.perf_counter()
        workers = min(self.max_workers, len(eligible))
        self.timing['root_prepare_workers'] = workers
        self.timing['root_prepare_jobs_queued'] = len(eligible)
        prepared: List[Tuple[int, SearchAction, List[RecordedAction], Dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._materialize_isolated_root_candidate, action, next_history): index
                for index, (action, next_history) in enumerate(eligible)
            }
            for future in as_completed(futures):
                index = futures[future]
                action, next_history = eligible[index]
                child_state, meta = future.result()
                self._merge_branch_timing(meta.get('timing'), meta.get('coverage'))
                prepared.append((index, action, next_history, child_state))
        self.timing['root_prepare_jobs_completed'] = len(prepared)
        self.timing['root_prepare_wall_ms'] = (time.perf_counter() - started) * 1000.0

        candidates: List[Dict[str, Any]] = []
        seen_child_hashes: set[str] = set()
        for _index, action, next_history, child_state in sorted(prepared, key=lambda row: row[0]):
            if self._is_unsupported_terminal_state(child_state) and action.action_type != 'end_turn':
                self.timing['unsupported_pruned_actions'] += 1
                continue
            if self._is_noop_potion_action(search_state, child_state, action):
                self.timing['noop_potion_pruned_actions'] += 1
                continue
            if self.state_dedup and child_state.get('success'):
                child_hash = self._exact_state_identity(child_state)
                if child_hash in seen_child_hashes:
                    self.timing['state_pruned_actions'] += 1
                    continue
                seen_child_hashes.add(child_hash)
            effect_score = self._transition_effect_score(search_state, child_state)
            candidates.append({
                'action': action,
                'next_history': next_history,
                'child_state': child_state,
                'effect_score': effect_score,
            })
            self._coverage_sets['candidate_edges'].add(
                f"{source_state_key}:{self._coverage_action_token(action)}"
            )
        self._audit_increment('candidate_action_edges', len(candidates))
        return candidates

    def _try_batch_expand_children(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        actions: Sequence[SearchAction],
    ) -> Dict[Tuple[Tuple[str, Tuple[Tuple[str, Any], ...]], ...], Dict[str, Any]]:
        if (
            not self.reuse_cli_processes
            or os.environ.get("STS2_DISABLE_BATCH_EXPANSION") == "1"
            or os.environ.get("STS2_ENABLE_BATCH_EXPANSION") == "0"
            or not actions
        ):
            return {}
        history_key = self._history_key(history)
        with self._cli_lock:
            worker_ctx = self._cli_by_thread.get(threading.get_ident())
        if worker_ctx is None:
            return {}
        parent_snapshot_id = worker_ctx.snapshot_ids_by_history.get(history_key)
        if not parent_snapshot_id:
            return {}

        requests: List[Dict[str, Any]] = []
        request_rows: List[Tuple[Tuple[Any, ...], RecordedAction]] = []
        for action in actions:
            # end_turn retains the existing independent replay + settled-phase
            # validation. The batch API is currently for direct player actions.
            if action.action_type == "end_turn":
                continue
            recorded = self._recorded_action_from_search_action(action)
            next_history = list(history) + [recorded]
            child_history_key = self._history_key(next_history)
            requests.append({
                "action": recorded.action,
                # The batch command restores this exact parent snapshot before
                # every child. Enemy/card ordering therefore cannot drift
                # between request construction and execution; preserve the
                # original indices. Semantic replay resolution is only for a
                # separately imported process, and collapses distinct targets
                # when multiple enemies share the same monster id.
                "args": self._strip_cli_payload(recorded.args, recorded.action),
                "snapshot_id": self._snapshot_id_for_history(child_history_key),
            })
            request_rows.append((child_history_key, recorded))
        if not requests:
            return {}

        started = time.perf_counter()
        try:
            result = worker_ctx.cli.expand_combat_children(
                parent_snapshot_id,
                requests,
                lang=self.combat_spec.lang,
                fingerprint_mode="none",
                timeout_s=self._engine_call_timeout_s(),
            )
            self._record_cli_transport(worker_ctx.cli, "expand")
        except TimeoutError as exc:
            # A timed-out CLI has an unread or partially written response. It
            # is not safe to return it to the pool or to continue reading from
            # it on the next request. Kill/evict it before any fallback replay.
            self._audit_increment("batch_expand_rpc_failures")
            self._audit_increment("batch_expand_rpc_timeouts")
            self._record_engine_rpc_failure("expand", exc)
            self._discard_worker_ctx(worker_ctx, reason="batch_expand_timeout")
            self._audit_increment("batch_expand_fallbacks", len(requests))
            return {}
        except Exception as exc:
            self._audit_increment("batch_expand_rpc_failures")
            self._record_engine_rpc_failure("expand", exc)
            self._discard_worker_ctx(
                worker_ctx, reason=f"batch_expand_exception:{type(exc).__name__}"
            )
            self._audit_increment("batch_expand_fallbacks", len(requests))
            return {}
        finally:
            self.timing["batch_expand_total_ms"] += (time.perf_counter() - started) * 1000.0
        self._audit_increment("batch_expand_calls")
        self._audit_increment("batch_expand_children", len(requests))
        if result.get("type") != "combat_children_expanded" or not result.get("success"):
            self._audit_increment("batch_expand_fallbacks", len(requests))
            return {}

        expanded: Dict[
            Tuple[Tuple[str, Tuple[Tuple[str, Any], ...]], ...], Dict[str, Any]
        ] = {}
        response_rows = result.get("children") or []
        for index, (child_history_key, recorded) in enumerate(request_rows):
            row = response_rows[index] if index < len(response_rows) else None
            if not isinstance(row, dict) or not row.get("success"):
                self._audit_increment("batch_expand_fallbacks")
                continue
            restore_result = row.get("restore_result")
            if isinstance(restore_result, dict):
                self._record_restore_mode(restore_result)
            action_result = row.get("action_result") or {}
            if isinstance(action_result, dict):
                self._record_headless_timing(
                    action_result, "headless_execute_ms", "headless_action_total_ms"
                )
                self._record_action_execution_profile(
                    action_result,
                    recorded,
                    history_len=len(history) + 1,
                    suffix_len=1,
                    cli_ms=float(row.get("elapsed_ms") or 0.0),
                )

            capture_result = row.get("snapshot_result") or {}
            capture_ms = float(row.get("capture_ms") or 0.0)
            state_ms = float(row.get("state_ms") or 0.0)
            if capture_ms > 0.0:
                self.timing["snapshot_capture_count"] += 1
                self.timing["snapshot_capture_total_ms"] += capture_ms
                self.timing["headless_capture_total_ms"] += float(
                    capture_result.get("headless_capture_ms") or capture_ms
                )
            if state_ms > 0.0:
                self.timing["headless_get_state_total_ms"] += state_ms

            if row.get("terminal"):
                terminal_result = dict(action_result)
                if terminal_result.get("decision") == "combat_reward":
                    terminal_result["decision"] = "victory"
                expanded[child_history_key] = self._build_terminal_search_state(
                    terminal_result, last_action=recorded
                )
                continue

            child_state = row.get("combat_state_for_search")
            if not isinstance(child_state, dict) or not child_state.get("success"):
                self._audit_increment("batch_expand_fallbacks")
                continue
            snapshot_id = str(row.get("snapshot_id") or "")
            fingerprint = str(row.get("state_fingerprint") or "")
            semantic_fingerprint = str(row.get("semantic_state_fingerprint") or "")
            if snapshot_id:
                worker_ctx.snapshot_ids_by_history[child_history_key] = snapshot_id
                child_state["engine_snapshot_id"] = snapshot_id
            if fingerprint:
                worker_ctx.snapshot_fingerprints_by_history[child_history_key] = fingerprint
                child_state["engine_state_fingerprint"] = fingerprint
                child_state["engine_state_fingerprint_schema"] = "sts2-combat-snapshot-v1"
            if semantic_fingerprint:
                worker_ctx.snapshot_semantic_fingerprints_by_history[child_history_key] = (
                    semantic_fingerprint
                )
                child_state["engine_semantic_state_fingerprint"] = semantic_fingerprint
                child_state["engine_semantic_state_fingerprint_schema"] = (
                    "sts2-combat-semantic-v1"
                )
            expanded[child_history_key] = child_state
        return expanded

    def _prepare_action_candidates(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        actions: Sequence[SearchAction],
    ) -> List[Dict[str, Any]]:
        self._audit_increment('decision_states_prepared')
        self._audit_increment('available_action_edges', len(actions))
        self._ensure_coverage_sets()
        source_state_key = hash_search_state(search_state)
        self._coverage_sets["decision_states"].add(source_state_key)
        self._coverage_sets["available_edges"].update(
            f"{source_state_key}:{self._coverage_action_token(action)}" for action in actions
        )
        candidate_actions = self._dedupe_symmetric_actions(actions, search_state) if self.symmetry_dedup else list(actions)
        candidate_actions = self._prioritize_actions(candidate_actions, search_state)
        expand_potions = self._should_expand_potions(search_state)
        filtered_actions: List[SearchAction] = []
        for action in candidate_actions:
            if action.action_type == "discard_potion":
                self.timing["discard_potion_pruned_actions"] += 1
                continue
            if action.action_type == "use_potion" and not expand_potions:
                self.timing["budget_pruned_actions"] += 1
                continue
            if action.action_type == "use_potion":
                # Skip modal-opening potions BEFORE executing them: their card
                # select wedges the worker's action executor and poisons every
                # later action on that process (see _MODAL_POTION_IDS). Must be
                # gated before combat_to_state, since the poison happens the
                # moment the potion is applied during expansion.
                potion_id = str((action.metadata or {}).get("potion_id") or "").strip().upper()
                potion_id = potion_id.replace('-', '_').replace(' ', '_').removeprefix('POTION.')
                if potion_id in AUTOMATIC_POTION_IDS:
                    self.timing["automatic_potion_pruned_actions"] = (
                        self.timing.get("automatic_potion_pruned_actions", 0) + 1
                    )
                    continue
                if potion_id in _MODAL_POTION_IDS:
                    self.timing["modal_potion_pruned_actions"] = (
                        self.timing.get("modal_potion_pruned_actions", 0) + 1
                    )
                    continue
            filtered_actions.append(action)

        batch_timeout_before = int(self.timing.get("batch_expand_rpc_timeouts") or 0)
        batch_states = self._try_batch_expand_children(search_state, history, filtered_actions)
        if int(self.timing.get("batch_expand_rpc_timeouts") or 0) > batch_timeout_before:
            # The batch call consumed the remaining decision budget and its
            # worker has already been evicted. Do not replay every sibling as a
            # fallback: that would create a fresh process storm after the
            # timeout and obscure the incomplete-tree result.
            if self._search_deadline is not None:
                self._search_deadline = time.perf_counter()
            self.timing["time_budget_exhausted"] = 1
            self._audit_increment("known_unexpanded_action_edges", len(filtered_actions))
            return []
        candidates: List[Dict[str, Any]] = []
        seen_child_candidates: Dict[
            str, List[Tuple[List[RecordedAction], Dict[str, Any]]]
        ] = {}
        with self._cli_lock:
            dedup_worker_ctx = self._cli_by_thread.get(threading.get_ident())
        for index, action in enumerate(filtered_actions):
            if self._time_budget_exhausted():
                self._audit_increment(
                    "known_unexpanded_action_edges", len(filtered_actions) - index
                )
                break
            next_history = list(history) + [self._recorded_action_from_search_action(action)]
            child_state = batch_states.get(self._history_key(next_history))
            if child_state is None:
                child_state_result = self.combat_to_state(next_history)
                child_state = self._extract_search_state(child_state_result)
            if self._is_unsupported_terminal_state(child_state):
                # end_turn is the always-legal fallback and resolves the enemy
                # turn, which a churned pooled worker can fail to build (returns
                # success=False / empty combat). Pruning a failed end_turn here is
                # what collapses the candidate list to zero — and when end_turn is
                # the ONLY surviving candidate (empty hand / no energy, potions
                # filtered) that emits a FAKE empty search mid-combat. For
                # end_turn specifically, retry the child build ONCE on a fresh
                # cold process (the verified-correct path); if it is still
                # unsupported, keep it as a candidate anyway rather than collapse
                # to zero. (Common, healthy path pays no cold cost: only a failed
                # end_turn child triggers the cold rebuild.)
                if action.action_type == "end_turn":
                    child_state = self._extract_search_state(
                        self.combat_to_state(next_history, prefer_cold=True)
                    )
                else:
                    self.timing["unsupported_pruned_actions"] += 1
                    continue
            if self._is_noop_potion_action(search_state, child_state, action):
                self.timing["noop_potion_pruned_actions"] += 1
                continue
            if self.state_dedup and child_state.get("success"):
                # The cheap projection is only a collision prefilter. A visible
                # match never authorizes pruning: both process-local snapshots
                # receive strict fingerprints on demand, and only identical
                # complete restorable states are merged.
                visible_key = hash_search_state_for_dedup(child_state)
                prior_candidates = seen_child_candidates.setdefault(
                    visible_key, []
                )
                strict = self._engine_state_fingerprint(child_state)
                if prior_candidates and not strict and dedup_worker_ctx is not None:
                    samples = self.timing.setdefault(
                        "state_dedup_collision_samples", []
                    )
                    if len(samples) < 20:
                        samples.append({
                            "source_state": source_state_key,
                            "current_history": repr(self._history_key(next_history)),
                            "prior_histories": [
                                repr(self._history_key(prior_history))
                                for prior_history, _ in prior_candidates
                            ],
                        })
                    strict, _ = self._ensure_snapshot_fingerprints(
                        dedup_worker_ctx,
                        next_history,
                        search_state=child_state,
                    )
                duplicate = False
                if strict:
                    for prior_history, prior_state in prior_candidates:
                        prior_strict = self._engine_state_fingerprint(prior_state)
                        if not prior_strict and dedup_worker_ctx is not None:
                            prior_strict, _ = self._ensure_snapshot_fingerprints(
                                dedup_worker_ctx,
                                prior_history,
                                search_state=prior_state,
                            )
                        if prior_strict and prior_strict == strict:
                            duplicate = True
                            break
                if duplicate:
                    self.timing["state_pruned_actions"] += 1
                    continue
                prior_candidates.append((list(next_history), child_state))
            effect_score = self._transition_effect_score(search_state, child_state)
            candidates.append(
                {
                    "action": action,
                    "next_history": next_history,
                    "child_state": child_state,
                    "effect_score": effect_score,
                }
            )
            self._coverage_sets["candidate_edges"].add(
                f"{source_state_key}:{self._coverage_action_token(action)}"
            )
        # Full-expansion policy: within the configured depth/chance_depth horizon
        # the search expands every legal candidate. Count-based truncation
        # (branch_limit / candidate_budget_control) is intentionally removed so
        # the searcher never silently drops a visible, playable action. Only
        # provably-equivalent dedup (symmetry / exact-state) is allowed to prune.
        self._audit_increment('candidate_action_edges', len(candidates))
        return candidates

    def _should_expand_potions(self, search_state: Dict[str, Any]) -> bool:
        return should_expand_potions_for_state(
            search_state,
            self.expand_potions,
            self.potion_emergency_hp_ratio,
        )

    @staticmethod
    def _is_unsupported_terminal_state(search_state: Dict[str, Any]) -> bool:
        terminal_decision = str(search_state.get("terminal_decision") or "")
        if terminal_decision in {"card_select", "bundle_select", "unknown"}:
            return True
        combat = search_state.get("combat")
        if terminal_decision:
            return False
        if not isinstance(combat, dict):
            return True
        player = combat.get("player")
        if not isinstance(player, dict) or not player:
            return True
        return False

    @staticmethod
    def _combat_signature_without_actions(search_state: Dict[str, Any]) -> Any:
        combat = search_state.get("combat") or {}
        if not isinstance(combat, dict):
            return None

        def freeze_card(card: Dict[str, Any]) -> tuple[Any, ...]:
            return (
                card.get("card_id"),
                card.get("upgrade"),
                card.get("current_cost"),
                card.get('display_cost'),
                card.get('display_costs_x'),
                tuple(card.get("keywords") or []),
                card.get("affliction"),
                card.get("affliction_count"),
            )

        def freeze_power(power: Dict[str, Any]) -> tuple[Any, ...]:
            return (power.get("id"), power.get("amount"), power.get("extra"))

        def freeze_enemy(enemy: Dict[str, Any]) -> tuple[Any, ...]:
            intent = enemy.get("intent") or {}
            return (
                enemy.get("monster_id"),
                enemy.get("hp"),
                enemy.get("max_hp"),
                enemy.get("block"),
                tuple(freeze_power(p) for p in (enemy.get("powers") or [])),
                (
                    tuple(intent.get("intent_types") or []),
                    intent.get("total_damage"),
                    intent.get("display_damage"),
                    intent.get("hits"),
                ),
            )

        player = combat.get("player") or {}
        return (
            combat.get("round_number"),
            combat.get("turn_number"),
            combat.get("is_player_turn"),
            (
                player.get("hp"),
                player.get("max_hp"),
                player.get("block"),
                player.get("energy"),
                tuple(freeze_power(p) for p in (player.get("powers") or [])),
            ),
            tuple(freeze_enemy(e) for e in (combat.get("enemies") or [])),
            tuple(freeze_card(c) for c in (combat.get("hand") or [])),
            tuple(freeze_card(c) for c in (combat.get("draw_pile") or [])),
            tuple(freeze_card(c) for c in (combat.get("discard_pile") or [])),
            tuple(freeze_card(c) for c in (combat.get("exhaust_pile") or [])),
            tuple(freeze_card(c) for c in (combat.get("play_pile") or [])),
        )

    @classmethod
    def _is_noop_potion_action(
        cls,
        parent_state: Dict[str, Any],
        child_state: Dict[str, Any],
        action: SearchAction,
    ) -> bool:
        if action.action_type != "use_potion":
            return False
        if not parent_state.get("success") or not child_state.get("success"):
            return False
        return cls._combat_signature_without_actions(parent_state) == cls._combat_signature_without_actions(child_state)

    def _dedupe_symmetric_actions(
        self,
        actions: Sequence[SearchAction],
        search_state: Dict[str, Any],
    ) -> List[SearchAction]:
        combat = search_state.get("combat") or {}
        enemies = combat.get("enemies") or []
        hand = combat.get("hand") or []

        enemy_by_index = {
            int(enemy.get("index")): enemy
            for enemy in enemies
            if isinstance(enemy, dict) and enemy.get("index") is not None
        }

        def enemy_class(target_index: Optional[int]) -> tuple[Any, ...]:
            if target_index is None:
                return ("no_target",)
            enemy = enemy_by_index.get(target_index) or {}
            intent = enemy.get("intent") or {}
            powers = tuple(
                sorted(
                    (str(p.get("id") or ""), int(p.get("amount") or 0))
                    for p in (enemy.get("powers") or [])
                    if isinstance(p, dict)
                )
            )
            return (
                str(enemy.get("monster_id") or ""),
                int(enemy.get("hp") or 0),
                int(enemy.get("block") or 0),
                tuple(intent.get("intent_types") or []),
                intent.get("total_damage"),
                intent.get("display_damage"),
                intent.get("hits"),
                powers,
            )

        def card_class(card_index: Optional[int], metadata: Optional[Dict[str, Any]]) -> tuple[Any, ...]:
            if card_index is None or card_index < 0 or card_index >= len(hand):
                card_id = (metadata or {}).get("card_id")
                return (str(card_id or ""), None, None, None, None, None)
            card = hand[card_index] or {}
            return (
                str(card.get("card_id") or (metadata or {}).get("card_id") or ""),
                int(card.get("upgrade") or 0),
                _card_energy_cost(card, (metadata or {}).get("card_id")),
                card.get("affliction"),
                card.get("affliction_count"),
                tuple(card.get("keywords") or []),
            )

        seen: set[tuple[Any, ...]] = set()
        deduped: List[SearchAction] = []
        pruned = 0
        for action in actions:
            sig = [action.action_type]
            if action.action_type == "play_card":
                sig.extend([card_class(action.card_index, action.metadata), enemy_class(action.target_index)])
            elif action.action_type in {"use_potion", "discard_potion"}:
                sig.append(tuple(sorted((action.metadata or {}).items())))
                sig.append(enemy_class(action.target_index))
            else:
                sig.append(action.target_index)
            signature = tuple(sig)
            if signature in seen:
                pruned += 1
                continue
            seen.add(signature)
            deduped.append(action)
        self.timing["symmetry_pruned_actions"] += pruned
        return deduped

    @staticmethod
    def _transition_effect_score(parent_state: Dict[str, Any], child_state: Dict[str, Any]) -> float:
        parent_combat = parent_state.get("combat") or {}
        child_combat = child_state.get("combat") or {}
        parent_player = parent_combat.get("player") or {}
        child_player = child_combat.get("player") or {}
        parent_enemies = parent_combat.get("enemies") or []
        child_enemies = child_combat.get("enemies") or []
        parent_enemy_hp = sum(int(e.get("hp") or 0) for e in parent_enemies)
        child_enemy_hp = sum(int(e.get("hp") or 0) for e in child_enemies)
        parent_player_hp = int(parent_player.get("hp") or 0)
        child_player_hp = int(child_player.get("hp") or 0)
        return float(parent_enemy_hp - child_enemy_hp) - 2.0 * float(parent_player_hp - child_player_hp)

    def _prioritize_actions(self, actions: List[SearchAction], search_state: Dict[str, Any]) -> List[SearchAction]:
        combat = search_state.get("combat") or {}
        context = search_state.get("context") or {}
        enemies = combat.get("enemies") or []
        hand = combat.get("hand") or []
        player = combat.get("player") or {}
        energy = int(player.get("energy") or 0)
        player_block = int(player.get("block") or 0)
        try:
            player_hp = float(player.get("hp") or 0.0)
            player_max_hp = float(player.get("max_hp") or 1.0)
        except Exception:
            player_hp, player_max_hp = 0.0, 1.0
        hp_ratio = player_hp / player_max_hp if player_max_hp > 0 else 1.0
        try:
            floor = int(context.get("floor")) if context.get("floor") is not None else None
        except Exception:
            floor = None

        enemy_by_index = {
            int(enemy.get("index")): enemy
            for enemy in enemies
            if isinstance(enemy, dict) and enemy.get("index") is not None
        }
        card_by_index = {
            idx: card for idx, card in enumerate(hand) if isinstance(card, dict)
        }

        has_non_end_turn = any(a.action_type != "end_turn" for a in actions)
        incoming_damage = 0.0
        for enemy in enemies:
            intent = enemy.get("intent") or {}
            if not intent_deals_damage(intent):
                continue
            incoming_damage += intent_total_damage(intent)
        base_hp_loss = max(0.0, incoming_damage - float(player_block))
        def estimated_card_damage(action: SearchAction) -> float:
            card = card_by_index.get(action.card_index or -1) or {}
            return _card_numeric_stat(card, "damage", (action.metadata or {}).get("card_id"))

        def estimated_card_block(action: SearchAction) -> float:
            card = card_by_index.get(action.card_index or -1) or {}
            return _card_numeric_stat(card, "block", (action.metadata or {}).get("card_id"))

        def potion_priority(action: SearchAction) -> float:
            return -5.0 if has_non_end_turn else 20.0

        def score(action: SearchAction) -> tuple[float, int, int, int]:
            action_type = action.action_type
            if action_type == "discard_potion":
                return (-1000.0, 0, 0, 0)
            if action_type == "end_turn":
                return ((-50.0 if has_non_end_turn else 10.0), 0, 0, 0)
            if action_type == "use_potion":
                return (potion_priority(action), 0, 0, -(action.target_index or 0))

            card = card_by_index.get(action.card_index or -1) or {}
            metadata = action.metadata or {}
            card_id = str(card.get("card_id") or metadata.get("card_id") or "")
            current_cost = _card_energy_cost(card, metadata.get("card_id"))
            target = enemy_by_index.get(action.target_index or -1) or {}
            target_hp = int(target.get("hp") or 0)
            target_intent = target.get("intent") or {}
            intent_damage = int(intent_total_damage(target_intent))
            damage = estimated_card_damage(action)
            block_gain = estimated_card_block(action)
            projected_loss = max(0.0, incoming_damage - float(player_block + block_gain))
            lethal_bonus = 40.0 if damage > 0 and target_hp > 0 and damage >= target_hp else 0.0
            bash_bonus = 18.0 if card_id == "BASH" else 0.0
            damage_bonus = float(damage) * 2.5
            intent_bonus = float(intent_damage) * 1.5 if damage > 0 else 0.0
            energy_penalty = float(max(current_cost - max(energy, 0), 0)) * 20.0
            target_bias = -float(action.target_index or 0) * 0.1
            defense_bias = 0.0
            if damage >= 2.0 * max(block_gain, 1):
                defense_bias = 18.0 + float(damage) * 2.0
            else:
                defense_bias = 30.0 + float(block_gain) * 6.0
                if projected_loss > 3.0:
                    defense_bias += 60.0 + 10.0 * projected_loss
            if block_gain > 0 and base_hp_loss <= 2.0:
                defense_bias -= 10.0
            return (
                bash_bonus + lethal_bonus + damage_bonus + intent_bonus + defense_bias - energy_penalty + target_bias,
                damage,
                -current_cost,
                -(action.target_index or 0),
            )

        ordered = sorted(actions, key=score, reverse=True)

        # Lossless dedup of fully-equivalent candidates is handled upstream by
        # `_dedupe_symmetric_actions` (strong enemy_class key incl. intent/powers).
        # No filtering/truncation here: the sort only sets a deterministic
        # tiebreak order and never drops a candidate. Returning `ordered` keeps
        # every visible action in the tree within the configured horizon.
        return ordered

    def _evaluate_leaf_cached(self, search_state: Dict[str, Any], state_hash: Optional[str] = None) -> float:
        self._audit_increment('leaf_score_requests')
        state_hash = state_hash or (
            hash_search_state_for_dedup(search_state)
            if self.state_dedup
            else hash_search_state(search_state)
        )
        self._ensure_coverage_sets()
        self._coverage_sets["leaf_states"].add(str(state_hash))
        cached = self.eval_cache.get(state_hash)
        if cached is not None:
            self.timing["eval_cache_hits"] += 1
            return cached
        score = self._evaluate_leaf_score(search_state)
        self._audit_increment('unique_leaf_scores')
        self.eval_cache[state_hash] = score
        return score

    def _evaluate_leaf_score(self, search_state: Dict[str, Any]) -> float:
        if getattr(self, 'preference_scorer', None) is not None:
            raise RuntimeError('Preference scoring requires _score_with_history to preserve resource costs')
        if search_state.get('success') is False:
            return float('-inf')
        score = evaluate_leaf(
            search_state, self._root_summary, self.leaf_score_mode, self.evaluator_coefficients
        )
        if self.leaf_dump_sink is not None:
            self._maybe_dump_leaf(search_state, [], score=score)
        return score

    def _score_with_history(self, state: Dict[str, Any], history: Sequence[RecordedAction],
                            *, collect_leaf: bool = True) -> float:
        scorer = getattr(self, 'preference_scorer', None)
        if scorer is None:
            if not collect_leaf:
                # Legacy scorers also support beam comparisons; do not let
                # their implicit leaf collector export speculative probes.
                if state.get('success') is False:
                    return float('-inf')
                return evaluate_leaf(state, self._root_summary, self.leaf_score_mode,
                                     self.evaluator_coefficients)
            return self._evaluate_leaf_cached(state)
        if self._preference_root is None:
            raise RuntimeError('Preference scoring root was not initialized')
        self._audit_increment('leaf_score_requests')
        relative_history = history[self._scoring_history_offset:]
        state_hash = hash_search_state(state)
        potion_uses = sum(row.action == 'use_potion' for row in relative_history)
        # Keep speculative ranking separate so its cache hit cannot suppress
        # the eventual candidate's training record.
        key = (state_hash, potion_uses, collect_leaf)
        self._ensure_coverage_sets()
        self._coverage_sets["leaf_states"].add(f"{state_hash}:potion_uses={potion_uses}")
        if key in self.eval_cache:
            self.timing['eval_cache_hits'] += 1
            return self.eval_cache[key]
        score = scorer.score(self._preference_root, state, history_trace(relative_history))
        if collect_leaf and self.leaf_dump_sink is not None:
            self._maybe_dump_leaf(state, relative_history, score=score)
        self.eval_cache[key] = score
        self._audit_increment('unique_leaf_scores')
        return score

    def _explain_result(self, result: SearchResult, history: Sequence[RecordedAction]) -> Dict[str, Any]:
        if self.preference_scorer:
            trace = history_trace(history[self._scoring_history_offset:]) + [{'action': {'action_type': action.action_type}, 'before': {}}
                                               for action in result.sequence]
            return self.preference_scorer.explain(self._preference_root, result.leaf_state, trace)
        return explain_leaf_score(result.leaf_state, self._root_summary, self.leaf_score_mode,
                                  self.evaluator_coefficients)

    def _root_action_bonus(
        self,
        parent_state: Dict[str, Any],
        action_info: Dict[str, Any],
        child: SearchResult,
        top_base_score: float,
    ) -> float:
        return 0.0

    def _root_adjustments(
        self,
        parent_state: Dict[str, Any],
        action_info: Dict[str, Any],
        child: SearchResult,
        top_base_score: float,
    ) -> Dict[str, Any]:
        ranker_bonus = self._root_action_bonus(parent_state, action_info, child, top_base_score)
        residual_bonus = 0.0
        residual_features: Dict[str, float] = {}
        total = float(ranker_bonus) + float(residual_bonus)
        return {
            "action_bonus": float(ranker_bonus),
            "root_action_residual": float(residual_bonus),
            "root_action_residual_features": residual_features,
            "root_adjustment": total,
        }

    @staticmethod
    def _with_root_adjusted_score(child: SearchResult, action_info: Dict[str, Any]) -> SearchResult:
        adjusted = float(action_info.get("adjusted_score", child.score))
        base = float(action_info.get("base_score", child.score))
        bonus = float(action_info.get("action_bonus", 0.0))
        residual = float(action_info.get("root_action_residual", 0.0))
        total_adjustment = float(action_info.get("root_adjustment", bonus + residual))
        return SearchResult(
            score=adjusted,
            sequence=list(child.sequence),
            leaf_state=child.leaf_state,
            state_hashes_after_actions=list(child.state_hashes_after_actions),
            state_keys_after_actions=copy.deepcopy(child.state_keys_after_actions),
            stats={
                **child.stats,
                "root_base_score": base,
                "root_action_bonus": bonus,
                "root_action_residual": residual,
                "root_adjustment": total_adjustment,
                "root_adjusted_score": adjusted,
            },
            root_candidates=child.root_candidates,
        )

    def _is_unresolved_player_leaf(self, search_state: Dict[str, Any]) -> bool:
        """A leaf that stops mid-turn: it is the player's turn, enemies are still
        alive, and the enemy intent for this turn has NOT been resolved yet.

        Scoring such a state directly is the core search-semantics bug: the leaf
        looks safe (incoming/unblocked damage reads 0 because the enemy has not
        acted), so a "stop playing cards" leaf outscores the only correct move
        (end_turn, which the engine resolves to the real, often lethal, damage).
        Comparing an unresolved leaf against a resolved end_turn leaf in the same
        max() is apples-to-oranges. We force every such leaf through one engine
        enemy turn so all compared leaves are post-resolution."""
        if search_state.get("terminal_decision"):
            return False
        combat = search_state.get("combat") or {}
        if not combat.get("is_player_turn"):
            return False
        enemies = combat.get("enemies") or []
        return any(float(e.get("hp") or 0) > 0 for e in enemies)

    def _settle_leaf_state(
        self, search_state: Dict[str, Any], history: Optional[Sequence[RecordedAction]]
    ) -> Tuple[Dict[str, Any], bool]:
        """Never substitute an unresolved state when enemy-turn resolution fails."""
        terminal = search_state.get('terminal_decision')
        if terminal:
            if terminal in {'card_reward', 'treasure', 'map_select', 'rest_site', 'shop',
                            'victory', 'defeat', 'game_over'} and search_state.get('success') is not False:
                if terminal not in {'defeat', 'game_over'} and float(
                        search_state.get('terminal_surviving_enemy_hp') or 0) > 0:
                    return self._settlement_failure('Victory still reports living enemies'), False
                return {**search_state, 'leaf_settlement': {'phase': 'combat_terminal'}}, False
            return self._settlement_failure(f'Unsupported leaf terminal: {terminal}'), False
        if history is None or not self._is_unresolved_player_leaf(search_state):
            return self._settlement_failure('Leaf has no resolvable player-turn boundary'), False
        end_turn_history = list(history) + [RecordedAction("end_turn", {})]
        try:
            resolved_result = self.combat_to_state(end_turn_history)
            resolved_state = self._extract_search_state(resolved_result)
        except Exception as exc:
            return self._settlement_failure(str(exc)), False
        checked = self._validate_settled_transition(search_state, resolved_state)
        return checked, checked.get('success') is not False

    def _settlement_failure(self, error: str) -> Dict[str, Any]:
        self._audit_increment('failed_settlements')
        return {**self._build_failed_search_state(error),
                'leaf_settlement': {'phase': 'failed', 'reason': error}}

    def _validate_settled_transition(self, before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
        if after.get('success') is not True:
            return self._settlement_failure(str(after.get('error') or after.get('message') or 'Resolution failed'))
        terminal = after.get('terminal_decision')
        if terminal:
            return self._settle_leaf_state(after, [])[0]
        previous = before.get('combat') or {}
        current = after.get('combat') or {}
        old_turn = previous.get('turn_number')
        new_turn = current.get('turn_number')
        if old_turn is None or new_turn is None:
            old_turn, new_turn = previous.get('round_number'), current.get('round_number')
        if (current.get('is_player_turn') is not True or type(old_turn) is not int
                or type(new_turn) is not int or new_turn <= old_turn):
            return self._settlement_failure('end_turn did not reach a later player-turn boundary')
        return {**after, 'leaf_settlement': {'phase': 'post_enemy_turn',
                                           'from_turn': old_turn, 'to_turn': new_turn}}

    def _settled_leaf_result(self, search_state: Dict[str, Any],
                             history: Sequence[RecordedAction]) -> SearchResult:
        leaf, appended = self._settle_leaf_state(search_state, history)
        if appended:
            self._audit_increment('completed_turn_lines')
        return SearchResult(
            score=self._score_with_history(leaf, history),
            sequence=[SearchAction('end_turn')] if appended else [], leaf_state=leaf,
            state_hashes_after_actions=[hash_search_state_for_plan_reuse(leaf)] if appended else [],
            state_keys_after_actions=[canonicalize_search_state_for_plan_reuse(leaf)] if appended else [],
            stats={
                'nodes': 1,
                'leaf_settlement': leaf.get('leaf_settlement'),
                'semantic_reuse_safe': self._semantic_transition_preserves_pile_order(
                    search_state, leaf
                ),
            })

    def _resolve_leaf_state(
        self, search_state: Dict[str, Any], history: Optional[Sequence[RecordedAction]]
    ) -> Dict[str, Any]:
        return self._settle_leaf_state(search_state, history)[0]

    def _build_root_candidates(
        self, paired_children: List[Tuple[Dict[str, Any], "SearchResult"]]
    ) -> List[Dict[str, Any]]:
        """Top-k root candidate actions for decision review (not for play).

        Each entry: the root action that was tried, the score the search gave it,
        and the full line (root action + the child's best continuation) it leads
        to. Ranked best-first. Uses a bounded heap for positive k and keeps all
        entries for negative k. The CHOSEN action is whichever ranks
        first here, so the reviewer sees the winner alongside the alternatives it
        beat.
        """
        import heapq

        k = self.capture_root_topk
        scored = []
        for i, (action_info, child) in enumerate(paired_children):
            action = action_info["action"]
            eff = float(action_info.get("effect_score") or 0.0)
            score = float(action_info.get("adjusted_score", child.score))
            base_score = float(action_info.get("base_score", child.score))
            bonus = float(action_info.get("action_bonus", 0.0))
            residual = float(action_info.get("root_action_residual", 0.0))
            total_adjustment = float(action_info.get("root_adjustment", bonus + residual))
            hard_rule_rank = int(action_info.get('hard_rule_rank') or 0)
            # i breaks ties deterministically so heap comparison never touches
            # the (unorderable) dict payload.
            scored.append((
                hard_rule_rank, score, eff, i, action, child, base_score, bonus,
                residual, total_adjustment, action_info,
            ))
        topk = (
            sorted(scored, key=lambda t: (t[0], t[1], t[2]), reverse=True)
            if k < 0 else
            heapq.nlargest(k, scored, key=lambda t: (t[0], t[1], t[2]))
        )

        def _act(a: SearchAction) -> Dict[str, Any]:
            return {
                "action_type": a.action_type,
                "card_index": a.card_index,
                "target_index": a.target_index,
                "metadata": a.metadata,
            }

        out: List[Dict[str, Any]] = []
        for rank, (
            hard_rule_rank, score, eff, _i, action, child, base_score, bonus,
            residual, total_adjustment, action_info,
        ) in enumerate(topk):
            line = [_act(a) for a in (child.sequence or [])] or [_act(action)]
            try:
                residual_feats = boss_residual_features(child.leaf_state, self._root_summary or {})
            except Exception:
                residual_feats = {}
            out.append({
                "rank": rank,
                "score": score if math.isfinite(score) else None,
                "base_score": base_score if math.isfinite(base_score) else None,
                "comparable": self._is_comparable_result(child),
                "leaf_settlement": child.leaf_state.get('leaf_settlement'),
                "action_bonus": bonus,
                "root_action_residual": residual,
                "root_adjustment": total_adjustment,
                "root_action_residual_features": action_info.get("root_action_residual_features") or {},
                "effect_score": eff,
                "hard_rule_rank": hard_rule_rank,
                "lethal_class": action_info.get('lethal_class', 'none'),
                "dominated_by": action_info.get('dominated_by'),
                "dominance_reasons": action_info.get('dominance_reasons') or [],
                "leaf_residual_features": residual_feats,
                "action": _act(action),
                "line": line,
            })
        return out

    def _maybe_dump_leaf(
        self,
        search_state: Dict[str, Any],
        history: Sequence[RecordedAction],
        score: Optional[float] = None,
    ) -> None:
        # Simulated draws are permitted. Export unordered piles and a separate
        # scoring root; the controller's legacy decision projection stays intact.
        with self._leaf_dump_lock:
            self._leaf_dump_counter += 1
            if self._leaf_dump_counter % self.leaf_dump_rate != 0:
                return
        try:
            relative_history = list(history)
            if self.preference_scorer is not None:
                features = self.preference_scorer.features(
                    self._preference_root or {},
                    search_state,
                    history_trace(relative_history),
                )
            else:
                features = extract_leaf_features(search_state, self._root_summary or {})
        except Exception:
            return

        from controller.combat_observation import project_model_state, MODEL_INFORMATION_POLICY

        combat = search_state.get("combat") or {}
        player = combat.get("player") or {}
        enemies = combat.get("enemies") or []

        def _action_record(row: RecordedAction) -> Dict[str, Any]:
            return {"action_type": row.action, "args": copy.deepcopy(row.args)}

        root_state = self._preference_root or {}
        root_id = hash_search_state_for_plan_reuse(root_state)
        settlement = search_state.get("leaf_settlement") or {}
        root_action = _action_record(relative_history[0]) if relative_history else None

        sample = {
            "schema": "sts2.combat_search.leaf.v3",
            "record_type": "leaf",
            "root_id": root_id,
            "root_action": root_action,
            "action_sequence": [_action_record(row) for row in relative_history],
            "settlement_action_appended": settlement.get("phase") == "post_enemy_turn" and (
                not relative_history or relative_history[-1].action != "end_turn"
            ),
            "leaf_settlement": copy.deepcopy(settlement),
            "leaf_visible_state": project_model_state(search_state),
            "root_scoring_state": project_model_state(root_state),
            "features": features,
            "round": combat.get("round_number"),
            "turn": combat.get("turn_number"),
            "player_hp": player.get("hp"),
            "player_max_hp": player.get("max_hp"),
            "enemy_ids": [e.get("monster_id") for e in enemies],
            "enemy_hp": [e.get("hp") for e in enemies],
            "hand_cards": [
                {"id": c.get("card_id"), "up": c.get("upgrade")}
                for c in (combat.get("hand") or []) if isinstance(c, dict)
            ],
            "score_mode": self.score_mode,
            "score": score,
            "information_boundary": MODEL_INFORMATION_POLICY,
            "search_context": {
                "chance_depth": self.chance_depth,
                "beam_ranking_policy": self.timing.get('beam_ranking_policy'),
                "root_snapshot_id": self.root_snapshot_id,
                "scorer": getattr(getattr(self, "preference_scorer", None), "identity", None),
            },
        }
        try:
            self.leaf_dump_sink(sample)
        except Exception:
            pass

    def _evaluate_enemy_chance_node(
        self,
        parent_state: Dict[str, Any],
        deterministic_child_state: Dict[str, Any],
        history_after_end_turn: Sequence[RecordedAction],
        next_action_budget: int,
        next_chance_depth: int,
    ) -> SearchResult:
        # Use the official engine's actual post-enemy-turn successor for the current
        # replayed trajectory. This guarantees the first enemy turn is fully resolved
        # from the true pre-end-turn combat state instead of a coarse template lookup.
        if (next_action_budget > 0 and next_chance_depth > 0
                and not deterministic_child_state.get('terminal_decision')):
            next_pre_chance_budget = self._initial_pre_chance_budget(next_action_budget, next_chance_depth)
            return self._search_from_state(
                deterministic_child_state,
                history_after_end_turn,
                next_action_budget,
                next_chance_depth,
                next_pre_chance_budget,
            )
        score = self._score_with_history(deterministic_child_state, history_after_end_turn)
        if self.score_mode == "balanced_future":
            parent_combat = parent_state.get("combat") or {}
            parent_player = parent_combat.get("player") or {}
            parent_enemies = parent_combat.get("enemies") or []
            incoming = 0.0
            for enemy in parent_enemies:
                intent = enemy.get("intent") or {}
                if not intent_deals_damage(intent):
                    continue
                incoming += intent_total_damage(intent)
            unblocked = max(0.0, incoming - float(parent_player.get("block") or 0.0))
            future_weight = max(0.0, 1.5 - 0.12 * unblocked)
            score += future_weight * expected_visible_draw_pool_strength(parent_state)
        return SearchResult(
            score=score,
            sequence=[],
            leaf_state=deterministic_child_state,
            stats={
                'nodes': 1,
                'leaf_settlement': deterministic_child_state.get('leaf_settlement'),
                'semantic_reuse_safe': True,
            },
        )

    @staticmethod
    def _replace_enemy_intents(
        deterministic_child_state: Dict[str, Any],
        next_state: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        branched = copy.deepcopy(deterministic_child_state)
        combat = branched.get("combat") or {}
        enemies = combat.get("enemies") or []
        for enemy, branch_enemy in zip(enemies, next_state):
            enemy["intent"] = {
                "intent_types": list(branch_enemy.get("intent_types") or []),
                "total_damage": branch_enemy.get("total_damage"),
                "display_damage": branch_enemy.get("display_damage"),
                "hits": branch_enemy.get("hits"),
            }
        return branched

    @staticmethod
    def _replace_enemy_intents_from_parent(
        parent_state: Dict[str, Any],
        child_state: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Compatibility for offline callers: engine successor intents are authoritative."""
        return child_state

    def _record_branch_timing(self, action: SearchAction, started: float, result: SearchResult) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.timing["branch_ms_samples"].append(
            {
                "action_type": action.action_type,
                "card_index": action.card_index,
                "target_index": action.target_index,
                "card_id": (action.metadata or {}).get("card_id"),
                "elapsed_ms": elapsed_ms,
                "score": result.score,
                "nodes": result.stats.get("nodes", 1),
            }
        )

    @staticmethod
    def _combat_summary(search_state: Dict[str, Any]) -> Dict[str, Any]:
        combat = search_state.get("combat") or {}
        player = combat.get("player") or {}
        enemies = combat.get("enemies") or []

        def freeze_power(power: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "id": power.get("id"),
                "amount": power.get("amount"),
                "extra": power.get("extra"),
            }

        def freeze_enemy(enemy: Dict[str, Any]) -> Dict[str, Any]:
            intent = enemy.get("intent") or {}
            return {
                "monster_id": enemy.get("monster_id"),
                "hp": int(enemy.get("hp") or 0),
                "max_hp": int(enemy.get("max_hp") or 0),
                "block": int(enemy.get("block") or 0),
                "powers": [freeze_power(p) for p in (enemy.get("powers") or []) if isinstance(p, dict)],
                "intent": {
                    "intent_types": list(intent.get("intent_types") or []),
                    "total_damage": intent.get("total_damage"),
                    "display_damage": intent.get("display_damage"),
                    "hits": intent.get("hits"),
                },
            }

        return {
            "player": {
                "hp": int(player.get("hp") or 0),
                "max_hp": int(player.get("max_hp") or 0),
            },
            "enemies": [freeze_enemy(enemy) for enemy in enemies if isinstance(enemy, dict)],
        }
