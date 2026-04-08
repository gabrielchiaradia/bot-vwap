# main.py
# -*- coding: utf-8 -*-
import json
import os
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from src.config import SYMBOL, BOT_ID, BOT_NAME, TP_RR_RATIO, RISK_PER_TRADE, BAND_MULT
from src.config import TIMEFRAME, LEVERAGE, STRATEGY, TRADING_WINDOW
from src.config import TIMEOUT_MINUTES_REVERSION
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
_bandas_actuales: dict | None = None
_bandas_lock      = threading.Lock()
_entrada_lock     = threading.Lock()   # lock atómico para evitar doble entrada
_entrada_en_curso = False              # flag protegido por _entrada_lock
_pos_abierta:     bool = False         # evita chequeo intravela
_precio_anterior: float = 0.0         # para detectar cruce del VWAP (cross strategy)

# ── Régimen actual leído del clasificador externo ─────────────────────────────
# 0 = Lateral (reversion), 1 = Tendencia (cross), 2 = Alta Volatilidad (stop)
_regime_actual: int = -1   # -1 = no leído aún, usa STRATEGY del .env como default
# Régimen que estaba activo cuando se abrió el trade actual (para detectar cambio)
_regime_al_abrir: int = -1

REGIME_STATE_PATH = Path(os.getenv("REGIME_STATE_PATH", "/shared/regime_state.json"))


def leer_regime() -> int:
    """
    Lee el régimen actual del JSON escrito por el regime-bot.
    Retorna:
        0 = Lateral    → usar reversion
        1 = Tendencia  → usar cross
        2 = Alta Vol   → no operar
       -1 = archivo no encontrado → usar STRATEGY del .env como default
    """
    try:
        if not REGIME_STATE_PATH.exists():
            return -1
        state = json.loads(REGIME_STATE_PATH.read_text())
        regime = state.get(SYMBOL, -1)
        # Verificar que el archivo no sea muy viejo (> 30 minutos = problema)
        updated_at = state.get("updated_at", "")
        if updated_at:
            last_update = datetime.fromisoformat(updated_at)
            age_minutes = (datetime.now(timezone.utc) - last_update).total_seconds() / 60
            if age_minutes > 30:
                logger.warning("regime_state.json tiene %.0f min de antigüedad — usando default", age_minutes)
                return -1
        return int(regime)
    except Exception as e:
        logger.warning("No se pudo leer regime_state.json: %s — usando default", e)
        return -1


def _get_strategy_from_regime(regime: int) -> str:
    """
    Mapea el régimen al nombre de estrategia.
    Si regime == -1 (no hay archivo), usa STRATEGY del .env.
    """
    if regime == -1:
        return STRATEGY  # fallback al .env
    if regime == 0:
        return "stop"
    if regime == 1:
        return "cross"
    return "stop"  # regime == 2


def _dentro_de_ventana_horaria() -> bool:
    """
    Verifica si la hora UTC actual está dentro de TRADING_WINDOW.
    '0-24' = siempre activo.
    """
    if not TRADING_WINDOW or TRADING_WINDOW == "0-24":
        return True
    try:
        partes = TRADING_WINDOW.split("-")
        h_ini, h_fin = int(partes[0]), int(partes[1])
        hora_utc = datetime.now(timezone.utc).hour
        return h_ini <= hora_utc < h_fin
    except Exception:
        return True


def _gestionar_posicion_abierta(pos_abierta, estrategia_activa: str) -> bool:
    """
    Gestión activa de posición abierta. Chequea:
    1. Timeout de mean-reversion (TIMEOUT_MINUTES_REVERSION min)
    2. Cambio de régimen (la tesis del trade murió)
    3. Resguardo de SL/TP

    Retorna True si cerró la posición, False si sigue abierta.
    """
    global _regime_al_abrir

    if not pos_abierta:
        return False

    # Buscar el trade OPEN en el journal
    all_trades = _load()
    current_trade = None
    for t in all_trades:
        if t.get('symbol') == SYMBOL and t.get('status') == 'OPEN' and t.get('bot_id', BOT_ID) == BOT_ID:
            current_trade = t
            break

    if not current_trade:
        # Trade manual o de otro bot — solo resguardar
        gestionar_resguardo_posicion(client, SYMBOL)
        return False

    entry_time_str = current_trade.get('entry_time', '')
    if not entry_time_str:
        gestionar_resguardo_posicion(client, SYMBOL)
        return False

    entry_time = datetime.fromisoformat(entry_time_str)
    minutos_abierto = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60.0

    # Determinar la estrategia con la que se abrió el trade
    bias_trade = current_trade.get('bias', 'MEAN_REV')
    es_reversion = bias_trade == 'MEAN_REV'

    # ── CHECK 1: Timeout para mean-reversion ──────────────────────────────
    if es_reversion and TIMEOUT_MINUTES_REVERSION > 0 and minutos_abierto >= TIMEOUT_MINUTES_REVERSION:
        logger.warning(
            "[%s] ⏰ TIMEOUT: trade MEAN_REV abierto hace %.0f min (límite: %d min) — cerrando a mercado.",
            SYMBOL, minutos_abierto, TIMEOUT_MINUTES_REVERSION
        )
        try:
            cancel_all_open_orders(client, SYMBOL)
            close_market_position(client, SYMBOL)
            crear_notifier().alert_error(
                f"⏰ *TIMEOUT* {SYMBOL}\n"
                f"Trade MEAN_REV cerrado tras {minutos_abierto:.0f} min\n"
                f"Límite: {TIMEOUT_MINUTES_REVERSION} min"
            )
        except Exception as e:
            logger.error("[%s] Error cerrando por timeout: %s", SYMBOL, e)
        return True

    # ── CHECK 2: Cambio de régimen ────────────────────────────────────────
    # Si el trade se abrió con reversion (régimen lateral) y ahora el
    # régimen cambió a tendencia o alta volatilidad, la tesis murió.
    if es_reversion and _regime_al_abrir >= 0 and _regime_actual >= 0:
        if _regime_actual != _regime_al_abrir:
            # Verificar PnL antes de cerrar — si está ganando, dejar correr con SL
            account = get_account_status(client)
            pnl_pct = account['unrealized_pnl'] / max(account['wallet_balance'], 1)
            if pnl_pct < 0.005:  # menos de +0.5% → cerrar
                logger.warning(
                    "[%s] 🔄 CAMBIO DE RÉGIMEN: trade MEAN_REV abierto en régimen %d, ahora régimen %d (PnL %.2f%%) — cerrando.",
                    SYMBOL, _regime_al_abrir, _regime_actual, pnl_pct * 100
                )
                try:
                    cancel_all_open_orders(client, SYMBOL)
                    close_market_position(client, SYMBOL)
                    crear_notifier().alert_error(
                        f"🔄 *RÉGIMEN CAMBIÓ* {SYMBOL}\n"
                        f"Abierto en régimen {_regime_al_abrir} → ahora {_regime_actual}\n"
                        f"PnL: {pnl_pct*100:.2f}% — cerrando"
                    )
                except Exception as e:
                    logger.error("[%s] Error cerrando por cambio de régimen: %s", SYMBOL, e)
                return True
            else:
                logger.info(
                    "[%s] Régimen cambió (%d→%d) pero PnL +%.2f%% — manteniendo con SL original.",
                    SYMBOL, _regime_al_abrir, _regime_actual, pnl_pct * 100
                )

    # ── Resguardo normal de SL/TP ─────────────────────────────────────────
    gestionar_resguardo_posicion(client, SYMBOL)
    return False


def inicializar():
    """Configuracion unica al arrancar el contenedor."""   
    logger.info("="*50)
    logger.info(f"Iniciando Bot {BOT_ID} para {SYMBOL} con WebSockets...")
    logger.info(f"Estrategia base: {STRATEGY.upper()} | TF: {TIMEFRAME}")
    logger.info(f"Regime state path: {REGIME_STATE_PATH}")
    logger.info(f"Trading window: {TRADING_WINDOW} UTC")
    logger.info(f"Timeout reversion: {TIMEOUT_MINUTES_REVERSION} min")
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
    Callback al cierre de cada vela.
    Lee el régimen del clasificador y elige la estrategia correspondiente.
    Responsabilidades:
    1. Auditoría de cuenta y posición
    2. Gestión activa de posición (timeout, cambio régimen)
    3. Actualizar bandas para el siguiente período intra-vela
    4. Dashboard
    La entrada NO se dispara aquí — lo hace _on_mark_price_tick().
    """                                                        
    
    global cycle_count, _bandas_actuales, _entrada_en_curso, _pos_abierta, _regime_actual

    try:
        if cycle_count % 60 == 0:
            logger.info("="*50)
            logger.info(f"  BOT {BOT_NAME}  R/R: {TP_RR_RATIO} Riesgo por trade: {RISK_PER_TRADE}%")
            logger.info(f"  Bot corriendo durante {cycle_count} minutos")
            logger.info("="*50)

        logger.info(f"--- Ciclo {cycle_count} para {SYMBOL} (Cierre detectado via WS) ---")

        # ── Leer régimen del clasificador ──────────────────────────────────
        _regime_actual = leer_regime()
        estrategia_activa = _get_strategy_from_regime(_regime_actual)
        logger.info("Regimen: %d | Estrategia: %s", _regime_actual, estrategia_activa.upper())

        # ── Régimen 2: Alta Volatilidad — parar todo ───────────────────────
        if estrategia_activa == "stop":
            logger.warning("[%s] Regimen ALTA VOLATILIDAD — operativa pausada.", SYMBOL)
            with _bandas_lock:
                _bandas_actuales = None  # bloquea _on_mark_price_tick
            # Si hay posición abierta con PnL > -1%, cerrar
            pos_abierta = get_open_position(client, SYMBOL)
            if pos_abierta:
                account = get_account_status(client)
                pnl_pct = account['unrealized_pnl'] / account['wallet_balance']
                if pnl_pct > -0.01:
                    logger.warning("[%s] Cerrando posicion por Alta Volatilidad (PnL %.2f%%)", SYMBOL, pnl_pct*100)
                    close_market_position(client, SYMBOL)
                else:
                    logger.info("[%s] Posicion con PnL negativo (%.2f%%) — heredar con SL original", SYMBOL, pnl_pct*100)
            cycle_count += 1
            return

        # ── Sincronización de cuenta ───────────────────────────────────────
        account = get_account_status(client)
        check_drawdown_alert(account['wallet_balance'], cycle_count)

        # -- Auditoría: sincronizar PnL real y detectar trades manuales                                                             
        sincronizar_realidad_vs_journal(client, SYMBOL)

        # ── Gestión ACTIVA de posición abierta ──────────────────────────────
        pos_abierta = get_open_position(client, SYMBOL)
        _pos_abierta = pos_abierta is not None

        if pos_abierta:
            cerro = _gestionar_posicion_abierta(pos_abierta, estrategia_activa)
            if cerro:
                _pos_abierta = False
                # Re-sincronizar para que el journal registre el cierre
                sincronizar_realidad_vs_journal(client, SYMBOL)

        # ── Actualizar bandas según estrategia activa ──────────────────────
        if estrategia_activa == "cross":
            nuevas_bandas = actualizar_bandas_cross(df_velas)
        else:
            nuevas_bandas = actualizar_bandas(df_velas)

        # ── Filtro de ventana horaria para nuevas entradas ─────────────────
        # Si estamos fuera de ventana, no publicamos bandas (bloquea entradas)
        # pero dejamos correr trades ya abiertos (gestionados arriba)
        if not _dentro_de_ventana_horaria():
            logger.debug("[%s] Fuera de ventana horaria %s UTC — bloqueando nuevas entradas.", SYMBOL, TRADING_WINDOW)
            with _bandas_lock:
                _bandas_actuales = None
        else:
            with _bandas_lock:
                _bandas_actuales = nuevas_bandas

        # Limpiar flag de entrada solo si no hay PENDING_FILL activo
        historial = _load()
        hay_pending = any(
            t.get('symbol') == SYMBOL and t.get('status') == 'PENDING_FILL'
            for t in historial
        )
        # Solo limpiar si no hay PENDING_FILL Y no hay thread de ejecución activo
        hay_ejecutando = any(t.name == "ejecutar-apertura" and t.is_alive()
                             for t in threading.enumerate())
        if not hay_pending and not hay_ejecutando:
            with _entrada_lock:
                _entrada_en_curso = False

        if nuevas_bandas:
            logger.info("Bandas | Upper: %.2f | Lower: %.2f | VWAP: %.2f",
                        nuevas_bandas["upper"], nuevas_bandas["lower"], nuevas_bandas["vwap"])
        else:
            logger.debug("Bandas no disponibles este ciclo (filtros activos).")

        # ── Dashboard ──────────────────────────────────────────────────────
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
    Usa la estrategia determinada por el régimen en _on_candle_close.
    Estrategia reversion: evalúa toque de banda.
    Estrategia cross: evalúa cruce del VWAP.
    Usa lock atómico para evitar race condition con múltiples ticks simultáneos.
    La ejecución de la orden se hace en thread separado para no bloquear el stream.                                      
                                                                      
    """
    global _entrada_en_curso, _precio_anterior, _regime_actual, _regime_al_abrir

    # Check atómico — si ya hay entrada en curso, salir inmediatamente                                                                 
    with _entrada_lock:
        if _entrada_en_curso:
            return

    with _bandas_lock:
        bandas = _bandas_actuales

    # bandas == None cuando régimen 2 (stop), filtros activos, o fuera de ventana horaria
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

        # ── Cooldown post-trade (incluye CANCELLED) ───────────────────────                                                                                                             
        if _cooldown_activo():
            _precio_anterior = mark_price
            return

        # ── Estrategia según régimen ───────────────────────────────────────
        estrategia_activa = _get_strategy_from_regime(_regime_actual)

        if estrategia_activa == "cross":
            if _precio_anterior <= 0:
                _precio_anterior = mark_price
                return
            # !!! FILTRO DE SEGURIDAD: ANCHO DE BANDA MÍNIMO !!!
            # Calculamos qué tan separadas están las bandas (volatilidad real de 1m)
            ancho_banda_pct = (bandas["upper"] - bandas["lower"]) / bandas["vwap"]
            # Definimos umbrales según el bot o símbolo
            if "BTC" in SYMBOL:
                UMBRAL_MINIMO = 0.005  # 0.5% para BTC (más estable)
            elif "SOL" in SYMBOL:
                UMBRAL_MINIMO = 0.01  # 1% para SOL (necesita más aire)
            else:
                UMBRAL_MINIMO = 0.008  # Default para otros
            
            if ancho_banda_pct < UMBRAL_MINIMO:
                # Si las bandas están muy pegadas, ignoramos el Cross para evitar ruidos
                if cycle_count % 5 == 0: # Para no spamear el log, logueamos cada tanto
                    logger.warning("[%s] CROSS abortado: Bandas demasiado estrechas (%.2f%%)", 
                                   SYMBOL, ancho_banda_pct * 100)
                _precio_anterior = mark_price
                return
            
            signal, entry_price, tp_price, sl_price = evaluar_cruce_vwap(
                mark_price, bandas, _precio_anterior
            )
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

        # ── Marcar entrada en curso ATÓMICAMENTE ─────────────────────────                                                                                                                            
        with _entrada_lock:
            if _entrada_en_curso:
                return
            _entrada_en_curso = True

        # Guardar el régimen al momento de abrir para detectar cambio después
        _regime_al_abrir = _regime_actual

        logger.info("[%s] Señal %s | Regimen: %d | Mark: %.2f | Entry: %.2f | TP: %.2f | SL: %.2f",
                    SYMBOL, estrategia_activa.upper(), _regime_actual,
                    mark_price, entry_price, tp_price, sl_price)

        # ── Calcular tamaño ───────────────────────────────────────────────                                                                                                                                                                       
        account = get_account_status(client)
        qty     = calculate_position_size(account['wallet_balance'], RISK_PER_TRADE, entry_price, sl_price)

        # Cap por margen disponible                           
        notional     = qty * entry_price
        max_notional = account['available'] * LEVERAGE * 0.8
        if notional > max_notional:
            qty_capped = round(max_notional / entry_price, 3)
            logger.warning("[%s] Qty capado: %.4f -> %.4f", SYMBOL, qty, qty_capped)
            qty = qty_capped

        if qty <= 0:
            logger.warning("[%s] Qty invalido, abortando.", SYMBOL)
            with _entrada_lock:
                _entrada_en_curso = False
            return

        # ── Ejecutar en thread separado para no bloquear el stream ────────                                                                                        
        _tp  = tp_price
        _sl  = sl_price
        _sig = signal
        _ep  = entry_price
        _bal = account['wallet_balance']
        _qty = qty
        _bias = "CROSS" if estrategia_activa == "cross" else "MEAN_REV"

        def _ejecutar():
            global _entrada_en_curso
            try:
                ejecutar_apertura_completa(
                    client, SYMBOL, _sig, _ep, _sl, _tp,
                    _qty, RISK_PER_TRADE, balance_at_open=_bal,
                    bias=_bias
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

    # candles_minimos se ajusta según el timeframe                                               
    tf_min_map  = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
    tf_min_val  = tf_min_map.get(TIMEFRAME, 1)
    candles_min = max(50, int(1440 / tf_min_val) + 10)

    logger.info("Conectando al WebSocket de velas...")
    stream_kline = BinanceKlineStream(
        symbol          = SYMBOL,
        interval        = TIMEFRAME,
        on_candle_close = _on_candle_close,
        testnet         = True,
        buffer_size     = max(1500, candles_min + 100),
        candles_minimos = candles_min,
    )
    stream_kline.iniciar(df_historico)

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
