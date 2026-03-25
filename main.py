import time
import notifier
from src.config import SYMBOL, BOT_ID, BOT_NAME, TP_RR_RATIO, RISK_PER_TRADE, BAND_MULT
# Asegurate de tener TIMEFRAME o INTERVALO en tu config (ej: "1m")
from src.config import TIMEFRAME 
from src.logger import logger
from src.exchange import get_client, get_account_status, get_open_position, set_leverage
from src.strategy import obtener_señal_actual
from src.execution import ejecutar_apertura_completa, gestionar_resguardo_posicion, sincronizar_realidad_vs_journal
from src.risk import calculate_position_size, check_drawdown_alert, can_trade
from src.live_writer import exportar_dashboard, exportar_status
from src.notifier import crear_notifier
from src.journal import _load

# Importamos el nuevo stream de WebSockets
from src.websocket_stream import BinanceKlineStream

# Variables globales para el ciclo
cycle_count = 0
client = None

def inicializar():
    """Configuración única al arrancar el contenedor."""
    logger.info("="*50)
    logger.info(f"🚀 Iniciando Bot {BOT_ID} para {SYMBOL} con WebSockets...")
    logger.info("="*50)
    c = get_client()
    
    # Configuración de Exchange inicial
    set_leverage(c, SYMBOL)

    # Notificación de arranque
    balance_inicial = get_account_status(c)['wallet_balance']
    notifier = crear_notifier()
    notifier.alert_startup(SYMBOL, RISK_PER_TRADE, TP_RR_RATIO, BAND_MULT, balance_inicial)
    
    return c

def ejecutar_ciclo_ws(df_velas, buffer):
    """
    Este es el nuevo ciclo.
    El WebSocket dispara esta función EXACTAMENTE en el milisegundo que cierra la vela.
    """
    global cycle_count
    global client
    
    try:
        # Log informativo cada 60 ciclos (1 hora)
        if cycle_count % 60 == 0:
            logger.info("="*50)
            logger.info(f"  BOT {BOT_NAME}  R/R: {TP_RR_RATIO} Riesgo por trade: {RISK_PER_TRADE}%")
            logger.info(f"  Bot corriendo durante {cycle_count} minutos")
            logger.info("="*50)

        logger.info(f"--- Ciclo {cycle_count} para {SYMBOL} (Cierre detectado via WS) ---")
        
        # 1. Sincronización de Cuenta y Alertas de Riesgo
        account = get_account_status(client)
        check_drawdown_alert(account['wallet_balance'])
        
        # 2. AUDITORÍA: Sincronizar PnL real y detectar trades manuales
        sincronizar_realidad_vs_journal(client, SYMBOL)

        # 3. Gestión de Posición Abierta
        pos_abierta = get_open_position(client, SYMBOL)
        if pos_abierta:
            gestionar_resguardo_posicion(client, SYMBOL)
            
        # Revisamos si el cortacircuitos diario nos permite operar
        if not can_trade(_load()):
            logger.warning(f"[{SYMBOL}] Cortacircuitos diario activo. No se operará hoy.")
            return  

        # 4. Análisis de Estrategia
        # (Ver nota abajo sobre esta línea)
        signal, entry_price, std_dev = obtener_señal_actual(client)

        # 5. Actualización del Dashboard Live
        exportar_status(
            account['wallet_balance'], cycle_count, 
            account['unrealized_pnl'], account['margin_balance'], 
            account['available'], 1 if pos_abierta else 0
        )
        exportar_dashboard(client) # Asegurate de no pasar 'client' si tu función exportar_dashboard no lo recibe
        
        # 6. Lógica de Disparo
        if signal and not pos_abierta:
            dist_sl = std_dev * 1.5
            if signal == "LONG":
                sl_price = entry_price - dist_sl
                tp_price = entry_price + (dist_sl * TP_RR_RATIO)
            else:
                sl_price = entry_price + dist_sl
                tp_price = entry_price - (dist_sl * TP_RR_RATIO)
            
            qty = calculate_position_size(account['wallet_balance'], RISK_PER_TRADE, entry_price, sl_price)

            if qty > 0:
                ejecutar_apertura_completa(client, SYMBOL, signal, entry_price, sl_price, tp_price, qty, RISK_PER_TRADE)

        cycle_count += 1

    except Exception as e:
        logger.error(f"Error crítico en ciclo {SYMBOL}: {e}", exc_info=True)

def main():
    global client
    # 1. Inicializamos y guardamos el cliente
    client = inicializar()
    
    # 2. DESCARGA INICIAL (Esto es lo que falta)
    # Necesitamos traer historial por REST para que el buffer no empiece de cero
    from src.exchange import get_klines_rest # Asegurate que esta función exista en exchange.py
    logger.info("Descargando historial inicial para el buffer...")
    df_historico = get_klines_rest(client, SYMBOL, TIMEFRAME, limite=100)

    # 3. Inicializamos el Stream de Binance
    logger.info("Conectando al WebSocket de Binance...")
    stream = BinanceKlineStream(
        symbol=SYMBOL,
        interval=TIMEFRAME, # Importado de config (ej: "1m")
        on_candle_close=ejecutar_ciclo_ws, # El bot ahora es manejado por esta función
        testnet=True, 
        buffer_size=300
    )
    
    # 3. Arrancamos el stream en un hilo secundario
    stream.iniciar(df_historico)
    
    # 4. Loop infinito para mantener vivo el contenedor Docker
    try:
        while True:
            time.sleep(1) # Ya no dormimos 60s, dormimos 1s para no saturar el CPU mientras el WS trabaja de fondo
    except KeyboardInterrupt:
        logger.info("Apagando bot...")
        stream.detener()

if __name__ == "__main__":
    main()