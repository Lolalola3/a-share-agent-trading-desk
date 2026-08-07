import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trading_desk import monitoring, state, wakeup


SHANGHAI = timezone(timedelta(hours=8))


class LocalWakeupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.original = (
            state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR,
            state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH,
        )
        state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR = root, root / "state", root / "records", root / "journal"
        state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH = state.STATE_DIR / "account.json", state.STATE_DIR / "watchlist.json", state.STATE_DIR / "settings.json"
        (root / "monitoring").mkdir(parents=True)
        source = Path(__file__).resolve().parent.parent / "monitoring" / "templates.json"
        (root / "monitoring" / "templates.json").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        state.initialize("2026-08-07", 1000, [{"code": "600000", "name": "测试", "shares": 100, "cost": 10}])

    def tearDown(self):
        (
            state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR,
            state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH,
        ) = self.original
        self.tmp.cleanup()

    @staticmethod
    def at(hour, minute):
        return datetime(2026, 8, 7, hour, minute, tzinfo=SHANGHAI)

    def _monitor_plan(self):
        return monitoring.apply_plan("2026-08-07", [{
            "id": "stop", "template_id": "price_breakdown", "code": "600000",
            "threshold": 9.40, "rearm_delta": 0.05, "cooldown_minutes": 5,
        }], "测试止损")

    def test_timer_and_monitor_have_independent_state_and_tokens(self):
        self._monitor_plan()
        timer = wakeup.arm_timer(
            "2026-08-07", "thread-a", self.at(10, 30).isoformat(), launch=False
        )
        monitor = wakeup.arm_monitor("2026-08-07", "thread-a", launch=False)
        self.assertEqual(timer["worker_kind"], "timer")
        self.assertFalse(timer["network_polling"])
        self.assertEqual(monitor["worker_kind"], "monitor")
        self.assertNotEqual(timer["token"], monitor["token"])
        self.assertEqual(wakeup.get_status()["delivery_mode"], "split_local_timer_and_monitor")

    def test_rearm_invalidates_previous_timer_token(self):
        first = wakeup.arm_timer("2026-08-07", "thread-a", self.at(10, 30).isoformat(), launch=False)
        second = wakeup.arm_timer("2026-08-07", "thread-a", self.at(11, 0).isoformat(), launch=False)
        self.assertNotEqual(first["token"], second["token"])
        dispatched = []
        result = wakeup.run_timer_worker(
            first["token"], now_provider=lambda: self.at(10, 30), sleep=lambda _seconds: None,
            dispatch=lambda *args: (dispatched.append(args) or (0, "ok")),
        )
        self.assertEqual(result["status"], "cancelled_or_replaced")
        self.assertEqual(dispatched, [])

    def test_timer_dispatches_once_without_market_polling(self):
        timer = wakeup.arm_timer("2026-08-07", "thread-a", self.at(10, 30).isoformat(), launch=False)
        dispatched = []
        with patch("trading_desk.market_packet.MarketPacketBuilder.monitor_snapshot") as snapshot:
            result = wakeup.run_timer_worker(
                timer["token"], now_provider=lambda: self.at(10, 30), sleep=lambda _seconds: None,
                dispatch=lambda *args: (dispatched.append(args) or (0, "ok")),
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0][2], "timer")
        snapshot.assert_not_called()

    def test_monitor_signal_dispatches_early_and_cancels_timer(self):
        self._monitor_plan()
        timer = wakeup.arm_timer("2026-08-07", "thread-a", self.at(10, 30).isoformat(), launch=False)
        monitor = wakeup.arm_monitor("2026-08-07", "thread-a", launch=False)
        dispatched = []
        result = wakeup.run_monitor_worker(
            monitor["token"], now_provider=lambda: self.at(10, 0), sleep=lambda _seconds: None,
            snapshot_provider=lambda _codes: {"600000": {"quote": {"last_price": 9.30}}},
            dispatch=lambda *args: (dispatched.append(args) or (0, "ok")),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0][2], "monitor")
        self.assertIn("600000", dispatched[0][3])
        self.assertNotEqual(wakeup.get_timer()["token"], timer["token"])
        self.assertEqual(wakeup.get_timer()["status"], "cancelled")

    def test_codex_dispatch_uses_windows_no_window_options(self):
        completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with patch("trading_desk.wakeup.shutil.which", return_value="codex.exe"), patch(
            "trading_desk.wakeup.subprocess.run", return_value=completed
        ) as run:
            code, _detail = wakeup._dispatch_prompt("2026-08-07", "thread-a", "timer")
        self.assertEqual(code, 0)
        kwargs = run.call_args.kwargs
        if wakeup.os.name == "nt":
            self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)
            self.assertIsNotNone(kwargs["startupinfo"])


if __name__ == "__main__":
    unittest.main()
