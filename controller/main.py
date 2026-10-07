from __future__ import annotations

import argparse
import json
from pathlib import Path

from cli.sts2_cli_adapter import CliConfig
from controller.orchestrator import ControllerConfig, Orchestrator


def main() -> None:
    parser = argparse.ArgumentParser(description="Three-layer controller for STS2 project")
    parser.add_argument("--mode", choices=["fast"], default="fast")
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument("--seed", default="42")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--pause-every", type=int, default=5)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    orch = Orchestrator(
        ControllerConfig(mode=args.mode, pause_every_steps=args.pause_every),
        CliConfig(repo_root=repo_root),
    )

    state = orch.start(character=args.character, seed=args.seed, ascension=args.ascension, lang=args.lang)
    print(json.dumps({"event": "start", "state": state}, ensure_ascii=False))

    try:
        # Demo loop: auto play until first combat then 3 steps.
        combat_steps = 0
        for _ in range(200):
            dec = state.get("decision")
            if dec == "event_choice":
                state = orch.apply_action("choose_option", {"option_index": 0}).get("result", {})
            elif dec == "map_select":
                choices = state.get("choices") or []
                if not choices:
                    break
                combat = [c for c in choices if c.get("type") in ("Monster", "Elite")]
                pick = combat[0] if combat else choices[0]
                state = orch.apply_action("select_map_node", {"col": pick["col"], "row": pick["row"]}).get("result", {})
            elif dec == "combat_play":
                hand = state.get("hand") or []
                playable = [c for c in hand if c.get("can_play")]
                if playable:
                    c = playable[0]
                    payload = {"card_index": c["index"]}
                    if c.get("target_type") in ("AnyEnemy", "SingleEnemy"):
                        payload["target_index"] = 0
                    res = orch.apply_action("play_card", payload)
                else:
                    res = orch.apply_action("end_turn")
                print(json.dumps(res, ensure_ascii=False))
                state = res.get("result", {})
                combat_steps += 1
                if combat_steps >= 3:
                    break
            elif dec == "card_reward":
                state = orch.apply_action("select_card_reward", {"card_index": 0}).get("result", {})
            elif state.get("type") == "error" or dec == "game_over":
                print(json.dumps({"event": "stop", "state": state}, ensure_ascii=False))
                break
            else:
                state = orch.apply_action("proceed").get("result", {})
    finally:
        orch.stop()


if __name__ == "__main__":
    main()
