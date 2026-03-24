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

def _all_positions_path() -> str:
    return os.path.join(LOG_DIR, f"open_positions_total.json")

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

# ── Exportar estado del bot ───────────────────────────────

def exportar_status(balance: float, cycle_count: int, pnl: float, margin: float,available: float, open_trades_count: int):
    """Estado general del bot formateado para el header del dashboard."""
    data = {
        "bot_name": BOT_NAME,
        "symbols": [SYMBOL],  
        "ltf": "1m",         
        "htf": "1m",          
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

    from src.journal import _load, _save
    
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
                active_map[p['symbol']] = {
                     "side": "LONG" if amt > 0 else "SHORT",
                    "pnl": float(p.get("unRealizedProfit", 0)),
                    "amt": amt
                }
    except Exception as e:
        logger.error(f"[DASHBOARD] Error consultando Binance: {e}")
        return # Si falla la API, no arriesgamos a cerrar trades por error

    for t in all_trades:
        t_dash = t.copy()
        symbol = t_dash["symbol"]
        side_bot = t_dash["direction"]
        
        # Saltamos trades que no son de este Bot para el archivo individual
        if t_dash.get("bot_id") != BOT_ID and t_dash.get("status") == "OPEN":
            continue

        # BUSCAMOS LA POSICIÓN REAL EN EL MAPA
        real_pos = active_map.get(symbol)
        
        # --- LÓGICA DE SINCRONIZACIÓN PROTEGIDA ---
        if t_dash.get("status") == "OPEN":
            debe_cerrar = False

            # Solo cerramos automáticamente si hay una posición OPUESTA (indica cierre y vuelta)
            if real_pos and real_pos["side"] != side_bot:
                debe_cerrar = True
                logger.warning(f"🔄 Detectada dirección opuesta en {symbol}. Sincronizando...")

            # Si NO hay posición en Binance, no cerramos de inmediato (evita errores de API)
            # El bot solo marcará como CLOSED si el ciclo de trading principal (main.py) lo decide   
            if debe_cerrar:
                logger.warning(f"🧹 Limpiando trade desincronizado: {symbol} {side_bot} (ID: {t_dash['trade_id']})")
                t_dash["status"] = "CLOSED"
                t_dash["close_time"] = ahora
                # Intentamos rescatar el último PnL cerrado
                try:
                    trades = client.futures_account_trades(symbol=symbol, limit=1)
                    if trades:
                        t_dash["pnl_usdt"] = float(trades[0].get("realizedPnl", 0))
                        t_dash["exit_price"] = float(trades[0].get("price", 0))
                except: pass
                
                t["status"] = "CLOSED"
                t["close_time"] = ahora
                t["pnl_usdt"] = t_dash.get("pnl_usdt", 0)
                t["exit_price"] = t_dash.get("exit_price")
                modified_journal = True
                
        # --- PREPARACIÓN PARA FRONTEND ---
        if t_dash.get("status") == "CLOSED":
            t_dash["pnl"] = t_dash.get("pnl_usdt", 0)
            t_dash["entry"] = t_dash.get("entry_price")
            t_dash["exit"] = t_dash.get("exit_price")
            closed_trades.append(t_dash)
        else:
                # Mapeo de campos que el Dashboard espera ver
                t_dash["pnl"] = round(real_pos["pnl"], 2) if real_pos else 0.0
                t_dash["entry"] = t_dash.get("entry_price")
                t_dash["sl"] = t_dash.get("sl_price")
                t_dash["tp"] = t_dash.get("tp_price")
                t_dash["time"] = t_dash.get("entry_time")
                t_dash["bot"] = t_dash.get("bot_id")
                
                # 3. Cálculo de Capital (Cantidad * Precio Entrada)
                qty = float(t_dash.get("quantity", 0))
                price = float(t_dash.get("entry_price", 0))
                t_dash["capital"] = round(qty * price, 2)
            
                t_dash["pnl"] = 0.0
            
                open_trades.append(t_dash)

    # Si hubo cierres, actualizamos la "Base de Datos" (journal.json)
    if modified_journal:
        _save(all_trades)
        logger.info(f"✅ Sincronización completa: {len(all_trades)} trades en el journal.")
        
    # 2. Exportamos las vistas filtradas para JavaScript
    _safe_write(_positions_path(), open_trades)  # Va a positions.json
    _safe_write(_dashboard_path(), closed_trades) # Va a dashboard.json

    # --- UNIFICACIÓN SIN PISARSE ---
    ruta_total = _all_positions_path()
    datos_finales = []
    if os.path.exists(ruta_total):
        try:
            with open(ruta_total, 'r') as f:
                existentes = json.load(f)
                # Mantenemos todo lo que NO sea de este Bot
                datos_finales = [x for x in existentes if x.get("bot_id") != BOT_ID]
        except: pass
    
    # Agregamos nuestras posiciones actuales (si las hay)
    datos_finales.extend(open_trades)
    _safe_write(ruta_total, datos_finales)