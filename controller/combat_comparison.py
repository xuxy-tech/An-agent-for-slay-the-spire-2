"""Persistent model library and paired, fail-closed combat replay jobs."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cli.sts2_cli_adapter import CliConfig
from controller.combat_scoring import CombatScoring, active_model, stage_for_floor, validate_model
from controller.combat_snapshot import snapshot_compatibility, snapshot_index
from controller.combat_abilities import ability_settings
from controller.sandbox_features import FEATURES, FEATURE_VERSION, OBSERVATION_MODE, schema
from scripts.verify_combat_replay_gate import (
    _recorded_config, compare_history, compare_replays, load_snapshot, replay_once,
)
from controller.snapshot_evidence import read_evidence, read_original_report
from controller.engine_consistency import load_gate, require_pass


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _process_alive(pid: Any) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


class ModelLibrary:
    def __init__(self, root: Path):
        self.root = root
        self.saved = root / "data/scoring/user_models"
        self.trained = root / "data/human_play/models"
        self.names_path = self.saved / "names.json"

    def catalog(self) -> dict[str, Any]:
        names = _read(self.names_path) if self.names_path.is_file() else {}
        models = [{"id": "active", "name": "当前实战模型", "source": "active",
                   "model": active_model()}]
        for path in sorted(self.trained.glob("*.json"), reverse=True):
            try:
                model = _read(path)
                validate_model(model)
                model_id = "trained:" + path.stem
                models.append({"id": model_id, "name": names.get(model_id, path.stem),
                               "source": "trained", "model": model})
            except (OSError, ValueError, KeyError, TypeError):
                continue
        for path in sorted(self.saved.glob("*.json"), reverse=True):
            if path == self.names_path:
                continue
            try:
                entry = _read(path)
                validate_model(entry["model"])
                model_id = "saved:" + path.stem
                models.append({"id": model_id, "name": names.get(model_id, entry["name"]),
                               "source": "saved", "model": entry["model"]})
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return {"features": schema(), "models": models}

    def get(self, model_id: str) -> dict[str, Any]:
        for row in self.catalog()["models"]:
            if row["id"] == model_id:
                return row["model"]
        raise ValueError("Unknown or incompatible scoring model")

    def save(self, name: str, weights: dict[str, Any], ability_config: dict | None = None) -> dict[str, Any]:
        name = self._valid_name(name)
        if not isinstance(weights, dict) or set(weights) != {item.key for item in FEATURES}:
            raise ValueError("Weights do not match current scoring features")
        clean = {item.key: float(weights[item.key]) for item in FEATURES}
        if any(not math.isfinite(value) or value < 0 or value > 1000 for value in clean.values()):
            raise ValueError("Weights must be finite and between 0 and 1000")
        model = {"kind": "linear_preference", "feature_version": FEATURE_VERSION,
                 "observation_mode": OBSERVATION_MODE, "trained": False, "weights": clean,
                 "ability_config": ability_settings(ability_config)}
        validate_model(model)
        model_id = _hash(model)[:16]
        path = self.saved / (model_id + ".json")
        if not path.exists():
            _write(path, {"name": name, "created_at_utc": time.time(), "model": model})
        saved_id = "saved:" + model_id
        names = _read(self.names_path) if self.names_path.is_file() else {}
        return {"id": saved_id, "name": names.get(saved_id, _read(path)["name"]), "model": model}

    @staticmethod
    def _valid_name(name: str) -> str:
        name = str(name).strip()
        if not 1 <= len(name) <= 60 or any(ord(ch) < 32 for ch in name):
            raise ValueError("Model name must contain 1–60 printable characters")
        return name

    def rename(self, model_id: str, name: str) -> dict[str, str]:
        row = next((item for item in self.catalog()["models"] if item["id"] == model_id), None)
        if row is None or row["source"] not in {"trained", "saved"}:
            raise ValueError("Only trained or saved models may be renamed")
        name = self._valid_name(name)
        names = _read(self.names_path) if self.names_path.is_file() else {}
        names[model_id] = name
        _write(self.names_path, names)
        return {"id": model_id, "name": name}

    def delete(self, model_id: str) -> None:
        if not model_id.startswith("saved:"):
            raise ValueError("Only saved models may be deleted")
        suffix = model_id[6:]
        if len(suffix) != 16 or any(ch not in "0123456789abcdef" for ch in suffix):
            raise ValueError("Invalid model id")
        (self.saved / (suffix + ".json")).unlink()
        if self.names_path.is_file():
            names = _read(self.names_path)
            names.pop(model_id, None)
            _write(self.names_path, names)


def compare_outcomes(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    win_boundaries = {"combat_reward", "card_reward", "map_select", "victory"}
    loss_boundaries = {"game_over", "defeat"}
    if a.get("status") != "COMPLETED" or b.get("status") != "COMPLETED":
        return {"category": "invalid", "reason": "incomplete_replay"}
    ta, tb = a.get("terminal"), b.get("terminal")
    if ta not in win_boundaries | loss_boundaries or tb not in win_boundaries | loss_boundaries:
        return {"category": "invalid", "reason": "unknown_terminal"}
    aw, bw = ta in win_boundaries, tb in win_boundaries
    if aw != bw:
        category = "a_win" if aw else "b_win"
    elif not aw:
        category = "both_lost"
    else:
        ao, bo = a.get("outcome") or {}, b.get("outcome") or {}
        ah, bh = ao.get("hp"), bo.get("hp")
        ap, bp = ao.get("potions"), bo.get("potions")
        if not isinstance(ah, (int, float)) or not isinstance(bh, (int, float)) or not isinstance(ap, list) or not isinstance(bp, list):
            return {"category": "invalid", "reason": "missing_terminal_resources"}
        hp_delta, potion_delta = ah - bh, len(ap) - len(bp)
        if hp_delta == 0 and potion_delta == 0:
            category = "equal_resources"
        elif hp_delta >= 0 and potion_delta >= 0:
            category = "a_resources"
        elif hp_delta <= 0 and potion_delta <= 0:
            category = "b_resources"
        else:
            category = "resource_tradeoff"
    ah, bh = (a.get("outcome") or {}).get("hp"), (b.get("outcome") or {}).get("hp")
    return {"category": category, "hp_delta": ah - bh if isinstance(ah, (int, float)) and isinstance(bh, (int, float)) else None,
            "potion_delta": len((a.get("outcome") or {}).get("potions") or []) - len((b.get("outcome") or {}).get("potions") or [])}


def _settings(metadata: dict[str, Any]) -> dict[str, Any]:
    recorded, workers = _recorded_config(metadata)
    return {"depth": int(recorded.get("depth") or 8),
            "chance_depth": int(recorded.get("chance_depth") or 1),
            "max_search_ms": float(recorded.get("search_budget_ms") or 20000),
            "workers": workers or 4, "max_actions": 120}


def _anchor_model(metadata: dict[str, Any], models: list[dict[str, Any]]) -> dict[str, Any] | None:
    evidence = read_evidence(metadata)
    if evidence is not None:
        rows = evidence.get('actions') or []
        scorer = (((rows[0].get('decision_telemetry') or {}).get('score_explanation') or {}).get('scorer') or {}) if rows else {}
        digest = scorer.get('weights_sha256')
        floor = (metadata.get('context') or {}).get('floor', metadata.get('floor'))
        stage = stage_for_floor(int(floor) if floor is not None else None)
        frozen = evidence.get('model')
        if isinstance(frozen, dict):
            validate_model(frozen)
            if CombatScoring(stage, frozen).identity['weights_sha256'] == digest:
                return frozen
        return next((model for model in models
                     if CombatScoring(stage, model).identity['weights_sha256'] == digest), None)
    session = Path((metadata.get("context") or {}).get("session_dir") or "")
    report = read_original_report(metadata)
    if report is None:
        return None
    start = datetime.fromtimestamp(float(metadata["created_at_utc"]), tz=timezone.utc)
    rows = [row for row in (report.get("actions") or [])
            if str(row.get("decision") or "").startswith("combat.")
            and datetime.fromisoformat(row["timestamp_utc"]).astimezone(timezone.utc) > start]
    if not rows:
        return None
    scorer = (((rows[0].get("decision_telemetry") or {}).get("score_explanation") or {}).get("scorer") or {})
    digest = scorer.get("weights_sha256")
    floor = (metadata.get("context") or {}).get("floor", metadata.get("floor"))
    stage = stage_for_floor(int(floor) if floor is not None else None)
    report_model = (report.get('config') or {}).get('scoring_model')
    if isinstance(report_model, dict):
        try:
            validate_model(report_model)
            if CombatScoring(stage, report_model).identity['weights_sha256'] == digest:
                return report_model
        except (ValueError, KeyError, TypeError):
            pass
    frozen_path = session / 'scoring_model.json'
    if frozen_path.is_file():
        try:
            frozen = _read(frozen_path)
            validate_model(frozen)
            if CombatScoring(stage, frozen).identity['weights_sha256'] == digest:
                return frozen
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return next((model for model in models
                 if CombatScoring(stage, model).identity["weights_sha256"] == digest), None)


def _cache_key(root: Path, metadata: dict[str, Any], model: dict[str, Any], settings: dict[str, Any]) -> str:
    sources = {}
    for name in ("scripts/verify_combat_replay_gate.py", "controller/combat_comparison.py",
                 "controller/combat_step.py", "controller/combat_scoring.py",
                 "controller/preference_model.py", "controller/sandbox_features.py",
                 "controller/combat_abilities.py", "controller/combat_opportunity.py",
                 "controller/combat_observation.py",
                 "cli/sts2_cli_adapter.py",
                 "controller/search/combat_search.py", "controller/run_agent.py"):
        sources[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    return _hash({"snapshot": metadata["snapshot_id"], "restore": metadata["restore_sha256"],
                  "runtime": metadata["compatibility"], "model": model,
                  "settings": settings, "sources": sources})


def _model_replay(root: Path, cache_root: Path, metadata: dict[str, Any], restore: str,
                  cfg: CliConfig, model: dict[str, Any], settings: dict[str, Any],
                  cancel_path: Path) -> dict[str, Any]:
    key = _cache_key(root, metadata, model, settings)
    path = cache_root / (key + ".json")
    if path.is_file():
        return _read(path)
    cancelled = cancel_path.exists
    first = replay_once(metadata, restore, cfg, model=model, cancelled=cancelled, **settings)
    if cancelled():
        return {"aa": {"status": "CANCELLED"}, "first": first}
    second = replay_once(metadata, restore, cfg, model=model, cancelled=cancelled, **settings)
    aa = compare_replays(first, second)
    result = {"aa": aa, "first": first, "second": second}
    # A timeout or fallback can be load-dependent; never freeze a transient
    # failure into the replay cache.
    if not cancelled() and aa["status"] == "PASS":
        _write(path, result)
    return result


def _compare_one(root: Path, cache_root: Path, snapshot: Path,
                 a_model: dict[str, Any], b_model: dict[str, Any],
                 anchor_models: list[dict[str, Any]], cancel_path: Path) -> dict[str, Any]:
    cfg = CliConfig(repo_root=root)
    snapshot_id = snapshot.name
    if cancel_path.exists():
        return {"snapshot_id": snapshot_id, "status": "CANCELLED"}
    try:
        metadata, restore = load_snapshot(snapshot, cfg)
        settings = _settings(metadata)
        anchor_model = _anchor_model(metadata, anchor_models)
        if anchor_model is None:
            return {"snapshot_id": snapshot_id, "status": "EXCLUDED", "reason": "live_scorer_unavailable",
                    "context": metadata.get("context") or {}}
        anchor = _model_replay(root, cache_root, metadata, restore, cfg, anchor_model, settings, cancel_path)
        if cancel_path.exists():
            return {"snapshot_id": snapshot_id, "status": "CANCELLED"}
        floor = (metadata.get("context") or {}).get("floor", metadata.get("floor"))
        scorer = CombatScoring(stage_for_floor(int(floor) if floor is not None else None), anchor_model).identity
        history = compare_history(metadata, anchor["first"], scorer, settings)
        if anchor["aa"]["status"] != "PASS" or history["status"] != "PASS":
            return {"snapshot_id": snapshot_id, "status": "EXCLUDED", "reason": "baseline_gate",
                    "aa": anchor["aa"], "history": history, "context": metadata.get("context") or {}}
        a = _model_replay(root, cache_root, metadata, restore, cfg, a_model, settings, cancel_path)
        if cancel_path.exists():
            return {"snapshot_id": snapshot_id, "status": "CANCELLED"}
        if a["aa"]["status"] != "PASS":
            return {"snapshot_id": snapshot_id, "status": "EXCLUDED", "reason": "model_a_gate",
                    "aa": a["aa"], "context": metadata.get("context") or {}}
        b = _model_replay(root, cache_root, metadata, restore, cfg, b_model, settings, cancel_path)
        if cancel_path.exists():
            return {"snapshot_id": snapshot_id, "status": "CANCELLED"}
        if b["aa"]["status"] != "PASS":
            return {"snapshot_id": snapshot_id, "status": "EXCLUDED", "reason": "model_b_gate",
                    "aa": b["aa"], "context": metadata.get("context") or {}}
        outcome = compare_outcomes(a["first"], b["first"])
        if outcome["category"] == "invalid":
            return {"snapshot_id": snapshot_id, "status": "EXCLUDED", "reason": outcome["reason"],
                    "context": metadata.get("context") or {}}
        a_actions, b_actions = a["first"]["actions"], b["first"]["actions"]
        divergence = next((i + 1 for i in range(max(len(a_actions), len(b_actions)))
                           if i >= len(a_actions) or i >= len(b_actions)
                           or (a_actions[i]["action"], a_actions[i]["payload"])
                           != (b_actions[i]["action"], b_actions[i]["payload"])), None)
        return {"snapshot_id": snapshot_id, "status": "VALID", "context": metadata.get("context") or {},
                "comparison": outcome, "first_divergence": divergence,
                "a": a["first"], "b": b["first"], "settings": settings}
    except Exception as exc:
        return {"snapshot_id": snapshot_id, "status": "EXCLUDED", "reason": str(exc)}


def run_job(root: Path, job_dir: Path) -> None:
    config = _read(job_dir / "config.json")
    cancel = job_dir / "cancel"
    cache = job_dir.parent.parent / "cache"
    paths = [Path(value) for value in config["snapshots"]]
    state = {"id": job_dir.name, "status": "running", "pid": os.getpid(),
             "a_id": config["a_id"], "b_id": config["b_id"],
             "a_name": config["a_name"], "b_name": config["b_name"],
             "completed": 0, "total": len(paths),
             "started_at_utc": time.time(), "counts": {}, "pairs": []}
    _write(job_dir / "status.json", state)
    a_model, b_model = config["a_model"], config["b_model"]
    anchor_models = config["anchor_models"]
    parallel = max(1, min(2, int(config["parallel_pairs"])))
    try:
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = [executor.submit(_compare_one, root, cache, path, a_model, b_model, anchor_models, cancel)
                       for path in paths]
            for future in as_completed(futures):
                pair = future.result()
                _write(job_dir / "pairs" / (pair["snapshot_id"] + ".json"), pair)
                state["completed"] += 1
                label = pair.get("comparison", {}).get("category") if pair["status"] == "VALID" else pair["status"].lower()
                state["counts"][label] = state["counts"].get(label, 0) + 1
                state["pairs"].append({"snapshot_id": pair["snapshot_id"], "status": pair["status"],
                                       "context": pair.get("context"), "comparison": pair.get("comparison"),
                                       "reason": pair.get("reason"), "first_divergence": pair.get("first_divergence"),
                                       "a_outcome": (pair.get("a") or {}).get("outcome"),
                                       "b_outcome": (pair.get("b") or {}).get("outcome")})
                _write(job_dir / "status.json", state)
        state["status"] = "cancelled" if cancel.exists() else "completed"
    except Exception as exc:
        state.update(status="failed", error=str(exc))
    state["finished_at_utc"] = time.time()
    _write(job_dir / "status.json", state)


class ComparisonManager:
    def __init__(self, root: Path, log_root: Path):
        self.root = root
        self.work = log_root / "comparisons"
        self.models = ModelLibrary(root)
        self.lock = threading.RLock()
        self.active: subprocess.Popen | None = None
        self.active_id: str | None = None
        self.output = None

    def _running_job_ids(self) -> list[str]:
        ids = []
        for path in (self.work / "jobs").glob("*/status.json"):
            try:
                row = _read(path)
                if row.get("status") in {"starting", "running"} and _process_alive(row.get("pid")):
                    ids.append(str(row["id"]))
            except (OSError, ValueError, KeyError):
                continue
        return ids

    def catalog(self) -> dict[str, Any]:
        rows = snapshot_index(self.work.parent)
        compatibility = snapshot_compatibility(CliConfig(repo_root=self.root))
        snapshots = [{"id": row["snapshot_id"], "floor": row.get("floor"),
                      "turn": row.get("turn"), "context": row.get("context") or {},
                      "status": row.get("status"), "reusable": row.get("reusable")}
                     for row in rows if row.get("status") == "RESTORE_VERIFIED" and row.get("reusable")
                     and row.get("compatibility") == compatibility]
        jobs = []
        for path in sorted((self.work / "jobs").glob("*/status.json"), reverse=True)[:20]:
            try:
                job = _read(path)
                if job.get("status") == "running" and not _process_alive(job.get("pid")):
                    job["status"] = "interrupted"
                jobs.append({key: job.get(key) for key in ("id", "status", "completed", "total", "started_at_utc", "a_name", "b_name")})
            except (OSError, ValueError, KeyError):
                continue
        running = self._running_job_ids()
        return {**self.models.catalog(), "snapshots": snapshots, "jobs": jobs,
                "active_job": running[0] if running else None,
                "engine_consistency": load_gate(self.work.parent)}

    def start(self, a_id: str, b_id: str, snapshot_ids: list[str]) -> dict[str, Any]:
        if not isinstance(snapshot_ids, list) or not 1 <= len(snapshot_ids) <= 48 or len(set(snapshot_ids)) != len(snapshot_ids):
            raise ValueError("Select 1–48 distinct snapshots")
        if a_id == b_id:
            raise ValueError("Choose two different saved models")
        with self.lock:
            require_pass(self.work.parent, snapshot_compatibility(CliConfig(repo_root=self.root)))
            if self._running_job_ids():
                raise ValueError("Another comparison is running")
            library = self.models.catalog()["models"]
            a_entry = next((row for row in library if row["id"] == a_id), None)
            b_entry = next((row for row in library if row["id"] == b_id), None)
            if a_entry is None or b_entry is None:
                raise ValueError("Unknown or incompatible scoring model")
            a_model, b_model = a_entry["model"], b_entry["model"]
            compatibility = snapshot_compatibility(CliConfig(repo_root=self.root))
            available = {row["snapshot_id"]: Path(row["artifact_dir"])
                         for row in snapshot_index(self.work.parent)
                         if row.get("status") == "RESTORE_VERIFIED" and row.get("reusable")
                         and row.get("compatibility") == compatibility}
            if any(item not in available for item in snapshot_ids):
                raise ValueError("Unknown or unverified snapshot")
            paths = [available[item] for item in snapshot_ids]
            workers = []
            for path in paths:
                try:
                    workers.append(_settings(_read(path / "metadata.json"))["workers"])
                except (OSError, ValueError, KeyError):
                    workers.append(4)
            parallel = min(2, max(1, (os.cpu_count() or 4) // max(workers)))
            job_id = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
            directory = self.work / "jobs" / job_id
            directory.mkdir(parents=True)
            anchors = [row["model"] for row in library]
            _write(directory / "config.json", {"a_id": a_id, "b_id": b_id,
                    "a_name": a_entry["name"], "b_name": b_entry["name"],
                    "a_model": a_model, "b_model": b_model,
                    "anchor_models": anchors,
                    "snapshots": [str(path) for path in paths], "parallel_pairs": parallel})
            _write(directory / "status.json", {"id": job_id, "status": "starting",
                    "a_id": a_id, "b_id": b_id, "a_name": a_entry["name"], "b_name": b_entry["name"],
                    "completed": 0, "total": len(paths), "counts": {}, "pairs": []})
            self.output = (directory / "worker.log").open("w", encoding="utf-8")
            try:
                self.active = subprocess.Popen([sys.executable, "-X", "utf8", "-m",
                                                  "scripts.compare_combat_models", "--job-dir", str(directory)],
                                                 cwd=self.root, stdout=self.output, stderr=subprocess.STDOUT,
                                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                self.output.close()
                self.output = None
                raise
            self.active_id = job_id
            starting = _read(directory / "status.json")
            if starting.get("status") == "starting":
                starting["pid"] = self.active.pid
                _write(directory / "status.json", starting)
            return self.job(job_id)

    def job(self, job_id: str) -> dict[str, Any]:
        if not job_id or any(ch not in "0123456789abcdefghijklmnopqrstuvwxyz_" for ch in job_id):
            raise ValueError("Invalid job id")
        path = self.work / "jobs" / job_id / "status.json"
        job = _read(path)
        if job.get("status") in {"starting", "running"} and not _process_alive(job.get("pid")):
            job["status"] = "interrupted"
        return job

    def pair(self, job_id: str, snapshot_id: str) -> dict[str, Any]:
        self.job(job_id)
        if not snapshot_id or any(ch not in "0123456789abcdefghijklmnopqrstuvwxyz_" for ch in snapshot_id):
            raise ValueError("Invalid snapshot id")
        return _read(self.work / "jobs" / job_id / "pairs" / (snapshot_id + ".json"))

    def stop(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.job(job_id)
            if job.get("status") not in {"starting", "running"}:
                raise ValueError("Comparison is not running")
            (self.work / "jobs" / job_id / "cancel").write_text("stop\n", encoding="utf-8")
            return self.job(job_id)
