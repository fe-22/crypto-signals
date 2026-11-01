"""
Camada de servico: reaproveita crypto_signals.py (sem depender do FastAPI).
Cuida de cache, montagem do sinal em JSON, backtest sob demanda e monitoramento.
"""
import os
import threading
import time
from datetime import datetime, timezone

import crypto_signals as cs

CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))   # segundos (candles)
BACKTEST_TTL = 3600                             # segundos (resultado de backtest)

_cache = {}
_lock = threading.Lock()
_backtest_slot = threading.Semaphore(1)         # 1 backtest por vez (e pesado)

STATE = {"started_at": None, "last_check": None, "watch": {}, "errors": []}

DISCLAIMER = "Ferramenta de apoio a decisao; nao e recomendacao de investimento."


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (now, value)
        if len(_cache) > 200:                   # evita crescer sem limite
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                del _cache[k]
    return value


# ----------------------------------------------------------------------------
# Sinal atual
# ----------------------------------------------------------------------------

def build_signal(symbol, tf, exchange):
    """Sinal da ULTIMA VELA FECHADA, em formato JSON-friendly."""
    df = _cached(("ohlcv", symbol, tf, exchange), CACHE_TTL,
                 lambda: cs.fetch_ohlcv(symbol, tf, 1000, exchange)).iloc[:-1]
    d = cs.analyze(df)
    if d.empty:
        raise ValueError("historico insuficiente para calcular os indicadores")
    row = d.iloc[-1]
    price = float(row.close)
    in_trend = bool(cs.positions(d, "trend").iloc[-1])
    return {
        "symbol": symbol,
        "timeframe": tf,
        "exchange": exchange,
        "candle_open_time": d.index[-1].isoformat(),
        "price": price,
        "signal": row.signal,                       # v1: COMPRAR / AGUARDAR / EVITAR
        "score": int(row.score),
        "trend_following": "EM_TENDENCIA" if in_trend else "FORA",   # v2
        "volatility_extreme": bool(row.vol_extreme),
        "rsi": round(float(row.rsi), 1),
        "stop_reference": round(price - 2 * float(row.atr), 8) if row.signal == "COMPRAR" else None,
        "reasons": cs.explain(row),
        "disclaimer": DISCLAIMER,
    }


# ----------------------------------------------------------------------------
# Backtest sob demanda
# ----------------------------------------------------------------------------

def run_backtest(symbol, tf, years, strategy, exchange, split=0.7):
    years = min(max(years, 0.5), 8)
    split = min(max(split, 0.5), 0.9)

    def job():
        with _backtest_slot:
            d = cs.analyze(cs.fetch_history(symbol, tf, years, exchange))
            if d.empty:
                raise ValueError("historico insuficiente para o backtest")
            return cs.backtest(d, strategy, split=split)

    result = _cached(("bt", symbol, tf, years, strategy, exchange, split), BACKTEST_TTL, job)
    return {"symbol": symbol, "timeframe": tf, "strategy": strategy,
            "results": result, "disclaimer": DISCLAIMER}


# ----------------------------------------------------------------------------
# Monitoramento (alertas)
# ----------------------------------------------------------------------------

def parse_watchlist(text):
    items = []
    for part in text.split(","):
        part = part.strip()
        if part:
            sym, tf = part.rsplit(":", 1)
            items.append((sym.strip().upper(), tf.strip()))
    return items


def format_alert(prev, cur):
    lines = [f"[{cur['symbol']} {cur['timeframe']}]"]
    if prev["signal"] != cur["signal"]:
        lines.append(f"Sinal: {prev['signal']} -> {cur['signal']} (score {cur['score']:+d})")
    if prev["trend_following"] != cur["trend_following"]:
        lines.append(f"Tendencia: {prev['trend_following']} -> {cur['trend_following']}")
    lines.append(f"Preco: {cur['price']:,.2f}")
    lines.append(cur["reasons"])
    if cur["stop_reference"]:
        lines.append(f"Referencia de stop (2xATR): {cur['stop_reference']:,.2f}")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def check_once(items, exchange, notify=cs.notify):
    """Consulta cada ativo; avisa quando o sinal ou a tendencia mudam.
    A primeira leitura de cada ativo so grava o estado (nao alerta)."""
    for symbol, tf in items:
        key = f"{symbol}:{tf}"
        try:
            cur = build_signal(symbol, tf, exchange)
        except Exception as e:
            print(f"[monitor] erro em {key}: {e!r}")
            STATE["errors"] = (STATE["errors"] + [f"{_now()} {key}: {e!r}"])[-20:]
            continue
        prev = STATE["watch"].get(key)
        STATE["watch"][key] = cur
        if prev and (prev["signal"] != cur["signal"]
                     or prev["trend_following"] != cur["trend_following"]):
            notify(format_alert(prev, cur))
    STATE["last_check"] = _now()


def monitor_loop(stop_event):
    items = parse_watchlist(os.getenv("WATCHLIST", "BTC/USDT:4h"))
    exchange = os.getenv("EXCHANGE", "binance")
    poll = max(60, int(os.getenv("POLL_SECONDS", "300")))
    STATE["started_at"] = _now()
    print(f"[monitor] iniciado: {items} em {exchange}, a cada {poll}s")
    while not stop_event.is_set():
        check_once(items, exchange)
        stop_event.wait(poll)