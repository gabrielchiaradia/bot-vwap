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

def exportar_status(balance: float, cycle_count: int, pnl: float, margin: float,available: float, open_trades_count: int):
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
        "pnl": round(pnl, 2),
        "margin": round(margin, 2),
        "available": round(available, 2),
        "open_trades": open_trades_count,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "uptime_since": _BOOT_TIME,
    }
    _safe_write(_status_path(), data)

# ── Exportar listas de trades ─────────────────────────────

def exportar_dashboard(client):
    from datetime import datetime, timezone
    from src.journal import _load, _safe_write, _journal_path
    
    all_trades = _load()
    ahora = datetime.now(timezone.utc).isoformat() # Capturamos el momento exacto
    
    modified_journal = False
    closed_trades = []
    open_trades = []

    try:
        # Obtenemos info de todas las posiciones abiertas en Binance
        actual_positions = client.futures_position_information()
        # Mapeamos los símbolos activos para búsqueda rápida
        active_map = {}
        for p in actual_positions:
            amt = float(p['positionAmt'])
            if amt != 0:
                # Guardamos la dirección real de Binance para comparar
                side_real = "LONG" if amt > 0 else "SHORT"
                active_map[p['symbol']] = {
                    "side": side_real,
                    "pnl": float(p.get("unrealizedProfit", 0)),
                    "amt": amt
                }
    except Exception as e:
        print(f"[DASHBOARD] Error consultando Binance: {e}")
        return # Si falla la API, no arriesgamos a cerrar trades por error

    for t in all_trades:
        t_dash = t.copy()
        symbol = t_dash["symbol"]
        side_bot = t_dash["direction"]
        
        # BUSCAMOS LA POSICIÓN REAL EN EL MAPA
        real_pos = active_map.get(symbol)
        
       # LÓGICA DE CIERRE MEJORADA:
        # Si el journal dice OPEN pero:
        # 1. No hay nada en Binance para ese símbolo OR
        # 2. Hay algo en Binance pero es de la dirección OPUESTA (ej: Bot dice LONG y hay un SHORT)
        if t_dash.get("status") == "OPEN":
            debe_cerrar = False
            if not real_pos:
                debe_cerrar = True
                print(f"✅ Sincronizando cierre de {symbol}...")
            elif real_pos["side"] != side_bot:
                debe_cerrar = True
                print(f"✅ Sincronizando cierre de {symbol}...")
                
            if debe_cerrar:
                print(f"🧹 Limpiando trade desincronizado: {symbol} {side_bot} (ID: {t_dash['trade_id']})")
                t_dash["status"] = "CLOSED"
                t_dash["close_time"] = ahora
                t["status"] = "CLOSED"
                t["close_time"] = ahora
                modified_journal = True
                
        # Clasificación para los archivos del Frontend
        if t_dash.get("status") == "CLOSED":
            closed_trades.append(t_dash)
        else:
            # CASO B: El trade sigue OPEN, actualizamos su PnL en vivo para el front
           if real_pos:
                t_dash["unrealized_pnl"] = real_pos["pnl"]
                open_trades.append(t_dash)

    # 1. Si hubo cierres, actualizamos la "Base de Datos" (journal.json)
    if modified_journal:
        _safe_write(_journal_path(), all_trades)

    # 2. Exportamos las vistas filtradas para JavaScript
    _safe_write(_positions_path(), open_trades)  # Va a positions.json
    _safe_write(_dashboard_path(), closed_trades) # Va a dashboard.json