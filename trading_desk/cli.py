from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import monitoring, reports, runtime, state


def _json_argument(value: str) -> Any:
    candidate = Path(value)
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    return json.loads(value)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local A-share trading desk")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--date", required=True)
    init.add_argument("--available-cash", required=True, type=float)
    init.add_argument("--positions", required=True, help="JSON string or path")
    commands.add_parser("account")
    commands.add_parser("context")
    packet = commands.add_parser("analysis-packet", help="按动态触发原因生成腾讯数据包")
    packet.add_argument("--trigger", default="manual")
    packet.add_argument("--without-intraday", action="store_true", help="只拉取批量实时行情，不拉取分时摘要")
    packet.add_argument("--ephemeral", action="store_true", help="仅输出临时数据包，不写缓存或归档")
    intent = commands.add_parser("intent")
    intent.add_argument("--code", required=True)
    intent.add_argument("--name", required=True)
    intent.add_argument("--side", choices=["buy", "sell"], required=True)
    intent.add_argument("--price", required=True, type=float)
    intent.add_argument("--shares", required=True, type=int)
    intent.add_argument("--valid-from", required=True)
    intent.add_argument("--valid-until", required=True)
    intent.add_argument("--feedback-deadline", required=True)
    intent.add_argument("--reason", required=True)
    intent.add_argument("--run-id")
    feedback = commands.add_parser("feedback")
    feedback.add_argument("--order-id", required=True)
    feedback.add_argument("--status", choices=["filled", "partial", "cancelled"], required=True)
    feedback.add_argument("--shares", type=int, default=0)
    feedback.add_argument("--price", type=float)
    feedback.add_argument("--fees", type=float, default=0)
    commands.add_parser("candidate-pool")
    update = commands.add_parser("candidate-pool-update")
    update.add_argument("--candidates", required=True, help="JSON string or path")
    update.add_argument("--rationale", required=True)
    update.add_argument("--metadata", help="JSON string or path")
    update.add_argument("--date")
    rollover = commands.add_parser("rollover")
    rollover.add_argument("--date", required=True)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--date", required=True)
    reconcile.add_argument("--available-cash", required=True, type=float)
    reconcile.add_argument("--positions", required=True, help="JSON string or path")
    reconcile.add_argument("--note", required=True)
    close = commands.add_parser("close-day")
    close.add_argument("--date", required=True)
    close.add_argument("--run-id", required=True)
    close.add_argument("--review", required=True, help="三段式收盘复盘 JSON 或文件路径")
    close.add_argument("--note", default="")
    session = commands.add_parser("session")
    session.add_argument("--date", required=True)
    poll = commands.add_parser("runtime-poll")
    poll.add_argument("--date", required=True)
    poll.add_argument("--source", default="manual")
    poll.add_argument("--force", action="store_true")
    monitors = commands.add_parser("monitor-plan")
    monitors.add_argument("--date", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            result = state.initialize(args.date, args.available_cash, _json_argument(args.positions))
        elif args.command == "account":
            result = state.get_account()
        elif args.command == "context":
            result = state.context_pack()
        elif args.command == "analysis-packet":
            from .market_packet import MarketPacketBuilder
            result = MarketPacketBuilder().build(
                args.trigger,
                include_intraday=not args.without_intraday,
                persist=not args.ephemeral,
            )
        elif args.command == "intent":
            result = state.create_order_intent(args.code, args.name, args.side, args.price, args.shares, args.valid_from, args.valid_until, args.reason, args.feedback_deadline, args.run_id)
        elif args.command == "feedback":
            result = state.record_trade_feedback(args.order_id, args.status, args.shares, args.price, args.fees)
        elif args.command == "candidate-pool":
            result = state.get_watchlist()
        elif args.command == "candidate-pool-update":
            metadata = _json_argument(args.metadata) if args.metadata else None
            result = state.update_watchlist(_json_argument(args.candidates), args.rationale, args.date, metadata)
        elif args.command == "rollover":
            result = state.rollover(args.date)
        elif args.command == "reconcile":
            result = state.reconcile_account(args.date, args.available_cash, _json_argument(args.positions), args.note)
        elif args.command == "close-day":
            result = {"daily": reports.close_day(args.date, args.run_id, _json_argument(args.review), args.note), "weekly": reports.weekly_report(args.date), "monthly": reports.monthly_report(args.date)}
        elif args.command == "session":
            result = runtime.get_session(args.date)
        elif args.command == "runtime-poll":
            from .market_packet import MarketPacketBuilder
            result = runtime.poll(args.date, args.source, args.force, MarketPacketBuilder().monitor_snapshot)
        elif args.command == "monitor-plan":
            result = {"templates": monitoring.load_templates(), "plan": monitoring.get_plan(args.date)}
        else:
            raise state.DeskError("Unsupported command")
    except state.DeskError as error:
        raise SystemExit(f"ERROR: {error}") from error
    _print(result)


if __name__ == "__main__":
    main()
