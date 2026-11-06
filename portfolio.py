"""Persistence and price monitoring for the authenticated portfolio tracker."""
import logging
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import service
import crypto_signals as cs

logger = logging.getLogger(__name__)
_refresh_lock = threading.Lock()


def _database_path():
    return Path(os.getenv("PORTFOLIO_DB", str(Path(__file__).parent / "portfolio.sqlite3")))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect():
    database = _database_path()
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize():
    with closing(_connect()) as connection:
        with connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS portfolio_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    purchase_price REAL NOT NULL,
                    target_profit_pct REAL NOT NULL,
                    stop_loss_pct REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    current_price REAL,
                    price_checked_at TEXT,
                    price_error TEXT,
                    alert_type TEXT,
                    alerted_at TEXT
                )"""
            )


def add_position(symbol, quantity, purchase_price, target_profit_pct, stop_loss_pct):
    initialize()
    with closing(_connect()) as connection:
        with connection:
            cursor = connection.execute(
                """INSERT INTO portfolio_positions
                   (symbol, quantity, purchase_price, target_profit_pct, stop_loss_pct, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (symbol, quantity, purchase_price, target_profit_pct, stop_loss_pct, _now()),
            )
            position_id = cursor.lastrowid
    return position_id


def list_positions():
    initialize()
    with closing(_connect()) as connection:
        rows = connection.execute(
            "SELECT * FROM portfolio_positions ORDER BY id DESC"
        ).fetchall()
    positions = []
    for row in rows:
        position = dict(row)
        target_price = position["purchase_price"] * (1 + position["target_profit_pct"] / 100)
        stop_price = position["purchase_price"] * (1 - position["stop_loss_pct"] / 100)
        price = position["current_price"]
        position.update({
            "target_price": target_price,
            "stop_price": stop_price,
            "current_value": price * position["quantity"] if price is not None else None,
            "profit_loss": (
                (price - position["purchase_price"]) * position["quantity"]
                if price is not None else None
            ),
            "return_pct": (
                (price / position["purchase_price"] - 1) * 100
                if price is not None else None
            ),
        })
        positions.append(position)
    return positions


def remove_position(position_id):
    initialize()
    with closing(_connect()) as connection:
        with connection:
            cursor = connection.execute(
                "DELETE FROM portfolio_positions WHERE id = ?", (position_id,)
            )
    return cursor.rowcount > 0


def refresh_positions(exchange=None, notify=cs.notify):
    with _refresh_lock:
        return _refresh_positions(exchange, notify)


def _refresh_positions(exchange, notify):
    positions = list_positions()
    if not positions:
        return {"positions": [], "alerts": []}

    prices = {}
    errors = {}
    exchange = exchange or os.getenv("EXCHANGE", "binance")
    for symbol in dict.fromkeys(position["symbol"] for position in positions):
        try:
            prices[symbol] = service.build_current_price(symbol, exchange)
        except Exception as error:
            logger.exception("Falha ao consultar cotacao de %s", symbol)
            errors[symbol] = "Não foi possível consultar a cotação agora."

    checked_at = _now()
    alerts = []
    with closing(_connect()) as connection:
        with connection:
            for position in positions:
                symbol = position["symbol"]
                if symbol in errors:
                    connection.execute(
                        """UPDATE portfolio_positions SET price_error = ?, price_checked_at = ?
                           WHERE id = ?""",
                        (errors[symbol], checked_at, position["id"]),
                    )
                    continue

                quote = prices[symbol]
                price = quote["price"]
                alert_type = None
                if price >= position["target_price"]:
                    alert_type = "target"
                elif price <= position["stop_price"]:
                    alert_type = "stop"

                cursor = connection.execute(
                    """UPDATE portfolio_positions
                       SET current_price = ?, price_checked_at = ?, price_error = NULL,
                           alert_type = CASE WHEN alert_type IS NULL THEN ? ELSE alert_type END,
                           alerted_at = CASE WHEN alert_type IS NULL AND ? IS NOT NULL
                                             THEN ? ELSE alerted_at END
                       WHERE id = ?""",
                    (price, checked_at, alert_type, alert_type, checked_at, position["id"]),
                )
                if alert_type and position["alert_type"] is None and cursor.rowcount:
                    alerts.append({
                        "symbol": symbol,
                        "alert_type": alert_type,
                        "price": price,
                        "target_price": position["target_price"],
                        "stop_price": position["stop_price"],
                    })

    for alert in alerts:
        threshold = alert["target_price"] if alert["alert_type"] == "target" else alert["stop_price"]
        label = "alvo de lucro" if alert["alert_type"] == "target" else "limite de perda"
        notify(
            f"[Carteira] {alert['symbol']} atingiu o {label} definido por voce.\n"
            f"Preco atual: {alert['price']:.8g}; limite: {threshold:.8g}.\n{service.DISCLAIMER}"
        )

    return {"positions": list_positions(), "alerts": alerts}


def monitor_loop(stop_event, exchange=None):
    poll = max(60, int(os.getenv("POLL_SECONDS", "300")))
    logger.info("Monitor de carteira iniciado; intervalo de %ss", poll)
    while not stop_event.is_set():
        try:
            refresh_positions(exchange)
        except Exception:
            logger.exception("Falha no ciclo de monitoramento da carteira")
        stop_event.wait(poll)
