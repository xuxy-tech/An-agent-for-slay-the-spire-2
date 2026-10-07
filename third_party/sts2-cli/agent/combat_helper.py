#!/usr/bin/env python3
"""Combat helper: auto-plays one combat encounter."""
import json, sys, urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 19044
URL = f"http://localhost:{PORT}"

def cmd(data):
    try:
        req = urllib.request.Request(URL, json.dumps(data).encode(), {'Content-Type': 'application/json'})
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as e:
        return {"type": "error", "message": str(e)}

def action(act, **args):
    return cmd({"cmd": "action", "action": act, "args": args})

def play_card(ci, ti=None):
    args = {"card_index": ci}
    if ti is not None: args["target_index"] = ti
    return action("play_card", **args)

def calc_incoming(enemies):
    """Calculate total incoming damage from all enemies, accounting for multi-hit."""
    total = 0
    for en in enemies:
        for it in (en.get("intents") or []):
            if it.get("type") in ("Attack", "DeathBlow"):
                dmg = min(it.get("damage", 0), 60)
                hits = it.get("hits", 1)
                total += dmg * hits if hits > 1 else dmg
    return total

def enemy_threat(en):
    """Score enemy threat: higher = more dangerous."""
    atk = 0
    for it in (en.get("intents") or []):
        if it.get("type") in ("Attack", "DeathBlow"):
            hits = it.get("hits", 1)
            atk += it.get("damage", 0) * (hits if hits > 1 else 1)
    return atk * 100 + (200 - en["hp"])  # Prioritize high-damage, then low HP

def pick_target(enemies, card_dmg=6):
    """Pick best target: one-shottable high-threat > highest threat."""
    if not enemies: return 0
    # Can we one-shot anyone who's attacking?
    best_oneshot = None
    for j, en in enumerate(enemies):
        if en["hp"] <= card_dmg and en.get("intends_attack"):
            if best_oneshot is None or enemy_threat(en) > enemy_threat(enemies[best_oneshot]):
                best_oneshot = j
    if best_oneshot is not None: return best_oneshot
    # Otherwise target highest threat
    return max(range(len(enemies)), key=lambda j: enemy_threat(enemies[j]))

def best_card(hand, enemies, osty, energy, rnd, inc, blk, hp):
    block_gap = inc - blk
    lethal = block_gap >= hp
    can_tank = hp > inc * 2
    need_block = block_gap > 0 and (lethal or not can_tank or block_gap > 15)
    best, best_p = None, 999
    for i, c in enumerate(hand):
        if not c.get("can_play") or c["cost"] > energy: continue
        name, cost, ctype, tt = c["name"], c["cost"], c["type"], c.get("target_type", "")
        kw = c.get("keywords") or []
        if ctype in ("Status", "Curse") or "Unplayable" in kw: continue
        if name in ["Slimed","Burn","Wound","Dazed","Infection"]: continue
        is_block = ctype == "Skill" and tt != "AnyEnemy"
        is_attack = tt == "AnyEnemy"
        # Priority scoring (lower = play first)
        if cost == 0 and is_block and need_block: p = 1  # Free block when needed
        elif cost == 0 and ctype == "Power": p = 2  # Free powers always good
        elif cost == 0 and is_attack: p = 3  # Free attacks
        elif cost == 0: p = 4  # Other free cards
        elif ctype == "Power" and not lethal and inc <= 15: p = 8 + cost  # Powers when safe
        elif ctype == "Power" and not lethal: p = 15 + cost  # Powers when moderate danger
        elif ctype == "Power" and lethal: p = 50  # Don't play powers when dying
        elif is_block and lethal: p = 2 + cost  # Block everything when dying
        elif is_block and need_block: p = 10 + cost  # Block when needed
        elif is_block and inc == 0: p = 45  # Don't block when no damage incoming
        elif is_block and block_gap <= 0: p = 42  # Already blocked enough
        elif is_block: p = 25 + cost  # Normal block
        elif is_attack and not lethal: p = 12 + cost  # Attacks when not dying
        elif is_attack and lethal: p = 40  # Don't attack when dying (block instead)
        else: p = 20 + cost
        ti = None
        if tt == "AnyEnemy" and enemies:
            card_dmg = (c.get("stats") or {}).get("damage", 6)
            ti = pick_target(enemies, card_dmg)
        if p < best_p: best_p, best = p, (i, ti)
    return best

def fight(d=None):
    if d is None: d = action("end_turn")
    for _ in range(200):
        dec = d.get("decision", d.get("type", ""))
        if dec in ("card_reward", "game_over", "map_select", "rest_site", "shop", "event_choice", "bundle_select"):
            return d
        if dec == "card_select":
            n = d.get("min_select", 1)
            d = action("select_cards", indices=",".join(str(i) for i in range(min(n, len(d.get("cards",[]))))))
            continue
        if dec != "combat_play":
            d = action("proceed")
            continue
        hand, enemies, osty = d.get("hand",[]), d.get("enemies",[]), d.get("osty",{})
        energy, rnd = d.get("energy",0), d.get("round",1)
        hp = d.get("player",{}).get("hp",99) if "player" in d else 99
        blk = d.get("player",{}).get("block",0) if "player" in d else 0
        inc = calc_incoming(enemies)
        # Use potions when critical
        potions = d.get("potions", [])
        if potions and (hp < 20 or (inc > hp and blk == 0)):
            for pi, pot in enumerate(potions):
                pname = pot.get("name", {}).get("en", "")
                ptt = pot.get("target_type", "")
                if "Heal" in pname or "Block" in pname or "Regen" in pname or "Fairy" in pname:
                    r = action("use_potion", potion_index=pi)
                    if r.get("type") != "error": d = r; break
                elif ptt == "AnyEnemy" and enemies:
                    ti = pick_target(enemies)
                    r = action("use_potion", potion_index=pi, target_index=ti)
                    if r.get("type") != "error": d = r; break
                elif ptt in ("Self", "AnyPlayer", ""):
                    r = action("use_potion", potion_index=pi)
                    if r.get("type") != "error": d = r; break
            hand, enemies = d.get("hand",[]), d.get("enemies",[])
            energy = d.get("energy", energy)
            inc = calc_incoming(enemies)
        choice = best_card(hand, enemies, osty, energy, rnd, inc, blk, hp)
        if choice is None:
            d = action("end_turn"); continue
        ci, ti = choice
        r = play_card(ci, ti) if ti is not None else play_card(ci)
        if r.get("type") == "error":
            if d.get("hand") and ci < len(d["hand"]): d["hand"][ci]["can_play"] = False
            continue
        d = r
    return action("end_turn")

if __name__ == "__main__":
    result = fight()
    dec = result.get("decision", "")
    p = result.get("player", {})
    if dec == "card_reward":
        print(f"WIN HP={p.get('hp')}/{p.get('max_hp')} g={p.get('gold')}")
        for c in result.get("cards", []):
            print(f"  C{c['index']}: {c['name']['zh']}({c.get('cost','?')}) {c.get('rarity','')} {c['type']} {c.get('stats',{})}")
    elif dec == "game_over":
        print(f"DEAD HP={p.get('hp')}")
    else:
        print(f"STATE: {dec} HP={p.get('hp','?')}/{p.get('max_hp','?')}")
