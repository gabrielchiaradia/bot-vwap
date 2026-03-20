import time
import uuid
from datetime import datetime, timezone
import pandas as pd

from src.config import SYMBOL, BOT_ID, BAND_MULT, TP_RR_RATIO, RISK_PER_TRADE, BOT_NAME
from src.logger import logger
from src.exchange import (
    get_client, get_futures_balance, set_leverage, 
    cancel_all_open_orders, place_limit_order, 
    place_sl_tp, get_open_position
)
from src.strategy import calculate_vwap_bands, get_vwap_signals
from src.risk import can_trade, calculate_quantity, check_drawdown_alert
from src.journal import record_open, record_close, _load
from src.live_writer import exportar_dashboard, exportar_status
from src.notifier import alert_trade_open, alert_trade_close, alert_error

def run_cycle(client):
    """Ciclo principal de ejecución (ejecutado cada ~60 segundos)"""
    try:
        # 1. Actualizar Datos y Estado
        balance = get_futures_balance(client)
        check_drawdown_alert(balance) # Alerta si cae > 10%
        
        # 2. Obtener velas (1m) para calcular VWAP
        # Pedimos 500 velas para tener suficiente historial del día
        candles = client.futures_klines(symbol=SYMBOL, interval='1m', limit=500)
        df = pd.DataFrame(candles, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'
        ])
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        for col in ['high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)

        # 3. Calcular Estrategia
        df = calculate_vwap_bands(df, mult=BAND_MULT)
        signals = get_vwap_signals(df)
        
        # 4. Verificar posición actual
        pos = get_open_position(client, SYMBOL)
        
        if pos:
            # ESTAMOS DENTRO: Monitorear si se cerró por SL o TP
            # Nota: El SL/TP en Binance cierra la posición automáticamente.
            # Solo exportamos estado al dashboard
            exportar_status(balance, cycle_count=0, open_count=1)
            exportar_dashboard()
            logger.debug(f"[{BOT_ID}] Posición abierta detectada. Monitoreando...")
            return

        # 5. NO ESTAMOS DENTRO: Gestionar órdenes LIMIT ("Pescar")
        trades_history = _load()
        if not can_trade(trades_history):
            logger.warning(f"[{BOT_ID}] Cortacircuitos activo (2 losses hoy). Esperando mañana.")
            cancel_all_open_orders(client, SYMBOL)
            return

        # Calculamos los niveles para la nueva orden LIMIT
        # Estrategia: Comprar en banda inferior, TP en VWAP.
        entry_price = signals['lower']
        vwap_price = signals['vwap']
        
        # Distancia para el SL basado en el RR
        dist_to_tp = vwap_price - entry_price
        sl_price = entry_price - (dist_to_tp * TP_RR_RATIO)

        # Si el precio actual está muy cerca o ya cruzó, no ponemos la orden este minuto
        last_close = df['close'].iloc[-1]
        if last_close <= entry_price:
            logger.info(f"[{BOT_ID}] El precio ya está en la zona de compra. Esperando estabilización.")
            return

        # 6. Actualizar "Anzuelo" (Cancel & Replace)
        cancel_all_open_orders(client, SYMBOL)
        
        qty = calculate_quantity(client, entry_price)
        if qty > 0:
            order = place_limit_order(client, SYMBOL, "BUY", entry_price, qty)
            
            if order and order.get('status') == 'FILLED':
                # Si se llenó instantáneamente (Market fallback)
                trade_id = str(uuid.uuid4())[:8]
                record_open(trade_id, SYMBOL, "LONG", entry_price, sl_price, vwap_price, qty, RISK_PER_TRADE)
                place_sl_tp(client, SYMBOL, "BUY", qty, sl_price, vwap_price)
                alert_trade_open(SYMBOL, "LONG", entry_price, sl_price, vwap_price, RISK_PER_TRADE)

        # Actualizar Dashboard
        exportar_status(balance, cycle_count=0, open_count=0)
        exportar_dashboard()

    except Exception as e:
        logger.error(f"Error en ciclo {SYMBOL}: {e}")
        alert_error(f"Ciclo {SYMBOL}", str(e))

def main():
    logger.info("="*50)
    logger.info(f"  BOT {BOT_NAME} INICIADO")
    logger.info(f"  Símbolo: {SYMBOL} | Riesgo: {RISK_PER_TRADE}%")
    logger.info("="*50)

    client = get_client()
    set_leverage(client, SYMBOL)

    while True:
        run_cycle(client)
        # Esperamos al cierre del minuto para recalcular bandas
        time.sleep(60)

if __name__ == "__main__":
    main()