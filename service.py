"""
Camada de servico: reaproveita crypto_signals.py (sem depender do FastAPI).
Cuida de cache, montagem do sinal em JSON, backtest sob demanda e monitoramento.
"""
import os
import math
import statistics
import threading
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone

import requests

import crypto_signals as cs

CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))   # segundos (candles)
BACKTEST_TTL = 3600                             # segundos (resultado de backtest)
CURRENCY_RATES_TTL = 1800                       # atualizacao diaria da fonte ECB
MACRO_TTL = 1800
PRICE_HISTORY_TTL = 300
MAX_BACKTEST_CANDLES = 50_000
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


class BacktestBusyError(RuntimeError):
    """Raised instead of queueing another memory-intensive backtest."""


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


def build_current_price(symbol, exchange):
    """Cotacao mais recente informada pela corretora, sem depender de candle fechado."""
    import ccxt

    def fetch_ticker(pair, exchange_id):
        client = getattr(ccxt, exchange_id)({"enableRateLimit": True})
        ticker = client.fetch_ticker(pair)
        price = ticker.get("last")
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise ValueError(f"cotacao invalida para {pair}")
        return float(price)

    price, used_exchange = _fetch_market_data(fetch_ticker, symbol, exchange=exchange)
    return {
        "symbol": symbol,
        "price": price,
        "exchange": used_exchange,
        "updated_at": _now(),
    }


def build_purchase_quote(symbol, quantity, exchange):
    """Calcula custo estimado da fracao usando ticker atual e cambio de referencia."""
    if not math.isfinite(quantity) or quantity <= 0:
        raise ValueError("a quantidade deve ser maior que zero")

    quote_currency = symbol.split("/", 1)[1]
    if quote_currency not in ("BRL", "USD", "USDT", "EUR", "GBP"):
        raise ValueError(
            "conversao para reais disponivel apenas para pares cotados em BRL, USD, USDT, EUR ou GBP"
        )

    ticker = build_current_price(symbol, exchange)
    if quote_currency == "BRL":
        brl_per_quote_unit = 1.0
        conversion_note = None
        rate_date = None
    else:
        exchange_rates = build_currency_rates()
        rate_currency = "USD" if quote_currency == "USDT" else quote_currency
        brl_per_quote_unit = exchange_rates["rates"][rate_currency]["brl_per_unit"]
        rate_date = exchange_rates.get("date")
        if quote_currency == "USDT":
            conversion_note = "USDT convertido pela cotacao de referencia do USD; paridade aproximada."
        else:
            conversion_note = "Cambio de referencia; pode diferir do valor praticado pela corretora."

    total_quote = ticker["price"] * quantity
    return {
        "symbol": symbol,
        "quantity": quantity,
        "unit_price": ticker["price"],
        "quote_currency": quote_currency,
        "total_quote": total_quote,
        "brl_per_quote_unit": brl_per_quote_unit,
        "estimated_total_brl": total_quote * brl_per_quote_unit,
        "exchange": ticker["exchange"],
        "exchange_rate_date": rate_date,
        "conversion_note": conversion_note,
    }


def simulate_investment(symbol, quantity, days, exchange):
    """Compara o prazo escolhido com janelas historicas da mesma duracao."""
    if days not in (7, 30, 90, 180, 365):
        raise ValueError("prazo invalido; escolha 7, 30, 90, 180 ou 365 dias")
    if not math.isfinite(quantity) or quantity <= 0:
        raise ValueError("a quantidade deve ser maior que zero")

    history, used_exchange = _cached(
        ("simulation-history", symbol, exchange),
        PRICE_HISTORY_TTL,
        lambda: _fetch_market_data(cs.fetch_history, symbol, "1d", 3, exchange=exchange),
    )
    history = history.iloc[:-1]
    closes = history["close"].to_numpy(dtype=float)
    if len(closes) <= days:
        raise ValueError("historico insuficiente para comparar esse prazo")
    if not all(math.isfinite(price) and price > 0 for price in closes):
        raise ValueError("historico de precos invalido para este ativo")

    historical_returns = [
        closes[index + days] / closes[index] - 1
        for index in range(len(closes) - days)
    ]
    if len(historical_returns) < 20:
        raise ValueError("historico insuficiente para estimar esse prazo com seguranca")

    analysis = cs.analyze(history)
    if analysis.empty:
        raise ValueError("historico insuficiente para calcular o sinal tecnico")
    signal_row = analysis.iloc[-1]
    signal_guidance = {
        "COMPRAR": {
            "action": "COMPRAR",
            "title": "Sinal técnico: comprar",
            "message": "Os indicadores atendem ao critério técnico de compra; isso não garante valorização.",
        },
        "EVITAR": {
            "action": "EVITAR",
            "title": "Não comprar · sinal de venda se já possui",
            "message": "Os indicadores apontam cautela. Considere seu preço de entrada e seus riscos; nenhuma ordem será executada.",
        },
        "AGUARDAR": {
            "action": "AGUARDAR",
            "title": "Aguardar; sem indicação de compra",
            "message": "Os indicadores não confirmam compra nem acionam o critério técnico de cautela.",
        },
    }[signal_row.signal]

    ordered_returns = sorted(historical_returns)

    def percentile(value):
        position = (len(ordered_returns) - 1) * value
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered_returns[lower]
        weight = position - lower
        return ordered_returns[lower] * (1 - weight) + ordered_returns[upper] * weight

    current_price = build_current_price(symbol, used_exchange)
    investment = quantity * current_price["price"]
    median_return = statistics.median(ordered_returns)
    lower_return = percentile(0.10)
    upper_return = percentile(0.90)

    def scenario(return_pct):
        return {
            "return_pct": round(return_pct * 100, 2),
            "estimated_value": round(investment * (1 + return_pct), 8),
            "estimated_profit": round(investment * return_pct, 8),
        }

    return {
        "symbol": symbol,
        "quantity": quantity,
        "days": days,
        "exchange": current_price["exchange"],
        "quote_currency": symbol.split("/", 1)[1],
        "current_price": current_price["price"],
        "estimated_investment": round(investment, 8),
        "historical_windows": len(historical_returns),
        "history_start": history.index[0].isoformat(),
        "history_end": history.index[-1].isoformat(),
        "technical_guidance": {
            **signal_guidance,
            "signal": signal_row.signal,
            "score": int(signal_row.score),
            "rsi": round(float(signal_row.rsi), 1),
            "timeframe": "1d",
            "reasons": cs.explain(signal_row),
        },
        "scenarios": {
            "lower_10th_percentile": scenario(lower_return),
            "median": scenario(median_return),
            "upper_90th_percentile": scenario(upper_return),
        },
        "disclaimer": (
            "Cenarios calculados a partir de variacoes historicas em janelas do mesmo prazo. "
            "Nao sao previsoes: o resultado real pode ser muito diferente e incluir perdas."
        ),
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


def build_price_history(symbol, tf, exchange):
    """Precos das velas fechadas para o grafico do ativo."""
    df, used_exchange = _cached(
        ("price-history", symbol, tf, exchange),
        PRICE_HISTORY_TTL,
        lambda: _fetch_market_data(cs.fetch_ohlcv, symbol, tf, 250, exchange=exchange),
    )
    closed = df.iloc[:-1]
    if closed.empty:
        raise ValueError("nao ha candles fechados disponiveis para este ativo")
    points = [
        {"time": timestamp.isoformat(), "price": float(price)}
        for timestamp, price in closed["close"].items()
        if math.isfinite(float(price)) and float(price) > 0
    ]
    if not points:
        raise ValueError("historico de precos invalido para este ativo")
    return {
        "symbol": symbol,
        "timeframe": tf,
        "exchange": used_exchange,
        "points": points,
        "source": "Candles publicos da corretora",
    }


def _fetch_selic_target():
    response = requests.get(
        "https://api.bcb.gov.br/dados/serie/bcdata.sgs.1178/dados/ultimos/1",
        params={"formato": "json"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise ValueError("a fonte do Banco Central nao retornou a meta Selic")
    row = payload[-1]
    try:
        rate = float(str(row["valor"]).replace(",", "."))
        date = datetime.strptime(row["data"], "%d/%m/%Y").date().isoformat()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("resposta invalida da meta Selic") from exc
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("valor invalido da meta Selic")
    return {"annual_rate_pct": rate, "date": date}


def _fetch_brent_prices():
    response = requests.get(
        "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F",
        params={"range": "3mo", "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("resposta invalida do historico Brent") from exc
    rows = []
    for timestamp, close in zip(timestamps, closes):
        if (isinstance(timestamp, (int, float)) and isinstance(close, (int, float))
                and math.isfinite(close) and close > 0):
            date = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
            rows.append({"date": date, "brent_usd": float(close)})
    if len(rows) < 2:
        raise ValueError("historico insuficiente para o preco do Brent")
    return rows


def _fetch_usd_brl_history():
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=100)
    response = requests.get(
        f"https://api.frankfurter.app/{start_date.isoformat()}..{end_date.isoformat()}",
        params={"from": "USD", "to": "BRL"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("historico USD/BRL invalido")
    rates = payload.get("rates")
    if payload.get("base") != "USD" or not isinstance(rates, dict) or not rates:
        raise ValueError("historico USD/BRL invalido")
    history = {}
    for date, daily_rates in rates.items():
        quote = daily_rates.get("BRL") if isinstance(daily_rates, dict) else None
        if isinstance(quote, (int, float)) and math.isfinite(quote) and quote > 0:
            history[date] = float(quote)
    if not history:
        raise ValueError("historico USD/BRL indisponivel")
    return history


def _percent_change(current, previous):
    return round((current / previous - 1) * 100, 2)


def build_macro_indicators():
    """Indicadores de referencia e alerta heuristico de pressao nos combustiveis."""
    def fetch():
        selic = _fetch_selic_target()
        brent_history = _fetch_brent_prices()
        usd_history = _fetch_usd_brl_history()
        usd_dates = sorted(usd_history)
        history = []
        for point in brent_history:
            position = bisect_right(usd_dates, point["date"]) - 1
            if position < 0:
                continue
            usd_brl = usd_history[usd_dates[position]]
            history.append({
                **point,
                "usd_brl": round(usd_brl, 4),
                "brent_brl": round(point["brent_usd"] * usd_brl, 2),
            })
        if len(history) < 2:
            raise ValueError("historico insuficiente para comparar Brent e dolar")

        latest = history[-1]
        comparison_date = (
            datetime.fromisoformat(latest["date"]).date() - timedelta(days=30)
        ).isoformat()
        baseline = history[0]
        for point in history:
            if point["date"] >= comparison_date:
                baseline = point
                break
        usd_baseline_date = next((date for date in usd_dates if date >= comparison_date), None)
        if usd_baseline_date is None:
            usd_baseline_date = usd_dates[0]
        usd_current = usd_history[usd_dates[-1]]
        usd_change = _percent_change(usd_current, usd_history[usd_baseline_date])
        brent_change = _percent_change(latest["brent_usd"], baseline["brent_usd"])
        brent_brl_change = _percent_change(latest["brent_brl"], baseline["brent_brl"])
        pressure = brent_brl_change >= 5

        return {
            "selic": {
                **selic,
                "description": "Meta Selic anual definida pelo Copom",
                "source": "Banco Central do Brasil (SGS 1178)",
            },
            "brent": {
                "usd_per_barrel": round(latest["brent_usd"], 2),
                "brl_per_barrel": latest["brent_brl"],
                "date": latest["date"],
                "change_30d_pct": brent_change,
                "source": "Yahoo Finance (BZ=F)",
            },
            "usd_brl": {
                "brl_per_usd": round(usd_current, 4),
                "date": usd_dates[-1],
                "change_30d_pct": usd_change,
                "source": "Frankfurter (referencia do Banco Central Europeu)",
            },
            "comparison": {
                "brent_brl_change_30d_pct": brent_brl_change,
                "baseline_date": baseline["date"],
            },
            "fuel_alert": {
                "status": "ATENCAO" if pressure else "SEM_PRESSAO_FORTE",
                "threshold_pct": 5,
                "message": (
                    f"Possivel pressao de alta: o custo de referencia do barril em reais "
                    f"subiu {brent_brl_change:.2f}% em cerca de 30 dias."
                    if pressure else
                    f"Sem pressao forte pelo criterio adotado: variacao do barril em reais "
                    f"de {brent_brl_change:.2f}% em cerca de 30 dias."
                ),
                "disclaimer": (
                    "Indicador de pressao baseado em Brent e USD/BRL; nao representa "
                    "previsao ou confirmacao de reajuste nos combustiveis. O repasse depende "
                    "de impostos, distribuicao, politica de precos e outras condicoes."
                ),
            },
            "history": history,
            "analyzed_at": _now(),
        }

    return _cached(("macro-indicators",), MACRO_TTL, fetch)


# ----------------------------------------------------------------------------
# Backtest sob demanda
# ----------------------------------------------------------------------------

def run_backtest(symbol, tf, years, strategy, exchange, split=0.7):
    years = min(max(years, 0.5), 8)
    split = min(max(split, 0.5), 0.9)
    timeframe_minutes = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}[tf]
    estimated_candles = math.ceil(years * 365 * 24 * 60 / timeframe_minutes)
    if estimated_candles > MAX_BACKTEST_CANDLES:
        raise ValueError(
            f"backtest excede o limite de {MAX_BACKTEST_CANDLES} candles; "
            "reduza o prazo ou escolha um timeframe maior"
        )

    def job():
        if not _backtest_slot.acquire(blocking=False):
            raise BacktestBusyError("backtest ocupado; tente novamente em alguns instantes")
        try:
            history, used_exchange = _fetch_market_data(
                cs.fetch_history, symbol, tf, years, exchange=exchange
            )
            d = cs.analyze(history)
            if d.empty:
                raise ValueError("historico insuficiente para o backtest")
            return cs.backtest(d, strategy, split=split), used_exchange
        finally:
            _backtest_slot.release()

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