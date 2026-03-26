import os
import json
from datetime import datetime, timezone
from src.config import RISK_PER_TRADE, SYMBOL, BOT_ID, JOURNAL_FILE
from src.logger import logger
from src.notifier import crear_notifier


# Estado interno para el Cortacircuitos (Daily Stop)
_last_check_date = None
_daily_losses = 0


# Archivo donde guardaremos el balance al inicio de cada día
LOG_DIR = os.path.abspath(os.path.dirname(JOURNAL_FILE) or "logs")
DAILY_RISK_FILE = os.path.join(LOG_DIR, f"daily_risk_{BOT_ID}.json")

# --- LÓGICA DE BALANCE DIARIO ---

def get_daily_initial_balance(current_balance):
    """
    Lee el balance con el que empezó el día.
    Si es un día nuevo, guarda el balance actual como el nuevo inicial.
    """
    today_str = datetime.now(timezone.utc).date().isoformat()
    
    # Intentamos leer si ya tenemos un balance guardado para HOY
    if os.path.exists(DAILY_RISK_FILE):
        try:
            with open(DAILY_RISK_FILE, "r") as f:
                data = json.load(f)
                if data.get("date") == today_str:
                    return float(data.get("initial_balance"))
        except Exception as e:
            logger.error(f"Error leyendo {DAILY_RISK_FILE}: {e}")

    # Si el archivo no existe o es un DÍA NUEVO, registramos el balance actual
    try:
        with open(DAILY_RISK_FILE, "w") as f:
            json.dump({"date": today_str, "initial_balance": current_balance}, f)
        logger.info(f"[{BOT_ID}] 🌅 Nuevo día detectado. Balance inicial fijado en {current_balance:.2f} USDT")
    except Exception as e:
        logger.error(f"Error guardando {DAILY_RISK_FILE}: {e}")
        
    return current_balance

def can_trade(trades_historicos):
    """
    Verifica si el bot tiene permitido operar hoy.
    Regla: Máximo 2 trades perdedores por día (UTC).
    """
    try:
        now = datetime.now(timezone.utc).date().isoformat()
        
        # Filtramos los trades de HOY que hayan resultado en PÉRDIDA
        losses_today = 0
        
        for t in trades_historicos:
            # 1. Verificamos que sea un trade de HOY, de ESTE BOT y que esté CERRADO
            is_today = str(t.get('close_time')).startswith(now)
            is_this_bot = t.get('bot_id') in [BOT_ID, "MANUAL"]
            is_closed = t.get('status') == 'CLOSED'
            
            if is_today and is_this_bot and is_closed:
                # 2. En lugar de buscar 'result', leemos directamente si el PNL fue negativo
                pnl = t.get('pnl_usdt', 0.0)
                if pnl < 0:
                    losses_today += 1
        
        if losses_today >= 2:
            logger.warning(f"[{BOT_ID}] 🛑 Cortacircuitos: Límite de {losses_today} pérdidas diarias alcanzado.")
            return False
            
        return True
    
    except Exception as e:
        logger.error(f"Error evaluando can_trade: {e}")
        # Por seguridad, si hay un error crítico leyendo, frenamos el bot
        return False

def calculate_position_size(balance, risk_pct, entry_price, sl_price):
    """
    Calcula la cantidad de cripto arriesgando un % del balance total,
    basado en la distancia EXACTA al Stop Loss.
    """
    try:
        # 1. ¿Cuántos dólares estamos dispuestos a perder?
        risk_usd = balance * (risk_pct / 100)
                
        # 2. Distancia real al SL por cada moneda
        sl_distance = abs(entry_price - sl_price)
        
        if sl_distance <= 0:
            logger.error(f"[{SYMBOL}] Error: Distancia al SL es 0. Entrada: {entry_price}, SL: {sl_price}")
            return 0.0
            
        # 3. Cantidad a operar.
        qty = risk_usd / sl_distance
        
        # Ajuste de precisión dinámico básico
        if "BTC" in SYMBOL:
            return round(qty, 3)
        else:
            return round(qty, 2)
    
    except Exception as e:
        logger.error(f"Error calculando tamaño de posición: {e}")
        return 0.0    
def check_drawdown_alert(current_balance, cycle_count):
    """Avisa por Telegram si la cuenta cae más del 10% del capital inicial DEL DÍA"""
    try:
        # Aquí entra la magia: buscamos con cuánto arrancamos hoy
        initial_balance = get_daily_initial_balance(current_balance)
        
        if initial_balance <= 0: return
        
        drop = (initial_balance - current_balance) / initial_balance
        
        if cycle_count % 60 == 0:
            if drop >= 0.10: # 10% de caída
                notifier = crear_notifier()
                notifier._send_async(f"🚨 <b>ALERTA DE DRAWDOWN DIARIO</b>\nLa cuenta cayó {drop*100:.1f}% hoy.\nInicio del día: {initial_balance:.2f} USDT\nActual: {current_balance:.2f} USDT")
    except Exception as e:
        logger.error(f"Error en alerta de drawdown: {e}")