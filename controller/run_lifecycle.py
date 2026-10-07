from __future__ import annotations

from typing import Any
from pathlib import Path
import re


def menu_action(state: dict[str, Any]):
    """Choose only observed lifecycle controls; None means wait/unsupported."""
    actions = set(state.get('available_actions') or [])
    if 'confirm_timeline_overlay' in actions:
        return 'confirm_timeline_overlay', {}
    timeline = state.get('timeline') or {}
    if 'choose_timeline_epoch' in actions:
        for slot in timeline.get('slots') or []:
            # Complete slots open an inspection panel, not a new reward.
            if (slot.get('state') == 'Obtained' and slot.get('is_actionable')
                    and type(slot.get('index')) is int):
                return 'choose_timeline_epoch', {'option_index': slot['index']}
    if 'close_main_menu_submenu' in actions:
        return 'close_main_menu_submenu', {}
    if 'return_to_main_menu' in actions:
        return 'return_to_main_menu', {}
    return None


def run_summary(report: dict[str, Any], session_id: str, fallback_time: float):
    if report.get('execution_mode') == 'headless_only':
        headless = report.get('headless_summary') or {}
        status = report.get('status', 'UNKNOWN')
        error_text = str(report.get('error') or '')
        observed_floor = max((int(((action.get('headless_before') or {}).get('run') or {}).get('floor') or 0)
                              for action in report.get('actions') or []), default=0)
        return {
            'id': session_id, 'status': status, 'execution_mode': 'headless_only',
            'scoring_model_id': (report.get('config') or {}).get('scoring_model_id'),
            'created_at': report.get('created_at') or fallback_time,
            'finished_at': report.get('finished_at'),
            'floor': headless.get('last_floor') or observed_floor,
            'deck_size': (headless.get('final_deck') or {}).get('deck_size'),
            'error': status in {'FAIL', 'BLOCKED', 'WATCHDOG', 'INTERRUPTED'},
            'error_reason': error_text[:120] if error_text else None,
            'error_example': error_text[:240] if error_text else None,
            'seed': (report.get('identity') or {}).get('run_id'),
            'started': bool(report.get('actions')), 'resume_from': None,
            'act1_passed': int(headless.get('max_act') or 0) >= 2,
            'act2_passed': int(headless.get('max_act') or 0) >= 3,
        }
    states = []
    for action in report.get('actions') or []:
        states.extend([action.get('client_before') or {}, action.get('client_after') or {}])
    states.append(report.get('terminal_client') or {})
    floors = [(s.get('run') or {}).get('floor') for s in states]
    decks = [(s.get('run') or {}).get('deck') for s in states]
    deck = next((d for d in reversed(decks) if isinstance(d, list)), None)
    status = report.get('status', 'UNKNOWN')
    identity = report.get('identity') or {}
    seed = identity.get('run_id') or (report.get('terminal_client') or {}).get('run_id')
    source = report.get('resume_report')
    resume_from = None
    if source:
        source_path = Path(source)
        if source_path.name == 'run_report.json' and source_path.parent.parent.name == 'live_dashboard':
            resume_from = f'live_dashboard/{source_path.parent.name}'
    error_text = str(report.get('error') or '')
    error_line = next((line.strip() for line in error_text.splitlines() if line.strip()), '')
    lower_error = error_line.lower()
    if 'restore' in lower_error or 'snapshot' in lower_error or 'recovery' in lower_error:
        error_reason = '快照或恢复失败'
    elif 'parity failed' in lower_error or 'mismatch' in lower_error or '不一致' in error_line:
        error_reason = '状态对照不一致'
    elif 'reward' in lower_error and ('differ' in lower_error or 'changed' in lower_error):
        error_reason = '奖励集合不一致'
    elif 'headless failed to execute shadow' in lower_error:
        error_reason = '影子事件执行失败'
    elif 'mod api' in lower_error and ('timed out' in lower_error or 'timeout' in lower_error):
        error_reason = '客户端通信超时'
    elif 'deadline expired' in lower_error or 'did not settle' in lower_error:
        error_reason = '动作结算超时'
    elif 'watchdog' in lower_error or status == 'WATCHDOG':
        error_reason = '长时间无进展'
    elif 'search' in lower_error or 'worker' in lower_error:
        error_reason = '搜索执行失败'
    else:
        error_reason = re.sub(r'\b\d+(?:\.\d+)?\b', '#', error_line)[:120] if error_line else status
    floor = max((f for f in floors if type(f) is int), default=None)
    acts = []
    for state in states:
        act = (state.get('run') or {}).get('act_id')
        if act is not None and str(act).isdigit():
            acts.append(int(act))
    max_act = max(acts, default=None)
    return {
        'id': session_id, 'status': status,
        'scoring_model_id': (report.get('config') or {}).get('scoring_model_id'),
        'created_at': report.get('created_at') or fallback_time,
        'finished_at': report.get('finished_at'),
        'floor': floor,
        'deck_size': len(deck) if deck is not None else None,
        'error': bool(report.get('error')) or status in {'FAIL', 'BLOCKED', 'WATCHDOG', 'INTERRUPTED'},
        'error_reason': error_reason if error_text or status in {'FAIL', 'BLOCKED', 'WATCHDOG', 'INTERRUPTED'} else None,
        'error_example': error_line[:240] or None,
        'seed': seed, 'started': bool(identity.get('run_id') or report.get('actions')),
        'resume_from': resume_from,
        'act1_passed': max_act >= 1 if max_act is not None else floor is not None and floor >= 18,
        'act2_passed': max_act >= 2 if max_act is not None else floor is not None and floor >= 35,
    }
