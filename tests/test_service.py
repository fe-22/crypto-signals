import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys

import pandas as pd

import service


class ExchangeFallbackTests(unittest.TestCase):
    def test_restricted_location_automatically_uses_another_exchange(self):
        attempts = []

        def fetch(symbol, exchange):
            attempts.append(exchange)
            if exchange == "binance":
                raise RuntimeError("Service unavailable from a restricted location")
            return {"symbol": symbol, "exchange": exchange}

        with patch.object(service, "EXCHANGE_FALLBACKS", ("binance", "kraken", "bybit")):
            data, exchange = service._fetch_market_data(fetch, "BTC/USDT", exchange="binance")

        self.assertEqual(attempts, ["binance", "kraken"])
        self.assertEqual(exchange, "kraken")
        self.assertEqual(data["exchange"], "kraken")

    def test_other_errors_are_not_retried_on_different_exchanges(self):
        fetch = Mock(side_effect=RuntimeError("network timeout"))

        with patch.object(service, "EXCHANGE_FALLBACKS", ("binance", "kraken")):
            with self.assertRaisesRegex(RuntimeError, "network timeout"):
                service._fetch_market_data(fetch, "BTC/USDT", exchange="binance")

        fetch.assert_called_once_with("BTC/USDT", "binance")


class CurrencyRateTests(unittest.TestCase):
    def test_rates_are_converted_from_brl_to_foreign_currency_prices(self):
        response = Mock()
        response.json.return_value = {
            "base": "BRL",
            "date": "2026-10-30",
            "rates": {"USD": 0.2, "EUR": 0.25, "GBP": 0.04},
        }
        with patch.object(service, "_cached", side_effect=lambda key, ttl, fn: fn()):
            with patch("service.requests.get", return_value=response) as get:
                result = service.build_currency_rates()

        get.assert_called_once_with(
            "https://api.frankfurter.app/latest",
            params={"from": "BRL", "to": "USD,EUR,GBP"},
            timeout=10,
        )
        self.assertEqual(result["rates"]["USD"]["brl_per_unit"], 5)
        self.assertEqual(result["rates"]["EUR"]["brl_per_unit"], 4)
        self.assertEqual(result["rates"]["GBP"]["brl_per_unit"], 25)
        self.assertEqual(result["date"], "2026-10-30")

    def test_rejects_missing_or_invalid_currency_quotes(self):
        response = Mock()
        response.json.return_value = {
            "base": "BRL",
            "date": "2026-10-30",
            "rates": {"USD": 0.2, "EUR": 0, "GBP": 0.04},
        }
        with patch.object(service, "_cached", side_effect=lambda key, ttl, fn: fn()):
            with patch("service.requests.get", return_value=response):
                with self.assertRaisesRegex(ValueError, "EUR"):
                    service.build_currency_rates()


class MarketHistoryTests(unittest.TestCase):
    def test_price_history_excludes_the_current_open_candle(self):
        index = pd.date_range("2026-10-01", periods=3, freq="D")
        candles = pd.DataFrame({"close": [100.0, 110.0, 999.0]}, index=index)
        with patch.object(service, "_cached", side_effect=lambda key, ttl, fn: fn()):
            with patch.object(service, "_fetch_market_data", return_value=(candles, "binance")):
                result = service.build_price_history("BTC/USDT", "1d", "binance")

        self.assertEqual([point["price"] for point in result["points"]], [100.0, 110.0])
        self.assertEqual(result["exchange"], "binance")


class CurrentPriceTests(unittest.TestCase):
    def test_current_price_uses_the_exchange_ticker(self):
        exchange = Mock()
        exchange.fetch_ticker.return_value = {"last": 12.5}
        ccxt = SimpleNamespace(binance=Mock(return_value=exchange))

        with patch.dict(sys.modules, {"ccxt": ccxt}):
            result = service.build_current_price("BTC/USDT", "binance")

        exchange.fetch_ticker.assert_called_once_with("BTC/USDT")
        self.assertEqual(result["price"], 12.5)
        self.assertEqual(result["exchange"], "binance")

    def test_current_price_rejects_missing_or_non_positive_ticker(self):
        exchange = Mock()
        exchange.fetch_ticker.return_value = {"last": 0}
        ccxt = SimpleNamespace(binance=Mock(return_value=exchange))

        with patch.dict(sys.modules, {"ccxt": ccxt}):
            with self.assertRaisesRegex(ValueError, "cotacao invalida"):
                service.build_current_price("BTC/USDT", "binance")


class PurchaseQuoteTests(unittest.TestCase):
    def test_purchase_quote_converts_fractional_usdt_amount_to_brl(self):
        rates = {
            "date": "2026-11-05",
            "rates": {"USD": {"brl_per_unit": 5.2}},
        }
        with patch.object(
            service, "build_current_price",
            return_value={"price": 100000, "exchange": "binance"},
        ):
            with patch.object(service, "build_currency_rates", return_value=rates):
                result = service.build_purchase_quote("BTC/USDT", 0.002, "binance")

        self.assertEqual(result["unit_price"], 100000)
        self.assertAlmostEqual(result["total_quote"], 200)
        self.assertAlmostEqual(result["brl_per_quote_unit"], 5.2)
        self.assertAlmostEqual(result["estimated_total_brl"], 1040)
        self.assertEqual(result["exchange_rate_date"], "2026-11-05")
        self.assertIn("aproximada", result["conversion_note"])

    def test_purchase_quote_uses_brl_price_without_external_currency_rate(self):
        with patch.object(
            service, "build_current_price",
            return_value={"price": 50, "exchange": "binance"},
        ):
            with patch.object(service, "build_currency_rates") as rates:
                result = service.build_purchase_quote("ETH/BRL", 0.5, "binance")

        self.assertEqual(result["estimated_total_brl"], 25)
        self.assertIsNone(result["conversion_note"])
        rates.assert_not_called()

    def test_purchase_quote_rejects_unsupported_quote_currency(self):
        with patch.object(
            service, "build_current_price",
            return_value={"price": 1, "exchange": "binance"},
        ):
            with self.assertRaisesRegex(ValueError, "conversao para reais"):
                service.build_purchase_quote("BTC/USDC", 1, "binance")


class InvestmentSimulationTests(unittest.TestCase):
    def test_simulation_uses_historical_windows_matching_requested_term(self):
        closes = [100 * (1.001 ** index) for index in range(500)]
        history = pd.DataFrame(
            {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1.0] * len(closes)},
            index=pd.date_range("2025-01-01", periods=len(closes), freq="D"),
        )
        with patch.object(service, "_cached", side_effect=lambda key, ttl, fn: fn()):
            with patch.object(service, "_fetch_market_data", return_value=(history, "binance")) as fetch:
                with patch.object(
                    service, "build_current_price",
                    return_value={"price": 200, "exchange": "binance"},
                ):
                    with patch.object(
                        service.cs, "analyze",
                        return_value=pd.DataFrame({"signal": ["COMPRAR"], "score": [4], "rsi": [55.0]}),
                    ):
                        with patch.object(service.cs, "explain", return_value="indicadores favoraveis"):
                            result = service.simulate_investment("BTC/USDT", 0.05, 30, "binance")

        fetch.assert_called_once()
        self.assertEqual(result["historical_windows"], 469)
        self.assertEqual(result["estimated_investment"], 10)
        self.assertAlmostEqual(result["scenarios"]["median"]["return_pct"], 3.04, places=1)
        self.assertAlmostEqual(result["scenarios"]["median"]["estimated_profit"], 0.3, places=2)
        self.assertEqual(result["quote_currency"], "USDT")
        self.assertEqual(result["technical_guidance"]["action"], "COMPRAR")
        self.assertEqual(result["technical_guidance"]["score"], 4)

    def test_simulation_guidance_explains_wait_and_existing_position_caution(self):
        history = pd.DataFrame(
            {"open": [100.0] * 500, "high": [100.0] * 500, "low": [100.0] * 500,
             "close": [100.0] * 500, "volume": [1.0] * 500},
            index=pd.date_range("2025-01-01", periods=500, freq="D"),
        )
        expected = {
            "AGUARDAR": "Aguardar; sem indicação de compra",
            "EVITAR": "Não comprar · sinal de venda se já possui",
        }
        for signal, title in expected.items():
            with self.subTest(signal=signal):
                with patch.object(service, "_cached", side_effect=lambda key, ttl, fn: fn()):
                    with patch.object(service, "_fetch_market_data", return_value=(history, "binance")):
                        with patch.object(
                            service, "build_current_price",
                            return_value={"price": 100, "exchange": "binance"},
                        ):
                            with patch.object(
                                service.cs, "analyze",
                                return_value=pd.DataFrame({"signal": [signal], "score": [0], "rsi": [50.0]}),
                            ):
                                with patch.object(service.cs, "explain", return_value="sem confirmacao"):
                                    result = service.simulate_investment("BTC/USDT", 0.01, 30, "binance")

                self.assertEqual(result["technical_guidance"]["title"], title)

    def test_simulation_requires_sufficient_history_for_selected_term(self):
        history = pd.DataFrame(
            {"close": [100.0] * 60},
            index=pd.date_range("2025-01-01", periods=60, freq="D"),
        )
        with patch.object(service, "_cached", side_effect=lambda key, ttl, fn: fn()):
            with patch.object(service, "_fetch_market_data", return_value=(history, "binance")):
                with self.assertRaisesRegex(ValueError, "historico insuficiente"):
                    service.simulate_investment("BTC/USDT", 0.01, 90, "binance")


class BacktestResourceLimitTests(unittest.TestCase):
    def test_rejects_backtest_that_exceeds_candle_budget_before_fetching_history(self):
        with patch.object(service, "_fetch_market_data") as fetch:
            with self.assertRaisesRegex(ValueError, "limite de 50000 candles"):
                service.run_backtest("BTC/USDT", "15m", 8, "trend", "binance")

        fetch.assert_not_called()

    def test_allows_supported_backtest_within_candle_budget(self):
        history = pd.DataFrame(
            {"close": [100.0] * 1000, "open": [100.0] * 1000, "high": [101.0] * 1000,
             "low": [99.0] * 1000, "volume": [1.0] * 1000},
            index=pd.date_range("2025-01-01", periods=1000, freq="D"),
        )
        analyzed = pd.DataFrame({"close": [100.0, 101.0]})
        with patch.object(service, "_fetch_market_data", return_value=(history, "binance")):
            with patch.object(service.cs, "analyze", return_value=analyzed):
                with patch.object(service.cs, "backtest", return_value={"result": {}}) as backtest:
                    result = service.run_backtest("BTC/USDT", "1d", 5, "trend", "binance")

        backtest.assert_called_once()
        self.assertEqual(result["exchange"], "binance")

    def test_rejects_concurrent_backtest_instead_of_queueing(self):
        busy_slot = Mock()
        busy_slot.acquire.return_value = False
        with patch.object(service, "_backtest_slot", busy_slot):
            with self.assertRaisesRegex(service.BacktestBusyError, "backtest ocupado"):
                service.run_backtest("BTC/USDT", "1d", 1, "trend", "binance")

        busy_slot.acquire.assert_called_once_with(blocking=False)
        busy_slot.release.assert_not_called()


class MacroIndicatorTests(unittest.TestCase):
    def test_macro_indicators_warn_when_brent_cost_in_reais_rises(self):
        today = datetime.now(timezone.utc).date()
        date_40 = (today - timedelta(days=40)).isoformat()
        date_25 = (today - timedelta(days=25)).isoformat()
        date_2 = (today - timedelta(days=2)).isoformat()
        brent = [
            {"date": date_40, "brent_usd": 80.0},
            {"date": date_25, "brent_usd": 88.0},
            {"date": date_2, "brent_usd": 100.0},
        ]
        usd_brl = {date_40: 5.0, date_25: 5.1, date_2: 5.2}
        with patch.object(service, "_cached", side_effect=lambda key, ttl, fn: fn()):
            with patch.object(service, "_fetch_selic_target", return_value={"annual_rate_pct": 15.0, "date": date_2}):
                with patch.object(service, "_fetch_brent_prices", return_value=brent):
                    with patch.object(service, "_fetch_usd_brl_history", return_value=usd_brl):
                        result = service.build_macro_indicators()

        self.assertEqual(result["selic"]["annual_rate_pct"], 15.0)
        self.assertEqual(result["usd_brl"]["brl_per_usd"], 5.2)
        self.assertEqual(result["fuel_alert"]["status"], "ATENCAO")
        self.assertGreater(result["comparison"]["brent_brl_change_30d_pct"], 5)


class OpportunityScanTests(unittest.TestCase):
    def test_scan_ranks_buy_signals_and_explains_each_asset(self):
        def fake_signal(symbol, timeframe, exchange):
            score = 5 if symbol == "BTC/USDT" else 2
            signal = "COMPRAR" if score >= 4 else "AGUARDAR"
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "exchange": exchange,
                "signal": signal,
                "score": score,
                "trend_following": "EM_TENDENCIA" if symbol == "BTC/USDT" else "FORA",
                "volatility_extreme": False,
                "rsi": 55.0,
                "price": 100.0,
                "reasons": "EMA e momentum analisados",
            }

        with patch.object(service, "OPPORTUNITY_SYMBOLS", ("BTC/USDT", "ETH/USDT")):
            with patch.object(service, "build_signal", side_effect=fake_signal):
                result = service.scan_opportunities("4h", "binance")

        self.assertEqual(result["candidates"], ["BTC/USDT"])
        self.assertEqual(result["assets"][0]["symbol"], "BTC/USDT")
        self.assertTrue(any("score tecnico" in item.lower() for item in result["assets"][0]["rationale"]))
        self.assertEqual(len(result["assets"]), 2)
        self.assertIn("nao e recomendacao", result["disclaimer"].lower())

    def test_scan_reports_partial_data_failures(self):
        def signal_or_fail(symbol, timeframe, exchange):
            if symbol == "ETH/USDT":
                raise RuntimeError("network detail")
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "exchange": exchange,
                "signal": "AGUARDAR",
                "score": 1,
                "trend_following": "FORA",
                "volatility_extreme": False,
                "rsi": 50.0,
                "price": 100.0,
                "reasons": "sem confirmacao",
            }

        with patch.object(service, "OPPORTUNITY_SYMBOLS", ("BTC/USDT", "ETH/USDT")):
            with patch.object(service, "build_signal", side_effect=signal_or_fail):
                result = service.scan_opportunities("4h", "binance")

        self.assertEqual(result["symbols_analyzed"], 1)
        self.assertEqual(result["unavailable"][0]["symbol"], "ETH/USDT")

    def test_scan_fails_when_no_assets_can_be_analyzed(self):
        with patch.object(service, "OPPORTUNITY_SYMBOLS", ("BTC/USDT",)):
            with patch.object(service, "build_signal", side_effect=RuntimeError("network")):
                with self.assertRaisesRegex(RuntimeError, "nenhum ativo"):
                    service.scan_opportunities("4h", "binance")


if __name__ == "__main__":
    unittest.main()
