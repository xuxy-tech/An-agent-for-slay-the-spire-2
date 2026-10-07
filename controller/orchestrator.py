from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter


@dataclass
class ControllerConfig:
    mode: str = "fast"
    pause_every_steps: int = 5


class Orchestrator:
    def __init__(self, controller_cfg: ControllerConfig, cli_cfg: CliConfig):
        self.controller_cfg = controller_cfg
        self.cli = Sts2CliAdapter(cli_cfg)
        self.step_id = 0

    def start(
        self,
        character: str,
        seed: str,
        ascension: int = 0,
        lang: str = "en",
        unlock_mode: str = "all",
        progress_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        self.cli.start()
        return self.cli.start_run(
            character=character,
            seed=seed,
            ascension=ascension,
            lang=lang,
            unlock_mode=unlock_mode,
            progress_path=progress_path,
        )

    def apply_action(self, action: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.step_id += 1

        if self.controller_cfg.mode != "fast":
            return {"type": "error", "message": f"Unknown mode: {self.controller_cfg.mode}"}
        result = self.cli.action(action, args=args, with_snapshot=True)
        return self._decorate_result(result)

    def stop(self) -> None:
        self.cli.stop()

    def _decorate_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(result)
        out["controller"] = {
            "mode": self.controller_cfg.mode,
            "step_id": self.step_id,
            "pause_checkpoint": self.step_id % self.controller_cfg.pause_every_steps == 0,
        }
        return out
