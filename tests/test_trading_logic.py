import unittest

from trading_desk import trading_logic


class TradingLogicTests(unittest.TestCase):
    def test_exit_levels_are_numeric_and_fixed(self):
        levels = trading_logic.exit_levels(100)
        self.assertEqual(levels["hard_stop"], 94.0)
        self.assertEqual(levels["take_profit_1"], 108.0)
        self.assertEqual(levels["take_profit_2"], 112.0)
        self.assertEqual(levels["trailing_activation"], 108.0)

    def test_entry_requires_all_hard_conditions(self):
        snapshot = {
            "candidate_score": 70, "technical_confirmation": True,
            "change_pct": 2, "price_vs_ma20": 1.01, "volume_ratio": 1.2,
            "sector_passed": True, "at_limit_up": False, "data_complete": True,
        }
        self.assertTrue(trading_logic.entry_check(snapshot)["allowed"])
        snapshot["change_pct"] = 6
        self.assertFalse(trading_logic.entry_check(snapshot)["allowed"])

    def test_low_buy_high_sell_t_requires_old_sellable_shares(self):
        position = {"shares": 1000, "sellable_shares": 400}
        low = trading_logic.t_low_buy_check(position, 100.5, 100, True)
        self.assertTrue(low["allowed"])
        self.assertEqual(low["buy_shares"], 300)
        high = trading_logic.t_high_sell_check(100.5, 102.51, low["buy_shares"])
        self.assertTrue(high["allowed"])
        self.assertEqual(high["sell_shares"], 300)
        position["sellable_shares"] = 0
        self.assertFalse(trading_logic.t_low_buy_check(position, 100.5, 100, True)["allowed"])

    def test_high_sell_low_buy_t_is_limited_to_sellable_old_shares(self):
        result = trading_logic.t_sell_first_check(
            {"shares": 1000, "sellable_shares": 400}, 102, 100, 500
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["sell_shares"], 300)
        self.assertEqual(result["max_rebuy_shares"], 300)

    def test_position_size_obeys_one_percent_risk_and_lot(self):
        self.assertEqual(trading_logic.position_size_by_risk(10000, 10), 100)

    def test_update_gate_requires_sample_and_validation(self):
        self.assertFalse(trading_logic.update_gate(19, True, 0.0)["allowed"])
        self.assertTrue(trading_logic.update_gate(20, True, 0.1)["allowed"])
        self.assertFalse(trading_logic.update_gate(20, False, 0.1)["allowed"])


if __name__ == "__main__":
    unittest.main()
