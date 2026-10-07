"""Profile one authoritative combat snapshot without changing search semantics."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from cli.sts2_cli_adapter import CliConfig
from controller.search.combat_search import CombatSearcher, CombatSpec, CombatWorkerPool
from controller.search.state_cache import (
    hash_search_state,
    hash_search_state_for_plan_reuse,
)


def _action_row(action: Any) -> dict[str, Any]:
    return {
        "action_type": getattr(action, "action_type", None),
        "card_index": getattr(action, "card_index", None),
        "target_index": getattr(action, "target_index", None),
        "metadata": getattr(action, "metadata", None),
    }


def _coverage_digests(searcher: CombatSearcher) -> dict[str, dict[str, Any]]:
    digests: dict[str, dict[str, Any]] = {}
    for key, values in searcher._coverage_snapshot().items():
        encoded = json.dumps(
            values,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digests[key] = {
            "count": len(values),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    return digests


def run(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path.cwd().resolve()
    snapshot_path = args.snapshot.resolve()
    capture_metadata = None
    if snapshot_path.is_dir():
        capture_metadata = json.loads((snapshot_path / 'metadata.json').read_text(encoding='utf-8'))
        if (capture_metadata.get('schema') != 'sts2.combat_snapshot.v2'
                or capture_metadata.get('status') != 'RESTORE_VERIFIED') and not args.allow_unverified:
            raise ValueError('Snapshot has no independent restore validation; use --allow-unverified for diagnosis')
        if capture_metadata.get('compatibility') and not args.allow_unverified:
            from controller.combat_snapshot import snapshot_compatibility
            current = snapshot_compatibility(CliConfig(repo_root=repo, dll_relpath=args.dll))
            if capture_metadata['compatibility'] != current:
                raise ValueError('Snapshot validation is stale for this runtime; revalidate before profiling')
        snapshot_path = snapshot_path / 'search_snapshot.json'
    raw = snapshot_path.read_bytes().decode('utf-8')
    snapshot_sha256 = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    if capture_metadata and snapshot_sha256 != capture_metadata.get('search_sha256'):
        raise ValueError('Snapshot digest differs from capture metadata')
    snapshot = json.loads(raw)
    if not isinstance(snapshot, dict):
        raise ValueError("Snapshot must contain one JSON object")

    cfg = CliConfig(repo_root=repo, dll_relpath=args.dll)
    pool = None if args.no_worker_pool else CombatWorkerPool(cfg)
    prewarm_started = time.perf_counter()
    if pool is not None:
        pool.prewarm(max(1, args.workers if args.parallel else 1))
    prewarm_ms = (time.perf_counter() - prewarm_started) * 1000.0
    spec = CombatSpec(
        character=args.character,
        encounter="ImportedSnapshot",
        seed=str(snapshot.get("Seed") or snapshot_path.stem),
        ascension=int(snapshot.get("AscensionLevel") or 0),
        lang=args.lang,
    )
    searcher = CombatSearcher(
        cfg,
        spec,
        parallel_top_level=args.parallel,
        max_workers=args.workers,
        parallel_frontier=args.parallel_frontier,
        score_mode=args.score_mode,
        reuse_cli_processes=True,
        root_snapshot_id=snapshot_path.stem,
        root_snapshot_json=raw,
        max_search_ms=args.max_search_ms,
        expand_potions=args.expand_potions,
        worker_pool=pool,
        search_mode=args.search_mode,
        beam_width=args.beam_width,
        beam_dominance=args.dominance,
    )
    started = time.perf_counter()
    try:
        result = searcher.search_from_history(
            [], depth=args.depth, chance_depth=args.chance_depth
        )
        wall_ms = (time.perf_counter() - started) * 1000.0
        report = {
            "schema": "sts2.search_snapshot_profile.v2",
            "snapshot": str(snapshot_path),
            "snapshot_sha256": snapshot_sha256,
            "capture_validation": capture_metadata.get('validation') if capture_metadata else None,
            "coverage_scope": "Discovered states/edges only; not a full-tree coverage denominator",
            "runtime_sha256": hashlib.sha256((repo / args.dll).read_bytes()).hexdigest(),
            "settings": {
                "search_mode": args.search_mode,
                "beam_width": args.beam_width,
                "dominance": args.dominance,
                "dominance_scope": "experimental metric heuristic; not proven strict dominance",
                "depth": args.depth,
                "chance_depth": args.chance_depth,
                "max_search_ms": args.max_search_ms,
                "parallel_top_level": args.parallel,
                "parallel_frontier": args.parallel_frontier,
                "workers": args.workers,
                "score_mode": args.score_mode,
                "expand_potions": args.expand_potions,
                "worker_pool": pool is not None,
                "strict_dag_enabled": searcher.strict_dag_enabled,
            },
            "prewarm_ms": round(prewarm_ms, 3),
            "wall_ms": round(wall_ms, 3),
            "score": result.score,
            "sequence": [_action_row(action) for action in result.sequence],
            "leaf_state_hash": hash_search_state(result.leaf_state),
            "leaf_plan_reuse_hash": hash_search_state_for_plan_reuse(result.leaf_state),
            "leaf_engine_state_fingerprint": str(
                result.leaf_state.get("engine_state_fingerprint") or ""
            ),
            "leaf_engine_semantic_state_fingerprint": str(
                result.leaf_state.get("engine_semantic_state_fingerprint") or ""
            ),
            "result_stats": result.stats,
            "timing": searcher.timing_summary(),
            "worker_pool": pool.stats() if pool is not None else None,
            "coverage_digests": _coverage_digests(searcher),
        }
        if args.include_coverage_keys:
            report["coverage_keys"] = searcher._coverage_snapshot()
    finally:
        searcher.close()
        if pool is not None:
            pool.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument(
        "--dll",
        type=Path,
        default=Path("third_party/sts2-cli/src/Sts2Headless/bin/Release/net9.0/Sts2Headless.dll"),
    )
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument("--lang", default="en")
    parser.add_argument("--search-mode", choices=['dfs', 'beam'], default='dfs')
    parser.add_argument("--beam-width", type=int, default=8)
    parser.add_argument("--dominance", action=argparse.BooleanOptionalAction, default=False,
                        help='Experimental metric pruning, NOT strict dominance (default: disabled)')
    parser.add_argument("--allow-unverified", action='store_true',
                        help='Allow an artifact directory without independent restore PASS for diagnosis')
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--chance-depth", type=int, default=1)
    parser.add_argument("--max-search-ms", type=float, default=0.0)
    parser.add_argument("--score-mode", default="preference")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--parallel", action="store_true")
    parser.add_argument("--parallel-frontier", action="store_true")
    parser.add_argument("--no-worker-pool", action="store_true")
    parser.add_argument("--expand-potions", action="store_true")
    parser.add_argument(
        "--include-coverage-keys",
        action="store_true",
        help="Include exact state/edge coverage keys for parity diagnosis.",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.beam_width < 2 or args.depth < 1 or args.max_search_ms < 0:
        parser.error('Require beam width >= 2, depth >= 1 and budget >= 0')
    if args.dominance and args.search_mode != 'beam':
        parser.error('--dominance requires --search-mode beam')
    report = run(args)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")


if __name__ == "__main__":
    main()
