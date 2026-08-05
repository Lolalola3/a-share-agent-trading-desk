import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from trading_desk import state


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.original = (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH)
        state.ROOT = root
        state.STATE_DIR = root / "state"
        state.RECORDS_DIR = root / "records"
        state.JOURNAL_DIR = root / "journal"
        state.ACCOUNT_PATH = state.STATE_DIR / "account.json"
        state.WATCHLIST_PATH = state.STATE_DIR / "watchlist.json"
        state.SETTINGS_PATH = state.STATE_DIR / "settings.json"
        state.initialize("2026-07-31", 10000, [{"code": "000002", "name": "DEMO", "shares": 100, "cost": 10.0}])

    def tearDown(self):
        (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH) = self.original
        self.tmp.cleanup()

    def test_today_bought_shares_are_not_sellable(self):
        with self.assertRaises(state.DeskError):
            state.create_order_intent("000002", "DEMO", "sell", 10.2, 100, "10:35", "10:40", "test")

    def test_cancelled_buy_releases_cash_and_pending_lock(self):
        order = state.create_order_intent("000002", "DEMO", "buy", 9.8, 100, "10:35", "10:40", "test")
        self.assertLess(state.get_account()["cash_available"], 10000)
        state.record_trade_feedback(order["id"], "cancelled")
        account = state.get_account()
        self.assertEqual(account["cash_available"], 10000)
        self.assertEqual(account["pending_orders"], [])

    def test_rollover_makes_shares_sellable(self):
        state.rollover("2026-08-03")
        account = state.get_account()
        self.assertEqual(account["positions"][0]["sellable_shares"], 100)

    def test_watchlist_rejects_non_unified_bucket(self):
        with self.assertRaises(state.DeskError):
            state.update_watchlist([{
                "code": "000001", "name": "测试", "bucket": "event", "score": 80,
            }], "测试")

    def _candidate(self, code="000001", sector="银行"):
        return {
            "code": code,
            "name": "测试股票",
            "sector": sector,
            "bucket": "core",
            "score": 75,
            "score_breakdown": {
                "technical": 23,
                "relative_strength": 15,
                "flow_liquidity": 11,
                "sector": 12,
                "fundamental_event": 7,
                "risk_reward": 7,
            },
            "technical_confirmation": True,
            "data_as_of": "2026-07-31",
            "sources": [{"name": "测试源", "captured_at": "2026-08-02T17:00:00"}],
        }

    def test_candidate_pool_is_auditable_and_excludes_holdings(self):
        pool = state.update_watchlist([self._candidate()], "周筛", as_of="2026-08-02")
        self.assertEqual(pool["schema_version"], 2)
        self.assertEqual(pool["health"]["status"], "normal")
        self.assertEqual(pool["candidates"][0]["score_breakdown"]["technical"], 23)
        with self.assertRaises(state.DeskError):
            state.update_watchlist([self._candidate("000002", "示例行业")], "不得包含持仓")

    def test_candidate_health_can_freeze_new_entries(self):
        state.update_watchlist([self._candidate()], "周筛", as_of="2026-08-02")
        pool = state.record_watchlist_health(
            "frozen", ["三只候选失效"], {"invalid_candidates": 3}, "盘后应急重筛", "2026-08-03"
        )
        self.assertEqual(pool["health"]["status"], "frozen")
        self.assertEqual(len(pool["health_history"]), 1)

    def test_task_session_is_unique_and_can_replace_confirmed_stale_thread(self):
        first = state.register_task_session("2026-08-03", "thread-a")
        self.assertEqual(first["thread_id"], "thread-a")
        with self.assertRaises(state.DeskError):
            state.register_task_session("2026-08-03", "thread-b")
        second = state.register_task_session("2026-08-03", "thread-b", replace=True)
        self.assertEqual(second["thread_id"], "thread-b")
        self.assertEqual(second["history"][0]["thread_id"], "thread-a")

    def test_backlog_only_allows_the_latest_due_node(self):
        now = datetime(2026, 8, 4, 12, 13)
        for node in ["09:08", "09:22", "10:30"]:
            result = state.claim_dispatch_node("2026-08-04", node, now)
            self.assertEqual(result["action"], "skip")
            self.assertEqual(result["effective_node"], "11:25")

        current = state.claim_dispatch_node("2026-08-04", "11:25", now)
        self.assertEqual(current["action"], "execute")
        duplicate = state.claim_dispatch_node("2026-08-04", "11:25", now)
        self.assertEqual(duplicate["action"], "skip")
        self.assertIn("已认领", duplicate["reason"])

    def test_next_real_node_remains_independent(self):
        before = datetime(2026, 8, 4, 12, 13)
        after = datetime(2026, 8, 4, 13, 1)
        state.claim_dispatch_node("2026-08-04", "11:25", before)
        result = state.claim_dispatch_node("2026-08-04", "13:00", after)
        self.assertEqual(result["action"], "execute")
        self.assertEqual(result["effective_node"], "13:00")

    def test_previous_day_backlog_is_rejected(self):
        result = state.claim_dispatch_node(
            "2026-08-03", "15:05", datetime(2026, 8, 4, 9, 30)
        )
        self.assertEqual(result["action"], "skip")
        self.assertIn("不是北京时间今天", result["reason"])

    def test_delivery_and_analysis_have_independent_states(self):
        prepared = state.prepare_node_delivery(
            "2026-08-05", "11:25", "daily-thread", source_thread_id="scheduler-thread"
        )
        self.assertEqual(prepared["delivery"]["status"], "pending")
        self.assertEqual(prepared["analysis"]["status"], "pending")

        confirmed = state.confirm_node_delivery(
            "2026-08-05", "11:25", prepared["delivery_id"], "carrier-automation"
        )
        self.assertEqual(confirmed["delivery"]["status"], "confirmed")
        self.assertEqual(confirmed["analysis"]["status"], "pending")

        state.record_run("2026-08-05", "run-simulated", {"summary": "isolated simulation"})
        completed = state.complete_node_analysis(
            "2026-08-05", "11:25", prepared["delivery_id"], "run-simulated", summary="ok"
        )
        self.assertEqual(completed["delivery"]["status"], "confirmed")
        self.assertEqual(completed["analysis"]["status"], "completed")

    def test_analysis_cannot_complete_before_delivery_confirmation(self):
        prepared = state.prepare_node_delivery("2026-08-05", "13:00", "daily-thread")
        state.record_run("2026-08-05", "run-too-early", {"summary": "isolated simulation"})
        with self.assertRaises(state.DeskError):
            state.complete_node_analysis(
                "2026-08-05", "13:00", prepared["delivery_id"], "run-too-early"
            )

    def test_failed_delivery_does_not_remain_pending(self):
        prepared = state.prepare_node_delivery("2026-08-05", "14:25", "archived-thread")
        failed = state.fail_node_delivery(
            "2026-08-05", "14:25", prepared["delivery_id"], "target thread archived"
        )
        self.assertEqual(failed["delivery"]["status"], "failed")
        self.assertEqual(failed["analysis"]["status"], "pending")
        self.assertEqual(failed["delivery"]["failure_reason"], "target thread archived")

    def test_missed_nodes_can_start_daily_task_and_complete_two_later_nodes(self):
        skipped = state.claim_dispatch_node("2026-08-05", "09:08", datetime(2026, 8, 5, 12, 13))
        self.assertEqual(skipped["action"], "skip")
        self.assertEqual(skipped["effective_node"], "11:25")

        first_claim = state.claim_dispatch_node("2026-08-05", "11:25", datetime(2026, 8, 5, 12, 13))
        self.assertEqual(first_claim["action"], "execute")
        state.register_task_session("2026-08-05", "sim-daily-thread")
        first = state.prepare_node_delivery("2026-08-05", "11:25", "sim-daily-thread")
        state.confirm_node_delivery("2026-08-05", "11:25", first["delivery_id"], "ack:11:25")
        state.record_run("2026-08-05", "sim-1125", {"summary": "建议一：继续持有观察"})
        first_done = state.complete_node_analysis(
            "2026-08-05", "11:25", first["delivery_id"], "sim-1125", summary="建议一已返回"
        )

        second_claim = state.claim_dispatch_node("2026-08-05", "13:00", datetime(2026, 8, 5, 13, 1))
        self.assertEqual(second_claim["action"], "execute")
        second = state.prepare_node_delivery("2026-08-05", "13:00", "sim-daily-thread")
        state.confirm_node_delivery("2026-08-05", "13:00", second["delivery_id"], "ack:13:00")
        state.record_run("2026-08-05", "sim-1300", {"summary": "建议二：等待突破确认"})
        second_done = state.complete_node_analysis(
            "2026-08-05", "13:00", second["delivery_id"], "sim-1300", summary="建议二已返回"
        )

        self.assertEqual(first_done["analysis"]["status"], "completed")
        self.assertEqual(second_done["analysis"]["status"], "completed")
        self.assertEqual(state.get_task_session("2026-08-05")["thread_id"], "sim-daily-thread")


if __name__ == "__main__":
    unittest.main()
