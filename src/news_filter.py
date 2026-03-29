# src/news_filter.py
"""
Filtro de noticias económicas de alto impacto.
Fuente: FCS API (https://fcsapi.com)

Vars de entorno requeridas:
  FCS_API_KEY               — API key de FCS
  NEWS_FILTER_ENABLED       — "true" / "false" (default: true)
  NEWS_BLOCK_MINUTES_BEFORE — minutos antes del evento para bloquear (default: 120)
  NEWS_CLOSE_IF_LOSS_PCT    — % máximo de pérdida para cerrar igual (default: -1.0)
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta
from src.logger import logger

# ── Configuración desde .env ──────────────────────────────────────────────────
FCS_API_KEY            = os.getenv("FCS_API_KEY", "")
NEWS_FILTER_ENABLED    = os.getenv("NEWS_FILTER_ENABLED", "true").lower() == "true"
BLOCK_MINUTES_BEFORE   = int(os.getenv("NEWS_BLOCK_MINUTES_BEFORE", "120"))
CLOSE_IF_LOSS_PCT      = float(os.getenv("NEWS_CLOSE_IF_LOSS_PCT", "-1.0"))  # ej: -1.0 = hasta -1%

# ── Cache en memoria ──────────────────────────────────────────────────────────
_cache_events: list      = []
_cache_timestamp: float  = 0.0
_CACHE_TTL_SECONDS       = 15 * 60  # 15 minutos

# ── Mapeo símbolo → monedas a monitorear ─────────────────────────────────────
# FCS usa códigos de moneda estándar. Crypto con alto impacto USD se trata como USD.
SYMBOL_CURRENCIES = {
    "BTCUSDT": ["USD"],
    "ETHUSDT": ["USD"],
    "SOLUSDT": ["USD"],
    "BNBUSDT": ["USD"],
}


def _fetch_events_from_api() -> list:
    if not FCS_API_KEY:
        logger.warning("[NewsFilter] FCS_API_KEY no configurada. Filtro desactivado.")
        return []

    # Pedir desde hoy hasta 7 días adelante
    now   = datetime.now(timezone.utc)
    date_from = now.strftime("%Y-%m-%d")
    date_to   = (now + timedelta(days=7)).strftime("%Y-%m-%d")

    url = "https://api-v4.fcsapi.com/forex/economy_cal"
    params = {
        "access_key": FCS_API_KEY,
        "symbol":     "USD",       # monedas de alto impacto para crypto
        "from":       date_from,
        "to":         date_to,
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") is False:
            logger.warning(f"[NewsFilter] FCS API error: {data.get('msg')}")
            return []

        events = []
        for item in data.get("response", []):
            try:
                # importance: "2" = high impact
                if str(item.get("importance", "0")) != "2":
                    continue
                dt_str   = item.get("date", "")
                currency = item.get("currency", item.get("country", "")).upper()
                title    = item.get("title", "")
                dt_utc   = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                events.append({
                    "time_utc": dt_utc,
                    "currency": currency,
                    "event":    title,
                })
            except Exception as e:
                logger.debug(f"[NewsFilter] Error parseando evento: {e}")
                continue

        logger.info(f"[NewsFilter] {len(events)} eventos high-impact descargados de FCS API.")
        return events

    except requests.RequestException as e:
        logger.error(f"[NewsFilter] Error conectando a FCS API: {e}")
        return []


def _get_events_cached() -> list:
    """Devuelve eventos desde cache, refrescando si expiró."""
    global _cache_events, _cache_timestamp

    now = time.monotonic()
    if now - _cache_timestamp > _CACHE_TTL_SECONDS:
        logger.info("[NewsFilter] Refrescando cache de eventos económicos...")
        _cache_events    = _fetch_events_from_api()
        _cache_timestamp = now

    return _cache_events


def is_news_blocked(symbol: str, now_utc: datetime = None) -> tuple[bool, str]:
    """
    Retorna (blocked: bool, reason: str).
    blocked=True si hay un evento de alto impacto en la ventana de bloqueo.
    """
    if not NEWS_FILTER_ENABLED:
        return False, ""

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    currencies = SYMBOL_CURRENCIES.get(symbol.upper(), ["USD"])
    events     = _get_events_cached()
    window_end = now_utc + timedelta(minutes=BLOCK_MINUTES_BEFORE)

    for ev in events:
        if ev["currency"] not in currencies:
            continue
        # Bloquear si el evento cae dentro de la ventana futura
        # También bloquear 15 min después del evento (volatilidad post-news)
        ev_time = ev["time_utc"]
        post_window = ev_time + timedelta(minutes=15)

        if now_utc <= ev_time <= window_end or (ev_time <= now_utc <= post_window):
            reason = f"{ev['event']} ({ev['currency']}) @ {ev_time.strftime('%H:%M')} UTC"
            return True, reason

    return False, ""


def should_close_position(current_pnl_pct: float) -> bool:
    """
    Retorna True si la posición debe cerrarse durante noticia.
    Cierra si está en profit (pnl_pct >= 0) o si la pérdida es <= CLOSE_IF_LOSS_PCT.
    Ejemplo: CLOSE_IF_LOSS_PCT=-1.0 → cierra si pnl_pct >= -1.0%
    """
    return current_pnl_pct >= CLOSE_IF_LOSS_PCT


def check_and_close_on_news(client, symbol: str, journal_load_fn, journal_close_fn,
                             get_position_fn, close_position_fn, notifier=None):
    """
    Si hay noticia bloqueante y hay posición abierta, evalúa si cerrarla.
    Compatible con el formato de get_open_position del VWAP bot:
      {"size": float, "side": "LONG"/"SHORT", "entry": float}
    """
    blocked, reason = is_news_blocked(symbol)
    if not blocked:
        return

    pos = get_position_fn(client, symbol)
    if not pos:
        return

    try:
        entry = float(pos.get("entry", 0))
        side  = pos.get("side", "LONG")

        # Obtener mark price actual
        ticker = client.futures_mark_price(symbol=symbol)
        mark   = float(ticker.get("markPrice", 0))

        if entry <= 0 or mark <= 0:
            logger.warning(f"[NewsFilter] No se pudo calcular PnL (entry={entry}, mark={mark})")
            return

        if side == "LONG":
            pnl_pct = ((mark - entry) / entry) * 100
        else:
            pnl_pct = ((entry - mark) / entry) * 100

    except Exception as e:
        logger.error(f"[NewsFilter] Error calculando PnL: {e}")
        return

    if should_close_position(pnl_pct):
        logger.warning(
            f"[NewsFilter] Cerrando {symbol} por noticia: {reason} | PnL: {pnl_pct:+.2f}%"
        )
        try:
            close_position_fn(client, symbol)
            if notifier:
                notifier.send(
                    f"📰 *Noticia detectada* — Posición cerrada\n"
                    f"Par: `{symbol}` | PnL: `{pnl_pct:+.2f}%`\n"
                    f"Evento: _{reason}_"
                )
        except Exception as e:
            logger.error(f"[NewsFilter] Error cerrando posición: {e}")
    else:
        logger.info(
            f"[NewsFilter] Noticia detectada ({reason}) pero PnL {pnl_pct:+.2f}% "
            f"< umbral {CLOSE_IF_LOSS_PCT}%. Posición continúa."
        )
