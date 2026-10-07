from __future__ import annotations

import json
import math
import os
import sys
from typing import Any, Dict, Iterable, Mapping, Optional

from controller.combat_intent import intent_deals_damage, intent_total_damage
from controller.search.card_strength import expected_future_attack_plays, expected_visible_draw_pool_strength
from controller.search.boss_residual import boss_residual


# One-shot guard so a spurious "victory with living enemies" is flagged loudly
# but does not spam stderr (evaluate_leaf runs once per search leaf).
_FALSE_VICTORY_WARNED = False


def _warn_false_victory(terminal_decision: str, surviving_enemy_hp: float) -> None:
    global _FALSE_VICTORY_WARNED
    if _FALSE_VICTORY_WARNED:
        return
    _FALSE_VICTORY_WARNED = True
    print(
        json.dumps(
            {
                "warning": "false_victory_rejected",
                "terminal_decision": terminal_decision,
                "surviving_enemy_hp": surviving_enemy_hp,
                "note": (
                    "engine reported a combat-end decision while enemies were "
                    "still alive; rejected the invalid result "
                    "(further occurrences this process "
                    "suppressed)"
                ),
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )


# Path to the learned leaf-value weights produced by tools/fit_leaf_value.py.
# Overridable via env for experiments. Loaded lazily, once, on first "learned"
# evaluation (see _learned_weights).
_LEARNED_WEIGHTS_PATH = os.environ.get(
    "LEAF_WEIGHTS_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "learning", "leaf_weights.json"),
)

# The learned model is fit toward an N-step discounted retained-HP return in
# roughly [0, 1]. The search's terminal bonuses are ±1e6 and the handcrafted
# in-combat scores live in the hundreds. To keep the learned in-combat value
# comparable to the handcrafted modes (so non-terminal leaves are ranked on the
# same order of magnitude the rest of the search expects) we scale the learned
# value up by this factor. Terminal leaves are NOT touched (see evaluate_leaf):
# the ±1e6 win/death lines are preserved regardless of mode.
_LEARNED_VALUE_SCALE = float(os.environ.get("LEAF_LEARNED_SCALE", "1000.0"))

_LEARNED_CACHE: Optional[Dict[str, Any]] = None


def _learned_weights() -> Optional[Dict[str, Any]]:
    """Lazily load and cache the learned leaf-value weights.

    Returns None if the file is missing or malformed, so the caller can fall
    back to a handcrafted mode rather than crash. Inference reproduces the fit
    tool's mapping exactly: the exported `intercept` is in standardized space
    and `weights` are already mapped back to raw-feature scale, so the raw-scale
    prediction is intercept + Σ w_raw[k] * (feat[k] - feature_mean[k]).
    """
    global _LEARNED_CACHE
    if _LEARNED_CACHE is not None:
        return _LEARNED_CACHE or None
    try:
        with open(_LEARNED_WEIGHTS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        # Validate the pieces inference needs.
        if not (data.get("feature_order") and isinstance(data.get("weights"), dict)):
            _LEARNED_CACHE = {}
            return None
        _LEARNED_CACHE = {
            "feature_order": list(data["feature_order"]),
            "weights": dict(data["weights"]),
            "intercept": float(data.get("intercept") or 0.0),
            "feature_mean": dict(data.get("feature_mean") or {}),
        }
        return _LEARNED_CACHE
    except (OSError, ValueError, KeyError):
        _LEARNED_CACHE = {}
        return None


PLAYER_POWER_WEIGHTS = {
    "STRENGTH": 6.0,
    "DEXTERITY": 4.0,
    "METALLICIZE": 3.0,
    "BARRICADE": 8.0,
    "VULNERABLE": -5.0,
    "WEAK": -4.0,
    "FRAIL": -4.0,
}

POSITIVE_PLAYER_POWER_IDS = {
    "BARRICADE_POWER",
    "COLOSSUS_POWER",
    "CRIMSON_MANTLE_POWER",
    "DARK_EMBRACE_POWER",
    "DRUM_OF_BATTLE_POWER",
    "DUPLICATION_POWER",
    "FEEL_NO_PAIN_POWER",
    "FLAME_BARRIER_POWER",
    "FREE_ATTACK_POWER",
    "HELLRAISER_POWER",
    "INFERNO_POWER",
    "INFLAME_POWER",
    "METALLICIZE_POWER",
    "ONE_TWO_PUNCH_POWER",
    "PLATING_POWER",
    "PYRE_POWER",
    "RAGE_POWER",
    "SETUP_STRIKE_POWER",
    "STAMPEDE_POWER",
    "STONE_ARMOR_POWER",
    "STRENGTH_POWER",
    "VICIOUS_POWER",
}

ENEMY_POWER_WEIGHTS = {
    "VULNERABLE": 4.0,
    "WEAK": 2.0,
    "POISON": 3.0,
    "STRENGTH": -5.0,
    "METALLICIZE": -3.0,
}


def _power_score(powers: Iterable[Dict[str, Any]], weights: Dict[str, float]) -> float:
    total = 0.0
    for power in powers:
        power_id = str(power.get("id") or "").upper()
        amount = int(power.get("amount") or 0)
        total += weights.get(power_id, 0.0) * amount
    return total


def _incoming_damage(enemies: Iterable[Dict[str, Any]]) -> float:
    total = 0.0
    for enemy in enemies:
        intent = enemy.get("intent") or {}
        if not intent_deals_damage(intent):
            continue
        total += intent_total_damage(intent)
    return total


def _enemy_intent_hp_weight(enemy: Dict[str, Any]) -> float:
    intent = enemy.get("intent") or {}
    intent_types = intent.get("intent_types") or []
    weight = 1.0
    if intent_deals_damage(intent):
        weight += 0.08 * max(1.0, intent_total_damage(intent))
    elif any(t in intent_types for t in ("Debuff", "DebuffStrong")):
        weight += 0.15
    elif "Buff" in intent_types:
        weight += 0.05
    elif "Summon" in intent_types:
        weight += 0.1
    return weight


def _weighted_enemy_hp(enemies: Iterable[Dict[str, Any]]) -> float:
    total = 0.0
    for enemy in enemies:
        hp = float(enemy.get("hp") or 0.0)
        total += hp * _enemy_intent_hp_weight(enemy)
    return total


def _enemy_hp(enemies: Iterable[Dict[str, Any]]) -> float:
    return float(sum(float(enemy.get("hp") or 0.0) for enemy in enemies))


def _weighted_enemy_hp_square(enemies: Iterable[Dict[str, Any]]) -> float:
    total = 0.0
    for enemy in enemies:
        hp = float(enemy.get("hp") or 0.0)
        total += (hp * hp) * _enemy_intent_hp_weight(enemy)
    return total


def _root_weighted_enemy_losses(root_enemies, enemies) -> tuple[float, float]:
    """Use the same root threat weights at both ends of an HP comparison.

    Exported enemy indexes compact after deaths, so they are not identities.
    Same-model enemies share a root mean weight; new/summoned model populations
    fall back to unweighted HP instead of guessing a correspondence.
    """
    def key(enemy):
        return str(enemy.get('monster_id') or enemy.get('enemy_id') or enemy.get('id') or '')

    groups = {}
    current_counts = {}
    for enemy in root_enemies:
        groups.setdefault(key(enemy), []).append(_enemy_intent_hp_weight(enemy))
    for enemy in enemies:
        identity = key(enemy)
        current_counts[identity] = current_counts.get(identity, 0) + 1
    matched = all(count <= len(groups.get(identity, [])) for identity, count in current_counts.items())
    weights = {identity: sum(values) / len(values) for identity, values in groups.items()} if matched else {}

    def total(rows, exponent):
        return sum(float(enemy.get('hp') or 0) ** exponent * weights.get(key(enemy), 1.0)
                   for enemy in rows)

    return (max(0.0, total(root_enemies, 1) - total(enemies, 1)),
            max(0.0, total(root_enemies, 2) - total(enemies, 2)))


def _enemy_hp_square(enemies: Iterable[Dict[str, Any]]) -> float:
    return float(sum(float(enemy.get("hp") or 0.0) ** 2 for enemy in enemies))


def weighted_enemy_hp(enemies: Iterable[Dict[str, Any]]) -> float:
    return _weighted_enemy_hp(enemies)


def _is_minion(enemy: Dict[str, Any]) -> bool:
    """A summoned/expendable add (e.g. KIN_FOLLOWER), not a priority target.

    Engine marks these with MINION_POWER. Killing minions rarely ends a boss
    fight; the core enemy (priest/boss) is what must be focused. Used to weight
    damage by target so the eval stops treating all enemy HP as fungible.
    """
    for p in enemy.get("powers") or []:
        if str(p.get("id") or "").upper() == "MINION_POWER":
            return True
    return False


def _priority_enemy_hp(enemies: Iterable[Dict[str, Any]]) -> float:
    """Total HP of NON-minion (priority) enemies only."""
    return float(sum(int(e.get("hp") or 0) for e in enemies if not _is_minion(e)))


# P4: debuffs that don't move HP (Vulnerable/Weak) are invisible to the HP/block
# features, so the eval scored "Weak on the priest" identically to "Weak on a
# minion" — the agent dumped debuff potions onto adds that drive nothing. Encode
# the stacks of these debuffs on PRIORITY (non-minion) enemies so target choice
# becomes visible: a Weak/Vulnerable landed on the core enemy is worth more than
# one landed on an expendable add.
_PRIORITY_DEBUFF_POWER_IDS = ("VULNERABLE_POWER", "WEAK_POWER")


def _priority_debuff_stacks(enemies: Iterable[Dict[str, Any]]) -> float:
    """Sum of Vulnerable/Weak stacks on non-minion (priority) enemies."""
    total = 0.0
    for enemy in enemies:
        if _is_minion(enemy):
            continue
        for power in enemy.get("powers") or []:
            if str(power.get("id") or "").upper() in _PRIORITY_DEBUFF_POWER_IDS:
                try:
                    total += float(power.get("amount") or 0.0)
                except (TypeError, ValueError):
                    continue
    return total


def _positive_player_power_stock(powers: Iterable[Dict[str, Any]]) -> float:
    total = 0.0
    for power in powers:
        power_id = str(power.get("id") or "").upper()
        if power_id not in POSITIVE_PLAYER_POWER_IDS:
            continue
        try:
            amount = float(power.get("amount") or 0.0)
        except (TypeError, ValueError):
            continue
        total += max(0.0, amount)
    return total


def _power_amount(powers: Iterable[Dict[str, Any]], *power_ids: str) -> float:
    wanted = {value.upper() for value in power_ids}
    total = 0.0
    for power in powers:
        if str(power.get('id') or power.get('power_id') or '').split('.')[-1].upper() not in wanted:
            continue
        try:
            total += float(power.get('amount') or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def _visible_upgrade_count(combat: Dict[str, Any]) -> float:
    total = 0.0
    for key in ("hand", "draw_pile", "discard_pile"):
        for card in combat.get(key) or []:
            total += float(card.get("upgrade") or 0.0)
    return total


def _eliminated_theft_value(
    root_enemies: Iterable[Dict[str, Any]],
    current_enemies: Iterable[Dict[str, Any]],
) -> tuple[float, float]:
    """Return gold and card theft prevented by killing the responsible enemy."""
    remaining_ids = [
        str(enemy.get('monster_id') or enemy.get('id') or '').upper()
        for enemy in current_enemies
    ]
    prevented_gold = 0.0
    prevented_cards = 0.0
    for enemy in root_enemies:
        enemy_id = str(enemy.get('monster_id') or enemy.get('id') or '').upper()
        if enemy_id in remaining_ids:
            remaining_ids.remove(enemy_id)
            continue
        powers = enemy.get('powers') or []
        for power in powers:
            if str(power.get('id') or power.get('power_id') or '').upper() == 'HEIST_POWER':
                prevented_gold += max(0.0, float(power.get('amount') or 0.0))
        intent = enemy.get('intent') or {}
        intent_types = {str(value) for value in (intent.get('intent_types') or [])}
        if enemy_id == 'THIEVING_HOPPER' and 'CardDebuff' in intent_types:
            prevented_cards += 1.0
    return prevented_gold, prevented_cards


def extract_leaf_features(
    search_state: Mapping[str, Any],
    root_combat: Mapping[str, Any],
) -> Dict[str, float]:
    """Compute the relative-to-root combat leaf features.

    This is the single source of truth for the feature vector used both by the
    hand-tuned weighted-sum scoring in ``evaluate_leaf`` (the ``root_combat``
    branch) and by the learned leaf-value model. Keeping one extractor
    guarantees the ``x`` seen at training time is byte-for-byte the ``x`` scored
    at search time. Values match the original inline computation exactly.
    """
    combat = search_state.get("combat") or {}
    player = combat.get("player") or {}
    enemies = combat.get("enemies") or []

    root_player = root_combat.get("player") or {}
    root_enemies = root_combat.get("enemies") or []
    root_player_hp = float(root_player.get("hp") or 0)
    root_enemy_total_hp = float(sum(int(e.get("hp") or 0) for e in root_enemies))
    root_enemy_unweighted_hp = _enemy_hp(root_enemies)
    root_enemy_unweighted_hp_square = _enemy_hp_square(root_enemies)
    root_priority_enemy_hp = _priority_enemy_hp(root_enemies)
    current_enemy_total_hp = float(sum(int(e.get("hp") or 0) for e in enemies))
    weighted_hp_loss, focused_hp_loss = _root_weighted_enemy_losses(root_enemies, enemies)
    current_enemy_unweighted_hp = _enemy_hp(enemies)
    current_enemy_unweighted_hp_square = _enemy_hp_square(enemies)
    current_priority_enemy_hp = _priority_enemy_hp(enemies)
    root_priority_debuff = _priority_debuff_stacks(root_enemies)
    current_priority_debuff = _priority_debuff_stacks(enemies)
    root_player_power_stock = _positive_player_power_stock(root_player.get("powers") or [])
    current_player_power_stock = _positive_player_power_stock(player.get("powers") or [])
    root_strength = _power_amount(root_player.get('powers') or [], 'STRENGTH', 'STRENGTH_POWER')
    current_strength = _power_amount(player.get('powers') or [], 'STRENGTH', 'STRENGTH_POWER')
    prevented_gold_theft, prevented_card_theft = _eliminated_theft_value(root_enemies, enemies)

    player_hp = float(player.get("hp") or 0)
    incoming_damage = _incoming_damage(enemies)
    player_block = float(player.get("block") or 0)
    remaining_turns = min(4.0, max(1.0, current_priority_enemy_hp / 18.0)) if enemies else 0.0
    strength_gain = max(0.0, current_strength - root_strength)
    future_attack_plays = expected_future_attack_plays(dict(search_state), remaining_turns)

    return {
        "enemy_hp_loss": max(0.0, root_enemy_total_hp - current_enemy_total_hp),
        "weighted_enemy_hp_loss": weighted_hp_loss,
        "unweighted_enemy_hp_loss": max(0.0, root_enemy_unweighted_hp - current_enemy_unweighted_hp),
        "focused_enemy_hp_loss": focused_hp_loss,
        "unweighted_focused_enemy_hp_loss": max(
            0.0,
            root_enemy_unweighted_hp_square - current_enemy_unweighted_hp_square,
        ),
        "priority_enemy_hp_loss": max(0.0, root_priority_enemy_hp - current_priority_enemy_hp),
        # P4: net Vulnerable/Weak stacks applied to priority (non-minion) enemies
        # this line. Makes debuff target choice visible to the eval so the search
        # prefers landing Weak/Vulnerable on the core enemy over an expendable add.
        "priority_debuff_gain": max(0.0, current_priority_debuff - root_priority_debuff),
        "player_hp_loss": max(0.0, root_player_hp - player_hp),
        "incoming_damage": incoming_damage,
        "player_block": player_block,
        "unblocked_damage": max(0.0, incoming_damage - player_block),
        "enemy_kills": max(0.0, float(len(root_enemies) - len(enemies))),
        "visible_upgrade_bonus": _visible_upgrade_count(combat),
        "hand_count": float(len(combat.get("hand") or [])),
        "enemy_count": float(len(enemies)),
        "energy": float(player.get("energy") or 0),
        "player_power_stock_gain": max(0.0, current_player_power_stock - root_player_power_stock),
        'strength_gain': strength_gain,
        'future_attack_plays': future_attack_plays,
        'strength_future_value': strength_gain * future_attack_plays,
        "prevented_gold_theft": prevented_gold_theft,
        "prevented_card_theft": prevented_card_theft,
    }


def evaluate_leaf(
    search_state: Dict[str, Any],
    root_combat: Optional[Mapping[str, Any]] = None,
    mode: str = "defense_first",
    coefficients: Optional[Mapping[str, float]] = None,
) -> float:
    if search_state.get('success') is False:
        return float('-inf')
    terminal_decision = str(search_state.get("terminal_decision") or "")
    terminal_result = search_state.get("terminal_result") or {}
    if terminal_decision:
        player = terminal_result.get("player") or {}
        hp = float(player.get("hp") or 0.0)
        max_hp = float(player.get("max_hp") or 1.0)
        hp_ratio = hp / max_hp if max_hp > 0 else 0.0
        if terminal_decision in {"game_over", "defeat"}:
            return -1_000_000.0 + hp
        if terminal_decision in {"error", "failed", "search_state_result"}:
            # The line reached an ERROR/failed frame, not a real decision: the
            # action result came back type='error' (e.g. a churn-induced "Current
            # state is not combat_play" / "Not in combat" on a warm worker), so
            # _build_terminal_search_state stamped terminal_decision='error' and
            # an EMPTY enemy list. Falling through to normal leaf scoring reads
            # enemies=[] as "all enemies dead" -> enemy_hp_loss == full root HP,
            # enemy_kills == enemy_count -> a 1800-3600 PHANTOM score that makes
            # the search chase a corrupt line (observed: seed42 THE_KIN s14+,
            # cur_enemies=[] player_hp=0 scoring 1866.9). Treat it as a failed
            # line, not a comparable alternative even to a real defeat.
            return float('-inf')
        if terminal_decision in {"card_select", "bundle_select", "unknown"}:
            # These are unresolved intermediate decision states. The current combat
            # search does not model the follow-up choice, so treating them as good
            # leaves creates a tree-semantic bug (for example, overvaluing
            # Colorless Potion because it opens a card-select modal). Exclude
            # them until they are modeled explicitly.
            return float('-inf')
        if terminal_decision in {"card_reward", "treasure", "map_select", "rest_site", "shop", "victory"}:
            # Sanity-guard the victory bonus: a real combat-end has no living
            # enemies. If the engine still reports surviving enemy HP at this
            # "terminal" decision, the victory is spurious (historically a
            # corrupt restore that dropped the enemy list) — awarding +1e6 here
            # makes the search pass the turn into a loss. Reject and flag it once.
            surviving_enemy_hp = float(search_state.get("terminal_surviving_enemy_hp") or 0.0)
            if surviving_enemy_hp > 0:
                _warn_false_victory(terminal_decision, surviving_enemy_hp)
                return float('-inf')
            # End-turn and enemy-turn effects can legitimately finish a combat.
            # Terminal lines still need to compete with each other.  In
            # particular, enemies such as thieves can end the fight while
            # escaping with gold; treating every victory as the same +1e6
            # score makes the search keep the first terminal edge it sees and
            # can prefer a no-op over killing the thief.  Preserve the root
            # resources in the terminal tie-breaker so lethal lines remain
            # dominant while resource-saving lethal lines win among them.
            root_player = (root_combat or {}).get("player") or {}
            root_gold = float(root_player.get("gold") or 0.0)
            terminal_gold = float(player.get("gold") or 0.0)
            gold_change = terminal_gold - root_gold
            return 1_000_000.0 + hp_ratio * 100.0 + hp + gold_change * 0.25

    combat = search_state.get("combat") or {}
    player = combat.get("player") or {}
    enemies = combat.get("enemies") or []

    player_hp = float(player.get("hp") or 0)
    energy = float(player.get("energy") or 0)
    hand_count = float(len(combat.get("hand") or []))
    enemy_count = float(len(enemies))

    score = 0.0
    if root_combat:
        feats = extract_leaf_features(search_state, root_combat)
        if mode == "learned":
            learned = _learned_weights()
            if learned is not None:
                # Reproduce the fit tool's raw-scale prediction:
                #   intercept + Σ w_raw[k] * (feat[k] - feature_mean[k]).
                # draw_pool_strength is the one feature not in extract_leaf_features;
                # compute it the same way combat_search does at search time.
                feat_vals = dict(feats)
                feat_vals["draw_pool_strength"] = expected_visible_draw_pool_strength(search_state)
                weights = learned["weights"]
                means = learned["feature_mean"]
                value = learned["intercept"]
                for key in learned["feature_order"]:
                    value += weights.get(key, 0.0) * (float(feat_vals.get(key, 0.0)) - float(means.get(key, 0.0)))
                # Scale the [0,1]-ish learned value into the search's working
                # range; terminal ±1e6 lines above are untouched.
                return value * _LEARNED_VALUE_SCALE
            # Weights unavailable: fall through to the handcrafted default below.
        enemy_hp_loss = feats["enemy_hp_loss"]
        weighted_enemy_hp_loss = feats["weighted_enemy_hp_loss"]
        unweighted_enemy_hp_loss = feats["unweighted_enemy_hp_loss"]
        focused_enemy_hp_loss = feats["focused_enemy_hp_loss"]
        unweighted_focused_enemy_hp_loss = feats["unweighted_focused_enemy_hp_loss"]
        player_hp_loss = feats["player_hp_loss"]
        incoming_damage = feats["incoming_damage"]
        player_block = feats["player_block"]
        unblocked_damage = feats["unblocked_damage"]
        enemy_kills = feats["enemy_kills"]
        visible_upgrade_bonus = feats["visible_upgrade_bonus"]
        priority_enemy_hp_loss = feats["priority_enemy_hp_loss"]
        priority_debuff_gain = feats["priority_debuff_gain"]
        prevented_gold_theft = feats["prevented_gold_theft"]
        prevented_card_theft = feats["prevented_card_theft"]
        if mode == "damage_first":
            score += enemy_hp_loss * 3.0
            score += weighted_enemy_hp_loss * 0.15
            score -= player_hp_loss * 2.0
            score += player_block * 0.5
            score -= unblocked_damage * 2.0
            score += prevented_gold_theft * 0.25
            score += prevented_card_theft * 18.0
        elif mode in {"balanced", "balanced_nosquare", "balanced_unweighted", "balanced_power"}:
            score += enemy_hp_loss * 3.0
            # Target discrimination: damage to non-minion (boss/core) enemies
            # counts EXTRA on top of the fungible total, so the search focuses the
            # priority target instead of treating all enemy HP as interchangeable.
            # Without this, "hit priest for 6" and "hit follower for 6" score
            # identically (both move enemy_hp_loss by 6), which is why the agent
            # dumped its burst into a minion while the priest stayed at full HP.
            score += priority_enemy_hp_loss * 3.0
            score += (unweighted_enemy_hp_loss if mode == "balanced_unweighted" else weighted_enemy_hp_loss) * 0.45
            if mode != "balanced_nosquare":
                score += (
                    unweighted_focused_enemy_hp_loss
                    if mode == "balanced_unweighted"
                    else focused_enemy_hp_loss
                ) * 0.02
            if mode == "balanced_power":
                score += feats["player_power_stock_gain"] * 7.5
            # P4: reward Weak/Vulnerable landed on priority (non-minion) enemies
            # so HP-neutral debuffs are no longer target-blind. Modest weight: a
            # debuff is setup, not damage; it should tilt target choice without
            # outweighing actual HP removal.
            score += priority_debuff_gain * 2.0
            score += prevented_gold_theft * 0.25
            score += prevented_card_theft * 18.0
            score += visible_upgrade_bonus * 0.35
            score -= player_hp_loss * 5.0
            score += player_block * 0.75
            score -= unblocked_damage * 3.5
            # P3: the flat per-kill bonus was 18.0, which manufactured a
            # kill-fixation — the search would burst a harmless add for +18
            # while a high-threat core enemy lived. Lowered so a kill is still
            # rewarded but no longer dominates threat-weighted HP loss
            # (priority_enemy_hp_loss / unblocked_damage). Killing the RIGHT
            # enemy is already captured by the weighted terms above.
            score += enemy_kills * 8.0
        elif mode == "balanced_future":
            score += enemy_hp_loss * 3.2
            score += weighted_enemy_hp_loss * 0.5
            score += focused_enemy_hp_loss * 0.025
            score += visible_upgrade_bonus * 0.4
            score -= player_hp_loss * 4.5
            score += player_block * 0.55
            score -= unblocked_damage * 3.8
            score += enemy_kills * 20.0
            score += prevented_gold_theft * 0.25
            score += prevented_card_theft * 18.0
        elif mode == "defense_first":
            score += enemy_hp_loss * 0.5
            score += weighted_enemy_hp_loss * 0.2
            score -= player_hp_loss * 25.0
            score += player_block * 8.0
            score -= unblocked_damage * 20.0
            score += prevented_gold_theft * 0.25
            score += prevented_card_theft * 18.0
        else:
            score += enemy_hp_loss
            score += weighted_enemy_hp_loss * 0.25
            score -= player_hp_loss * 5.0
            score += player_block * 3.0
            score -= unblocked_damage * 5.0
            score += prevented_gold_theft * 0.25
            score += prevented_card_theft * 18.0
    else:
        enemy_total_hp = float(sum(int(e.get("hp") or 0) for e in enemies))
        incoming_damage = _incoming_damage(enemies)
        player_block = float(player.get("block") or 0)
        hp_loss = max(0.0, incoming_damage - player_block)
        score += player_hp * 0.2
        score += player_block * 1.25
        score += hand_count * 0.15
        score += _power_score(player.get("powers") or [], PLAYER_POWER_WEIGHTS)
        score += _power_score(
            [p for e in enemies for p in (e.get("powers") or [])],
            ENEMY_POWER_WEIGHTS,
        )
        score -= enemy_total_hp * 3.0
        score -= enemy_count * 12.0
        score -= energy * 0.5
        score -= 8.0 * hp_loss

    if root_combat and coefficients:
        score += feats['player_hp_loss'] * float(coefficients.get('player_hp_loss_delta') or 0.0)
        score += feats['player_block'] * float(coefficients.get('player_block_delta') or 0.0)
        score += feats['unblocked_damage'] * float(coefficients.get('unblocked_damage_delta') or 0.0)
        score += feats['player_power_stock_gain'] * float(coefficients.get('power_stock_delta') or 0.0)
        score += feats['strength_future_value'] * float(coefficients.get('strength_future_delta') or 0.0)
        score += feats['energy'] * float(coefficients.get('energy_delta') or 0.0)
        score += feats['hand_count'] * float(coefficients.get('hand_count_delta') or 0.0)

    # minor tie-breakers
    score += hand_count * 0.05
    score -= enemy_count * 0.5
    score -= energy * 0.1
    # Boss-specific learned residual: V = baseline + Δ_boss. Δ is EXACTLY 0.0
    # unless trained weights exist for the active boss, so this line is a no-op
    # for every non-boss search and for any boss without a residual model —
    # the evaluator stays bit-identical to the hand-tuned baseline by default.
    score += boss_residual(search_state, root_combat)
    return score


def explain_leaf_score(
    search_state: Dict[str, Any],
    root_combat: Optional[Mapping[str, Any]] = None,
    mode: str = 'defense_first',
    coefficients: Optional[Mapping[str, float]] = None,
) -> Dict[str, Any]:
    '''Return an audit decomposition while keeping evaluate_leaf authoritative.'''
    total = float(evaluate_leaf(search_state, root_combat, mode, coefficients))
    if not math.isfinite(total):
        return {'mode': mode, 'total': None, 'terminal': 'invalid',
                'comparable': False, 'error': search_state.get('error'), 'contributions': []}
    terminal_decision = str(search_state.get('terminal_decision') or '')
    if abs(total) >= 200_000.0:
        label = '战斗胜利' if total > 0 else '战斗失败或无效终局'
        return {
            'mode': mode,
            'total': total,
            'terminal': terminal_decision or 'terminal',
            'contributions': [{'key': 'terminal', 'label': label, 'contribution': total}],
        }

    combat = search_state.get('combat') or {}
    player = combat.get('player') or {}
    enemies = combat.get('enemies') or []
    features = extract_leaf_features(search_state, root_combat) if root_combat else {}
    contributions: list[Dict[str, Any]] = []

    def add(key: str, label: str, value: float, weight: float) -> None:
        contribution = float(value) * float(weight)
        if abs(contribution) >= 1e-9:
            contributions.append({
                'key': key, 'label': label, 'value': float(value),
                'weight': float(weight), 'contribution': contribution,
            })

    if mode == 'balanced':
        weights = {
            'enemy_hp_loss': ('敌方生命损失', 3.0),
            'priority_enemy_hp_loss': ('优先目标生命损失', 3.0),
            'weighted_enemy_hp_loss': ('意图加权伤害', 0.45),
            'focused_enemy_hp_loss': ('集中伤害', 0.02),
            'priority_debuff_gain': ('优先目标减益', 2.0),
            'visible_upgrade_bonus': ('可见升级收益', 0.35),
            'player_hp_loss': ('自身生命损失', -4.0),
            'player_block': ('当前格挡', 0.5),
            'unblocked_damage': ('预计未格挡伤害', -3.0),
            'enemy_kills': ('击杀敌人数', 8.0),
        }
        weights['player_hp_loss'] = (weights['player_hp_loss'][0], -5.0)
        weights['player_block'] = (weights['player_block'][0], 0.75)
        weights['unblocked_damage'] = (weights['unblocked_damage'][0], -3.5)
        for key, (label, weight) in weights.items():
            add(key, label, float(features.get(key, 0.0)), weight)
        add('prevented_gold_theft', '避免金币被盗', float(features.get('prevented_gold_theft', 0.0)), 0.25)
        add('prevented_card_theft', '避免卡牌被盗', float(features.get('prevented_card_theft', 0.0)), 18.0)
    for key, feature, label in (
        ('player_hp_loss_delta', 'player_hp_loss', '配置生命损失修正'),
        ('player_block_delta', 'player_block', '配置格挡修正'),
        ('unblocked_damage_delta', 'unblocked_damage', '配置未格挡伤害修正'),
        ('power_stock_delta', 'player_power_stock_gain', '配置能力成长修正'),
        ('strength_future_delta', 'strength_future_value', '配置力量后续收益修正'),
        ('energy_delta', 'energy', '配置能量修正'),
        ('hand_count_delta', 'hand_count', '配置手牌修正'),
    ):
        add(f'profile:{feature}', label, float(features.get(feature, 0.0)),
            float((coefficients or {}).get(key) or 0.0))
    add('hand_count', '剩余手牌', float(len(combat.get('hand') or [])), 0.05)
    add('enemy_count', '存活敌人数', float(len(enemies)), -0.5)
    add('energy', '剩余能量', float(player.get('energy') or 0.0), -0.1)
    remainder = total - sum(float(row['contribution']) for row in contributions)
    if abs(remainder) >= 1e-6:
        contributions.append({'key': 'other', 'label': '其他规则或模型修正', 'contribution': remainder})
    contributions.sort(key=lambda row: abs(float(row['contribution'])), reverse=True)
    return {
        'mode': mode, 'total': total, 'terminal': None,
        'features': features, 'contributions': contributions,
    }
