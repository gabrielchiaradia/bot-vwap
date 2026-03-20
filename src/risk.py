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

def calculate_quantity(client, price):
    """
    Calcula la cantidad (Qty) basada en el riesgo dinámico del .env
    Riesgo = (Balance * RISK_PER_TRADE) / Distancia al SL
    """
    try:
        acc = client.futures_account()
        balance = float(acc['availableBalance'])
        
        # Validación de Alerta de Drawdown (10%)
        total_wallet = float(acc['totalMarginBalance'])
        # Si el balance actual es 10% menor al inicial (asumiendo 1000 de base o similar)
        # Aquí podrías comparar contra un histórico, por ahora avisamos si el balance baja fuerte
        
        # Cálculo de riesgo en dólares
        risk_usd = balance * (RISK_PER_TRADE / 100)
        
        # En VWAP, la distancia al SL es técnica (RR 0.4)
        # Usamos una distancia estándar de seguridad para el cálculo inicial de qty
        # o calculamos basado en el SL real que usará la estrategia
        # Para simplificar y ser conservadores:
        stop_dist_pct = 0.01  # Asumimos un SL del 1% para el dimensionamiento
        
        qty = risk_usd / (price * stop_dist_pct)
        
        # Ajuste de precisión (ETH suele ser 3 decimales, BTC 3)
        if "BTC" in SYMBOL:
            return round(qty, 3)
        return round(qty, 2)
        
    except Exception as e:
        logger.error(f"Error calculando riesgo: {e}")
        return 0.0

def check_drawdown_alert(balance, initial_balance=1000):
    """Avisa por Telegram si la cuenta cae más del 10% del capital inicial"""
    drop = (initial_balance - balance) / initial_balance
    if drop >= 0.10:
        msg = _tag(f"🚨 <b>ALERTA DE DRAWDOWN</b>\nLa cuenta ha caído un {drop*100:.1f}%\nBalance actual: {balance:.2f} USDT")
        _send_async(msg)