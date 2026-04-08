# src/execution.py

import time
import uuid
from datetime import datetime, timezone
from src.logger import logger
from src.config import BOT_ID, TIMEOUT_MINUTES_CROSS
from src.journal import record_open, _load, _save
from src.notifier import crear_notifier
from src.exchange import (
    cancel_all_open_orders,
    place_limit_order,
    place_market_order,
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
                break  # Lo encontramos, cortamos la búsqueda

        # 2. Evaluación del trade
        if current_trade:
            owner = current_trade.get('bot_id', BOT_ID)

            if owner == "MANUAL":
                return
            elif owner == BOT_ID:
                from src.exchange import verificar_y_rescatar_sl_tp
                verificar_y_rescatar_sl_tp(client, symbol, current_trade)
            else:
                pass

        else:
            logger.warning(f"[{symbol}] Hay posición en Binance pero no encontré el trade OPEN en el Journal.")

    except Exception as e:
        logger.error(f"Error en gestionar_resguardo_posicion para {symbol}: {e}")


def ejecutar_apertura_completa(client, symbol, signal, entry_price, sl_price, tp_price, qty, risk_pct, balance_at_open: float = 0.0, bias: str = "MEAN_REV"):
    """
    Orquesta la apertura: Cancela previas, pone LIMIT, espera FILL y clava SL/TP.
    bias: "MEAN_REV" o "CROSS" — se guarda en el journal para gestión posterior.
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

        # 3. Bucle de espera (Wait for Fill)
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
            record_open(trade_id, symbol, signal, entry_price, sl_price, tp_price, qty, risk_pct, balance_at_open, bias=bias)
            crear_notifier().alert_trade_open(symbol, signal, entry_price, sl_price, tp_price, qty, risk_pct, strategy=bias)
        else:
            logger.warning(f"[{symbol}] ⚠️ LIMIT no se llenó en 10s. Orden activa en Binance, monitoreando...")
            record_open(trade_id, symbol, signal, entry_price, sl_price, tp_price, qty, risk_pct, balance_at_open, status="PENDING_FILL", bias=bias)

        return True

    except Exception as e:
        logger.error(f"Error crítico en ejecutar_apertura_completa: {e}")
        return False


def cerrar_posicion_por_timeout(client, symbol, trade):
    """
    Cierra una posición CROSS a mercado cuando supera TIMEOUT_MINUTES_CROSS.
    Cancela SL/TP antes de cerrar para evitar órdenes huérfanas.
    """
    try:
        direction = trade['direction']
        qty       = float(trade['quantity'])
        close_side = "SELL" if direction == "LONG" else "BUY"

        cancel_all_open_orders(client, symbol)
        order = place_market_order(client, symbol, close_side, qty)

        if order:
            logger.info(f"[{symbol}] ⏱️ Posición CROSS cerrada por timeout (dir={direction} qty={qty})")
            return True
        else:
            logger.error(f"[{symbol}] ❌ Fallo al cerrar por timeout")
            return False

    except Exception as e:
        logger.error(f"[{symbol}] Error en cerrar_posicion_por_timeout: {e}")
        return False


def sincronizar_realidad_vs_journal(client, symbol):
    """
    Audita Binance vs Journal para:
    1. Registrar trades abiertos a mano.
    2. Cerrar trades en el journal con el PnL REAL si se cerraron (SL/TP o a mano).
    3. Promover PENDING_FILL a OPEN si la posición ya existe en Binance.
    4. Cancelar PENDING_FILL silenciosamente si la orden nunca se ejecutó.
    5. Cerrar trades CROSS que superaron TIMEOUT_MINUTES_CROSS.
    """
    try:
        all_trades = _load()
        pos_real = get_open_position(client, symbol)

        open_in_journal = [
            t for t in all_trades
            if t.get('symbol') == symbol and t.get('status') in ('OPEN', 'PENDING_FILL')
        ]

        modified = False
        ahora = datetime.now(timezone.utc).isoformat()

        # --- FUNCIÓN INTERNA PARA NO REPETIR LÓGICA DE PNL/FEES ---
        def calcular_pnl_y_fees_final(trade):
            try:
                entry_dt = datetime.fromisoformat(trade['entry_time'])
                entry_ts = int(entry_dt.timestamp() * 1000)

                historial = client.futures_account_trades(
                    symbol=symbol,
                    startTime=entry_ts,
                    limit=100
                )

                if not historial:
                    logger.warning(f"[{symbol}] Sin historial de trades en Binance")
                    return

                hay_cierre = any(float(op.get('realizedPnl', 0)) != 0 for op in historial)
                if not hay_cierre:
                    logger.info(f"[{symbol}] Historial sin PnL realizado — orden aun no ejecutada. Ignorando cierre falso.")
                    return

                entry_side = "BUY" if trade['direction'] == "LONG" else "SELL"
                close_side = "SELL" if trade['direction'] == "LONG" else "BUY"

                pnl_acumulado    = 0.0
                fees_acumulados  = 0.0
                ultimo_precio    = trade.get('entry_price', 0)
                qty_entrada      = float(trade.get('quantity', 0))

                for op in historial:
                    realizado = float(op.get('realizedPnl', 0))
                    comm      = float(op.get('commission', 0))
                    op_side   = op.get('side', '')
                    op_qty    = float(op.get('qty', 0))

                    fees_acumulados += comm

                    if realizado != 0:
                        pnl_acumulado += realizado
                        ultimo_precio  = float(op.get('price', 0))

                trade['pnl_bruto']  = round(pnl_acumulado, 4)
                trade['fees']       = round(fees_acumulados, 4)
                trade['pnl_usdt']   = round(pnl_acumulado - fees_acumulados, 4)
                trade['exit_price'] = ultimo_precio

                logger.info(
                    f"[{symbol}] PnL final: "
                    f"Bruto={trade['pnl_bruto']} "
                    f"Fees={trade['fees']} "
                    f"Neto={trade['pnl_usdt']} "
                    f"Exit={ultimo_precio}"
                )

                notifier = crear_notifier()
                pnl_neto  = trade['pnl_usdt']
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
            except Exception as e:
                logger.error(f"Error calculando PnL/Fees: {e}")

        # ==========================================
        # CASO 0: PENDING_FILL — la orden todavía no se ejecutó
        # ==========================================
        pending_in_journal  = [t for t in open_in_journal if t.get('status') == 'PENDING_FILL']
        only_open_in_journal = [t for t in open_in_journal if t.get('status') == 'OPEN']

        if pending_in_journal:
            promovido = False
            for t in pending_in_journal:
                if pos_real and not promovido:
                    logger.info(f"[{symbol}] ✅ Orden PENDING_FILL ahora ejecutada. Promoviendo a OPEN.")
                    t['status'] = 'OPEN'
                    promovido = True
                    try:
                        side = "BUY" if t['direction'] == "LONG" else "SELL"
                        place_sl_tp(client, symbol, side, float(t['quantity']), float(t['sl_price']), float(t['tp_price']))
                    except Exception as e:
                        logger.error(f"[{symbol}] Error colocando SL/TP en promoción: {e}")
                    crear_notifier().alert_trade_open(
                        symbol, t['direction'], float(t['entry_price']),
                        float(t['sl_price']), float(t['tp_price']),
                        float(t['quantity']), float(t['risk_pct']),
                        strategy=t.get('bias', '')
                    )
                    modified = True
                else:
                    try:
                        ordenes_abiertas = client.futures_get_open_orders(symbol=symbol)
                        sigue_activa = any(
                            abs(float(o.get('price', 0)) - float(t.get('entry_price', 0))) < 0.02
                            for o in ordenes_abiertas
                            if o.get('side') == ('BUY' if t['direction'] == 'LONG' else 'SELL')
                        )
                    except Exception:
                        sigue_activa = True

                    if sigue_activa:
                        logger.info(f"[{symbol}] PENDING_FILL sigue activo en Binance — esperando fill.")
                    else:
                        pos_recheck = get_open_position(client, symbol)
                        if pos_recheck:
                            logger.info(f"[{symbol}] ✅ PENDING_FILL ejecutado en recheck. Promoviendo a OPEN.")
                            t['status'] = 'OPEN'
                            try:
                                side = "BUY" if t['direction'] == "LONG" else "SELL"
                                place_sl_tp(client, symbol, side, float(t['quantity']), float(t['sl_price']), float(t['tp_price']))
                            except Exception as e:
                                logger.error(f"[{symbol}] Error colocando SL/TP en recheck: {e}")
                            crear_notifier().alert_trade_open(
                                symbol, t['direction'], float(t['entry_price']),
                                float(t['sl_price']), float(t['tp_price']),
                                float(t['quantity']), float(t['risk_pct']),
                                strategy=t.get('bias', '')
                            )
                        else:
                            logger.warning(f"[{symbol}] PENDING_FILL cancelado/expirado en Binance: {t['trade_id']}")
                            t['status']     = 'CANCELLED'
                            t['close_time'] = ahora
                            t['result']     = 'CANCELLED'
                        modified = True

            if modified:
                _save(all_trades)
            return

        # ==========================================
        # CASO TIMEOUT: CROSS abierto demasiado tiempo
        # ==========================================
        if pos_real and only_open_in_journal:
            t = only_open_in_journal[0]
            if t.get('bias') == 'CROSS' and t.get('entry_time'):
                try:
                    entry_dt       = datetime.fromisoformat(t['entry_time'])
                    minutos_abierto = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60
                    if minutos_abierto >= TIMEOUT_MINUTES_CROSS:
                        logger.warning(
                            f"[{symbol}] ⏱️ CROSS timeout ({minutos_abierto:.0f}min >= {TIMEOUT_MINUTES_CROSS}min). Cerrando a mercado."
                        )
                        cerrado = cerrar_posicion_por_timeout(client, symbol, t)
                        if cerrado:
                            t['status']     = 'CLOSED'
                            t['close_time'] = ahora
                            t['result']     = 'TIMEOUT'
                            calcular_pnl_y_fees_final(t)
                            modified = True
                            _save(all_trades)
                            return
                except Exception as e:
                    logger.error(f"[{symbol}] Error evaluando timeout CROSS: {e}")

        # ==========================================
        # CASO 1: SE CERRÓ (SL, TP o lo cerraste a mano)
        # ==========================================
        if not pos_real and only_open_in_journal:
            for t in only_open_in_journal:
                logger.info(f"[{symbol}] Detectado cierre externo.")
                t['status']     = 'CLOSED'
                t['close_time'] = ahora
                calcular_pnl_y_fees_final(t)
                logger.info("Limpiando órdenes huérfanas previas...")
                cancel_all_open_orders(client, symbol)
                modified = True

        # ==========================================
        # CASO 2: SE ABRIÓ A MANO DESDE EL CELULAR/PC
        # ==========================================
        elif pos_real and not only_open_in_journal:
            try:
                import json, os
                otros_journals = [
                    f for f in os.listdir('logs')
                    if f.startswith('journal_') and f != f'journal_{BOT_ID}.json'
                ]
                posicion_de_otro_bot = False
                for jfile in otros_journals:
                    try:
                        with open(f'logs/{jfile}', 'r') as f:
                            otros_trades = json.load(f)
                        if any(
                            t.get('symbol') == symbol and
                            t.get('status') in ('OPEN', 'PENDING_FILL')
                            for t in otros_trades
                        ):
                            posicion_de_otro_bot = True
                            logger.info(f"[{symbol}] Posición abierta pertenece a otro bot ({jfile}). Ignorando.")
                            break
                    except Exception:
                        pass
            except Exception:
                posicion_de_otro_bot = False

            if not posicion_de_otro_bot:
                logger.warning(f"[{symbol}] ⚠️ Detectada posición abierta a mano. Registrando en Journal...")
                nuevo_trade = {
                    "trade_id":      f"MANUAL-{str(uuid.uuid4())[:4]}",
                    "bot_id":        "MANUAL",
                    "symbol":        symbol,
                    "direction":     pos_real['side'],
                    "entry_price":   pos_real['entry'],
                    "sl_price":      0.0,
                    "tp_price":      0.0,
                    "quantity":      pos_real['size'],
                    "risk_pct":      0.0,
                    "status":        "OPEN",
                    "entry_time":    ahora,
                    "close_time":    None,
                    "pnl_usdt":      0.0
                }
                all_trades.append(nuevo_trade)
                modified = True

        # ==========================================
        # CASO 3: FLIP A MANO
        # ==========================================
        elif pos_real and only_open_in_journal:
            t = only_open_in_journal[0]
            if t['direction'] != pos_real['side']:
                logger.warning(f"[{symbol}] Cambio de dirección manual detectado.")
                t['status']     = 'CLOSED'
                t['close_time'] = ahora
                calcular_pnl_y_fees_final(t)
                cancel_all_open_orders(client, symbol)
                logger.info(f"[{symbol}] 🧹 Limpieza de órdenes por cambio de dirección manual.")
                modified = True

        if modified:
            _save(all_trades)

    except Exception as e:
        logger.error(f"Error en sincronizador: {e}")
