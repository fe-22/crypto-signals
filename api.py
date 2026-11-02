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
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import requests

import service

API_KEY = os.getenv("API_KEY")                       # se definida, exige o header X-API-Key
DEFAULT_EXCHANGE = os.getenv("EXCHANGE", "binance")
_monitor_lock = threading.Lock()
_monitor_stop = None
_monitor_thread = None
_monitor_config = {}

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}/[A-Z0-9]{2,10}$")
TIMEFRAMES = {"15m", "1h", "4h", "1d"}
EXCHANGES = {"binance", "kraken", "bybit", "okx", "coinbase"}


@asynccontextmanager
async def lifespan(app):
    if os.getenv("ENABLE_MONITOR", "0") == "1":
        try:
            items = _validated_watchlist(os.getenv("WATCHLIST", "BTC/USDT:4h"), DEFAULT_EXCHANGE)
            _start_monitor(items, DEFAULT_EXCHANGE)
        except ValueError as exc:
            print(f"[monitor] configuracao invalida: {exc}")
    yield
    _stop_monitor()


app = FastAPI(title="Crypto Signals", version="1.0", lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


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


class MonitorRequest(BaseModel):
    watchlist: str
    exchange: str | None = None


def _validated_watchlist(text, exchange):
    exchange = (exchange or DEFAULT_EXCHANGE).lower()
    if exchange not in EXCHANGES:
        raise ValueError(f"exchange invalida; use uma de {sorted(EXCHANGES)}")
    try:
        items = service.parse_watchlist(text)
    except ValueError as exc:
        raise ValueError("watchlist invalida; use o formato BTC/USDT:4h,ETH/USDT:1d") from exc
    if not items:
        raise ValueError("watchlist deve conter pelo menos um ativo")
    if len(items) > 20:
        raise ValueError("watchlist aceita no maximo 20 ativos")
    validated = []
    for symbol, tf in items:
        try:
            validated.append(validate(symbol, tf, exchange)[:2])
        except HTTPException as exc:
            raise ValueError(exc.detail) from exc
    return validated, exchange


def _start_monitor(items, exchange):
    global _monitor_stop, _monitor_thread, _monitor_config
    with _monitor_lock:
        if _monitor_thread and _monitor_thread.is_alive():
            raise HTTPException(409, "o monitor ja esta ativo ou encerrando")
        _monitor_stop = threading.Event()
        _monitor_config = {"watchlist": [f"{symbol}:{tf}" for symbol, tf in items], "exchange": exchange}
        _monitor_thread = threading.Thread(
            target=service.monitor_loop,
            args=(_monitor_stop, items, exchange),
            daemon=True,
        )
        _monitor_thread.start()


def _monitor_is_running():
    with _monitor_lock:
        return bool(_monitor_thread and _monitor_thread.is_alive() and not _monitor_stop.is_set())


def _stop_monitor():
    global _monitor_stop, _monitor_thread
    with _monitor_lock:
        stop, thread = _monitor_stop, _monitor_thread
        if not stop or not thread or not thread.is_alive():
            _monitor_stop = None
            _monitor_thread = None
            return False
        stop.set()
    thread.join(timeout=10)
    with _monitor_lock:
        if not thread.is_alive():
            _monitor_stop = None
            _monitor_thread = None
    return not thread.is_alive()


def call(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        print("erro:", repr(e))
        raise HTTPException(
            502,
            "Falha ao consultar a corretora. Em caso de bloqueio regional, o sistema "
            "tenta outras corretoras automaticamente. Consulte os logs para detalhes.",
        )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/info")
def info():
    return {"name": "Crypto Signals", "docs": "/docs",
            "endpoints": ["/signal", "/opportunities", "/currencies", "/backtest", "/status", "/health"],
            "disclaimer": service.DISCLAIMER}


@app.get("/signal")
def signal(symbol: str = "BTC/USDT", tf: str = "4h", exchange: str | None = None):
    return call(service.build_signal, *validate(symbol, tf, exchange))


@app.get("/opportunities")
def opportunities(tf: str = "4h", exchange: str | None = None):
    _, tf, exchange = validate("BTC/USDT", tf, exchange)
    return call(service.scan_opportunities, tf, exchange)


@app.get("/currencies")
def currencies():
    try:
        return service.build_currency_rates()
    except requests.RequestException as exc:
        print("erro ao consultar cotacoes:", repr(exc))
        raise HTTPException(502, "Nao foi possivel consultar as cotacoes de moedas agora.") from exc
    except ValueError as exc:
        print("resposta invalida de cotacoes:", repr(exc))
        raise HTTPException(502, "A fonte de cotacoes retornou dados invalidos.") from exc


@app.get("/currencies")
def currencies():
    try:
        return service.build_currency_rates()
    except requests.RequestException as exc:
        print("erro ao consultar cotacoes:", repr(exc))
        raise HTTPException(502, "Nao foi possivel consultar as cotacoes de moedas agora.") from exc
    except ValueError as exc:
        print("resposta invalida de cotacoes:", repr(exc))
        raise HTTPException(502, "A fonte de cotacoes retornou dados invalidos.") from exc


@app.get("/backtest")
def backtest(symbol: str = "BTC/USDT", tf: str = "1d", years: float = 5,
             strategy: str = "trend", exchange: str | None = None, split: float = 0.7):
    symbol, tf, exchange = validate(symbol, tf, exchange)
    if strategy not in ("trend", "score"):
        raise HTTPException(422, "strategy deve ser 'trend' ou 'score'")
    return call(service.run_backtest, symbol, tf, years, strategy, exchange, split)


@app.get("/status")
def status(_=Depends(auth)):
    return {
        "monitor_enabled": _monitor_is_running(),
        "monitor_config": dict(_monitor_config),
        **service.STATE,
    }


@app.post("/monitor/start")
def start_monitor(request: MonitorRequest, _=Depends(auth)):
    try:
        items, exchange = _validated_watchlist(request.watchlist, request.exchange)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _start_monitor(items, exchange)
    return status()


@app.post("/monitor/stop")
def stop_monitor(_=Depends(auth)):
    stopped = _stop_monitor()
    result = status()
    result["stop_requested"] = True
    result["monitor_stopped"] = stopped
    return result