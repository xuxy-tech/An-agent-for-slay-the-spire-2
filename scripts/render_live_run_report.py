from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a compact HTML report from live-run JSON logs")
    parser.add_argument("--run-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-combat-report", type=Path)
    parser.add_argument("--bootstrap-session-log", type=Path)
    args = parser.parse_args()

    run_report = json.loads(args.run_report.read_text(encoding="utf-8"))
    combats: List[Dict[str, Any]] = []
    if args.bootstrap_combat_report:
        bootstrap = json.loads(args.bootstrap_combat_report.read_text(encoding="utf-8"))
        actions = list(bootstrap.get("actions") or [])
        telemetry = list(_combat_live_actions(args.bootstrap_session_log))
        searches = [row for row in telemetry if not row.get("reused_plan")]
        combats.append(_combat_summary(1, actions, searches, telemetry))
    for combat in run_report.get("combats") or []:
        actions = list(combat.get("actions") or [])
        searches = [row for row in actions if not row.get("reused_plan")]
        combats.append(_combat_summary(int(combat["combat_number"]), actions, searches, actions))
        combats[-1]['status'] = combat.get('status', 'UNKNOWN')

    checks = list(run_report.get("parity_checkpoints") or [])
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render(run_report, combats, checks), encoding="utf-8")
    print(output)


def _combat_live_actions(path: Path | None) -> Iterable[Dict[str, Any]]:
    if path is None:
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") != "live_action" or event.get("decision") != "combat_play":
            continue
        telemetry = dict(event.get("decision_telemetry") or {})
        telemetry["client_action_ms"] = event.get("action_wall_ms")
        rows.append(telemetry)
    return rows


def _combat_summary(
    number: int,
    actions: List[Dict[str, Any]],
    searches: List[Dict[str, Any]],
    telemetry: List[Dict[str, Any]],
) -> Dict[str, Any]:
    search_ms = [float(row["search_ms"]) for row in searches if row.get("search_ms") is not None]
    return {
        "number": number,
        "actions": len(actions),
        "searches": len(search_ms),
        "reused": sum(1 for row in telemetry if row.get("reused_plan")),
        "avg_search_ms": round(sum(search_ms) / len(search_ms), 1) if search_ms else None,
        "nodes": int(sum(float(row.get("nodes") or 0.0) for row in searches)),
    }


def _render(
    run_report: Dict[str, Any],
    combats: List[Dict[str, Any]],
    checks: List[Dict[str, Any]],
) -> str:
    completed = int(run_report.get("completed_combat_count", 0))
    total_actions = sum(int(row["actions"]) for row in combats)
    passes = sum(1 for row in checks if row.get("status") == "PASS")
    reanchors = sum(1 for row in checks if row.get("status") == "REANCHORED_PASS")
    failures = sum(1 for row in checks if row.get("status") in {'FAIL', 'INCOMPLETE'})
    check_text = f'{passes} 一致 / {failures} 异常 / {reanchors} 重新对齐' if checks else '未验证'
    identity = run_report.get('identity') or {}
    identity_text = html.escape(' · '.join(str(identity.get(key) or '未记录') for key in
                                        ('game_version', 'run_id', 'character', 'ascension')))
    outcome = html.escape(str(run_report.get('status') or 'UNKNOWN'))
    error = html.escape(str(run_report.get('error') or run_report.get('reason') or ''))
    configuration = html.escape(json.dumps(run_report.get('config') or {}, ensure_ascii=False))
    max_actions = max(1, max((int(row["actions"]) for row in combats), default=1))
    bars = []
    table_rows = []
    for row in combats:
        average_label = f'{row["avg_search_ms"]:.1f} ms' if row.get('avg_search_ms') is not None else '未记录'
        searched_pct = 100.0 * int(row["searches"]) / max_actions
        reused_pct = 100.0 * int(row["reused"]) / max_actions
        bars.append(
            f'<div class="bar-label">第 {row["number"]} 战</div>'
            f'<div class="bar" aria-label="第 {row["number"]} 战，'
            f'{row["searches"]} 次搜索，{row["reused"]} 次计划复用">'
            f'<span class="searched" style="width:{searched_pct:.2f}%"></span>'
            f'<span class="reused" style="width:{reused_pct:.2f}%"></span></div>'
            f'<div class="bar-value">{row["actions"]} 动作</div>'
        )
        table_rows.append(
            "<tr>"
            f'<td>第 {row["number"]} 战</td>'
            f'<td>{row["actions"]}</td><td>{row["searches"]}</td><td>{row["reused"]}</td>'
            f'<td>{average_label}</td><td>{row["nodes"]}</td><td>{html.escape(str(row.get("status", "UNKNOWN")))}</td>'
            "</tr>"
        )
    rewards = []
    for reward in run_report.get("rewards") or []:
        choice = (reward.get("payload") or {}).get("card_index")
        offered = reward.get("offered") or []
        chosen = next((str(card.get("id") or "").split(".")[-1] for card in offered if card.get("index") == choice), "SKIP")
        rewards.append(f'第 {reward.get("combat_number")} 战奖励：<strong>{html.escape(chosen)}</strong>')
    reward_text = " · ".join(rewards) or "未记录"
    detail = html.escape(json.dumps({'actions': run_report.get('actions', []),
                                    'checkpoints': checks}, ensure_ascii=False, indent=2))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>STS2 Agent 真实客户端验证</title>
<style>
:root{{--bg:#f7f7f4;--panel:#fff;--text:#202124;--muted:#687076;--border:#d9dddf;--a:#356ae6;--b:#38a169;--ok:#16803c}}
@media(prefers-color-scheme:dark){{:root{{--bg:#171819;--panel:#222426;--text:#f2f3f3;--muted:#a7adb1;--border:#3b3f42;--a:#7aa2ff;--b:#54c780;--ok:#6bdc8e}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:980px;margin:auto;padding:32px 20px}} h1,h2{{font-weight:600}} h1{{margin:0}} .sub{{color:var(--muted);margin-top:4px}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:24px 0}} .stat{{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px}}
.stat b{{display:block;font-size:24px}} .stat span{{color:var(--muted)}} section{{margin-top:26px}} .chart{{display:grid;grid-template-columns:72px 1fr 72px;gap:10px;align-items:center}}
.bar{{height:26px;background:var(--panel);display:flex;overflow:hidden;border-radius:4px}} .searched{{background:var(--a)}} .reused{{background:var(--b)}} .bar-value{{text-align:right}}
.legend{{display:flex;gap:18px;color:var(--muted);margin-bottom:12px}} .dot{{display:inline-block;width:10px;height:10px;margin-right:6px}} .dot.a{{background:var(--a)}} .dot.b{{background:var(--b)}}
table{{width:100%;border-collapse:collapse;background:var(--panel)}} th,td{{padding:10px 12px;border-bottom:1px solid var(--border);text-align:right}} th:first-child,td:first-child{{text-align:left}} .pass{{color:var(--ok);font-weight:600}}
.note{{padding:14px 16px;background:var(--panel);border-left:4px solid var(--a)}} code{{font-family:ui-monospace,monospace}} @media(max-width:620px){{.stats{{grid-template-columns:1fr}}main{{padding:20px 12px}}}}
</style></head><body><main>
<h1>STS2 Agent 运行记录</h1><p class="sub">{identity_text}</p>
<p><strong>{outcome}</strong> {error}</p><p style="overflow-wrap:anywhere">{configuration}</p>
<div class="stats"><div class="stat"><span>已完成战斗</span><b>{completed} 场</b></div><div class="stat"><span>已记录战斗动作</span><b>{total_actions} 次</b></div><div class="stat"><span>覆盖字段检查</span><b>{check_text}</b></div></div>
<section><h2>搜索与计划复用</h2><div class="legend"><span><i class="dot a"></i>重新搜索</span><span><i class="dot b"></i>复用既有计划</span></div><div class="chart">{''.join(bars)}</div></section>
<section><h2>逐场数据</h2><table><thead><tr><th>阶段</th><th>动作</th><th>搜索</th><th>复用</th><th>平均搜索</th><th>展开节点</th><th>结果</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table></section>
<section><h2>地图与奖励</h2><p>{reward_text}</p></section>
<section class="note"><strong>判定边界：</strong>仅比较检查点中已覆盖的字段；未验证程序集身份、完整牌堆与 RNG。客户端独立执行的房间不计为锁步通过。旧日志按原记录展示，不追认字段完整性。</section>
<details><summary>动作与检查点详情</summary><pre style="overflow:auto;max-height:600px">{detail}</pre></details>
</main></body></html>"""


if __name__ == "__main__":
    main()
