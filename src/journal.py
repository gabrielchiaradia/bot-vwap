import json
import os
from datetime import datetime, timezone
from src.config import JOURNAL_FILE, BOT_ID
from src.logger import logger

# Asegurar que el directorio de logs exista
os.makedirs(os.path.dirname(JOURNAL_FILE) or "logs", exist_ok=True)

def _load() -> list:
    if not os.path.exists(JOURNAL_FILE):
        return []
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

def _save(trades: list):
    with open(JOURNAL_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2, ensure_ascii=False)

def record_open(trade_id, symbol, direction, entry_price, sl_price, tp_price, quantity, risk_pct):
    trades = _load()
    nuevo_trade = {
        "trade_id": trade_id,
        "bot_id": BOT_ID, # Diferenciador para el Dashboard
        "symbol": symbol,
        "direction": direction,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "quantity": quantity,
        "risk_pct": risk_pct,
        "status": "OPEN",
        "result": None,
        "exit_price": None,
        "pnl_usdt": 0.0,
        "close_time": None
    }
    trades.append(nuevo_trade)
    _save(trades)
    logger.info(f"[{BOT_ID}] Trade guardado en Journal: {trade_id}")
    return nuevo_trade

def record_close(trade_id, exit_price, pnl_usdt):
    trades = _load()
    for t in trades:
        if t["trade_id"] == trade_id and t["status"] == "OPEN":
            t["status"] = "CLOSED"
            t["exit_price"] = exit_price
            t["pnl_usdt"] = pnl_usdt
            t["close_time"] = datetime.now(timezone.utc).isoformat()
            t["result"] = "WIN" if pnl_usdt > 0 else "LOSS"
            _save(trades)
            logger.info(f"[{BOT_ID}] Trade cerrado en Journal: {trade_id} PnL: {pnl_usdt}")
            return