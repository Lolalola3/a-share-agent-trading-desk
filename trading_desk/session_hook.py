"""Codex SessionStart hook: inject idempotent daily-session instructions.

The hook is intentionally read-only.  Hooks cannot create Codex tasks or run
MCP tools; they only add developer context to the first Agent turn.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT.parent
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _read_input() -> dict[str, Any]:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def _load_session(day: str) -> dict[str, Any] | None:
    path = ROOT / "state" / "task_sessions" / f"{day}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def main() -> None:
    payload = _read_input()
    cwd = Path(str(payload.get("cwd") or ".")).resolve()
    if cwd != ROOT and cwd != WORKSPACE and ROOT not in cwd.parents:
        return
    current = datetime.now(SHANGHAI)
    day = current.date().isoformat()
    hm = current.strftime("%H:%M")
    session = _load_session(day)
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")
    if not session or session.get("status") in {None, "missing"}:
        timing = (
            "当前早于09:15：只登记唯一日任务并启用纯本地计时worker；analysis_runtime_poll必须返回skip，禁止拉行情或分析。"
            if hm < "09:15"
            else "当前09:15-09:30：首次分析必须使用pre_market_session.md且include_intraday=false。"
            if hm < "09:30"
            else "当前已进入连续竞价：按daily_session.md执行首次完整分析。"
        )
        context = (
            f"A股交易台启动要求：北京时间 {day} 尚无已登记日任务。"
            f"当前 Codex session_id={session_id or 'unknown'}。"
            "本轮先使用 a-share-trading-desk 技能与 analysis_protocol_get，"
            "创建或将当前合适任务登记为当日唯一任务，启用拆分的timer/monitor worker，并让运行时自动完成交易日滚动。"
            f"{timing}"
            "禁止恢复八个固定节点。"
        )
    elif session.get("status") == "closed":
        context = f"A股交易台：{day} 已完成收盘归档，计时器应保持暂停；不要重新启动盘中分析。"
    elif session_id and session_id == str(session.get("thread_id")):
        timing = (
            "早于09:15时即使force=true也不得分析。"
            if hm < "09:15"
            else "09:30前若返回analyze，必须走盘前专用协议且不请求分时。"
            if hm < "09:30"
            else "连续竞价阶段按盘中协议执行。"
        )
        context = (
            f"A股交易台：当前是 {day} 已登记日任务。先调用 analysis_runtime_poll；"
            f"仅在返回 analyze 时按其 analysis_prompt 执行。{timing}"
            "盘前分析完成后下一次安排在09:30；盘中手动分析完成后重置一小时计时。"
        )
    else:
        context = (
            f"A股交易台：{day} 已有唯一活动任务 {session.get('thread_id')}。"
            "先用 Codex 任务读取工具验证它是否可访问；可访问时禁止创建第二个任务。"
            f"若明确返回不存在，允许将当前 session_id={session_id or 'unknown'} 作为失效登记替换，随后执行动态协议。"
        )
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
