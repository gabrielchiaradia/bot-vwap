# -*- coding: utf-8 -*-
import time
from src.config import SYMBOL, BOT_ID, BOT_NAME, TP_RR_RATIO, RISK_PER_TRADE, BAND_MULT
from src.config import TIMEFRAME, LEVERAGE
from src.logger import logger
from src.exchange import get_client, get_account_status, get_open_position, set_leverage
from src.strategy import obtener_señal_actual
from src.execution import ejecutar_apertura_completa, gestionar_resguardo_posicion, sincronizar_realidad_vs_journal
from src.risk import calculate_position_size, check_drawdown_alert, can_trade
from src.live_writer import exportar_dashboard, exportar_status
from src.notifier import crear_notifier
from src.journal import _load
from src.websocket_stream import BinanceKlineStream

cycle_count = 0
client = None

def inicializar():
    """Configuracion unica al arrancar el contenedor."""
    logger.info("="*50)
    logger.info(f"Iniciando Bot {BOT_ID} para {SYMBOL} con WebSockets...")
    logger.info("="*50)
    c = get_client()
    set_leverage(c, SYMBOL)
    logger.info("Limpiando órdenes huérfanas previas...")
    cancel_all_open_orders(c, SYMBOL)    
    balance_inicial = get_account_status(c)['wallet_balance']
    notifier = crear_notifier()
    notifier.alert_startup(SYMBOL, RISK_PER_TRADE, TP_RR_RATIO, BAND_MULT, balance_inicial)
    return c

def ejecutar_ciclo_ws(df_velas, buffer):
    """
    Ciclo principal. El WebSocket dispara esta funcion al cierre de cada vela.
    """
    global cycle_count
    global client

    try:
        if cycle_count % 60 == 0:
            logger.info("="*50)
            logger.info(f"  BOT {BOT_NAME}  R/R: {TP_RR_RATIO} Riesgo por trade: {RISK_PER_TRADE}%")
            logger.info(f"  Bot corriendo durante {cycle_count} minutos")
            logger.info("="*50)

        logger.info(f"--- Ciclo {cycle_count} para {SYMBOL} (Cierre detectado via WS) ---")

        # 1. Sincronizacion de cuenta y alertas de riesgo
        account = get_account_status(client)
        check_drawdown_alert(account['wallet_balance'], cycle_count)

        # 2. Auditoria: sincronizar PnL real y detectar trades manuales
        sincronizar_realidad_vs_journal(client, SYMBOL)

        # 3. Gestion de posicion abierta
        pos_abierta = get_open_position(client, SYMBOL)
        if pos_abierta:
            gestionar_resguardo_posicion(client, SYMBOL)

        # Cortacircuitos diario
        historial = _load()
        if not can_trade(historial):
            logger.warning(f"[{SYMBOL}] Cortacircuitos diario activo. No se operara hoy.")
            return

        # 4. Analisis de estrategia
        signal, entry_price, std_dev = obtener_señal_actual(client)

        # 5. Actualizacion del dashboard
        exportar_status(
            account['wallet_balance'], cycle_count,
            account['unrealized_pnl'], account['margin_balance'],
            account['available'], 1 if pos_abierta else 0
        )
        exportar_dashboard(client)

        # 6. Logica de disparo
        if signal and not pos_abierta:
            dist_sl = std_dev * 1.5
            if signal == "LONG":
                sl_price = entry_price - dist_sl
                tp_price = entry_price + (dist_sl * TP_RR_RATIO)
            else:
                sl_price = entry_price + dist_sl
                tp_price = entry_price - (dist_sl * TP_RR_RATIO)

            qty = calculate_position_size(account['wallet_balance'], RISK_PER_TRADE, entry_price, sl_price)

            # Cap: notional maximo = 80% del margen disponible * leverage
            notional = qty * entry_price
            max_notional = account['available'] * LEVERAGE * 0.8
            if notional > max_notional:
                qty_capped = round(max_notional / entry_price, 3)
                logger.warning(f"[{SYMBOL}] Qty capado por margen: {qty:.4f} -> {qty_capped:.4f} (notional {notional:.0f} -> {max_notional:.0f})")
                qty = qty_capped

            if qty > 0:
                ejecutar_apertura_completa(
                    client, SYMBOL, signal, entry_price, sl_price, tp_price,
                    qty, RISK_PER_TRADE, balance_at_open=account['wallet_balance']
                )

        # Heartbeat
        notifier = crear_notifier()
        notifier.heartbeat_si_corresponde(client, cycle_count)

        cycle_count += 1

    except Exception as e:
        logger.error(f"Error critico en ciclo {SYMBOL}: {e}", exc_info=True)

def main():
    global client
    client = inicializar()

    from src.exchange import get_klines_rest
    logger.info("Descargando historial inicial para el buffer...")
    df_historico = get_klines_rest(client, SYMBOL, TIMEFRAME, limite=100)

    logger.info("Conectando al WebSocket de Binance...")
    stream = BinanceKlineStream(
        symbol=SYMBOL,
        interval=TIMEFRAME,
        on_candle_close=ejecutar_ciclo_ws,
        testnet=True,
        buffer_size=300
    )

    stream.iniciar(df_historico)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Apagando bot...")
        stream.detener()

if __name__ == "__main__":
    main()
