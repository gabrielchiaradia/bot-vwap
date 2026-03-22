"""
src/live_writer.py
──────────────────
Exporta datos del journal VWAP a JSONs consumibles por el dashboard live del bot Scalp.
Aplica mapeo de variables para asegurar la compatibilidad de lectura.
"""

import json
import os
import threading
from datetime import datetime, timezone
from typing import Optional

# Importamos las variables específicas del VWAP
from src.config import BOT_ID, BOT_NAME, SYMBOL, TP_RR_RATIO, RISK_PER_TRADE, JOURNAL_FILE
from src.logger import logger

_lock = threading.Lock()
LOG_DIR = os.path.abspath(os.path.dirname(JOURNAL_FILE) or "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_BOOT_TIME = datetime.now(timezone.utc).isoformat()

# ── Paths dinámicos ───────────────────────────────────────
# Usan BOT_ID para no sobrescribir los archivos del bot Scalp si corren en el mismo server
def _dashboard_path() -> str:
    return os.path.join(LOG_DIR, f"dashboard_trades_{BOT_ID}.json")

def _positions_path() -> str:
    return os.path.join(LOG_DIR, f"open_positions_{BOT_ID}.json")

def _status_path() -> str:
    return os.path.join(LOG_DIR, f"bot_status_{BOT_ID}.json")

def _safe_write(path: str, data: dict | list):
    try:
        with _lock:
            temp_path = f"{path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, path)
    except Exception as e:
        logger.error(f"Error escribiendo JSON para Dashboard en {path}: {e}")

# ── Helpers de compatibilidad ─────────────────────────────

def _calc_duration(trade: dict) -> Optional[float]:
    """Calcula la duración usando los campos del VWAP (entry_time y close_time)"""
    try:
        # Soporta open_time (Scalp) o entry_time (VWAP)
        start_str = trade.get("open_time") or trade.get("entry_time") 
        end_str = trade.get("close_time")
        if not start_str or not end_str: 
            return None
        
        opened = datetime.fromisoformat(start_str)
        closed = datetime.fromisoformat(end_str)
        return round((closed - opened).total_seconds() / 60, 1)
    except Exception:
        return None

# ── Exportar estado del bot ───────────────────────────────

def exportar_status(balance: float, cycle_count: int, open_trades_count: int):
    """Estado general del bot formateado para el header del dashboard."""
    data = {
        "bot_name": BOT_NAME,
        "symbols": [SYMBOL],  # El Dashboard espera una lista
        "ltf": "1m",          # VWAP corre en 1m
        "htf": "1m",          # Mock para que el dashboard no tire error
        "rr": TP_RR_RATIO,
        "risk_per_trade": RISK_PER_TRADE,
        "max_open_trades": 1, # VWAP abre 1 a la vez
        "balance": round(balance, 2),
        "cycle_count": cycle_count,
        "open_trades": open_trades_count,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "uptime_since": _BOOT_TIME,
    }
    _safe_write(_status_path(), data)

# ── Exportar listas de trades ─────────────────────────────

def exportar_dashboard():
    """Lee el journal VWAP y lo formatea para el Frontend."""
    from src.journal import _load
    all_trades = _load()
    
    closed_trades = []
    open_trades = []

    for t in all_trades:
        # Creamos una copia para inyectar variables extra de compatibilidad
        t_dash = t.copy()
        
        # Mapeo vital: El dashboard Scalp busca "open_time"
        if "entry_time" in t_dash and "open_time" not in t_dash:
            t_dash["open_time"] = t_dash["entry_time"]
            
        t_dash["duration_min"] = _calc_duration(t_dash)
        
        if t_dash.get("status") == "CLOSED":
            closed_trades.append(t_dash)
        elif t_dash.get("status") == "OPEN":
            open_trades.append(t_dash)

    _safe_write(_dashboard_path(), closed_trades)
    _safe_write(_positions_path(), open_trades)