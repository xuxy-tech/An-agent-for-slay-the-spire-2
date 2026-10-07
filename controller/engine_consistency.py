"""Current-version engine consistency gate for snapshot-backed runs."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from controller.combat_scoring import active_model
from controller.combat_snapshot import revalidate_saved_combat_snapshot, snapshot_compatibility, snapshot_index
from controller.sandbox_features import FEATURE_VERSION
from cli.sts2_cli_adapter import CliConfig

SCHEMA = "sts2.engine_consistency.v1"


def gate_path(log_root: Path) -> Path:
    return log_root / "engine_consistency.json"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def load_gate(log_root: Path) -> dict[str, Any]:
    path = gate_path(log_root)
    if not path.is_file():
        return {"schema": SCHEMA, "status": "NOT_RUN"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"schema": SCHEMA, "status": "INVALID"}
    except (OSError, json.JSONDecodeError):
        return {"schema": SCHEMA, "status": "INVALID"}


def run_check(root: Path, log_root: Path, snapshot_ids: list[str] | None = None) -> dict[str, Any]:
    """Revalidate the current snapshot set and persist a fail-closed report.

    Capture artifacts already contain the authoritative client observation and
    a restore-verified headless state. Revalidation reruns the independent
    headless restore, while the stored capture metadata binds it to the real
    client state. No historical scorer/search result is required.
    """
    cfg = CliConfig(repo_root=root)
    compatibility = snapshot_compatibility(cfg)
    rows = [row for row in snapshot_index(log_root)
            if row.get("status") == "RESTORE_VERIFIED" and row.get("reusable")
            and row.get("compatibility") == compatibility]
    if snapshot_ids is not None:
        wanted = set(snapshot_ids)
        rows = [row for row in rows if row.get("snapshot_id") in wanted]
    failures: list[dict[str, Any]] = []
    passed: list[str] = []
    for row in rows:
        artifact = Path(str(row.get("artifact_dir") or ""))
        try:
            result = revalidate_saved_combat_snapshot(artifact, cli_config=cfg)
            if result.get("status") != "RESTORE_VERIFIED" or not result.get("reusable"):
                raise ValueError(str(result.get("error") or result.get("status") or "revalidation_failed"))
            passed.append(str(row.get("snapshot_id")))
        except Exception as exc:
            failures.append({"snapshot_id": row.get("snapshot_id"), "artifact_dir": str(artifact),
                             "error": str(exc)})
    model = active_model()
    report = {
        "schema": SCHEMA,
        "checked_at_utc": time.time(),
        "status": "PASS" if passed and not failures else "FAIL",
        "feature_version": FEATURE_VERSION,
        "scoring_model_digest": _digest(model),
        "compatibility": compatibility,
        "snapshot_count": len(rows),
        "passed": passed,
        "failures": failures,
    }
    path = gate_path(log_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


def require_pass(log_root: Path, compatibility: dict[str, Any]) -> dict[str, Any]:
    report = load_gate(log_root)
    if report.get("schema") != SCHEMA or report.get("status") != "PASS":
        raise ValueError("双端引擎一致性检验未通过")
    if report.get("feature_version") != FEATURE_VERSION or report.get("compatibility") != compatibility:
        raise ValueError("一致性检验与当前评分或引擎版本不匹配，请重新检验")
    return report
