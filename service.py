"""
Camada de servico: reaproveita crypto_signals.py (sem depender do FastAPI).
Cuida de cache, montagem do sinal em JSON, backtest sob demanda e monitoramento.
"""
import os
import math
import threading
import time
from datetime import datetime, timezone

import requests

import crypto_signals as cs

CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))   # segundos (candles)
BACKTEST_TTL = 3600                             # segundos (resultado de backtest)
CURRENCY_RATES_TTL = 1800                       # atualizacao diaria da fonte ECB
OPPORTUNITY_SYMBOLS = (
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "ADA/USDT",
    "DOGE/USDT",
)
EXCHANGE_FALLBACKS = ("binance", "kraken", "bybit", "okx", "coinbase")

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


def _is_location_restriction(error):
    message = str(error).lower()
    return any(marker in message for marker in (
        "restricted location",
        "restricted region",
        "regional restriction",
        "geographic restriction",
        "geo-restricted",
        "not available from your location",
        "unavailable in your jurisdiction",
        "451",
    ))


def _fetch_market_data(fetcher, *args, exchange):
    try:
        return fetcher(*args, exchange), exchange
    except Exception as error:
        if not _is_location_restriction(error):
            raise

        print(f"[exchange] {exchange} bloqueada por restricao geografica; tentando alternativas")
        for fallback in EXCHANGE_FALLBACKS:
            if fallback == exchange:
                continue
            try:
                data = fetcher(*args, fallback)
                print(f"[exchange] consulta concluida usando {fallback}")
                return data, fallback
            except Exception as fallback_error:
                print(f"[exchange] falha em {fallback}: {fallback_error!r}")
        raise


# ----------------------------------------------------------------------------
# Sinal atual
# ----------------------------------------------------------------------------

def build_signal(symbol, tf, exchange):
    """Sinal da ULTIMA VELA FECHADA, em formato JSON-friendly."""
    df, used_exchange = _cached(
        ("ohlcv", symbol, tf, exchange),
        CACHE_TTL,
        lambda: _fetch_market_data(cs.fetch_ohlcv, symbol, tf, 1000, exchange=exchange),
    )
    df = df.iloc[:-1]
    d = cs.analyze(df)
    if d.empty:
        raise ValueError("historico insuficiente para calcular os indicadores")
    row = d.iloc[-1]
    price = float(row.close)
    in_trend = bool(cs.positions(d, "trend").iloc[-1])
    return {
        "symbol": symbol,
        "timeframe": tf,
        "exchange": used_exchange,
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


def scan_opportunities(tf, exchange):
    """Compara sinais tecnicos de uma lista fixa de ativos liquidos."""
    analyzed = []
    errors = []
    for symbol in OPPORTUNITY_SYMBOLS:
        try:
            signal = build_signal(symbol, tf, exchange)
        except Exception as exc:
            print(f"[opportunities] falha ao analisar {symbol}: {exc!r}")
            errors.append({"symbol": symbol, "error": "Dados indisponiveis para este ativo nesta corretora."})
            continue

        if signal["signal"] == "COMPRAR":
            rationale = [
                f"Score tecnico {signal['score']} atingiu o limiar de compra (4).",
                "A volatilidade nao ultrapassou o limite historico definido pelo sistema.",
            ]
        else:
            rationale = [
                f"O sinal atual e {signal['signal']} com score {signal['score']}; nao atende ao criterio tecnico de compra (score >= 4).",
            ]
        rationale.append(signal["reasons"])
        if signal["trend_following"] == "EM_TENDENCIA":
            rationale.append("Tendencia de longo prazo confirmada pelo criterio EMA50/EMA200.")
        if signal["volatility_extreme"]:
            rationale.append("Volatilidade extrema: o sistema sinaliza cautela.")

        analyzed.append({**signal, "rationale": rationale})

    if not analyzed:
        raise RuntimeError("Nao foi possivel analisar nenhum ativo; verifique a corretora e tente novamente.")

    analyzed.sort(
        key=lambda item: (
            item["signal"] == "COMPRAR",
            item["score"],
            item["trend_following"] == "EM_TENDENCIA",
            not item["volatility_extreme"],
        ),
        reverse=True,
    )
    candidates = [item["symbol"] for item in analyzed if item["signal"] == "COMPRAR"]
    return {
        "timeframe": tf,
        "exchange": exchange,
        "analyzed_at": _now(),
        "symbols_analyzed": len(analyzed),
        "candidates": candidates,
        "assets": analyzed,
        "unavailable": errors,
        "method": "Indicadores tecnicos EMA50/EMA200, RSI, MACD, ATR e score; ultima vela fechada.",
        "disclaimer": (
            "Radar educativo, nao e recomendacao de investimento nem previsao de lucro. "
            "Sinais tecnicos podem falhar e nao avaliam noticias, fundamentos, liquidez, "
            "custos ou adequacao ao seu perfil."
        ),
    }


def build_currency_rates():
    """Cotacoes indicativas de moedas estrangeiras em reais."""
    def fetch():
        response = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "BRL", "to": "USD,EUR,GBP"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        rates = payload.get("rates")
        date = payload.get("date")
        if payload.get("base") != "BRL" or not isinstance(rates, dict) or not date:
            raise ValueError("resposta invalida da fonte de cotacoes")

        currencies = {
            "USD": "Dolar americano",
            "EUR": "Euro",
            "GBP": "Libra esterlina",
        }
        converted = {}
        for code, name in currencies.items():
            rate = rates.get(code)
            if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
                raise ValueError(f"cotacao invalida para {code}")
            converted[code] = {
                "name": name,
                "brl_per_unit": round(1 / rate, 4),
            }
        return {
            "base_currency": "BRL",
            "date": date,
            "source": "Frankfurter (referencia do Banco Central Europeu)",
            "rates": converted,
            "disclaimer": "Cotacoes de referencia; podem diferir dos valores comerciais e de tempo real.",
        }

    return _cached(("currency-rates", "BRL"), CURRENCY_RATES_TTL, fetch)


# ----------------------------------------------------------------------------
# Backtest sob demanda
# ----------------------------------------------------------------------------

def run_backtest(symbol, tf, years, strategy, exchange, split=0.7):
    years = min(max(years, 0.5), 8)
    split = min(max(split, 0.5), 0.9)

    def job():
        with _backtest_slot:
            history, used_exchange = _fetch_market_data(
                cs.fetch_history, symbol, tf, years, exchange=exchange
            )
            d = cs.analyze(history)
            if d.empty:
                raise ValueError("historico insuficiente para o backtest")
            return cs.backtest(d, strategy, split=split), used_exchange

    result, used_exchange = _cached(("bt", symbol, tf, years, strategy, exchange, split), BACKTEST_TTL, job)
    return {"symbol": symbol, "timeframe": tf, "strategy": strategy, "exchange": used_exchange,
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


def monitor_loop(stop_event, items=None, exchange=None):
    items = items if items is not None else parse_watchlist(os.getenv("WATCHLIST", "BTC/USDT:4h"))
    exchange = exchange or os.getenv("EXCHANGE", "binance")
    poll = max(60, int(os.getenv("POLL_SECONDS", "300")))
    STATE["started_at"] = _now()
    print(f"[monitor] iniciado: {items} em {exchange}, a cada {poll}s")
    while not stop_event.is_set():
        check_once(items, exchange)
        stop_event.wait(poll)