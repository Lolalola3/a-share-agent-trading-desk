from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import state


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def templates_path() -> Path:
    return state.ROOT / "monitoring" / "templates.json"


def plan_path(day: str) -> Path:
    return state.STATE_DIR / "monitor_plans" / f"{day}.json"


def runtime_path(day: str) -> Path:
    return state.STATE_DIR / "monitor_runtime" / f"{day}.json"


def load_templates() -> dict[str, Any]:
    path = templates_path()
    if not path.exists():
        raise state.DeskError("监控模板文件不存在，不能启用盘中监控。")
    payload = json.loads(path.read_text(encoding="utf-8"))
    templates = payload.get("templates")
    if not isinstance(templates, list) or not templates:
        raise state.DeskError("监控模板文件没有可用模板。")
    return payload


def get_plan(day: str) -> dict[str, Any]:
    return state._read_json(plan_path(day), {
        "schema_version": 1,
        "day": day,
        "status": "empty",
        "updated_at": None,
        "rationale": "",
        "monitors": [],
    })


def _tracked_instruments() -> dict[str, str]:
    account = state.get_account()
    watchlist = state.get_watchlist()
    tracked = {str(item["code"]): str(item.get("name", "")) for item in account.get("positions", [])}
    tracked.update({str(item["code"]): str(item.get("name", "")) for item in watchlist.get("candidates", [])})
    return tracked


def apply_plan(day: str, monitors: list[dict[str, Any]], rationale: str) -> dict[str, Any]:
    catalog = load_templates()
    template_by_id = {str(item["id"]): item for item in catalog["templates"]}
    if len(monitors) > int(catalog.get("max_active_monitors", 20)):
        raise state.DeskError("启用的监控规则超过数量上限。")
    tracked = _tracked_instruments()
    normalized = []
    seen_ids: set[str] = set()
    for item in monitors:
        template_id = str(item.get("template_id", ""))
        template = template_by_id.get(template_id)
        if template is None:
            raise state.DeskError(f"未知监控模板：{template_id}")
        code = str(item.get("code", ""))
        if code not in tracked:
            raise state.DeskError(f"{code} 不在当前持仓或候选池中，不能启用自动监控。")
        try:
            threshold = float(item["threshold"])
            rearm_delta = float(item.get("rearm_delta", template.get("default_rearm_delta", 0)))
            cooldown = int(item.get("cooldown_minutes", 30))
        except (KeyError, TypeError, ValueError) as exc:
            raise state.DeskError(f"{code} 的监控阈值参数无效。") from exc
        if rearm_delta < 0 or cooldown < 0:
            raise state.DeskError("监控重置距离和冷却时间不能为负数。")
        monitor_id = str(item.get("id") or uuid.uuid4().hex[:12])
        if monitor_id in seen_ids:
            raise state.DeskError("监控规则 ID 重复。")
        seen_ids.add(monitor_id)
        expires_at = item.get("expires_at")
        if expires_at:
            try:
                parsed_expiry = datetime.fromisoformat(str(expires_at))
                if parsed_expiry.tzinfo is None:
                    raise ValueError("timezone required")
            except ValueError as exc:
                raise state.DeskError(f"{code} 的监控到期时间必须是带时区的 ISO 时间。") from exc
        normalized.append({
            "id": monitor_id,
            "template_id": template_id,
            "template_name": template["name"],
            "code": code,
            "name": str(item.get("name") or tracked[code]),
            "metric": template["metric"],
            "operator": template["operator"],
            "threshold": threshold,
            "rearm_delta": rearm_delta,
            "cooldown_minutes": cooldown,
            "expires_at": str(expires_at) if expires_at else None,
            "note": str(item.get("note", "")),
            "enabled": bool(item.get("enabled", True)),
        })
    now = shanghai_now().isoformat(timespec="seconds")
    plan = {
        "schema_version": 1,
        "day": day,
        "status": "active" if normalized else "empty",
        "updated_at": now,
        "rationale": str(rationale),
        "monitors": normalized,
    }
    state._write_json(plan_path(day), plan)
    state._write_json(runtime_path(day), {
        "schema_version": 1,
        "day": day,
        "updated_at": now,
        "states": {item["id"]: {"armed": True, "last_signal_at": None, "last_value": None} for item in normalized},
    })
    return plan


def _metric_value(snapshot: dict[str, Any], path: str) -> float | None:
    value: Any = snapshot
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _matches(operator: str, value: float, threshold: float) -> bool:
    if operator == "gte":
        return value >= threshold
    if operator == "lte":
        return value <= threshold
    raise state.DeskError(f"监控模板使用了不支持的比较运算：{operator}")


def _rearmed(operator: str, value: float, threshold: float, delta: float) -> bool:
    if operator == "gte":
        return value <= threshold - delta
    if operator == "lte":
        return value >= threshold + delta
    return False


def _append_signal(day: str, signal: dict[str, Any]) -> None:
    path = state.RECORDS_DIR / "monitor_signals" / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(signal, ensure_ascii=False) + "\n")


def evaluate(day: str, snapshots: dict[str, dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    current = now or shanghai_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    plan = get_plan(day)
    runtime = state._read_json(runtime_path(day), {"schema_version": 1, "day": day, "states": {}})
    states = runtime.setdefault("states", {})
    signals: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for monitor in plan.get("monitors", []):
        if not monitor.get("enabled", True):
            continue
        expires_at = monitor.get("expires_at")
        if expires_at and current > datetime.fromisoformat(expires_at):
            continue
        value = _metric_value(snapshots.get(monitor["code"], {}), monitor["metric"])
        monitor_state = states.setdefault(monitor["id"], {"armed": True, "last_signal_at": None, "last_value": None})
        if value is None:
            unavailable.append(monitor["id"])
            continue
        threshold = float(monitor["threshold"])
        delta = float(monitor.get("rearm_delta", 0))
        last_signal_at = monitor_state.get("last_signal_at")
        cooldown_ready = True
        if last_signal_at:
            cooldown_ready = current >= datetime.fromisoformat(last_signal_at) + timedelta(minutes=int(monitor.get("cooldown_minutes", 0)))
        if not monitor_state.get("armed", True):
            if _rearmed(monitor["operator"], value, threshold, delta):
                monitor_state["armed"] = True
                monitor_state["rearmed_at"] = current.isoformat(timespec="seconds")
        elif cooldown_ready and _matches(monitor["operator"], value, threshold):
            signal = {
                "signal_id": uuid.uuid4().hex[:16],
                "monitor_id": monitor["id"],
                "template_id": monitor["template_id"],
                "triggered_at": current.isoformat(timespec="seconds"),
                "code": monitor["code"],
                "name": monitor["name"],
                "metric": monitor["metric"],
                "operator": monitor["operator"],
                "threshold": threshold,
                "value": value,
                "note": monitor.get("note", ""),
                "instruction": "该信号只触发重新分析，不代表自动买卖。",
            }
            signals.append(signal)
            _append_signal(day, signal)
            monitor_state["armed"] = False
            monitor_state["last_signal_at"] = signal["triggered_at"]
        monitor_state["last_value"] = value
        monitor_state["updated_at"] = current.isoformat(timespec="seconds")
    runtime["updated_at"] = current.isoformat(timespec="seconds")
    state._write_json(runtime_path(day), runtime)
    return {
        "day": day,
        "checked_at": current.isoformat(timespec="seconds"),
        "active_monitors": sum(bool(item.get("enabled", True)) for item in plan.get("monitors", [])),
        "signals": signals,
        "unavailable_monitor_ids": unavailable,
    }
