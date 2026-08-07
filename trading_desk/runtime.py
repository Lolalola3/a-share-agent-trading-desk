from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import monitoring, reports, state, wakeup


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def load_config() -> dict[str, Any]:
    path = state.ROOT / "config" / "runtime.json"
    defaults = {
        "analysis_interval_minutes": 60,
        "analysis_lease_minutes": 15,
        "failed_retry_minutes": 10,
        "earliest_analysis_time": "09:15",
        "continuous_session_start_time": "09:30",
        "market_close_time": "15:00",
        "local_monitor_poll_seconds": 30,
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


def _session_time(day: str, hm: str) -> datetime:
    try:
        return datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=SHANGHAI)
    except ValueError as exc:
        raise state.DeskError(f"运行时配置时间无效：{hm}") from exc


def market_phase(value: datetime | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    current = _normalize_now(value)
    cfg = config or load_config()
    hm = current.strftime("%H:%M")
    earliest = str(cfg["earliest_analysis_time"])
    continuous_start = str(cfg["continuous_session_start_time"])
    if hm < earliest:
        name = "waiting_before_open"
        analysis_mode = "waiting"
    elif hm < continuous_start:
        name = "opening_auction"
        analysis_mode = "pre_market"
    elif hm < "11:30":
        name = "morning_session"
        analysis_mode = "intraday"
    elif hm < "13:00":
        name = "midday_break"
        analysis_mode = "intraday"
    elif hm < "15:00":
        name = "afternoon_session"
        analysis_mode = "intraday"
    else:
        name = "post_close"
        analysis_mode = "close"
    allows_orders = (continuous_start <= hm < "11:25") or ("13:00" <= hm < "14:55")
    return {
        "name": name,
        "analysis_mode": analysis_mode,
        "as_of": current.isoformat(timespec="seconds"),
        "allows_new_orders": allows_orders,
        "include_intraday": analysis_mode in {"intraday", "close"},
        "analysis_prompt": "pre_market_session.md" if analysis_mode == "pre_market" else "daily_session.md",
        "requires_close_review": name == "post_close",
    }


def _ensure_trading_day(day: str) -> dict[str, Any]:
    account = state.get_account()
    account_day = str(account.get("as_of", ""))
    if account_day == day:
        return {"status": "current", "account_as_of": account_day}
    try:
        if account_day and datetime.fromisoformat(account_day).date() > datetime.fromisoformat(day).date():
            return {
                "status": "blocked",
                "account_as_of": account_day,
                "reason": "账户日期晚于目标交易日，禁止自动回退账户状态。",
            }
    except ValueError:
        return {"status": "blocked", "account_as_of": account_day, "reason": "账户交易日格式无效。"}
    pending = [item for item in account.get("pending_orders", []) if item.get("status") == "pending_feedback"]
    if pending:
        return {
            "status": "blocked",
            "account_as_of": account_day,
            "pending_order_ids": [str(item.get("id", "")) for item in pending],
            "reason": "存在未反馈委托，必须先核对成交或撤单，不能自动滚动交易日。",
        }
    rolled = state.rollover(day)
    return {
        "status": "rolled",
        "account_as_of": str(rolled.get("as_of", day)),
        "reason": "已在启动流程中自动完成交易日滚动与 T+1 可卖数量更新。",
    }


def get_session(day: str) -> dict[str, Any]:
    session = state.get_task_session(day)
    if session.get("status") == "missing":
        return session
    cfg = load_config()
    result = {
        "analysis_interval_minutes": cfg["analysis_interval_minutes"],
        "last_analysis_at": None,
        "next_analysis_at": None,
        "close_completed_at": None,
        "wakeup_timer": wakeup.get_timer(),
        "monitor_worker": wakeup.get_monitor(),
        "rollover": None,
        "current_cycle": None,
        "cycle_history": [],
        **session,
    }
    result.pop("heartbeat_interval_minutes", None)
    result.pop("heartbeat_automation_id", None)
    result.pop("sector_snapshot_maintenance", None)
    return result


def register_session(
    day: str,
    thread_id: str,
    host_id: str = "local",
    title: str = "",
    source: str = "daily_bootstrap",
    replace: bool = False,
) -> dict[str, Any]:
    previous = state._read_json(_session_path(day), {})
    base = state.register_task_session(day, thread_id, host_id, title, source, replace)
    same_thread = previous.get("thread_id") == thread_id
    cfg = load_config()
    rollover = _ensure_trading_day(day)
    initial_due = _session_time(day, str(cfg["earliest_analysis_time"])).isoformat(timespec="seconds")
    session = {
        **previous,
        **base,
        "analysis_interval_minutes": int(previous.get("analysis_interval_minutes", cfg["analysis_interval_minutes"])),
        "last_analysis_at": previous.get("last_analysis_at"),
        "next_analysis_at": previous.get("next_analysis_at") or initial_due,
        "close_completed_at": previous.get("close_completed_at"),
        "rollover": rollover,
        "current_cycle": previous.get("current_cycle") if same_thread else None,
        "cycle_history": list(previous.get("cycle_history", [])),
        "wakeup_timer": wakeup.get_timer(),
        "monitor_worker": wakeup.get_monitor(),
        "delivery_mode": "split_local_timer_and_monitor",
    }
    session.pop("heartbeat_interval_minutes", None)
    session.pop("heartbeat_automation_id", None)
    session.pop("sector_snapshot_maintenance", None)
    state._write_json(_session_path(day), session)
    return session


def register_heartbeat(day: str, automation_id: str) -> dict[str, Any]:
    raise state.DeskError("5分钟聊天心跳已停用；请使用本地可重置唤醒器。")


def migrate_session_delivery(day: str) -> dict[str, Any]:
    """Migrate delivery to independent timer and monitor workers without claiming analysis."""
    with _runtime_lock(day):
        session = state.get_task_session(day)
        if session.get("status") == "missing":
            raise state.DeskError("当日交易任务尚未创建，无法迁移计时器。")
        removed = []
        for key in ("heartbeat_interval_minutes", "heartbeat_automation_id"):
            if key in session:
                removed.append(key)
                session.pop(key, None)
        next_due = session.get("next_analysis_at")
        session["rollover"] = _ensure_trading_day(day)
        session.pop("sector_snapshot_maintenance", None)
        if session.get("status") == "closed" or not next_due:
            cancelled = wakeup.cancel_all(day, "session_delivery_migration_closed")
            timer = cancelled["timer"]
            monitor_worker = cancelled["monitor"]
        else:
            timer = _ensure_wakeup(session, day, str(next_due), "session_delivery_migration")
            monitor_worker = _ensure_monitor(session, day, "session_delivery_migration")
        session["wakeup_timer"] = timer
        session["monitor_worker"] = monitor_worker
        session["delivery_mode"] = "split_local_timer_and_monitor"
        session["updated_at"] = shanghai_now().isoformat(timespec="seconds")
        state._write_json(_session_path(day), session)
        return {
            "day": day,
            "status": "migrated",
            "removed_fields": removed,
            "next_analysis_at": next_due,
            "wakeup_timer": timer,
            "monitor_worker": monitor_worker,
        }


def _ensure_wakeup(session: dict[str, Any], day: str, run_at: str, reason: str) -> dict[str, Any]:
    timer = wakeup.get_timer()
    if (
        timer.get("status") == "armed"
        and timer.get("day") == day
        and timer.get("thread_id") == session.get("thread_id")
        and timer.get("run_at") == run_at
    ):
        return timer
    return wakeup.arm(
        day,
        str(session.get("thread_id", "")),
        run_at,
        str(session.get("host_id", "local")),
        reason,
    )


def _ensure_monitor(session: dict[str, Any], day: str, reason: str) -> dict[str, Any]:
    plan = monitoring.get_plan(day)
    active = [item for item in plan.get("monitors", []) if item.get("enabled", True)]
    if not active:
        return wakeup.cancel_monitor(day, "no_active_monitors")
    monitor_worker = wakeup.get_monitor()
    if (
        monitor_worker.get("status") == "armed"
        and monitor_worker.get("day") == day
        and monitor_worker.get("thread_id") == session.get("thread_id")
        and monitor_worker.get("plan_updated_at") == plan.get("updated_at")
    ):
        return monitor_worker
    cfg = load_config()
    return wakeup.arm_monitor(
        day,
        str(session.get("thread_id", "")),
        str(session.get("host_id", "local")),
        int(cfg["local_monitor_poll_seconds"]),
        reason,
    )


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
    source: str = "timer",
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
        phase = market_phase(current, cfg)
        rollover = _ensure_trading_day(day)
        session["rollover"] = rollover
        if rollover["status"] == "blocked":
            session["updated_at"] = current.isoformat(timespec="seconds")
            state._write_json(_session_path(day), session)
            return {
                "action": "blocked",
                "day": day,
                "reason": rollover["reason"],
                "rollover": rollover,
                "phase": phase,
            }
        session.pop("sector_snapshot_maintenance", None)
        earliest = _session_time(day, str(cfg["earliest_analysis_time"]))
        if current < earliest:
            session["next_analysis_at"] = earliest.isoformat(timespec="seconds")
            session["wakeup_timer"] = _ensure_wakeup(
                session, day, session["next_analysis_at"], "earliest_analysis_time"
            )
            session["monitor_worker"] = wakeup.cancel_monitor(day, "before_earliest_analysis_time")
            session["updated_at"] = current.isoformat(timespec="seconds")
            state._write_json(_session_path(day), session)
            return {
                "action": "skip",
                "day": day,
                "reason_code": "before_earliest_analysis_time",
                "reason": f"最早分析时间为 {cfg['earliest_analysis_time']}；当前只保持日任务和本地静默唤醒器，不拉取行情、不执行分析。",
                "next_analysis_at": session["next_analysis_at"],
                "phase": phase,
            }
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
            if session.get("next_analysis_at"):
                session["wakeup_timer"] = _ensure_wakeup(
                    session, day, str(session["next_analysis_at"]), "next_analysis_at"
                )
            session["monitor_worker"] = _ensure_monitor(session, day, "active_monitor_plan")
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
        cancelled = wakeup.cancel_all(day, f"analysis_claimed:{run_id}")
        session["wakeup_timer"] = cancelled["timer"]
        session["monitor_worker"] = cancelled["monitor"]
        session["current_cycle"] = cycle
        session["updated_at"] = current.isoformat(timespec="seconds")
        state._write_json(_session_path(day), session)
        return {
            "action": "analyze",
            "day": day,
            "run_id": run_id,
            "trigger_reasons": cycle["reasons"],
            "phase": phase,
            "analysis_mode": phase["analysis_mode"],
            "analysis_prompt": phase["analysis_prompt"],
            "include_intraday": phase["include_intraday"],
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
        user_visible_output: str | None = None
        if status == "completed":
            validated = reports.validate_analysis_record(
                state._read_json(sorted(run_paths)[-1]), require_rendered=True
            )
            user_visible_output = validated["user_visible_output"]
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
            cancelled = wakeup.cancel_all(day, "close_session_completed")
            session["wakeup_timer"] = cancelled["timer"]
            session["monitor_worker"] = cancelled["monitor"]
            timer_action = "pause"
        else:
            if status == "completed" and (cycle.get("phase") or {}).get("analysis_mode") == "pre_market":
                continuous_start = _session_time(day, str(cfg["continuous_session_start_time"]))
                session["next_analysis_at"] = max(current, continuous_start).isoformat(timespec="seconds")
                timer_action = "await_continuous_session"
            else:
                delay = int(cfg["analysis_interval_minutes"] if status == "completed" else cfg["failed_retry_minutes"])
                next_due = current + timedelta(minutes=delay)
                close_at = _session_time(day, str(cfg["market_close_time"]))
                if status == "completed" and current < close_at < next_due:
                    next_due = close_at
                session["next_analysis_at"] = next_due.isoformat(timespec="seconds")
                timer_action = "keep_active"
            session["wakeup_timer"] = _ensure_wakeup(
                session,
                day,
                str(session["next_analysis_at"]),
                "continuous_session_start" if timer_action == "await_continuous_session" else "analysis_interval",
            )
            session["monitor_worker"] = _ensure_monitor(session, day, "analysis_cycle_completed")
        session["updated_at"] = current.isoformat(timespec="seconds")
        state._write_json(_session_path(day), session)
        return {
            "session": session,
            "cycle": cycle,
            "user_visible_output": user_visible_output,
            "display_contract": (
                "本轮回复必须完整原样输出 user_visible_output，不得只发送 summary。"
                if user_visible_output
                else None
            ),
            "timer": {
                "action": timer_action,
                "delivery": "split_local_timer_and_monitor",
                "timer_worker_pid": (session.get("wakeup_timer") or {}).get("pid"),
                "monitor_worker_pid": (session.get("monitor_worker") or {}).get("pid"),
                "next_analysis_at": session.get("next_analysis_at"),
            },
        }
