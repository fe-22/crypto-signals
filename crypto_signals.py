#!/usr/bin/env python3
"""
Sistema de apoio a investimentos em criptomoedas
================================================
Indica momentos FAVORAVEIS e DESFAVORAVEIS para entrar, usando tendencia,
momentum e volatilidade. NAO executa ordens: apenas sinaliza.

Uso:
    python crypto_signals.py now --symbol BTC/USDT --tf 4h
    python crypto_signals.py backtest --symbol BTC/USDT --tf 4h --years 5
    python crypto_signals.py backtest --symbol BTC/USDT --tf 1d --years 8 --strategy score
    python crypto_signals.py live --symbol BTC/USDT --tf 1h

Alertas no Telegram (opcional): defina as variaveis de ambiente
    TELEGRAM_TOKEN e TELEGRAM_CHAT_ID

AVISO: isto e uma ferramenta de apoio, nao recomendacao de investimento.
"""
import argparse
import os
import time

import numpy as np
import pandas as pd
import requests

# ----------------------------------------------------------------------------
# 1. Dados
# ----------------------------------------------------------------------------

def fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=1000, exchange_id="binance"):
    """Baixa candles publicos (nao precisa de chave de API)."""
    import ccxt
    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms")
    return df.drop(columns="ts")


def fetch_history(symbol="BTC/USDT", timeframe="4h", years=4, exchange_id="binance"):
    """Baixa varios anos de candles, paginando de 1000 em 1000."""
    import ccxt
    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    since = ex.milliseconds() - int(years * 365 * 24 * 3600 * 1000)
    rows = []
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms")
    df = df.drop(columns="ts")
    return df[~df.index.duplicated()]


# ----------------------------------------------------------------------------
# 2. Indicadores
# ----------------------------------------------------------------------------

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    result = 100 - 100 / (1 + rs)
    result = result.mask((loss == 0) & (gain > 0), 100)
    return result.mask((loss == 0) & (gain == 0), 50)


def atr(df, n=14):
    prev = df["close"].shift()
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def add_features(df):
    d = df.copy()
    c = d["close"]
    d["ema50"] = ema(c, 50)
    d["ema200"] = ema(c, 200)
    d["rsi"] = rsi(c)
    macd = ema(c, 12) - ema(c, 26)
    d["macd_hist"] = macd - ema(macd, 9)
    d["atr"] = atr(d)
    d["atr_pct"] = d["atr"] / c
    # volatilidade "extrema" = acima do percentil 90 das ultimas 500 velas
    d["vol_limit"] = d["atr_pct"].rolling(500, min_periods=100).quantile(0.90)
    d["vol_ok"] = d["volume"] > d["volume"].rolling(50).mean() * 0.5
    return d


# ----------------------------------------------------------------------------
# 3. Motor de sinais
# ----------------------------------------------------------------------------

def add_score(d):
    """Pontuacao de -5 a +5. Cada regra e simples e explicavel."""
    d = d.copy()
    score = pd.Series(0, index=d.index, dtype=float)

    score += np.where(d["close"] > d["ema200"], 2, -2)            # tendencia longa
    score += np.where(d["ema50"] > d["ema200"], 1, -1)            # tendencia media
    rising = d["macd_hist"] > d["macd_hist"].shift()
    score += np.where((d["macd_hist"] > 0) & rising, 1,
                      np.where(d["macd_hist"] < 0, -1, 0))        # momentum
    score += np.select(
        [d["rsi"] > 75, d["rsi"] < 30, (d["rsi"] >= 45) & (d["rsi"] <= 65)],
        [-2, -1, 1], default=0,
    )                                                             # esticado/faca caindo

    d["score"] = score
    d["vol_extreme"] = d["atr_pct"] > d["vol_limit"]
    return d


def classify(score, vol_extreme):
    if vol_extreme or score <= -1:
        return "EVITAR"
    if score >= 4:
        return "COMPRAR"
    return "AGUARDAR"


def explain(row):
    motivos = []
    motivos.append("preco acima da media de 200" if row.close > row.ema200 else "preco ABAIXO da media de 200")
    motivos.append("EMA50 > EMA200" if row.ema50 > row.ema200 else "EMA50 < EMA200")
    motivos.append(f"RSI {row.rsi:.0f}")
    motivos.append("MACD positivo" if row.macd_hist > 0 else "MACD negativo")
    if row.vol_extreme:
        motivos.append("VOLATILIDADE EXTREMA")
    return "; ".join(motivos)


def analyze(df):
    d = add_score(add_features(df)).dropna(subset=["ema200", "rsi", "vol_limit"])
    d["signal"] = [classify(s, v) for s, v in zip(d["score"], d["vol_extreme"])]
    return d


# ----------------------------------------------------------------------------
# 4. Estrategias e backtest (use ANTES de confiar em qualquer sinal)
# ----------------------------------------------------------------------------

def positions(d, strategy="trend", enter=4, exit_=1):
    """
    Retorna 1 (comprado) ou 0 (fora) para cada vela, ainda SEM atraso.

    score : v1. Entra com score alto e sai quando o score cai (muito giro).
    trend : v2. Entra com tendencia confirmada (preco > EMA200 e EMA50 > EMA200)
            e so sai quando o preco fecha abaixo da EMA200 (pouco giro).
    """
    close, e50, e200 = d["close"].values, d["ema50"].values, d["ema200"].values
    score, vol = d["score"].values, d["vol_extreme"].values
    pos = np.zeros(len(d))
    in_pos = False
    for i in range(len(d)):
        if strategy == "score":
            if not in_pos and score[i] >= enter and not vol[i]:
                in_pos = True
            elif in_pos and (score[i] <= exit_ or vol[i]):
                in_pos = False
        else:
            if not in_pos and close[i] > e200[i] and e50[i] > e200[i]:
                in_pos = True
            elif in_pos and close[i] < e200[i]:
                in_pos = False
        pos[i] = in_pos
    return pd.Series(pos, index=d.index)


def evaluate(d, pos, fee=0.001, trade_events=None):
    """Metricas de um trecho. `pos` representa a exposicao em cada intervalo."""
    if d.empty:
        raise ValueError("nao ha dados suficientes para avaliar o periodo")
    ret = d["close"].pct_change().fillna(0)
    trades = trade_events if trade_events is not None else pos.diff().abs().fillna(0)
    strat = pos * ret - trades * fee
    equity = (1 + strat).cumprod()
    hold = (1 + ret).cumprod()

    def max_dd(x):
        return (x / x.cummax() - 1).min()

    return {
        "periodo": f"{d.index[0].date()} a {d.index[-1].date()}",
        "estrategia_retorno_%": round(float((equity.iloc[-1] - 1) * 100), 1),
        "buy_hold_retorno_%": round(float((hold.iloc[-1] - 1) * 100), 1),
        "estrategia_max_drawdown_%": round(float(max_dd(equity) * 100), 1),
        "buy_hold_max_drawdown_%": round(float(max_dd(hold) * 100), 1),
        "trocas_de_posicao": int(trades.sum()),
        "tempo_investido_%": round(float(pos.mean() * 100), 1),
    }


def backtest(d, strategy="trend", fee=0.001, split=0.7):
    """
    Sinais sao calculados no fechamento e executados no fechamento da vela
    seguinte. O retorno so comeca no intervalo posterior a execucao.
    """
    if not 0 < split < 1:
        raise ValueError("split deve estar entre 0 e 1")
    if len(d) < 2:
        raise ValueError("historico insuficiente para dividir o backtest")

    signals = positions(d, strategy)
    executed = signals.shift(1).fillna(0)
    exposure = signals.shift(2).fillna(0)
    trades = executed.diff().abs().fillna(0)
    k = int(len(d) * split)
    if k == 0 or k == len(d):
        raise ValueError("split deve deixar dados nos periodos inicial e de teste")
    return {
        "periodo completo": evaluate(d, exposure, fee, trades),
        f"{int(split * 100)}% inicial": evaluate(d.iloc[:k], exposure.iloc[:k], fee, trades.iloc[:k]),
        f"{100 - int(split * 100)}% final (teste)": evaluate(
            d.iloc[k:], exposure.iloc[k:], fee, trades.iloc[k:]
        ),
    }


def print_report(res):
    cols = list(res)
    rows = list(next(iter(res.values())))
    print(f"{'':28s}" + "".join(f"{c:>26s}" for c in cols))
    for r in rows:
        print(f"{r:28s}" + "".join(f"{str(res[c][r]):>26s}" for c in cols))


# ----------------------------------------------------------------------------
# 5. Alertas e bot em tempo real
# ----------------------------------------------------------------------------

def notify(text):
    print(text)
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat, "text": text}, timeout=10,
            )
        except requests.RequestException as e:
            print("Falha ao enviar Telegram:", e)


def run_live(symbol, tf, poll=60, exchange_id="binance"):
    """Verifica a cada `poll` segundos; so reage a velas FECHADAS."""
    last_candle, last_signal = None, None
    print(f"Monitorando {symbol} ({tf}) em {exchange_id}. Ctrl+C para parar.")
    while True:
        try:
            df = fetch_ohlcv(symbol, tf, limit=1000, exchange_id=exchange_id)
            df = df.iloc[:-1]                      # descarta a vela ainda aberta
            if df.empty:
                print("Sem candles fechados disponiveis.")
                time.sleep(poll)
                continue
            d = analyze(df)
            if d.empty:
                print("Historico insuficiente para calcular os indicadores.")
                time.sleep(poll)
                continue
            row = d.iloc[-1]
            if d.index[-1] != last_candle:
                last_candle = d.index[-1]
                if row.signal != last_signal:
                    msg = (
                        f"[{symbol} {tf}] {row.signal}  (score {row.score:+.0f})\n"
                        f"Preco: {row.close:,.2f}\n{explain(row)}"
                    )
                    if row.signal == "COMPRAR":
                        stop = row.close - 2 * row.atr
                        msg += f"\nReferencia de stop (2xATR): {stop:,.2f}"
                    notify(msg)
                    last_signal = row.signal
                else:
                    print(f"{last_candle} sem mudanca: {row.signal} ({row.score:+.0f})")
        except Exception as e:                     # rede, limite de API etc.
            print("Erro:", e)
        time.sleep(poll)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("mode", choices=["backtest", "live", "now"])
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--tf", default="1h")
    p.add_argument("--exchange", default="binance")
    p.add_argument("--poll", type=int, default=60)
    p.add_argument("--years", type=float, default=0,
                   help="anos de historico (0 = so as ultimas 1000 velas)")
    p.add_argument("--strategy", choices=["trend", "score"], default="trend",
                   help="trend = v2 (menos giro); score = v1")
    p.add_argument("--split", type=float, default=0.7,
                   help="fracao inicial do historico; o resto e o teste")
    a = p.parse_args()

    if a.mode == "live":
        run_live(a.symbol, a.tf, a.poll, a.exchange)
    else:
        raw = (fetch_history(a.symbol, a.tf, a.years, a.exchange) if a.years > 0
               else fetch_ohlcv(a.symbol, a.tf, 1000, a.exchange))
        raw = raw.iloc[:-1]                         # descarta a vela ainda aberta
        d = analyze(raw)
        if d.empty:
            p.error("historico insuficiente para calcular os indicadores")
        if a.mode == "now":
            r = d.iloc[-1]
            print(f"{a.symbol} {a.tf}: {r.signal} (score {r.score:+.0f}) | {explain(r)}")
        else:
            print(f"Estrategia: {a.strategy}")
            print_report(backtest(d, a.strategy, split=a.split))