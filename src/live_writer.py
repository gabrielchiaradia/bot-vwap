# src/live_writer.py
import json
import os
import threading
from datetime import datetime, timezone

from src.config import BOT_ID, BOT_NAME, SYMBOL, TP_RR_RATIO, RISK_PER_TRADE, JOURNAL_FILE, BAND_MULT
from src.logger import logger

_lock = threading.Lock()
LOG_DIR = os.path.abspath(os.path.dirname(JOURNAL_FILE) or "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ── Paths dinámicos usando BOT_ID ──────────────────────────
def _dashboard_path():
    return os.path.join(LOG_DIR, f"dashboard_trades_{BOT_ID}.json")

def _positions_path():
    return os.path.join(LOG_DIR, f"open_positions_{BOT_ID}.json")

def _all_positions_path():
    return os.path.join(LOG_DIR, f"open_positions_total.json")

def _status_path():
    return os.path.join(LOG_DIR, f"bot_status_{BOT_ID}.json")

def _safe_write(path: str, data):
    try:
        with _lock:
            temp_path = f"{path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, path)
    except Exception as e:
        logger.error(f"Error escribiendo {path}: {e}")

# ── Exportar listas de trades ─────────────────────────────
def exportar_dashboard(client):
    from src.journal import _load
    from src.exchange import get_account_status
    
    all_trades = _load()
    
    # 1. Separamos trades CERRADOS y ABIERTOS para ESTE BOT
    closed_trades = [t for t in all_trades if t.get('status') == 'CLOSED' and t.get('bot_id') in [BOT_ID, "MANUAL"]]
    open_trades_journal = [t for t in all_trades if t.get('status') == 'OPEN' and t.get('bot_id') in [BOT_ID, "MANUAL"]]

    total_trades = len(closed_trades)
    pnl_neto_total = sum(t.get('pnl_usdt', 0) for t in closed_trades)
    fees_totales = sum(t.get('fees', 0) for t in closed_trades)
    pnl_bruto_total = sum(t.get('pnl_bruto', 0) for t in closed_trades)

    # ==========================================
    # LÓGICA 1: TRADES CERRADOS (Formato Backtest)
    # ==========================================
    wins, losses, pnl_total, gross_profit, gross_loss = 0, 0, 0.0, 0.0, 0.0
    current_balance = get_account_status(client).get('wallet_balance', 1000)
    capital_simulado = current_balance - sum(t.get('pnl_usdt', 0) for t in closed_trades)
    
    formatted_closed = []
    
    for t in closed_trades:
        pnl = t.get('pnl_usdt', 0.0)
        pnl_total += pnl
        
        if pnl > 0:
            wins += 1
            gross_profit += pnl
            result = "WIN"
        else:
            losses += 1
            gross_loss += abs(pnl)
            result = "LOSS"
            
        capital_simulado += pnl
        
        try:
            t_in = datetime.fromisoformat(t['entry_time'])
            t_out = datetime.fromisoformat(t['close_time'])
            duration = round((t_out - t_in).total_seconds() / 60, 1)
        except:
            duration = 0.0

        t_dash = {
            "time": t.get("entry_time"),
            "close_time": t.get("close_time"),
            "symbol": t.get("symbol"),
            "direction": t.get("direction"),
            "entry": t.get("entry_price"),
            "sl": t.get("sl_price"),
            "tp": t.get("tp_price"),
            "exit": t.get("exit_price", t.get("entry_price")),
            "result": result,
            "pnl_bruto": round(pnl, 2),
            "fees": 0.0, 
            "pnl": round(pnl, 2),
            "capital": round(capital_simulado, 2),
            "score": 100,
            "duration_min": duration,
            "bias": "MEAN_REV",
            "ob_zone": f"Band_{BAND_MULT}s"
        }
        formatted_closed.append(t_dash)

    total_trades = wins + losses
    winrate = round((wins / total_trades * 100), 2) if total_trades > 0 else 0.0
    profit_factor = round((gross_profit / gross_loss), 2) if gross_loss > 0 else round(gross_profit, 2)
    
    dashboard_data = {
        "summaries": [{
            "label": f"LIVE_{BOT_ID}",
            "symbol": SYMBOL,
            "ltf": "1m",
            "htf": "1m",
            "band_mult": BAND_MULT,
            "rr": TP_RR_RATIO,
            "risk_pct": RISK_PER_TRADE,
            "total": total_trades,
            "wins": wins,
            "losses": losses,
            "winrate": winrate,
            "profit_factor": profit_factor,
            "pnl_total": round(pnl_total, 2),
            "capital_final": round(current_balance, 2),
            "pnl_bruto": round(pnl_bruto_total, 2),
            "fees_totales": round(fees_totales, 2),
            "pnl_total": round(pnl_neto_total, 2),
            "trades": formatted_closed
        }]
    }
    _safe_write(_dashboard_path(), dashboard_data)

    # ==========================================
    # LÓGICA 2: POSICIONES ABIERTAS (En vivo)
    # ==========================================
    formatted_open = []
    
    if open_trades_journal:
        try:
            # Traemos el PnL flotante real desde Binance
            actual_positions = client.futures_position_information(symbol=SYMBOL)
            real_pos = next((p for p in actual_positions if float(p['positionAmt']) != 0), None)
            
            for t in open_trades_journal:
                t_dash = t.copy()
                
                # Mapeo de campos que el Dashboard espera ver
                t_dash["pnl"] = round(float(real_pos["unRealizedProfit"]), 2) if real_pos else 0.0
                t_dash["entry"] = t.get("entry_price")
                t_dash["sl"] = t.get("sl_price")
                t_dash["tp"] = t.get("tp_price")
                t_dash["time"] = t.get("entry_time")
                t_dash["bot"] = BOT_ID
                
                # Cálculo de Capital invertido
                qty = float(t.get("quantity", 0))
                price = float(t.get("entry_price", 0))
                t_dash["capital"] = round(qty * price, 2)
                
                formatted_open.append(t_dash)
        except Exception as e:
            logger.error(f"[DASHBOARD] Error consultando Binance para posiciones abiertas: {e}")

    _safe_write(_positions_path(), formatted_open)

    # ==========================================
    # LÓGICA 3: UNIFICAR TODAS LAS POSICIONES (Multi-bot)
    # ==========================================
    ruta_total = _all_positions_path()
    datos_finales = []
    if os.path.exists(ruta_total):
        try:
            with open(ruta_total, 'r') as f:
                existentes = json.load(f)
                # Mantenemos todo lo que NO sea de este Bot
                datos_finales = [x for x in existentes if x.get("bot") != BOT_ID]
        except: pass
    
    datos_finales.extend(formatted_open)
    _safe_write(ruta_total, datos_finales)

# ── Exportar estado del bot ───────────────────────────────
def exportar_status(balance: float, cycle_count: int, pnl: float, margin: float, available: float, open_trades_count: int):
    """Estado general del bot formateado para el header del dashboard."""
    data = {
        "bot_name": BOT_NAME,
        "symbols": [SYMBOL],  
        "ltf": "1m",         
        "htf": "1m",          
        "rr": TP_RR_RATIO,
        "risk_per_trade": RISK_PER_TRADE,
        "max_open_trades": 1, 
        "balance": round(balance, 2),
        "cycle_count": cycle_count,
        "pnl": round(pnl, 2),
        "margin": round(margin, 2),
        "available": round(available, 2),
        "open_trades": open_trades_count,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    _safe_write(_status_path(), data)