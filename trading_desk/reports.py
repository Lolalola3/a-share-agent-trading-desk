from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import state


NEXT_DAY_SIDES = {"buy": "买入", "sell": "卖出", "hold": "持有", "watch": "观察"}


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


def _required_text(section: dict[str, Any], key: str, label: str) -> str:
    value = str(section.get(key, "")).strip()
    if not value:
        raise state.DeskError(f"收盘复盘缺少{label}。")
    return value


def _validate_next_day_action(action: dict[str, Any]) -> dict[str, Any]:
    required = ("time", "code", "name", "side", "exact_price", "shares", "feedback_wait", "trigger", "invalidation", "rationale")
    if any(key not in action or action.get(key) in {None, ""} for key in required):
        raise state.DeskError("明日行动预案字段不完整。")
    side = str(action["side"])
    if side not in NEXT_DAY_SIDES:
        raise state.DeskError("明日行动方向必须是 buy、sell、hold 或 watch。")
    try:
        price = round(float(action["exact_price"]), 3)
        shares = int(action["shares"])
    except (TypeError, ValueError) as exc:
        raise state.DeskError("明日行动的精确价格或数量无效。") from exc
    if price <= 0 or shares < 0 or shares % 100:
        raise state.DeskError("明日行动价格必须大于0，数量必须是非负100股整数倍。")
    code = str(action["code"])
    name = str(action["name"])
    instruction_line = (
        f"{action['time']}，{code} {name}，明日{NEXT_DAY_SIDES[side]}参考价 {price:.3f} 元，"
        f"{shares} 股，{action['feedback_wait']}"
    )
    return {
        "time": str(action["time"]),
        "code": code,
        "name": name,
        "side": side,
        "exact_price": price,
        "shares": shares,
        "feedback_wait": str(action["feedback_wait"]),
        "trigger": str(action["trigger"]),
        "invalidation": str(action["invalidation"]),
        "rationale": str(action["rationale"]),
        "status": "conditional_plan_not_order",
        "instruction_line": instruction_line,
    }


def validate_close_review(day: str, review: dict[str, Any]) -> dict[str, Any]:
    """Validate analysis -> day review -> next-day outlook as one close artifact."""
    if not isinstance(review, dict):
        raise state.DeskError("收盘复盘必须提供结构化 review。")
    close_analysis = review.get("close_analysis")
    day_review = review.get("day_review")
    outlook = review.get("next_day_outlook")
    if not all(isinstance(item, dict) for item in (close_analysis, day_review, outlook)):
        raise state.DeskError("收盘复盘必须包含 close_analysis、day_review、next_day_outlook。")
    packet_path = Path(_required_text(close_analysis, "packet_path", "收盘分析数据包路径")).resolve()
    packet_root = (state.RECORDS_DIR / "market_packets" / day).resolve()
    if packet_root not in packet_path.parents or not packet_path.is_file():
        raise state.DeskError("收盘分析必须引用当日已持久化的市场数据包。")
    try:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise state.DeskError("收盘市场数据包无法读取。") from exc
    if packet.get("trigger") != "market_close" or str(packet.get("generated_at", ""))[:10] != day:
        raise state.DeskError("收盘分析数据包必须由当日 market_close 触发生成。")
    normalized_close = {
        "packet_path": str(packet_path),
        "data_health": _required_text(close_analysis, "data_health", "数据健康分析"),
        "market_analysis": _required_text(close_analysis, "market_analysis", "大盘分析"),
        "sector_analysis": _required_text(close_analysis, "sector_analysis", "板块分析"),
        "holding_analysis": _required_text(close_analysis, "holding_analysis", "持仓分析"),
        "candidate_analysis": _required_text(close_analysis, "candidate_analysis", "候选池分析"),
        "conclusion": _required_text(close_analysis, "conclusion", "收盘分析结论"),
    }
    normalized_day = {
        "record_summary": _required_text(day_review, "record_summary", "当日记录复核"),
        "orders_and_feedback": _required_text(day_review, "orders_and_feedback", "委托与反馈复核"),
        "execution_deviations": list(day_review.get("execution_deviations") or []),
        "lessons": list(day_review.get("lessons") or []),
        "account_reconciliation": _required_text(day_review, "account_reconciliation", "账户核对结论"),
    }
    if not normalized_day["lessons"]:
        raise state.DeskError("收盘复盘必须给出至少一条当日经验或纪律结论。")
    next_day = str(outlook.get("trading_day", ""))
    try:
        if date.fromisoformat(next_day) <= date.fromisoformat(day):
            raise ValueError("not future")
    except ValueError as exc:
        raise state.DeskError("next_day_outlook.trading_day 必须晚于复盘日。") from exc
    position_plan = [_validate_next_day_action(item) for item in list(outlook.get("position_plan") or [])]
    candidate_plan = [_validate_next_day_action(item) for item in list(outlook.get("candidate_plan") or [])]
    normalized_outlook = {
        "trading_day": next_day,
        "market_expectation": _required_text(outlook, "market_expectation", "明日市场预期"),
        "base_case": _required_text(outlook, "base_case", "明日基准情景"),
        "bull_case": _required_text(outlook, "bull_case", "明日偏强情景"),
        "bear_case": _required_text(outlook, "bear_case", "明日偏弱情景"),
        "position_plan": position_plan,
        "candidate_plan": candidate_plan,
        "risk_points": list(outlook.get("risk_points") or []),
        "pre_market_checks": list(outlook.get("pre_market_checks") or []),
        "no_trade_conditions": list(outlook.get("no_trade_conditions") or []),
    }
    if not normalized_outlook["risk_points"] or not normalized_outlook["pre_market_checks"]:
        raise state.DeskError("明日展望必须包含风险点和盘前核验事项。")
    if not position_plan and not candidate_plan and not normalized_outlook["no_trade_conditions"]:
        raise state.DeskError("明日展望必须提供行动预案或明确不交易条件。")
    return {
        "schema_version": 1,
        "trading_day": day,
        "close_analysis": normalized_close,
        "day_review": normalized_day,
        "next_day_outlook": normalized_outlook,
    }


def close_day(day: str, run_id: str, review: dict[str, Any], narrative: str = "") -> dict[str, Any]:
    normalized_review = validate_close_review(day, review)
    run_paths_for_id = list((state.RECORDS_DIR / "runs" / day).glob(f"*_{run_id}.json"))
    if not run_paths_for_id:
        raise state.DeskError("收盘归档前必须先保存对应 analysis_run_record。")
    run_payload = json.loads(run_paths_for_id[-1].read_text(encoding="utf-8"))
    if run_payload.get("close_review") != review:
        raise state.DeskError("analysis_run_record 中的 close_review 必须与归档内容一致。")
    account = state.get_account()
    trades = _trades_for_dates({day})
    pending = [item for item in account["pending_orders"] if item["status"] == "pending_feedback"]
    run_paths = sorted((state.RECORDS_DIR / "runs" / day).glob("*.json"))
    runs = []
    for path in run_paths:
        try:
            runs.append({"path": str(path), "payload": json.loads(path.read_text(encoding="utf-8"))})
        except (OSError, json.JSONDecodeError):
            runs.append({"path": str(path), "payload": {"status": "unreadable"}})
    monitor_signals = _read_jsonl(state.RECORDS_DIR / "monitor_signals" / f"{day}.jsonl")
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
        "close_review": normalized_review,
        "analysis_runs": runs,
        "monitor_signals": monitor_signals,
        "requires_reconciliation": account["reconciliation_status"] != "reconciled" or bool(pending),
    }
    handoff_path = state.STATE_DIR / "next_day_context.json"
    handoff_path.write_text(json.dumps(handoff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"- 可用资金：{account['cash_available']:.2f} 元",
        f"- 冻结资金：{account['cash_frozen']:.2f} 元",
        f"- 已记录成交或撤单反馈：{len(trades)} 条",
        f"- 等待反馈委托：{len(pending)} 笔",
        f"- 当日分析回合：{len(runs)} 次",
        f"- 监控信号触发：{len(monitor_signals)} 次",
        f"- 候选池健康状态：{handoff['watchlist_health'].get('status', '未知')}",
        f"- 是否需要账户核对：{'是' if handoff['requires_reconciliation'] else '否'}",
        f"- 分析备注：{narrative or '无'}",
        "",
        "## 收盘时点完整分析",
        "",
        f"- 数据健康：{normalized_review['close_analysis']['data_health']}",
        f"- 大盘：{normalized_review['close_analysis']['market_analysis']}",
        f"- 板块：{normalized_review['close_analysis']['sector_analysis']}",
        f"- 持仓：{normalized_review['close_analysis']['holding_analysis']}",
        f"- 候选池：{normalized_review['close_analysis']['candidate_analysis']}",
        f"- 结论：{normalized_review['close_analysis']['conclusion']}",
        "",
        "## 当日记录复核",
        "",
        f"- 记录：{normalized_review['day_review']['record_summary']}",
        f"- 委托与反馈：{normalized_review['day_review']['orders_and_feedback']}",
        f"- 账户核对：{normalized_review['day_review']['account_reconciliation']}",
    ]
    for item in normalized_review["day_review"]["execution_deviations"]:
        lines.append(f"- 执行偏差：{item}")
    for item in normalized_review["day_review"]["lessons"]:
        lines.append(f"- 经验/纪律：{item}")
    outlook = normalized_review["next_day_outlook"]
    lines.extend([
        "", f"## 次日建议与预期：{outlook['trading_day']}", "",
        f"- 市场预期：{outlook['market_expectation']}",
        f"- 基准情景：{outlook['base_case']}",
        f"- 偏强情景：{outlook['bull_case']}",
        f"- 偏弱情景：{outlook['bear_case']}",
    ])
    for item in outlook["position_plan"] + outlook["candidate_plan"]:
        lines.extend([
            f"- {item['instruction_line']}",
            f"  - 触发：{item['trigger']}",
            f"  - 失效：{item['invalidation']}",
            f"  - 理由：{item['rationale']}",
            "  - 状态：条件预案，非委托；次日必须重新分析后才能登记订单。",
        ])
    for item in outlook["risk_points"]:
        lines.append(f"- 风险：{item}")
    for item in outlook["pre_market_checks"]:
        lines.append(f"- 盘前核验：{item}")
    for item in outlook["no_trade_conditions"]:
        lines.append(f"- 不交易条件：{item}")
    if runs:
        lines.extend(["", "## 分析回合索引", ""])
        for item in runs:
            payload = item["payload"]
            lines.append(f"- {Path(item['path']).name}：{payload.get('summary') or payload.get('conclusion') or '已留档'}")
    if monitor_signals:
        lines.extend(["", "## 监控触发索引", ""])
        for signal in monitor_signals:
            lines.append(f"- {signal.get('triggered_at', '')} {signal.get('code', '')} {signal.get('template_id', '')} = {signal.get('value')}")
    report_path = _write_report(state.JOURNAL_DIR / "daily" / f"{day}_summary.md", f"收盘日报：{day}", lines)
    artifact = {"run_id": run_id, "created_at": handoff["created_at"], "review": normalized_review}
    close_review_path = state.RECORDS_DIR / "close_reviews" / f"{day}.json"
    state._write_json(close_review_path, artifact)
    return {"daily_summary": report_path, "next_day_context": str(handoff_path), "close_review": str(close_review_path), "handoff": handoff}


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
