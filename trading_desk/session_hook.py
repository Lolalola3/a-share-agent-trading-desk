"""Codex SessionStart hook: inject idempotent daily-session instructions.

The hook is intentionally read-only.  Hooks cannot create Codex tasks or run
MCP tools; they only add developer context to the first Agent turn.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = Path(os.environ.get("A_SHARE_DESK_HOME", str(PACKAGE_ROOT))).expanduser().resolve()
WORKSPACE = PACKAGE_ROOT.parent
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _read_input() -> dict[str, Any]:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def _load_session(day: str) -> dict[str, Any] | None:
    path = RUNTIME_ROOT / "state" / "task_sessions" / f"{day}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def main() -> None:
    payload = _read_input()
    cwd = Path(str(payload.get("cwd") or ".")).resolve()
    if cwd != PACKAGE_ROOT and cwd != WORKSPACE and PACKAGE_ROOT not in cwd.parents:
        return
    day = datetime.now(SHANGHAI).date().isoformat()
    session = _load_session(day)
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")
    if not session or session.get("status") in {None, "missing"}:
        context = (
            f"A股交易台启动要求：北京时间 {day} 尚无已登记日任务。"
            f"当前 Codex session_id={session_id or 'unknown'}。"
            "本轮先使用 a-share-trading-desk 技能与 analysis_protocol_get，"
            "创建或将当前合适任务登记为当日唯一任务，立即运行 startup 分析，并建立唯一心跳。"
            "禁止恢复八个固定节点。"
        )
    elif session.get("status") == "closed":
        context = f"A股交易台：{day} 已完成收盘归档，计时器应保持暂停；不要重新启动盘中分析。"
    elif session_id and session_id == str(session.get("thread_id")):
        context = (
            f"A股交易台：当前是 {day} 已登记日任务。先调用 analysis_runtime_poll；"
            "仅在返回 analyze 时按动态分析协议执行，任何手动分析完成后重置一小时计时。"
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
