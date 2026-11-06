import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import portfolio


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ, {"PORTFOLIO_DB": os.path.join(self.directory.name, "portfolio.sqlite3")}
        )
        self.environment.start()
        portfolio.initialize()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def test_fractional_position_tracks_return_and_notifies_only_once_at_target(self):
        position_id = portfolio.add_position("BTC/USDT", 0.02, 100000, 10, 5)
        notifier = Mock()
        with patch.object(
            portfolio.service, "build_current_price",
            return_value={"price": 111000, "exchange": "binance"},
        ):
            result = portfolio.refresh_positions("binance", notifier)
            repeated = portfolio.refresh_positions("binance", notifier)

        self.assertEqual(result["alerts"][0]["alert_type"], "target")
        self.assertEqual(result["positions"][0]["id"], position_id)
        self.assertAlmostEqual(result["positions"][0]["profit_loss"], 220)
        self.assertAlmostEqual(result["positions"][0]["return_pct"], 11)
        self.assertEqual(repeated["alerts"], [])
        notifier.assert_called_once()

    def test_stop_limit_alert_is_saved(self):
        portfolio.add_position("ETH/USDT", 0.5, 2000, 10, 5)
        with patch.object(
            portfolio.service, "build_current_price",
            return_value={"price": 1900, "exchange": "binance"},
        ):
            result = portfolio.refresh_positions("binance", Mock())

        self.assertEqual(result["alerts"][0]["alert_type"], "stop")
        self.assertEqual(result["positions"][0]["alert_type"], "stop")

    def test_quote_failure_is_reported_without_discarding_position(self):
        portfolio.add_position("SOL/USDT", 1.5, 100, 10, 5)
        with patch.object(
            portfolio.service, "build_current_price", side_effect=RuntimeError("exchange unavailable")
        ):
            result = portfolio.refresh_positions("binance", Mock())

        self.assertEqual(len(result["positions"]), 1)
        self.assertIn("cotação", result["positions"][0]["price_error"])
        self.assertIsNone(result["positions"][0]["current_price"])


if __name__ == "__main__":
    unittest.main()
