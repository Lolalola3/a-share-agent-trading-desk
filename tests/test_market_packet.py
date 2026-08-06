import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trading_desk import market_packet, mcp_server, state


class MarketPacketTests(unittest.TestCase):
    def test_protocol_pack_avoids_unattended_shell_reads(self):
        result = mcp_server.call_tool("analysis_protocol_get", {})
        self.assertEqual(result["prompt_workflow_version"], "5.1.0")
        self.assertIn("daily_session.md", result["prompts"])
        self.assertIn("腾讯", result["prompts"]["data_acquisition.md"])
        self.assertNotIn("daily_dispatcher.md", result["prompts"])
        self.assertIn("version", result["strategy"])

    def test_mcp_dynamic_packet_tool_avoids_shell_entrypoint(self):
        expected = {"trigger": "manual", "status": "ready"}
        with patch.object(market_packet.MarketPacketBuilder, "build", return_value=expected) as build:
            result = mcp_server.call_tool(
                "analysis_packet_get",
                {"trigger": "manual", "include_intraday": True, "persist": True},
            )
        self.assertEqual(result, expected)
        build.assert_called_once_with("manual", include_intraday=True, persist=True)

    def test_fixed_node_tools_are_not_exposed(self):
        names = {tool["name"] for tool in mcp_server.TOOLS}
        self.assertIn("analysis_runtime_poll", names)
        self.assertIn("monitor_plan_apply", names)
        self.assertNotIn("dispatch_node_claim", names)
        self.assertNotIn("node_packet_get", names)

    def test_tencent_parser_normalizes_and_calculates_fields(self):
        fields = [""] * 40
        fields[1], fields[3], fields[4], fields[5], fields[30] = "测试", "10.50", "10.00", "10.10", "20260804100000"
        raw = f'v_sh600000="{"~".join(fields)}";'.encode("gb18030")
        parsed = market_packet.parse_tencent_realtime(raw, ["600000"])
        self.assertEqual(parsed["600000"]["last_price"], 10.5)
        self.assertEqual(parsed["600000"]["previous_close"], 10.0)
        self.assertEqual(parsed["600000"]["quote_timestamp"], "2026-08-04T10:00:00+08:00")

    def test_fresh_single_source_is_tradeable(self):
        now = market_packet.shanghai_now()
        stamp = now.replace(microsecond=0).isoformat()
        primary = {"last_price": 10.0, "previous_close": 9.8, "quote_timestamp": stamp}
        ready = market_packet.MarketPacketBuilder._combined_quote("600000", primary, None, None, now)
        self.assertEqual(ready["status"], "primary_ready")
        self.assertTrue(ready["tradeable"])

        stale = {**primary, "quote_timestamp": (now - market_packet.timedelta(seconds=91)).isoformat()}
        rejected = market_packet.MarketPacketBuilder._combined_quote("600000", stale, None, None, now)
        self.assertEqual(rejected["status"], "stale_or_invalid")
        self.assertFalse(rejected["tradeable"])

    def test_secondary_latest_is_not_called_when_tencent_is_compliant(self):
        now = market_packet.shanghai_now().replace(microsecond=0)

        class PrimaryFirstBuilder(market_packet.MarketPacketBuilder):
            secondary_calls = 0

            def _fetch_tencent_latest(self, codes):
                return {code: {"code": code, "name": "测试", "last_price": 10.0, "previous_close": 9.8, "quote_timestamp": now.isoformat(), "source": "腾讯实时行情"} for code in codes}

            def _fetch_eastmoney_latest(self, codes):
                self.secondary_calls += 1
                return {}

            def _fetch_tencent_kline(self, code):
                return []

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH)
            try:
                state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR = root, root / "state", root / "records", root / "journal"
                state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH = state.STATE_DIR / "account.json", state.STATE_DIR / "watchlist.json", state.STATE_DIR / "settings.json"
                state.initialize("2026-08-05", 1000, [{"code": "600000", "name": "测试", "shares": 100, "cost": 10}])
                builder = PrimaryFirstBuilder(cache_path=state.STATE_DIR / "market_cache.json")
                packet = builder.build("10:30", include_intraday=False, persist=False)
                self.assertEqual(builder.secondary_calls, 0)
                self.assertEqual(packet["source_health"][1]["status"], "disabled")
                self.assertTrue(packet["instruments"][0]["quote"]["tradeable"])
            finally:
                (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH) = original

    def test_tencent_intraday_keeps_a_sampled_path_and_local_features(self):
        payload = {"data": {"sh600000": {"data": {"date": "20260806", "data": [
            "0930 10.00 100 100000.00",
            "0931 10.10 220 221200.00",
            "0932 10.20 350 353800.00",
        ]}}}}
        parsed = market_packet.parse_tencent_intraday(payload, "600000")
        self.assertEqual(parsed["points_count"], 3)
        self.assertEqual(parsed["sampled_path"][-1]["price"], 10.2)
        self.assertAlmostEqual(parsed["features"]["vwap_price"], 10.1086, places=4)
        self.assertEqual(parsed["features"]["high_time"], "20260806 0932")

    def test_daily_kline_generates_trader_features_locally(self):
        rows = []
        for index in range(70):
            close = 10 + index * 0.1
            rows.append([f"2026-05-{index + 1:02d}", str(close - 0.05), str(close), str(close + 0.1), str(close - 0.1), str(1000 + index)])
        payload = {"data": {"sh600000": {"qfqday": rows}}}
        bars = market_packet.parse_tencent_kline(payload, "600000")
        quote = {"last_price": 17.0, "open_price": 16.8, "high_price": 17.1, "low_price": 16.7, "volume_lots": 500}
        features = market_packet._technical_features(bars, quote, market_packet.datetime(2026, 8, 6, 10, 30, tzinfo=market_packet.SHANGHAI))
        self.assertEqual(len(bars), 70)
        self.assertIsNotNone(features["ma60"])
        self.assertIsNotNone(features["atr14"])
        self.assertEqual(features["ma_alignment"], "多头排列")

    def test_eastmoney_batch_parser_keeps_the_requested_codes_only(self):
        payload = {"data": {"diff": [
            {"f12": "600000", "f14": "测试", "f2": 10.1, "f18": 10, "f17": 10, "f15": 10.2, "f16": 9.9, "f5": 100, "f6": 1000, "f124": 1785828876},
            {"f12": "300000", "f14": "不在范围", "f2": 20, "f18": 19},
        ]}}
        parsed = market_packet.parse_eastmoney_realtime(payload, ["600000"])
        self.assertEqual(set(parsed), {"600000"})
        self.assertEqual(parsed["600000"]["last_price"], 10.1)
        self.assertEqual(parsed["600000"]["source"], "东方财富实时行情")

    def test_packet_keeps_state_read_only_and_uses_cache_when_sources_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH)
            try:
                state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR = root, root / "state", root / "records", root / "journal"
                state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH = state.STATE_DIR / "account.json", state.STATE_DIR / "watchlist.json", state.STATE_DIR / "settings.json"
                state.initialize("2026-08-04", 1000, [{"code": "600000", "name": "测试", "shares": 100, "cost": 10}])
                state._write_json(state.WATCHLIST_PATH, {**state.get_watchlist(), "candidates": []})
                cache = state.STATE_DIR / "market_cache.json"
                cache.write_text(json.dumps({"quotes": {"600000": {"last_price": 10.0, "previous_close": 9.8, "quote_timestamp": None}}, "intraday": {}}, ensure_ascii=False), encoding="utf-8")

                def failing_fetcher(_url, _timeout):
                    raise market_packet.MarketDataError("offline")

                packet = market_packet.MarketPacketBuilder(failing_fetcher, cache).build("09:08", include_intraday=False)
                self.assertEqual(packet["instruments"][0]["quote"]["status"], "cached")
                self.assertFalse(packet["instruments"][0]["quote"]["tradeable"])
                self.assertEqual(state.get_account()["positions"][0]["shares"], 100)
            finally:
                (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH) = original

    def test_ephemeral_packet_does_not_write_cache_or_packet_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH)
            try:
                state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR = root, root / "state", root / "records", root / "journal"
                state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH = state.STATE_DIR / "account.json", state.STATE_DIR / "watchlist.json", state.STATE_DIR / "settings.json"
                state.initialize("2026-08-04", 1000, [{"code": "600000", "name": "测试", "shares": 100, "cost": 10}])

                def failing_fetcher(_url, _timeout):
                    raise market_packet.MarketDataError("offline")

                packet = market_packet.MarketPacketBuilder(failing_fetcher).build("09:08", include_intraday=False, persist=False)
                self.assertTrue(packet["ephemeral"])
                self.assertIsNone(packet["packet_path"])
                self.assertFalse((state.STATE_DIR / "market_cache.json").exists())
                self.assertFalse((state.RECORDS_DIR / "market_packets").exists())
            finally:
                (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH) = original

    def test_sector_proxy_uses_audited_membership_and_tencent_quotes(self):
        now = market_packet.shanghai_now().replace(microsecond=0)

        class TencentOnlyBuilder(market_packet.MarketPacketBuilder):
            def _fetch_tencent_latest(self, codes):
                result = {}
                for index, requested in enumerate(codes):
                    code = requested[2:] if requested.startswith(("sh", "sz")) else requested
                    result[code] = {
                        "code": code, "name": code, "last_price": 10 + index / 10,
                        "previous_close": 10, "quote_timestamp": now.isoformat(),
                        "amount_cny": 1_000_000 + index, "volume_lots": 1000,
                        "source": "腾讯实时行情",
                    }
                return result

            def _fetch_tencent_kline(self, code):
                return []

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH)
            try:
                state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR = root, root / "state", root / "records", root / "journal"
                state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH = state.STATE_DIR / "account.json", state.STATE_DIR / "watchlist.json", state.STATE_DIR / "settings.json"
                state.initialize(now.date().isoformat(), 1000, [{"code": "600000", "name": "测试", "shares": 100, "cost": 10, "sector": "银行"}])
                state.update_sector_universe(
                    [{"name": "银行", "constituents": [{"code": "600000"}, {"code": "601398"}, {"code": "000001"}]}],
                    [{"name": "人工核验源", "captured_at": now.isoformat(), "data_as_of": now.date().isoformat(), "status": "online", "completeness_ratio": 1.0, "consecutive_successes": 2}],
                    now.date().isoformat(), now.date().isoformat(), "测试",
                )
                packet = TencentOnlyBuilder(cache_path=state.STATE_DIR / "market_cache.json").build("manual", include_intraday=False, persist=False)
                sector = packet["sector_context"]["sectors"][0]
                self.assertEqual(packet["source_policy"]["intraday_primary"], "腾讯")
                self.assertEqual(packet["source_health"][1]["status"], "disabled")
                self.assertEqual(sector["status"], "ready")
                self.assertEqual(sector["fresh_tencent_quotes"], 3)
                self.assertEqual(sector["tracked_stock_ranks"][0]["code"], "600000")
            finally:
                (state.ROOT, state.STATE_DIR, state.RECORDS_DIR, state.JOURNAL_DIR, state.ACCOUNT_PATH, state.WATCHLIST_PATH, state.SETTINGS_PATH) = original


if __name__ == "__main__":
    unittest.main()
