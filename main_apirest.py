import time
from src.config import SYMBOL, BOT_ID, BOT_NAME, TP_RR_RATIO, RISK_PER_TRADE, BAND_MULT
from src.logger import logger
from src.exchange import get_client, get_account_status, get_open_position, set_leverage
from src.strategy import obtener_señal_actual
from src.execution import ejecutar_apertura_completa, gestionar_resguardo_posicion
from src.risk import calculate_position_size, check_drawdown_alert,can_trade
from src.live_writer import exportar_dashboard, exportar_status
from src.notifier import alert_startup
from src.journal import _load
from src.execution import ejecutar_apertura_completa, gestionar_resguardo_posicion, sincronizar_realidad_vs_journal


def inicializar():
    """Configuración única al arrancar el contenedor."""
    logger.info("="*50)
    logger.info(f"🚀 Iniciando Bot {BOT_ID} para {SYMBOL}...")
    logger.info("="*50)
    client = get_client()
    
    # Configuración de Exchange inicial
    set_leverage(client, SYMBOL)

    # Notificación de arranque
    balance_inicial = get_account_status(client)['wallet_balance']
    alert_startup(SYMBOL, RISK_PER_TRADE, TP_RR_RATIO, BAND_MULT, balance_inicial)
    
    return client

def ejecutar_ciclo(client, cycle_count): # Agregamos el cycle_count como parámetro
    """Ciclo que se repite cada 1 minuto."""
    try:
        logger.info(f"--- Ciclo {cycle_count} para {SYMBOL} ---")
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
            return  # Corta el ciclo acá y no analiza señales hasta mañana

        # 4. Análisis de Estrategia
        signal, entry_price, std_dev = obtener_señal_actual(client)

        # 5. Actualización del Dashboard Live
        exportar_status(
            account['wallet_balance'], cycle_count, 
            account['unrealized_pnl'], account['margin_balance'], 
            account['available'], 1 if pos_abierta else 0
        )
        exportar_dashboard(client)
        
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

    except Exception as e:
        logger.error(f"Error crítico en ciclo {SYMBOL}: {e}", exc_info=True)

def main():
    # 1. Inicializamos y guardamos el cliente
    client = inicializar()
    
    # 2. Arrancamos el loop infinito
    cycle_count = 0
    while True:
        # Log informativo cada 60 ciclos (1 hora)
        if cycle_count % 60 == 0:
            logger.info("="*50)
            logger.info(f"  BOT {BOT_NAME}  R/R: {TP_RR_RATIO} Riesgo por trade: {RISK_PER_TRADE}%")
            logger.info(F"  Bot corriendo daunte {cycle_count} minutos")
            logger.info("="*50)
        ejecutar_ciclo(client, cycle_count)
        cycle_count += 1      
        time.sleep(60)  # Esperamos al cierre del minuto para recalcular bandas

if __name__ == "__main__":
    main()

   

        