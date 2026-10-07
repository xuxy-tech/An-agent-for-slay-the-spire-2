#!/usr/bin/env python3
"""
sts2-cli 启动器：选择新游戏（角色、进阶）或读取存档，再进入 python/play.py。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
PLAY_PY = os.path.join(ROOT, "python", "play.py")
SAVE_DIR = os.path.join(ROOT, "saves")
LOC_CHARS = os.path.join(ROOT, "localization_zhs", "characters.json")

# play.py --character 使用的英文名，顺序与官方角色选择一致
CLI_CHARACTERS = ["Ironclad", "Silent", "Defect", "Regent", "Necrobinder"]


def _load_char_titles() -> dict[str, str]:
    """官方中文角色名（localization_zhs/characters.json）。"""
    titles: dict[str, str] = {}
    if not os.path.isfile(LOC_CHARS):
        return titles
    with open(LOC_CHARS, encoding="utf-8") as f:
        z = json.load(f)
    for key in ("IRONCLAD", "SILENT", "DEFECT", "REGENT", "NECROBINDER"):
        titles[key] = z.get(f"{key}.title", key)
    return titles


def _char_zh(titles: dict[str, str], cli_name: str) -> str:
    return titles.get(cli_name.upper(), cli_name)


def _prompt_line(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0) from None


def _pick_int(prompt: str, lo: int, hi: int, default: int | None = None) -> int:
    while True:
        raw = _prompt_line(prompt)
        if not raw and default is not None:
            return default
        try:
            v = int(raw)
        except ValueError:
            print(f"  请输入 {lo}–{hi} 之间的整数。")
            continue
        if lo <= v <= hi:
            return v
        print(f"  请输入 {lo}–{hi} 之间的整数。")


def _collect_save_entries() -> list[dict]:
    if not os.path.isdir(SAVE_DIR):
        return []
    out: list[dict] = []
    for name in os.listdir(SAVE_DIR):
        path = os.path.join(SAVE_DIR, name)
        if not os.path.isfile(path):
            continue
        st = os.stat(path)
        if name.endswith(".json"):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                out.append({
                    "kind": "replay",
                    "path": path,
                    "name": name,
                    "mtime": st.st_mtime,
                    "character": d.get("character", "?"),
                    "seed": d.get("seed", "?"),
                    "actions": len(d.get("actions", [])),
                })
            except (json.JSONDecodeError, OSError):
                pass
        elif name.endswith(".save"):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                seed = data.get("rng", {}).get("seed", "?")
                asc = data.get("ascension", 0)
                char_id = "?"
                pl = data.get("players", [])
                if pl:
                    char_id = pl[0].get("character_id", "?")
                out.append({
                    "kind": "native",
                    "path": path,
                    "name": name,
                    "mtime": st.st_mtime,
                    "seed": seed,
                    "ascension": asc,
                    "character_id": char_id,
                })
            except (json.JSONDecodeError, OSError):
                out.append({
                    "kind": "native",
                    "path": path,
                    "name": name,
                    "mtime": st.st_mtime,
                    "broken": True,
                })
    out.sort(key=lambda x: -x["mtime"])
    return out


def _format_entry(titles: dict[str, str], e: dict) -> str:
    ts = datetime.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M")
    if e["kind"] == "replay":
        ch = str(e.get("character", "?"))
        zh = _char_zh(titles, ch)
        return f"{e['name']}  |  {zh}  |  种子 {e['seed']}  |  {e['actions']} 步  |  {ts}"
    if e.get("broken"):
        return f"{e['name']}  |  （文件损坏或无法解析）  |  {ts}"
    cid = str(e.get("character_id", "?"))
    zh = titles.get(cid.upper(), cid) if cid != "?" else "?"
    return (
        f"{e['name']}  |  {zh}  |  进阶 {e['ascension']}  |  种子 {e['seed']}  |  {ts}"
    )


def _run_play(args: list[str], lang: str) -> int:
    cmd = [sys.executable, PLAY_PY, "--lang", lang, *args]
    # play.py 内 ROOT 由文件路径解析，不依赖 cwd；统一设为仓库根目录便于相对路径。
    r = subprocess.run(cmd, cwd=ROOT)
    return r.returncode


def _menu_new_game(titles: dict[str, str], lang: str) -> None:
    print("\n── 选择角色 ──")
    for i, cli in enumerate(CLI_CHARACTERS, 0):
        zh = _char_zh(titles, cli)
        print(f"  {i}  {zh}  ({cli})")
    idx = _pick_int("\n输入编号 (0–4): ", 0, 4)
    character = CLI_CHARACTERS[idx]
    asc = _pick_int(
        "\n进阶等级 0–10（0 为标准模式，直接回车默认为 0）: ",
        0,
        10,
        default=0,
    )
    print(f"\n启动：{ _char_zh(titles, character) }  |  进阶 {asc}\n")
    _run_play(["--character", character, "--ascension", str(asc)], lang)


def _menu_load_save(titles: dict[str, str], lang: str) -> None:
    entries = _collect_save_entries()
    if not entries:
        print("\n  saves/ 下没有 .save 或 .json 存档。请先在对局中存档或退出时选择保存。\n")
        return

    print("\n── 读取存档（按修改时间从新到旧）──")
    print("  [继续游戏] = 游戏原生 .save")
    print("  [操作回放] = 对局内 save 命令生成的 .json\n")
    for i, e in enumerate(entries, 1):
        tag = "继续游戏" if e["kind"] == "native" else "操作回放"
        print(f"  {i:2}  [{tag}]  {_format_entry(titles, e)}")
    print(f"\n  0  返回上一级")
    choice = _pick_int("\n输入编号: ", 0, len(entries))
    if choice == 0:
        return
    sel = entries[choice - 1]
    rel = os.path.relpath(sel["path"], ROOT)
    if sel["kind"] == "native":
        print(f"\n以继续游戏方式加载：{rel}\n")
        _run_play(["--continue", rel], lang)
    else:
        print(f"\n以操作回放方式加载：{rel}\n")
        _run_play(["--load", rel], lang)


def _main_interactive(lang: str) -> None:
    sys.path.insert(0, os.path.join(ROOT, "python"))
    import play as play_mod  # noqa: PLC0415

    play_mod.ensure_setup()
    titles = _load_char_titles()

    while True:
        print(
            f"""
╔══════════════════════════════════════╗
║       Slay the Spire 2  CLI          ║
╚══════════════════════════════════════╝

  1  新游戏
  2  读取存档
  0  退出
"""
        )
        c = _prompt_line("请选择 (0–2): ").lower()
        if c in ("0", "q", "quit", "exit", ""):
            print("再见。")
            break
        if c == "1":
            _menu_new_game(titles, lang)
        elif c == "2":
            _menu_load_save(titles, lang)
        else:
            print("  无效输入，请输入 0、1 或 2。")


def main() -> None:
    parser = argparse.ArgumentParser(description="sts2-cli 交互式启动器")
    parser.add_argument(
        "--lang",
        choices=["zh", "en", "both"],
        default="zh",
        help="交给 play.py 的显示语言",
    )
    args = parser.parse_args()
    _main_interactive(args.lang)


if __name__ == "__main__":
    main()
