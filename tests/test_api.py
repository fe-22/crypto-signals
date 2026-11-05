import unittest
from unittest.mock import patch

from fastapi.responses import FileResponse
from fastapi import HTTPException
import requests

import api


class HomepageTests(unittest.TestCase):
    def test_market_data_routes_are_public_but_monitor_routes_remain_protected(self):
        routes = {route.path: route for route in api.app.routes if hasattr(route, "dependant")}

        for path in ("/signal", "/opportunities", "/backtest", "/currencies", "/macro", "/market-history"):
            dependencies = {item.call for item in routes[path].dependant.dependencies}
            self.assertNotIn(api.auth, dependencies, path)

        for path in ("/status", "/monitor/start", "/monitor/stop"):
            dependencies = {item.call for item in routes[path].dependant.dependencies}
            self.assertIn(api.auth, dependencies, path)

    def test_homepage_serves_project_artwork_page(self):
        response = api.root()

        self.assertIsInstance(response, FileResponse)
        self.assertTrue(response.path.is_file())
        self.assertIn("/assets/crypto-signals-banner.png", response.path.read_text(encoding="utf-8"))

    def test_info_endpoint_preserves_api_metadata(self):
        self.assertEqual(api.info()["docs"], "/docs")
        self.assertIn("/signal", api.info()["endpoints"])

    def test_watchlist_rejects_invalid_assets_and_timeframes(self):
        with self.assertRaises(ValueError):
            api._validated_watchlist("BTCUSDT:4h", "binance")
        with self.assertRaises(ValueError):
            api._validated_watchlist("BTC/USDT:2m", "binance")

    def test_monitor_can_start_and_stop_from_dashboard(self):
        def wait_until_stopped(stop_event, items, exchange):
            stop_event.wait(2)

        with patch.object(api.service, "monitor_loop", side_effect=wait_until_stopped):
            started = api.start_monitor(api.MonitorRequest(watchlist="BTC/USDT:4h", exchange="binance"))
            self.assertTrue(started["monitor_enabled"])
            stopped = api.stop_monitor()

        self.assertTrue(stopped["monitor_stopped"])
        self.assertFalse(stopped["monitor_enabled"])

    def test_monitor_rejects_invalid_watchlist(self):
        with self.assertRaises(HTTPException) as error:
            api.start_monitor(api.MonitorRequest(watchlist="BTCUSDT:4h", exchange="binance"))
        self.assertEqual(error.exception.status_code, 422)

    def test_currency_endpoint_returns_rates_without_api_key(self):
        expected = {"rates": {"USD": {"brl_per_unit": 5.2}}}
        with patch.object(api.service, "build_currency_rates", return_value=expected):
            self.assertEqual(api.currencies(), expected)

    def test_currency_endpoint_reports_provider_failure(self):
        with patch.object(api.service, "build_currency_rates", side_effect=requests.Timeout):
            with self.assertRaises(HTTPException) as error:
                api.currencies()
        self.assertEqual(error.exception.status_code, 502)

    def test_macro_endpoint_returns_indicators_without_api_key(self):
        expected = {"selic": {"annual_rate_pct": 15.0}, "fuel_alert": {"status": "SEM_PRESSAO_FORTE"}}
        with patch.object(api.service, "build_macro_indicators", return_value=expected):
            self.assertEqual(api.macro(), expected)

    def test_market_history_uses_validated_asset_and_parameters(self):
        expected = {"symbol": "ETH/USDT", "points": []}
        with patch.object(api.service, "build_price_history", return_value=expected) as history:
            self.assertEqual(api.market_history("ETH/USDT", "1d", "kraken"), expected)
        history.assert_called_once_with("ETH/USDT", "1d", "kraken")

    def test_market_history_rejects_invalid_assets(self):
        with self.assertRaises(HTTPException) as error:
            api.market_history("ETHUSDT", "1d", "binance")
        self.assertEqual(error.exception.status_code, 422)

    def test_opportunities_endpoint_uses_validated_timeframe_and_exchange(self):
        expected = {"assets": [], "candidates": []}
        with patch.object(api.service, "scan_opportunities", return_value=expected) as scan:
            self.assertEqual(api.opportunities(tf="1d", exchange="binance"), expected)
        scan.assert_called_once_with("1d", "binance")

    def test_opportunities_endpoint_rejects_unsupported_timeframe(self):
        with self.assertRaises(HTTPException) as error:
            api.opportunities(tf="2m", exchange="binance")
        self.assertEqual(error.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
