"""Streaming, replaceable training backend contract for human-play datasets."""
from __future__ import annotations

import hashlib
import importlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from controller.human_capture import DATASET_SCHEMA, utc_now

SPLITS = ("train", "validation", "test")


@dataclass(frozen=True)
class HumanDataset:
    root: Path
    manifest: dict[str, Any]

    @classmethod
    def open(cls, root: Path) -> "HumanDataset":
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != DATASET_SCHEMA:
            raise ValueError("Unsupported human-play dataset manifest")
        for split in SPLITS:
            if not (root / f"{split}.jsonl").is_file():
                raise ValueError(f"Human-play dataset is missing {split}.jsonl")
        return cls(root=root, manifest=manifest)

    def split_path(self, split: str) -> Path:
        if split not in SPLITS:
            raise ValueError(f"Unsupported dataset split {split!r}")
        return self.root / f"{split}.jsonl"

    def iter_split(self, split: str) -> Iterator[dict[str, Any]]:
        path = self.split_path(split)
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("schema") != DATASET_SCHEMA:
                    raise ValueError(f"Unsupported dataset schema in {path}:{line_number}")
                yield row

    def inspect(self) -> dict[str, Any]:
        counts = {split: sum(1 for _ in self.iter_split(split)) for split in SPLITS}
        expected = self.manifest.get("counts")
        if isinstance(expected, dict):
            mismatches = {
                split: {"manifest": int(expected.get(split, 0)), "actual": counts[split]}
                for split in SPLITS
                if int(expected.get(split, 0)) != counts[split]
            }
            if mismatches:
                raise ValueError(f"Dataset manifest counts do not match JSONL: {mismatches}")
        return {
            "schema": DATASET_SCHEMA,
            "manifest_sha256": file_sha256(self.root / "manifest.json"),
            "split_sha256": {
                split: file_sha256(self.split_path(split)) for split in SPLITS
            },
            "counts": counts,
        }


class HumanTrainingBackend(Protocol):
    identity: dict[str, Any]

    def fit(self, dataset: HumanDataset, output_dir: Path) -> dict[str, Any]: ...

    def evaluate(self, dataset: HumanDataset, split: str) -> dict[str, Any]: ...


def file_sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def load_training_backend(
    spec: str, config: dict[str, Any] | None = None
) -> HumanTrainingBackend:
    backend_config = dict(config or {})
    if spec == "frequency":
        backend: Any = ActionFrequencyBaseline(backend_config)
    else:
        module_name, separator, attribute = spec.partition(":")
        if not separator or not module_name or not attribute:
            raise ValueError("External backend must use module:factory syntax")
        factory: Callable[[dict[str, Any]], Any] = getattr(
            importlib.import_module(module_name), attribute
        )
        backend = factory(backend_config)
    if not isinstance(getattr(backend, "identity", None), dict):
        raise TypeError("Training backend must expose an identity dictionary")
    if not callable(getattr(backend, "fit", None)) or not callable(getattr(backend, "evaluate", None)):
        raise TypeError("Training backend must implement fit(dataset, output_dir) and evaluate(dataset, split)")
    return backend


def run_training(
    dataset_dir: Path, output_dir: Path, backend: HumanTrainingBackend
) -> dict[str, Any]:
    dataset = HumanDataset.open(dataset_dir)
    dataset_identity = dataset.inspect()
    if dataset_identity["counts"]["train"] == 0:
        raise ValueError("Training split is empty")
    output_dir.mkdir(parents=True, exist_ok=False)
    fit = backend.fit(dataset, output_dir)
    result = {
        "schema": "sts2.human_training.run.v2",
        "created_at_utc": utc_now(),
        "backend": backend.identity,
        "dataset": dataset_identity,
        "fit": fit,
        "test": backend.evaluate(dataset, "test"),
    }
    (output_dir / "training_run.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return result


class ActionFrequencyBaseline:
    """Streaming pipeline smoke model, not a claim of useful gameplay strength."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = dict(config or {})
        context_fields = self.config.get("context_fields", ["screen", "in_combat"])
        if not isinstance(context_fields, list) or not all(
            isinstance(field, str) and field for field in context_fields
        ):
            raise ValueError("frequency.context_fields must be a list of non-empty strings")
        self.context_fields = context_fields
        self.identity = {
            "name": "action_frequency_baseline",
            "version": 2,
            "purpose": "pipeline_feasibility_only",
            "config": self.config,
        }
        self.by_context: dict[str, str] = {}
        self.default_action = "end_turn"

    def context(self, example: dict[str, Any]) -> str:
        observation = example.get("observation") or {}
        return json.dumps(
            [observation.get(field) for field in self.context_fields],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def fit(self, dataset: HumanDataset, output_dir: Path) -> dict[str, Any]:
        grouped: dict[str, Counter[str]] = defaultdict(Counter)
        global_counts: Counter[str] = Counter()
        for example in dataset.iter_split("train"):
            action = str((example.get("chosen_action") or {}).get("type") or "unknown")
            grouped[self.context(example)][action] += 1
            global_counts[action] += 1
        self.default_action = global_counts.most_common(1)[0][0]
        self.by_context = {
            context: counts.most_common(1)[0][0]
            for context, counts in sorted(grouped.items())
        }
        model = {
            "schema": "sts2.human_training.action_frequency.v2",
            "backend": self.identity,
            "default_action": self.default_action,
            "by_context": self.by_context,
        }
        (output_dir / "model.json").write_text(
            json.dumps(model, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return {
            "train": self.evaluate(dataset, "train"),
            "validation": self.evaluate(dataset, "validation"),
        }

    def evaluate(self, dataset: HumanDataset, split: str) -> dict[str, Any]:
        examples = 0
        correct = 0
        for example in dataset.iter_split(split):
            examples += 1
            predicted = self.by_context.get(self.context(example), self.default_action)
            actual = str((example.get("chosen_action") or {}).get("type") or "unknown")
            correct += predicted == actual
        return {
            "examples": examples,
            "accuracy": None if examples == 0 else correct / examples,
        }


def build_frequency_backend(config: dict[str, Any]) -> ActionFrequencyBaseline:
    """Example external factory implementing the module:factory contract."""
    return ActionFrequencyBaseline(config)
