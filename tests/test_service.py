import unittest
from unittest.mock import Mock, patch

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
