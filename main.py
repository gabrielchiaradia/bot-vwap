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
from src.risk import can_trade, calculate_position_size, check_drawdown_alert
from src.journal import record_open, record_close, _load
from src.live_writer import exportar_dashboard, exportar_status
from src.notifier import alert_trade_open, alert_trade_close, alert_error, alert_startup

def run_cycle(client, cycle_count): # Agregamos el cycle_count como parámetro
    try:
        balance = get_futures_balance(client)
        check_drawdown_alert(balance)
        # Revisar posiciones abiertas para el dashboard
        pos_abierta = get_open_position(client, SYMBOL)
        open_count = 1 if pos_abierta else 0
        # Obtenemos datos 
        candles = client.futures_klines(symbol=SYMBOL, interval='1m', limit=1500)
        df = pd.DataFrame(candles, columns=['timestamp','open','high','low','close','volume','ct','qav','tr','tba','tqa','i'])
        df['open_time'] = pd.to_datetime(df['timestamp'], unit='ms')
        cols = ['open','high','low','close','volume']
        df[cols] = df[cols].astype(float)
        
        # --- 1. ESTRATEGIA: Evaluar el mercado ---
        df_bands = calculate_vwap_bands(df, mult=BAND_MULT)
        
        # --- 2. Generar Señales ---
        signal, entry_price, tp_price = get_vwap_signals(df_bands)
        
        # ACTUALIZAR DASHBOARD SIEMPRE AL FINALIZAR LECTURA

        exportar_status(balance, cycle_count, open_count)
        exportar_dashboard()
        
        # -- 3. EJECUCIÓN: Si hay señal y no hay posición ---
        if signal and open_count == 0:
            print(f"[{datetime.now()}] ¡Señal {signal} detectada a {entry_price}!")
            
            # A. Calcular distancia base usando volatilidad pura (Desviación Estándar)
            last_row = df_bands.iloc[-1]
            dist_sl = last_row['std_dev'] * 1.5
            
            # B. Definir el precio del Stop Loss          
            if signal == "LONG":
                sl_price = entry_price - dist_sl
                tp_price = entry_price + (dist_sl * TP_RR_RATIO)
                side = "BUY"
            else:
                sl_price = entry_price + dist_sl
                tp_price = entry_price - (dist_sl * TP_RR_RATIO)
                side = "SELL"
           
           # C. GESTIÓN DE RIESGO DINÁMICA
            qty = calculate_position_size(
                balance=balance,           # Tu balance en vivo (ej: 5000)
                risk_pct=RISK_PER_TRADE,   # Tu % del .env (ej: 3.0)
                entry_price=entry_price,   # Precio de entrada
                sl_price=sl_price          # Precio del SL calculado por tu estrategia
            )
            
            # D. Cargar ordenes
            if qty > 0:
                cancel_all_open_orders(client, SYMBOL)
                
                # 1. Poner orden de entrada
                order = place_limit_order(client, SYMBOL, side, entry_price, qty)
                
                # 2. Si Binance la recibe ('NEW') o la ejecuta de una ('FILLED')...
                if order and order.get('status') in ['NEW', 'FILLED']:
                    trade_id = str(uuid.uuid4())[:8]
                    # ...clavamos el SL y TP en la exchange en ese mismo milisegundo ("y chau")
                    place_sl_tp(client, SYMBOL, side, qty, sl_price, tp_price)
                    record_open(trade_id, SYMBOL, signal, entry_price, sl_price, tp_price, qty, RISK_PER_TRADE)
                    alert_trade_open(SYMBOL, signal, entry_price, sl_price, tp_price, RISK_PER_TRADE)
                    
                    # Forzar refresh del dashboard al abrir trade
                    exportar_status(balance, cycle_count, 1)
                    exportar_dashboard()
                else:
                    logger.warning(f"Orden rechazada o fallida. Estado: {order.get('status') if order else 'None'}")
                    
    except Exception as e:
        logger.error(f"Error en ciclo {SYMBOL}: {e}")
        alert_error(f"Ciclo {SYMBOL}", str(e))
        logger.error(f"Error en ciclo {SYMBOL}: {e}")
        alert_error(f"Ciclo {SYMBOL}", str(e))

def main():
    logger.info("="*50)
    logger.info(f"  BOT {BOT_NAME} INICIADO")
    logger.info(f"  Símbolo: {SYMBOL}")
    client = get_client()
    balance = get_futures_balance(client)
    balanceinicial = int(balance)
    alert_startup(SYMBOL, RISK_PER_TRADE, TP_RR_RATIO, balanceinicial )
    set_leverage(client, SYMBOL)

    cycle_count = 0
    while True:
        if cycle_count % 60 == 0:
            logger.info("="*50)
            logger.info(f"  BOT {BOT_NAME}  R/R: {TP_RR_RATIO} Riesgo por trade: {RISK_PER_TRADE}%")
            logger.info(F"  Bot corriendo daunte {cycle_count} minutos")
            logger.info("="*50)
        run_cycle(client, cycle_count)
        cycle_count += 1      
        time.sleep(60)  # Esperamos al cierre del minuto para recalcular bandas

if __name__ == "__main__":
    main()

   

        