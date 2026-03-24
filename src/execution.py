# src/execution.py

import time
import uuid
from datetime import datetime, timezone
from src.logger import logger
from src.config import BOT_ID
from src.journal import record_open, _load, _save
from src.notifier import alert_trade_open
from src.exchange import (
    cancel_all_open_orders, 
    place_limit_order, 
    place_sl_tp,
    verificar_y_rescatar_sl_tp,
    get_open_position
)

def gestionar_resguardo_posicion(client, symbol):
    """
    Busca el trade activo en el journal y llama a la función de exchange
    para asegurar que los Stop Loss y Take Profit sigan vivos en Binance.
    """
    try:
        all_trades = _load()
        
        # Buscamos en la lista el trade que coincida con el símbolo, que sea de este bot y esté OPEN
        current_trade = next((t for t in all_trades 
                              if t.get('symbol') == symbol 
                              and t.get('status') == 'OPEN' 
                              and t.get('bot_id') == BOT_ID), None)
        
        if current_trade:
            # Si lo encontramos, le pasamos la pelota a tu función de rescate original
            verificar_y_rescatar_sl_tp(client, symbol, current_trade)
        else:
            logger.warning(f"[{symbol}] Hay posición en Binance pero no encontré el trade OPEN en el Journal.")
            
    except Exception as e:
        logger.error(f"Error en gestionar_resguardo_posicion para {symbol}: {e}")

def ejecutar_apertura_completa(client, symbol, signal, entry_price, sl_price, tp_price, qty, risk_pct):
    """
    Orquesta la apertura: Cancela previas, pone LIMIT, espera FILL y clava SL/TP.
    """
    try:
        # 1. Limpieza previa
        cancel_all_open_orders(client, symbol)
        
        # 2. Enviar orden principal
        side = "BUY" if signal == "LONG" else "SELL"
        order = place_limit_order(client, symbol, side, entry_price, qty)
        
        if not order or order.get('status') not in ['NEW', 'FILLED']:
            logger.warning(f"[{symbol}] Orden LIMIT rechazada o fallida.")
            return False

        # Creamos el ID único para el journal
        trade_id = str(uuid.uuid4())[:8]
        logger.info(f"[{symbol}] Orden LIMIT enviada (ID: {trade_id}). Esperando ejecución...")

        # 3. Bucle de espera (Wait for Fill) - Tu lógica de 10 segundos
        filled = False
        for _ in range(10):
            pos_info = client.futures_position_information(symbol=symbol)
            if any(float(p['positionAmt']) != 0 for p in pos_info if p['symbol'] == symbol):
                filled = True
                break
            time.sleep(1)

        # 4. Acciones post-fill
        if filled:
            try:
                place_sl_tp(client, symbol, side, qty, sl_price, tp_price)
                logger.info(f"[{symbol}] ✅ Posición detectada. SL/TP colocados.")
            except Exception as e:
                logger.error(f"Error colocando SL/TP post-fill: {e}")
        else:
            logger.warning(f"[{symbol}] ⚠️ LIMIT no se llenó en 10s. El SL/TP se colocará en el próximo ciclo de monitoreo.")

        # 5. Registro y Notificación
        record_open(trade_id, symbol, signal, entry_price, sl_price, tp_price, qty, risk_pct)
        alert_trade_open(symbol, signal, entry_price, sl_price, tp_price, risk_pct)
        
        return True

    except Exception as e:
        logger.error(f"Error crítico en ejecutar_apertura_completa: {e}")
        return False
    
def sincronizar_realidad_vs_journal(client, symbol):
    """
    Audita Binance vs Journal para:
    1. Registrar trades abiertos a mano.
    2. Cerrar trades en el journal con el PnL REAL si se cerraron (SL/TP o a mano).
    """
    try:
        all_trades = _load()
        pos_real = get_open_position(client, symbol) # Lo que hay en Binance
        
        # Buscamos qué dice el journal que debería estar abierto para este símbolo
        open_in_journal = [t for t in all_trades if t.get('symbol') == symbol and t.get('status') == 'OPEN']
        
        modified = False
        ahora = datetime.now(timezone.utc).isoformat()

        # ==========================================
        # CASO 1: SE CERRÓ (SL, TP o lo cerraste a mano)
        # ==========================================
        if not pos_real and open_in_journal:
            for t in open_in_journal:
                logger.info(f"[{symbol}] Detectado cierre externo. Buscando PnL real en Binance...")
                t['status'] = 'CLOSED'
                t['close_time'] = ahora
                
                # Vamos a buscar a Binance cuánta plata ganaste/perdiste realmente
                try:
                    # Traemos los últimos 5 trades de la cuenta para ese símbolo
                    historial = client.futures_account_trades(symbol=symbol, limit=5)
                    pnl_real = 0.0
                    precio_salida = 0.0
                    
                    # Sumamos el PnL de las órdenes que cerraron esta posición
                    for operacion in reversed(historial):
                        pnl_op = float(operacion.get('realizedPnl', 0))
                        if pnl_op != 0:
                            pnl_real += pnl_op
                            precio_salida = float(operacion.get('price', 0))
                            
                    t['pnl_usdt'] = round(pnl_real, 2)
                    t['exit_price'] = precio_salida if precio_salida > 0 else t.get('entry_price')
                    logger.info(f"[{symbol}] Trade cerrado en journal. PnL Real: {t['pnl_usdt']} USDT")
                except Exception as e:
                    logger.error(f"Error buscando PnL en Binance: {e}")
                    t['pnl_usdt'] = 0.0
                    
                modified = True

        # ==========================================
        # CASO 2: SE ABRIÓ A MANO DESDE EL CELULAR/PC
        # ==========================================
        elif pos_real and not open_in_journal:
            logger.warning(f"[{symbol}] ⚠️ Detectada posición abierta a mano. Registrando en Journal...")
            
            nuevo_trade = {
                "trade_id": f"MANUAL-{str(uuid.uuid4())[:4]}",
                "bot_id": "MANUAL", # Para que sepas que no fue la estrategia
                "symbol": symbol,
                "direction": pos_real['side'],
                "entry_price": pos_real['entry'],
                "sl_price": 0.0, # Al ser manual, asume 0 hasta que el rescatador actúe
                "tp_price": 0.0,
                "quantity": pos_real['size'],
                "risk_pct": 0.0,
                "status": "OPEN",
                "entry_time": ahora,
                "close_time": None,
                "pnl_usdt": 0.0
            }
            all_trades.append(nuevo_trade)
            modified = True
            
        # ==========================================
        # CASO 3: FLIP A MANO (Estabas LONG, y abriste SHORT de golpe)
        # ==========================================
        elif pos_real and open_in_journal:
            t = open_in_journal[0]
            if t['direction'] != pos_real['side']:
                logger.warning(f"[{symbol}] Cambio de dirección manual detectado. Cerrando anterior...")
                t['status'] = 'CLOSED'
                t['close_time'] = ahora
                # Acá podrías repetir la lógica de buscar el PnL si querés
                modified = True

        if modified:
            _save(all_trades)
            
    except Exception as e:
        logger.error(f"Error en sincronizador: {e}")