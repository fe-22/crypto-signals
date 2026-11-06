import asyncio
import os
from http.cookies import SimpleCookie
import unittest
import tempfile
from unittest.mock import Mock, patch

from fastapi.responses import FileResponse
from fastapi import HTTPException, Response
from starlette.requests import Request
import requests

import api


def make_request(path="/", cookie=None):
    headers = [(b"cookie", cookie.encode("ascii"))] if cookie else []
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
    })


class HomepageTests(unittest.TestCase):
    def test_market_data_routes_are_public_but_monitor_routes_remain_protected(self):
        routes = {route.path: route for route in api.app.routes if hasattr(route, "dependant")}

        for path in ("/signal", "/opportunities", "/simulation", "/backtest", "/currencies", "/macro", "/market-history"):
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

    def test_simulation_validates_inputs_and_uses_selected_exchange(self):
        expected = {"symbol": "BTC/USDT", "days": 30, "scenarios": {}}
        request = api.SimulationRequest(
            symbol="btc/usdt", quantity=0.01, days=30, exchange="kraken"
        )
        with patch.object(api.service, "simulate_investment", return_value=expected) as simulate:
            self.assertEqual(api.simulation(request), expected)
        simulate.assert_called_once_with("BTC/USDT", 0.01, 30, "kraken")

        with self.assertRaises(HTTPException) as invalid_term:
            api.simulation(api.SimulationRequest(
                symbol="BTC/USDT", quantity=0.01, days=45, exchange="binance"
            ))
        self.assertEqual(invalid_term.exception.status_code, 422)

        with self.assertRaises(HTTPException) as invalid_symbol:
            api.simulation(api.SimulationRequest(
                symbol="BTCUSDT", quantity=0.01, days=30, exchange="binance"
            ))
        self.assertEqual(invalid_symbol.exception.status_code, 422)

    def test_market_history_rejects_invalid_assets(self):
        with self.assertRaises(HTTPException) as error:
            api.market_history("ETHUSDT", "1d", "binance")
        self.assertEqual(error.exception.status_code, 422)

    def test_backtest_rejects_high_memory_timeframe_and_range(self):
        with self.assertRaises(HTTPException) as error:
            api.backtest(symbol="BTC/USDT", tf="15m", years=8, exchange="binance")

        self.assertEqual(error.exception.status_code, 422)
        self.assertIn("reduza o prazo", error.exception.detail)

    def test_backtest_returns_service_unavailable_when_another_is_running(self):
        busy_slot = Mock()
        busy_slot.acquire.return_value = False
        with patch.object(api.service, "_backtest_slot", busy_slot):
            with self.assertRaises(HTTPException) as error:
                api.backtest(symbol="BTC/USDT", tf="1d", years=1, exchange="binance")

        self.assertEqual(error.exception.status_code, 503)
        self.assertIn("tente novamente", error.exception.detail)

    def test_portfolio_routes_require_admin_session(self):
        routes = {route.path: route for route in api.app.routes if hasattr(route, "dependant")}

        for path in (
            "/admin/portfolio", "/admin/portfolio/quote", "/admin/portfolio/refresh",
            "/admin/portfolio/{position_id}",
        ):
            dependencies = {item.call for item in routes[path].dependant.dependencies}
            self.assertIn(api.require_admin, dependencies, path)

    def test_portfolio_quote_validates_symbol_and_quantity(self):
        expected = {"symbol": "BTC/USDT", "estimated_total_brl": 520}
        with patch.object(api.service, "build_purchase_quote", return_value=expected) as quote:
            self.assertEqual(api.quote_portfolio_position("btc/usdt", 0.001), expected)
        quote.assert_called_once_with("BTC/USDT", 0.001, api.DEFAULT_EXCHANGE)

        with self.assertRaises(HTTPException) as invalid_quantity:
            api.quote_portfolio_position("BTC/USDT", 0)
        self.assertEqual(invalid_quantity.exception.status_code, 422)

        with self.assertRaises(HTTPException) as invalid_symbol:
            api.quote_portfolio_position("BTCUSDT", 0.01)
        self.assertEqual(invalid_symbol.exception.status_code, 422)

    def test_portfolio_rejects_invalid_symbols_and_non_finite_amounts(self):
        with self.assertRaises(HTTPException) as invalid_symbol:
            api.create_portfolio_position(
                api.PortfolioPositionRequest(
                    symbol="BTCUSDT", quantity=0.1, purchase_price=100, target_profit_pct=10,
                    stop_loss_pct=5,
                )
            )
        self.assertEqual(invalid_symbol.exception.status_code, 422)

        with self.assertRaises(HTTPException) as invalid_amount:
            api.create_portfolio_position(
                api.PortfolioPositionRequest(
                    symbol="BTC/USDT", quantity=float("inf"), purchase_price=100,
                    target_profit_pct=10, stop_loss_pct=5,
                )
            )
        self.assertEqual(invalid_amount.exception.status_code, 422)

    def test_portfolio_routes_store_refresh_and_remove_a_fractional_position(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"PORTFOLIO_DB": os.path.join(directory, "portfolio.sqlite3")}):
                position = api.create_portfolio_position(
                    api.PortfolioPositionRequest(
                        symbol="btc/usdt", quantity=0.005, purchase_price=50000,
                        target_profit_pct=12, stop_loss_pct=6,
                    )
                )["position"]
                with patch.object(
                    api.service, "build_current_price",
                    return_value={"price": 51000, "exchange": "binance"},
                ):
                    refreshed = api.refresh_admin_portfolio()
                self.assertEqual(refreshed["positions"][0]["id"], position["id"])
                self.assertAlmostEqual(refreshed["positions"][0]["profit_loss"], 5)
                self.assertTrue(api.delete_portfolio_position(position["id"])["deleted"])
                self.assertEqual(api.admin_portfolio()["positions"], [])

    def test_lifespan_starts_and_stops_portfolio_monitor(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {
                "PORTFOLIO_DB": os.path.join(directory, "portfolio.sqlite3"),
                "ENABLE_MONITOR": "0",
            }):
                async def run_lifespan():
                    async with api.app.router.lifespan_context(api.app):
                        self.assertTrue(api._portfolio_monitor_thread.is_alive())

                asyncio.run(run_lifespan())

        self.assertIsNone(api._portfolio_monitor_thread)

    def test_opportunities_endpoint_uses_validated_timeframe_and_exchange(self):
        expected = {"assets": [], "candidates": []}
        with patch.object(api.service, "scan_opportunities", return_value=expected) as scan:
            self.assertEqual(api.opportunities(tf="1d", exchange="binance"), expected)
        scan.assert_called_once_with("1d", "binance")

    def test_opportunities_endpoint_rejects_unsupported_timeframe(self):
        with self.assertRaises(HTTPException) as error:
            api.opportunities(tf="2m", exchange="binance")
        self.assertEqual(error.exception.status_code, 422)

    def test_manager_access_log_requires_password_and_records_homepage_visit(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"ACCESS_LOG_DB": f"{directory}\\access.sqlite3"}):
                with patch.object(api, "ADMIN_PASSWORD", "long-test-password"):
                    async def next_response(_):
                        return Response()

                    asyncio.run(api.record_homepage_visits(make_request(), next_response))
                    with self.assertRaises(HTTPException) as error:
                        api.require_admin(make_request())
                    self.assertEqual(error.exception.status_code, 401)

                    login_response = Response()
                    api.admin_login(
                        api.AdminLoginRequest(password="long-test-password"),
                        login_response,
                        make_request("/admin/login"),
                    )
                    cookie = SimpleCookie()
                    cookie.load(login_response.headers["set-cookie"])
                    session = f"admin_session={cookie['admin_session'].value}"
                    authenticated_request = make_request("/admin/accesses", session)
                    api.require_admin(authenticated_request)
                    accesses = api.admin_accesses()

        self.assertEqual(len(accesses["accesses"]), 1)
        self.assertEqual(accesses["accesses"][0]["ip_address"], "testclient")

    def test_manager_login_is_disabled_without_configured_password(self):
        with patch.object(api, "ADMIN_PASSWORD", None):
            with self.assertRaises(HTTPException) as error:
                api.admin_login(
                    api.AdminLoginRequest(password="anything"),
                    Response(),
                    make_request("/admin/login"),
                )
        self.assertEqual(error.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
