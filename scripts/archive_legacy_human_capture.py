from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


SESSION_IDS = (
    "human_43a55c407f7e414d9e14aad401629bce",
    "human_57092b8a4c4744469c83a4c9959e4b9a",
    "human_8a5560ba1f6249c9b3ade65b3c686d05",
    "human_df43fd69fbd1435799949ed7407ee15b",
)
EXPERIMENTS = (
    "linear_scorer_probe_20260920_v2",
    "human_leaf_join_20260920",
)
ARCHIVE_NAME = "20260920_pre_authoritative_snapshot_v3"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    human_root = repo / "data" / "human_play"
    archive = human_root / "archive" / ARCHIVE_NAME
    if archive.exists():
        raise SystemExit(f"Archive already exists: {archive}")

    raw_destination = archive / "raw"
    experiments_destination = archive / "experiments"
    raw_destination.mkdir(parents=True)
    experiments_destination.mkdir(parents=True)

    for session_id in SESSION_IDS:
        source = human_root / "raw" / session_id
        if not (source / "events.jsonl").is_file():
            raise SystemExit(f"Missing legacy session: {source}")
        shutil.copytree(source, raw_destination / session_id)

    for experiment in EXPERIMENTS:
        source = human_root / "experiments" / experiment
        if not source.is_dir():
            raise SystemExit(f"Missing legacy experiment: {source}")
        shutil.copytree(source, experiments_destination / experiment)

    probe = json.loads((
        experiments_destination / "linear_scorer_probe_20260920_v2" / "report.json"
    ).read_text(encoding="utf-8"))
    join = json.loads((
        experiments_destination / "human_leaf_join_20260920" / "report.json"
    ).read_text(encoding="utf-8"))
    sessions = probe["source"]["sessions"]
    generic = next(row for row in probe["variants"] if row["variant"] == "generic_numeric")
    identity = next(
        row for row in probe["variants"] if row["variant"] == "generic_plus_card_identity"
    )
    totals = {
        "sessions": len(sessions),
        "decisions": sum(row["audit"]["decisions"] for row in sessions),
        "settled": sum(row["audit"]["settlements"].get("settled", 0) for row in sessions),
        "ambiguous_overlap": sum(
            row["audit"]["settlements"].get("ambiguous_overlap", 0) for row in sessions
        ),
        "usable_card_or_end_turn_examples": probe["source"]["examples"],
        "play_card_examples": sum(row["audit"]["actions"].get("play_card", 0) for row in sessions),
        "end_turn_examples": sum(row["audit"]["actions"].get("end_turn", 0) for row in sessions),
    }
    weighted_generic = generic["weighted"]
    weighted_identity = identity["weighted"]
    conclusions = {
        "supported": [
            "The v3 capture path recorded and audited real-player action sequences and observer transitions.",
            (
                f"The four sessions contain {totals['decisions']} decisions, including "
                f"{totals['settled']} settled and {totals['ambiguous_overlap']} ambiguous-overlap rows."
            ),
            (
                f"There are {totals['usable_card_or_end_turn_examples']} settled combat "
                f"play-card/end-turn examples usable for the historical action-ranking probe."
            ),
            (
                "The generic numeric linear action ranker exceeded the action-frequency baseline: "
                f"top-1 {weighted_generic['top1_accuracy']:.6f} versus "
                f"{weighted_generic['frequency_baseline_top1']:.6f}; pairwise "
                f"{weighted_generic['pairwise_accuracy']:.6f}."
            ),
            (
                "Adding card identity did not improve weighted top-1 accuracy "
                f"({weighted_identity['top1_accuracy']:.6f}) but increased pairwise accuracy "
                f"to {weighted_identity['pairwise_accuracy']:.6f}."
            ),
        ],
        "not_supported": [
            "The probe does not validate a leaf-state scoring function; it evaluates immediate action ranking.",
            "The historical sessions contain no authoritative restorable combat snapshots.",
            "No same-root headless counterfactual set exists for these sessions.",
            (
                f"The strict leaf join produced {join['matched_rows']} matched rows and "
                f"{join['fit_ready_rows']} fit-ready rows, so these sessions cannot train or validate "
                "the intended leaf evaluator."
            ),
            "Complete engine and RNG state cannot be reconstructed retrospectively from observer payloads.",
        ],
    }

    files = []
    for path in sorted(archive.rglob("*")):
        if path.is_file():
            files.append({
                "path": path.relative_to(archive).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            })
    manifest = {
        "schema": "sts2.human_capture.archive.v1",
        "archive_id": ARCHIVE_NAME,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_protocol": "2026-09-20-human-capture-v3",
        "reason": "Historical observer-only captures archived before authoritative combat snapshots became mandatory.",
        "source_sessions": list(SESSION_IDS),
        "source_experiments": list(EXPERIMENTS),
        "totals": totals,
        "conclusions": conclusions,
        "files": files,
        "source_data_preserved": True,
    }
    (archive / "archive_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    markdown = [
        "# Historical Human Capture Conclusions",
        "",
        "This archive preserves the four v3 observer-only sessions recorded before authoritative combat snapshots were required.",
        "The original directories under data/human_play/raw remain untouched.",
        "",
        "## Supported Conclusions",
        "",
        *[f"- {item}" for item in conclusions["supported"]],
        "",
        "## Unsupported Conclusions",
        "",
        *[f"- {item}" for item in conclusions["not_supported"]],
        "",
        "## Provenance",
        "",
        "See archive_manifest.json for source IDs, aggregate counts, and SHA-256 hashes of every copied file.",
    ]
    (archive / "CONCLUSIONS.md").write_text(
        "\n".join(markdown) + "\n", encoding="utf-8", newline="\n"
    )
    print(archive)


if __name__ == "__main__":
    main()
