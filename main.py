# main.py
# -*- coding: utf-8 -*-
import time
import threading
from src.config import SYMBOL, BOT_ID, BOT_NAME, TP_RR_RATIO, RISK_PER_TRADE, BAND_MULT
from src.config import TIMEFRAME, LEVERAGE
from src.logger import logger
from src.exchange import (
    get_client, get_account_status, get_open_position,
    set_leverage, cancel_all_open_orders, close_market_position
)
from src.strategy import actualizar_bandas, evaluar_precio_intra_vela, _cooldown_activo
from src.execution import ejecutar_apertura_completa, gestionar_resguardo_posicion, sincronizar_realidad_vs_journal
from src.risk import calculate_position_size, check_drawdown_alert, can_trade
from src.live_writer import exportar_dashboard, exportar_status
from src.notifier import crear_notifier
from src.journal import _load
from src.websocket_stream import BinanceKlineStream, MarkPriceStream
from src.news_filter import is_news_blocked, check_and_close_on_news

cycle_count = 0
client      = None

# ── Estado compartido entre threads ───────────────────────────────────────────
_bandas_actuales: dict | None = None
_bandas_lock      = threading.Lock()
_entrada_lock     = threading.Lock()   # lock atómico para evitar doble entrada
_entrada_en_curso = False              # flag protegido por _entrada_lock
_pos_abierta:     bool = False         # evita chequeo intravela

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


def _on_candle_close(df_velas, buffer):
    """
    Callback al cierre de cada vela (1m).
    Responsabilidades:
      1. Auditoría de cuenta y posición
      2. Actualizar bandas para el siguiente período intra-vela
      3. Dashboard
    La entrada ya NO se dispara aquí — lo hace _on_mark_price_tick().
    """
    global cycle_count, _bandas_actuales

    try:
        if cycle_count % 60 == 0:
            logger.info("="*50)
            logger.info(f"  BOT {BOT_NAME}  R/R: {TP_RR_RATIO} Riesgo por trade: {RISK_PER_TRADE}%")
            logger.info(f"  Bot corriendo durante {cycle_count} minutos")
            logger.info("="*50)

        logger.info(f"--- Ciclo {cycle_count} para {SYMBOL} (Cierre detectado via WS) ---")

        # 1. Sincronización de cuenta y alertas de riesgo
        account = get_account_status(client)
        check_drawdown_alert(account['wallet_balance'], cycle_count)

        # 2. Auditoría: sincronizar PnL real y detectar trades manuales
        sincronizar_realidad_vs_journal(client, SYMBOL)

        # 3. Gestión de posición abierta
        global _pos_abierta
        pos_abierta = get_open_position(client, SYMBOL)
        _pos_abierta = pos_abierta is not None
        if pos_abierta:
            gestionar_resguardo_posicion(client, SYMBOL)

        # 4. Actualizar bandas para el próximo período intra-vela
        nuevas_bandas = actualizar_bandas(df_velas)
        with _bandas_lock:
            _bandas_actuales = nuevas_bandas

        # Limpiar flag de entrada solo si no hay PENDING_FILL activo
        global _entrada_en_curso
        historial = _load()
        hay_pending = any(
            t.get('symbol') == SYMBOL and t.get('status') == 'PENDING_FILL'
            for t in historial
        )
        if not hay_pending:
            with _entrada_lock:
                _entrada_en_curso = False

        if nuevas_bandas:
            logger.info("Bandas | Upper: %.2f | Lower: %.2f | VWAP: %.2f",
                        nuevas_bandas["upper"], nuevas_bandas["lower"], nuevas_bandas["vwap"])
        else:
            logger.debug("Bandas no disponibles este ciclo (filtros activos).")

        # 5. Dashboard
        exportar_status(
            account['wallet_balance'], cycle_count,
            account['unrealized_pnl'], account['margin_balance'],
            account['available'], 1 if pos_abierta else 0
        )
        exportar_dashboard(client)

        # Heartbeat
        notifier = crear_notifier()
        notifier.heartbeat_si_corresponde(client, cycle_count)
        cycle_count += 1

    except Exception as e:
        logger.error(f"Error critico en ciclo {SYMBOL}: {e}", exc_info=True)


def _on_mark_price_tick(mark_price: float):
    """
    Callback en cada tick de mark price (~1s).
    Evalúa si el precio toca una banda y dispara entrada inmediata.
    Usa lock atómico para evitar race condition con múltiples ticks simultáneos.
    La ejecución de la orden se hace en thread separado para no bloquear el stream.
    """
    global _entrada_en_curso

    # Check atómico — si ya hay entrada en curso, salir inmediatamente
    with _entrada_lock:
        if _entrada_en_curso:
            return

    with _bandas_lock:
        bandas = _bandas_actuales

    if not bandas:
        return

    try:
        # ── Filtro de noticias ────────────────────────────────────────────
        news_blocked, _ = is_news_blocked(SYMBOL)
        if news_blocked:
            return

        # ── Verificar si hay posición abierta ─────────────────────────────
        if _pos_abierta:
            return

        # ── Cortacircuitos diario ─────────────────────────────────────────
        historial = _load()
        if not can_trade(historial):
            return

        # ── Cooldown post-trade (incluye CANCELLED) ───────────────────────
        if _cooldown_activo():
            return

        # ── Evaluar toque de banda ────────────────────────────────────────
        signal, entry_price, tp_vwap = evaluar_precio_intra_vela(mark_price, bandas)
        if not signal:
            return

        # ── Marcar entrada en curso ATÓMICAMENTE ─────────────────────────
        with _entrada_lock:
            if _entrada_en_curso:
                return  # otro tick llegó antes
            _entrada_en_curso = True

        logger.info("[%s] 🎯 Toque de banda | Mark: %.2f | Signal: %s | Entry: %.2f | TP: %.2f",
                    SYMBOL, mark_price, signal, entry_price, tp_vwap)

        # ── Calcular SL y tamaño ──────────────────────────────────────────
        account  = get_account_status(client)
        reward   = abs(tp_vwap - entry_price)
        dist_sl  = reward / TP_RR_RATIO
        sl_price = entry_price - dist_sl if signal == "LONG" else entry_price + dist_sl
        qty      = calculate_position_size(account['wallet_balance'], RISK_PER_TRADE, entry_price, sl_price)

        # Cap por margen disponible
        notional     = qty * entry_price
        max_notional = account['available'] * LEVERAGE * 0.8
        if notional > max_notional:
            qty_capped = round(max_notional / entry_price, 3)
            logger.warning("[%s] Qty capado: %.4f -> %.4f", SYMBOL, qty, qty_capped)
            qty = qty_capped

        if qty <= 0:
            logger.warning("[%s] Qty inválido, abortando.", SYMBOL)
            with _entrada_lock:
                _entrada_en_curso = False
            return

        # ── Ejecutar en thread separado para no bloquear el stream ────────
        def _ejecutar():
            global _entrada_en_curso
            try:
                ejecutar_apertura_completa(
                    client, SYMBOL, signal, entry_price, sl_price, tp_vwap,
                    qty, RISK_PER_TRADE, balance_at_open=account['wallet_balance']
                )
            except Exception as e:
                logger.error(f"Error en ejecución de apertura: {e}", exc_info=True)
            finally:
                # Solo limpiar si no quedó PENDING_FILL
                hist = _load()
                hay_pending = any(
                    t.get('symbol') == SYMBOL and t.get('status') == 'PENDING_FILL'
                    for t in hist
                )
                if not hay_pending:
                    with _entrada_lock:
                        _entrada_en_curso = False

        threading.Thread(target=_ejecutar, daemon=True, name="ejecutar-apertura").start()

    except Exception as e:
        logger.error(f"Error en _on_mark_price_tick: {e}", exc_info=True)
        with _entrada_lock:
            _entrada_en_curso = False


def main():
    global client
    client = inicializar()

    from src.exchange import get_klines_rest
    logger.info("Descargando historial inicial para el buffer...")
    df_historico = get_klines_rest(client, SYMBOL, TIMEFRAME, limite=1500)

    # ── Stream de velas (cierre de vela → actualizar bandas y auditoría) ──
    logger.info("Conectando al WebSocket de velas...")
    stream_kline = BinanceKlineStream(
        symbol          = SYMBOL,
        interval        = TIMEFRAME,
        on_candle_close = _on_candle_close,
        testnet         = True,
        buffer_size     = 1500,
        candles_minimos = 130,   # necesitamos 120 de bar_num + margen
    )
    stream_kline.iniciar(df_historico)

    # ── Stream de mark price (intra-vela → disparar entradas) ─────────────
    logger.info("Conectando al WebSocket de mark price...")
    stream_mark = MarkPriceStream(
        symbol  = SYMBOL,
        on_tick = _on_mark_price_tick,
        testnet = True,
    )
    stream_mark.iniciar()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Apagando bot...")
        stream_kline.detener()
        stream_mark.detener()


if __name__ == "__main__":
    main()
