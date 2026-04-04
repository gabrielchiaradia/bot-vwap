# main.py
# -*- coding: utf-8 -*-
import time
import threading
from src.config import SYMBOL, BOT_ID, BOT_NAME, TP_RR_RATIO, RISK_PER_TRADE, BAND_MULT
from src.config import TIMEFRAME, LEVERAGE, STRATEGY
from src.logger import logger
from src.exchange import (
    get_client, get_account_status, get_open_position,
    set_leverage, cancel_all_open_orders, close_market_position
)
from src.strategy import (
    actualizar_bandas, evaluar_precio_intra_vela,
    actualizar_bandas_cross, evaluar_cruce_vwap,
    _cooldown_activo
)
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
# Las bandas se actualizan al cierre de cada vela (thread WS kline).
# El mark price las lee para evaluar entradas (thread WS markprice).
_bandas_actuales: dict | None = None
_bandas_lock      = threading.Lock()
_entrada_en_curso = threading.Event()   # evita doble entrada intra-vela
_pos_abierta:     bool = False          # evita chequeo intravela
_precio_anterior: float = 0.0          # para detectar cruce del VWAP (cross strategy)

def inicializar():
    """Configuracion unica al arrancar el contenedor."""
    logger.info("="*50)
    logger.info(f"Iniciando Bot {BOT_ID} para {SYMBOL} con WebSockets...")
    logger.info(f"Estrategia: {STRATEGY.upper()} | TF: {TIMEFRAME}")
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
        if STRATEGY == "cross":
            nuevas_bandas = actualizar_bandas_cross(df_velas)
        else:
            nuevas_bandas = actualizar_bandas(df_velas)
        with _bandas_lock:
            _bandas_actuales = nuevas_bandas
            # Solo limpiar si no hay PENDING_FILL activo
            historial = _load()
            hay_pending = any(
                t.get('symbol') == SYMBOL and t.get('status') == 'PENDING_FILL'
                for t in historial
            )
            if not hay_pending:
                _entrada_en_curso.clear()

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
    Estrategia reversion: evalúa toque de banda.
    Estrategia cross: evalúa cruce del VWAP.
    """
    global _bandas_actuales, _precio_anterior

    if _entrada_en_curso.is_set():
        return

    with _bandas_lock:
        bandas = _bandas_actuales

    if not bandas:
        _precio_anterior = mark_price
        return

    try:
        # ── Filtro de noticias ────────────────────────────────────────────
        news_blocked, _ = is_news_blocked(SYMBOL)
        if news_blocked:
            _precio_anterior = mark_price
            return

        # ── Verificar si hay posición abierta ─────────────────────────────
        if _pos_abierta:
            _precio_anterior = mark_price
            return

        # ── Cortacircuitos diario ─────────────────────────────────────────
        historial = _load()
        if not can_trade(historial):
            _precio_anterior = mark_price
            return

        # ── Cooldown post-trade ───────────────────────────────────────────
        if _cooldown_activo():
            _precio_anterior = mark_price
            return

        # ── Evaluar señal según estrategia ────────────────────────────────
        if STRATEGY == "cross":
            if _precio_anterior <= 0:
                _precio_anterior = mark_price
                return
            signal, entry_price, tp_price, sl_price = evaluar_cruce_vwap(
                mark_price, bandas, _precio_anterior
            )
            if signal:
                logger.info(f"[{SYMBOL}] 🎯 Cruce detectado: {signal} | prev={_precio_anterior:.2f} | now={mark_price:.2f} | vwap={bandas['vwap']:.2f}")
            else:
                logger.debug(f"[{SYMBOL}] tick cross | prev={_precio_anterior:.2f} | now={mark_price:.2f} | vwap={bandas['vwap']:.2f}")
        else:
            signal, entry_price, tp_vwap = evaluar_precio_intra_vela(mark_price, bandas)
            if signal:
                reward   = abs(tp_vwap - entry_price)
                dist_sl  = reward / TP_RR_RATIO
                sl_price = entry_price - dist_sl if signal == "LONG" else entry_price + dist_sl
                tp_price = tp_vwap
            else:
                sl_price = tp_price = None

        # Actualizar precio anterior para el próximo tick
        _precio_anterior = mark_price

        if not signal:
            return

        # Marcar entrada en curso (atómico)
        if not _entrada_en_curso.is_set():
            _entrada_en_curso.set()
        else:
            return

        logger.info("[%s] 🎯 Señal %s | Mark: %.2f | Signal: %s | Entry: %.2f | TP: %.2f | SL: %.2f",
                    SYMBOL, STRATEGY.upper(), mark_price, signal, entry_price, tp_price, sl_price)

        # ── Calcular tamaño ───────────────────────────────────────────────
        account = get_account_status(client)
        qty = calculate_position_size(account['wallet_balance'], RISK_PER_TRADE, entry_price, sl_price)

        # Cap por margen disponible
        notional     = qty * entry_price
        max_notional = account['available'] * LEVERAGE * 0.8
        if notional > max_notional:
            qty_capped = round(max_notional / entry_price, 3)
            logger.warning("[%s] Qty capado por margen: %.4f -> %.4f", SYMBOL, qty, qty_capped)
            qty = qty_capped

        if qty <= 0:
            logger.warning("[%s] Qty inválido, abortando entrada.", SYMBOL)
            _entrada_en_curso.clear()
            return

        ejecutar_apertura_completa(
            client, SYMBOL, signal, entry_price, sl_price, tp_price,
            qty, RISK_PER_TRADE, balance_at_open=account['wallet_balance']
        )
        # Liberar solo si no quedó PENDING_FILL — si quedó, bloqueamos
        # hasta que el sincronizador lo resuelva en el próximo ciclo
        historial = _load()
        hay_pending = any(
            t.get('symbol') == SYMBOL and t.get('status') == 'PENDING_FILL'
            for t in historial
        )
        if not hay_pending:
            _entrada_en_curso.clear()
    except Exception as e:
        logger.error(f"Error en _on_mark_price_tick: {e}", exc_info=True)
        _entrada_en_curso.clear()


def main():
    global client
    client = inicializar()

    from src.exchange import get_klines_rest
    logger.info("Descargando historial inicial para el buffer...")
    df_historico = get_klines_rest(client, SYMBOL, TIMEFRAME, limite=1500)

    # ── Stream de velas (cierre de vela → actualizar bandas y auditoría) ──
    logger.info("Conectando al WebSocket de velas...")
    # candles_minimos: en 1m necesitamos 120 de bar_num + margen
    # en 5m son 5 velas de inicio de sesión + historial del día
    tf_min_map   = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
    tf_min_val   = tf_min_map.get(TIMEFRAME, 1)
    candles_min  = max(50, int(1440 / tf_min_val) + 10)  # velas de 1 día + margen

    stream_kline = BinanceKlineStream(
        symbol          = SYMBOL,
        interval        = TIMEFRAME,
        on_candle_close = _on_candle_close,
        testnet         = True,
        buffer_size     = max(1500, candles_min + 100),
        candles_minimos = candles_min,
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
