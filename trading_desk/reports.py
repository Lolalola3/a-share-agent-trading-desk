from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import state


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _trades_for_dates(days: set[str]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for path in (state.RECORDS_DIR / "trades").glob("*.jsonl"):
        if path.stem in days:
            values.extend(_read_jsonl(path))
    return values


def _write_report(path: Path, title: str, lines: list[str]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# " + title + "\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def close_day(day: str, narrative: str = "") -> dict[str, Any]:
    account = state.get_account()
    trades = _trades_for_dates({day})
    pending = [item for item in account["pending_orders"] if item["status"] == "pending_feedback"]
    handoff = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "trading_day": day,
        "cash_available": account["cash_available"],
        "cash_frozen": account["cash_frozen"],
        "positions": account["positions"],
        "pending_orders": pending,
        "watchlist": state.get_watchlist()["candidates"],
        "watchlist_health": state.get_watchlist().get("health", {}),
        "note": narrative,
        "requires_reconciliation": account["reconciliation_status"] != "reconciled" or bool(pending),
    }
    handoff_path = state.STATE_DIR / "next_day_context.json"
    handoff_path.write_text(json.dumps(handoff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"- 可用资金：{account['cash_available']:.2f} 元",
        f"- 冻结资金：{account['cash_frozen']:.2f} 元",
        f"- 已记录成交或撤单反馈：{len(trades)} 条",
        f"- 等待反馈委托：{len(pending)} 笔",
        f"- 候选池健康状态：{handoff['watchlist_health'].get('status', '未知')}",
        f"- 是否需要账户核对：{'是' if handoff['requires_reconciliation'] else '否'}",
        f"- 分析备注：{narrative or '无'}",
    ]
    report_path = _write_report(state.JOURNAL_DIR / "daily" / f"{day}_summary.md", f"收盘日报：{day}", lines)
    return {"daily_summary": report_path, "next_day_context": str(handoff_path), "handoff": handoff}


def weekly_report(day: str) -> str:
    target = date.fromisoformat(day)
    iso_year, iso_week, _ = target.isocalendar()
    all_days = {path.stem for path in (state.RECORDS_DIR / "trades").glob("*.jsonl") if date.fromisoformat(path.stem).isocalendar()[:2] == (iso_year, iso_week)}
    trades = _trades_for_dates(all_days)
    filled = [item for item in trades if item["status"] in {"filled", "partial"}]
    cancelled = [item for item in trades if item["status"] == "cancelled"]
    lines = [
        f"- 有反馈记录的交易日：{len(all_days)}",
        f"- 成交或部分成交委托：{len(filled)} 笔",
        f"- 撤单委托：{len(cancelled)} 笔",
        "- 调整策略前，复核执行偏差、漏报反馈以及候选替换规则是否得到遵守。",
    ]
    return _write_report(state.JOURNAL_DIR / "weekly" / f"{iso_year}-W{iso_week:02d}.md", f"周度交易复盘：{iso_year}-W{iso_week:02d}", lines)


def monthly_report(day: str) -> str:
    target = date.fromisoformat(day)
    prefix = target.strftime("%Y-%m")
    all_days = {path.stem for path in (state.RECORDS_DIR / "trades").glob("*.jsonl") if path.stem.startswith(prefix)}
    trades = _trades_for_dates(all_days)
    lines = [
        f"- 有反馈记录的交易日：{len(all_days)}",
        f"- 已记录委托反馈：{len(trades)} 笔",
        "- 将实际结果与原始证据快照对照，不要用单个月份判断策略质量。",
    ]
    return _write_report(state.JOURNAL_DIR / "monthly" / f"{prefix}.md", f"月度交易复盘：{prefix}", lines)
