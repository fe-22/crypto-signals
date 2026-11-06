"""
Backend FastAPI para o sistema de sinais.

Local:   uvicorn api:app --reload
Render:  uvicorn api:app --host 0.0.0.0 --port $PORT
Docs:    /docs  (interface interativa gerada automaticamente)
"""
import os
import re
import secrets
import sqlite3
import threading
import time
import hashlib
import hmac
import logging
import math
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import requests

import service
import portfolio

API_KEY = os.getenv("API_KEY")                       # se definida, exige o header X-API-Key
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_SESSION_SECONDS = 12 * 60 * 60
ADMIN_SESSION_KEY = secrets.token_bytes(32)
DEFAULT_EXCHANGE = os.getenv("EXCHANGE", "binance")
_monitor_lock = threading.Lock()
_monitor_stop = None
_monitor_thread = None
_monitor_config = {}
_portfolio_monitor_stop = None
_portfolio_monitor_thread = None

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}/[A-Z0-9]{2,10}$")
TIMEFRAMES = {"15m", "1h", "4h", "1d"}
EXCHANGES = {"binance", "kraken", "bybit", "okx", "coinbase"}


@asynccontextmanager
async def lifespan(app):
    _start_portfolio_monitor()
    if os.getenv("ENABLE_MONITOR", "0") == "1":
        try:
            items = _validated_watchlist(os.getenv("WATCHLIST", "BTC/USDT:4h"), DEFAULT_EXCHANGE)
            _start_monitor(items, DEFAULT_EXCHANGE)
        except ValueError as exc:
            print(f"[monitor] configuracao invalida: {exc}")
    yield
    _stop_monitor()
    _stop_portfolio_monitor()


app = FastAPI(title="Crypto Signals", version="1.0", lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


def _access_log_path():
    return Path(os.getenv("ACCESS_LOG_DB", str(Path(__file__).parent / "access_log.sqlite3")))


def _record_access(ip_address):
    database = _access_log_path()
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, timeout=10)) as connection:
        with connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS access_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accessed_at TEXT NOT NULL,
                    ip_address TEXT NOT NULL
                )"""
            )
            connection.execute(
                "INSERT INTO access_log (accessed_at, ip_address) VALUES (?, ?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), ip_address),
            )
            connection.execute(
                "DELETE FROM access_log WHERE id NOT IN "
                "(SELECT id FROM access_log ORDER BY id DESC LIMIT 500)"
            )


@app.middleware("http")
async def record_homepage_visits(request: Request, call_next):
    if request.method == "GET" and request.url.path == "/":
        try:
            _record_access(request.client.host if request.client else "unknown")
        except (OSError, sqlite3.Error):
            logging.getLogger(__name__).exception("Nao foi possivel registrar acesso ao painel")
    return await call_next(request)


def _session_signature(timestamp):
    return hmac.new(ADMIN_SESSION_KEY, timestamp.encode("ascii"), hashlib.sha256).hexdigest()


def _valid_admin_session(token):
    if not ADMIN_PASSWORD or not token or "." not in token:
        return False
    timestamp, signature = token.split(".", 1)
    if not timestamp.isdigit() or not secrets.compare_digest(_session_signature(timestamp), signature):
        return False
    age = int(time.time()) - int(timestamp)
    return 0 <= age < ADMIN_SESSION_SECONDS


def require_admin(request: Request):
    if not _valid_admin_session(request.cookies.get("admin_session")):
        raise HTTPException(status_code=401, detail="Acesso administrativo necessario")


class AdminLoginRequest(BaseModel):
    password: str


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


class PortfolioPositionRequest(BaseModel):
    symbol: str
    quantity: float
    purchase_price: float
    target_profit_pct: float = 10
    stop_loss_pct: float = 5


class SimulationRequest(BaseModel):
    symbol: str
    quantity: float
    days: int
    exchange: str | None = None


def _start_portfolio_monitor():
    global _portfolio_monitor_stop, _portfolio_monitor_thread
    with _monitor_lock:
        if _portfolio_monitor_thread and _portfolio_monitor_thread.is_alive():
            return
        portfolio.initialize()
        _portfolio_monitor_stop = threading.Event()
        _portfolio_monitor_thread = threading.Thread(
            target=portfolio.monitor_loop,
            args=(_portfolio_monitor_stop, DEFAULT_EXCHANGE),
            daemon=True,
        )
        _portfolio_monitor_thread.start()


def _stop_portfolio_monitor():
    global _portfolio_monitor_stop, _portfolio_monitor_thread
    with _monitor_lock:
        stop, thread = _portfolio_monitor_stop, _portfolio_monitor_thread
        if not stop or not thread or not thread.is_alive():
            _portfolio_monitor_stop = None
            _portfolio_monitor_thread = None
            return
        stop.set()
    thread.join(timeout=10)
    with _monitor_lock:
        if not thread.is_alive():
            _portfolio_monitor_stop = None
            _portfolio_monitor_thread = None


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
    except service.BacktestBusyError as e:
        raise HTTPException(503, str(e)) from e
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


@app.get("/gestor")
def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


@app.post("/admin/login")
def admin_login(request: AdminLoginRequest, response: Response, req: Request):
    password = ADMIN_PASSWORD
    if not password:
        raise HTTPException(status_code=503, detail="Acesso gestor nao configurado")
    if not secrets.compare_digest(request.password.encode("utf-8"), password.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Senha incorreta")
    timestamp = str(int(time.time()))
    response.set_cookie(
        "admin_session",
        f"{timestamp}.{_session_signature(timestamp)}",
        max_age=ADMIN_SESSION_SECONDS,
        httponly=True,
        secure=req.url.scheme == "https",
        samesite="strict",
        path="/admin",
    )
    return {"authenticated": True}


@app.post("/admin/logout")
def admin_logout(response: Response):
    response.delete_cookie("admin_session", path="/admin", httponly=True, samesite="strict")
    return {"authenticated": False}


@app.get("/admin/accesses")
def admin_accesses(_=Depends(require_admin)):
    database = _access_log_path()
    if not database.exists():
        return {"accesses": []}
    with closing(sqlite3.connect(database, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT accessed_at, ip_address FROM access_log ORDER BY id DESC LIMIT 500"
        ).fetchall()
    return {"accesses": [dict(row) for row in rows]}


@app.get("/admin/portfolio")
def admin_portfolio(_=Depends(require_admin)):
    return {"positions": portfolio.list_positions()}


@app.post("/admin/portfolio")
def create_portfolio_position(request: PortfolioPositionRequest, _=Depends(require_admin)):
    symbol = request.symbol.strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise HTTPException(422, "symbol invalido (ex.: BTC/USDT)")
    values = (
        request.quantity,
        request.purchase_price,
        request.target_profit_pct,
        request.stop_loss_pct,
    )
    if not all(math.isfinite(value) for value in values):
        raise HTTPException(422, "os valores da posicao devem ser numeros finitos")
    if request.quantity <= 0 or request.purchase_price <= 0:
        raise HTTPException(422, "quantidade e preco de compra devem ser maiores que zero")
    if not 0 < request.target_profit_pct <= 1000:
        raise HTTPException(422, "meta de lucro deve estar entre 0 e 1000%")
    if not 0 < request.stop_loss_pct <= 100:
        raise HTTPException(422, "limite de perda deve estar entre 0 e 100%")
    position_id = portfolio.add_position(
        symbol,
        request.quantity,
        request.purchase_price,
        request.target_profit_pct,
        request.stop_loss_pct,
    )
    return {"id": position_id, "position": next(
        position for position in portfolio.list_positions() if position["id"] == position_id
    )}


@app.get("/admin/portfolio/quote")
def quote_portfolio_position(symbol: str, quantity: float, _=Depends(require_admin)):
    if not math.isfinite(quantity) or quantity <= 0:
        raise HTTPException(422, "a quantidade deve ser um numero finito maior que zero")
    symbol, _, exchange = validate(symbol, "1d", DEFAULT_EXCHANGE)
    return call(service.build_purchase_quote, symbol, quantity, exchange)


@app.post("/admin/portfolio/refresh")
def refresh_admin_portfolio(_=Depends(require_admin)):
    return portfolio.refresh_positions(DEFAULT_EXCHANGE)


@app.delete("/admin/portfolio/{position_id}")
def delete_portfolio_position(position_id: int, _=Depends(require_admin)):
    if position_id <= 0 or not portfolio.remove_position(position_id):
        raise HTTPException(404, "posicao nao encontrada")
    return {"deleted": True}


@app.get("/info")
def info():
    return {"name": "Crypto Signals", "docs": "/docs",
            "endpoints": ["/signal", "/opportunities", "/simulation", "/currencies", "/macro",
                          "/market-history", "/backtest", "/status", "/health"],
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


@app.get("/macro")
def macro():
    try:
        return service.build_macro_indicators()
    except requests.RequestException as exc:
        print("erro ao consultar indicadores macroeconomicos:", repr(exc))
        raise HTTPException(502, "Nao foi possivel consultar os indicadores macroeconomicos agora.") from exc
    except ValueError as exc:
        print("resposta invalida de indicadores macroeconomicos:", repr(exc))
        raise HTTPException(502, "Uma das fontes retornou dados macroeconomicos invalidos.") from exc


@app.get("/market-history")
def market_history(symbol: str = "BTC/USDT", tf: str = "1d", exchange: str | None = None):
    symbol, tf, exchange = validate(symbol, tf, exchange)
    return call(service.build_price_history, symbol, tf, exchange)


@app.post("/simulation")
def simulation(request: SimulationRequest):
    if not math.isfinite(request.quantity) or request.quantity <= 0:
        raise HTTPException(422, "a quantidade deve ser um numero finito maior que zero")
    symbol, _, exchange = validate(request.symbol, "1d", request.exchange)
    if request.days not in (7, 30, 90, 180, 365):
        raise HTTPException(422, "prazo invalido; escolha 7, 30, 90, 180 ou 365 dias")
    return call(service.simulate_investment, symbol, request.quantity, request.days, exchange)


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