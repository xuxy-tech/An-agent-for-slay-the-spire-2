#!/usr/bin/env python3
"""Auto-play a full STS2 Necrobinder run via the bridge."""
import json, sys, urllib.request, time, uuid

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 19234
URL = f"http://localhost:{PORT}"

def cmd(data):
    """Send command to bridge, return parsed JSON."""
    try:
        req = urllib.request.Request(URL, json.dumps(data).encode(), {'Content-Type': 'application/json'})
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except Exception as e:
        print(f"  ERR: {e}")
        return {"type": "error", "message": str(e)}

def action(act, **args):
    return cmd({"cmd": "action", "action": act, "args": args})

def play_card(ci, ti=None):
    args = {"card_index": ci}
    if ti is not None:
        args["target_index"] = ti
    return action("play_card", **args)

def pick_best_card(hand, enemies, osty, energy, rnd, inc, player_block=0, player_hp=99):
    """Pick the single best card to play right now. Returns (hand_index, target_index_or_None) or None."""
    best = None
    best_p = 999

    # Calculate block gap and survivability
    block_gap = inc - player_block
    lethal = (block_gap >= player_hp)  # Will die if don't block
    can_tank = (player_hp > inc * 2)  # Can afford to take a full hit

    for i, c in enumerate(hand):
        if not c.get("can_play"):
            continue
        name = c["name"]
        cost = c["cost"]
        ctype = c["type"]
        tt = c.get("target_type", "")

        if cost > energy:
            continue

        # Skip status/curse
        if ctype == "Status" or name in ["Slimed", "Burn", "Wound", "Dazed", "Infection"]:
            continue

        # Priority scoring (lower = play first)
        is_block_card = ctype == "Skill" and tt != "AnyEnemy" and name not in ["Bodyguard", "Wisp", "Borrowed Time"]
        block_val = (c.get("stats") or {}).get("block", 0)

        if cost == 0:
            p = 1  # 0-cost ALWAYS first (Wisp, Borrowed Time, etc)
        elif ctype == "Power":
            p = 5 + cost if inc < 25 else 30
        elif name == "Bodyguard" and (rnd <= 2 or (osty.get("hp", 0) <= 1 and inc == 0)):
            p = 6  # Bodyguard R1-R2 or when safe and Osty needs growth
        elif name == "Enfeebling Touch" and inc > 10:
            p = 9
        elif name == "Defy" and block_gap > 0:
            p = 10
        elif name == "Bodyguard" and inc == 0:
            p = 11  # Bodyguard when safe
        elif name == "Flatten" and osty.get("alive"):
            p = 12  # Flatten is top damage card
        elif name == "Bodyguard" and block_gap <= 0:
            p = 14  # Bodyguard when block is enough
        elif is_block_card and inc == 0:
            p = 50  # NEVER block when no incoming damage
        elif is_block_card and block_gap <= 0:
            p = 45  # Don't over-block
        elif is_block_card and lethal:
            p = 7  # LETHAL: block is everything
        elif is_block_card and block_gap > 0 and not can_tank:
            p = 16  # Block when needed AND can't tank
        elif is_block_card and block_gap > 0 and can_tank:
            p = 30  # Can tank - STRONGLY prefer attacks over blocks
        elif tt == "AnyEnemy":
            p = 13  # Attack cards - always higher priority than blocks when tankable
        elif name == "Bodyguard":
            p = 25
        else:
            p = 20

        # Target
        ti = None
        if tt == "AnyEnemy" and enemies:
            ti = 0
            if len(enemies) > 1:
                # Target lowest HP enemy that's a threat
                for j, en in enumerate(enemies):
                    if en["hp"] < enemies[ti]["hp"]:
                        ti = j

        if p < best_p:
            best_p = p
            best = (i, ti)

    return best

def combat_turn(d):
    """Play one combat turn: pick and play cards ONE AT A TIME with fresh state. Returns state after end_turn."""
    rnd = d.get("round", 1)
    enemies_str = " + ".join(f"{en['name']['zh']}{en['hp']}hp" for en in d.get("enemies", []))
    max_plays = 8
    cards_played = []
    for _ in range(max_plays):
        hand = d.get("hand", [])
        enemies = d.get("enemies", [])
        osty = d.get("osty", {})
        energy = d.get("energy", 0)
        rnd = d.get("round", 1)

        # Calculate incoming
        inc = 0
        for en in enemies:
            for intent in (en.get("intents") or []):
                if intent.get("type") == "Attack":
                    inc += min(intent.get("damage", 0), 60)

        player_block = d.get("player", {}).get("block", 0) if "player" in d else 0
        player_hp = d.get("player", {}).get("hp", 99) if "player" in d else 99
        choice = pick_best_card(hand, enemies, osty, energy, rnd, inc, player_block, player_hp)
        if choice is None:
            break

        ci, ti = choice
        card_name = hand[ci]["name"] if ci < len(hand) else "?"
        if ti is not None:
            result = play_card(ci, ti)
        else:
            result = play_card(ci)

        rdec = result.get("decision", result.get("type", ""))
        if rdec in ("card_reward", "game_over"):
            return result
        if result.get("type") == "error":
            if d.get("hand") and ci < len(d["hand"]):
                d["hand"][ci]["can_play"] = False
            continue

        cards_played.append(card_name)
        d = result

    if cards_played:
        print(f"    R{rnd}: {' → '.join(cards_played)} | {enemies_str}")
    result = action("end_turn")
    return result

def handle_card_reward(d):
    """Pick best card from reward, or skip."""
    cards = d.get("cards", [])
    player = d.get("player", {})
    deck_size = player.get("deck_size", 10)

    # Priority cards for Necrobinder
    priority = {
        "Calcify": 100, "Flatten": 90, "Sic 'Em": 85, "Fetch": 80,
        "Bodyguard": 75, "Unleash": 70,
        "Reave": 65, "Grave Warden": 60, "Wisp": 55, "Borrowed Time": 50,
        "Enfeebling Touch": 45, "Drain Power": 40, "Defy": 35, "Haunt": 30,
        "Capture Spirit": 28, "Melancholy": 25, "Deathbringer": 20
    }

    # Track existing cards in deck to avoid duplicates of non-core cards
    existing = {}
    for dc in player.get("deck", []):
        dn = dc["name"]
        existing[dn] = existing.get(dn, 0) + 1

    core_cards = {"Calcify", "Flatten", "Bodyguard", "Unleash", "Sic 'Em", "Fetch"}

    best_idx = -1
    best_score = 0
    for c in cards:
        name = c["name"]
        score = priority.get(name, 0)
        ctype = c.get("type", "")
        stats = c.get("stats") or {}
        # Early game (deck < 12): take any decent card
        if deck_size < 12:
            if ctype == "Attack" and stats.get("damage", 0) >= 8:
                score = max(score, 15)
            if ctype == "Power":
                score = max(score, 18)
        # Don't take duplicates of non-core cards
        if name not in core_cards and existing.get(name, 0) >= 1:
            score = min(score, 5)  # Greatly reduce score for duplicates
        # Reduce score if deck is getting big
        if deck_size > 15 and score < 50:
            score = 0
        if score > best_score:
            best_score = score
            best_idx = c["index"]

    if best_score >= 15 and deck_size < 18:
        print(f"  PICK: {cards[best_idx]['name']['zh']} (score={best_score})")
        return action("select_card_reward", card_index=best_idx)
    else:
        print(f"  SKIP card reward (deck={deck_size})")
        return action("skip_card_reward")

def handle_shop(d):
    """Buy high-priority cards, remove strikes."""
    player = d["player"]
    gold = player["gold"]
    deck_size = player["deck_size"]
    removal_cost = d.get("card_removal_cost", 75)

    # Buy priority cards first
    priority_names = {"Calcify", "Flatten", "Sic 'Em", "Fetch", "Wisp", "Borrowed Time",
                      "Drain Power", "Enfeebling Touch", "Bodyguard", "Unleash", "Reave"}

    for c in d.get("cards", []):
        if not c["is_stocked"]:
            continue
        name = c["name"]
        cost = c["cost"]
        if name in priority_names and cost <= gold:
            print(f"  BUY: {c['name']['zh']} ({cost}g)")
            result = action("buy_card", card_index=c["index"])
            gold -= cost
            deck_size += 1

    # Remove a Strike if affordable
    if gold >= removal_cost and deck_size > 8:
        print(f"  REMOVE: Strike ({removal_cost}g)")
        action("remove_card")
        # Select first Strike
        result = action("select_cards", indices="0")
        gold -= removal_cost

    return action("leave_room")

def handle_rest(d):
    """Heal if HP < 75%, else smith."""
    player = d["player"]
    hp_pct = player["hp"] / player["max_hp"]
    floor = d["context"]["floor"]

    if hp_pct < 0.75 or floor >= 15:
        print(f"  HEAL (HP={player['hp']}/{player['max_hp']})")
        return action("choose_option", option_index=0)
    else:
        print(f"  SMITH (HP={player['hp']}/{player['max_hp']})")
        result = action("choose_option", option_index=1)
        # Smith: pick best card to upgrade
        if result.get("decision") == "card_select":
            # Upgrade priority: Calcify > Bodyguard > Unleash > Flatten > Drain Power
            cards = result.get("cards", [])
            upgrade_priority = {"Calcify": 10, "Bodyguard": 9, "Unleash": 8, "Flatten": 7,
                               "Drain Power": 6, "Enfeebling Touch": 5}
            best_idx = 0
            best_score = 0
            for c in cards:
                score = upgrade_priority.get(c["name"], 0)
                if score > best_score:
                    best_score = score
                    best_idx = c["index"]
            print(f"    Upgrade: {cards[best_idx]['name']['zh']}")
            return action("select_cards", indices=str(best_idx))
        return result

def handle_map(d):
    """Choose map node based on HP and available choices."""
    player = d["player"]
    hp_pct = player["hp"] / player["max_hp"]
    choices = d.get("choices", [])
    floor = d.get("act", 1) * 17 + d.get("floor", 0)  # rough
    floor = d["context"]["floor"]

    deck = player.get("deck", [])
    deck_names = {c["name"] for c in deck}
    has_scaling = bool(deck_names & {"Calcify", "Flatten", "Sic 'Em", "Drain Power"})

    # Priority: Treasure > RestSite > Shop > Monster > Unknown > Elite
    type_priority = {
        "Treasure": 100,
        "RestSite": 80 if hp_pct < 0.7 else 30,
        "Shop": 70,
        "Unknown": 40,
        "Monster": 35 if hp_pct > 0.4 else 10,
        "Elite": 50 if (hp_pct > 0.85 and "Calcify" in deck_names and len(deck) >= 14 and floor >= 12) else 1,
        "Boss": 90,
    }

    # Before boss, prefer rest
    if floor >= 15:
        type_priority["RestSite"] = 95
    # Low HP: avoid monsters too
    if hp_pct < 0.3:
        type_priority["Monster"] = 3

    best = max(choices, key=lambda c: type_priority.get(c["type"], 0))
    print(f"  MAP: ({best['col']},{best['row']}) {best['type']} (HP={player['hp']}/{player['max_hp']})")
    return action("select_map_node", col=best["col"], row=best["row"])

def handle_event(d):
    """Choose best event option."""
    options = d.get("options", [])
    player = d["player"]

    # Pick first non-locked option, prefer ones without HP loss at low HP
    for opt in options:
        if opt.get("is_locked"):
            continue
        vars_ = opt.get("vars") or {}
        hp_loss = vars_.get("HpLoss", 0)
        if hp_loss > 0 and player["hp"] < player["max_hp"] * 0.5:
            continue
        print(f"  EVENT: {opt['title']['zh']} {vars_}")
        return action("choose_option", option_index=opt["index"])

    # Fallback: first option
    print(f"  EVENT: {options[0]['title']['zh']} (fallback)")
    return action("choose_option", option_index=0)

def use_potions_at_boss(d):
    """Use all potions at boss/elite fights."""
    potions = d.get("potions", [])
    enemies = d.get("enemies", [])
    for pot in potions:
        tt = pot.get("target_type", "")
        pi = pot["index"]
        if tt == "AnyEnemy":
            action("use_potion", potion_index=pi, target_index=0)
        elif tt in ("Self", "AnyPlayer"):
            action("use_potion", potion_index=pi)

# Main game loop
def play_game():
    seed = uuid.uuid4().hex[:12]
    print(f"Starting Necrobinder run (seed={seed})...")
    d = cmd({"cmd": "start_run", "character": "Necrobinder", "seed": seed})

    boss_name = d.get("context", {}).get("boss", {}).get("name", {}).get("zh", "?")
    print(f"Boss: {boss_name}")

    max_steps = 2000
    for step in range(max_steps):
        dec = d.get("decision", "")

        if dec == "game_over":
            victory = d.get("victory", False)
            hp = d["player"]["hp"]
            act = d["context"]["act"]
            floor = d["context"]["floor"]
            print(f"\n{'VICTORY!' if victory else 'DEFEAT'} Act{act} F{floor} HP={hp}/{d['player']['max_hp']}")
            if victory:
                return True
            return False

        elif dec == "event_choice":
            d = handle_event(d)

        elif dec == "map_select":
            d = handle_map(d)

        elif dec == "combat_play":
            # Check if boss/elite for potion use
            room = d.get("context", {}).get("room_type", "")
            if room in ("Boss", "Elite") and d.get("round", 1) == 1:
                use_potions_at_boss(d)
            d = combat_turn(d)

        elif dec == "card_reward":
            d = handle_card_reward(d)

        elif dec == "rest_site":
            d = handle_rest(d)

        elif dec == "shop":
            d = handle_shop(d)

        elif dec == "card_select":
            # Generic card select - pick index 0
            cards = d.get("cards", [])
            n = d.get("min_select", 1)
            indices = ",".join(str(i) for i in range(min(n, len(cards))))
            print(f"  CARD_SELECT: indices={indices}")
            d = action("select_cards", indices=indices)

        elif dec == "bundle_select":
            d = action("select_bundle", bundle_index=0)

        elif d.get("type") == "error":
            print(f"  ERROR: {d.get('message', '')[:80]}")
            d = action("proceed")
            if d.get("type") == "error":
                d = action("leave_room")
                if d.get("type") == "error":
                    d = action("end_turn")

        else:
            print(f"  UNKNOWN: {dec} - trying proceed")
            d = action("proceed")

    print("Max steps reached!")
    return False

def restart_bridge():
    """Kill old bridge and start fresh one."""
    import subprocess, signal, os
    # Kill processes on PORT
    try:
        result = subprocess.run(["lsof", "-ti", f":{PORT}"], capture_output=True, text=True)
        for pid in result.stdout.strip().split("\n"):
            if pid:
                os.kill(int(pid), signal.SIGKILL)
    except: pass
    time.sleep(2)
    # Start new bridge
    log = f"/tmp/sts2_game_{PORT}.jsonl"
    proc = subprocess.Popen(
        ["python3", "agent/sts2_bridge.py", str(PORT), "--compact", "--log", log],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(6)
    return proc

if __name__ == "__main__":
    max_attempts = 10
    for attempt in range(1, max_attempts + 1):
        print(f"\n{'='*40}")
        print(f"ATTEMPT {attempt}/{max_attempts}")
        print(f"{'='*40}")
        if attempt > 1:
            restart_bridge()
        if play_game():
            print(f"\nWON on attempt {attempt}!")
            break
    else:
        print(f"\nFailed all {max_attempts} attempts.")
