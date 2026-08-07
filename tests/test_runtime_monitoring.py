import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trading_desk import monitoring, reports, runtime, state


SHANGHAI = timezone(timedelta(hours=8))


class RuntimeMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_disable_wakeup = os.environ.get("A_SHARE_DESK_DISABLE_WAKEUP")
        os.environ["A_SHARE_DESK_DISABLE_WAKEUP"] = "1"
        root = Path(self.tmp.name)
        self.original = (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH)
        source_templates = Path(__file__).resolve().parent.parent / "monitoring" / "templates.json"
        (root / "monitoring").mkdir(parents=True)
        (root / "monitoring" / "templates.json").write_text(source_templates.read_text(encoding="utf-8"), encoding="utf-8")
        (root / "config").mkdir(parents=True)
        (root / "config" / "runtime.json").write_text(json.dumps({
            "analysis_interval_minutes": 60, "local_monitor_poll_seconds": 30,
            "analysis_lease_minutes": 15, "failed_retry_minutes": 10,
        }), encoding="utf-8")
        state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR = root, root / "state", root / "records", root / "journal"
        state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH = state.STATE_DIR / "account.json", state.STATE_DIR / "watchlist.json", state.STATE_DIR / "settings.json"
        state.initialize("2026-08-06", 10000, [{"code": "601398", "name": "工商银行", "shares": 100, "cost": 7.84, "sector": "银行"}])
        runtime.register_session("2026-08-06", "thread-a")

    def tearDown(self):
        (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH) = self.original
        if self.original_disable_wakeup is None:
            os.environ.pop("A_SHARE_DESK_DISABLE_WAKEUP", None)
        else:
            os.environ["A_SHARE_DESK_DISABLE_WAKEUP"] = self.original_disable_wakeup
        self.tmp.cleanup()

    @staticmethod
    def at(hour, minute):
        return datetime(2026, 8, 6, hour, minute, tzinfo=SHANGHAI)

    def _complete(self, result, completed_at):
        reports.record_analysis_run("2026-08-06", result["run_id"], self._analysis_payload())
        return runtime.complete_cycle("2026-08-06", result["run_id"], "completed", "test", now=completed_at)

    @staticmethod
    def _analysis_payload(close_review=None):
        payload = {
            "data_health": {"quotes": "腾讯报价新鲜", "intraday": "腾讯分时可用"},
            "facts": ["测试事实"],
            "interpretation": ["测试解读"],
            "rules_applied": ["测试规则"],
            "conclusion": {
                "trading_advice": "本轮无交易建议",
                "reason": "测试未触发交易规则。",
                "instruction_lines": [],
            },
            "monitor_decision": {"rationale": "测试不启用监控", "monitors": []},
        }
        if close_review is not None:
            payload["close_review"] = close_review
        return payload

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
        self.assertIn("## 事实", completed["user_visible_output"])
        self.assertIn("不得只发送 summary", completed["display_contract"])
        self.assertEqual(runtime.poll("2026-08-06", now=self.at(10, 0))["action"], "skip")
        manual = runtime.poll("2026-08-06", "manual", now=self.at(10, 0))
        completed = self._complete(manual, self.at(10, 1))
        self.assertEqual(completed["session"]["next_analysis_at"], "2026-08-06T11:01:00+08:00")

    def test_legacy_heartbeat_state_migrates_to_split_local_workers(self):
        path = state.STATE_DIR / "task_sessions" / "2026-08-06.json"
        session = state._read_json(path)
        session["heartbeat_interval_minutes"] = 5
        session["heartbeat_automation_id"] = "legacy-heartbeat"
        session["next_analysis_at"] = "2026-08-06T10:30:00+08:00"
        state._write_json(path, session)
        migrated = runtime.migrate_session_delivery("2026-08-06")
        saved = state._read_json(path)
        self.assertEqual(migrated["status"], "migrated")
        self.assertNotIn("heartbeat_interval_minutes", saved)
        self.assertNotIn("heartbeat_automation_id", saved)
        self.assertEqual(saved["delivery_mode"], "split_local_timer_and_monitor")
        self.assertEqual(saved["wakeup_timer"]["run_at"], "2026-08-06T10:30:00+08:00")
        self.assertIn("monitor_worker", saved)

    def test_replacing_archived_thread_preserves_today_history(self):
        first = runtime.poll("2026-08-06", "startup", True, now=self.at(9, 30))
        self._complete(first, self.at(9, 31))
        replaced = runtime.register_session(
            "2026-08-06", "thread-b", title="A股交易台 2026-08-06", source="restart_after_archive", replace=True
        )
        self.assertEqual(replaced["thread_id"], "thread-b")
        self.assertEqual(len(replaced["cycle_history"]), 1)
        self.assertIsNone(replaced["current_cycle"])
        self.assertEqual(replaced["history"][-1]["thread_id"], "thread-a")

    def test_completed_cycle_arms_independent_timer_and_monitor(self):
        result = runtime.poll("2026-08-06", "manual", True, now=self.at(10, 0))
        monitoring.apply_plan("2026-08-06", [{
            "id": "stop", "template_id": "price_breakdown", "code": "601398",
            "threshold": 7.70, "rearm_delta": 0.05, "cooldown_minutes": 30,
        }], "持仓止损")
        completed = self._complete(result, self.at(10, 1))
        timer = completed["session"]["wakeup_timer"]
        monitor_worker = completed["session"]["monitor_worker"]
        self.assertEqual(timer["worker_kind"], "timer")
        self.assertEqual(monitor_worker["worker_kind"], "monitor")
        self.assertNotEqual(timer["token"], monitor_worker["token"])

    def test_force_cannot_bypass_0915_earliest_gate(self):
        result = runtime.poll("2026-08-06", "startup", True, now=self.at(9, 14))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason_code"], "before_earliest_analysis_time")
        self.assertEqual(result["next_analysis_at"], "2026-08-06T09:15:00+08:00")
        self.assertIsNone(runtime.get_session("2026-08-06")["current_cycle"])

    def test_pre_market_uses_separate_prompt_then_schedules_0930(self):
        result = runtime.poll("2026-08-06", "startup", True, now=self.at(9, 15))
        self.assertEqual(result["action"], "analyze")
        self.assertEqual(result["analysis_mode"], "pre_market")
        self.assertEqual(result["analysis_prompt"], "pre_market_session.md")
        self.assertFalse(result["include_intraday"])
        self.assertFalse(result["phase"]["allows_new_orders"])
        completed = self._complete(result, self.at(9, 16))
        self.assertEqual(completed["timer"]["action"], "await_continuous_session")
        self.assertEqual(completed["session"]["next_analysis_at"], "2026-08-06T09:30:00+08:00")
        intraday = runtime.poll("2026-08-06", "timer", now=self.at(9, 30))
        self.assertEqual(intraday["action"], "analyze")
        self.assertEqual(intraday["analysis_mode"], "intraday")
        self.assertTrue(intraday["include_intraday"])

    def test_hourly_timer_is_capped_at_market_close(self):
        result = runtime.poll("2026-08-06", "manual", True, now=self.at(14, 29))
        completed = self._complete(result, self.at(14, 30))
        self.assertEqual(completed["session"]["next_analysis_at"], "2026-08-06T15:00:00+08:00")

    def test_poll_auto_rolls_stale_account_before_analysis(self):
        account = state.get_account()
        account["as_of"] = "2026-08-05"
        account["positions"][0]["sellable_shares"] = 0
        account["positions"][0]["today_bought_shares"] = 100
        state.save_account(account)
        result = runtime.poll("2026-08-06", "startup", True, now=self.at(9, 15))
        self.assertEqual(result["action"], "analyze")
        self.assertEqual(runtime.get_session("2026-08-06")["rollover"]["status"], "rolled")
        rolled = state.get_account()
        self.assertEqual(rolled["as_of"], "2026-08-06")
        self.assertEqual(rolled["positions"][0]["sellable_shares"], 100)

    def test_legacy_sector_snapshot_maintenance_is_removed(self):
        path = state.STATE_DIR / "task_sessions" / "2026-08-06.json"
        session = state._read_json(path)
        session["sector_snapshot_maintenance"] = {"required": True}
        state._write_json(path, session)
        result = runtime.poll("2026-08-06", "startup", True, now=self.at(9, 30))
        self.assertNotIn("sector_snapshot_maintenance", result)
        self.assertNotIn("sector_snapshot_maintenance", runtime.get_session("2026-08-06"))

    def test_monitor_crossing_triggers_analysis_without_auto_trade(self):
        first = runtime.poll("2026-08-06", "startup", True, now=self.at(9, 30))
        self._complete(first, self.at(9, 31))
        monitoring.apply_plan("2026-08-06", [{
            "id": "stop", "template_id": "price_breakdown", "code": "601398",
            "threshold": 7.70, "rearm_delta": 0.05, "cooldown_minutes": 30,
        }], "持仓失效价")

        def snapshot(_codes):
            return {"601398": {"quote": {"last_price": 7.69}}}

        result = runtime.poll("2026-08-06", "monitor", True, snapshot_provider=snapshot, now=self.at(10, 0))
        self.assertEqual(result["action"], "analyze")
        self.assertIn("monitor_signal", result["trigger_reasons"])
        self.assertIn("只触发重新分析", result["monitor_result"]["signals"][0]["instruction"])
        self.assertEqual(state.get_account()["pending_orders"], [])

    def test_post_close_archive_pauses_timer(self):
        result = runtime.poll("2026-08-06", "timer", now=self.at(15, 5))
        self.assertTrue(result["close_required"])
        review = self._close_review()
        reports.record_analysis_run("2026-08-06", result["run_id"], self._analysis_payload(review))
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
        result = runtime.poll("2026-08-06", "timer", now=self.at(15, 5))
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
