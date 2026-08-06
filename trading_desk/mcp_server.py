"""Dependency-free MCP stdio server for the local trading-desk tools."""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable

from . import reports, state, trading_logic


if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


TOOLS = [
    {"name": "account_get", "description": "读取账户主档案，包括资金、可卖股份和未反馈委托锁定。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "context_get", "description": "读取本次盘面分析必须使用的固定上下文。", "inputSchema": {"type": "object", "properties": {"day": {"type": "string"}}}},
    {"name": "node_packet_get", "description": "由确定性程序拉取持仓与候选池的节点数据包，避免无人值守任务通过 Shell 触发审批。腾讯合规时立即采用，仅失败时使用备用源。", "inputSchema": {"type": "object", "required": ["node"], "properties": {"node": {"type": "string", "enum": ["09:08", "09:22", "10:30", "11:25", "13:00", "14:25", "14:50", "15:05"]}, "include_intraday": {"type": "boolean"}, "persist": {"type": "boolean"}}}},
    {"name": "dispatch_node_claim", "description": "后台调度任务的第一道时间门禁。按北京时间原子认领截至当前最近的到期节点；过期、未来或重复节点返回skip且不得创建任务。", "inputSchema": {"type": "object", "required": ["day", "node"], "properties": {"day": {"type": "string"}, "node": {"type": "string", "enum": ["09:08", "09:22", "10:30", "11:25", "13:00", "14:25", "14:50", "15:05"]}}}},
    {"name": "task_session_get", "description": "读取指定交易日唯一交易线程的本地登记；用于各独立定时节点复用同一线程。", "inputSchema": {"type": "object", "required": ["day"], "properties": {"day": {"type": "string"}}}},
    {"name": "task_session_register", "description": "登记指定交易日的唯一交易线程。默认拒绝覆盖其他线程；仅确认旧线程失效后才可 replace。", "inputSchema": {"type": "object", "required": ["day", "thread_id"], "properties": {"day": {"type": "string"}, "thread_id": {"type": "string"}, "host_id": {"type": "string"}, "title": {"type": "string"}, "source": {"type": "string"}, "replace": {"type": "boolean"}}}},
    {"name": "order_intent_create", "description": "登记一笔精确的、不会自动提交的限价买卖指令，并锁定资金或可卖股份。", "inputSchema": {"type": "object", "required": ["code", "name", "side", "limit_price", "shares", "valid_from", "valid_until", "reason"], "properties": {"code": {"type": "string"}, "name": {"type": "string"}, "side": {"type": "string", "enum": ["buy", "sell"]}, "limit_price": {"type": "number"}, "shares": {"type": "integer"}, "valid_from": {"type": "string"}, "valid_until": {"type": "string"}, "reason": {"type": "string"}, "run_id": {"type": "string"}}}},
    {"name": "trade_feedback_record", "description": "记录用户实际成交、部分成交或撤单，并更新资金和 T+1 可卖数量。", "inputSchema": {"type": "object", "required": ["order_id", "status"], "properties": {"order_id": {"type": "string"}, "status": {"type": "string", "enum": ["filled", "partial", "cancelled"]}, "filled_shares": {"type": "integer"}, "fill_price": {"type": "number"}, "fees": {"type": "number"}}}},
    {"name": "account_reconcile", "description": "所有委托反馈完成后，按用户提供的券商数据进行盘后账户核对。", "inputSchema": {"type": "object", "required": ["as_of", "available_cash", "positions", "note"], "properties": {"as_of": {"type": "string"}, "available_cash": {"type": "number"}, "positions": {"type": "array"}, "note": {"type": "string"}}}},
    {"name": "trading_day_rollover", "description": "新交易日开始时，在确认没有未反馈委托后，将前一交易日买入的股份转为可卖。", "inputSchema": {"type": "object", "required": ["as_of"], "properties": {"as_of": {"type": "string"}}}},
    {"name": "candidate_pool_get", "description": "读取当前周候选池、评分分解、来源、有效期和健康状态。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "candidate_pool_update", "description": "保存由大模型按周筛 Prompt 完成并通过结构校验的最多五只候选股；失败时不覆盖旧池。", "inputSchema": {"type": "object", "required": ["candidates", "rationale"], "properties": {"candidates": {"type": "array", "minItems": 1, "maxItems": 5}, "rationale": {"type": "string"}, "as_of": {"type": "string"}, "metadata": {"type": "object"}}}},
    {"name": "candidate_pool_health_record", "description": "记录工作日候选池健康状态及是否冻结新买入或触发盘后应急重筛。", "inputSchema": {"type": "object", "required": ["status", "reasons", "metrics", "action"], "properties": {"status": {"type": "string", "enum": ["normal", "watch", "frozen", "invalidated"]}, "reasons": {"type": "array", "items": {"type": "string"}}, "metrics": {"type": "object"}, "action": {"type": "string"}, "as_of": {"type": "string"}}}},
    {"name": "analysis_run_record", "description": "保存一次定时分析的证据、结论和中文用户可见文本。", "inputSchema": {"type": "object", "required": ["day", "run_id", "payload"], "properties": {"day": {"type": "string"}, "run_id": {"type": "string"}, "payload": {"type": "object"}}}},
    {"name": "node_delivery_prepare", "description": "在异步投递前创建节点执行记录；投递与分析状态均为 pending。", "inputSchema": {"type": "object", "required": ["day", "node", "target_thread_id"], "properties": {"day": {"type": "string"}, "node": {"type": "string", "enum": ["09:08", "09:22", "10:30", "11:25", "13:00", "14:25", "14:50", "15:05"]}, "target_thread_id": {"type": "string"}, "target_host_id": {"type": "string"}, "source_thread_id": {"type": "string"}}}},
    {"name": "node_delivery_confirm", "description": "异步任务载体创建成功后，仅确认投递成功，不代表分析已完成。", "inputSchema": {"type": "object", "required": ["day", "node", "delivery_id", "transport_id"], "properties": {"day": {"type": "string"}, "node": {"type": "string"}, "delivery_id": {"type": "string"}, "transport_id": {"type": "string"}}}},
    {"name": "node_delivery_fail", "description": "确认投递无法完成时登记 failed，避免节点永久停留在 pending。", "inputSchema": {"type": "object", "required": ["day", "node", "delivery_id", "reason"], "properties": {"day": {"type": "string"}, "node": {"type": "string"}, "delivery_id": {"type": "string"}, "reason": {"type": "string"}}}},
    {"name": "node_analysis_complete", "description": "分析任务完成留档后，独立登记 completed 或 failed 状态。", "inputSchema": {"type": "object", "required": ["day", "node", "delivery_id", "run_id"], "properties": {"day": {"type": "string"}, "node": {"type": "string"}, "delivery_id": {"type": "string"}, "run_id": {"type": "string"}, "status": {"type": "string", "enum": ["completed", "failed"]}, "summary": {"type": "string"}}}},
    {"name": "node_execution_status_get", "description": "分别读取指定节点的投递状态和分析状态。", "inputSchema": {"type": "object", "required": ["day", "node"], "properties": {"day": {"type": "string"}, "node": {"type": "string"}}}},
    {"name": "reports_close_day", "description": "收盘后生成日报交接包、周报和月报。", "inputSchema": {"type": "object", "required": ["day"], "properties": {"day": {"type": "string"}, "note": {"type": "string"}}}},
    {"name": "strategy_logic_get", "description": "读取当前版本的数字化交易逻辑，包括止损止盈、入场与 T 的硬性规则。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "strategy_entry_check", "description": "按照当前固定规则检查候选股是否允许入场，不直接下单。", "inputSchema": {"type": "object", "required": ["snapshot"], "properties": {"snapshot": {"type": "object"}}}},
    {"name": "strategy_exit_check", "description": "根据成本、现价和最高收盘价检查固定止损、止盈和移动止盈触发，并返回明确价格。", "inputSchema": {"type": "object", "required": ["position", "current_price"], "properties": {"position": {"type": "object"}, "current_price": {"type": "number"}, "highest_close": {"type": "number"}}}},
    {"name": "strategy_t_low_buy_check", "description": "检查低位买入、高位卖出的 T 方案，确认旧仓可卖量、支撑位和反弹条件。", "inputSchema": {"type": "object", "required": ["position", "current_price", "support_price", "rebound_confirmed"], "properties": {"position": {"type": "object"}, "current_price": {"type": "number"}, "support_price": {"type": "number"}, "rebound_confirmed": {"type": "boolean"}}}},
    {"name": "strategy_t_high_sell_check", "description": "检查低位买入之后的高位卖出 T 利润条件，并返回具体卖出数量和目标价。", "inputSchema": {"type": "object", "required": ["buy_price", "current_price", "buy_shares"], "properties": {"buy_price": {"type": "number"}, "current_price": {"type": "number"}, "buy_shares": {"type": "integer"}}}},
    {"name": "strategy_t_sell_first_check", "description": "检查先高位卖出、后低位买回的 T 方案，卖出和回补数量均受可卖老仓与 30% 上限约束。", "inputSchema": {"type": "object", "required": ["position", "current_price", "resistance_price", "planned_shares"], "properties": {"position": {"type": "object"}, "current_price": {"type": "number"}, "resistance_price": {"type": "number"}, "planned_shares": {"type": "integer"}}}},
    {"name": "strategy_position_size", "description": "按风险预算、入场价和固定止损计算整手股数。", "inputSchema": {"type": "object", "required": ["equity", "entry_price"], "properties": {"equity": {"type": "number"}, "entry_price": {"type": "number"}}}},
    {"name": "strategy_update_gate", "description": "检查新策略是否满足样本量、样本外验证和最大回撤门槛，激活时间为下一交易日。", "inputSchema": {"type": "object", "required": ["completed_trades", "out_of_sample_ok", "drawdown_worse_fraction"], "properties": {"completed_trades": {"type": "integer"}, "out_of_sample_ok": {"type": "boolean"}, "drawdown_worse_fraction": {"type": "number"}}}},
    {"name": "strategy_review_record", "description": "记录每日或每周策略复盘、观察和候选规则变更；只生成提案，不修改当前盘中规则。", "inputSchema": {"type": "object", "required": ["as_of", "scope", "completed_trades", "out_of_sample_ok", "drawdown_worse_fraction", "observations", "proposed_changes"], "properties": {"as_of": {"type": "string"}, "scope": {"type": "string"}, "completed_trades": {"type": "integer"}, "out_of_sample_ok": {"type": "boolean"}, "drawdown_worse_fraction": {"type": "number"}, "observations": {"type": "array", "items": {"type": "string"}}, "proposed_changes": {"type": "array", "items": {"type": "string"}}}}},
]


def call_tool(name: str, args: dict[str, Any]) -> Any:
    def build_node_packet() -> dict[str, Any]:
        from .market_packet import MarketPacketBuilder
        return MarketPacketBuilder().build(
            args["node"],
            include_intraday=bool(args.get("include_intraday", True)),
            persist=bool(args.get("persist", True)),
        )

    handlers: dict[str, Callable[[], Any]] = {
        "account_get": state.get_account,
        "context_get": lambda: state.context_pack(args.get("day")),
        "node_packet_get": build_node_packet,
        "dispatch_node_claim": lambda: state.claim_dispatch_node(args["day"], args["node"]),
        "task_session_get": lambda: state.get_task_session(args["day"]),
        "task_session_register": lambda: state.register_task_session(args["day"], args["thread_id"], args.get("host_id", "local"), args.get("title", ""), args.get("source", "scheduled_node"), bool(args.get("replace", False))),
        "order_intent_create": lambda: state.create_order_intent(args["code"], args["name"], args["side"], float(args["limit_price"]), int(args["shares"]), args["valid_from"], args["valid_until"], args["reason"], args.get("run_id")),
        "trade_feedback_record": lambda: state.record_trade_feedback(args["order_id"], args["status"], int(args.get("filled_shares", 0)), args.get("fill_price"), float(args.get("fees", 0))),
        "account_reconcile": lambda: state.reconcile_account(args["as_of"], float(args["available_cash"]), args["positions"], args["note"]),
        "trading_day_rollover": lambda: state.rollover(args["as_of"]),
        "candidate_pool_get": state.get_watchlist,
        "candidate_pool_update": lambda: state.update_watchlist(args["candidates"], args["rationale"], args.get("as_of"), args.get("metadata")),
        "candidate_pool_health_record": lambda: state.record_watchlist_health(args["status"], args["reasons"], args["metrics"], args["action"], args.get("as_of")),
        "analysis_run_record": lambda: str(state.record_run(args["day"], args["run_id"], args["payload"])),
        "node_delivery_prepare": lambda: state.prepare_node_delivery(args["day"], args["node"], args["target_thread_id"], args.get("target_host_id", "local"), args.get("source_thread_id", "")),
        "node_delivery_confirm": lambda: state.confirm_node_delivery(args["day"], args["node"], args["delivery_id"], args["transport_id"]),
        "node_delivery_fail": lambda: state.fail_node_delivery(args["day"], args["node"], args["delivery_id"], args["reason"]),
        "node_analysis_complete": lambda: state.complete_node_analysis(args["day"], args["node"], args["delivery_id"], args["run_id"], args.get("status", "completed"), args.get("summary", "")),
        "node_execution_status_get": lambda: state.get_node_execution_status(args["day"], args["node"]),
        "reports_close_day": lambda: {"daily": reports.close_day(args["day"], args.get("note", "")), "weekly": reports.weekly_report(args["day"]), "monthly": reports.monthly_report(args["day"])},
        "strategy_logic_get": trading_logic.load_logic,
        "strategy_entry_check": lambda: trading_logic.entry_check(args["snapshot"]),
        "strategy_exit_check": lambda: trading_logic.exit_check(args["position"], float(args["current_price"]), args.get("highest_close")),
        "strategy_t_low_buy_check": lambda: trading_logic.t_low_buy_check(args["position"], float(args["current_price"]), float(args["support_price"]), bool(args["rebound_confirmed"])),
        "strategy_t_high_sell_check": lambda: trading_logic.t_high_sell_check(float(args["buy_price"]), float(args["current_price"]), int(args["buy_shares"])),
        "strategy_t_sell_first_check": lambda: trading_logic.t_sell_first_check(args["position"], float(args["current_price"]), float(args["resistance_price"]), int(args["planned_shares"])),
        "strategy_position_size": lambda: trading_logic.position_size_by_risk(float(args["equity"]), float(args["entry_price"])),
        "strategy_update_gate": lambda: trading_logic.update_gate(int(args["completed_trades"]), bool(args["out_of_sample_ok"]), float(args["drawdown_worse_fraction"])),
        "strategy_review_record": lambda: str(trading_logic.record_review(args["as_of"], args["scope"], int(args["completed_trades"]), bool(args["out_of_sample_ok"]), float(args["drawdown_worse_fraction"]), args["observations"], args["proposed_changes"])),
    }
    if name not in handlers:
        raise state.DeskError(f"未知工具：{name}")
    return handlers[name]()


def response(request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error:
        message["error"] = error
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve() -> None:
    for line in sys.stdin:
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            request_id = request.get("id")
            method = request.get("method")
            if method == "initialize":
                response(request_id, {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "a-share-trading-desk", "version": "0.2.1"}})
            elif method == "tools/list":
                response(request_id, {"tools": TOOLS})
            elif method == "tools/call":
                params = request.get("params", {})
                result = call_tool(params["name"], params.get("arguments", {}))
                response(request_id, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]})
            elif request_id is not None:
                response(request_id, error={"code": -32601, "message": f"不支持的方法：{method}"})
        except Exception as error:
            if request.get("id") is not None:
                response(request["id"], error={"code": -32000, "message": f"交易台工具执行失败：{error}"})
            else:
                traceback.print_exc(file=sys.stderr)


if __name__ == "__main__":
    serve()
