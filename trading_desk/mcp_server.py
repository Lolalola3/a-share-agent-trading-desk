"""Dependency-free MCP stdio server for the local trading-desk tools."""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

from . import monitoring, reports, runtime, state, trading_logic, wakeup
from .market_packet import MarketPacketBuilder


if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


TOOLS = [
    {"name": "account_get", "description": "读取账户主档案，包括资金、可卖股份和未反馈委托锁定。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "context_get", "description": "读取本次盘面分析必须使用的固定上下文。", "inputSchema": {"type": "object", "properties": {"day": {"type": "string"}}}},
    {"name": "analysis_protocol_get", "description": "一次性读取无人值守节点所需的四份提示协议和当前策略，避免通过 Shell 读取本地文件而触发审批。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "order_intent_create", "description": "登记一笔精确的、不会自动提交的限价买卖指令，并锁定资金或可卖股份。", "inputSchema": {"type": "object", "required": ["code", "name", "side", "limit_price", "shares", "valid_from", "valid_until", "reason"], "properties": {"code": {"type": "string"}, "name": {"type": "string"}, "side": {"type": "string", "enum": ["buy", "sell"]}, "limit_price": {"type": "number"}, "shares": {"type": "integer"}, "valid_from": {"type": "string"}, "valid_until": {"type": "string"}, "reason": {"type": "string"}, "run_id": {"type": "string"}}}},
    {"name": "trade_feedback_record", "description": "记录用户实际成交、部分成交或撤单，并更新资金和 T+1 可卖数量。", "inputSchema": {"type": "object", "required": ["order_id", "status"], "properties": {"order_id": {"type": "string"}, "status": {"type": "string", "enum": ["filled", "partial", "cancelled"]}, "filled_shares": {"type": "integer"}, "fill_price": {"type": "number"}, "fees": {"type": "number"}}}},
    {"name": "account_reconcile", "description": "所有委托反馈完成后，按用户提供的券商数据进行盘后账户核对。", "inputSchema": {"type": "object", "required": ["as_of", "available_cash", "positions", "note"], "properties": {"as_of": {"type": "string"}, "available_cash": {"type": "number"}, "positions": {"type": "array"}, "note": {"type": "string"}}}},
    {"name": "trading_day_rollover", "description": "新交易日开始时，在确认没有未反馈委托后，将前一交易日买入的股份转为可卖。", "inputSchema": {"type": "object", "required": ["as_of"], "properties": {"as_of": {"type": "string"}}}},
    {"name": "candidate_pool_get", "description": "读取当前周候选池、评分分解、来源、有效期和健康状态。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "candidate_pool_update", "description": "保存由大模型按周筛 Prompt 完成并通过结构校验的最多五只候选股；失败时不覆盖旧池。", "inputSchema": {"type": "object", "required": ["candidates", "rationale"], "properties": {"candidates": {"type": "array", "minItems": 1, "maxItems": 5}, "rationale": {"type": "string"}, "as_of": {"type": "string"}, "metadata": {"type": "object"}}}},
    {"name": "candidate_pool_health_record", "description": "记录工作日候选池健康状态及是否冻结新买入或触发盘后应急重筛。", "inputSchema": {"type": "object", "required": ["status", "reasons", "metrics", "action"], "properties": {"status": {"type": "string", "enum": ["normal", "watch", "frozen", "invalidated"]}, "reasons": {"type": "array", "items": {"type": "string"}}, "metrics": {"type": "object"}, "action": {"type": "string"}, "as_of": {"type": "string"}}}},
    {"name": "analysis_run_record", "description": "校验事实→解读→规则→结论并保存分析；返回必须完整展示的 user_visible_output。", "inputSchema": {"type": "object", "required": ["day", "run_id", "payload"], "properties": {"day": {"type": "string"}, "run_id": {"type": "string"}, "payload": {"type": "object", "required": ["data_health", "facts", "interpretation", "rules_applied", "conclusion", "monitor_decision"], "properties": {"data_health": {"oneOf": [{"type": "object"}, {"type": "array", "items": {"type": "string"}}, {"type": "string"}]}, "facts": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "interpretation": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "rules_applied": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "conclusion": {"type": "object", "required": ["trading_advice", "reason", "instruction_lines"], "properties": {"trading_advice": {"type": "string"}, "reason": {"type": "string"}, "instruction_lines": {"type": "array", "items": {"type": "string"}}}}, "monitor_decision": {"type": "object", "required": ["rationale"], "properties": {"rationale": {"type": "string"}, "monitors": {"type": "array", "items": {"type": "object"}}}}}, "additionalProperties": True}}}},
    {"name": "reports_close_day", "description": "校验并归档收盘时点完整分析、当日记录复核和次日建议预期，再生成日报交接包、周报和月报。", "inputSchema": {"type": "object", "required": ["day", "run_id", "review"], "properties": {"day": {"type": "string"}, "run_id": {"type": "string"}, "review": {"type": "object"}, "note": {"type": "string"}}}},
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

# Replace the v4 order schema with the strict five-field v5 schema below.
TOOLS = [tool for tool in TOOLS if tool["name"] != "order_intent_create"]
TOOLS[2]["description"] = "读取动态日任务、腾讯数据、监控和收盘归档协议及当前策略。"
TOOLS.extend([
    {"name": "analysis_packet_get", "description": "拉取腾讯个股/指数与腾讯申万二级行业总体行情；不请求板块成分股、不做本地板块行情计算。", "inputSchema": {"type": "object", "properties": {"trigger": {"type": "string"}, "include_intraday": {"type": "boolean"}, "persist": {"type": "boolean"}}}},
    {"name": "daily_session_get", "description": "读取当日唯一交易任务及可重置一小时计时器状态。", "inputSchema": {"type": "object", "required": ["day"], "properties": {"day": {"type": "string"}}}},
    {"name": "local_wakeup_get", "description": "分别读取纯计时worker与独立腾讯监控worker状态；普通轮询不会创建对话消息。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "daily_session_delivery_migrate", "description": "清除旧聊天心跳状态，按next_analysis_at启用纯计时worker，并为活动规则另启独立监控worker；不会立即认领分析。", "inputSchema": {"type": "object", "required": ["day"], "properties": {"day": {"type": "string"}}}},
    {"name": "daily_session_register", "description": "登记当日唯一交易任务；默认拒绝重复任务。", "inputSchema": {"type": "object", "required": ["day", "thread_id"], "properties": {"day": {"type": "string"}, "thread_id": {"type": "string"}, "host_id": {"type": "string"}, "title": {"type": "string"}, "source": {"type": "string"}, "replace": {"type": "boolean"}}}},
    {"name": "analysis_runtime_poll", "description": "执行09:15最早门禁、自动交易日滚动，并路由盘前/盘中/收盘分析。", "inputSchema": {"type": "object", "required": ["day"], "properties": {"day": {"type": "string"}, "source": {"type": "string"}, "force": {"type": "boolean"}}}},
    {"name": "analysis_cycle_complete", "description": "分析留档后完成当前周期并重置一小时计时器；收盘归档后关闭计时器。", "inputSchema": {"type": "object", "required": ["day", "run_id", "status", "summary"], "properties": {"day": {"type": "string"}, "run_id": {"type": "string"}, "status": {"type": "string", "enum": ["completed", "failed"]}, "summary": {"type": "string"}, "close_session": {"type": "boolean"}}}},
    {"name": "monitor_templates_get", "description": "读取 Agent 可选择的监控程序模板和参数说明。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "monitor_plan_get", "description": "读取当日启用的监控规则。", "inputSchema": {"type": "object", "required": ["day"], "properties": {"day": {"type": "string"}}}},
    {"name": "monitor_plan_apply", "description": "分析后由 Agent 选择启用、修改或清空监控；只能监控持仓和候选池。", "inputSchema": {"type": "object", "required": ["day", "monitors", "rationale"], "properties": {"day": {"type": "string"}, "monitors": {"type": "array", "items": {"type": "object", "required": ["template_id", "code", "threshold"], "properties": {"id": {"type": "string"}, "template_id": {"type": "string"}, "code": {"type": "string"}, "name": {"type": "string"}, "threshold": {"type": "number"}, "rearm_delta": {"type": "number"}, "cooldown_minutes": {"type": "integer"}, "expires_at": {"type": "string"}, "note": {"type": "string"}, "enabled": {"type": "boolean"}}, "additionalProperties": True}}, "rationale": {"type": "string"}}}},
    {"name": "order_intent_create", "description": "登记不自动提交的精确限价建议并锁定资金或股份；返回严格五字段 instruction_line。", "inputSchema": {"type": "object", "required": ["code", "name", "side", "limit_price", "shares", "valid_from", "valid_until", "feedback_deadline", "reason"], "properties": {"code": {"type": "string"}, "name": {"type": "string"}, "side": {"type": "string", "enum": ["buy", "sell"]}, "limit_price": {"type": "number"}, "shares": {"type": "integer"}, "valid_from": {"type": "string"}, "valid_until": {"type": "string"}, "feedback_deadline": {"type": "string"}, "reason": {"type": "string"}, "run_id": {"type": "string"}}}},
])


def analysis_protocol_pack() -> dict[str, Any]:
    prompt_root = Path(__file__).resolve().parent.parent / "prompts"
    names = ("session_bootstrap.md", "global_policy.md", "data_acquisition.md", "pre_market_session.md", "daily_session.md", "monitoring.md", "weekly_candidate_screen.md")
    return {
        "prompt_workflow_version": "5.4.0",
        "runtime_setting_version": state.get_settings().get("prompt_workflow_version"),
        "prompts": {name: (prompt_root / name).read_text(encoding="utf-8") for name in names},
        "strategy": trading_logic.load_logic(),
        "instruction": "本工具是动态日任务的协议入口；不得再用 Shell 重复读取这些文件。",
    }


def call_tool(name: str, args: dict[str, Any]) -> Any:
    handlers: dict[str, Callable[[], Any]] = {
        "account_get": state.get_account,
        "context_get": lambda: state.context_pack(args.get("day")),
        "analysis_protocol_get": analysis_protocol_pack,
        "analysis_packet_get": lambda: MarketPacketBuilder().build(args.get("trigger", "manual"), include_intraday=bool(args.get("include_intraday", True)), persist=bool(args.get("persist", True))),
        "daily_session_get": lambda: runtime.get_session(args["day"]),
        "local_wakeup_get": wakeup.get_status,
        "daily_session_delivery_migrate": lambda: runtime.migrate_session_delivery(args["day"]),
        "daily_session_register": lambda: runtime.register_session(args["day"], args["thread_id"], args.get("host_id", "local"), args.get("title", ""), args.get("source", "daily_bootstrap"), bool(args.get("replace", False))),
        "analysis_runtime_poll": lambda: runtime.poll(args["day"], args.get("source", "timer"), bool(args.get("force", False)), MarketPacketBuilder().monitor_snapshot),
        "analysis_cycle_complete": lambda: runtime.complete_cycle(args["day"], args["run_id"], args["status"], args["summary"], bool(args.get("close_session", False))),
        "monitor_templates_get": monitoring.load_templates,
        "monitor_plan_get": lambda: monitoring.get_plan(args["day"]),
        "monitor_plan_apply": lambda: monitoring.apply_plan(args["day"], args["monitors"], args["rationale"]),
        "order_intent_create": lambda: state.create_order_intent(args["code"], args["name"], args["side"], float(args["limit_price"]), int(args["shares"]), args["valid_from"], args["valid_until"], args["reason"], args["feedback_deadline"], args.get("run_id")),
        "trade_feedback_record": lambda: state.record_trade_feedback(args["order_id"], args["status"], int(args.get("filled_shares", 0)), args.get("fill_price"), float(args.get("fees", 0))),
        "account_reconcile": lambda: state.reconcile_account(args["as_of"], float(args["available_cash"]), args["positions"], args["note"]),
        "trading_day_rollover": lambda: state.rollover(args["as_of"]),
        "candidate_pool_get": state.get_watchlist,
        "candidate_pool_update": lambda: state.update_watchlist(args["candidates"], args["rationale"], args.get("as_of"), args.get("metadata")),
        "candidate_pool_health_record": lambda: state.record_watchlist_health(args["status"], args["reasons"], args["metrics"], args["action"], args.get("as_of")),
        "analysis_run_record": lambda: reports.record_analysis_run(args["day"], args["run_id"], args["payload"]),
        "reports_close_day": lambda: {"daily": reports.close_day(args["day"], args["run_id"], args["review"], args.get("note", "")), "weekly": reports.weekly_report(args["day"]), "monthly": reports.monthly_report(args["day"])},
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
                response(request_id, {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "a-share-trading-desk", "version": "0.5.3"}})
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
