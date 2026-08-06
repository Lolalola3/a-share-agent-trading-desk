from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from . import state


LOGIC_PATH = state.PACKAGE_ROOT / "strategy" / "active_logic.json"


def load_logic() -> dict[str, Any]:
    if not LOGIC_PATH.exists():
        raise state.DeskError("交易逻辑文件不存在，不能生成交易判断。")
    return json.loads(LOGIC_PATH.read_text(encoding="utf-8"))


def format_trade_instruction(order: dict[str, Any]) -> str:
    """Return the only accepted five-field user-facing order sentence."""
    side = {"buy": "买", "sell": "卖"}.get(str(order.get("side")))
    if side is None:
        raise ValueError("交易方向必须是 buy 或 sell。")
    valid_from = str(order.get("valid_from", "")).strip()
    valid_until = str(order.get("valid_until", "")).strip()
    deadline = str(order.get("feedback_deadline", "")).strip()
    if not valid_from or not valid_until or not deadline:
        raise ValueError("交易时间与反馈等待时间不能为空。")
    code = str(order.get("code", "")).strip()
    name = str(order.get("name", "")).strip()
    price = float(order["limit_price"])
    shares = int(order.get("requested_shares", order.get("shares", 0)))
    return f"{valid_from}-{valid_until}，{code} {name}，{side} {price:.3f} 元，{shares} 股，等待反馈至 {deadline}"


def _floor_lot(shares: float, lot_size: int) -> int:
    return max(0, math.floor(float(shares) / lot_size) * lot_size)


def entry_check(snapshot: dict[str, Any], logic: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = (logic or load_logic())["entry"]
    checks = {
        "candidate_score": float(snapshot.get("candidate_score", 0)) >= cfg["candidate_score_min"],
        "technical_confirmation": bool(snapshot.get("technical_confirmation", False)) if cfg["technical_confirmation_required"] else True,
        "change_not_chasing": float(snapshot.get("change_pct", 999)) <= cfg["max_change_pct"],
        "position_in_band": cfg["price_vs_ma20_min"] <= float(snapshot.get("price_vs_ma20", 0)) <= cfg["price_vs_ma20_max"],
        "volume_confirmed": float(snapshot.get("volume_ratio", 0)) >= cfg["volume_ratio_min"],
        "sector_passed": bool(snapshot.get("sector_passed", False)),
        "not_limit_up": not bool(snapshot.get("at_limit_up", False)) if cfg["avoid_limit_up"] else True,
        "data_complete": bool(snapshot.get("data_complete", False)),
    }
    return {"allowed": all(checks.values()), "checks": checks}


def exit_levels(entry_price: float, logic: dict[str, Any] | None = None) -> dict[str, float]:
    cfg = (logic or load_logic())["exit"]
    entry = float(entry_price)
    return {
        "hard_stop": round(entry * (1 - cfg["hard_stop_loss_pct"]), 3),
        "take_profit_1": round(entry * (1 + cfg["take_profit_1_pct"]), 3),
        "take_profit_2": round(entry * (1 + cfg["take_profit_2_pct"]), 3),
        "trailing_activation": round(entry * (1 + cfg["trailing_activate_pct"]), 3),
    }


def exit_check(
    position: dict[str, Any],
    current_price: float,
    highest_close: float | None = None,
    logic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = (logic or load_logic())["exit"]
    entry = float(position["cost"])
    price = float(current_price)
    levels = exit_levels(entry, logic)
    highest = float(highest_close or price)
    trailing_stop = max(levels["hard_stop"], highest * (1 - cfg["trailing_stop_from_high_pct"]))
    if cfg["sell_only_sellable_shares"] and int(position.get("sellable_shares", 0)) <= 0:
        return {"action": "hold_t1", "reason": "触发退出条件但当前没有可卖股份。", "levels": levels, "trailing_stop": round(trailing_stop, 3)}
    if price <= levels["hard_stop"]:
        return {"action": "stop_loss", "reason": "价格触及固定止损线。", "levels": levels, "trailing_stop": round(trailing_stop, 3)}
    if price >= levels["take_profit_2"]:
        return {"action": "take_profit_2", "reason": "价格触及第二止盈线，退出剩余目标仓位。", "levels": levels, "trailing_stop": round(trailing_stop, 3)}
    if price >= levels["take_profit_1"]:
        return {"action": "take_profit_1", "reason": "价格触及第一止盈线，退出计划仓位的一半。", "levels": levels, "trailing_stop": round(trailing_stop, 3)}
    if price >= levels["trailing_activation"] and price <= trailing_stop:
        return {"action": "trailing_stop", "reason": "盈利后从最高收盘价回撤达到移动止盈线。", "levels": levels, "trailing_stop": round(trailing_stop, 3)}
    return {"action": "hold", "reason": "尚未触发固定止损、止盈或移动止盈。", "levels": levels, "trailing_stop": round(trailing_stop, 3)}


def t_low_buy_check(
    position: dict[str, Any],
    current_price: float,
    support_price: float,
    rebound_confirmed: bool,
    logic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = (logic or load_logic())["t_trade"]
    shares = int(position.get("shares", 0))
    sellable = int(position.get("sellable_shares", 0))
    support = float(support_price)
    price = float(current_price)
    max_t_shares = _floor_lot(shares * cfg["max_position_fraction"], 100)
    checks = {
        "enabled": bool(cfg["enabled"]),
        "has_sellable_old_shares": sellable >= max_t_shares and max_t_shares > 0,
        "near_support": support * (1 - cfg["max_support_break_pct"]) <= price <= support * (1 + cfg["support_band_pct"]),
        "rebound_confirmed": bool(rebound_confirmed) if cfg["rebound_confirmation_required"] else True,
    }
    return {"allowed": all(checks.values()), "buy_shares": max_t_shares if all(checks.values()) else 0, "checks": checks}


def t_high_sell_check(buy_price: float, current_price: float, buy_shares: int, logic: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = (logic or load_logic())["t_trade"]
    target = float(buy_price) * (1 + cfg["min_expected_profit_pct"])
    allowed = int(buy_shares) > 0 and float(current_price) >= target
    return {
        "allowed": allowed,
        "sell_shares": int(buy_shares) if allowed else 0,
        "target_price": round(target, 3),
        "reason": "达到低买后的最低 2% T 利润目标。" if allowed else "尚未达到低买后的最低 2% T 利润目标。",
    }


def t_sell_first_check(
    position: dict[str, Any],
    current_price: float,
    resistance_price: float,
    planned_shares: int,
    logic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check the sell-high then buy-low T sequence."""
    cfg = (logic or load_logic())["t_trade"]
    shares = int(position.get("shares", 0))
    sellable = int(position.get("sellable_shares", 0))
    max_t_shares = _floor_lot(shares * cfg["max_position_fraction"], 100)
    requested = _floor_lot(planned_shares, 100)
    allowed_shares = min(max_t_shares, requested)
    checks = {
        "enabled": bool(cfg["enabled"]),
        "has_sellable_old_shares": sellable >= allowed_shares and allowed_shares > 0,
        "at_resistance": float(current_price) >= float(resistance_price),
    }
    allowed = all(checks.values())
    return {
        "allowed": allowed,
        "sell_shares": allowed_shares if allowed else 0,
        "max_rebuy_shares": allowed_shares if allowed else 0,
        "checks": checks,
        "reason": "先卖出可卖老仓，回落后最多买回同等数量。" if allowed else "未满足先高卖后低买 T 的硬性条件。",
    }


def position_size_by_risk(equity: float, entry_price: float, logic: dict[str, Any] | None = None) -> int:
    cfg = (logic or load_logic())
    stop = float(entry_price) * (1 - cfg["exit"]["hard_stop_loss_pct"])
    risk_budget = float(equity) * cfg["sizing"]["risk_budget_per_trade_pct"]
    risk_per_share = max(0.01, float(entry_price) - stop)
    by_risk = _floor_lot(risk_budget / risk_per_share, cfg["sizing"]["lot_size"])
    by_weight = _floor_lot(float(equity) * cfg["sizing"]["max_position_weight"] / float(entry_price), cfg["sizing"]["lot_size"])
    return min(by_risk, by_weight)


def update_gate(completed_trades: int, out_of_sample_ok: bool, drawdown_worse_fraction: float, logic: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = (logic or load_logic())["evolution"]
    checks = {
        "minimum_sample": int(completed_trades) >= cfg["min_completed_trades_before_activation"],
        "out_of_sample": bool(out_of_sample_ok) if cfg["require_out_of_sample_check"] else True,
        "drawdown_not_worse": float(drawdown_worse_fraction) <= cfg["require_max_drawdown_not_worse_pct"],
    }
    return {"allowed": all(checks.values()), "checks": checks, "activate_next_trading_day": bool(cfg["activate_new_version_next_trading_day"])}


def record_review(
    as_of: str,
    scope: str,
    completed_trades: int,
    out_of_sample_ok: bool,
    drawdown_worse_fraction: float,
    observations: list[str],
    proposed_changes: list[str],
    logic: dict[str, Any] | None = None,
) -> Path:
    """Persist a review proposal without mutating the active rule set."""
    cfg = logic or load_logic()
    gate = update_gate(completed_trades, out_of_sample_ok, drawdown_worse_fraction, cfg)
    safe_scope = "".join(char if char.isalnum() or char in "-_" else "_" for char in scope) or "review"
    path = state.ROOT / "strategy" / "reviews" / f"{as_of}_{safe_scope}.json"
    payload = {
        "as_of": as_of,
        "scope": scope,
        "active_version": cfg["version"],
        "observations": list(observations),
        "proposed_changes": list(proposed_changes),
        "gate": gate,
        "status": "eligible_for_next_day_activation" if gate["allowed"] else "recorded_only",
        "rule": "复盘不修改当前盘中规则；只有通过门槛的候选版本才允许下一交易日激活。",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
