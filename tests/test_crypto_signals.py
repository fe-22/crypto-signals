import unittest
from unittest.mock import patch

import pandas as pd

import crypto_signals as cs


class IndicatorTests(unittest.TestCase):
    def test_rsi_handles_monotonic_and_flat_prices(self):
        increasing = pd.Series([1.0, 2.0, 3.0, 4.0])
        flat = pd.Series([2.0, 2.0, 2.0, 2.0])

        self.assertEqual(cs.rsi(increasing).iloc[-1], 100)
        self.assertEqual(cs.rsi(flat).iloc[-1], 50)


class BacktestTests(unittest.TestCase):
    def setUp(self):
        index = pd.date_range("2024-01-01", periods=4, freq="D")
        self.data = pd.DataFrame(
            {"close": [100.0, 110.0, 121.0, 133.1]},
            index=index,
        )

    def test_backtest_does_not_count_returns_before_execution(self):
        signals = pd.Series(1.0, index=self.data.index)
        with patch.object(cs, "positions", return_value=signals):
            report = cs.backtest(self.data, split=0.5)

        result = report["periodo completo"]
        expected_equity = (1 - 0.001) * 1.1 * 1.1
        self.assertAlmostEqual(result["estrategia_retorno_%"], (expected_equity - 1) * 100, places=1)
        self.assertEqual(result["trocas_de_posicao"], 1)

    def test_backtest_rejects_invalid_split(self):
        with self.assertRaises(ValueError):
            cs.backtest(self.data, split=1)


if __name__ == "__main__":
    unittest.main()
