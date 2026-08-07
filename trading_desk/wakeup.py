"""Independent local timer and market-monitor workers for the daily Codex task.

The timer worker only waits on local state and dispatches once at its due time.
The monitor worker separately polls Tencent through Python's HTTP stack and
dispatches only when a configured threshold crosses.  Neither worker emits
ordinary polling turns into the Codex conversation.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import monitoring, state


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
TERMINAL_STATES = {"idle", "cancelled", "completed", "failed"}


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def timer_path() -> Path:
    return state.STATE_DIR / "wakeup_timer.json"


def monitor_path() -> Path:
    return state.STATE_DIR / "monitor_worker.json"


def get_timer() -> dict[str, Any]:
    return state._read_json(timer_path(), {
        "schema_version": 2,
        "worker_kind": "timer",
        "status": "idle",
        "day": None,
        "token": None,
        "run_at": None,
        "thread_id": None,
        "pid": None,
    })


def get_monitor() -> dict[str, Any]:
    return state._read_json(monitor_path(), {
        "schema_version": 1,
        "worker_kind": "monitor",
        "status": "idle",
        "day": None,
        "token": None,
        "thread_id": None,
        "pid": None,
        "poll_seconds": None,
    })


def get_status() -> dict[str, Any]:
    return {
        "delivery_mode": "split_local_timer_and_monitor",
        "timer": get_timer(),
        "monitor": get_monitor(),
    }


def _write_timer(payload: dict[str, Any]) -> dict[str, Any]:
    state._write_json(timer_path(), payload)
    return payload


def _write_monitor(payload: dict[str, Any]) -> dict[str, Any]:
    state._write_json(monitor_path(), payload)
    return payload


def _cancel(
    payload: dict[str, Any],
    writer: Callable[[dict[str, Any]], dict[str, Any]],
    day: str,
    reason: str,
) -> dict[str, Any]:
    if payload.get("day") != day or payload.get("status") in TERMINAL_STATES:
        return payload
    payload.update({
        "status": "cancelled",
        "cancelled_at": shanghai_now().isoformat(timespec="seconds"),
        "cancel_reason": str(reason),
        "token": uuid.uuid4().hex,
    })
    return writer(payload)


def cancel_timer(day: str, reason: str) -> dict[str, Any]:
    return _cancel(get_timer(), _write_timer, day, reason)


def cancel_monitor(day: str, reason: str) -> dict[str, Any]:
    return _cancel(get_monitor(), _write_monitor, day, reason)


def cancel_all(day: str, reason: str) -> dict[str, Any]:
    return {
        "timer": cancel_timer(day, reason),
        "monitor": cancel_monitor(day, reason),
    }


# Backward-compatible timer-only API for old callers during state migration.
def cancel(day: str, reason: str) -> dict[str, Any]:
    return cancel_timer(day, reason)


def _windows_subprocess_options(detached: bool = False) -> dict[str, Any]:
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if detached:
            creationflags |= (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {"creationflags": creationflags, "startupinfo": startupinfo}


def _worker_command(kind: str, token: str) -> list[str]:
    return [sys.executable, "-m", "trading_desk.wakeup", f"{kind}-worker", "--token", token]


def _launch_worker(kind: str, day: str, token: str) -> tuple[int, str]:
    log_dir = state.ROOT / "diagnostics" / "wakeup"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{day}_{kind}_{token[:8]}.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(  # noqa: S603 - fixed interpreter/module and validated token
            _worker_command(kind, token),
            cwd=state.ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **_windows_subprocess_options(detached=True),
        )
    return process.pid, str(log_path)


def arm_timer(
    day: str,
    thread_id: str,
    run_at: str,
    host_id: str = "local",
    reason: str = "timer_due",
    launch: bool = True,
) -> dict[str, Any]:
    if not thread_id:
        raise state.DeskError("本地计时器缺少目标任务 ID。")
    try:
        due = datetime.fromisoformat(run_at)
    except ValueError as exc:
        raise state.DeskError("本地计时器时间格式无效。") from exc
    if due.tzinfo is None:
        raise state.DeskError("本地计时器时间必须包含时区。")
    if due.astimezone(SHANGHAI).date().isoformat() != day:
        raise state.DeskError("本地计时器时间必须属于目标交易日。")
    token = uuid.uuid4().hex
    payload = {
        "schema_version": 2,
        "worker_kind": "timer",
        "status": "armed",
        "day": day,
        "token": token,
        "run_at": due.astimezone(SHANGHAI).isoformat(timespec="seconds"),
        "thread_id": str(thread_id),
        "host_id": str(host_id or "local"),
        "reason": str(reason),
        "armed_at": shanghai_now().isoformat(timespec="seconds"),
        "pid": None,
        "network_polling": False,
    }
    _write_timer(payload)
    if not launch or os.environ.get("A_SHARE_DESK_DISABLE_WAKEUP") == "1":
        return payload
    pid, log_path = _launch_worker("timer", day, token)
    current = get_timer()
    if current.get("token") == token and current.get("status") == "armed":
        current.update({"pid": pid, "log_path": log_path})
        return _write_timer(current)
    return payload


# Backward-compatible alias used by the runtime.
def arm(
    day: str,
    thread_id: str,
    run_at: str,
    host_id: str = "local",
    reason: str = "timer_due",
    launch: bool = True,
) -> dict[str, Any]:
    return arm_timer(day, thread_id, run_at, host_id, reason, launch)


def arm_monitor(
    day: str,
    thread_id: str,
    host_id: str = "local",
    poll_seconds: int = 30,
    reason: str = "active_monitor_plan",
    launch: bool = True,
) -> dict[str, Any]:
    if not thread_id:
        raise state.DeskError("本地监控进程缺少目标任务 ID。")
    plan = monitoring.get_plan(day)
    active = [item for item in plan.get("monitors", []) if item.get("enabled", True)]
    if not active:
        current = cancel_monitor(day, "no_active_monitors")
        if current.get("status") == "idle" and current.get("day") != day:
            current = {
                **current,
                "day": day,
                "thread_id": str(thread_id),
                "host_id": str(host_id or "local"),
                "reason": "no_active_monitors",
            }
            _write_monitor(current)
        return current
    token = uuid.uuid4().hex
    payload = {
        "schema_version": 1,
        "worker_kind": "monitor",
        "status": "armed",
        "day": day,
        "token": token,
        "thread_id": str(thread_id),
        "host_id": str(host_id or "local"),
        "reason": str(reason),
        "armed_at": shanghai_now().isoformat(timespec="seconds"),
        "poll_seconds": max(5, int(poll_seconds)),
        "plan_updated_at": plan.get("updated_at"),
        "active_monitor_count": len(active),
        "pid": None,
    }
    _write_monitor(payload)
    if not launch or os.environ.get("A_SHARE_DESK_DISABLE_WAKEUP") == "1":
        return payload
    pid, log_path = _launch_worker("monitor", day, token)
    current = get_monitor()
    if current.get("token") == token and current.get("status") == "armed":
        current.update({"pid": pid, "log_path": log_path})
        return _write_monitor(current)
    return payload


def _is_market_monitor_time(now: datetime) -> bool:
    hm = now.astimezone(SHANGHAI).strftime("%H:%M")
    return ("09:30" <= hm < "11:30") or ("13:00" <= hm < "15:00")


def _dispatch_prompt(day: str, thread_id: str, source: str, signal_summary: str = "") -> tuple[int, str]:
    executable = shutil.which("codex")
    if not executable:
        return 127, "未找到 codex CLI，无法唤醒任务。"
    force = ", force=true" if source == "monitor" else ""
    prompt = (
        "$a-share-trading-desk\n\n"
        f"本地静默唤醒器触发：{source}。先调用 analysis_protocol_get，再调用 "
        f"analysis_runtime_poll(day=\"{day}\", source=\"{source}\"{force})。"
        "若返回 analyze，严格按返回的 analysis_prompt 完成数据获取、事实→解读→规则→结论、"
        "监控选择、analysis_run_record 和 analysis_cycle_complete；完整输出 user_visible_output。"
        "若返回 skip，静默结束。仅分析，不连接券商、不自动下单。"
        + (f"监控信号摘要：{signal_summary}" if signal_summary else "")
    )
    command = [
        executable,
        "-C",
        str(state.ROOT),
        "exec",
        "resume",
        "--skip-git-repo-check",
        str(thread_id),
        prompt,
    ]
    completed = subprocess.run(  # noqa: S603 - no shell, fixed executable and argument vector
        command,
        cwd=state.ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=7200,
        check=False,
        **_windows_subprocess_options(detached=False),
    )
    detail = (completed.stdout or completed.stderr or "").strip()
    return completed.returncode, detail[-2000:]


def _finish_dispatch(
    getter: Callable[[], dict[str, Any]],
    writer: Callable[[dict[str, Any]], dict[str, Any]],
    token: str,
    code: int,
    detail: str,
    now: datetime,
) -> None:
    latest = getter()
    if latest.get("token") == token and latest.get("status") == "dispatching":
        latest.update({
            "status": "completed" if code == 0 else "failed",
            "dispatch_exit_code": code,
            "dispatch_detail": detail,
            "dispatch_completed_at": now.astimezone(SHANGHAI).isoformat(timespec="seconds"),
        })
        writer(latest)


def run_timer_worker(
    token: str,
    now_provider: Callable[[], datetime] = shanghai_now,
    sleep: Callable[[float], None] = time.sleep,
    dispatch: Callable[[str, str, str, str], tuple[int, str]] = _dispatch_prompt,
) -> dict[str, Any]:
    """Wait only on local timer state; never fetch market data."""
    while True:
        timer = get_timer()
        if timer.get("token") != token or timer.get("status") != "armed":
            return {"status": "cancelled_or_replaced"}
        now = now_provider().astimezone(SHANGHAI)
        due = datetime.fromisoformat(str(timer["run_at"])).astimezone(SHANGHAI)
        if now >= due:
            current = get_timer()
            if current.get("token") != token or current.get("status") != "armed":
                return {"status": "cancelled_or_replaced"}
            current.update({
                "status": "dispatching",
                "dispatch_source": "timer",
                "dispatch_started_at": now.isoformat(timespec="seconds"),
            })
            _write_timer(current)
            cancel_monitor(str(current["day"]), "timer_dispatching")
            code, detail = dispatch(str(current["day"]), str(current["thread_id"]), "timer", "")
            _finish_dispatch(get_timer, _write_timer, token, code, detail, now_provider())
            return {"status": "completed" if code == 0 else "failed", "exit_code": code}
        sleep(max(0.25, min((due - now).total_seconds(), 5.0)))


def run_monitor_worker(
    token: str,
    now_provider: Callable[[], datetime] = shanghai_now,
    sleep: Callable[[float], None] = time.sleep,
    snapshot_provider: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None,
    dispatch: Callable[[str, str, str, str], tuple[int, str]] = _dispatch_prompt,
) -> dict[str, Any]:
    """Poll only monitor rules and dispatch once on a real crossing signal."""
    from .market_packet import MarketPacketBuilder

    snapshot_provider = snapshot_provider or MarketPacketBuilder().monitor_snapshot
    while True:
        worker = get_monitor()
        if worker.get("token") != token or worker.get("status") != "armed":
            return {"status": "cancelled_or_replaced"}
        now = now_provider().astimezone(SHANGHAI)
        day = str(worker["day"])
        if now.date().isoformat() != day or now.strftime("%H:%M") >= "15:00":
            worker.update({
                "status": "completed",
                "completed_at": now.isoformat(timespec="seconds"),
                "completion_reason": "outside_monitor_session",
            })
            _write_monitor(worker)
            return {"status": "completed", "reason": "outside_monitor_session"}
        plan = monitoring.get_plan(day)
        active = [item for item in plan.get("monitors", []) if item.get("enabled", True)]
        if not active:
            worker.update({
                "status": "completed",
                "completed_at": now.isoformat(timespec="seconds"),
                "completion_reason": "no_active_monitors",
            })
            _write_monitor(worker)
            return {"status": "completed", "reason": "no_active_monitors"}
        if _is_market_monitor_time(now):
            codes = sorted({str(item["code"]) for item in active})
            try:
                result = monitoring.evaluate(day, snapshot_provider(codes), now)
                signals = list(result.get("signals") or [])
                worker["last_check_at"] = now.isoformat(timespec="seconds")
                worker["last_check_status"] = "ok"
                worker["unavailable_monitor_ids"] = result.get("unavailable_monitor_ids", [])
                _write_monitor(worker)
                if signals:
                    signal_summary = json.dumps(signals, ensure_ascii=False)
                    current = get_monitor()
                    if current.get("token") != token or current.get("status") != "armed":
                        return {"status": "cancelled_or_replaced"}
                    current.update({
                        "status": "dispatching",
                        "dispatch_source": "monitor",
                        "dispatch_started_at": now.isoformat(timespec="seconds"),
                        "signal_summary": signal_summary,
                    })
                    _write_monitor(current)
                    cancel_timer(day, "monitor_signal_dispatching")
                    code, detail = dispatch(day, str(current["thread_id"]), "monitor", signal_summary)
                    _finish_dispatch(get_monitor, _write_monitor, token, code, detail, now_provider())
                    return {"status": "completed" if code == 0 else "failed", "exit_code": code}
            except Exception as exc:
                worker = get_monitor()
                if worker.get("token") == token and worker.get("status") == "armed":
                    worker["last_check_status"] = "error"
                    worker["last_monitor_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
                    worker["last_monitor_error_at"] = now.isoformat(timespec="seconds")
                    _write_monitor(worker)
        sleep(float(max(5, int(worker.get("poll_seconds") or 30))))


# Old entry point now means timer-only and performs no monitoring/network access.
def run_worker(
    token: str,
    now_provider: Callable[[], datetime] = shanghai_now,
    sleep: Callable[[float], None] = time.sleep,
    snapshot_provider: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None,
    dispatch: Callable[[str, str, str, str], tuple[int, str]] = _dispatch_prompt,
) -> dict[str, Any]:
    del snapshot_provider
    return run_timer_worker(token, now_provider, sleep, dispatch)


def main() -> None:
    parser = argparse.ArgumentParser(description="A股交易台本地静默投递器")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("timer-worker", "monitor-worker", "worker"):
        worker = subparsers.add_parser(name)
        worker.add_argument("--token", required=True)
    args = parser.parse_args()
    if args.command == "monitor-worker":
        result = run_monitor_worker(args.token)
    else:
        result = run_timer_worker(args.token)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
