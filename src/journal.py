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

def record_open(trade_id, symbol, direction, entry_price, sl_price, tp_price, quantity, risk_pct, balance_at_open: float = 0.0, status: str = "OPEN", bias: str = "MEAN_REV"):
    trades = _load()
    nuevo_trade = {
        "trade_id": trade_id,
        "bot_id": BOT_ID,
        "symbol": symbol,
        "direction": direction,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "quantity": quantity,
        "risk_pct": risk_pct,
        "balance_at_open": balance_at_open,
        "bias": bias,
        "status": status,
        "result": None,
        "exit_price": None,
        "pnl_usdt": 0.0,
        "close_time": None
    }
    trades.append(nuevo_trade)
    _save(trades)
    label = "PENDING_FILL" if status == "PENDING_FILL" else "Trade guardado"
    logger.info(f"[{BOT_ID}] {label} en Journal: {trade_id} | Bias: {bias}")
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
