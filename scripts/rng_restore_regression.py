from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.rng_parity import compare_rng_snapshots, flatten_rng_streams


def _digest(snapshot: dict[str, Any]) -> str | None:
    return snapshot.get("digest_sha256")


def _assert_same(label: str, expected: dict[str, Any], actual: dict[str, Any]) -> None:
    comparison = compare_rng_snapshots(expected, actual)
    if not comparison.passed:
        raise RuntimeError(f"{label}: RNG mismatch: {comparison.differences}")
    if _digest(expected) != _digest(actual):
        raise RuntimeError(
            f"{label}: canonical digest mismatch despite equal fields: "
            f"{_digest(expected)} != {_digest(actual)}"
        )


def _replay_snapshot(path: Path) -> str:
    document = json.loads(path.read_text(encoding="utf-8"))
    snapshot_json = document.get("snapshot_json")
    if not isinstance(snapshot_json, str) or not snapshot_json:
        raise RuntimeError(f"Replay has no snapshot_json: {path}")
    return snapshot_json


def _perturb_rng(cli: Sts2CliAdapter, snapshot: dict[str, Any]) -> dict[str, Any]:
    streams = flatten_rng_streams(snapshot)
    run_names = [name.removeprefix("run.") for name in streams if name.startswith("run.")]
    if not run_names:
        raise RuntimeError("Cannot perturb RNG: no run stream exists")
    stream_name = sorted(run_names)[0]
    current_seed = int(streams[f"run.{stream_name}"]["seed"])
    replacement = (current_seed ^ 0x5A17C9E3) & 0x7FFFFFFF
    if replacement == current_seed:
        replacement = (replacement + 1) & 0x7FFFFFFF
    changed = cli.reseed_rng_stream({stream_name: replacement})
    if changed.get("success") is not True or stream_name not in changed.get("run_streams_changed", []):
        raise RuntimeError(f"RNG perturbation did not affect {stream_name}: {changed}")
    after = cli.get_rng_snapshot()
    if compare_rng_snapshots(snapshot, after).passed:
        raise RuntimeError(f"RNG perturbation left snapshot unchanged for {stream_name}")
    return {"stream": stream_name, "replacement_seed": replacement}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify deterministic RNG restoration in one persistent headless process"
    )
    parser.add_argument("--save", type=Path)
    parser.add_argument("--resume-room", action="store_true")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("replays", nargs="*", type=Path)
    args = parser.parse_args()
    if args.repeat < 2:
        parser.error("--repeat must be at least 2")
    if args.save is None and not args.replays:
        parser.error("provide --save and/or at least one replay.json")

    report: dict[str, Any] = {
        "status": "PASS",
        "persistent_process": True,
        "save": None,
        "replays": [],
    }
    cli = Sts2CliAdapter(CliConfig(ROOT))
    try:
        cli.start()
        if args.save is not None:
            expected: dict[str, Any] | None = None
            digests: list[str | None] = []
            decisions: list[Any] = []
            for index in range(args.repeat):
                loaded = cli.load_save(str(args.save.resolve()), resume_room=args.resume_room)
                if loaded.get("type") == "error" or loaded.get("success") is False:
                    raise RuntimeError(f"save load {index + 1} failed: {loaded}")
                current = cli.get_rng_snapshot()
                if expected is None:
                    expected = current
                else:
                    _assert_same(f"save reload {index + 1}", expected, current)
                digests.append(_digest(current))
                decisions.append(loaded.get("decision"))
            report["save"] = {
                "path": str(args.save.resolve()),
                "repeats": args.repeat,
                "digests": digests,
                "decision_types": [
                    decision.get("type") if isinstance(decision, dict) else None
                    for decision in decisions
                ],
            }

        for replay_index, replay_path in enumerate(args.replays):
            snapshot_id = f"rng_restore_{replay_index}"
            imported = cli.import_combat_snapshot(_replay_snapshot(replay_path), snapshot_id)
            if imported.get("success") is not True:
                raise RuntimeError(f"snapshot import failed for {replay_path}: {imported}")
            expected = None
            restores: list[dict[str, Any]] = []
            for index in range(args.repeat):
                restored = cli.restore_combat_snapshot(snapshot_id, compact=True)
                if restored.get("type") == "error" or restored.get("success") is False:
                    raise RuntimeError(f"restore {index + 1} failed for {replay_path}: {restored}")
                current = cli.get_rng_snapshot()
                if expected is None:
                    expected = current
                else:
                    _assert_same(f"{replay_path} restore {index + 1}", expected, current)
                restore_record: dict[str, Any] = {
                    "iteration": index + 1,
                    "mode": restored.get("restore_mode"),
                    "digest": _digest(current),
                }
                if index + 1 < args.repeat:
                    restore_record["perturbation"] = _perturb_rng(cli, current)
                restores.append(restore_record)
            report["replays"].append({
                "path": str(replay_path.resolve()),
                "restores": restores,
            })
    except Exception as exc:
        report["status"] = "FAIL"
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    finally:
        cli.stop()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
