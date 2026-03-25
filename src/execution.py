# src/execution.py

import time
import uuid
from datetime import datetime, timezone
from src.logger import logger
from src.config import BOT_ID
from src.journal import record_open, _load, _save
from src.notifier import crear_notifier
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
    from src.journal import _load
    from src.config import BOT_ID
    from src.logger import logger
    
    try:
        all_trades = _load()
        current_trade = None
        
        # 1. Búsqueda explícita paso a paso
        for t in all_trades:
            if t.get('symbol') == symbol and t.get('status') == 'OPEN':
                current_trade = t
                break # Lo encontramos, cortamos la búsqueda
                
        # 2. Evaluación del trade
        if current_trade:
            # Vemos de quién es el trade (si no dice, asumimos que es de este bot)
            owner = current_trade.get('bot_id', BOT_ID)
            
            if owner == "MANUAL":
                # Es manual: El bot lo ignora silenciosamente para no ponerle SL/TP
                return 
            elif owner == BOT_ID:
                # Es nuestro: Lo rescatamos
                from src.exchange import verificar_y_rescatar_sl_tp
                verificar_y_rescatar_sl_tp(client, symbol, current_trade)
            else:
                # Es de otro bot (ej: de ETH si este es de BTC), no hacemos nada
                pass
                
        else:
            # Si llegó acá, es porque en el JSON no hay ningún trade OPEN para esta moneda
            logger.warning(f"[{symbol}] Hay posición en Binance pero no encontré el trade OPEN en el Journal.")
            
    except Exception as e:
        logger.error(f"Error en gestionar_resguardo_posicion para {symbol}: {e}")

def ejecutar_apertura_completa(client, symbol, signal, entry_price, sl_price, tp_price, qty, risk_pct, balance_at_open: float = 0.0):
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
        record_open(trade_id, symbol, signal, entry_price, sl_price, tp_price, qty, risk_pct, balance_at_open)
        crear_notifier().alert_trade_open(symbol, signal, entry_price, sl_price, tp_price, qty, risk_pct)
        
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

        # --- FUNCIÓN INTERNA PARA NO REPETIR LÓGICA DE PNL/FEES ---
        def calcular_pnl_y_fees_final(trade):
            try:
                entry_dt = datetime.fromisoformat(trade['entry_time'])
                entry_ts = int(entry_dt.timestamp() * 1000)
        
                # Traer historial desde la apertura
                historial = client.futures_account_trades(
                    symbol=symbol, 
                    startTime=entry_ts,
                    limit=100
                )
        
                if not historial:
                    logger.warning(f"[{symbol}] Sin historial de trades en Binance")
                    return

                # Verificar que realmente hubo un cierre (realizedPnl != 0)
                # Si no hay, la orden LIMIT no se llenó todavía — no es un cierre real
                hay_cierre = any(float(op.get('realizedPnl', 0)) != 0 for op in historial)
                if not hay_cierre:
                    logger.info(f"[{symbol}] Historial sin PnL realizado — orden aun no ejecutada. Ignorando cierre falso.")
                    return
        
                # Filtrar solo los trades de ESTA posición:
                # Los que tienen el mismo side que la apertura (entry)
                # y los que tienen side contrario (cierre)
                entry_side = "BUY" if trade['direction'] == "LONG" else "SELL"
                close_side = "SELL" if trade['direction'] == "LONG" else "BUY"
        
                pnl_acumulado = 0.0
                fees_acumulados = 0.0
                ultimo_precio = trade.get('entry_price', 0)
                qty_entrada = float(trade.get('quantity', 0))
        
                for op in historial:
                    realizado = float(op.get('realizedPnl', 0))
                    comm = float(op.get('commission', 0))
                    op_side = op.get('side', '')
                    op_qty = float(op.get('qty', 0))
            
                    # Siempre sumar fees (tanto apertura como cierre)
                    fees_acumulados += comm
            
                    # Solo sumar PnL de operaciones de cierre
                    if realizado != 0:
                        pnl_acumulado += realizado
                        ultimo_precio = float(op.get('price', 0))
        
                trade['pnl_bruto'] = round(pnl_acumulado, 4)
                trade['fees'] = round(fees_acumulados, 4)
                trade['pnl_usdt'] = round(pnl_acumulado - fees_acumulados, 4)
                trade['exit_price'] = ultimo_precio
        
                logger.info(
                    f"[{symbol}] PnL final: "
                    f"Bruto={trade['pnl_bruto']} "
                    f"Fees={trade['fees']} "
                    f"Neto={trade['pnl_usdt']} "
                    f"Exit={ultimo_precio}"
                )

                # ==========================================
                # INICIO NUEVO: ALERTA DE TELEGRAM AL CERRAR
                # ==========================================
                notifier = crear_notifier()
                
                pnl_neto = trade['pnl_usdt']
                if pnl_neto > 0:
                    resultado = "WIN"
                elif pnl_neto < 0:
                    resultado = "LOSS"
                else:
                    resultado = "BREAKEVEN"

                notifier.alert_trade_close(
                    symbol=symbol,
                    pnl=pnl_neto,
                    result=resultado,
                    qty=float(trade.get('quantity', 0)),
                    entry_price=float(trade.get('entry_price', 0)),
                    exit_price=ultimo_precio,
                    balance_at_open=float(trade.get('balance_at_open', 0.0))
                )
                # ==========================================
            except Exception as e:
                logger.error(f"Error calculando PnL/Fees: {e}")


        # ==========================================
        # CASO 1: SE CERRÓ (SL, TP o lo cerraste a mano)
        # ==========================================
        if not pos_real and open_in_journal:
            for t in open_in_journal:
                logger.info(f"[{symbol}] Detectado cierre externo.")
                t['status'] = 'CLOSED'
                t['close_time'] = ahora
                calcular_pnl_y_fees_final(t) # <--- LLAMADA A LA LÓGICA NUEVA
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
                logger.warning(f"[{symbol}] Cambio de dirección manual detectado.")
                t['status'] = 'CLOSED'
                t['close_time'] = ahora
                calcular_pnl_y_fees_final(t) # <--- TAMBIÉN CALCULAMOS ACÁ
                modified = True

        if modified:
            _save(all_trades)
            
    except Exception as e:
        logger.error(f"Error en sincronizador: {e}")