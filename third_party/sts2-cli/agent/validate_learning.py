#!/usr/bin/env python3
"""Validate learning files: line count + card/enemy name verification.

Usage: python3 agent/validate_learning.py <file>
Exit 0 = pass, Exit 1 = fail (prints errors to stderr)
"""
import json, sys, os, re

MAX_LINES = 100
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Build name database from localization files (lazy loaded)
_names_db = None

def load_names_db():
    global _names_db
    if _names_db is not None:
        return _names_db
    _names_db = {"en": set(), "zh": set()}
    loc_dirs = [
        os.path.join(REPO, "localization_eng"),
        os.path.join(REPO, "localization_zhs"),
    ]
    for loc_dir in loc_dirs:
        if not os.path.isdir(loc_dir):
            continue
        lang = "en" if "eng" in loc_dir else "zh"
        for fname in os.listdir(loc_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(loc_dir, fname)) as f:
                    data = json.load(f)
                for key, val in data.items():
                    # Collect all .name, .title entries as valid game terms
                    if any(key.endswith(s) for s in [".name", ".title", ".title+"]):
                        if isinstance(val, str) and len(val) > 1:
                            _names_db[lang].add(val.strip())
            except Exception:
                pass
    return _names_db


def check_line_count(filepath):
    with open(filepath) as f:
        lines = f.readlines()
    if len(lines) > MAX_LINES:
        return f"FAIL: {os.path.basename(filepath)} has {len(lines)} lines (max {MAX_LINES})"
    return None


def check_card_names(filepath):
    """Check that bold card/enemy names in the file exist in the game database."""
    db = load_names_db()
    lang = "zh" if "_cn.md" in filepath else "en"
    valid_names = db[lang]
    if not valid_names:
        return None  # Can't validate without DB

    with open(filepath) as f:
        content = f.read()

    # Extract bold terms: **Name** or **Name**(
    bold_pattern = re.findall(r'\*\*([^*]+)\*\*', content)

    errors = []
    for term in bold_pattern:
        term = term.strip()
        # Skip non-name patterns (short terms, pure numbers, common words)
        if len(term) < 2 or term.isdigit():
            continue
        # Skip strategy keywords that aren't game names
        skip_terms = {
            # EN common strategy words
            "EXCEPTION", "Multi-hit cards critical", "Slippery", "SKIP",
            "NEVER", "Multi-hit", "HARD LIMIT", "Deck thinning",
            "Exhaust cards", "#1 cause of death", "Osty dies turn 1-2",
            # CN strategy words
            "永远的神", "你铁甲的毕业证", "纯摆设", "纯诈骗", "赌狗专用",
            "删牌比拿牌重要", "消耗牌是隐藏的删牌", "必须R2前杀掉或虚弱药水",
            "23血进场=必死", "252血纯靠打击/出击打不死",
        }
        if term in skip_terms:
            continue
        # Skip terms that contain game mechanics descriptions
        if any(c in term for c in ['>', '<', '=', '+', '→', '/', '×']):
            continue
        if len(term) > 20:  # Long phrases are descriptions not names
            continue

        # Check if it's a valid game name (fuzzy: check if any DB name contains this term)
        found = any(term.lower() in name.lower() or name.lower() in term.lower()
                     for name in valid_names)
        if not found:
            # Could be a valid but unlisted term - only flag if it looks like a card name
            # (Capitalized in EN, or 2-6 chars in CN)
            if lang == "en" and term[0].isupper() and " " not in term:
                errors.append(f"  Unknown EN name: '{term}' — not in game database")
            elif lang == "zh" and 2 <= len(term) <= 6 and not any(c.isascii() for c in term):
                # Skip common CN commentary/slang that isn't a game name
                cn_skip = {"纯纯陷阱", "能力牌跟上", "例外：快死了", "你要死了", "全部干了",
                           "必拿", "通用好牌", "致命模式", "正常优先级", "关键", "顶级",
                           "核心能力牌", "绝对不进", "临时的", "快死了", "怕什么", "无脑",
                           "大爹", "白嫖", "陷阱", "纯坑", "必死", "速杀", "离谱"}
                if term not in cn_skip:
                    errors.append(f"  Unknown ZH name: '{term}' — not in game database")

    if errors:
        return f"WARNING: Possible hallucinated names in {os.path.basename(filepath)}:\n" + "\n".join(errors[:5])
    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: validate_learning.py <file>", file=sys.stderr)
        sys.exit(1)

    filepath = sys.argv[1]
    if not filepath.endswith(".md") or "learning_" not in filepath:
        sys.exit(0)  # Not a learning file, skip

    errors = []

    # Check 1: Line count
    err = check_line_count(filepath)
    if err:
        errors.append(err)

    # Check 2: Card names (EN only — CN bold words are too noisy with 锐评 style)
    if "_en.md" in filepath:
        warn = check_card_names(filepath)
        if warn:
            print(warn, file=sys.stderr)

    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
