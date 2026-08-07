import tempfile
import unittest
from pathlib import Path

from trading_desk import reports, state


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.original = (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH)
        state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR = root, root / "state", root / "records", root / "journal"
        state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH = state.STATE_DIR / "account.json", state.STATE_DIR / "watchlist.json", state.STATE_DIR / "settings.json"
        state.initialize("2026-08-06", 10000, [{"code": "601398", "name": "工商银行", "shares": 100, "cost": 7.84, "sector": "银行"}])

    def tearDown(self):
        (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH) = self.original
        self.tmp.cleanup()

    def test_today_bought_shares_are_not_sellable(self):
        with self.assertRaises(state.DeskError):
            state.create_order_intent("601398", "工商银行", "sell", 8.0, 100, "10:35", "10:40", "test", "10:45")

    def test_cancelled_buy_releases_cash_and_strict_instruction(self):
        order = state.create_order_intent("601398", "工商银行", "buy", 7.8, 100, "10:35", "10:40", "test", "10:45")
        self.assertEqual(order["instruction_line"], "10:35-10:40，601398 工商银行，买 7.800 元，100 股，等待反馈至 10:45")
        self.assertEqual(len(order["instruction_line"].split("，")), 5)
        state.record_trade_feedback(order["id"], "cancelled")
        self.assertEqual(state.get_account()["cash_available"], 10000)

    def test_rollover_makes_shares_sellable(self):
        state.rollover("2026-08-07")
        self.assertEqual(state.get_account()["positions"][0]["sellable_shares"], 100)

    def test_settings_migrate_legacy_heartbeat_runtime(self):
        state._write_json(state.SETTINGS_PATH, {
            "analysis_runtime": {
                "mode": "first_open_then_resettable_timer",
                "heartbeat_interval_minutes": 5,
            }
        })
        runtime_settings = state.get_settings()["analysis_runtime"]
        self.assertEqual(runtime_settings["mode"], "first_open_then_split_local_timer_monitor")
        self.assertNotIn("heartbeat_interval_minutes", runtime_settings)

    def _candidate(self, code="000001", sector="银行II"):
        return {
            "code": code, "name": "测试股票", "sector": sector, "bucket": "core", "score": 75,
            "score_breakdown": {"technical": 23, "relative_strength": 15, "flow_liquidity": 11, "sector": 12, "fundamental_event": 7, "risk_reward": 7},
            "technical_confirmation": True, "data_as_of": "2026-08-06",
            "sources": [{"name": "测试源", "captured_at": "2026-08-06T17:00:00+08:00"}],
        }

    def test_candidate_pool_is_auditable_and_excludes_holdings(self):
        pool = state.update_watchlist([self._candidate()], "周筛", as_of="2026-08-06")
        self.assertEqual(pool["health"]["status"], "normal")
        with self.assertRaises(state.DeskError):
            state.update_watchlist([self._candidate("601398", "银行")], "不得包含持仓")

    def test_task_session_is_unique(self):
        state.register_task_session("2026-08-06", "thread-a")
        with self.assertRaises(state.DeskError):
            state.register_task_session("2026-08-06", "thread-b")

    def test_context_has_no_sector_membership_snapshot(self):
        self.assertNotIn("sector_universe", state.context_pack())

    def test_analysis_record_forces_complete_user_visible_chain(self):
        payload = {
            "data_health": {"quotes": "腾讯报价新鲜"},
            "facts": ["事实A"],
            "interpretation": ["解读A"],
            "rules_applied": ["规则A"],
            "conclusion": {
                "trading_advice": "本轮无交易建议",
                "reason": "没有触发规则。",
                "instruction_lines": [],
            },
            "monitor_decision": {"rationale": "没有必要监控", "monitors": []},
            "user_visible_summary": "旧式短摘要",
        }
        recorded = reports.record_analysis_run("2026-08-06", "run-output", payload)
        output = recorded["user_visible_output"]
        for heading in ("## 事实", "## 解读", "## 规则", "## 结论"):
            self.assertIn(heading, output)
        saved = state._read_json(Path(recorded["path"]))
        self.assertEqual(saved["user_visible_output"], output)
        journal = (state.JOURNAL_DIR / "daily" / "2026-08-06.md").read_text(encoding="utf-8")
        self.assertIn("## 事实", journal)

    def test_summary_only_analysis_record_is_rejected(self):
        with self.assertRaises(state.DeskError):
            reports.record_analysis_run("2026-08-06", "run-summary", {"summary": "只有摘要"})


if __name__ == "__main__":
    unittest.main()
