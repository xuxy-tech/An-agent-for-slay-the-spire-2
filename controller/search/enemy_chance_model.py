from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.combat_intent import intent_total_damage


IntentSignature = Tuple[Tuple[str, ...], int | None, int | None]
EnemyIntentKey = Tuple[str, IntentSignature]
EncounterIntentState = Tuple[EnemyIntentKey, ...]


def intent_signature(intent: Mapping[str, Any] | None) -> IntentSignature:
    if not intent:
        return (tuple(), None, None)
    return (
        tuple(str(x) for x in (intent.get("intent_types") or [])),
        int(intent_total_damage(intent)) if intent.get("total_damage") is not None or intent.get("display_damage") is not None else None,
        None if intent.get("hits") is None else int(intent["hits"]),
    )


def encounter_intent_state_from_search_state(search_state: Mapping[str, Any]) -> EncounterIntentState:
    combat = search_state.get("combat") or {}
    enemies = combat.get("enemies") or []
    state: List[EnemyIntentKey] = []
    for enemy in enemies:
        if not isinstance(enemy, Mapping):
            continue
        state.append(
            (
                str(enemy.get("monster_id") or ""),
                intent_signature(enemy.get("intent") if isinstance(enemy.get("intent"), Mapping) else None),
            )
        )
    return tuple(state)


def _state_to_jsonable(state: EncounterIntentState) -> List[Dict[str, Any]]:
    return [
        {
            "monster_id": monster_id,
            "intent_types": list(sig[0]),
            "total_damage": sig[1],
            "display_damage": sig[1],
            "hits": sig[2],
        }
        for monster_id, sig in state
    ]


def _state_from_jsonable(items: Sequence[Mapping[str, Any]]) -> EncounterIntentState:
    state: List[EnemyIntentKey] = []
    for item in items:
        state.append(
            (
                str(item.get("monster_id") or ""),
                (
                    tuple(str(x) for x in (item.get("intent_types") or [])),
                    int(intent_total_damage(item)) if item.get("total_damage") is not None or item.get("display_damage") is not None else None,
                    None if item.get("hits") is None else int(item["hits"]),
                ),
            )
        )
    return tuple(state)


def _normalize_distribution(counter: Counter[EncounterIntentState]) -> List[Dict[str, Any]]:
    total = sum(counter.values())
    if total <= 0:
        return []
    return [
        {
            "next_state": _state_to_jsonable(next_state),
            "count": count,
            "probability": count / total,
        }
        for next_state, count in counter.items()
    ]


@dataclass
class EnemyChanceModel:
    encounter: str
    transition_counts: Dict[EncounterIntentState, Counter[EncounterIntentState]]

    def next_state_distribution(self, current_state: EncounterIntentState) -> List[Dict[str, Any]]:
        return _normalize_distribution(self.transition_counts.get(current_state, Counter()))

    def to_dict(self) -> Dict[str, Any]:
        rows = []
        for current_state, counter in self.transition_counts.items():
            rows.append(
                {
                    "current_state": _state_to_jsonable(current_state),
                    "next_state_distribution": _normalize_distribution(counter),
                }
            )
        return {"encounter": self.encounter, "rows": rows}

    def to_lookup_table(self) -> "EnemyChanceLookupTable":
        return EnemyChanceLookupTable.from_model(self)


@dataclass
class EnemyChanceLookupTable:
    encounter: str
    rows: Dict[str, List[Dict[str, Any]]]
    default_row: List[Dict[str, Any]]

    @staticmethod
    def _key(state: EncounterIntentState) -> str:
        return json.dumps(_state_to_jsonable(state), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_model(cls, model: EnemyChanceModel) -> "EnemyChanceLookupTable":
        rows: Dict[str, List[Dict[str, Any]]] = {}
        for current_state, counter in model.transition_counts.items():
            rows[cls._key(current_state)] = _normalize_distribution(counter)
        return cls(encounter=model.encounter, rows=rows, default_row=[])

    def lookup(self, current_state: EncounterIntentState) -> List[Dict[str, Any]]:
        return self.rows.get(self._key(current_state), self.default_row)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "encounter": self.encounter,
            "rows": [
                {
                    "current_state": json.loads(key),
                    "next_state_distribution": value,
                }
                for key, value in self.rows.items()
            ],
        }

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "EnemyChanceLookupTable":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rows: Dict[str, List[Dict[str, Any]]] = {}
        for row in data.get("rows") or []:
            current_state = _state_from_jsonable(row.get("current_state") or [])
            rows[cls._key(current_state)] = list(row.get("next_state_distribution") or [])
        return cls(encounter=str(data.get("encounter") or ""), rows=rows, default_row=[])


def _normalize_model_key(monster_id: str) -> str:
    return "".join(ch for ch in (monster_id or "").upper() if ch.isalnum())


def _intent_types_from_model_state(state_info: Mapping[str, Any]) -> list[str]:
    kinds: list[str] = []
    observable_kind = str(state_info.get("observable_kind") or "")
    state_name = str(state_info.get("state_name") or "").upper()
    has_attack = (state_info.get("attack_damage") or 0) not in (None, 0)

    if observable_kind == "Attack" or has_attack:
        kinds.append("Attack")
    if observable_kind == "Buff":
        kinds.append("Buff")
    if observable_kind == "Debuff":
        kinds.append("Debuff")
    if observable_kind == "Defend":
        kinds.append("Defend")

    # Minimal name-based recovery for mixed intents that the extracted model marks only as Attack.
    if has_attack and any(token in state_name for token in ("UPPERCUT", "WEB", "SPIT", "SCREECH", "TERROR", "DEBUFF")):
        if "Debuff" not in kinds:
            kinds.append("Debuff")
    return kinds


def _visible_signature_from_model_state(
    monster_id: str,
    state_info: Mapping[str, Any],
    runtime_state_info: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    damage = state_info.get("attack_damage")
    hits = state_info.get("attack_repeat")
    if runtime_state_info and runtime_state_info.get("intents"):
        intent_types = [
            str(x.get("intent_type") or "")
            for x in (runtime_state_info.get("intents") or [])
            if isinstance(x, Mapping)
        ]
    else:
        intent_types = _intent_types_from_model_state(state_info)
    return {
        "monster_id": monster_id,
        "intent_types": intent_types,
        "total_damage": (int(damage) * int(hits or 1)) if damage not in (None, 0) else None,
        "display_damage": (int(damage) * int(hits or 1)) if damage not in (None, 0) else None,
        "hits": hits if damage not in (None, 0) else None,
    }


def _load_enemy_models(repo_root: Path) -> Dict[str, Any]:
    path = repo_root / "data/enemy_models/monster_move_models.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _next_distribution_from_model_state(
    monster_models: Mapping[str, Any],
    monster_id: str,
    current_state_id: str,
    runtime_states: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    model = monster_models.get(_normalize_model_key(monster_id))
    if not isinstance(model, Mapping):
        return []
    states = model.get("states") or {}
    branches = model.get("branches") or {}
    runtime_state_by_id = {
        str(x.get("id") or ""): x for x in runtime_states if isinstance(x, Mapping)
    }

    current_local = None
    current = states.get(current_state_id)
    if isinstance(current, Mapping):
        current_local = current_state_id
    else:
        for local, state in states.items():
            if str((state or {}).get("state_name") or "") == current_state_id:
                current_local = local
                current = state
                break
    if not isinstance(current, Mapping):
        return []

    follow_up = current.get("follow_up_local")
    if not follow_up:
        return []

    if follow_up in branches:
        out: List[Dict[str, Any]] = []
        for branch in branches.get(follow_up, {}).get("branches") or []:
            target = branch.get("target_local")
            target_state = states.get(target) or {}
            runtime_target = runtime_state_by_id.get(str(target_state.get("state_name") or ""))
            out.append(
                {
                    "next_state": _visible_signature_from_model_state(monster_id, target_state, runtime_target),
                    "probability": float(branch.get("probability") or 0.0),
                }
            )
        return out

    target_state = states.get(follow_up)
    if not isinstance(target_state, Mapping):
        return []
    runtime_target = runtime_state_by_id.get(str(target_state.get("state_name") or ""))
    return [
        {
            "next_state": _visible_signature_from_model_state(monster_id, target_state, runtime_target),
            "probability": 1.0,
        }
    ]


def sample_enemy_chance_model(
    cli_cfg: CliConfig,
    encounter: str,
    character: str = "Ironclad",
    ascension: int = 0,
    lang: str = "en",
    seed_start: int = 1,
    num_seeds: int = 20,
) -> EnemyChanceModel:
    transitions: Dict[EncounterIntentState, Counter[EncounterIntentState]] = defaultdict(Counter)

    for seed in range(seed_start, seed_start + num_seeds):
        cli = Sts2CliAdapter(cli_cfg)
        try:
            cli.start()
            cli.start_test_combat(
                character=character,
                encounter=encounter,
                seed=str(seed),
                ascension=ascension,
                lang=lang,
            )
            first_state = encounter_intent_state_from_search_state(
                cli.get_search_state().get("combat_state_for_search") or {}
            )
            cli.action("end_turn", with_snapshot=False)
            second_state = encounter_intent_state_from_search_state(
                cli.get_search_state().get("combat_state_for_search") or {}
            )
            transitions[first_state][second_state] += 1
        finally:
            cli.stop()

    return EnemyChanceModel(encounter=encounter, transition_counts=transitions)


def sample_enemy_lookup_table(
    cli_cfg: CliConfig,
    encounter: str,
    character: str = "Ironclad",
    ascension: int = 0,
    lang: str = "en",
    seed_start: int = 1,
    num_seeds: int = 20,
) -> EnemyChanceLookupTable:
    rows: Dict[str, List[Dict[str, Any]]] = {}
    grouped: Dict[str, Counter[str]] = defaultdict(Counter)
    payloads: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for seed in range(seed_start, seed_start + num_seeds):
        cli = Sts2CliAdapter(cli_cfg)
        try:
            cli.start()
            cli.start_test_combat(
                character=character,
                encounter=encounter,
                seed=str(seed),
                ascension=ascension,
                lang=lang,
            )
            current_search_state = cli.get_search_state().get("combat_state_for_search") or {}
            current_state = encounter_intent_state_from_search_state(current_search_state)
            current_key = EnemyChanceLookupTable._key(current_state)

            cli.action("end_turn", with_snapshot=False)
            next_search_state = cli.get_search_state().get("combat_state_for_search") or {}
            next_state = encounter_intent_state_from_search_state(next_search_state)
            next_state_json = json.dumps(
                next_search_state,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

            grouped[current_key][next_state_json] += 1
            payloads[(current_key, next_state_json)] = {
                "next_state": _state_to_jsonable(next_state),
                "next_search_state": next_search_state,
            }
        finally:
            cli.stop()

    for current_key, counter in grouped.items():
        total = sum(counter.values())
        if total <= 0:
            continue
        current_rows: List[Dict[str, Any]] = []
        for next_state_json, count in counter.items():
            payload = payloads[(current_key, next_state_json)]
            current_rows.append(
                {
                    "next_state": payload["next_state"],
                    "next_search_state": payload["next_search_state"],
                    "count": count,
                    "probability": count / total,
                }
            )
        rows[current_key] = current_rows

    return EnemyChanceLookupTable(encounter=encounter, rows=rows, default_row=[])


def build_lookup_table_from_models(
    cli_cfg: CliConfig,
    encounter: str,
    character: str = "Ironclad",
    ascension: int = 0,
    lang: str = "en",
    seed_start: int = 1,
    num_seeds: int = 20,
) -> EnemyChanceLookupTable:
    repo_root = Path(cli_cfg.repo_root)
    monster_models = _load_enemy_models(repo_root)
    rows: Dict[str, List[Dict[str, Any]]] = {}

    for seed in range(seed_start, seed_start + num_seeds):
        cli = Sts2CliAdapter(cli_cfg)
        try:
            cli.start()
            cli.start_test_combat(
                character=character,
                encounter=encounter,
                seed=str(seed),
                ascension=ascension,
                lang=lang,
            )
            state_result = cli.get_search_state()
            search_state = state_result.get("combat_state_for_search") or {}
            current_state = encounter_intent_state_from_search_state(search_state)
            if EnemyChanceLookupTable._key(current_state) in rows:
                continue

            ai = cli.send({"cmd": "inspect_enemy_ai"})
            ai_enemies = ai.get("enemies") or []
            search_enemies = ((search_state.get("combat") or {}).get("enemies") or [])
            if len(ai_enemies) != len(search_enemies):
                continue

            per_enemy: List[List[Dict[str, Any]]] = []
            valid = True
            for ai_enemy, search_enemy in zip(ai_enemies, search_enemies):
                live_monster_id = str(search_enemy.get("monster_id") or ai_enemy.get("monster_id") or "")
                current_state_id = str(ai_enemy.get("current_state_id") or "")
                dist = _next_distribution_from_model_state(
                    monster_models,
                    live_monster_id,
                    current_state_id,
                    ai_enemy.get("states") or [],
                )
                if not dist:
                    valid = False
                    break
                per_enemy.append(dist)
            if not valid:
                continue

            combined: List[Dict[str, Any]] = [{"next_state": [], "probability": 1.0}]
            for enemy_dist in per_enemy:
                next_combined: List[Dict[str, Any]] = []
                for prefix in combined:
                    for branch in enemy_dist:
                        next_combined.append(
                            {
                                "next_state": list(prefix["next_state"]) + [branch["next_state"]],
                                "probability": float(prefix["probability"]) * float(branch["probability"]),
                            }
                        )
                combined = next_combined

            rows[EnemyChanceLookupTable._key(current_state)] = [
                {
                    "next_state": row["next_state"],
                    "count": None,
                    "probability": row["probability"],
                }
                for row in combined
            ]
        finally:
            cli.stop()

    return EnemyChanceLookupTable(encounter=encounter, rows=rows, default_row=[])


def build_lookup_table(
    cli_cfg: CliConfig,
    encounter: str,
    character: str = "Ironclad",
    ascension: int = 0,
    lang: str = "en",
    seed_start: int = 1,
    num_seeds: int = 20,
    source: str = "sample",
) -> EnemyChanceLookupTable:
    if source in {"auto", "model"}:
        table = build_lookup_table_from_models(
            cli_cfg=cli_cfg,
            encounter=encounter,
            character=character,
            ascension=ascension,
            lang=lang,
            seed_start=seed_start,
            num_seeds=num_seeds,
        )
        if source == "model" or table.rows:
            return table

    return sample_enemy_lookup_table(
        cli_cfg=cli_cfg,
        encounter=encounter,
        character=character,
        ascension=ascension,
        lang=lang,
        seed_start=seed_start,
        num_seeds=num_seeds,
    )
