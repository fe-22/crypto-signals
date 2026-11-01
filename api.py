"""
Backend FastAPI para o sistema de sinais.

Local:   uvicorn api:app --reload
Render:  uvicorn api:app --host 0.0.0.0 --port $PORT
Docs:    /docs  (interface interativa gerada automaticamente)
"""
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException

import service

API_KEY = os.getenv("API_KEY")                       # se definida, exige o header X-API-Key
DEFAULT_EXCHANGE = os.getenv("EXCHANGE", "binance")

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}/[A-Z0-9]{2,10}$")
TIMEFRAMES = {"15m", "1h", "4h", "1d"}
EXCHANGES = {"binance", "kraken", "bybit", "okx", "coinbase"}


@asynccontextmanager
async def lifespan(app):
    stop = threading.Event()
    if os.getenv("ENABLE_MONITOR", "0") == "1":
        threading.Thread(target=service.monitor_loop, args=(stop,), daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="Crypto Signals", version="1.0", lifespan=lifespan)


def auth(x_api_key: str | None = Header(default=None)):
    if API_KEY and not secrets.compare_digest(x_api_key or "", API_KEY):
        raise HTTPException(status_code=401, detail="X-API-Key ausente ou invalida")


def validate(symbol, tf, exchange):
    symbol = symbol.upper()
    exchange = (exchange or DEFAULT_EXCHANGE).lower()
    if not SYMBOL_RE.match(symbol):
        raise HTTPException(422, "symbol invalido (ex.: BTC/USDT)")
    if tf not in TIMEFRAMES:
        raise HTTPException(422, f"tf invalido; use um de {sorted(TIMEFRAMES)}")
    if exchange not in EXCHANGES:
        raise HTTPException(422, f"exchange invalida; use uma de {sorted(EXCHANGES)}")
    return symbol, tf, exchange


def call(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        print("erro:", repr(e))
        raise HTTPException(
            502,
            "Falha ao consultar a corretora. Se o log mostrar 'restricted location', "
            "troque a variavel EXCHANGE ou a regiao do servico.",
        )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"name": "Crypto Signals", "docs": "/docs",
            "endpoints": ["/signal", "/backtest", "/status", "/health"],
            "disclaimer": service.DISCLAIMER}


@app.get("/signal")
def signal(symbol: str = "BTC/USDT", tf: str = "4h", exchange: str | None = None,
           _=Depends(auth)):
    return call(service.build_signal, *validate(symbol, tf, exchange))


@app.get("/backtest")
def backtest(symbol: str = "BTC/USDT", tf: str = "1d", years: float = 5,
             strategy: str = "trend", exchange: str | None = None, split: float = 0.7,
             _=Depends(auth)):
    symbol, tf, exchange = validate(symbol, tf, exchange)
    if strategy not in ("trend", "score"):
        raise HTTPException(422, "strategy deve ser 'trend' ou 'score'")
    return call(service.run_backtest, symbol, tf, years, strategy, exchange, split)


@app.get("/status")
def status(_=Depends(auth)):
    return {"monitor_enabled": os.getenv("ENABLE_MONITOR", "0") == "1", **service.STATE}