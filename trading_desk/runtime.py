from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import monitoring, reports, state


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def load_config() -> dict[str, Any]:
    path = state.PACKAGE_ROOT / "config" / "runtime.json"
    defaults = {
        "analysis_interval_minutes": 60,
        "heartbeat_interval_minutes": 5,
        "analysis_lease_minutes": 15,
        "failed_retry_minutes": 10,
        "market_close_time": "15:00",
    }
    if not path.exists():
        return defaults
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return {**defaults, **loaded}


def _session_path(day: str) -> Path:
    return state.STATE_DIR / "task_sessions" / f"{day}.json"


def _normalize_now(value: datetime | None) -> datetime:
    current = value or shanghai_now()
    return current.replace(tzinfo=SHANGHAI) if current.tzinfo is None else current.astimezone(SHANGHAI)


def market_phase(value: datetime | None = None) -> dict[str, Any]:
    current = _normalize_now(value)
    hm = current.strftime("%H:%M")
    if hm < "09:15":
        name = "pre_market"
    elif hm < "09:30":
        name = "opening_auction"
    elif hm < "11:30":
        name = "morning_session"
    elif hm < "13:00":
        name = "midday_break"
    elif hm < "15:00":
        name = "afternoon_session"
    else:
        name = "post_close"
    allows_orders = ("09:25" <= hm < "11:25") or ("12:55" <= hm < "14:55")
    return {
        "name": name,
        "as_of": current.isoformat(timespec="seconds"),
        "allows_new_orders": allows_orders,
        "requires_close_review": name == "post_close",
    }


def get_session(day: str) -> dict[str, Any]:
    session = state.get_task_session(day)
    if session.get("status") == "missing":
        return session
    cfg = load_config()
    return {
        "analysis_interval_minutes": cfg["analysis_interval_minutes"],
        "heartbeat_interval_minutes": cfg["heartbeat_interval_minutes"],
        "last_analysis_at": None,
        "next_analysis_at": None,
        "close_completed_at": None,
        "heartbeat_automation_id": None,
        "current_cycle": None,
        "cycle_history": [],
        **session,
    }


def register_session(
    day: str,
    thread_id: str,
    host_id: str = "local",
    title: str = "",
    source: str = "daily_bootstrap",
    replace: bool = False,
) -> dict[str, Any]:
    base = state.register_task_session(day, thread_id, host_id, title, source, replace)
    existing = state._read_json(_session_path(day), {})
    cfg = load_config()
    session = {
        **existing,
        **base,
        "analysis_interval_minutes": int(existing.get("analysis_interval_minutes", cfg["analysis_interval_minutes"])),
        "heartbeat_interval_minutes": int(existing.get("heartbeat_interval_minutes", cfg["heartbeat_interval_minutes"])),
        "last_analysis_at": existing.get("last_analysis_at"),
        "next_analysis_at": existing.get("next_analysis_at"),
        "close_completed_at": existing.get("close_completed_at"),
        "heartbeat_automation_id": existing.get("heartbeat_automation_id"),
        "current_cycle": existing.get("current_cycle"),
        "cycle_history": list(existing.get("cycle_history", [])),
    }
    state._write_json(_session_path(day), session)
    return session


def register_heartbeat(day: str, automation_id: str) -> dict[str, Any]:
    session = get_session(day)
    if session.get("status") == "missing":
        raise state.DeskError("当日交易任务尚未登记。")
    if not automation_id:
        raise state.DeskError("心跳自动化 ID 不能为空。")
    existing = session.get("heartbeat_automation_id")
    if existing and existing != automation_id:
        raise state.DeskError("当日任务已登记其他心跳自动化，禁止静默覆盖。")
    session["heartbeat_automation_id"] = automation_id
    session["updated_at"] = shanghai_now().isoformat(timespec="seconds")
    state._write_json(_session_path(day), session)
    return session


@contextmanager
def _runtime_lock(day: str) -> Iterator[None]:
    directory = state.STATE_DIR / "runtime_locks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day}.lock"
    deadline = time.monotonic() + 2.0
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > 60:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise state.DeskError("分析运行时锁繁忙，请稍后重试。")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _is_due(timestamp: str | None, now: datetime) -> bool:
    if not timestamp:
        return True
    return now >= datetime.fromisoformat(timestamp)


def poll(
    day: str,
    source: str = "heartbeat",
    force: bool = False,
    snapshot_provider: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _normalize_now(now)
    if day != current.date().isoformat():
        return {"action": "skip", "day": day, "reason": "仅允许处理北京时间今天的会话。"}
    with _runtime_lock(day):
        session = get_session(day)
        if session.get("status") == "missing":
            return {"action": "bootstrap", "day": day, "reason": "当日交易任务尚未创建。"}
        if session.get("status") == "closed":
            if force and source == "close_revision":
                session["status"] = "active"
                session["close_completed_at"] = None
                session["next_analysis_at"] = None
                session["reopened_at"] = current.isoformat(timespec="seconds")
                session["reopen_reason"] = "用户要求重做收盘分析与次日计划"
            else:
                return {"action": "skip", "day": day, "reason": "当日收盘复盘已完成，计时器已暂停。", "session": session}
        cfg = load_config()
        phase = market_phase(current)
        active_cycle = session.get("current_cycle") or {}
        if active_cycle.get("status") == "running":
            lease_until = datetime.fromisoformat(active_cycle["lease_until"])
            if current < lease_until:
                return {"action": "skip", "day": day, "reason": "已有分析正在运行。", "current_cycle": active_cycle}
            active_cycle["status"] = "expired"
            active_cycle["expired_at"] = current.isoformat(timespec="seconds")
            session.setdefault("cycle_history", []).append(active_cycle)
            session["current_cycle"] = None

        monitor_result = {"signals": [], "active_monitors": 0, "unavailable_monitor_ids": []}
        plan = monitoring.get_plan(day)
        active_codes = sorted({item["code"] for item in plan.get("monitors", []) if item.get("enabled", True)})
        if active_codes and snapshot_provider is not None:
            try:
                monitor_result = monitoring.evaluate(day, snapshot_provider(active_codes), current)
            except Exception as exc:
                monitor_result = {
                    "signals": [],
                    "active_monitors": len(active_codes),
                    "unavailable_monitor_ids": [item["id"] for item in plan.get("monitors", [])],
                    "error": f"{type(exc).__name__}: {str(exc)[:120]}",
                }

        reasons: list[str] = []
        if force or source in {"startup", "user", "manual", "close_revision"}:
            reasons.append(source)
        if _is_due(session.get("next_analysis_at"), current):
            reasons.append("timer_due")
        if monitor_result.get("signals"):
            reasons.append("monitor_signal")
        if phase["requires_close_review"] and not session.get("close_completed_at"):
            reasons.append("market_close")
        if not reasons:
            session["updated_at"] = current.isoformat(timespec="seconds")
            state._write_json(_session_path(day), session)
            return {
                "action": "skip",
                "day": day,
                "reason": "计时未到且没有监控信号。",
                "next_analysis_at": session.get("next_analysis_at"),
                "monitor_result": monitor_result,
            }

        run_id = f"{day}-{current:%H%M%S}-{uuid.uuid4().hex[:8]}"
        cycle = {
            "run_id": run_id,
            "status": "running",
            "source": source,
            "reasons": list(dict.fromkeys(reasons)),
            "claimed_at": current.isoformat(timespec="seconds"),
            "lease_until": (current + timedelta(minutes=int(cfg["analysis_lease_minutes"]))).isoformat(timespec="seconds"),
            "phase": phase,
            "monitor_signals": monitor_result.get("signals", []),
        }
        session["current_cycle"] = cycle
        session["updated_at"] = current.isoformat(timespec="seconds")
        state._write_json(_session_path(day), session)
        return {
            "action": "analyze",
            "day": day,
            "run_id": run_id,
            "trigger_reasons": cycle["reasons"],
            "phase": phase,
            "close_required": phase["requires_close_review"],
            "monitor_result": monitor_result,
        }


def complete_cycle(
    day: str,
    run_id: str,
    status: str,
    summary: str,
    close_session: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    if status not in {"completed", "failed"}:
        raise state.DeskError("分析状态必须是 completed 或 failed。")
    current = _normalize_now(now)
    with _runtime_lock(day):
        session = get_session(day)
        cycle = session.get("current_cycle") or {}
        if cycle.get("run_id") != run_id:
            completed = next((item for item in session.get("cycle_history", []) if item.get("run_id") == run_id), None)
            if completed:
                return {"session": session, "cycle": completed, "idempotent": True}
            raise state.DeskError("运行 ID 与当前分析周期不一致。")
        run_paths = list((state.RECORDS_DIR / "runs" / day).glob(f"*_{run_id}.json"))
        if status == "completed" and not run_paths:
            raise state.DeskError("未找到对应 analysis_run_record，不能完成分析周期。")
        if close_session and status == "completed":
            report = state.JOURNAL_DIR / "daily" / f"{day}_summary.md"
            if not report.exists():
                raise state.DeskError("收盘周期必须先完成 reports_close_day 归档。")
            artifact_path = state.RECORDS_DIR / "close_reviews" / f"{day}.json"
            artifact = state._read_json(artifact_path)
            if not artifact or artifact.get("run_id") != run_id:
                raise state.DeskError("收盘周期缺少与当前 run_id 对应的三段式 close_review 归档。")
            reports.validate_close_review(day, artifact.get("review"))
        cycle.update({
            "status": status,
            "completed_at": current.isoformat(timespec="seconds"),
            "summary": str(summary),
        })
        session.setdefault("cycle_history", []).append(cycle)
        session["current_cycle"] = None
        session["last_analysis_at"] = current.isoformat(timespec="seconds")
        cfg = load_config()
        if close_session and status == "completed":
            session["status"] = "closed"
            session["close_completed_at"] = current.isoformat(timespec="seconds")
            session["next_analysis_at"] = None
            timer_action = "pause"
        else:
            delay = int(cfg["analysis_interval_minutes"] if status == "completed" else cfg["failed_retry_minutes"])
            session["next_analysis_at"] = (current + timedelta(minutes=delay)).isoformat(timespec="seconds")
            timer_action = "keep_active"
        session["updated_at"] = current.isoformat(timespec="seconds")
        state._write_json(_session_path(day), session)
        return {
            "session": session,
            "cycle": cycle,
            "timer": {
                "action": timer_action,
                "heartbeat_automation_id": session.get("heartbeat_automation_id"),
                "heartbeat_interval_minutes": session.get("heartbeat_interval_minutes"),
                "next_analysis_at": session.get("next_analysis_at"),
            },
        }
