from src.config import RISK_PER_TRADE, SYMBOL, BOT_ID
from src.logger import logger
from src.notifier import _send_async, _tag

# Estado interno para el Cortacircuitos (Daily Stop)
# Se reinicia al detectar un cambio de fecha
_last_check_date = None
_daily_losses = 0

def _check_daily_reset(current_date):
    global _last_check_date, _daily_losses
    if _last_check_date != current_date:
        _last_check_date = current_date
        _daily_losses = 0
        logger.info(f"[{BOT_ID}] Nuevo día detectado. Contador de pérdidas reiniciado.")

def can_trade(trades_today):
    """
    Verifica si el bot tiene permitido operar hoy.
    Regla: Máximo 2 trades perdedores por día.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).date()
    _check_daily_reset(now)
    
    losses_today = len([t for t in trades_today if t.get('result') == 'LOSS' 
                        and t.get('close_time', '').startswith(now.isoformat())])
    
    if losses_today >= 2:
        return False
    return True

def calculate_position_size(balance, risk_pct, entry_price, sl_price):
    """
    Calcula la cantidad de cripto arriesgando un % del balance total,
    basado en la distancia EXACTA al Stop Loss.
    """
    try:
        # 1. ¿Cuántos dólares estamos dispuestos a perder? (Ej: 5000 * 3% = 150 USD)
        risk_usd = balance * (risk_pct / 100)
                
        # 2. Distancia real al SL por cada moneda
        sl_distance = abs(entry_price - sl_price)
        
        if sl_distance <= 0:
            print("Error: Distancia al SL es 0.")
            return 0.0
            
        # 3. Cantidad a operar.
        qty = risk_usd / sl_distance
        
        # Ajuste de precisión (ETH 2 decimales, BTC 3)
        if "BTC" in SYMBOL:
            return round(qty, 3)
        return round(qty, 2)
    
    except Exception as e:
            print(f"Error calculando riesgo: {e}")
            return 0.0    
        
def check_drawdown_alert(balance, initial_balance=1000):
    """Avisa por Telegram si la cuenta cae más del 10% del capital inicial"""
    drop = (initial_balance - balance) / initial_balance
    if drop >= 0.10:
        msg = _tag(f"🚨 <b>ALERTA DE DRAWDOWN</b>\nLa cuenta ha caído un {drop*100:.1f}%\nBalance actual: {balance:.2f} USDT")
        _send_async(msg)