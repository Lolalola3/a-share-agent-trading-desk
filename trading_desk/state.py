from __future__ import annotations

import json
import os
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("A_SHARE_DESK_HOME", str(PACKAGE_ROOT))).expanduser().resolve()
STATE_DIR = ROOT / "state"
RECORDS_DIR = ROOT / "records"
JOURNAL_DIR = ROOT / "journal"
ACCOUNT_PATH = STATE_DIR / "account.json"
WATCHLIST_PATH = STATE_DIR / "watchlist.json"
SETTINGS_PATH = STATE_DIR / "settings.json"
SESSION_SCHEDULE = ["09:08", "09:22", "10:30", "11:25", "13:00", "14:25", "14:50", "15:05"]


class DeskError(ValueError):
    pass


def shanghai_now() -> datetime:
    return datetime.now().astimezone().replace(tzinfo=None)


def today_str() -> str:
    return date.today().isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path, fallback: Any = None) -> Any:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def default_settings() -> dict[str, Any]:
    return {
        "market_scope": "仅沪深主板",
        "style": "短线波段为主，长期趋势辅助",
        "output_language": "简体中文",
        "auto_trade": False,
        "chase_limit_up": False,
        "instruction_delay_minutes": 5,
        "feedback_window_minutes": 5,
        "watchlist_limit": 5,
        "core_watchlist_limit": 5,
        "event_watchlist_limit": 0,
        "t_trade_max_fraction": 0.30,
        "replacement_score_gap": 15,
        "allowed_code_prefixes": ["600", "601", "603", "605", "000", "001", "002", "003"],
        "session_schedule": SESSION_SCHEDULE,
        "mandatory_context_days": 5,
        "prompt_workflow_version": "2.1.1",
        "candidate_pool": {
            "frequency": "每周日 17:00，必要时盘后应急重筛",
            "sector_count": 5,
            "max_stocks_per_sector": 10,
            "max_candidates": 5,
            "one_stock_per_sector": True,
            "minimum_score": 65,
            "emergency_refresh_cooldown_days": 2,
            "replacement_score_gap": 15,
            "health_triggers": {
                "invalid_candidates": 3,
                "three_day_relative_underperformance_pct": -3,
                "below_ma20_candidates": 4,
                "sector_breadth_floor": 0.45,
                "no_entry_setup_trading_days": 3
            },
            "score_weights": {
                "technical": 30,
                "relative_strength": 20,
                "flow_liquidity": 15,
                "sector": 15,
                "fundamental_event": 10,
                "risk_reward": 10
            }
        },
    }


def initialize(initial_date: str, available_cash: float, positions: list[dict[str, Any]]) -> dict[str, Any]:
    if ACCOUNT_PATH.exists():
        raise DeskError("账户已经初始化，请使用账户核对，不要重复初始化。")
    if available_cash < 0:
        raise DeskError("可用资金不能为负数。")
    normalized_positions = []
    for position in positions:
        shares = int(position["shares"])
        if shares <= 0 or shares % 100:
            raise DeskError(f"{position['code']}：股数必须是正的 100 股整数倍。")
        normalized_positions.append({
            "code": str(position["code"]),
            "name": str(position["name"]),
            "shares": shares,
            "sellable_shares": 0,
            "today_bought_shares": shares,
            "cost": round(float(position["cost"]), 4),
            "opened_on": initial_date,
        })
    account = {
        "schema_version": 1,
        "as_of": initial_date,
        "updated_at": shanghai_now().isoformat(timespec="seconds"),
        "cash_available": round(float(available_cash), 2),
        "cash_frozen": 0.0,
        "positions": normalized_positions,
        "pending_orders": [],
        "reconciliation_status": "pending_close_check",
        "notes": ["初始持仓均为今日买入，下一交易日前不可卖出。"],
    }
    watchlist = {
        "schema_version": 2,
        "as_of": initial_date,
        "generated_at": None,
        "valid_until": None,
        "status": "empty",
        "health": {"status": "empty", "updated_at": None, "reasons": []},
        "metadata": {},
        "candidates": [],
        "history": [],
        "health_history": [],
    }
    _write_json(ACCOUNT_PATH, account)
    _write_json(WATCHLIST_PATH, watchlist)
    _write_json(SETTINGS_PATH, default_settings())
    append_daily(initial_date, "account_initialized", {
        "available_cash": account["cash_available"],
        "positions": normalized_positions,
    })
    return account


def get_account() -> dict[str, Any]:
    account = _read_json(ACCOUNT_PATH)
    if account is None:
        raise DeskError("账户尚未初始化。")
    return account


def save_account(account: dict[str, Any]) -> None:
    account["updated_at"] = shanghai_now().isoformat(timespec="seconds")
    _write_json(ACCOUNT_PATH, account)


def get_settings() -> dict[str, Any]:
    return _read_json(SETTINGS_PATH, default_settings())


def get_watchlist() -> dict[str, Any]:
    return _read_json(WATCHLIST_PATH, {
        "schema_version": 2,
        "as_of": today_str(),
        "generated_at": None,
        "valid_until": None,
        "status": "empty",
        "health": {"status": "empty", "updated_at": None, "reasons": []},
        "metadata": {},
        "candidates": [],
        "history": [],
        "health_history": [],
    })


def get_task_session(day: str) -> dict[str, Any]:
    path = STATE_DIR / "task_sessions" / f"{day}.json"
    session = _read_json(path)
    if session is None:
        return {"day": day, "status": "missing", "thread_id": None, "host_id": None}
    return session


def claim_dispatch_node(day: str, node: str, current_time: datetime | None = None) -> dict[str, Any]:
    """Atomically allow only the most recent due node to dispatch for a trading day."""
    schedule = list(get_settings().get("session_schedule", SESSION_SCHEDULE))
    if node not in schedule:
        raise DeskError(f"未知调度节点：{node}")

    now = current_time or shanghai_now()
    now_day = now.date().isoformat()
    now_hm = now.strftime("%H:%M")
    due_nodes = [item for item in schedule if item <= now_hm]
    effective_node = due_nodes[-1] if due_nodes else None
    next_node = next((item for item in schedule if item > now_hm), None)

    base = {
        "day": day,
        "requested_node": node,
        "current_time": now.isoformat(timespec="seconds"),
        "effective_node": effective_node,
        "next_node": next_node,
    }
    if day != now_day:
        return {**base, "action": "skip", "reason": "调度日期不是北京时间今天，禁止补发。"}
    if effective_node is None:
        return {**base, "action": "skip", "reason": "今日首个节点尚未到时。"}
    if node != effective_node:
        relation = "过期" if node < effective_node else "尚未到时"
        return {**base, "action": "skip", "reason": f"请求节点{relation}；当前只允许{effective_node}节点。"}

    claim_dir = STATE_DIR / "dispatch_claims"
    claim_path = claim_dir / f"{day}.json"
    lock_path = claim_dir / f"{day}.lock"
    claim_dir.mkdir(parents=True, exist_ok=True)
    lock_fd: int | None = None
    deadline = time.monotonic() + 2.0
    while lock_fd is None:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                return {**base, "action": "skip", "reason": "节点认领锁繁忙，关闭式退出；下一节点可独立重试。"}
            time.sleep(0.05)

    try:
        state = _read_json(claim_path, {"day": day, "claims": {}})
        claims = dict(state.get("claims", {}))
        if node in claims:
            return {
                **base,
                "action": "skip",
                "reason": f"{node}节点今日已认领，禁止重复投递。",
                "existing_claim": claims[node],
            }
        claimed_at = now.isoformat(timespec="seconds")
        claims[node] = {"claimed_at": claimed_at, "status": "claimed"}
        _write_json(claim_path, {
            "day": day,
            "latest_claimed_node": node,
            "updated_at": claimed_at,
            "claims": claims,
        })
        return {**base, "action": "execute", "reason": f"{node}是截至当前最近的到期节点，认领成功。", "claimed_at": claimed_at}
    finally:
        os.close(lock_fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def register_task_session(
    day: str,
    thread_id: str,
    host_id: str = "local",
    title: str = "",
    source: str = "scheduled_node",
    replace: bool = False,
) -> dict[str, Any]:
    if not day or not thread_id:
        raise DeskError("交易日期和任务线程 ID 不能为空。")
    path = STATE_DIR / "task_sessions" / f"{day}.json"
    existing = _read_json(path)
    if existing and existing.get("thread_id") != thread_id and not replace:
        raise DeskError(f"{day} 已登记其他交易线程，禁止重复创建。")
    history = list(existing.get("history", [])) if existing else []
    if existing and existing.get("thread_id") != thread_id:
        history.append({
            "thread_id": existing.get("thread_id"),
            "host_id": existing.get("host_id"),
            "status": existing.get("status"),
            "replaced_at": shanghai_now().isoformat(timespec="seconds"),
        })
    same_thread = existing and existing.get("thread_id") == thread_id
    session = {
        "day": day,
        "thread_id": thread_id,
        "host_id": host_id,
        "title": title or f"A股交易台 {day}",
        "status": "active",
        "source": source,
        "registered_at": existing.get("registered_at") if same_thread else shanghai_now().isoformat(timespec="seconds"),
        "updated_at": shanghai_now().isoformat(timespec="seconds"),
        "history": history,
    }
    _write_json(path, session)
    return session


def _find_position(account: dict[str, Any], code: str) -> dict[str, Any] | None:
    return next((item for item in account["positions"] if item["code"] == code), None)


def _find_order(account: dict[str, Any], order_id: str) -> dict[str, Any]:
    order = next((item for item in account["pending_orders"] if item["id"] == order_id), None)
    if not order:
        raise DeskError(f"Unknown pending order: {order_id}")
    return order


def create_order_intent(
    code: str,
    name: str,
    side: str,
    limit_price: float,
    shares: int,
    valid_from: str,
    valid_until: str,
    reason: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    account = get_account()
    settings = get_settings()
    if side not in {"buy", "sell"}:
        raise DeskError("交易方向必须是买入或卖出。")
    if not any(code.startswith(prefix) for prefix in settings["allowed_code_prefixes"]):
        raise DeskError(f"{code} 不在当前沪深主板范围内。")
    if shares <= 0 or shares % 100:
        raise DeskError("股数必须是正的 100 股整数倍。")
    if limit_price <= 0:
        raise DeskError("限价必须大于 0。")
    order_id = uuid.uuid4().hex[:12]
    order = {
        "id": order_id,
        "code": code,
        "name": name,
        "side": side,
        "limit_price": round(float(limit_price), 3),
        "requested_shares": int(shares),
        "filled_shares": 0,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "reason": reason,
        "run_id": run_id,
        "status": "pending_feedback",
        "created_at": shanghai_now().isoformat(timespec="seconds"),
    }
    # Attach the active numeric risk plan to every intent for auditability.
    from . import trading_logic
    position_for_risk = _find_position(account, code)
    risk_entry = float(position_for_risk["cost"]) if side == "sell" and position_for_risk else limit_price
    order["strategy_version"] = trading_logic.load_logic()["version"]
    order["risk_levels"] = trading_logic.exit_levels(risk_entry)
    if side == "buy":
        reservation = round(limit_price * shares * 1.001, 2)
        if account["cash_available"] + 0.005 < reservation:
            raise DeskError(f"可用资金不足，需要预留 {reservation:.2f} 元。")
        account["cash_available"] = round(account["cash_available"] - reservation, 2)
        account["cash_frozen"] = round(account["cash_frozen"] + reservation, 2)
        order["reserved_cash"] = reservation
        order["reserved_shares"] = 0
    else:
        position = _find_position(account, code)
        if not position or position["sellable_shares"] < shares:
            raise DeskError(f"{code} 可卖股数不足，T+1 持仓今日不能卖出。")
        position["sellable_shares"] -= shares
        order["reserved_cash"] = 0
        order["reserved_shares"] = shares
    account["pending_orders"].append(order)
    save_account(account)
    append_daily(account["as_of"], "order_intent", order)
    return order


def record_trade_feedback(
    order_id: str,
    status: str,
    filled_shares: int = 0,
    fill_price: float | None = None,
    fees: float = 0.0,
    feedback_at: str | None = None,
) -> dict[str, Any]:
    if status not in {"filled", "partial", "cancelled"}:
        raise DeskError("反馈状态必须是已成交、部分成交或已撤单对应的内部状态。")
    account = get_account()
    order = _find_order(account, order_id)
    if order["status"] != "pending_feedback":
        raise DeskError("这笔委托已经记录过反馈。")
    requested = order["requested_shares"]
    filled_shares = int(filled_shares)
    if filled_shares < 0 or filled_shares > requested or filled_shares % 100:
        raise DeskError("成交股数必须在委托数量以内，并且是 100 股整数倍。")
    if status == "filled" and filled_shares != requested:
        raise DeskError("标记为全部成交时，成交股数必须等于委托股数。")
    if status == "cancelled" and filled_shares:
        raise DeskError("标记为撤单时，成交股数必须为 0。")
    if filled_shares and (fill_price is None or fill_price <= 0):
        raise DeskError("有成交股数时必须提供大于 0 的成交价。")
    fees = round(float(fees), 2)
    fill_price = float(fill_price or 0)
    unfilled = requested - filled_shares
    if order["side"] == "buy":
        reserved = float(order["reserved_cash"])
        actual = round(filled_shares * fill_price + fees, 2)
        account["cash_frozen"] = round(account["cash_frozen"] - reserved, 2)
        account["cash_available"] = round(account["cash_available"] + reserved - actual, 2)
        if account["cash_available"] < -0.01:
            raise DeskError("实际买入金额超过预留金额，请先进行账户核对。")
        if filled_shares:
            position = _find_position(account, order["code"])
            if position:
                old_value = position["shares"] * position["cost"]
                position["shares"] += filled_shares
                position["today_bought_shares"] += filled_shares
                position["cost"] = round((old_value + actual) / position["shares"], 4)
            else:
                account["positions"].append({
                    "code": order["code"], "name": order["name"], "shares": filled_shares,
                    "sellable_shares": 0, "today_bought_shares": filled_shares,
                    "cost": round(actual / filled_shares, 4), "opened_on": account["as_of"],
                })
    else:
        position = _find_position(account, order["code"])
        if not position:
            raise DeskError("卖出反馈前持仓已经不存在，请进行账户核对。")
        position["sellable_shares"] += unfilled
        if filled_shares:
            position["shares"] -= filled_shares
            account["cash_available"] = round(account["cash_available"] + filled_shares * fill_price - fees, 2)
        if position["shares"] == 0:
            account["positions"].remove(position)
    order.update({
        "status": status,
        "filled_shares": filled_shares,
        "fill_price": round(fill_price, 3) if filled_shares else None,
        "fees": fees,
        "feedback_at": feedback_at or shanghai_now().isoformat(timespec="seconds"),
    })
    account["pending_orders"].remove(order)
    save_account(account)
    _append_trade(account["as_of"], order)
    append_daily(account["as_of"], "trade_feedback", order)
    return {"order": order, "account": account}


def reconcile_account(
    as_of: str,
    available_cash: float,
    positions: list[dict[str, Any]],
    note: str,
) -> dict[str, Any]:
    account = get_account()
    if account["pending_orders"] and any(item["status"] == "pending_feedback" for item in account["pending_orders"]):
        raise DeskError("账户核对前必须先处理所有未反馈委托。")
    normalized = []
    for position in positions:
        shares = int(position["shares"])
        sellable = int(position.get("sellable_shares", shares))
        today_bought = int(position.get("today_bought_shares", max(shares - sellable, 0)))
        normalized.append({
            "code": str(position["code"]), "name": str(position["name"]), "shares": shares,
            "sellable_shares": sellable, "today_bought_shares": today_bought,
            "cost": round(float(position["cost"]), 4), "opened_on": str(position.get("opened_on", as_of)),
        })
    account.update({
        "as_of": as_of, "cash_available": round(float(available_cash), 2), "cash_frozen": 0.0,
        "positions": normalized, "reconciliation_status": "reconciled",
    })
    save_account(account)
    append_daily(as_of, "account_reconciled", {"note": note, "account": account})
    return account


def rollover(as_of: str) -> dict[str, Any]:
    account = get_account()
    if any(item["status"] == "pending_feedback" for item in account["pending_orders"]):
        raise DeskError("存在未反馈委托，不能进行交易日滚动。")
    for position in account["positions"]:
        position["sellable_shares"] = position["shares"]
        position["today_bought_shares"] = 0
    account["as_of"] = as_of
    account["reconciliation_status"] = "pending_close_check"
    save_account(account)
    append_daily(as_of, "day_rollover", {"positions": account["positions"]})
    return account


SCORE_LIMITS = {
    "technical": 30,
    "relative_strength": 20,
    "flow_liquidity": 15,
    "sector": 15,
    "fundamental_event": 10,
    "risk_reward": 10,
}


def _is_mainboard_code(code: str) -> bool:
    return any(code.startswith(prefix) for prefix in get_settings()["allowed_code_prefixes"])


def _next_sunday(day: str) -> str:
    value = date.fromisoformat(day)
    days_ahead = (6 - value.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (value + timedelta(days=days_ahead)).isoformat()


def update_watchlist(
    candidates: list[dict[str, Any]],
    rationale: str,
    as_of: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    pool_settings = settings["candidate_pool"]
    if len(candidates) > pool_settings["max_candidates"]:
        raise DeskError("候选池超过设定数量上限。")
    if not candidates:
        raise DeskError("新的候选池不能为空；筛选失败时必须保留上一期有效候选池。")
    codes = [str(item["code"]) for item in candidates]
    if len(codes) != len(set(codes)):
        raise DeskError("候选池中存在重复股票。")
    holding_codes = {str(item["code"]) for item in get_account().get("positions", [])}
    duplicate_holdings = holding_codes.intersection(codes)
    if duplicate_holdings:
        raise DeskError(f"候选池不能包含当前持仓：{', '.join(sorted(duplicate_holdings))}")
    core = sum(1 for item in candidates if item.get("bucket") == "core")
    event = sum(1 for item in candidates if item.get("bucket") == "event")
    if event or core > settings["core_watchlist_limit"]:
        raise DeskError("候选池只允许同一套规则筛出的核心观察股，且最多 5 只。")
    normalized = []
    sectors: set[str] = set()
    for item in candidates:
        if item.get("bucket") != "core":
            raise DeskError("候选池中的股票必须来自统一筛选规则，不能使用事件或其他分类。")
        code = str(item["code"])
        if not _is_mainboard_code(code):
            raise DeskError(f"{code} 不属于允许的沪深主板范围。")
        score = round(float(item["score"]), 2)
        if not pool_settings["minimum_score"] <= score <= 100:
            raise DeskError(f"候选股评分必须在 {pool_settings['minimum_score']} 到 100 之间。")
        sector = str(item.get("sector", "")).strip()
        if not sector:
            raise DeskError(f"{code} 缺少所属入选板块。")
        if pool_settings["one_stock_per_sector"] and sector in sectors:
            raise DeskError(f"板块 {sector} 已有候选股，每个板块最多一只。")
        sectors.add(sector)
        breakdown = item.get("score_breakdown", {})
        if set(breakdown) != set(SCORE_LIMITS):
            raise DeskError(f"{code} 的评分分解字段不完整。")
        normalized_breakdown = {}
        for key, limit in SCORE_LIMITS.items():
            value = round(float(breakdown[key]), 2)
            if not 0 <= value <= limit:
                raise DeskError(f"{code} 的 {key} 评分超出 0-{limit}。")
            normalized_breakdown[key] = value
        if abs(sum(normalized_breakdown.values()) - score) > 0.11:
            raise DeskError(f"{code} 的总分与分项得分不一致。")
        sources = item.get("sources", [])
        if not sources or any(not source.get("name") or not source.get("captured_at") for source in sources):
            raise DeskError(f"{code} 缺少可审计的数据来源和采集时间。")
        if not bool(item.get("technical_confirmation", False)):
            raise DeskError(f"{code} 未通过技术确认，不能进入正式候选池。")
        normalized.append({
            "code": code, "name": str(item["name"]), "sector": sector, "bucket": item["bucket"],
            "score": score, "admitted_on": item.get("admitted_on", as_of or today_str()),
            "data_as_of": str(item.get("data_as_of", as_of or today_str())),
            "score_breakdown": normalized_breakdown,
            "technical_confirmation": True,
            "reference_price": item.get("reference_price"),
            "support_price": item.get("support_price"),
            "resistance_price": item.get("resistance_price"),
            "invalidation_price": item.get("invalidation_price"),
            "target_price": item.get("target_price"),
            "risk_reward_ratio": item.get("risk_reward_ratio"),
            "catalyst": str(item.get("catalyst", "")), "risk": str(item.get("risk", "")),
            "invalidation": str(item.get("invalidation", "")),
            "sources": sources,
            "replacement_eligible": bool(item.get("replacement_eligible", False)),
            "status": "active",
        })
    watchlist = get_watchlist()
    generated_at = shanghai_now().isoformat(timespec="seconds")
    effective_day = as_of or today_str()
    watchlist.setdefault("history", []).append({
        "at": generated_at,
        "rationale": rationale,
        "as_of": watchlist.get("as_of"),
        "candidates": watchlist.get("candidates", []),
    })
    watchlist.update({
        "schema_version": 2,
        "as_of": effective_day,
        "generated_at": generated_at,
        "valid_until": (metadata or {}).get("valid_until", _next_sunday(effective_day)),
        "status": "active",
        "health": {"status": "normal", "updated_at": generated_at, "reasons": []},
        "metadata": metadata or {},
    })
    watchlist["candidates"] = normalized
    _write_json(WATCHLIST_PATH, watchlist)
    append_daily(watchlist["as_of"], "watchlist_updated", {"rationale": rationale, "candidates": normalized})
    return watchlist


def record_watchlist_health(
    status: str,
    reasons: list[str],
    metrics: dict[str, Any],
    action: str,
    as_of: str | None = None,
) -> dict[str, Any]:
    allowed = {"normal", "watch", "frozen", "invalidated"}
    if status not in allowed:
        raise DeskError(f"候选池健康状态必须是：{', '.join(sorted(allowed))}。")
    watchlist = get_watchlist()
    recorded_at = shanghai_now().isoformat(timespec="seconds")
    health = {
        "status": status,
        "updated_at": recorded_at,
        "as_of": as_of or today_str(),
        "reasons": list(reasons),
        "metrics": metrics,
        "action": action,
    }
    watchlist["health"] = health
    watchlist.setdefault("health_history", []).append(health)
    _write_json(WATCHLIST_PATH, watchlist)
    append_daily(health["as_of"], "watchlist_health", health)
    return watchlist


def append_daily(day: str, event_type: str, payload: dict[str, Any]) -> Path:
    path = JOURNAL_DIR / "daily" / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# A 股交易台日报：{day}\n\n", encoding="utf-8")
    labels = {
        "account_initialized": "账户初始化",
        "order_intent": "委托指令登记",
        "trade_feedback": "成交反馈",
        "account_reconciled": "账户核对",
        "day_rollover": "交易日滚动",
        "watchlist_updated": "候选池更新",
        "watchlist_health": "候选池健康检查",
        "market_run": "盘面分析",
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"## {shanghai_now().strftime('%H:%M:%S')} | {labels.get(event_type, event_type)}\n\n")
        handle.write("```json\n")
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        handle.write("\n```\n\n")
    return path


def _append_trade(day: str, payload: dict[str, Any]) -> None:
    path = RECORDS_DIR / "trades" / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def record_run(day: str, run_id: str, payload: dict[str, Any]) -> Path:
    timestamp = shanghai_now().strftime("%H%M%S")
    path = RECORDS_DIR / "runs" / day / f"{timestamp}_{run_id}.json"
    _write_json(path, payload)
    append_daily(day, "market_run", {"run_id": run_id, "path": str(path), "summary": payload.get("summary", "")})
    return path


def context_pack(day: str | None = None) -> dict[str, Any]:
    account = get_account()
    settings = get_settings()
    watchlist = get_watchlist()
    current_day = day or account["as_of"]
    daily_files = sorted((JOURNAL_DIR / "daily").glob("*.md"), reverse=True)[:settings["mandatory_context_days"]]
    return {
        "as_of": current_day,
        "account": account,
        "settings": settings,
        "watchlist": watchlist,
        "task_session": get_task_session(current_day),
        "candidate_pool_policy": {
            "frequency": settings["candidate_pool"]["frequency"],
            "current_status": watchlist.get("status", "empty"),
            "health": watchlist.get("health", {}),
            "valid_until": watchlist.get("valid_until"),
            "rule": "周日生成基础候选池；工作日只分析持仓和候选。池失效时先冻结新买入，盘后应急重筛。",
        },
        "mandatory_daily_files": [str(path) for path in daily_files],
        "instruction_policy": {
            "analysis_start": "T 时刻开始分析",
            "last_refresh": "T+4 分钟最后刷新行情",
            "execution_window": "T+5 至 T+10 分钟执行",
            "no_feedback": "没有反馈时继续锁定资金或股份，后续分析不得重复使用。",
        },
    }
