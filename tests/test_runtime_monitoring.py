import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trading_desk import monitoring, reports, runtime, state


SHANGHAI = timezone(timedelta(hours=8))


class RuntimeMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.original = (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH)
        source_templates = Path(__file__).resolve().parent.parent / "monitoring" / "templates.json"
        (root / "monitoring").mkdir(parents=True)
        (root / "monitoring" / "templates.json").write_text(source_templates.read_text(encoding="utf-8"), encoding="utf-8")
        (root / "config").mkdir(parents=True)
        (root / "config" / "runtime.json").write_text(json.dumps({
            "analysis_interval_minutes": 60, "heartbeat_interval_minutes": 5,
            "analysis_lease_minutes": 15, "failed_retry_minutes": 10,
        }), encoding="utf-8")
        state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR = root, root / "state", root / "records", root / "journal"
        state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH = state.STATE_DIR / "account.json", state.STATE_DIR / "watchlist.json", state.STATE_DIR / "settings.json"
        state.initialize("2026-08-06", 10000, [{"code": "601398", "name": "工商银行", "shares": 100, "cost": 7.84, "sector": "银行"}])
        runtime.register_session("2026-08-06", "thread-a")

    def tearDown(self):
        (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH) = self.original
        self.tmp.cleanup()

    @staticmethod
    def at(hour, minute):
        return datetime(2026, 8, 6, hour, minute, tzinfo=SHANGHAI)

    def _complete(self, result, completed_at):
        state.record_run("2026-08-06", result["run_id"], {"summary": "test"})
        return runtime.complete_cycle("2026-08-06", result["run_id"], "completed", "test", now=completed_at)

    def _close_review(self):
        packet_path = state.RECORDS_DIR / "market_packets" / "2026-08-06" / "150500_market_close.json"
        state._write_json(packet_path, {
            "schema_version": 3,
            "trigger": "market_close",
            "generated_at": "2026-08-06T15:05:00+08:00",
        })
        return {
            "close_analysis": {
                "packet_path": str(packet_path),
                "data_health": "腾讯收盘报价可用，分时不可用。",
                "market_analysis": "指数震荡，风险偏好中性。",
                "sector_analysis": "板块快照不足，板块硬条件不可用。",
                "holding_analysis": "持仓未触发止损止盈。",
                "candidate_analysis": "候选等待次日确认。",
                "conclusion": "收盘不创建订单。",
            },
            "day_review": {
                "record_summary": "已复核全部分析回合。",
                "orders_and_feedback": "当日无委托。",
                "execution_deviations": ["午后覆盖不足"],
                "lessons": ["数据不足时不制造交易"],
                "account_reconciliation": "待券商账单确认。",
            },
            "next_day_outlook": {
                "trading_day": "2026-08-07",
                "market_expectation": "预计震荡分化。",
                "base_case": "指数平开后窄幅震荡。",
                "bull_case": "放量站上压力位则风险偏好改善。",
                "bear_case": "低开且跌破支撑则优先防守。",
                "position_plan": [{
                    "time": "09:25-10:00", "code": "601398", "name": "工商银行", "side": "hold",
                    "exact_price": 8.0, "shares": 100, "feedback_wait": "信号出现后等待5分钟复核",
                    "trigger": "价格保持在止损线上方", "invalidation": "跌破固定止损价",
                    "rationale": "未触发退出纪律。",
                }],
                "candidate_plan": [],
                "risk_points": ["板块条件不可用"],
                "pre_market_checks": ["核验集合竞价与板块快照"],
                "no_trade_conditions": ["腾讯行情不可用时不交易"],
            },
        }

    def test_hourly_due_time_resets_after_manual_analysis(self):
        first = runtime.poll("2026-08-06", "startup", True, now=self.at(9, 30))
        completed = self._complete(first, self.at(9, 31))
        self.assertEqual(completed["session"]["next_analysis_at"], "2026-08-06T10:31:00+08:00")
        self.assertEqual(runtime.poll("2026-08-06", now=self.at(10, 0))["action"], "skip")
        manual = runtime.poll("2026-08-06", "manual", now=self.at(10, 0))
        completed = self._complete(manual, self.at(10, 1))
        self.assertEqual(completed["session"]["next_analysis_at"], "2026-08-06T11:01:00+08:00")

    def test_monitor_crossing_triggers_analysis_without_auto_trade(self):
        first = runtime.poll("2026-08-06", "startup", True, now=self.at(9, 30))
        self._complete(first, self.at(9, 31))
        monitoring.apply_plan("2026-08-06", [{
            "id": "stop", "template_id": "price_breakdown", "code": "601398",
            "threshold": 7.70, "rearm_delta": 0.05, "cooldown_minutes": 30,
        }], "持仓失效价")

        def snapshot(_codes):
            return {"601398": {"quote": {"last_price": 7.69}}}

        result = runtime.poll("2026-08-06", "heartbeat", snapshot_provider=snapshot, now=self.at(10, 0))
        self.assertEqual(result["action"], "analyze")
        self.assertIn("monitor_signal", result["trigger_reasons"])
        self.assertIn("只触发重新分析", result["monitor_result"]["signals"][0]["instruction"])
        self.assertEqual(state.get_account()["pending_orders"], [])

    def test_post_close_archive_pauses_timer(self):
        result = runtime.poll("2026-08-06", "heartbeat", now=self.at(15, 5))
        self.assertTrue(result["close_required"])
        review = self._close_review()
        state.record_run("2026-08-06", result["run_id"], {"summary": "收盘复盘", "close_review": review})
        archived = reports.close_day("2026-08-06", result["run_id"], review, "整日复盘")
        completed = runtime.complete_cycle("2026-08-06", result["run_id"], "completed", "收盘完成", close_session=True, now=self.at(15, 6))
        self.assertEqual(completed["timer"]["action"], "pause")
        self.assertIsNone(completed["session"]["next_analysis_at"])
        report = Path(archived["daily_summary"]).read_text(encoding="utf-8")
        self.assertIn("收盘时点完整分析", report)
        self.assertIn("次日建议与预期", report)
        self.assertIn("条件预案，非委托", report)
        self.assertEqual(runtime.poll("2026-08-06", now=self.at(15, 10))["action"], "skip")
        revision = runtime.poll("2026-08-06", "close_revision", True, now=self.at(15, 11))
        self.assertEqual(revision["action"], "analyze")
        self.assertIn("close_revision", revision["trigger_reasons"])

    def test_summary_only_close_review_is_rejected(self):
        result = runtime.poll("2026-08-06", "heartbeat", now=self.at(15, 5))
        state.record_run("2026-08-06", result["run_id"], {"summary": "仅总结旧记录"})
        with self.assertRaises(state.DeskError):
            reports.close_day("2026-08-06", result["run_id"], {"summary": "仅总结"})

    def test_monitor_requires_timezone_on_expiry(self):
        with self.assertRaises(state.DeskError):
            monitoring.apply_plan("2026-08-06", [{
                "template_id": "price_breakout", "code": "601398", "threshold": 8.0,
                "expires_at": "2026-08-06T14:00:00",
            }], "测试")


if __name__ == "__main__":
    unittest.main()
