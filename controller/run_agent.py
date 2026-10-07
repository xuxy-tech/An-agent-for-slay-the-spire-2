from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import re
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cli.sts2_cli_adapter import CliConfig, Sts2CliAdapter
from controller.combat_intent import intent_deals_damage, intent_total_damage
from controller.global_q import GlobalQPolicy, card_reward_candidates, map_candidates, rest_site_candidates
from controller.search.actions import (
    SearchAction,
    available_actions_from_search_state,
    cli_payload_for_action,
    resolve_combat_payload,
    resolve_planned_action,
    summarize_combat_action,
)
from controller.search.combat_search import CombatSearcher, CombatSpec, CombatWorkerPool, RecordedAction
from controller.search.state_cache import UNMODELED_SEARCH_POTION_IDS
from controller.combat_step import (
    CombatStepConfig,
    DEFAULT_TURN_ACTION_CAP,
    PlanState,
    decide_combat_action,
)
from controller.route_strategy import choose_weighted_route
from controller.card_catalog import load_card_catalog
from controller.deck_profile import (
    card_id_from_row,
    choose_profile_reward,
    deck_card_ids,
    load_deck_profile,
    profile_card_priority,
    score_profile_card,
)
from controller.combat_scoring import validate_model


_HEADLESS_DECK_PROFILE_PATH: Path | None = None
_HEADLESS_DECK_PROFILE: dict | None = None


def _default_deck_profile() -> dict:
    return _HEADLESS_DECK_PROFILE if _HEADLESS_DECK_PROFILE is not None else load_deck_profile(_HEADLESS_DECK_PROFILE_PATH)


IRONCLAD_STARTER_CARD_IDS = frozenset({'STRIKE_IRONCLAD', 'DEFEND_IRONCLAD', 'BASH'})


def choose_treasure_action(state: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Follow the same native chest boundaries as the visible runner."""
    decision = state.get('decision')
    if decision == 'treasure':
        return 'open_chest', {}
    if decision == 'treasure_relic':
        relics = state.get('relics') or []
        if (not isinstance(relics, list) or not relics or not isinstance(relics[0], dict)
                or type(relics[0].get('index')) is not int):
            raise ValueError('Treasure relic choice has no stable engine index')
        return 'choose_treasure_relic', {'relic_index': relics[0]['index']}
    if decision == 'treasure_complete':
        return 'leave_room', {}
    raise ValueError(f'Not a treasure decision: {decision!r}')


class NoncombatProgressGuard:
    """Fail fast when an identical noncombat command leaves the state unchanged."""
    DECISIONS = frozenset({'treasure', 'treasure_relic', 'treasure_complete', 'unknown',
                           'shop', 'rest_site', 'event_choice', 'map_select',
                           'combat_reward', 'card_reward', 'card_select', 'bundle_select'})

    def __init__(self, limit: int = 3):
        self.limit = limit
        self.key: tuple[str, str, str, str] | None = None
        self.count = 0

    def check(self, before: Dict[str, Any], after: Dict[str, Any],
              decision: str, action: str, payload: Dict[str, Any]) -> None:
        if decision not in self.DECISIONS or after.get('type') == 'error':
            self.key, self.count = None, 0
            return
        before_json = json.dumps(before, sort_keys=True, ensure_ascii=False)
        if before_json != json.dumps(after, sort_keys=True, ensure_ascii=False):
            self.key, self.count = None, 0
            return
        key = (decision, action, json.dumps(payload, sort_keys=True, ensure_ascii=False), before_json)
        self.count = self.count + 1 if key == self.key else 1
        self.key = key
        if self.count >= self.limit:
            raise RuntimeError(f'Headless {decision} made no progress after {self.count} '
                               f'{action} actions; payload={payload!r}')


def _load_encounter_ids(repo_root: Path) -> List[str]:
    path = repo_root / "third_party/sts2-cli" / "localization_eng" / "encounters.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    ids: List[str] = []
    for key in data.keys():
        if not key.endswith(".title"):
            continue
        encounter_id = key[:-6]
        if "EVENT" in encounter_id or "DEPRECATED" in encounter_id:
            continue
        # Do not rely on suffix heuristics here. Some real combat encounters used
        # in live runs (for example OVERGROWTH_CRAWLERS) do not carry the usual
        # _WEAK/_NORMAL/_ELITE/_BOSS suffix. We allow all titled encounters here
        # and let start_test_combat() probe/filter the truly unsupported ones.
        ids.append(encounter_id)
    return sorted(set(ids))


def _combat_signature(search_state: Dict[str, Any]) -> Tuple[Tuple[Any, ...], ...]:
    combat = (search_state.get("combat") or {})
    enemies = combat.get("enemies") or []
    sig: List[Tuple[Any, ...]] = []
    for e in enemies:
        intent = e.get("intent") or {}
        sig.append(
            (
                e.get("monster_id"),
                e.get("hp"),
                e.get("block"),
                tuple(intent.get("intent_types") or []),
                intent.get("total_damage"),
                intent.get("display_damage"),
                intent.get("hits"),
            )
        )
    return tuple(sig)


def _enemy_ids_signature(search_state: Dict[str, Any]) -> Tuple[str, ...]:
    combat = (search_state.get("combat") or {})
    enemies = combat.get("enemies") or []
    return tuple(str(e.get("monster_id")) for e in enemies)


def _normalize_enemy_family(monster_id: str) -> str:
    parts = [p for p in str(monster_id).split("_") if p]
    if len(parts) >= 2:
        return "_".join(parts[-2:])
    return str(monster_id)


def _normalize_card_id(card_id: str) -> str:
    cid = str(card_id or '').strip().upper()
    if cid.startswith('CARD.'):
        cid = cid[5:]
    cid = cid.replace('-', '_').replace(' ', '_')
    return cid


def _enemy_family_signature(search_state: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(sorted(_normalize_enemy_family(mid) for mid in _enemy_ids_signature(search_state)))


def build_encounter_index(cli_cfg: CliConfig, seed: str = "42") -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    repo_root = cli_cfg.repo_root
    cache_path = repo_root / "data" / f"encounter_signature_index_{seed}.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if "exact" in cached and "enemy_ids" in cached and "enemy_families" in cached:
            return cached.get("exact") or {}, cached.get("enemy_ids") or {}, cached.get("enemy_families") or {}
        # Old cache formats are missing one or more secondary indexes. Rebuild so
        # we also get the enemy-id and family fallback indexes for real-run
        # encounter detection.
        cache_path.unlink()
    encounter_ids = _load_encounter_ids(repo_root)
    exact: Dict[str, str] = {}
    enemy_ids: Dict[str, str] = {}
    enemy_families: Dict[str, str] = {}
    enemy_ids_collisions: set[str] = set()
    enemy_families_collisions: set[str] = set()
    for encounter_id in encounter_ids:
        cli = Sts2CliAdapter(cli_cfg)
        try:
            cli.start()
            cli.start_test_combat(character="Ironclad", encounter=encounter_id, seed=seed, ascension=0, lang="en")
            state = cli.get_search_state().get("combat_state_for_search") or {}
            sig = _combat_signature(state)
            enemy_sig = json.dumps(_enemy_ids_signature(state), ensure_ascii=False)
            family_sig = json.dumps(_enemy_family_signature(state), ensure_ascii=False)
            if sig and json.dumps(sig, ensure_ascii=False) not in exact:
                exact[json.dumps(sig, ensure_ascii=False)] = encounter_id
            if enemy_sig:
                if enemy_sig in enemy_ids and enemy_ids[enemy_sig] != encounter_id:
                    enemy_ids_collisions.add(enemy_sig)
                elif enemy_sig not in enemy_ids:
                    enemy_ids[enemy_sig] = encounter_id
            if family_sig:
                if family_sig in enemy_families and enemy_families[family_sig] != encounter_id:
                    enemy_families_collisions.add(family_sig)
                elif family_sig not in enemy_families:
                    enemy_families[family_sig] = encounter_id
        except Exception:
            continue
        finally:
            cli.stop()
    for key in enemy_ids_collisions:
        enemy_ids.pop(key, None)
    for key in enemy_families_collisions:
        enemy_families.pop(key, None)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"exact": exact, "enemy_ids": enemy_ids, "enemy_families": enemy_families}, ensure_ascii=False, indent=2))
    return exact, enemy_ids, enemy_families


def infer_encounter_id(
    search_state: Dict[str, Any],
    encounter_exact: Dict[str, str],
    encounter_enemy_ids: Dict[str, str],
    encounter_enemy_families: Dict[str, str],
) -> Optional[str]:
    sig = json.dumps(_combat_signature(search_state), ensure_ascii=False)
    if sig in encounter_exact:
        return encounter_exact[sig]
    enemy_sig = json.dumps(_enemy_ids_signature(search_state), ensure_ascii=False)
    if enemy_sig in encounter_enemy_ids:
        return encounter_enemy_ids[enemy_sig]
    family_sig = json.dumps(_enemy_family_signature(search_state), ensure_ascii=False)
    if family_sig in encounter_enemy_families:
        return encounter_enemy_families[family_sig]
    return None


def choose_map_node(state: Dict[str, Any]) -> Dict[str, Any]:
    player = state.get("player") or {}
    hp = float(player.get("hp") or 0)
    max_hp = float(player.get("max_hp") or 1)
    gold = float(player.get("gold") or 0)
    ratio = hp / max_hp if max_hp > 0 else 1.0
    floor = _run_floor(state)
    choices = state.get("choices") or []

    preferred_orders: List[List[str]] = []
    if ratio < 0.45:
        preferred_orders.append(["RestSite", "Monster", "Treasure", "Unknown", "Event", "Shop", "Elite"])
    elif floor >= 2 and ratio >= 0.85:
        preferred_orders.append(["Monster", "Treasure", "RestSite", "Unknown", "Event", "Shop", "Elite"])
    elif gold >= 120 and floor >= 10 and ratio >= 0.65:
        preferred_orders.append(["Monster", "Treasure", "RestSite", "Unknown", "Event", "Shop", "Elite"])
    else:
        preferred_orders.append(["Monster", "Treasure", "RestSite", "Unknown", "Event", "Shop", "Elite"])
    preferred_orders.append(["Monster", "Treasure", "RestSite", "Unknown", "Event", "Shop", "Elite"])

    for order in preferred_orders:
        for wanted in order:
            for c in choices:
                if c.get("type") == wanted:
                    return {"col": c["col"], "row": c["row"]}
    if not choices:
        raise RuntimeError("No map choices available")
    c = choices[0]
    return {"col": c["col"], "row": c["row"]}


def choose_map_node_global(
    state: Dict[str, Any],
    map_data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    choices = state.get("choices") or []
    if not choices or not isinstance(map_data, dict) or map_data.get("type") != "map":
        return choose_map_node(state)

    node_map: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for row_nodes in map_data.get("rows") or []:
        for node in row_nodes or []:
            try:
                key = (int(node.get("col")), int(node.get("row")))
            except Exception:
                continue
            node_map[key] = dict(node)
    boss = map_data.get("boss") or {}
    if boss.get("col") is not None and boss.get("row") is not None:
        node_map[(int(boss["col"]), int(boss["row"]))] = dict(boss)

    memo: Dict[Tuple[int, int], Tuple[int, int, int, int, int, int]] = {}

    def _path_score(node_key: Tuple[int, int]) -> Tuple[int, int, int, int, int, int]:
        cached = memo.get(node_key)
        if cached is not None:
            return cached
        node = node_map.get(node_key) or {}
        node_type = str(node.get("type") or "")
        own = (
            1 if node_type == "RestSite" else 0,
            1 if node_type == "Shop" else 0,
            1 if node_type == "Boss" else 0,
            1 if node_type == "Unknown" else 0,
            1 if node_type == "Monster" else 0,
            1 if node_type == "Elite" else 0,
        )
        children = node.get("children") or []
        if not children:
            memo[node_key] = own
            return own
        best_child = max(
            (
                _path_score((int(child.get("col")), int(child.get("row"))))
                for child in children
                if child.get("col") is not None and child.get("row") is not None
            ),
            default=(0, 0, 0, 0, 0, 0),
        )
        total = tuple(int(own[i]) + int(best_child[i]) for i in range(len(own)))
        memo[node_key] = total
        return total

    def _choice_key(choice: Dict[str, Any]) -> Tuple[int, int, int, int, int, int, int]:
        rest_sites, shops, bosses, unknowns, monsters, elites = _path_score((int(choice["col"]), int(choice["row"])))
        return (
            rest_sites,
            shops,
            bosses,
            unknowns,
            -monsters,
            elites,
            -int(choice.get("col") or 0),
        )

    best = max(choices, key=_choice_key)
    return {"col": int(best["col"]), "row": int(best["row"])}


def choose_map_route_global(
    state: Dict[str, Any],
    map_data: Optional[Dict[str, Any]],
    descriptions: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if isinstance(map_data, dict):
        try:
            return choose_weighted_route(state.get('choices') or [], map_data)
        except ValueError:
            pass

    # Legacy planner below remains a compatibility fallback for partial maps.
    choices = state.get("choices") or []
    if not choices or not isinstance(map_data, dict) or map_data.get("type") != "map":
        chosen = choose_map_node(state)
        route = [{"col": int(chosen["col"]), "row": int(chosen["row"]), "type": None}]
        return chosen, route

    descriptions = descriptions or {}
    player = state.get("player") or {}
    hp = float(player.get("hp") or 0.0)
    max_hp = float(player.get("max_hp") or 1.0)
    hp_ratio = hp / max_hp if max_hp > 0 else 0.0
    floor = _run_floor(state)
    deck = _player_deck_cards(state)
    basic_bloat = _basic_card_bloat(deck)
    thresholds = _port_thresholds_for_floor(floor)
    deck_totals = _deck_port_totals(state, descriptions) if descriptions else {
        key: 0.0 for key in thresholds
    }
    deficit_mass = 0.0
    for port, threshold in thresholds.items():
        deficit = max(0.0, float(threshold) - float(deck_totals.get(port) or 0.0))
        deficit_mass += min(deficit, 3.0)

    elite_bias = 0
    if floor <= 8:
        if hp_ratio < 0.85 or basic_bloat >= 5 or deficit_mass >= 5.0:
            elite_bias = -1
        elif hp_ratio >= 0.92 and basic_bloat <= 2 and deficit_mass <= 2.5 and floor >= 6:
            elite_bias = 1
    elif hp_ratio >= 0.9 and deficit_mass <= 2.0:
        elite_bias = 1

    node_map: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for row_nodes in map_data.get("rows") or []:
        for node in row_nodes or []:
            try:
                key = (int(node.get("col")), int(node.get("row")))
            except Exception:
                continue
            node_map[key] = dict(node)
    boss = map_data.get("boss") or {}
    if boss.get("col") is not None and boss.get("row") is not None:
        node_map[(int(boss["col"]), int(boss["row"]))] = dict(boss)

    score_memo: Dict[Tuple[int, int], Tuple[int, int, int, int, int, int]] = {}
    route_memo: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}

    def _node_own_score(node_key: Tuple[int, int]) -> Tuple[int, int, int, int, int, int]:
        node = node_map.get(node_key) or {}
        node_type = str(node.get("type") or "")
        return (
            1 if node_type == "RestSite" else 0,
            1 if node_type == "Shop" else 0,
            1 if node_type == "Boss" else 0,
            1 if node_type == "Unknown" else 0,
            1 if node_type == "Monster" else 0,
            1 if node_type == "Elite" else 0,
        )

    def _combine_scores(
        own: Tuple[int, int, int, int, int, int],
        child: Tuple[int, int, int, int, int, int],
    ) -> Tuple[int, int, int, int, int, int]:
        return tuple(int(own[i]) + int(child[i]) for i in range(len(own)))

    def _path_priority(
        node_key: Tuple[int, int],
        score: Tuple[int, int, int, int, int, int],
        route: List[Dict[str, Any]],
    ) -> Tuple[int, int, int, int, int, int, int, int]:
        rest_sites, shops, bosses, unknowns, monsters, elites = score
        # The primary route policy is still resource-first. Elite preference is
        # state-dependent: weak early decks should not be committed into
        # forced elites just because higher-priority counts tie, while healthy
        # later decks may treat elites as acceptable upside.
        elite_term = elites * elite_bias
        return (
            rest_sites,
            shops,
            bosses,
            unknowns,
            elite_term,
            -monsters,
            len(route),
            -int(node_key[0]),
        )

    def _best_from(node_key: Tuple[int, int]) -> Tuple[Tuple[int, int, int, int, int, int], List[Dict[str, Any]]]:
        cached_score = score_memo.get(node_key)
        cached_route = route_memo.get(node_key)
        if cached_score is not None and cached_route is not None:
            return cached_score, list(cached_route)

        own = _node_own_score(node_key)
        node = node_map.get(node_key) or {}
        children = node.get("children") or []
        valid_children = [
            (int(child.get("col")), int(child.get("row")))
            for child in children
            if child.get("col") is not None and child.get("row") is not None
        ]
        if not valid_children:
            score_memo[node_key] = own
            route_memo[node_key] = [
                {"col": int(node_key[0]), "row": int(node_key[1]), "type": str(node.get("type") or "")}
            ]
            return own, list(route_memo[node_key])

        best_child_score: Optional[Tuple[int, int, int, int, int, int]] = None
        best_child_route: Optional[List[Dict[str, Any]]] = None
        best_child_key: Optional[Tuple[int, int]] = None
        for child_key in valid_children:
            child_score, child_route = _best_from(child_key)
            total_score = _combine_scores(own, child_score)
            total_route = [
                {"col": int(node_key[0]), "row": int(node_key[1]), "type": str(node.get("type") or "")}
            ] + child_route
            if (
                best_child_score is None
                or _path_priority(node_key, total_score, total_route)
                > _path_priority(best_child_key or node_key, best_child_score, best_child_route or [node_key])
            ):
                best_child_score = total_score
                best_child_route = total_route
                best_child_key = node_key

        assert best_child_score is not None and best_child_route is not None
        score_memo[node_key] = best_child_score
        route_memo[node_key] = list(best_child_route)
        return best_child_score, list(best_child_route)

    best_choice: Optional[Dict[str, Any]] = None
    best_route: Optional[List[Tuple[int, int]]] = None
    best_score: Optional[Tuple[int, int, int, int, int, int]] = None
    for choice in choices:
        choice_key = (int(choice["col"]), int(choice["row"]))
        score, route = _best_from(choice_key)
        if (
            best_choice is None
            or _path_priority(choice_key, score, route)
            > _path_priority((int(best_choice["col"]), int(best_choice["row"])), best_score or (0, 0, 0, 0, 0, 0), best_route or [])
        ):
            best_choice = choice
            best_route = route
            best_score = score

    if best_choice is None or best_route is None:
        chosen = choose_map_node_global(state, map_data)
        return chosen, [{"col": int(chosen["col"]), "row": int(chosen["row"]), "type": None}]
    return {"col": int(best_choice["col"]), "row": int(best_choice["row"])}, best_route


def map_context_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    player = state.get("player") or {}
    choices = state.get("choices") or []
    summarized = []
    for choice in choices:
        summarized.append(
            {
                "col": choice.get("col"),
                "row": choice.get("row"),
                "type": choice.get("type"),
                "icon": choice.get("icon"),
            }
        )
    return {
        "hp": player.get("hp"),
        "max_hp": player.get("max_hp"),
        "gold": player.get("gold"),
        "choices": summarized,
    }


def choose_rest_option(state: Dict[str, Any], descriptions: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    options = [o for o in (state.get("options") or []) if o.get("is_enabled")]
    player = state.get("player") or {}
    hp = float(player.get("hp") or 0)
    max_hp = float(player.get("max_hp") or 1)
    hp_ratio = hp / max_hp if max_hp > 0 else 0.0
    floor = _run_floor(state)
    deck = _player_deck_cards(state)
    basic_bloat = _basic_card_bloat(deck)
    descriptions = descriptions or {}
    thresholds = _port_thresholds_for_floor(floor)
    if descriptions:
        deck_totals = _deck_port_totals(state, descriptions)
    else:
        deck_totals = {key: 0.0 for key in thresholds}
    deficit_mass = 0.0
    for port, threshold in thresholds.items():
        deficit = max(0.0, float(threshold) - float(deck_totals.get(port) or 0.0))
        deficit_mass += min(deficit, 3.0)

    # Rest-site decisions should stay generalized: when the run is weak or near
    # a major checkpoint, healing becomes more valuable at higher HP ratios than
    # the old fixed 50% rule. Healthy/developed runs should still prefer smith.
    rest_threshold = 0.5
    if floor >= 12:
        rest_threshold = 0.72
    elif floor >= 8:
        rest_threshold = 0.64
    if basic_bloat >= 5:
        rest_threshold += 0.04
    elif basic_bloat <= 2 and deficit_mass <= 2.0:
        rest_threshold -= 0.03
    if deficit_mass >= 6.0:
        rest_threshold += 0.05
    elif deficit_mass >= 4.0:
        rest_threshold += 0.03
    if max_hp - hp <= 10:
        rest_threshold -= 0.03
    prefer_rest = hp_ratio < max(0.45, min(rest_threshold, 0.82))
    if not options:
        return {"option_index": 0}
    wanted_order = (("HEAL", "REST", "SMITH") if prefer_rest else ("SMITH", "HEAL", "REST"))
    for wanted in wanted_order:
        for opt in options:
            if str(opt.get("option_id") or "").upper() == wanted:
                return {"option_index": int(opt["index"])}
    return {"option_index": int(options[0]["index"])}


def event_context_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    player = state.get("player") or {}
    options = state.get("options") or []
    summarized = []
    for opt in options:
        summarized.append(
            {
                "index": opt.get("index"),
                "option_id": opt.get("option_id"),
                "label": opt.get("label"),
                "is_enabled": opt.get("is_enabled"),
            }
        )
    return {
        "hp": player.get("hp"),
        "max_hp": player.get("max_hp"),
        "gold": player.get("gold"),
        "options": summarized,
    }


def _event_option_text(opt: Dict[str, Any]) -> str:
    # Color/image markup is presentation metadata, not an effect. In
    # particular, a color tag must never be interpreted as gaining gold.
    visible_parts = [
        re.sub(r"\[[^\]]*\]", "", str(opt.get("title") or "")),
        re.sub(r"\[[^\]]*\]", "", str(opt.get("description") or "")),
        re.sub(r"\[[^\]]*\]", "", str(opt.get("label") or "")),
    ]
    option_id = str(opt.get("option_id") or "").upper()
    text = " ".join(visible_parts).upper()
    stable_effect_hints = {
        # Neow's relic-like choices use names rather than effect verbs in their
        # localization keys.  Add only effects verified from game data/API.
        "GOLDEN_PEARL": " GAIN GOLD RELIC",
        "NEW_LEAF": " TRANSFORM CARD RELIC",
    }
    for token, hint in stable_effect_hints.items():
        if token in option_id:
            text += hint
    return text


def _event_option_effects(opt: Dict[str, Any]) -> Dict[str, float | bool]:
    """Extract only explicit event costs/recovery from visible effect text."""

    description = re.sub(r"\[[^\]]*\]", "", str(opt.get("description") or ""))
    upper = description.upper()

    def first(patterns: Tuple[str, ...]) -> float:
        for pattern in patterns:
            match = re.search(pattern, description, re.I)
            if match:
                return float(match.group(1))
        return 0.0

    return {
        "max_hp_loss": first((
            r"失去\s*(\d+(?:\.\d+)?)\s*点?\s*最大生命",
            r"LOSE\s+(\d+(?:\.\d+)?)\s+(?:MAX|MAXIMUM)\s+HP",
        )),
        "hp_loss": first((
            r"失去\s*(\d+(?:\.\d+)?)\s*点?\s*生命(?!上限)",
            r"LOSE\s+(\d+(?:\.\d+)?)\s+HP",
            r"TAKE\s+(\d+(?:\.\d+)?)\s+DAMAGE",
        )),
        "heal": first((
            r"回复\s*(\d+(?:\.\d+)?)\s*点?\s*生命",
            r"恢复\s*(\d+(?:\.\d+)?)\s*点?\s*生命",
            r"HEAL\s+(\d+(?:\.\d+)?)",
        )),
        "will_kill_player": bool(opt.get("will_kill_player")) or "WILL KILL" in upper,
    }


def _event_option_is_safe(state: Dict[str, Any], opt: Dict[str, Any]) -> bool:
    player = state.get("player") or {}
    hp = float(player.get("hp") or 0)
    max_hp = float(player.get("max_hp") or 1)
    effects = _event_option_effects(opt)
    hp_loss = float(effects["hp_loss"])
    max_hp_loss = float(effects["max_hp_loss"])
    if effects["will_kill_player"]:
        return False
    # Simple run-level guardrails: do not let one event choice consume an
    # unbounded amount of health. Combat search semantics remain unchanged.
    if hp_loss >= hp or hp_loss > min(20.0, max_hp * 0.25):
        return False
    if hp_loss > 0 and hp - hp_loss < max(1.0, max_hp * 0.20):
        return False
    if max_hp_loss > 10.0 or max_hp - max_hp_loss < 30.0:
        return False
    return True


def _extract_first_number(text: str) -> float:
    match = re.search(r"(\d+)", text)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except Exception:
        return 0.0


def infer_card_selection_mode(
    option: Optional[Dict[str, Any]] = None,
    selection_kind: Optional[str] = None,
) -> Optional[str]:
    """Recover why a card-selection screen was opened.

    Selection purpose is part of the decision state.  Losing it causes the
    generic "take the best card" policy to do the exact opposite of what a
    remove/transform event requires.  The visible Mod exposes a stable
    ``selection.kind``; the headless event path falls back to stable option ids
    and, last, localized display text.
    """

    kind = str(selection_kind or "").upper()
    kind_signals = (
        ("REMOVE", "remove"),
        ("DELETE", "remove"),
        ("TRANSFORM", "transform"),
        ("UPGRADE", "upgrade"),
        ("SMITH", "upgrade"),
        ("COPY", "copy"),
        ("DUPLICATE", "copy"),
        ("ENCHANT", "enchant"),
        ("ADD", "pick"),
        ("CHOOSE", "pick"),
        ("REWARD", "pick"),
    )
    for token, mode in kind_signals:
        if token in kind:
            return mode

    if option is None:
        return None
    text = _event_option_text(option)
    # Named Neow rewards are stable identifiers but do not always describe the
    # effect in their key.  Keep these mappings explicit and auditable.
    named_option_modes = {
        "NEW_LEAF": "transform",
    }
    for token, mode in named_option_modes.items():
        if token in text:
            return mode

    option_signals = (
        (("REMOVE", "DELETE", "移除", "删除"), "remove"),
        (("TRANSFORM", "变化", "变换", "转换"), "transform"),
        (("UPGRADE", "SMITH", "升级", "锻造"), "upgrade"),
        (("COPY", "DUPLICATE", "复制"), "copy"),
        (("ENCHANT", "附魔"), "enchant"),
        (("CHOOSE", "ADD", "获得", "选择"), "pick"),
    )
    for tokens, mode in option_signals:
        if any(token in text for token in tokens):
            return mode
    return None


def selected_event_option(
    state: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    wanted = payload.get("option_index")
    if wanted is None:
        return None
    return next(
        (
            option
            for option in state.get("options") or []
            if option.get("index") is not None and int(option["index"]) == int(wanted)
        ),
        None,
    )


def _score_generic_event_option(state: Dict[str, Any], opt: Dict[str, Any]) -> float:
    player = state.get("player") or {}
    hp = float(player.get("hp") or 0)
    max_hp = float(player.get("max_hp") or 1)
    gold = float(player.get("gold") or 0)
    hp_ratio = hp / max_hp if max_hp > 0 else 1.0
    deck = _player_deck_cards(state)
    deck_size = len(deck)

    text = _event_option_text(opt)
    effects = _event_option_effects(opt)
    number = _extract_first_number(text)
    score = 0.0

    positive_signals = [
        ("RELIC", 20.0),
        ("RARE CARD", 13.0),
        ("UNCOMMON CARD", 6.0),
        ("UPGRADE", 12.0),
        ("升级", 12.0),
        ("REMOVE", 18.0),
        ("移除", 18.0),
        ("DELETE", 18.0),
        ("TRANSFORM", 10.0),
        ("变化", 10.0),
        ("GAIN GOLD", 5.0),
        ("获得金币", 5.0),
        ("HEAL", 8.0),
        ("回复", 8.0),
        ("GAIN MAX HP", 6.0),
        ("获得最大生命", 6.0),
    ]
    negative_signals = [
        ("CURSE", -20.0),
        ("LOSE MAX HP", -18.0),
        ("失去最大生命", -18.0),
        ("LOSE ALL GOLD", -20.0),
        ("LOSE GOLD", -9.0),
        ("LOSE HP", -12.0),
        ("失去生命", -12.0),
        ("TAKE DAMAGE", -12.0),
        ("DAMAGE", -8.0),
        ("WOUND", -8.0),
        ("DAZED", -6.0),
        ("BURN", -8.0),
        ("REGRET", -16.0),
        ("PAIN", -16.0),
        ("DOUBT", -15.0),
        ("SHAME", -15.0),
        ("DECAY", -18.0),
    ]

    for token, weight in positive_signals:
        if token in text:
            score += weight
    for token, weight in negative_signals:
        if token in text:
            score += weight

    if "REMOVE" in text and deck_size >= 13:
        score += 4.0
    if "UPGRADE" in text and hp_ratio >= 0.55:
        score += 2.5
    if "HEAL" in text or "回复" in text or "恢复" in text:
        score += max(0.0, (0.7 - hp_ratio) * 18.0)
    hp_loss = float(effects["hp_loss"])
    max_hp_loss = float(effects["max_hp_loss"])
    heal = float(effects["heal"])
    if hp_loss > 0:
        score -= max(0.0, (0.65 - hp_ratio) * 26.0)
        score -= 10.0 + min(hp_loss, 20.0) * 0.7
    if heal > 0:
        score += min(heal, max(0.0, max_hp - hp)) * 0.35
    if "LOSE GOLD" in text or "LOSE ALL GOLD" in text:
        score -= min(gold, max(number, gold if "ALL GOLD" in text else number)) * 0.05
    if max_hp_loss > 0:
        score -= 12.0 + min(max_hp_loss, 20.0) * 1.5
    if "CURSE" in text and "REMOVE" not in text:
        score -= 4.0
    if "NOTHING" in text and score < 0:
        score += 3.0

    return score


def choose_event_option(state: Dict[str, Any]) -> Dict[str, Any]:
    options = state.get("options") or []
    enabled = [
        opt for opt in options
        if not bool(opt.get("is_locked")) and opt.get("index") is not None
    ]
    if not enabled:
        return {"option_index": 0}

    safe = [opt for opt in enabled if _event_option_is_safe(state, opt)]
    if safe:
        enabled = safe
    else:
        nonlethal = [
            opt for opt in enabled
            if not _event_option_effects(opt)["will_kill_player"]
        ]
        if nonlethal:
            enabled = nonlethal

    if str(state.get("event_name") or state.get("event_id") or "").upper() != "NEOW":
        best = max(enabled, key=lambda opt: _score_generic_event_option(state, opt))
        return {"option_index": int(best["index"])}

    def score_option(opt: Dict[str, Any]) -> float:
        text = _event_option_text(opt)
        score = _score_generic_event_option(state, opt)

        positive_signals = [
            ("RANDOM RELIC", 22.0),
            ("DRAW", 8.0),
            ("ELITE COMBATS", 4.0),
            ("RARE CARD", 14.0),
            ("UPGRADE 1 OF YOUR STRIKES AND 1 OF YOUR DEFENDS", 15.0),
            ("UPGRADE A CARD", 14.0),
            ("BOSS DROPS", 13.0),
            ("CARD REWARDS YOU SEE ARE UPGRADED", 14.0),
            ("TRANSFORM 1 OF YOUR STRIKES AND 1 OF YOUR DEFENDS", 8.0),
            ("CHOOSE 1 OF 2 PACKS OF CARDS", 8.0),
        ]
        negative_signals = [
            ("LOSE ALL GOLD", -18.0),
            ("LOSE GOLD", -8.0),
            ("LOSE HP", -14.0),
            ("TAKE DAMAGE", -14.0),
            ("CURSE", -18.0),
            ("LOSE MAX HP", -14.0),
            ("EMPTY", -6.0),
            ("ADD AN ADDITIONAL STRIKE AND DEFEND", -10.0),
        ]

        for token, weight in positive_signals:
            if token in text:
                score += weight
        for token, weight in negative_signals:
            if token in text:
                score += weight

        return score

    best = max(enabled, key=score_option)
    return {"option_index": int(best["index"])}


def _legal_combat_actions(search_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Authoritative legal play_card actions for the current combat search-state.

    The engine enumerates every legal (card, target) move in
    `combat.available_actions`; each entry already carries card_index,
    target_index and metadata{card_id, target_type, target_monster_id}. This is
    the ONLY reliable source — the serialized `hand` cards do NOT include
    `can_play`, `index`, or `target_type` (their fields are card_id / upgrade /
    current_cost / keywords / affliction), so any fallback that filtered the hand
    on `can_play` silently matched nothing and passed the whole combat. Build all
    combat choosers off available_actions instead.
    """
    combat = search_state.get("combat") or {}
    actions = combat.get("available_actions") or []
    return [a for a in actions if (a or {}).get("action_type") == "play_card"]


def _action_payload(action: Dict[str, Any]) -> Dict[str, Any]:
    """Build an apply_action payload from an available_actions entry."""
    payload: Dict[str, Any] = {"card_index": action.get("card_index")}
    if action.get("target_index") is not None:
        payload["target_index"] = int(action["target_index"])
    return payload


def choose_combat_fallback(search_state: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    combat = search_state.get("combat") or {}
    player = combat.get("player") or {}
    enemies = combat.get("enemies") or []
    enemy_hp = sum(int(e.get("hp") or 0) for e in enemies)
    incoming = 0.0
    for enemy in enemies:
        intent = enemy.get("intent") or {}
        if intent_deals_damage(intent):
            incoming += intent_total_damage(intent)
    block = int(player.get("block") or 0)

    playable = _legal_combat_actions(search_state)
    if not playable:
        return "end_turn", {}

    def _card_id(a: Dict[str, Any]) -> str:
        return str((a.get("metadata") or {}).get("card_id") or "")

    # Block up if a hit is coming and we have a defend available.
    if incoming > block + 5:
        for a in playable:
            if _card_id(a).endswith("DEFEND_IRONCLAD"):
                return "play_card", _action_payload(a)
    # Otherwise prefer reliable damage.
    if enemy_hp > 0:
        for preferred in ("BASH", "STRIKE_IRONCLAD", "IRON_WAVE", "POMMEL_STRIKE"):
            for a in playable:
                if preferred in _card_id(a):
                    return "play_card", _action_payload(a)
    return "play_card", _action_payload(playable[0])


def choose_global_random(
    decision: str,
    state: Dict[str, Any],
    rng: "random.Random",
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Uniform-random legal choice for a global (non-combat) decision.

    Lower-bound baseline for the global agent. Returns (action, payload) in the
    same shape the heuristic choosers produce, or None to signal "no random
    override available" (caller should fall back to the heuristic path). Side
    effects in the main loop (map room-type, rest SMITH->upgrade) still run on
    the returned payload, so randomized choices stay consistent.
    """
    if decision in ("event_choice", "rest_site"):
        options = state.get("options") or []
        enabled = [o for o in options if not bool(o.get("is_locked")) and o.get("index") is not None]
        if not enabled:
            return None
        pick = rng.choice(enabled)
        return ("choose_option", {"option_index": int(pick["index"])})

    if decision == "map_select":
        choices = [
            c for c in (state.get("choices") or [])
            if c.get("col") is not None and c.get("row") is not None
        ]
        if not choices:
            return None
        pick = rng.choice(choices)
        return ("select_map_node", {"col": int(pick["col"]), "row": int(pick["row"])})

    if decision == "card_reward":
        cards = state.get("cards") or state.get("rewards") or []
        # include "skip" as one option so the random agent sometimes declines
        if not cards or rng.random() < 0.15:
            return ("skip_card_reward", {})
        idx = rng.randrange(len(cards))
        return ("select_card_reward", {"card_index": int((cards[idx] or {}).get("index", idx))})

    if decision == "card_select":
        cards = state.get("cards") or []
        if not cards:
            return ("skip_select", {})
        minimum = max(1, int(state.get("min_select") or 1))
        count = min(minimum, len(cards))
        chosen = rng.sample(list(enumerate(cards)), count)
        indices = [int((card or {}).get("index", position)) for position, card in chosen]
        return ("select_cards", {"indices": ",".join(map(str, indices))})

    if decision == "bundle_select":
        bundles = state.get("bundles") or []
        if not bundles:
            return None
        picked = rng.choice(bundles)
        return ("select_bundle", {"bundle_index": int(picked.get("index", 0))})

    return None


def choose_combat_naive(search_state: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    playable = _legal_combat_actions(search_state)
    if not playable:
        return "end_turn", {}
    return "play_card", _action_payload(playable[0])


def choose_combat_random(
    search_state: Dict[str, Any],
    rng: "random.Random",
) -> Tuple[str, Dict[str, Any]]:
    """True lower-bound baseline: uniformly random legal action.

    Picks uniformly among the engine's legal play_card moves plus end_turn.
    available_actions already enumerates each (card, target) pair as a distinct
    entry, so target selection is implicit. Deliberately ignores buffs, powers,
    intents, and value — it is the floor the search agent must beat. `rng` is a
    seeded random.Random for reproducible batch comparisons.
    """
    # end_turn is always a legal choice; include it as one option so the random
    # agent sometimes ends the turn instead of dumping its whole hand.
    options: List[Tuple[str, Dict[str, Any]]] = [("end_turn", {})]
    for action in _legal_combat_actions(search_state):
        options.append(("play_card", _action_payload(action)))
    return rng.choice(options)


def resolve_live_combat_payload(
    current_search_state: Dict[str, Any],
    action: str,
    payload: Dict[str, Any],
    chosen_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Compatibility wrapper for the backend-state action resolver."""
    return resolve_combat_payload(
        current_search_state, action, payload, chosen_metadata
    )


def resolve_live_planned_action(
    current_search_state: Dict[str, Any],
    chosen: SearchAction,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Compatibility wrapper; resolution is not client-specific."""
    return resolve_planned_action(current_search_state, chosen)


def summarize_live_combat_action(
    current_search_state: Dict[str, Any],
    action: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Compatibility wrapper; summaries use backend action semantics."""
    return summarize_combat_action(current_search_state, action, payload)


def resolved_combat_action_summary(
    current_search_state: Dict[str, Any],
    action: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Semantic summary of the payload actually applied on the live board.

    The searcher's chosen indices refer to the searched snapshot. Before
    applying in run_agent, the action is resolved against the live hand/enemy
    list, so payload indices may legitimately differ. Logging the resolved
    semantic action keeps transition audits from treating index remapping as
    behavior drift.
    """
    summarized = summarize_live_combat_action(current_search_state, action, payload)
    return {
        "action_type": summarized.get("action_type"),
        "card_index": summarized.get("card_index"),
        "target_index": summarized.get("target_index"),
        "metadata": summarized.get("metadata") or {},
    }


def _turn_plan_marker(search_state: Dict[str, Any]) -> Tuple[Any, ...]:
    combat = search_state.get("combat") or {}
    return (
        combat.get("round_number"),
        combat.get("turn_number"),
        bool(combat.get("is_player_turn")),
    )


def restart_cli_from_exact_save(
    cli: Sts2CliAdapter,
    cli_cfg: CliConfig,
    lang: str,
    save_tag: str,
) -> Tuple[Sts2CliAdapter, Dict[str, Any]]:
    save_path = Path(tempfile.gettempdir()) / f"sts2_agent_{save_tag}.save"
    save_result = cli.write_exact_save(str(save_path))
    if not save_result.get("success"):
        raise RuntimeError(f"Failed to write exact save: {save_result}")
    cli.stop()
    fresh_cli = Sts2CliAdapter(cli_cfg)
    fresh_cli.start()
    state = fresh_cli.load_save(str(save_path), lang=lang)
    if state.get("type") == "error":
        raise RuntimeError(f"Failed to load exact save: {state}")
    return fresh_cli, state


def _normalize_cli_state(result: Dict[str, Any]) -> Dict[str, Any]:
    if result.get("type") == "search_state_result" and result.get("success"):
        combat_state = result.get("combat_state_for_search")
        if isinstance(combat_state, dict) and combat_state:
            return combat_state
    return result


def _refresh_decision_state(cli: Sts2CliAdapter, timeout_s: float) -> Dict[str, Any]:
    last_error: Optional[BaseException] = None
    for fetch in (cli.get_search_state, cli.get_map):
        try:
            refreshed = _normalize_cli_state(fetch(timeout_s=timeout_s))
        except Exception as exc:
            last_error = exc
            continue
        if refreshed.get("type") == "error":
            return refreshed
        if refreshed.get("decision") is not None:
            return refreshed
    if last_error is not None:
        raise last_error
    return {}


def apply_transition_action_with_recovery(
    cli: Sts2CliAdapter,
    cli_cfg: CliConfig,
    current_state: Dict[str, Any],
    decision: str,
    action: str,
    payload: Dict[str, Any],
    lang: str,
    save_tag: str,
    timeout_s: float = 8.0,
    assume_transition_fresh: bool = False,
) -> Tuple[Sts2CliAdapter, Dict[str, Any], float, float]:
    restart_cli_ms = 0.0
    action_started = time.perf_counter()
    if decision != "map_select":
        if decision == "combat_play":
            # Re-sync the long-running in-place CLI before applying a combat action.
            # The in-place process drifts: a targeted `use_potion` silently no-ops
            # (potion not consumed, debuff not applied), while export->import to a
            # fresh engine applies it correctly. Because the searcher reasons over
            # the clean exported snapshot, it keeps choosing a potion the in-place
            # engine refuses to execute -> the fight spins forever on an unchanging
            # state. Laundering the drift through a capture/export/import/restore
            # round-trip restores a correct engine, so the chosen action applies on
            # the same clean state the searcher planned against. This costs an extra
            # snapshot round-trip per combat action but is required for correctness.
            resync_id = f"presync_{save_tag}"
            capture = cli.capture_combat_snapshot(resync_id)
            if capture.get("success"):
                exported = cli.export_combat_snapshot(resync_id)
                if exported.get("success"):
                    # Rollback point: if the import/restore round-trip errors, the
                    # engine is left empty/corrupt (deck vanished, enemy HP reset
                    # to max) and acting on it silently wipes the fight. Stash an
                    # exact save first so we can reconstruct the pre-resync engine.
                    rollback_path = Path(tempfile.gettempdir()) / f"sts2_resync_rollback_{save_tag}.save"
                    rollback_ok = cli.write_exact_save(str(rollback_path)).get("success")

                    def _reload_rollback_in_fresh_cli(reason: str) -> Dict[str, Any]:
                        nonlocal cli, restart_cli_ms
                        restart_started = time.perf_counter()
                        try:
                            cli.stop()
                        except Exception:
                            pass
                        fresh_cli = Sts2CliAdapter(cli_cfg)
                        fresh_cli.start()
                        loaded = fresh_cli.load_save(str(rollback_path), lang=lang)
                        if loaded.get("type") == "error":
                            try:
                                fresh_cli.stop()
                            except Exception:
                                pass
                            raise RuntimeError(
                                f"Failed to reload rollback exact save after combat resync {reason}: {loaded}"
                            )
                        cli = fresh_cli
                        restart_cli_ms += (time.perf_counter() - restart_started) * 1000.0
                        return loaded

                    imported = cli.import_combat_snapshot(exported["snapshot_json"], resync_id)
                    restored = cli.restore_combat_snapshot(resync_id, allow_full=False)
                    restore_failed = (
                        not imported.get("success")
                        or restored.get("type") == "error"
                    )
                    if not restore_failed and restored.get("restore_mode") == "full":
                        # Full combat-snapshot restore rebuilds a combat-only test
                        # run and drops live run-level map context (ActFloor /
                        # CurrentMapCoord). That is fine for search workers, but
                        # corrupts the live run after the combat ends. Use the
                        # exact-save rollback instead; it preserves the real run.
                        print(json.dumps({
                            "warning": "combat_resync_full_restore_rolled_back",
                            "save_tag": save_tag,
                            "action": action,
                            "note": "full snapshot restore would drop live map context; reloaded exact rollback before applying action",
                        }, ensure_ascii=False))
                        if rollback_ok:
                            _reload_rollback_in_fresh_cli("full_restore")
                            restore_failed = False
                        else:
                            restore_failed = True
                    if restore_failed:
                        # Loud flag: a restore failure is a snapshot defect to fix,
                        # not something to mask. Recover the pre-resync engine from
                        # the rollback save so we apply the action on a sane (if
                        # drifted) state instead of a corrupt one.
                        print(json.dumps({
                            "warning": "combat_resync_restore_failed",
                            "save_tag": save_tag,
                            "action": action,
                            "import_success": imported.get("success"),
                            "restore_type": restored.get("type"),
                            "restore_message": restored.get("message"),
                            "recovered_from_rollback": rollback_ok,
                            "note": "engine corrupted by resync round-trip; rolled back to pre-resync exact save",
                        }, ensure_ascii=False))
                        if rollback_ok:
                            _reload_rollback_in_fresh_cli("restore_failed")
        result = _normalize_cli_state(cli.action(action, args=payload, with_snapshot=False))
        return cli, result, restart_cli_ms, (time.perf_counter() - action_started) * 1000.0

    prior_context = current_state.get("context") or {}
    prior_floor = current_state.get("floor") or prior_context.get("floor")
    prior_act = current_state.get("act") or prior_context.get("act")
    prior_choices = current_state.get("choices")

    def _map_transition_complete(result: Dict[str, Any]) -> bool:
        if result.get("type") == "error":
            return True
        next_decision = result.get("decision")
        next_context = result.get("context") or {}
        next_floor = result.get("floor") or next_context.get("floor")
        next_act = result.get("act") or next_context.get("act")
        if next_decision is not None and str(next_decision) != "map_select":
            return True
        if str(next_decision) == "map_select":
            if next_floor != prior_floor or next_act != prior_act:
                return True
            if result.get("choices") != prior_choices:
                return True
        return False

    save_path = Path(tempfile.gettempdir()) / f"sts2_agent_{save_tag}.save"
    save_result = cli.write_exact_save(str(save_path))
    if not save_result.get("success"):
        raise RuntimeError(f"Failed to write exact save before transition action: {save_result}")
    if not assume_transition_fresh:
        restart_started = time.perf_counter()
        try:
            cli.stop()
        except Exception:
            pass
        cli = Sts2CliAdapter(cli_cfg)
        cli.start()
        loaded = cli.load_save(str(save_path), lang=lang)
        if loaded.get("type") == "error":
            raise RuntimeError(f"Failed to reload exact save before transition action: {loaded}")
        restart_cli_ms = (time.perf_counter() - restart_started) * 1000.0
    try:
        result = _normalize_cli_state(cli.action(action, args=payload, with_snapshot=False, timeout_s=timeout_s))
        if not _map_transition_complete(result):
            result = _refresh_decision_state(cli, timeout_s=timeout_s)
        if not _map_transition_complete(result):
            raise TimeoutError("map_select transition did not advance after action")
        return cli, result, restart_cli_ms, (time.perf_counter() - action_started) * 1000.0
    except TimeoutError:
        restart_started = time.perf_counter()
        try:
            cli.stop()
        except Exception:
            pass
        fresh_cli = Sts2CliAdapter(cli_cfg)
        fresh_cli.start()
        loaded = fresh_cli.load_save(str(save_path), lang=lang)
        if loaded.get("type") == "error":
            try:
                fresh_cli.stop()
            except Exception:
                pass
            try:
                fallback = _refresh_decision_state(cli, timeout_s=timeout_s)
            except Exception:
                fallback = {}
            if _map_transition_complete(fallback):
                return cli, fallback, restart_cli_ms, (time.perf_counter() - action_started) * 1000.0
            raise RuntimeError(f"Failed to reload exact save after transition timeout: {loaded}")
        result = _normalize_cli_state(fresh_cli.action(action, args=payload, with_snapshot=False, timeout_s=timeout_s))
        if not _map_transition_complete(result):
            result = _refresh_decision_state(fresh_cli, timeout_s=timeout_s)
        if not _map_transition_complete(result):
            raise RuntimeError("map_select transition remained incomplete after restart retry")
        restart_cli_ms = (time.perf_counter() - restart_started) * 1000.0
        return fresh_cli, result, restart_cli_ms, (time.perf_counter() - action_started) * 1000.0


def combat_summary(search_state: Dict[str, Any]) -> Dict[str, Any]:
    combat = search_state.get("combat") or {}
    player = combat.get("player") or {}
    enemies = combat.get("enemies") or []
    return {
        "round": combat.get("round_number"),
        "turn": combat.get("turn_number"),
        "hp": player.get("hp"),
        "block": player.get("block"),
        "energy": player.get("energy"),
        "enemy_hp": [e.get("hp") for e in enemies],
        "enemy_ids": [e.get("monster_id") for e in enemies],
    }


def write_presave(
    cli: "Sts2CliAdapter",
    presave_dir: Optional[str],
    *,
    seed: str,
    character: str,
    step_id: int,
    kind: str,
    state: Dict[str, Any],
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Persist the engine's NATIVE exact-save at a pre-room decision point.

    Unlike --capture-hard-combats (which exports an in-combat snapshot with the
    opening hand already dealt and frozen), this writes the engine's faithful
    save BEFORE the room is entered. A replay tool can then load_save -> reseed
    Shuffle -> enter_room so the opening hand is re-randomized per draw.

    Writes <seed>_<step>_<kind>.save and appends one line to
    presave_manifest.jsonl. Never raises into the run loop — collection must not
    break a run. Returns the manifest record (also stored on the event) or None.
    """
    if not presave_dir:
        return None
    try:
        d = Path(presave_dir).resolve()
        d.mkdir(parents=True, exist_ok=True)
        save_name = f"{seed}_{step_id}_{kind}.save"
        # Absolute path: the engine subprocess resolves relative paths against
        # ITS cwd (third_party/sts2-cli), not ours, so a relative path silently
        # lands in the wrong tree. The manifest still stores the bare save_name.
        save_path = d / save_name
        res = cli.write_exact_save(str(save_path))
        if not res.get("success"):
            return {"presave_error": f"write_exact_save failed: {res}"}
        ctx = state.get("context") or {}
        rec: Dict[str, Any] = {
            "seed": seed,
            "character": character,
            "step_id": step_id,
            "kind": kind,
            "save": save_name,
            "floor": ctx.get("floor") or state.get("floor"),
            "act": ctx.get("act") or state.get("act"),
            "decision": state.get("decision"),
        }
        if extra:
            rec.update(extra)
        with (d / "presave_manifest.jsonl").open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception as exc:  # never let collection break a run
        return {"presave_error": str(exc)}


def _incoming_intent_damage(search_state: Dict[str, Any]) -> Tuple[float, List[Dict[str, Any]]]:
    """Sum the attack damage the enemies are telegraphing for the next enemy turn.

    Mirrors the intent reading in choose_combat_fallback: a damage intent's
    Threatened damage uses explicit total_damage when available, with a legacy
    display_damage * hits fallback. Returns the total plus a
    per-enemy breakdown (hp + intent) so a turn log can tell a single big hit
    (burst) apart from steady chip (attrition).
    """
    combat = search_state.get("combat") or {}
    enemies = combat.get("enemies") or []
    per_enemy: List[Dict[str, Any]] = []
    total = 0.0
    for enemy in enemies:
        intent = enemy.get("intent") or {}
        intent_types = list(intent.get("intent_types") or [])
        damage = 0.0
        if intent_deals_damage(intent):
            damage = intent_total_damage(intent)
        total += damage
        per_enemy.append(
            {
                "monster_id": enemy.get("monster_id"),
                "hp": enemy.get("hp"),
                "block": enemy.get("block"),
                "intent_types": intent_types,
                "total_damage": intent.get("total_damage"),
                "display_damage": intent.get("display_damage"),
                "hits": intent.get("hits"),
                "incoming_damage": damage,
            }
        )
    return total, per_enemy


def _seed_to_int(seed: Any) -> int:
    """Map a run seed (possibly a non-numeric string) to a stable int for RNG.

    Numeric seeds map to themselves; anything else hashes deterministically so
    the random baselines stay reproducible across runs of the same seed.
    """
    try:
        return int(seed)
    except (TypeError, ValueError):
        import hashlib
        return int(hashlib.sha256(str(seed).encode("utf-8")).hexdigest(), 16) % (2**31)


def combat_full_board(search_state: Dict[str, Any]) -> Dict[str, Any]:
    """Complete, human-readable board state for decision-review traces.

    Unlike combat_turn_snapshot (per-turn HP/intent summary), this captures the
    FULL information a human needs to judge a play: every card in hand (with cost
    and upgrade), the draw/discard/exhaust pile contents, player powers/relics,
    and each enemy's hp/block/intent/powers. Emitted once per player action when
    --dump-combat-trace is on.
    """
    combat = search_state.get("combat") or {}
    player = combat.get("player") or {}
    enemies = combat.get("enemies") or []
    total_incoming, enemy_intents = _incoming_intent_damage(search_state)

    def _cards(key: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for c in (combat.get(key) or []):
            if not isinstance(c, dict):
                continue
            out.append({
                "card_id": c.get("card_id"),
                "cost": c.get("current_cost"),
                "upgrade": c.get("upgrade"),
                "keywords": c.get("keywords") or None,
                "affliction": c.get("affliction") or None,
            })
        return out

    return {
        "round": combat.get("round_number"),
        "turn": combat.get("turn_number"),
        "is_player_turn": combat.get("is_player_turn"),
        "player": {
            "hp": player.get("hp"),
            "max_hp": player.get("max_hp"),
            "block": player.get("block"),
            "energy": player.get("energy"),
            "powers": player.get("powers") or [],
            "relics": player.get("relics") or [],
        },
        "hand": _cards("hand"),
        "draw_pile": _cards("draw_pile"),
        "discard_pile": _cards("discard_pile"),
        "exhaust_pile": _cards("exhaust_pile"),
        "enemies": [
            {
                "monster_id": e.get("monster_id"),
                "hp": e.get("hp"),
                "max_hp": e.get("max_hp"),
                "block": e.get("block"),
                "intent": e.get("intent"),
                "powers": e.get("powers") or [],
            }
            for e in enemies
        ],
        "incoming_damage": total_incoming,
        "enemy_intents": enemy_intents,
    }


def combat_turn_snapshot(search_state: Dict[str, Any]) -> Dict[str, Any]:
    """Per-turn combat state captured at the first player action of each turn.

    Records the turn-start player HP/block/energy, every enemy's HP, and the
    incoming intent damage telegraphed for the next enemy turn. The HP delta
    between consecutive turn snapshots is the damage actually absorbed, while
    incoming_damage is the damage threatened — together they distinguish burst
    losses from attrition losses.
    """
    combat = search_state.get("combat") or {}
    player = combat.get("player") or {}
    enemies = combat.get("enemies") or []
    total_incoming, enemy_intents = _incoming_intent_damage(search_state)
    return {
        "round": combat.get("round_number"),
        "turn": combat.get("turn_number"),
        "hp": player.get("hp"),
        "max_hp": player.get("max_hp"),
        "block": player.get("block"),
        "energy": player.get("energy"),
        "enemy_hp": [e.get("hp") for e in enemies],
        "enemy_ids": [e.get("monster_id") for e in enemies],
        "incoming_damage": total_incoming,
        "enemy_intents": enemy_intents,
    }


def _player_deck_cards(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    player = state.get("player") or {}
    return list(player.get("deck") or [])


def _deck_scalars(state: Dict[str, Any]) -> Dict[str, Any]:
    """Objective, non-subjective deck descriptors for ablation logging.

    Pure counts — no value judgement. Used only as reference context when
    comparing agents; never as an optimization target (per the agreed metric
    policy: deck/event choices are recorded but not analyzed subjectively).
    """
    deck = _player_deck_cards(state)
    type_counts: Dict[str, int] = {}
    upgrades = 0
    for c in deck:
        ctype = str(c.get("type") or "Unknown")
        type_counts[ctype] = type_counts.get(ctype, 0) + 1
        if c.get("upgrade") or c.get("upgraded") or "+" in str(c.get("card_id") or ""):
            upgrades += 1
    return {
        "deck_size": len(deck),
        "type_counts": type_counts,
        "curses": type_counts.get("Curse", 0),
        "upgrades": upgrades,
    }


def _run_floor(state: Dict[str, Any]) -> int:
    context = state.get("context") or {}
    try:
        return int(context.get("floor") or 0)
    except Exception:
        return 0


def _profile_act(state: Dict[str, Any]) -> Optional[int]:
    context = state.get('context') or {}
    try:
        value = context.get('act', state.get('act'))
        if value is not None:
            return int(value)
        run = state.get('run') or {}
        if run.get('act_id') is not None:
            return int(run['act_id']) + 1
    except (TypeError, ValueError):
        pass
    return None


def _base_card_ratings(repo_root: Path) -> Dict[str, float]:
    import csv
    path = repo_root / "data" / "card_stats" / "gamersky_sts2_card_stats.csv"
    ratings: Dict[str, float] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if str(row.get("character_query") or "") != "铁甲战士":
                continue
            card_id = _normalize_card_id(str(row.get("cardEnName") or ""))
            try:
                pick = float(row.get("avgPickRate") or 0.0)
                win = float(row.get("avgWinRate") or 0.0)
                up = float(row.get("upgradeRate") or 0.0)
            except ValueError:
                pick = win = up = 0.0
            ratings[card_id] = pick * 0.6 + win * 0.3 + up * 0.1
    return ratings


def _card_upgrade_ratings(repo_root: Path) -> Dict[str, float]:
    import csv
    path = repo_root / "data" / "card_stats" / "gamersky_sts2_card_stats.csv"
    ratings: Dict[str, float] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if str(row.get("character_query") or "") != "铁甲战士":
                continue
            card_id = _normalize_card_id(str(row.get("cardEnName") or ""))
            try:
                upgrade_rate = float(row.get("upgradeRate") or 0.0)
                win_rate = float(row.get("avgWinRate") or 0.0)
            except ValueError:
                upgrade_rate = 0.0
                win_rate = 0.0
            ratings[card_id] = upgrade_rate * 0.8 + win_rate * 0.2
    return ratings


def _card_descriptions(repo_root: Path) -> Dict[str, str]:
    path = repo_root / "third_party" / "sts2-cli" / "localization_eng" / "cards.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    descriptions: Dict[str, str] = {}
    for key, value in data.items():
        if not key.endswith(".description"):
            continue
        card_id = _normalize_card_id(key[:-12])
        descriptions[card_id] = str(value or "")
    return descriptions


def _count_deck_exhaust_synergy_sources(deck: List[Dict[str, Any]], descriptions: Dict[str, str]) -> int:
    count = 0
    for c in deck:
        cid = _normalize_card_id(str(c.get("id") or c.get("card_id") or c.get("name") or ""))
        text = str(c.get("description") or descriptions.get(cid, "")).upper()
        if "EXHAUST" in text:
            count += 1
    return count


def _count_deck_vulnerable_sources(deck: List[Dict[str, Any]], descriptions: Dict[str, str]) -> int:
    count = 0
    for c in deck:
        cid = _normalize_card_id(str(c.get("id") or c.get("card_id") or c.get("name") or ""))
        text = str(c.get("description") or descriptions.get(cid, "")).upper()
        if "VULNERABLE" in text:
            count += 1
    return count


def _adjust_card_value_for_run(
    state: Dict[str, Any],
    card: Dict[str, Any],
    base_score: float,
    descriptions: Optional[Dict[str, str]] = None,
) -> float:
    descriptions = descriptions or {}
    floor = _run_floor(state)
    deck = _player_deck_cards(state)
    extra_cards = max(0, len(deck) - 10)
    card_type = str(card.get("type") or "")
    rarity = str(card.get("rarity") or "")
    description = str(card.get("description") or descriptions.get(_normalize_card_id(str(card.get("card_id") or card.get("id") or card.get("name") or "")), ""))
    upper_desc = description.upper()
    try:
        cost = int(card.get("cost") if card.get("cost") is not None else 99)
    except Exception:
        cost = 99

    power_count = sum(1 for c in deck if str(c.get("type") or "") == "Power")
    attack_count = sum(1 for c in deck if str(c.get("type") or "") == "Attack")
    exhaust_sources = _count_deck_exhaust_synergy_sources(deck, descriptions)
    vulnerable_sources = _count_deck_vulnerable_sources(deck, descriptions)
    card_ports = _card_port_contributions(card, descriptions)
    deck_ports = _deck_port_totals(state, descriptions)
    port_thresholds = _port_thresholds_for_floor(floor)
    energy_deficit = max(0.0, float(port_thresholds.get("energy") or 0.0) - float(deck_ports.get("energy") or 0.0))
    hp = float((state.get("player") or {}).get("hp") or 0.0)
    max_hp = float((state.get("player") or {}).get("max_hp") or 1.0)
    hp_ratio = hp / max_hp if max_hp > 0 else 1.0

    score = base_score
    if floor and floor <= 6:
        if card_type == "Power":
            score -= 8.0
            if extra_cards < 3:
                score -= 3.0
            if power_count >= 1:
                score -= 2.5
        if cost >= 2:
            score -= 4.0
        elif cost == 0:
            score += 1.5
        elif cost == 1:
            score += 1.0
        if rarity == "Common":
            score += 0.5

    if floor and floor <= 4 and attack_count <= 6:
        if card_type == "Attack":
            score += 11.0
            if cost <= 1:
                score += 2.0
            if rarity == "Common":
                score += 1.0
        elif card_type == "Skill" and rarity != "Common":
            score -= 8.0

    if floor and floor <= 5 and "LOSE" in upper_desc and "HP" in upper_desc:
        lose_hp_penalty = 16.0
        if float(card_ports.get("energy") or 0.0) > 0.0:
            # Early energy is still a missing capability for many weak runs.
            # Keep a real HP-risk penalty, but let the port model surface these
            # cards when the deck is energy-starved and the current health is
            # not already collapsing.
            mitigation = 4.0 + min(4.0, energy_deficit * 4.0)
            if hp_ratio >= 0.45:
                mitigation += 2.0
            lose_hp_penalty = max(6.0, lose_hp_penalty - mitigation)
        score -= lose_hp_penalty

    if floor and floor <= 4 and "IF YOU EXHAUSTED A CARD THIS TURN" in upper_desc and exhaust_sources == 0:
        score -= 10.0
    if floor and floor <= 4 and "WHENEVER YOU EXHAUST" in upper_desc and exhaust_sources == 0:
        score -= 8.0
    if floor and floor <= 4 and card_type == "Power" and "VULNERABLE" in upper_desc and "DRAW" in upper_desc and vulnerable_sources <= 1:
        score -= 8.0

    if floor and floor <= 5 and cost == 0 and card_type == "Skill":
        if (
            "BLOCK" not in upper_desc
            and "DRAW" not in upper_desc
            and "EXHAUST" not in upper_desc
            and float(card_ports.get("energy") or 0.0) <= 0.0
        ):
            score -= 8.0

    return score


def _port_thresholds_for_floor(floor: int) -> Dict[str, float]:
    if floor <= 4:
        return {
            "attack": 24.0,
            "aoe": 1.0,
            "block": 16.0,
            "vulnerable": 1.0,
            "draw": 0.5,
            "energy": 0.5,
            "exhaust": 0.5,
            "scaling": 0.5,
            "control": 0.5,
        }
    if floor <= 8:
        return {
            "attack": 34.0,
            "aoe": 2.0,
            "block": 24.0,
            "vulnerable": 1.0,
            "draw": 1.5,
            "energy": 1.0,
            "exhaust": 1.0,
            "scaling": 1.5,
            "control": 1.0,
        }
    return {
        "attack": 42.0,
        "aoe": 2.5,
        "block": 30.0,
        "vulnerable": 1.0,
        "draw": 2.0,
        "energy": 1.0,
        "exhaust": 1.5,
        "scaling": 2.5,
        "control": 1.5,
    }


def _card_port_contributions(card: Dict[str, Any], descriptions: Dict[str, str]) -> Dict[str, float]:
    card_id = _normalize_card_id(str(card.get("card_id") or card.get("id") or card.get("name") or ""))
    card_type = str(card.get("type") or "")
    try:
        cost = int(card.get("cost") if card.get("cost") is not None else card.get("energy_cost") or 99)
    except Exception:
        cost = 99
    description = str(card.get("description") or descriptions.get(card_id, "")).upper()
    stats = card.get("stats") or {}
    damage = float(stats.get("damage") or 0.0)
    block = float(stats.get("block") or 0.0)
    contributions = {
        "attack": 0.0,
        "aoe": 0.0,
        "block": 0.0,
        "vulnerable": 0.0,
        "draw": 0.0,
        "energy": 0.0,
        "exhaust": 0.0,
        "scaling": 0.0,
        "control": 0.0,
    }
    if card_type == "Attack" or damage > 0:
        contributions["attack"] += max(0.0, damage / max(cost, 1))
        if cost <= 1:
            contributions["attack"] += 1.5
    if block > 0:
        contributions["block"] += block / max(cost, 1)
    if "VULNERABLE" in description:
        contributions["vulnerable"] += 1.0
        contributions["control"] += 0.4
    if "WEAK" in description:
        contributions["control"] += 1.0
    if "DRAW" in description:
        contributions["draw"] += 1.0
    if (
        ("ALL ENEMIES" in description or "EACH ENEMY" in description or "ALL OTHER ENEMIES" in description)
        and (card_type == "Attack" or damage > 0 or "DAMAGE" in description)
    ):
        contributions["aoe"] += 1.5 + max(0.0, damage / max(cost, 1)) * 0.4
    if "EXHAUST" in description:
        contributions["exhaust"] += 1.0
    if "ENERGY" in description or "ENERGYICONS" in description:
        contributions["energy"] += 1.0
    if card_type == "Power":
        contributions["scaling"] += 1.2
    if "STRENGTH" in description:
        contributions["scaling"] += 0.9
    if "DEXTERITY" in description:
        contributions["scaling"] += 0.8
    if "PLATED" in description or "METALLICIZE" in description:
        contributions["scaling"] += 0.8
    if "RITUAL" in description:
        contributions["scaling"] += 0.8
    if "LOSE HP" in description and ("ENERGY" in description or "STRENGTH" in description):
        contributions["scaling"] += 0.4
    return contributions


def _deck_port_totals(state: Dict[str, Any], descriptions: Dict[str, str]) -> Dict[str, float]:
    totals = {
        name: 0.0
        for name in ("attack", "aoe", "block", "vulnerable", "draw", "energy", "exhaust", "scaling", "control")
    }
    for card in _player_deck_cards(state):
        for port, value in _card_port_contributions(card, descriptions).items():
            totals[port] += float(value)
    return totals


def _basic_card_bloat(deck: List[Dict[str, Any]]) -> int:
    count = 0
    for card in deck:
        cid = _normalize_card_id(str(card.get("id") or card.get("card_id") or card.get("name") or ""))
        if cid in {"STRIKE_IRONCLAD", "DEFEND_IRONCLAD"} and not bool(card.get("upgraded")):
            count += 1
    return count

def choose_card_reward(state: Dict[str, Any], repo_root: Path,
                       deck_profile: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    cards = state.get("cards") or state.get("rewards") or []
    if not cards:
        return None
    deck = _player_deck_cards(state)
    best, _ = choose_profile_reward(deck_profile or _default_deck_profile(), cards, deck,
                                    act=_profile_act(state))
    if best is None:
        if state.get('can_skip') is False:
            return choose_forced_card_reward(state, repo_root, deck_profile)
        return None
    return {'card_index': int(best['index'])}


def choose_forced_card_reward(state: Dict[str, Any], repo_root: Path,
                              deck_profile: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Mandatory acquisitions may relax caps/whitelist, never optional rewards."""
    cards = state.get('cards') or state.get('rewards') or []
    if not cards:
        return None
    order = profile_card_priority(deck_profile or _default_deck_profile())
    ratings = _base_card_ratings(repo_root)
    ranked = sorted(enumerate(cards), key=lambda pair: (
        card_id_from_row(pair[1]) not in order,
        order.get(card_id_from_row(pair[1]), 9999),
        -float(ratings.get(card_id_from_row(pair[1]), 0.0)), pair[0]))
    index, card = ranked[0]
    return {'card_index': int(card.get('index', index))}


def _selection_count(state: Dict[str, Any], default: int = 1) -> int:
    """Return the mandatory number of cards for an atomic selection.

    Headless states provide authoritative min/max values.  Some older visible
    Mod builds report 0/0 for deck grids; callers adapting those states retain
    the safe one-click default and let the native UI enforce the transaction.
    """

    try:
        minimum = int(state.get("min_select") or 0)
    except (TypeError, ValueError):
        minimum = 0
    try:
        maximum = int(state.get("max_select") or 0)
    except (TypeError, ValueError):
        maximum = 0
    count = minimum if minimum > 0 else default
    if maximum > 0:
        count = min(count, maximum)
    return max(1, count)


def _selection_payload(cards: List[Dict[str, Any]], ranked: List[int], count: int) -> Optional[Dict[str, Any]]:
    chosen = ranked[: min(count, len(ranked))]
    if not chosen:
        return None
    return {
        "indices": ",".join(str(index) for index in chosen),
        "card_id": _normalize_card_id(
            str(
                (cards[chosen[0]] or {}).get("id")
                or (cards[chosen[0]] or {}).get("card_id")
                or (cards[chosen[0]] or {}).get("name")
                or ""
            )
        ),
        "card_ids": [
            _normalize_card_id(
                str(
                    (cards[index] or {}).get("id")
                    or (cards[index] or {}).get("card_id")
                    or (cards[index] or {}).get("name")
                    or ""
                )
            )
            for index in chosen
        ],
    }


def choose_card_select_pick(state: Dict[str, Any], repo_root: Path,
                            deck_profile: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    cards = state.get("cards") or []
    if not cards:
        return None
    profile = deck_profile or _default_deck_profile()
    deck_ids = deck_card_ids(_player_deck_cards(state))
    scored: List[Tuple[Tuple[int, ...], int]] = []
    for idx, card in enumerate(cards):
        result = score_profile_card(profile, card_id_from_row(card), deck_ids,
                                    act=_profile_act(state))
        if result['eligible']:
            scored.append((tuple(result['priority_rank']), idx))
    ranked = [idx for _, idx in sorted(scored, key=lambda row: (row[0], row[1]))]
    count = _selection_count(state)
    mandatory = state.get('can_skip') is False or (state.get('can_skip') is not True and int(state.get('min_select') or 0) > 0)
    if mandatory and len(ranked) < count:
        order = profile_card_priority(profile)
        ratings = _base_card_ratings(repo_root)
        remaining = sorted((idx for idx in range(len(cards)) if idx not in ranked), key=lambda idx: (
            card_id_from_row(cards[idx]) not in order, order.get(card_id_from_row(cards[idx]), 9999),
            -float(ratings.get(card_id_from_row(cards[idx]), 0.0)), idx))
        ranked.extend(remaining)
    return _selection_payload(cards, ranked, _selection_count(state))


def _upgraded_card_variant(card: Dict[str, Any]) -> Dict[str, Any]:
    upgraded = dict(card)
    after = dict(card.get("after_upgrade") or {})
    if after:
        if "cost" in after:
            upgraded["cost"] = after.get("cost")
        if "stats" in after and isinstance(after.get("stats"), dict):
            upgraded["stats"] = dict(after.get("stats") or {})
        if "description" in after:
            upgraded["description"] = after.get("description")
        added = list(after.get("added_keywords") or [])
        removed = set(after.get("removed_keywords") or [])
        base_keywords = list(card.get("keywords") or [])
        upgraded["keywords"] = [kw for kw in base_keywords if kw not in removed] + [kw for kw in added if kw not in base_keywords]
    upgraded["upgraded"] = True
    return upgraded


def choose_card_select_upgrade(state: Dict[str, Any], repo_root: Path) -> Optional[Dict[str, Any]]:
    cards = state.get("cards") or []
    if not cards:
        return None
    descriptions = _card_descriptions(repo_root)
    upgrade_ratings = _card_upgrade_ratings(repo_root)
    deck = _player_deck_cards(state)
    deck_size = len(deck)
    floor = _run_floor(state)
    candidates = [(idx, card) for idx, card in enumerate(cards)
                  if card_id_from_row(card) not in IRONCLAD_STARTER_CARD_IDS]
    # This is a mandatory native selection. Prefer non-starter cards, but do
    # not return an empty command when the game only offers starter cards.
    # The caller must submit a legal selection to leave the screen.
    if len(candidates) < _selection_count(state):
        candidates = list(enumerate(cards))
    scored: List[Tuple[float, int]] = []
    for idx, card in candidates:
        upgraded = _upgraded_card_variant(card)
        card_id = _normalize_card_id(str(card.get("id") or card.get("card_id") or card.get("name") or ""))
        score = float(upgrade_ratings.get(card_id, 0.0))
        current_stats = card.get("stats") or {}
        upgraded_stats = upgraded.get("stats") or {}
        try:
            current_cost = int(card.get("cost") if card.get("cost") is not None else 99)
        except Exception:
            current_cost = 99
        try:
            upgraded_cost = int(upgraded.get("cost") if upgraded.get("cost") is not None else current_cost)
        except Exception:
            upgraded_cost = current_cost
        damage_delta = float(upgraded_stats.get("damage") or 0.0) - float(current_stats.get("damage") or 0.0)
        block_delta = float(upgraded_stats.get("block") or 0.0) - float(current_stats.get("block") or 0.0)
        cost_reduction = max(0, current_cost - upgraded_cost)
        score += damage_delta * 0.6 + block_delta * 0.7 + float(cost_reduction) * 6.0
        if deck_size >= 12 and card_id in {"STRIKE_IRONCLAD", "DEFEND_IRONCLAD"}:
            score -= 12.0
        if floor >= 10 and current_cost == 0 and damage_delta <= 2.0 and block_delta <= 2.0 and cost_reduction == 0:
            score -= 4.0
        if floor >= 9 and card_id in {"STRIKE_IRONCLAD", "DEFEND_IRONCLAD"}:
            score -= 6.0
        if floor >= 8 and current_cost <= 1 and cost_reduction == 0 and damage_delta <= 2.0 and block_delta <= 0.0:
            score -= 5.0
        scored.append((score, idx))
    ranked = [idx for _, idx in sorted(scored, key=lambda row: (
        card_id_from_row(cards[row[1]]) in IRONCLAD_STARTER_CARD_IDS, -row[0], row[1]))]
    return _selection_payload(cards, ranked, _selection_count(state))


def choose_card_select_enchant(state: Dict[str, Any], repo_root: Path,
                               deck_profile: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    cards = state.get('cards') or []
    if not cards:
        return None
    profile_order = profile_card_priority(deck_profile or _default_deck_profile())
    candidates = [(idx, card) for idx, card in enumerate(cards)
                  if card_id_from_row(card) not in IRONCLAD_STARTER_CARD_IDS]
    # Forced enchant selections can occur before the deck has any whitelist
    # card. Keep the preference for non-starters, but retain a legal starter
    # fallback instead of deadlocking the client.
    if len(candidates) < _selection_count(state):
        candidates = list(enumerate(cards))
    ratings = _base_card_ratings(repo_root)
    ranked = [
        idx for _, _, _, idx in sorted(
            (
                0 if card_id_from_row(card) in profile_order else 1,
                profile_order.get(card_id_from_row(card), 9999),
                -float(ratings.get(card_id_from_row(card), 0.0)),
                idx,
            )
            for idx, card in candidates
        )
    ]
    return _selection_payload(cards, ranked, _selection_count(state))


def _removal_rank(card, deck, profile, repo_root):
    """Remove negatives, then Bash, then the better-replaced starter role."""
    card_id = card_id_from_row(card)
    catalog = {row['id']: row for row in load_card_catalog(repo_root)}
    metadata = catalog.get(card_id, {})
    card_type = str(card.get('type') or card.get('card_type') or metadata.get('type') or '').upper()
    rarity = str(card.get('rarity') or metadata.get('rarity') or '').upper()
    upgraded = bool(card.get('upgraded') or card.get('upgrade') or card.get('upgrade_count'))
    if card_type in {'CURSE', 'STATUS'} or rarity == 'CURSE':
        return (0, 0, upgraded)
    if card_id == 'BASH':
        return (1, 0, upgraded)
    if card_id in {'STRIKE_IRONCLAD', 'DEFEND_IRONCLAD'}:
        specs = {row['id']: row for row in profile['cards']}
        targets = {row['id']: max(1, row['target']) for row in profile['ports']}
        attack_count = defense_count = 0
        for row in deck:
            cid = card_id_from_row(row)
            if cid in IRONCLAD_STARTER_CARD_IDS:
                continue
            info = catalog.get(cid, {})
            kind = str(row.get('type') or row.get('card_type') or info.get('type') or '').upper()
            ports = specs.get(cid, {}).get('ports', [])
            attack_count += kind == 'ATTACK' or 'attack' in ports
            defense_count += 'defense' in ports
        attack_fill = attack_count / targets.get('attack', 4)
        defense_fill = defense_count / targets.get('defense', 4)
        if attack_fill == defense_fill:
            strikes = sum(card_id_from_row(row) == 'STRIKE_IRONCLAD' for row in deck)
            defends = sum(card_id_from_row(row) == 'DEFEND_IRONCLAD' for row in deck)
            preferred = 'STRIKE_IRONCLAD' if strikes >= defends else 'DEFEND_IRONCLAD'
        else:
            preferred = 'STRIKE_IRONCLAD' if attack_fill > defense_fill else 'DEFEND_IRONCLAD'
        return (2, card_id != preferred, upgraded)
    return (3, 0, upgraded)


def choose_card_select_remove(
    state: Dict[str, Any], repo_root: Path, *, transform: bool = False,
    deck_profile: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Choose the least valuable legal card for remove/transform effects."""

    cards = state.get("cards") or []
    if not cards:
        return None
    ratings = _base_card_ratings(repo_root)
    descriptions = _card_descriptions(repo_root)
    scored: List[Tuple[float, int]] = []
    for idx, card in enumerate(cards):
        card_id = _normalize_card_id(
            str(card.get("card_id") or card.get("id") or card.get("cardEnName") or card.get("name") or "")
        )
        value = _adjust_card_value_for_run(
            state, card, ratings.get(card_id, 0.0), descriptions
        )
        card_type = str(card.get("type") or card.get("card_type") or "").upper()
        rarity = str(card.get("rarity") or "").upper()
        if card_type in {"CURSE", "STATUS"} or rarity == "CURSE":
            value -= 100.0
        if card_id.startswith("STRIKE_"):
            value -= 18.0
        elif card_id.startswith("DEFEND_"):
            value -= 14.0
        if bool(card.get("upgraded")):
            value += 12.0
        # Transform has an uncertain replacement, so preserve rare/high-value
        # cards even more strongly than a deterministic removal.
        if transform and rarity in {"RARE", "UNCOMMON"}:
            value += 8.0
        scored.append((value, idx))
    if transform:
        ranked = [idx for _, idx in sorted(scored, key=lambda row: (row[0], row[1]))]
    else:
        profile = deck_profile or _default_deck_profile()
        remaining_deck = list(_player_deck_cards(state) or cards)
        pending = dict((idx, value) for value, idx in scored)
        ranked = []
        while pending and len(ranked) < _selection_count(state):
            idx = min(pending, key=lambda i: (
                _removal_rank(cards[i], remaining_deck, profile, repo_root), pending[i], i))
            ranked.append(idx)
            pending.pop(idx)
            for position, row in enumerate(remaining_deck):
                if card_id_from_row(row) == card_id_from_row(cards[idx]):
                    remaining_deck.pop(position)
                    break
    return _selection_payload(cards, ranked, _selection_count(state))


def choose_card_selection(
    state: Dict[str, Any], repo_root: Path, mode: Optional[str],
    deck_profile: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Dispatch a selection by its operation instead of treating all grids alike."""

    normalized = str(mode or "pick").lower()
    if normalized == "upgrade":
        return choose_card_select_upgrade(state, repo_root)
    if normalized == "remove":
        return choose_card_select_remove(state, repo_root, deck_profile=deck_profile)
    if normalized == "transform":
        return choose_card_select_remove(state, repo_root, transform=True)
    if normalized == 'enchant':
        return choose_card_select_enchant(state, repo_root, deck_profile)
    return choose_card_select_pick(state, repo_root, deck_profile)


def score_card_reward_options(state: Dict[str, Any], repo_root: Path,
                              deck_profile: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    cards = state.get('cards') or state.get('rewards') or []
    profile = deck_profile or _default_deck_profile()
    _, archetype_scores = choose_profile_reward(profile, cards, _player_deck_cards(state),
                                                 act=_profile_act(state))
    return [
        {
            **row,
            'type': card.get('type'),
            'rarity': card.get('rarity'),
            'cost': card.get('cost'),
            'total_score': round(float(row['score']), 3) if row['eligible'] else None,
            'policy': str(profile.get('id') or 'deck_profile'),
        }
        for card, row in zip(cards, archetype_scores)
    ]


def choose_shop_action(state: Dict[str, Any], repo_root: Path,
                       deck_profile: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    player = state.get('player') or {}
    gold = int(player.get('gold') or 0)
    deck = _player_deck_cards(state)
    deck_ids = deck_card_ids(deck)
    affordable = []
    for position, card in enumerate(state.get('cards') or []):
        if card.get('is_stocked') is False:
            continue
        price = int(card.get('cost') if card.get('cost') is not None else 9999)
        if price > gold:
            continue
        row = score_profile_card(deck_profile or _default_deck_profile(), card_id_from_row(card),
                                 deck_ids, act=_profile_act(state))
        if row['eligible']:
            affordable.append((float(row['score']), -price, int(card.get('index', position))))
    if affordable:
        _, _, card_index = max(affordable)
        return {'action': 'buy_card', 'card_index': card_index}
    removal_cost = int(state.get('card_removal_cost') or 9999)
    removal_choice = choose_shop_removal(state, deck_profile, repo_root)
    if state.get('card_removal_available', True) and removal_choice is not None and gold >= removal_cost:
        return {'action': 'remove_card', 'remove_target': removal_choice}
    return None


def score_shop_options(state: Dict[str, Any], repo_root: Path,
                       deck_profile: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    player = state.get("player") or {}
    gold = int(player.get("gold") or 0)
    profile = deck_profile or _default_deck_profile()
    deck_ids = deck_card_ids(_player_deck_cards(state))
    scored: List[Dict[str, Any]] = []
    for card in state.get("cards") or []:
        price = int(card.get("cost") if card.get("cost") is not None else 9999)
        result = score_profile_card(profile, card_id_from_row(card), deck_ids,
                                    act=_profile_act(state))
        scored.append(
            {
                **result,
                "index": card.get("index"),
                "price": price,
                "affordable": price <= gold,
                'is_stocked': card.get('is_stocked', True),
                "total_score": round(float(result['score']), 3) if result['eligible'] else None,
            }
        )
    scored.sort(key=lambda row: tuple(row.get('priority_rank') or [999, 999, 999, 999]))
    return scored


def choose_shop_removal(state: Dict[str, Any], deck_profile=None, repo_root=None) -> Optional[Dict[str, Any]]:
    repo_root = repo_root or Path(__file__).resolve().parents[1]
    deck = _player_deck_cards(state)
    profile = deck_profile or _default_deck_profile()
    eligible = [i for i, card in enumerate(deck)
                if card.get('can_remove') is not False
                and _removal_rank(card, deck, profile, repo_root)[0] < 3]
    if not eligible:
        return None
    idx = min(eligible, key=lambda i: (_removal_rank(deck[i], deck, profile, repo_root), i))
    card = deck[idx]
    return {'select_index': idx, 'card_id': card_id_from_row(card),
            'upgraded': bool(card.get('upgraded') or card.get('upgrade') or card.get('upgrade_count')),
            'label': card_id_from_row(card)}


def choose_card_select_for_shop_removal(state: Dict[str, Any], removal_target: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cards = state.get('cards') or []
    for idx, card in enumerate(cards):
        upgraded = bool(card.get('upgraded') or card.get('upgrade') or card.get('upgrade_count'))
        if (card_id_from_row(card) == removal_target.get('card_id')
                and upgraded == bool(removal_target.get('upgraded'))):
            return {'indices': str(idx), 'card_id': card_id_from_row(card)}
    return None


def reward_card_label(state: Dict[str, Any], payload: Dict[str, Any]) -> Optional[str]:
    cards = state.get("cards") or state.get("rewards") or []
    idx = payload.get("card_index")
    if idx is None:
        return None
    if not (0 <= int(idx) < len(cards)):
        return None
    card = cards[int(idx)] or {}
    return _normalize_card_id(str(card.get("card_id") or card.get("id") or card.get("cardEnName") or ""))


def reward_context_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    cards = state.get("cards") or state.get("rewards") or []
    summarized = []
    for idx, card in enumerate(cards):
        summarized.append(
            {
                "index": idx,
                "label": _normalize_card_id(str(card.get("card_id") or card.get("id") or card.get("cardEnName") or "")),
                "cost": card.get("cost"),
                "type": card.get("type"),
                "rarity": card.get("rarity"),
            }
        )
    return {"cards": summarized}


def shop_item_label(state: Dict[str, Any], payload: Dict[str, Any]) -> Optional[str]:
    cards = state.get("cards") or []
    idx = payload.get("card_index")
    if idx is None:
        return None
    for card in cards:
        card_index = card.get("index")
        if card_index is None or int(card_index) != int(idx):
            continue
        return _normalize_card_id(
            str(card.get("card_id") or card.get("id") or card.get("name") or "")
        )
    return None


def shop_context_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    player = state.get("player") or {}
    summarized = []
    for card in (state.get("cards") or [])[:8]:
        summarized.append(
            {
                "index": card.get("index"),
                "type": "card",
                "label": _normalize_card_id(str(card.get("card_id") or card.get("id") or card.get("name") or "")),
                "price": card.get("cost"),
            }
        )
    for relic in (state.get("relics") or [])[:4]:
        summarized.append(
            {
                "index": relic.get("index"),
                "type": "relic",
                "label": _normalize_card_id(str(relic.get("id") or relic.get("name") or "")),
                "price": relic.get("cost"),
            }
        )
    for potion in (state.get("potions") or [])[:4]:
        summarized.append(
            {
                "index": potion.get("index"),
                "type": "potion",
                "label": _normalize_card_id(str(potion.get("id") or potion.get("name") or "")),
                "price": potion.get("cost"),
            }
        )
    return {
        "gold": int(player.get("gold") or 0),
        "items": summarized,
    }

def build_player_overrides(search_state: Dict[str, Any]) -> Dict[str, Any]:
    combat = search_state.get("combat") or {}
    player = combat.get("player") or {}
    return {
        "hp": player.get("hp"),
        "max_hp": player.get("max_hp"),
    }


def sanitize_snapshot_json_for_search(snapshot_json: str) -> Tuple[str, Dict[str, Any]]:
    """Strip only currently unsupported/no-op potion inventory from search roots.

    Some live-exported combat snapshots become pathologically slow when imported
    into a fresh search worker while carrying certain potion inventory states.
    We keep this fix narrow: remove only potions that the current search model
    does not represent meaningfully anyway, and leave supported potion inventory
    intact. Live gameplay state is left untouched.
    """
    snapshot = json.loads(snapshot_json)
    player = json.loads(snapshot.get("PlayerJson") or "{}")
    potions = list(player.get("potions") or [])
    if not potions:
        return snapshot_json, {"potions_removed": 0}
    kept = []
    removed_ids: List[str] = []
    for potion in potions:
        raw_id = potion.get("Entry") or potion.get("entry") or potion.get("id") or potion.get("name") or ""
        if isinstance(raw_id, dict):
            raw_id = raw_id.get("Entry") or raw_id.get("entry") or raw_id.get("Name") or raw_id.get("name") or ""
        potion_id = _normalize_card_id(str(raw_id))
        if potion_id in UNMODELED_SEARCH_POTION_IDS:
            removed_ids.append(potion_id)
            continue
        kept.append(potion)
    if len(kept) == len(potions):
        return snapshot_json, {"potions_removed": 0}
    player["potions"] = kept
    # Do NOT rewrite max_potion_slot_count here. Potions in the snapshot carry a
    # fixed `slot_index` (0..max-1). Lowering the slot count after dropping a
    # potion can leave a kept potion whose slot_index now exceeds the new max
    # (e.g. removing slot 0 from a 4-slot inventory leaves potions at slots
    # 1/2/3 but a max of 3 -> slot_index 3 is out of range). The engine's
    # restore validates slot consistency and errors out on that mismatch, which
    # silently kills the search worker (root success=False -> -inf / empty
    # sequence -> the run plays a passive end_turn every turn and throws the
    # fight). A smaller inventory under the same slot capacity is a perfectly
    # legal state, so leave the capacity untouched.
    snapshot["PlayerJson"] = json.dumps(player, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(snapshot, ensure_ascii=False), {
        "potions_removed": len(removed_ids),
        "removed_potion_ids": removed_ids,
    }


def should_parallelize_combat_search(
    search_state: Dict[str, Any],
    user_requested_parallel: bool,
    floor: Optional[int] = None,
    depth: int = 0,
    chance_depth: int = 0,
) -> bool:
    if user_requested_parallel:
        return True
    combat = search_state.get("combat") or {}
    legal_actions = available_actions_from_search_state(search_state)
    enemy_count = len(combat.get("enemies") or [])
    if len(legal_actions) <= 1:
        return False
    # Deep pre-chance search on live multi-enemy fights is the primary heavy
    # case in real runs. Top-level parallelism preserves the exact same search
    # tree while cutting wall-clock enough to avoid mistaking long searches for
    # transition hangs.
    if chance_depth > 0 and depth >= 8:
        if enemy_count >= 2 or len(legal_actions) >= 5:
            return True
    if chance_depth > 0 and depth >= 5:
        if enemy_count >= 2 and len(legal_actions) >= 4:
            return True
        if floor is not None and floor >= 5 and len(legal_actions) >= 5:
            return True
    if floor is not None and floor <= 2:
        return False
    # Single-enemy early ACT1 fights repeatedly show worse wall-clock under
    # forced top-level parallelism. Keep those serial, while preserving
    # parallel search for deeper-floor and multi-enemy pressure states.
    if floor is not None and floor <= 4 and enemy_count <= 1:
        return False
    if floor is not None and floor >= 7 and enemy_count >= 3:
        return True
    return False


def is_failed_root_search(result: Any, search_state: Dict[str, Any]) -> bool:
    """True when a root combat search returned no usable plan on a state that
    should have been searchable.

    The search returns an empty sequence with score -inf when the root combat
    state failed to build inside the worker (e.g. an import/restore error, as
    the slot_index sanitizer bug once caused). That is categorically different
    from a legal empty plan: here the live decision really is combat_play with
    playable actions, yet the searcher saw nothing. Detecting it lets the run
    retry instead of silently passing the turn and throwing the fight.
    """
    if result is None:
        return True
    if result.sequence:
        return False
    score = result.score
    if score == float("-inf"):
        return True
    nodes = (result.stats or {}).get("nodes")
    # nodes==1 with no sequence on a live combat_play state means the root
    # produced zero expandable candidates — treat as a failed root, not a
    # deliberate end-of-options leaf.
    if nodes == 1 and str(search_state.get("decision")) == "combat_play":
        return True
    return False


class LeafCollector:
    """Offline collector for combat-search leaf samples with N-step labels.

    Buffers the leaf feature-vectors a searcher dumps during one combat and
    records the realized per-round player-HP trajectory of the actual fight.
    When the combat resolves, each leaf is labeled with an N-step discounted
    return computed from the HP lost over the rounds following that leaf, plus
    a gamma^N-discounted terminal term (retained-HP fraction; 0.0 on defeat).

    Rationale: a whole-combat retained-HP label is dominated by the encounter
    and deck (a confounder), so in-combat features added ~no marginal signal.
    A near-term label reflects THIS leaf's tactical quality (how much damage is
    taken in the next few rounds) and should let the features carry real signal.

    Each row also stores the realized future per-round HP trajectory relative to
    the leaf, so N and gamma can be re-swept offline without re-collecting.

    Active only when --collect-leaf-data is set; otherwise never constructed.
    """

    def __init__(self, path: str, seed: str, gamma: float = 0.9, nstep: int = 3) -> None:
        self.path = path
        self.seed = seed
        self.gamma = gamma
        self.nstep = nstep
        self._fh = open(path, "a", encoding="utf-8")
        self._pending: List[Dict[str, Any]] = []
        self._pending_decisions: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        # realized fight trajectory: round_number -> player hp at start of that
        # round (first observation wins, matching the live turn order).
        self._hp_by_round: Dict[int, float] = {}
        # realized enemy-side trajectory: round_number -> total enemy hp at the
        # start of that round (first observation wins). Mirrors _hp_by_round so
        # the label can reward combat progress (enemy hp removed), not just
        # player-hp retention — the retained-only label gave the features ~no
        # within-encounter signal (sweep LIFT ~0.04).
        self._enemy_hp_by_round: Dict[int, float] = {}
        self._max_hp: float = 0.0
        self.combat_index = 0
        self.written = 0

    def sink(self, sample: Dict[str, Any]) -> None:
        # Called from the searcher (possibly worker threads) per sampled leaf.
        with self._lock:
            self._pending.append(sample)

    @staticmethod
    def _visible_state(state: Dict[str, Any]) -> Dict[str, Any]:
        projected = json.loads(json.dumps(state, ensure_ascii=False))
        combat = projected.get("combat")
        if isinstance(combat, dict):
            # The scorer's information boundary is the current hand and public
            # combat state. Future pile identity/order is deliberately absent.
            for key in ("draw_pile", "discard_pile", "exhaust_pile", "play_pile"):
                combat.pop(key, None)
        return projected

    @staticmethod
    def _search_action(action: Optional[SearchAction]) -> Optional[Dict[str, Any]]:
        if action is None:
            return None
        return {
            "action_type": action.action_type,
            "card_index": action.card_index,
            "target_index": action.target_index,
            "metadata": dict(action.metadata or {}),
        }

    def record_search(self, root_state: Dict[str, Any], result: Any,
                      search_context: Dict[str, Any],
                      record_metadata: Optional[Dict[str, Any]] = None) -> None:
        """Record one root search and all evaluated first-action alternatives.

        The candidates are engine-generated counterfactuals. They are not
        treated as human negatives; a later join may attach a human choice to
        the same root, but this record preserves the distinction explicitly.
        """
        from controller.search.state_cache import hash_search_state_for_plan_reuse

        root_id = hash_search_state_for_plan_reuse(root_state)
        candidates = []
        for index, candidate in enumerate(result.root_candidates or []):
            candidates.append({
                "candidate_id": f"{root_id}:{index}",
                "action": candidate.get("action"),
                "line": candidate.get("line") or [],
                "score": candidate.get("score"),
                "base_score": candidate.get("base_score"),
                "comparable": bool(candidate.get("comparable")),
                "leaf_settlement": candidate.get("leaf_settlement"),
                "selection_metadata": {
                    key: candidate.get(key)
                    for key in (
                        "hard_rule_rank", "lethal_class", "dominated_by",
                        "dominance_reasons", "root_adjustment",
                    )
                    if key in candidate
                },
                "label": {
                    "source": "engine_counterfactual",
                    "human_preference": None,
                    "policy_selected": False,
                },
            })
        selected = self._search_action((result.sequence or [None])[0])
        def _action_signature(row: Optional[Dict[str, Any]]) -> tuple:
            if not row:
                return (None, None, None, None)
            metadata = row.get("metadata") or {}
            return (
                row.get("action_type"),
                row.get("card_index"),
                row.get("target_index"),
                metadata.get("potion_index"),
            )

        selected_signature = _action_signature(selected)
        for candidate in candidates:
            if _action_signature(candidate.get("action")) == selected_signature:
                candidate["label"]["policy_selected"] = True
        record = {
            "schema": "sts2.combat_search.leaf.v2",
            "record_type": "decision",
            "root_id": root_id,
            "combat_index": self.combat_index,
            "seed": self.seed,
            "root_visible_state": self._visible_state(root_state),
            "policy_selected_root_action": selected,
            "counterfactual_candidates": candidates,
            "counterfactual_source": "headless_search",
            "human_label_status": "unlabeled",
            "search_context": dict(search_context),
            "information_boundary": "hand_and_stage_no_draw_pile",
        }
        if record_metadata:
            record.update(dict(record_metadata))
        with self._lock:
            self._pending_decisions.append(record)

    def flush_counterfactual(self) -> int:
        """Write unlabelled search samples without inventing a combat outcome.

        Offline searches rooted at human snapshots have no realized outcome for
        the counterfactual branches. They are valid for human-choice joining,
        but must not be passed through finish_combat because that method
        synthesizes N-step return labels from a live run.
        """
        with self._lock:
            pending = list(self._pending)
            pending_decisions = list(self._pending_decisions)
            self._pending = []
            self._pending_decisions = []
        for sample in pending:
            row = {"counterfactual_label_status": "unlabeled", **sample}
            self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.written += 1
        for record in pending_decisions:
            record["counterfactual_label_status"] = "unlabeled"
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        return len(pending_decisions)

    def observe_live_hp(self, round_number: Optional[int], hp: Optional[float],
                        max_hp: Optional[float],
                        enemy_total_hp: Optional[float] = None) -> None:
        # Record the realized player HP at the start of a live combat round.
        # Called once per live turn from the run loop; first value per round
        # wins so we capture HP entering the round (before that round's damage).
        if round_number is None or hp is None:
            return
        rn = int(round_number)
        if rn not in self._hp_by_round:
            self._hp_by_round[rn] = float(hp)
        if max_hp:
            self._max_hp = float(max_hp)
        # Mirror for the enemy side: total enemy hp entering this round (used
        # to reward combat progress in the label). First value per round wins.
        if enemy_total_hp is not None and rn not in self._enemy_hp_by_round:
            self._enemy_hp_by_round[rn] = float(enemy_total_hp)


    def finish_combat(self, encounter_id: Optional[str], retained_hp_fraction: float,
                      won: bool) -> None:
        # Back-fill an N-step discounted-return label for every buffered leaf
        # from the realized HP trajectory of the fight. The end (resolution)
        # round is inferred from the buffered samples (max observed round + 1).
        max_hp = self._max_hp or 1.0
        rounds = [s.get("round") for s in self._pending if isinstance(s.get("round"), (int, float))]
        end_round = (int(max(rounds)) + 1) if rounds else None
        r_terminal = retained_hp_fraction if won else 0.0

        # Start-of-fight total enemy hp = value at the earliest observed round;
        # the denominator for the enemy-progress fraction. Fall back to 1.0.
        enemy_start = 0.0
        if self._enemy_hp_by_round:
            enemy_start = float(self._enemy_hp_by_round[min(self._enemy_hp_by_round)])
        enemy_start = enemy_start or 1.0
        # Last observed enemy hp (used to hold enemy hp flat after a loss, where
        # the fight ended with enemies still alive).
        enemy_last = (
            float(self._enemy_hp_by_round[max(self._enemy_hp_by_round)])
            if self._enemy_hp_by_round else enemy_start
        )

        def hp_at(rn: int) -> Optional[float]:
            # Realized player HP entering round rn; on/after resolution it is the
            # terminal HP (0 on defeat, else retained fraction * max_hp).
            if end_round is not None and rn >= end_round:
                return r_terminal * max_hp
            return self._hp_by_round.get(rn)

        def enemy_hp_at(rn: int) -> Optional[float]:
            # Realized total enemy HP entering round rn; on/after resolution it
            # is 0 on a win (all enemies dead) or the last observed value on a
            # loss (the fight ended with enemies still standing).
            if end_round is not None and rn >= end_round:
                return 0.0 if won else enemy_last
            return self._enemy_hp_by_round.get(rn)

        with self._lock:
            pending = list(self._pending)
            pending_decisions = list(self._pending_decisions)
            self._pending = []
            self._pending_decisions = []

        for s in pending:
            leaf_round = s.get("round")
            future_hp: List[Optional[float]] = []
            future_enemy_hp: List[Optional[float]] = []
            label_retained = r_terminal
            label = r_terminal
            if isinstance(leaf_round, (int, float)) and end_round is not None:
                lr = int(leaf_round)
                base_hp = hp_at(lr)
                base_ehp = enemy_hp_at(lr)
                # N-step discounted COMBAT-PROGRESS return: each step rewards
                # enemy hp removed (as a fraction of start-of-fight enemy hp) and
                # penalizes player hp lost (as a fraction of max hp), discounted
                # by gamma, plus a gamma^N terminal win term. This gives the
                # in-combat features a within-encounter gradient the retained-only
                # label lacked (offense was invisible to the old label).
                disc_loss = 0.0          # old retained-only label (kept for compare)
                disc_progress = 0.0      # new: enemy_removed - player_lost
                prev = base_hp
                prev_e = base_ehp
                for k in range(1, self.nstep + 1):
                    cur = hp_at(lr + k)
                    cur_e = enemy_hp_at(lr + k)
                    future_hp.append(cur)
                    future_enemy_hp.append(cur_e)
                    disc = self.gamma ** (k - 1)
                    if prev is not None and cur is not None:
                        player_loss = max(0.0, prev - cur) / max_hp
                        disc_loss += disc * player_loss
                        disc_progress -= disc * player_loss
                    if prev_e is not None and cur_e is not None:
                        enemy_removed = max(0.0, prev_e - cur_e) / enemy_start
                        disc_progress += disc * enemy_removed
                    prev = cur if cur is not None else prev
                    prev_e = cur_e if cur_e is not None else prev_e
                label_retained = (self.gamma ** self.nstep) * r_terminal - disc_loss
                label = (self.gamma ** self.nstep) * (1.0 if won else 0.0) + disc_progress

            row = {
                "seed": self.seed,
                "combat_index": self.combat_index,
                "encounter_id": encounter_id,
                "won": won,
                "retained_hp_fraction": retained_hp_fraction,
                "label": label,
                "label_retained": label_retained,
                "nstep": self.nstep,
                "gamma": self.gamma,
                "leaf_hp": (hp_at(int(leaf_round)) if isinstance(leaf_round, (int, float)) else None),
                "future_hp": future_hp,
                "leaf_enemy_hp": (enemy_hp_at(int(leaf_round)) if isinstance(leaf_round, (int, float)) else None),
                "future_enemy_hp": future_enemy_hp,
                "enemy_hp_start": enemy_start,
                "end_round": end_round,
                "combat_max_hp": max_hp,
                **s,
            }
            self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.written += 1
        for record in pending_decisions:
            record["combat_outcome"] = {
                "encounter_id": encounter_id,
                "won": won,
                "retained_hp_fraction": retained_hp_fraction,
            }
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        self._hp_by_round = {}
        self._enemy_hp_by_round = {}
        self._max_hp = 0.0
        self.combat_index += 1

    def discard_combat(self) -> None:
        # Drop buffered samples for a combat we can't label (e.g. run aborted).
        self._pending = []
        self._hp_by_round = {}
        self._enemy_hp_by_round = {}
        self._max_hp = 0.0


    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a conservative run-level agent with combat search")
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument('--scoring-model', type=Path, help='Frozen scoring model for this run')
    parser.add_argument('--deck-profile', type=Path, help='Frozen live deck profile for this run')
    parser.add_argument("--seed", default="42")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--unlock-mode", choices=("all", "profile"), default="all",
                        help="use all-unlocked test pools or a real profile's progression")
    parser.add_argument("--progress-path", type=Path,
                        help="official progress.save; required when --unlock-mode=profile")
    parser.add_argument("--depth", type=int, default=DEFAULT_TURN_ACTION_CAP,
                        help="Maximum player actions expanded before the first enemy turn")
    parser.add_argument("--chance-depth", type=int, default=1)
    parser.add_argument("--parallel-top-level", action="store_true")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument(
        "--max-search-ms",
        type=float,
        default=0.0,
        help="Optional per-combat-decision wall-clock budget. 0 keeps the historical full-search behavior.",
    )
    parser.add_argument("--reuse-cli-processes", action="store_true")
    parser.add_argument(
        "--experimental-worker-pool",
        action="store_true",
        help="Keep CLI worker processes warm across decision steps within a "
        "combat (requires --reuse-cli-processes). Skips the ~241ms per-process "
        "ModelDB init, giving ~6x combat-search speedup. Cross-step warm in_place "
        "restore is now bit-equivalent to fresh-process restore: per-combat relic "
        "state (e.g. Vambrace's once-per-combat block double) is captured in the "
        "snapshot and reset on restore, so pooled runs match baseline decisions "
        "across all tested seeds. See analysis/round2/worker_reuse_plan.md.",
    )
    parser.add_argument(
        "--score-mode",
        default="preference",
        choices=[
            "preference",
            "damage_first",
            "balanced",
            "balanced_action",
            "balanced_future",
            "balanced_nosquare",
            "balanced_unweighted",
            "balanced_power",
            "balanced_r0a",
            "balanced_r0b",
            "balanced_r0c",
            "balanced_r0d",
            "defense_first",
            "fallback",
            "learned",
        ],
    )
    parser.add_argument("--max-steps", type=int, default=400)
    parser.add_argument(
        "--combat-policy",
        default="search",
        choices=["search", "fallback", "naive", "random"],
        help="search: current combat search baseline; fallback: heuristic no-search baseline; naive: first-playable baseline; random: uniform random legal action (lower bound)",
    )
    parser.add_argument(
        "--global-policy",
        default="full",
        choices=["full", "random", "r1"],
        help="full: current heuristic global agent (map/event/rest/reward/shop); random: uniform random global choices (lower bound); r1: learned approximate-Q global policy for map/card/rest decisions",
    )
    parser.add_argument(
        "--r1-model",
        default=None,
        help="Path to an R1 approximate-Q model JSON. Missing/omitted path starts from zero weights.",
    )
    parser.add_argument(
        "--r1-epsilon",
        type=float,
        default=0.0,
        help="Epsilon-greedy exploration rate for --global-policy r1 map/card/rest decisions.",
    )
    parser.add_argument(
        "--r1-behavior",
        default="q",
        choices=["q", "heuristic", "mixed"],
        help="R1 behavior policy. q uses the learned Q policy; heuristic logs baseline choices for off-policy training; mixed uses heuristic except epsilon exploratory Q/random choices.",
    )
    parser.add_argument(
        "--r1-decisions",
        default="map_select,card_reward,rest_site",
        help="Comma-separated R1-controlled/logged decision types. Use e.g. map_select,card_reward to leave rest sites on the heuristic policy.",
    )
    parser.add_argument(
        "--stop-after-act",
        type=int,
        default=None,
        help="Curriculum/eval horizon: stop the run once a later act is reached. Use 1 for Act-1-first episodes.",
    )
    parser.add_argument(
        "--policy-seed",
        type=int,
        default=None,
        help="Seed for random combat/global policies. Defaults to int(seed) so each run seed is reproducible yet distinct.",
    )
    parser.add_argument("--capture-combat-action", type=int)
    parser.add_argument("--capture-combat-number", type=int)
    parser.add_argument("--capture-output")
    parser.add_argument(
        "--dump-combat-trace",
        default=None,
        help="Decision-review: write a JSONL trace of every combat decision "
        "(full board: hand + draw/discard/exhaust piles, player powers/relics, "
        "enemy hp/intent/powers, plus the chosen action and the top-k root "
        "candidate lines the search considered). One JSON object per player "
        "action. Default None = disabled (zero overhead).",
    )
    parser.add_argument(
        "--dump-combat-trace-topk", type=int, default=6,
        help="How many ranked root candidate actions to keep per decision in the "
        "--dump-combat-trace output (default 6). Ignored unless trace is on.",
    )
    parser.add_argument(
        "--capture-hard-combats",
        default=None,
        help="Offline eval-set collection: directory to write an exported engine "
        "snapshot (incl. full deck) at each elite/boss combat entry, continuing "
        "the run. Files: <seed>_<encounter>_<n>.json. Default None = disabled.",
    )
    parser.add_argument(
        "--capture-presave",
        default=None,
        help="Offline pre-room save collection: directory to write the engine's "
        "NATIVE exact-save (faithful deck/upgrades/relics/potions/HP/RNG) at "
        "decision points BEFORE entering a room — pre-boss/elite (map node), "
        "pre-card-reward, pre-rest, pre-shop. Lets a replay tool reseed Shuffle "
        "then enter_room so the OPENING HAND is re-randomized per draw (the "
        "in-combat snapshot freezes it). Writes <seed>_<step>_<kind>.save plus a "
        "presave_manifest.jsonl. Continues the run. Default None = disabled.",
    )
    parser.add_argument(
        "--collect-leaf-data",
        default=None,
        help="Offline data collection: path to write a JSONL of sampled combat "
        "search leaves with Monte-Carlo end-of-combat labels back-filled. Only "
        "active when set; combat search speed is unchanged otherwise.",
    )
    parser.add_argument(
        "--leaf-dump-rate",
        type=int,
        default=20,
        help="Subsample 1 out of every N evaluated leaves when --collect-leaf-data "
        "is set (default 20).",
    )
    parser.add_argument(
        "--draft-explore",
        type=float,
        default=0.0,
        help="Epsilon-greedy DRAFT exploration for learning-data collection. With "
        "this probability, each card_reward picks a uniformly-random offered option "
        "(including skip) instead of the heuristic best, generating counterfactual "
        "deck variance so a learner can attribute outcomes to draft choices. Uses "
        "the global RNG stream. Default 0.0 = pure heuristic (unchanged).",
    )
    parser.add_argument(
        "--draft-policy",
        choices=["heuristic", "learned", "branchvalue", "branchvalue_act2"],
        default="heuristic",
        help="heuristic: port-based card-reward scorer (default). learned: deck-value "
        "model. branchvalue: offline model-based causal ΔReturn model with softmax "
        "sampling + rule fallback (models/branch_value.pt).",
    )
    parser.add_argument("--draft-tau", type=float, default=0.05,
                        help="branchvalue: Boltzmann temperature on ΔReturn scores")
    parser.add_argument("--draft-model", default=None,
                        help="branchvalue: path to a model checkpoint (default: models/branch_value.pt)")
    parser.add_argument("--draft-heuristic-reward-count", type=int, default=0,
                        help="branchvalue: use heuristic for first N non-starter card rewards in Act 1, then switch")
    args = parser.parse_args()

    global _HEADLESS_DECK_PROFILE_PATH, _HEADLESS_DECK_PROFILE
    _HEADLESS_DECK_PROFILE_PATH = args.deck_profile
    _HEADLESS_DECK_PROFILE = load_deck_profile(args.deck_profile)
    scoring_model = (json.loads(args.scoring_model.read_text(encoding='utf-8'))
                     if args.scoring_model else None)
    if scoring_model is not None:
        validate_model(scoring_model)

    repo_root = Path(__file__).resolve().parents[1]
    # Seeded RNGs for the random baseline policies. Default to int(seed) so each
    # run seed gives a reproducible-yet-distinct random trajectory; combat and
    # global use separate streams so changing one axis doesn't perturb the other.
    _policy_seed = args.policy_seed if args.policy_seed is not None else _seed_to_int(args.seed)
    combat_rng = random.Random(_policy_seed * 2 + 1)
    global_rng = random.Random(_policy_seed * 2 + 2)
    r1_policy = GlobalQPolicy.load(args.r1_model) if args.global_policy == "r1" else None
    r1_decisions = {x.strip() for x in str(args.r1_decisions or "").split(",") if x.strip()}
    cli_cfg = CliConfig(repo_root=repo_root)
    descriptions = _card_descriptions(repo_root)

    encounter_exact, encounter_enemy_ids, encounter_enemy_families = build_encounter_index(cli_cfg, seed=args.seed)

    cli = Sts2CliAdapter(cli_cfg)
    cli.start()
    state = cli.start_run(
        character=args.character,
        seed=args.seed,
        ascension=args.ascension,
        lang=args.lang,
        unlock_mode=args.unlock_mode,
        progress_path=args.progress_path,
    )

    leaf_collector: Optional[LeafCollector] = (
        LeafCollector(args.collect_leaf_data, seed=str(args.seed))
        if args.collect_leaf_data
        else None
    )
    # Decision-review trace: one JSON object per combat player action, holding the
    # full board (combat_full_board) + the chosen action + the search's top-k root
    # candidate lines. Opened only when --dump-combat-trace is set.
    trace_fh = open(args.dump_combat_trace, "a", encoding="utf-8") if args.dump_combat_trace else None

    combat_history: List[RecordedAction] = []
    current_encounter_id: Optional[str] = None
    # Warm CLI worker pool, scoped to one combat. Created lazily on the first
    # search step of a combat and closed when the combat ends. Keeping worker
    # processes alive across decision steps means the ~241ms ModelDB init (the
    # bulk of a `full` snapshot restore) is paid once per process instead of
    # once per step. Only used when reuse_cli_processes is on.
    combat_worker_pool: Optional[CombatWorkerPool] = None
    # Room type of the node currently being entered ("Monster"/"Elite"/"Boss"/...).
    # Drives whether combat search expands potions (Elite/Boss only); see the
    # CombatSearcher construction below.
    current_room_type: Optional[str] = None
    combats_seen = 0
    combat_action_index = 0
    pending_shop_removal: Optional[Dict[str, Any]] = None
    pending_card_select_context: Optional[Dict[str, Any]] = None
    planned_combat_sequence: List[SearchAction] = []
    planned_state_hashes: List[str] = []
    planned_state_keys: List[Dict[str, Any]] = []
    planned_turn_marker: Optional[Tuple[Any, ...]] = None
    # Marker of the last turn we emitted a per-turn combat snapshot for, so each
    # turn is logged exactly once (at its first player action). Reset at combat
    # end alongside the other per-combat state.
    last_logged_turn_marker: Optional[Tuple[Any, ...]] = None
    planned_map_route: List[Dict[str, Any]] = []
    planned_map_route_act: Optional[int] = None

    # --- Ablation metrics (objective only; see paired-comparison analysis) ---
    # Per-floor HP loss: hp_by_floor[f] = (first_seen_hp, last_seen_hp). The drop
    # within a floor distinguishes attrition from burst; summing across floors
    # gives total HP attrition. Recorded from run-level state.player each step.
    hp_by_floor: Dict[int, Dict[str, Any]] = {}
    deck_snapshots: List[Dict[str, Any]] = []
    event_choices_log: List[Dict[str, Any]] = []

    # Highest act the run ever reached. Tracked separately from last_floor
    # because floor numbering is NOT reliable across acts (it can reset/overlap),
    # so "cleared Act 1" must be read from act progression, not last_floor. A run
    # that reaches act>=2 has, by definition, beaten the Act-1 boss — this is the
    # field analysis must use for clear-rate, not last_floor==17 heuristics.
    max_act_seen: int = 0
    # Last (act, floor) actually reached, in arrival order — NOT a max. last_floor
    # alone is misleading across acts because floor resets each act (Act-2 floor 2
    # < Act-1 floor 17, so a max would report the stale Act-1 number). This pair
    # records where the run truly ended so analysis can say "Act 2 floor 2".
    last_act_seen: int = 0
    last_act_floor_seen: int = 0

    def _record_act(st: Dict[str, Any]) -> None:
        nonlocal max_act_seen, last_act_seen, last_act_floor_seen
        ctx = st.get("context") or {}
        a = ctx.get("act") or st.get("act")
        try:
            a = int(a)
        except (TypeError, ValueError):
            return
        if a > max_act_seen:
            max_act_seen = a
        # Track the latest position. Floor may be absent on some frames; fall
        # back to the run-floor reader. Only advance position when the act is
        # the same-or-later than what we've recorded, so a transient stale frame
        # can't rewind us to an earlier act.
        f = ctx.get("floor")
        if f is None:
            f = _run_floor(st)
        try:
            f = int(f)
        except (TypeError, ValueError):
            return
        if (a, f) >= (last_act_seen, last_act_floor_seen) or a > last_act_seen:
            last_act_seen, last_act_floor_seen = a, f

    def _record_floor_hp(st: Dict[str, Any]) -> None:
        player = st.get("player") or {}
        hp = player.get("hp")
        if hp is None:
            return
        floor = _run_floor(st)
        rec = hp_by_floor.get(floor)
        if rec is None:
            hp_by_floor[floor] = {"first_hp": int(hp), "last_hp": int(hp),
                                  "max_hp": int(player.get("max_hp") or 0)}
        else:
            rec["last_hp"] = int(hp)
            rec["max_hp"] = int(player.get("max_hp") or rec["max_hp"])

    def _emit_run_summary(outcome: str, error_state: Optional[Dict[str, Any]] = None) -> None:
        # Single machine-readable summary line for paired-comparison analysis.
        # per_floor_hp_loss[f] = max(0, first_hp - last_hp) seen on that floor.
        floors = sorted(hp_by_floor)
        per_floor_loss = {
            str(f): max(0, hp_by_floor[f]["first_hp"] - hp_by_floor[f]["last_hp"])
            for f in floors
        }
        last_floor = floors[-1] if floors else 0
        summary = {
            "run_summary": True,
            "seed": args.seed,
            "outcome": outcome,
            "last_floor": last_floor,
            # Act-aware clear tracking. max_act = highest act reached; reaching
            # act N means the act-(N-1) boss was beaten, so acts_cleared =
            # max(0, max_act-1). Use THESE for Act-clear rate, never last_floor
            # (floor numbers are not reliable across acts). A run that dies in
            # Act 2 still has acts_cleared >= 1.
            "max_act": max_act_seen,
            "acts_cleared": max(0, max_act_seen - 1),
            # Where the run actually ended, act-aware. last_act_floor is the floor
            # WITHIN last_act (floor resets each act), and last_position is the
            # human-readable "A<act>F<floor>" so logs/analysis can show "A2F2"
            # instead of the misleading flat last_floor=17. Prefer these over
            # last_floor for any per-act depth analysis.
            "last_act": last_act_seen,
            "last_act_floor": last_act_floor_seen,
            "last_position": f"A{last_act_seen}F{last_act_floor_seen}",
            "combat_policy": args.combat_policy,
            "global_policy": args.global_policy,
            "score_mode": args.score_mode,
            "policy_seed": _policy_seed,
            "r1_model": args.r1_model if args.global_policy == "r1" else None,
            "r1_epsilon": args.r1_epsilon if args.global_policy == "r1" else None,
            "r1_behavior": args.r1_behavior if args.global_policy == "r1" else None,
            "r1_decisions": sorted(r1_decisions) if args.global_policy == "r1" else None,
            "stop_after_act": args.stop_after_act,
            "total_hp_loss": sum(per_floor_loss.values()),
            "per_floor_hp_loss": per_floor_loss,
            "final_deck": deck_snapshots[-1] if deck_snapshots else None,
        }
        if outcome == "error":
            # An "error" outcome is NOT a natural run end: the engine/driver
            # aborted (often a non-combat navigation frame, e.g. map_select),
            # so last_floor is an ARTIFACT of where it broke, not where the
            # agent's play actually stopped. Mark it untrustworthy and record
            # enough to triage the abort post-hoc (this was previously dropped,
            # leaving error runs as unattributable floor-count noise).
            es = error_state or {}
            summary["last_floor_truncated_by_error"] = True
            summary["error_context"] = {
                "message": es.get("message") or es.get("error") or es.get("reason"),
                "decision": es.get("decision"),
                "result_type": es.get("type"),
                "floor": (es.get("context") or {}).get("floor"),
                "act": (es.get("context") or {}).get("act"),
            }
        print(json.dumps(summary, ensure_ascii=False))



    progress_guard = NoncombatProgressGuard()
    try:
        # Running deck for the learned drafter (--draft-policy learned): Ironclad
        # starter + each taken reward card. Used to score candidates by predicted
        # post-take deck-value. Tracked here since the loop has no deck var.
        running_deck: List[str] = ["STRIKE_IRONCLAD"]*5 + ["DEFEND_IRONCLAD"]*4 + ["BASH"]
        for step_id in range(1, args.max_steps + 1):
            step_started = time.perf_counter()
            decision = state.get("decision")
            context = state.get("context") or {}
            _record_floor_hp(state)
            _record_act(state)
            event: Dict[str, Any] = {
                "step_id": step_id,
                "decision": decision,
                "floor": context.get("floor"),
                "act": context.get("act"),
                "headless_before": {
                    "run": {"floor": context.get("floor"), "act_id": context.get("act"),
                            "current_hp": (state.get("player") or {}).get("hp"),
                            "max_hp": (state.get("player") or {}).get("max_hp"),
                            "gold": (state.get("player") or {}).get("gold")},
                    "screen": decision,
                },
            }
            current_event = event
            if args.stop_after_act is not None and max_act_seen > int(args.stop_after_act):
                event["terminal"] = {
                    "decision": "act_horizon_reached",
                    "stop_after_act": int(args.stop_after_act),
                    "context": context,
                    "player": state.get("player"),
                }
                print(json.dumps(event, ensure_ascii=False))
                _emit_run_summary("act_horizon_reached")
                break
            restart_cli_ms = 0.0
            refresh_ms = 0.0

            if decision == "event_choice":
                event["event_context"] = event_context_summary(state)
                if args.global_policy == "random":
                    _rand = choose_global_random(decision, state, global_rng)
                    action, payload = _rand if _rand is not None else ("choose_option", choose_event_option(state))
                else:
                    action = "choose_option"
                    payload = choose_event_option(state)
                chosen_event_option = selected_event_option(state, payload)
                pending_card_select_context = {
                    "source": "event",
                    "event_id": state.get("event_id") or state.get("event_name"),
                    "option_index": payload.get("option_index"),
                    "option_id": (chosen_event_option or {}).get("option_id"),
                    "mode": infer_card_selection_mode(chosen_event_option),
                }
                event["pending_card_select_context"] = pending_card_select_context
            elif decision == "map_select":
                current_act = context.get("act")
                if planned_map_route_act != current_act:
                    planned_map_route = []
                    planned_map_route_act = current_act
                try:
                    full_map = cli.get_map(timeout_s=8.0)
                except Exception as exc:
                    event["map_prefetch_error"] = {
                        "type": exc.__class__.__name__,
                        "message": str(exc),
                    }
                    full_map = None
                event["map_context"] = map_context_summary(state)
                action = "select_map_node"
                choices = state.get("choices") or []
                current_choices = {
                    (int(choice.get("col")), int(choice.get("row")))
                    for choice in choices
                    if choice.get("col") is not None and choice.get("row") is not None
                }
                next_planned = planned_map_route[0] if planned_map_route else None
                route_reused = False
                next_planned_key = None
                if next_planned is not None:
                    next_planned_key = (int(next_planned.get("col")), int(next_planned.get("row")))
                r1_enabled = "map_select" in r1_decisions
                if args.global_policy == "r1" and r1_enabled and r1_policy is not None and args.r1_behavior in {"q", "mixed"} and (
                    args.r1_behavior == "q" or global_rng.random() < float(args.r1_epsilon)
                ):
                    choice = r1_policy.choose_map_select(
                        state,
                        full_map,
                        global_rng,
                        epsilon=(float(args.r1_epsilon) if args.r1_behavior == "q" else 1.0),
                    )
                    if choice is not None:
                        payload = choice.payload
                        event["global_decision"] = choice.trace
                        event["global_decision"]["behavior"] = args.r1_behavior
                        planned_map_route = []
                    else:
                        payload, planned_map_route = choose_map_route_global(state, full_map, descriptions)
                elif next_planned_key is not None and next_planned_key in current_choices:
                    payload = {"col": int(next_planned_key[0]), "row": int(next_planned_key[1])}
                    route_reused = True
                elif args.global_policy == "random":
                    _rand = choose_global_random(decision, state, global_rng)
                    if _rand is not None:
                        payload = _rand[1]
                    else:
                        payload, planned_map_route = choose_map_route_global(state, full_map, descriptions)
                else:
                    payload, planned_map_route = choose_map_route_global(state, full_map, descriptions)
                if args.global_policy == "r1" and r1_enabled and r1_policy is not None and event.get("global_decision") is None:
                    trace_choice = r1_policy.trace_for_selection(
                        "map_select",
                        map_candidates(state, full_map),
                        selected_action="select_map_node",
                        selected_payload=payload,
                        behavior="heuristic",
                    )
                    if trace_choice is not None:
                        event["global_decision"] = trace_choice.trace
                chosen_key = (int(payload["col"]), int(payload["row"]))
                # Remember the type of the node we are walking into, so combat
                # search can decide whether to expand potions (Elite/Boss).
                for choice in choices:
                    if (
                        choice.get("col") is not None
                        and choice.get("row") is not None
                        and (int(choice.get("col")), int(choice.get("row"))) == chosen_key
                    ):
                        current_room_type = str(choice.get("type") or "") or None
                        break
                if route_reused:
                    planned_map_route = planned_map_route[1:]
                elif planned_map_route and (
                    int(planned_map_route[0].get("col")) == chosen_key[0]
                    and int(planned_map_route[0].get("row")) == chosen_key[1]
                ):
                    planned_map_route = planned_map_route[1:]
                event["route_plan"] = {
                    "reused": route_reused,
                    "chosen": {"col": int(payload["col"]), "row": int(payload["row"])},
                    "remaining": [
                        {"col": int(node.get("col")), "row": int(node.get("row")), "type": node.get("type")}
                        for node in planned_map_route[:8]
                    ],
                }
                # Pre-room native save: we are at the map decision, the chosen
                # node type is known (current_room_type), and we have NOT yet
                # entered the room. Persisting the engine's exact-save here lets
                # a replay tool load_save -> reseed Shuffle -> enter_room so the
                # opening hand is re-randomized per draw (the in-combat snapshot
                # freezes it). Gate on Elite/Boss = the hard fights we denoise.
                if args.capture_presave and current_room_type in {"Elite", "Boss"}:
                    event["presave"] = write_presave(
                        cli, args.capture_presave, seed=str(args.seed),
                        character=args.character, step_id=step_id,
                        kind=f"pre_{current_room_type.lower()}", state=state,
                        extra={"node_type": current_room_type,
                               "node": {"col": int(payload["col"]),
                                        "row": int(payload["row"])}})
            elif decision == "combat_reward":
                rewards = state.get("rewards") or []
                if rewards:
                    reward = next((row for row in rewards if row.get("reward_type") != "Card"), rewards[0])
                    action = "claim_combat_reward"
                    payload = {"reward_index": int(reward["index"])}
                else:
                    action = "finish_combat_rewards"
                    payload = {}
            elif decision == "card_reward":
                event["reward_context"] = reward_context_summary(state)
                event["reward_scores"] = score_card_reward_options(state, repo_root)
                if args.capture_presave:
                    event["presave"] = write_presave(
                        cli, args.capture_presave, seed=str(args.seed),
                        character=args.character, step_id=step_id,
                        kind="card_reward", state=state)
                r1_enabled = "card_reward" in r1_decisions
                if args.global_policy == "r1" and r1_enabled and r1_policy is not None and args.r1_behavior in {"q", "mixed"} and (
                    args.r1_behavior == "q" or global_rng.random() < float(args.r1_epsilon)
                ):
                    choice = r1_policy.choose_card_reward(
                        state,
                        global_rng,
                        epsilon=(float(args.r1_epsilon) if args.r1_behavior == "q" else 1.0),
                        reward_scores=event.get("reward_scores") or [],
                    )
                    if choice is None:
                        action, payload = "skip_card_reward", {}
                    else:
                        action, payload = choice.action, choice.payload
                        event["global_decision"] = choice.trace
                        event["global_decision"]["behavior"] = args.r1_behavior
                    event["reward_choice"] = {
                        "action": action,
                        "card_index": payload.get("card_index"),
                        "card_id": reward_card_label(state, payload) if payload else None,
                    }
                elif args.global_policy == "random":
                    _rand = choose_global_random(decision, state, global_rng)
                    action, payload = _rand if _rand is not None else ("skip_card_reward", {})
                    event["reward_choice"] = {
                        "action": action,
                        "card_index": payload.get("card_index"),
                        "card_id": reward_card_label(state, payload) if payload else None,
                    }
                else:
                    explored = False
                    if args.draft_explore > 0.0 and global_rng.random() < args.draft_explore:
                        # epsilon-greedy draft exploration: uniformly pick among the
                        # offered cards OR skip, to generate counterfactual deck variance.
                        cards = state.get("cards") or state.get("rewards") or []
                        choices = list(range(len(cards))) + [None]  # None = skip
                        pick = global_rng.choice(choices)
                        explored = True
                        if pick is None:
                            action = "skip_card_reward"
                            payload = {}
                            event["reward_choice"] = {"action": action, "explored": True}
                        else:
                            action = "select_card_reward"
                            payload = {"card_index": int((cards[pick] or {}).get("index", pick))}
                            event["reward_choice"] = {
                                "action": action,
                                "card_index": payload.get("card_index"),
                                "card_id": reward_card_label(state, payload),
                                "explored": True,
                            }
                    if not explored:
                        if args.draft_policy == "learned":
                            from controller.learned_drafter import choose_card_reward_learned
                            reward_pick = choose_card_reward_learned(state, running_deck)
                        elif args.draft_policy == "branchvalue":
                            reward_count = int(getattr(args, "draft_heuristic_reward_count", 0))
                            current_act_val = (state.get("context") or {}).get("act", 1) or 1
                            if reward_count > 0 and current_act_val <= 1:
                                non_starter = sum(1 for c in running_deck
                                                  if _normalize_card_id(str(c)) not in {"STRIKE_IRONCLAD", "DEFEND_IRONCLAD", "BASH"})
                                if non_starter < reward_count:
                                    reward_pick = choose_card_reward(state, repo_root)
                                else:
                                    from controller.branch_value_drafter import choose_card_reward_branchvalue
                                    reward_pick = choose_card_reward_branchvalue(
                                        state, running_deck, rng=global_rng,
                                        tau=float(getattr(args, "draft_tau", 0.05)),
                                        model_path=getattr(args, "draft_model", None))
                                    if isinstance(reward_pick, dict) and reward_pick.get("_fallback"):
                                        reward_pick = choose_card_reward(state, repo_root)
                            else:
                                from controller.branch_value_drafter import choose_card_reward_branchvalue
                                reward_pick = choose_card_reward_branchvalue(
                                    state, running_deck, rng=global_rng,
                                    tau=float(getattr(args, "draft_tau", 0.05)),
                                    model_path=getattr(args, "draft_model", None))
                                if isinstance(reward_pick, dict) and reward_pick.get("_fallback"):
                                    reward_pick = choose_card_reward(state, repo_root)
                        elif args.draft_policy == "branchvalue_act2":
                            current_act = (state.get("context") or {}).get("act", 1) or 1
                            if current_act >= 2:
                                from controller.branch_value_drafter import choose_card_reward_branchvalue
                                reward_pick = choose_card_reward_branchvalue(
                                    state, running_deck, rng=global_rng,
                                    tau=float(getattr(args, "draft_tau", 0.05)),
                                    model_path=getattr(args, "draft_model", None))
                                if isinstance(reward_pick, dict) and reward_pick.get("_fallback"):
                                    reward_pick = choose_card_reward(state, repo_root)
                            else:
                                reward_pick = choose_card_reward(state, repo_root)
                        else:
                            reward_pick = choose_card_reward(state, repo_root)
                        if reward_pick is None:
                            action = "skip_card_reward"
                            payload = {}
                            event["reward_choice"] = {"action": action}
                        else:
                            action = "select_card_reward"
                            payload = reward_pick
                            event["reward_choice"] = {
                                "action": action,
                                "card_index": payload.get("card_index"),
                                "card_id": reward_card_label(state, payload),
                            }
                    if args.global_policy == "r1" and r1_enabled and r1_policy is not None and event.get("global_decision") is None:
                        trace_choice = r1_policy.trace_for_selection(
                            "card_reward",
                            card_reward_candidates(state, event.get("reward_scores") or []),
                            selected_action=action,
                            selected_payload=payload,
                            behavior="heuristic",
                        )
                        if trace_choice is not None:
                            event["global_decision"] = trace_choice.trace
                # Track the running deck for the learned drafter (covers
                # heuristic/learned/explore branches): append any taken card id.
                _rc = event.get("reward_choice") or {}
                if _rc.get("action") == "select_card_reward" and _rc.get("card_id"):
                    running_deck.append(str(_rc.get("card_id")).upper())
            elif decision == "rest_site":
                if args.capture_presave:
                    event["presave"] = write_presave(
                        cli, args.capture_presave, seed=str(args.seed),
                        character=args.character, step_id=step_id,
                        kind="rest_site", state=state)
                action = "choose_option"
                r1_enabled = "rest_site" in r1_decisions
                if args.global_policy == "r1" and r1_enabled and r1_policy is not None and args.r1_behavior in {"q", "mixed"} and (
                    args.r1_behavior == "q" or global_rng.random() < float(args.r1_epsilon)
                ):
                    choice = r1_policy.choose_rest_site(
                        state,
                        global_rng,
                        epsilon=(float(args.r1_epsilon) if args.r1_behavior == "q" else 1.0),
                    )
                    payload = choice.payload if choice is not None else choose_rest_option(state, descriptions)
                    if choice is not None:
                        event["global_decision"] = choice.trace
                        event["global_decision"]["behavior"] = args.r1_behavior
                elif args.global_policy == "random":
                    _rand = choose_global_random(decision, state, global_rng)
                    payload = _rand[1] if _rand is not None else choose_rest_option(state, descriptions)
                else:
                    payload = choose_rest_option(state, descriptions)
                if args.global_policy == "r1" and r1_enabled and r1_policy is not None and event.get("global_decision") is None:
                    trace_choice = r1_policy.trace_for_selection(
                        "rest_site",
                        rest_site_candidates(state),
                        selected_action="choose_option",
                        selected_payload=payload,
                        behavior="heuristic",
                    )
                    if trace_choice is not None:
                        event["global_decision"] = trace_choice.trace
                event["rest_choice"] = payload
                pending_card_select_context = None
                for opt in state.get("options") or []:
                    if int(opt.get("index") or -1) == int(payload.get("option_index") or -1):
                        if str(opt.get("option_id") or "").upper() == "SMITH":
                            pending_card_select_context = {
                                "source": "rest_site",
                                "option_id": "SMITH",
                                "mode": "upgrade",
                            }
                        break
            elif decision == "card_select":
                if args.global_policy == "random" and pending_shop_removal is None:
                    _rand = choose_global_random(decision, state, global_rng)
                    action, payload = _rand if _rand is not None else ("skip_select", {})
                    event["card_select_choice"] = {"action": action, "payload": payload, "random": True}
                elif pending_shop_removal is not None:
                    removal_pick = choose_card_select_for_shop_removal(state, pending_shop_removal)
                    if removal_pick is None:
                        action = "skip_select"
                        payload = {}
                        event["shop_removal_choice"] = {"action": action, "failed": True}
                    else:
                        action = "select_cards"
                        payload = {"indices": str(removal_pick["indices"])}
                        event["shop_removal_choice"] = {
                            "action": action,
                            "indices": payload["indices"],
                            "card_id": removal_pick.get("card_id"),
                        }
                elif pending_card_select_context is not None:
                    selection_mode = pending_card_select_context.get("mode")
                    select_pick = choose_card_selection(state, repo_root, selection_mode)
                    if select_pick is None:
                        action = "skip_select"
                        payload = {}
                        event["card_select_choice"] = {
                            "action": action,
                            "mode": selection_mode,
                            "context": pending_card_select_context,
                        }
                    else:
                        action = "select_cards"
                        payload = {"indices": str(select_pick["indices"])}
                        event["card_select_choice"] = {
                            "action": action,
                            "indices": payload["indices"],
                            "card_id": select_pick.get("card_id"),
                            "card_ids": select_pick.get("card_ids"),
                            "mode": selection_mode,
                            "context": pending_card_select_context,
                        }
                else:
                    select_pick = choose_card_select_pick(state, repo_root)
                    if select_pick is None:
                        action = "skip_select"
                        payload = {}
                        event["card_select_choice"] = {"action": action}
                    else:
                        action = "select_cards"
                        payload = {"indices": str(select_pick["indices"])}
                        event["card_select_choice"] = {
                            "action": action,
                            "indices": payload["indices"],
                            "card_id": select_pick.get("card_id"),
                        }
            elif decision == "bundle_select":
                bundles = state.get("bundles") or []
                if not bundles:
                    raise RuntimeError("bundle_select has no bundle options")
                action = "select_bundle"
                payload = {"bundle_index": int(bundles[0].get("index", 0))}
            elif decision == "combat_play":
                combat_action_index += 1
                if current_encounter_id is None:
                    try:
                        current_search_state = cli.get_search_state(timeout_s=8.0).get("combat_state_for_search") or {}
                    except TimeoutError:
                        cli, state = restart_cli_from_exact_save(
                            cli,
                            cli_cfg,
                            args.lang,
                            save_tag=f"{args.seed}_{step_id}_combat_entry",
                        )
                        current_search_state = cli.get_search_state(timeout_s=8.0).get("combat_state_for_search") or {}
                else:
                    current_search_state = cli.get_search_state().get("combat_state_for_search") or {}
                current_turn_marker = _turn_plan_marker(current_search_state)
                if planned_turn_marker != current_turn_marker:
                    planned_combat_sequence = []
                    planned_state_hashes = []
                    planned_state_keys = []
                    planned_turn_marker = current_turn_marker
                if current_encounter_id is None:
                    current_encounter_id = infer_encounter_id(
                        current_search_state,
                        encounter_exact,
                        encounter_enemy_ids,
                        encounter_enemy_families,
                    )
                    encounter_inferred = current_encounter_id is not None
                    if current_encounter_id is None:
                        # Encounter-id inference failed: this enemy combination was
                        # not in the offline signature index (e.g. a known pool
                        # reshuffled into an unseen pairing). The id is only a
                        # stats LABEL — the searcher restores from the live engine
                        # snapshot, not from `encounter`, so search can still run
                        # fully. Synthesize a descriptive label so the fight is
                        # tracked and searched normally instead of silently
                        # degrading to the heuristic fallback. Loudly flag it so
                        # the gap gets recorded for later index backfill.
                        synthetic = "UNKNOWN__" + "_".join(_enemy_ids_signature(current_search_state))
                        current_encounter_id = synthetic[:120]
                        warning = {
                            "warning": "encounter_id_inference_failed",
                            "step_id": step_id,
                            "floor": context.get("floor"),
                            "enemy_ids": list(_enemy_ids_signature(current_search_state)),
                            "synthetic_encounter_id": current_encounter_id,
                            "note": "search still runs on live snapshot; label is synthetic",
                        }
                        print(json.dumps(warning, ensure_ascii=False))
                        event["encounter_inference"] = warning
                    # Either way we now have an encounter label (real or
                    # synthetic) — register the fight and let search proceed.
                    combat_history = []
                    combats_seen += 1
                    event["combat_start"] = {
                        "encounter_id": current_encounter_id,
                        "encounter_inferred": encounter_inferred,
                        "summary": combat_summary(current_search_state),
                    }
                    # Offline eval-set collection: at each elite/boss combat
                    # entry, export the engine snapshot (which includes the
                    # full PlayerJson.deck) and CONTINUE the run. Unlike
                    # --capture-combat-action this does not break, so one run
                    # yields every hard-fight entry it reaches.
                    if args.capture_hard_combats and str(current_encounter_id).upper().endswith(("_ELITE", "_BOSS")):
                        try:
                            _hc_id = f"hardcap_{args.seed}_{step_id}"
                            _hc_cap = cli.capture_combat_snapshot(_hc_id)
                            _hc_exp = cli.export_combat_snapshot(_hc_id) if _hc_cap.get("success") else {}
                            if _hc_exp.get("success"):
                                _hc_dir = Path(args.capture_hard_combats)
                                _hc_dir.mkdir(parents=True, exist_ok=True)
                                _hc_path = _hc_dir / f"{args.seed}_{current_encounter_id}_{combats_seen}.json"
                                _hc_path.write_text(json.dumps({
                                    "seed": args.seed,
                                    "character": args.character,
                                    "encounter_id": current_encounter_id,
                                    "combats_seen": combats_seen,
                                    "step_id": step_id,
                                    "entry_summary": combat_summary(current_search_state),
                                    "snapshot_json": _hc_exp["snapshot_json"],
                                }, ensure_ascii=False))
                                event["hard_combat_capture"] = {
                                    "encounter_id": current_encounter_id,
                                    "path": str(_hc_path),
                                }
                        except Exception as _hc_err:  # never let collection break a run
                            event["hard_combat_capture_error"] = str(_hc_err)
                    planned_combat_sequence = []
                    planned_state_hashes = []
                    planned_state_keys = []
                    planned_turn_marker = current_turn_marker

                # Emit a per-turn combat snapshot at the first player action of
                # each turn (keyed on the turn marker so it fires exactly once
                # per turn). This is the per-turn HP/block/incoming-damage curve
                # that distinguishes burst vs. attrition boss losses.
                if (
                    current_encounter_id is not None
                    and last_logged_turn_marker != current_turn_marker
                ):
                    last_logged_turn_marker = current_turn_marker
                    event["combat_turn"] = combat_turn_snapshot(current_search_state)
                    if leaf_collector is not None:
                        _lc_combat = (current_search_state.get("combat") or {})
                        _lc_player = (_lc_combat.get("player") or {})
                        _lc_enemies = (_lc_combat.get("enemies") or [])
                        _lc_enemy_total = float(sum(int(e.get("hp") or 0) for e in _lc_enemies))
                        leaf_collector.observe_live_hp(
                            _lc_combat.get("round_number"),
                            _lc_player.get("hp"),
                            _lc_player.get("max_hp"),
                            enemy_total_hp=_lc_enemy_total,
                        )

                should_capture = (
                    args.capture_combat_action is not None
                    and combat_action_index == args.capture_combat_action
                    and (args.capture_combat_number is None or combats_seen == args.capture_combat_number)
                )
                if should_capture:
                    live_snapshot_id = f"capture_step_{step_id}"
                    capture = cli.capture_combat_snapshot(live_snapshot_id)
                    if not capture.get("success"):
                        raise RuntimeError(f"Failed to capture live combat snapshot: {capture}")
                    exported = cli.export_combat_snapshot(live_snapshot_id)
                    if not exported.get("success"):
                        raise RuntimeError(f"Failed to export live combat snapshot: {exported}")
                    snapshot_bundle = {
                        "step_id": step_id,
                        "combat_action_index": combat_action_index,
                        "seed": args.seed,
                        "character": args.character,
                        "encounter_id": current_encounter_id,
                        "snapshot_id": live_snapshot_id,
                        "search_state": current_search_state,
                        "snapshot_json": exported["snapshot_json"],
                    }
                    if args.capture_output:
                        Path(args.capture_output).write_text(json.dumps(snapshot_bundle, ensure_ascii=False, indent=2))
                    print(json.dumps({
                        "step_id": step_id,
                        "capture": {
                            "combat_action_index": combat_action_index,
                            "encounter_id": current_encounter_id,
                            "output": args.capture_output,
                        },
                    }, ensure_ascii=False))
                    break

                # The searcher restores from the live engine snapshot captured
                # below, not from `encounter` — so a synthetic/unknown encounter
                # label is no obstacle to searching. Run search whenever the
                # policy asks for it and we have a combat state.
                if args.combat_policy == "search" and current_encounter_id is not None:
                    plan_length_before = len(planned_combat_sequence)
                    # Lazily create the shared worker pool exactly when a fresh
                    # search may run (a combat's first step can never reuse a plan).
                    if (
                        args.experimental_worker_pool
                        and args.reuse_cli_processes
                        and combat_worker_pool is None
                        and not planned_combat_sequence
                    ):
                        combat_worker_pool = CombatWorkerPool(cli_cfg)
                    step_cfg = CombatStepConfig(
                        cli_cfg=cli_cfg,
                        spec=CombatSpec(
                            character=args.character,
                            encounter=current_encounter_id,
                            seed=args.seed,
                            ascension=args.ascension,
                            lang=args.lang,
                        ),
                        depth=args.depth,
                        chance_depth=args.chance_depth,
                        score_mode=args.score_mode,
                        max_workers=args.max_workers,
                        reuse_cli_processes=args.reuse_cli_processes,
                        user_parallel=args.parallel_top_level,
                        floor=context.get("floor"),
                        room_type=current_room_type,
                        worker_pool=combat_worker_pool,
                        leaf_dump_sink=(leaf_collector.sink if leaf_collector else None),
                        leaf_dump_rate=args.leaf_dump_rate,
                        # Leaf collection needs every evaluated root edge so the
                        # dataset contains explicit engine counterfactuals.
                        # Trace-only runs keep their existing bounded top-k view.
                        capture_root_topk=(
                            -1 if leaf_collector is not None else
                            (args.dump_combat_trace_topk if args.dump_combat_trace else 0)
                        ),
                        max_search_ms=args.max_search_ms,
                        scorer_model=scoring_model,
                        evaluator_coefficients=dict(_default_deck_profile().get('combat_coefficients') or {}),
                    )
                    plan_state = PlanState(
                        sequence=planned_combat_sequence,
                        expected_state_hashes=planned_state_hashes,
                        expected_state_keys=planned_state_keys,
                    )
                    sr = decide_combat_action(cli, current_search_state, step_cfg, plan_state)
                    planned_combat_sequence = plan_state.sequence
                    planned_state_hashes = plan_state.expected_state_hashes
                    planned_state_keys = plan_state.expected_state_keys

                    # Bridge primitive result -> recording locals. Plan validation,
                    # reuse, fresh search, raw-retry and fallback all live in
                    # decide_combat_action;
                    # we surface the same warning line and per-step telemetry here.
                    if sr.plan_diverged is not None:
                        print(json.dumps({
                            "warning": "plan_reuse_board_diverged",
                            "step_id": step_id,
                            "encounter_id": current_encounter_id,
                            **sr.plan_diverged,
                            "note": "cached combat state diverged from the searched line; discarding plan and re-searching",
                        }, ensure_ascii=False))
                    chosen = sr.chosen
                    reused_plan = sr.reused_plan
                    search_failed = sr.search_failed
                    raw_retry_used = sr.raw_retry_used
                    raw_retry_recovered = sr.raw_retry_recovered
                    use_parallel_top_level = sr.parallel
                    search_snapshot_meta = sr.search_meta or {"potions_removed": 0}
                    ran_search = sr.ran_search
                    timing_summary = sr.searcher_timing_summary
                    searcher_timing = sr.searcher_timing
                    result_score = sr.search_score
                    result_nodes = sr.nodes
                    result_root_candidates = sr.root_candidates
                    _t = sr.timing
                    capture_ms = _t.get("capture_ms", 0.0)
                    export_ms = _t.get("export_ms", 0.0)
                    searcher_construct_ms = _t.get("construct_ms", 0.0)
                    close_ms = _t.get("close_ms", 0.0)
                    search_wall_ms = _t.get("search_ms", 0.0)
                    resolve_payload_ms = _t.get("resolve_ms", 0.0)


                    # DIAGNOSTIC (B-class slow hunt): when a search is
                    # pathologically slow, dump the raw root snapshot + timing so
                    # the full-restore storm can be reproduced offline. Gated by
                    # STS2_DUMP_SLOW_MS (unset => no-op). Rebuilt from the primitive
                    # result (sr) now that search lives in decide_combat_action.
                    _dump_ms = os.environ.get("STS2_DUMP_SLOW_MS")
                    if ran_search and _dump_ms and search_wall_ms >= float(_dump_ms):
                        try:
                            _tm = searcher_timing
                            _restore_total = (_tm.get("replay_total_ms") or 0.0)
                            _calls = _tm.get("replay_calls") or 1
                            _ddir = Path(os.environ.get("STS2_DUMP_SLOW_DIR") or "analysis/slow_snapshots")
                            _ddir.mkdir(parents=True, exist_ok=True)
                            _stem = f"seed{args.seed}_step{step_id}_{current_encounter_id}"
                            (_ddir / f"{_stem}.json").write_text(json.dumps({
                                "seed": args.seed, "step_id": step_id,
                                "floor": _run_floor(current_search_state),
                                "encounter_id": current_encounter_id,
                                "search_wall_ms": search_wall_ms,
                                "nodes": result_nodes,
                                "replay_calls": _calls,
                                "restore_mode_full_hits": _tm.get("restore_mode_full_hits"),
                                "restore_mode_in_place_hits": _tm.get("restore_mode_in_place_hits"),
                                "in_place_fail_reasons": _tm.get("in_place_fail_reasons"),
                                "avg_replay_ms": round(_restore_total / max(1, _calls), 1),
                                "snapshot_json": sr.root_snapshot_json,
                            }, ensure_ascii=False))
                        except Exception as _dex:
                            print(f"[DUMP_SLOW failed: {_dex}]", flush=True)

                    action, payload = sr.action, sr.payload
                    chosen_summary = (
                        {
                            "action_type": chosen.action_type,
                            "card_index": chosen.card_index,
                            "target_index": chosen.target_index,
                            "metadata": chosen.metadata,
                        }
                        if chosen is not None
                        else summarize_live_combat_action(current_search_state, action, payload)
                    )
                    sequence_summary = (
                        [
                            {
                                "action_type": a.action_type,
                                "card_index": a.card_index,
                                "target_index": a.target_index,
                                "metadata": a.metadata,
                            }
                            for a in ([chosen] + planned_combat_sequence)
                        ]
                        if chosen is not None
                        else [chosen_summary]
                    )
                    resolved_summary = resolved_combat_action_summary(
                        current_search_state,
                        action,
                        payload,
                    )
                    event["combat_action"] = {
                        "encounter_id": current_encounter_id,
                        "parallel_top_level": use_parallel_top_level,
                        "reused_plan": reused_plan,
                        "planned_actions_before": plan_length_before,
                        "planned_actions_after": len(planned_combat_sequence),
                        "search_wall_ms": round(search_wall_ms, 3),
                        "capture_snapshot_ms": round(capture_ms, 3),
                        "export_snapshot_ms": round(export_ms, 3),
                        "searcher_construct_ms": round(searcher_construct_ms, 3),
                        "searcher_close_ms": round(close_ms, 3),
                        "resolve_payload_ms": round(resolve_payload_ms, 3),
                        "search_snapshot_meta": search_snapshot_meta,
                        "search_failed": search_failed,
                        "raw_retry_used": raw_retry_used,
                        "raw_retry_recovered": raw_retry_recovered,
                        "replay_calls": searcher_timing.get("replay_calls") if ran_search else 0,
                        "replay_total_ms": round(float(searcher_timing.get("replay_total_ms") or 0.0), 3) if ran_search else 0.0,
                        "replay_avg_ms": round(float(timing_summary.get("replay_avg_ms") or 0.0), 3),
                        "replay_max_ms": round(float(timing_summary.get("replay_max_ms") or 0.0), 3),
                        "branch_avg_ms": round(float(timing_summary.get("branch_avg_ms") or 0.0), 3),
                        "branch_max_ms": round(float(timing_summary.get("branch_max_ms") or 0.0), 3),
                        "snapshot_restore_hits": int(timing_summary.get("snapshot_restore_hits") or 0),
                        "snapshot_capture_count": int(timing_summary.get("snapshot_capture_count") or 0),
                        "subtree_cache_hits": int(timing_summary.get("subtree_cache_hits") or 0),
                        "eval_cache_hits": int(timing_summary.get("eval_cache_hits") or 0),
                        "time_budget_ms": round(float(timing_summary.get("time_budget_ms") or 0.0), 3),
                        "time_budget_exhausted": bool(timing_summary.get("time_budget_exhausted") or False),
                        "replay_cost_breakdown": {
                            key: round(float(value), 3)
                            for key, value in (timing_summary.get("replay_cost_breakdown") or {}).items()
                        },
                        "chosen": chosen_summary,
                        "resolved": resolved_summary,
                        "score": result_score if ran_search else None,
                        "nodes": result_nodes if ran_search else None,
                        "sequence": sequence_summary,
                    }
                    if trace_fh is not None:
                        trace_row = {
                            "seed": str(args.seed),
                            "step_id": step_id,
                            "combats_seen": combats_seen,
                            "combat_action_index": combat_action_index,
                            "encounter_id": current_encounter_id,
                            "board": combat_full_board(current_search_state),
                            "chosen": chosen_summary,
                            "resolved": resolved_summary,
                            "chosen_line": sequence_summary,
                            "chosen_score": result_score if ran_search else None,
                            "nodes": result_nodes if ran_search else None,
                            "reused_plan": reused_plan,
                            "root_candidates": result_root_candidates if ran_search else [],
                        }
                        trace_fh.write(json.dumps(trace_row, ensure_ascii=False) + "\n")
                        trace_fh.flush()
                else:
                    if args.combat_policy == "naive":
                        action, payload = choose_combat_naive(current_search_state)
                    elif args.combat_policy == "random":
                        action, payload = choose_combat_random(current_search_state, combat_rng)
                    else:
                        action, payload = choose_combat_fallback(current_search_state)
                    planned_combat_sequence = []
                    planned_state_hashes = []
                    planned_state_keys = []
                    event["combat_action"] = {
                        "encounter_id": None,
                        "chosen": {"action_type": action, "payload": payload},
                        "score": None,
                        "nodes": None,
                        "fallback": args.combat_policy != "search",
                        "combat_policy": args.combat_policy,
                        "live_signature": {
                            "enemy_ids": list(_enemy_ids_signature(current_search_state)),
                            "summary": combat_summary(current_search_state),
                        },
                    }
            elif decision == "shop":
                event["shop_context"] = shop_context_summary(state)
                event["shop_scores"] = score_shop_options(state, repo_root)
                if args.capture_presave:
                    event["presave"] = write_presave(
                        cli, args.capture_presave, seed=str(args.seed),
                        character=args.character, step_id=step_id,
                        kind="shop", state=state)
                shop_pick = choose_shop_action(state, repo_root)
                if shop_pick is None:
                    action = "leave_room"
                    payload = {}
                    event["shop_choice"] = {"action": action}
                    pending_shop_removal = None
                else:
                    action = str(shop_pick.get("action") or "leave_room")
                    payload = {k: v for k, v in shop_pick.items() if k != "action"}
                    event["shop_choice"] = {
                        "action": action,
                        "card_index": payload.get("card_index"),
                        "item_id": shop_item_label(state, payload),
                    }
                    if action == "remove_card":
                        pending_shop_removal = dict(shop_pick.get("remove_target") or {})
                        event["shop_choice"]["remove_target"] = pending_shop_removal
                    else:
                        pending_shop_removal = None
            elif decision in ("treasure", "treasure_relic", "treasure_complete"):
                action, payload = choose_treasure_action(state)
            elif decision == "unknown":
                action = "proceed"
                payload = {}
            elif decision in ("game_over", "victory"):
                event["terminal"] = state
                _record_floor_hp(state)
                deck_snapshots.append({"floor": _run_floor(state), **_deck_scalars(state)})
                print(json.dumps(event, ensure_ascii=False))
                _emit_run_summary(decision)
                break
            else:
                action = "proceed"
                payload = {}

            before_state = state
            cli, result, restart_cli_ms, action_call_ms = apply_transition_action_with_recovery(
                cli,
                cli_cfg,
                state,
                decision,
                action,
                payload,
                args.lang,
                save_tag=f"{args.seed}_{step_id}_{decision}",
                assume_transition_fresh=(decision == "map_select"),
            )
            state = result
            _record_act(state)
            if decision == "card_select" and state.get("decision") != "card_select":
                pending_shop_removal = None
                pending_card_select_context = None
            elif decision in {"event_choice", "rest_site"} and state.get("decision") != "card_select":
                # A context belongs to exactly one immediate selection
                # transaction.  Never leak it into an unrelated future screen.
                pending_card_select_context = None
            if state.get("type") == "error":
                event["error"] = state
            elif state.get("decision") is None and decision != "combat_play":
                refresh_started = time.perf_counter()
                refreshed = cli.get_search_state()
                refresh_ms = (time.perf_counter() - refresh_started) * 1000.0
                if refreshed.get("type") == "search_state_result" and refreshed.get("success"):
                    state = refreshed
            if decision == "combat_play" and state.get("decision") != "combat_play":
                if leaf_collector is not None:
                    next_decision = str(state.get("decision") or "")
                    won = next_decision not in {"game_over", "defeat"}
                    end_player = state.get("player") or {}
                    end_hp = float(end_player.get("hp") or 0.0)
                    end_max_hp = float(end_player.get("max_hp") or 1.0)
                    retained = (end_hp / end_max_hp) if end_max_hp > 0 else 0.0
                    leaf_collector.finish_combat(
                        encounter_id=current_encounter_id,
                        retained_hp_fraction=retained,
                        won=won,
                    )
                combat_history = []
                current_encounter_id = None
                combat_action_index = 0
                if combat_worker_pool is not None:
                    combat_worker_pool.close()
                    combat_worker_pool = None
                planned_combat_sequence = []
                planned_state_hashes = []
                planned_state_keys = []
                planned_turn_marker = None
                last_logged_turn_marker = None
                event["combat_end"] = {
                    "next_decision": state.get("decision"),
                    "context": state.get("context"),
                    "result_type": state.get("type"),
                }
            post_context = state.get("context") or {}
            event["post_state"] = {
                "type": state.get("type"),
                "decision": state.get("decision"),
                "floor": post_context.get("floor"),
                "act": post_context.get("act"),
                "current_hp": (state.get("player") or {}).get("hp"),
                "max_hp": (state.get("player") or {}).get("max_hp"),
                "gold": (state.get("player") or {}).get("gold"),
            }
            event["applied"] = {"action": action, "payload": payload}
            event["runtime"] = {
                "step_wall_ms": round((time.perf_counter() - step_started) * 1000.0, 3),
                "restart_cli_ms": round(restart_cli_ms, 3),
                "action_call_ms": round(action_call_ms, 3),
                "refresh_ms": round(refresh_ms, 3),
            }
            progress_guard.check(before_state, state, decision, action, payload)
            print(json.dumps(event, ensure_ascii=False))
            if state.get("type") == "error":
                _record_floor_hp(state)
                deck_snapshots.append({"floor": _run_floor(state), **_deck_scalars(state)})
                _emit_run_summary("error", error_state=state)
                break
        else:
            # loop exhausted max_steps without hitting a terminal decision
            _record_floor_hp(state)
            deck_snapshots.append({"floor": _run_floor(state), **_deck_scalars(state)})
            _emit_run_summary("max_steps")
    except Exception as exc:
        error_state = {
            "type": "error",
            "message": str(exc),
            "decision": state.get("decision") if isinstance(state, dict) else None,
            "context": (state.get("context") if isinstance(state, dict) else None) or {},
        }
        try:
            _record_floor_hp(state)
            deck_snapshots.append({"floor": _run_floor(state), **_deck_scalars(state)})
        except Exception:
            pass
        if current_event is not None:
            current_event["error"] = error_state
            print(json.dumps(current_event, ensure_ascii=False))
        _emit_run_summary("error", error_state=error_state)
        raise
    finally:
        if trace_fh is not None:
            try:
                trace_fh.close()
            except Exception:
                pass
        if combat_worker_pool is not None:
            try:
                combat_worker_pool.close()
            except Exception:
                pass
        cli.stop()


if __name__ == "__main__":
    main()
