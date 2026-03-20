import json
import os
import threading
from datetime import datetime, timezone
from src.config import BOT_ID, BOT_NAME, SYMBOL, JOURNAL_FILE

_lock = threading.Lock()
LOG_DIR = os.path.dirname(JOURNAL_FILE) or "logs"

def _safe_write(path, data):
    with _lock:
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_path, path)

def exportar_dashboard():
    """Exporta trades cerrados y abiertos específicos de este BOT_ID"""
    from src.journal import _load
    all_trades = _load()
    
    # Filtrar solo lo que corresponde a este proceso
    closed_trades = [t for t in all_trades if t["status"] == "CLOSED"]
    open_trades = [t for t in all_trades if t["status"] == "OPEN"]

    # Archivo de trades históricos para gráficos
    _safe_write(os.path.join(LOG_DIR, f"dashboard_trades_{BOT_ID}.json"), closed_trades)
    # Archivo de posiciones actuales
    _safe_write(os.path.join(LOG_DIR, f"open_positions_{BOT_ID}.json"), open_trades)

def exportar_status(balance, cycle_count, open_count):
    """Estado del header del Dashboard"""
    data = {
        "bot_id": BOT_ID,
        "bot_name": BOT_NAME,
        "symbol": SYMBOL,
        "balance": round(balance, 2),
        "cycle_count": cycle_count,
        "open_trades": open_count,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    _safe_write(os.path.join(LOG_DIR, f"bot_status_{BOT_ID}.json"), data)