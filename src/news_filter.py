# src/news_filter.py
"""
Filtro de noticias económicas de alto impacto.
Fuente: Forex Factory (API pública no oficial, sin key requerida)
  Semana actual:  https://nfs.faireconomy.media/ff_calendar_thisweek.json
  Semana próxima: https://nfs.faireconomy.media/ff_calendar_nextweek.json

Vars de entorno:
  NEWS_FILTER_ENABLED       — "true" / "false" (default: true)
  NEWS_BLOCK_MINUTES_BEFORE — minutos antes del evento para bloquear (default: 120)
  NEWS_CLOSE_IF_LOSS_PCT    — % máximo de pérdida para cerrar igual (default: -1.0)
                              ej: -1.0 significa: cierra si PnL >= -1.0%
                              (en profit O hasta 1% abajo)
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta
from src.logger import logger

# ── Configuración desde .env ──────────────────────────────────────────────────
NEWS_FILTER_ENABLED  = os.getenv("NEWS_FILTER_ENABLED", "true").lower() == "true"
BLOCK_MINUTES_BEFORE = int(os.getenv("NEWS_BLOCK_MINUTES_BEFORE", "120"))
CLOSE_IF_LOSS_PCT    = float(os.getenv("NEWS_CLOSE_IF_LOSS_PCT", "0.0"))

# ── Cache en memoria ──────────────────────────────────────────────────────────
_cache_events: list   = []
_cache_timestamp: float = 0.0
_CACHE_TTL_SECONDS    = 15 * 60  # refrescar cada 15 minutos

# ── URLs Forex Factory ────────────────────────────────────────────────────────
FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]


def _fetch_events_from_ff() -> list:
    """
    Descarga eventos de alto impacto de Forex Factory.
    Filtra solo impacto 'High' y moneda 'USD'.
    Retorna lista de dicts: {time_utc, currency, event}
    """
    events = []

    for url in FF_URLS:
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 404:
                continue  # silencioso, nextweek no siempre existe
            if resp.status_code != 200:
                logger.warning(f"[NewsFilter] FF {url} → HTTP {resp.status_code}")
                continue

            data = resp.json()
            for item in data:
                try:
                    # Forex Factory: impact = "High" / "Medium" / "Low" / "Holiday"
                    if item.get("impact", "").lower() != "high":
                        continue

                    currency = item.get("country", "").upper()
                    if currency != "USD":
                        continue

                    title   = item.get("title", "")
                    dt_str  = item.get("date", "")  # formato: "2026-03-28T12:30:00-04:00"

                    # Parsear con timezone incluido
                    dt_utc = datetime.fromisoformat(dt_str).astimezone(timezone.utc)

                    events.append({
                        "time_utc": dt_utc,
                        "currency": currency,
                        "event":    title,
                    })
                except Exception as e:
                    logger.debug(f"[NewsFilter] Error parseando item FF: {e}")
                    continue

            logger.debug(f"[NewsFilter] {url} OK — {len(events)} high-impact USD hasta ahora")

        except requests.RequestException as e:
            logger.warning(f"[NewsFilter] Error descargando {url}: {e}")
            continue

    logger.info(f"[NewsFilter] {len(events)} eventos high-impact USD cargados de Forex Factory.")
    return events


def _get_events_cached() -> list:
    """Devuelve eventos desde cache, refrescando si expiró el TTL."""
    global _cache_events, _cache_timestamp

    now = time.monotonic()
    if now - _cache_timestamp > _CACHE_TTL_SECONDS:
        logger.info("[NewsFilter] Refrescando cache de eventos económicos (Forex Factory)...")
        _cache_events    = _fetch_events_from_ff()
        _cache_timestamp = now

    return _cache_events


def is_news_blocked(symbol: str, now_utc: datetime = None) -> tuple:
    """
    Retorna (blocked: bool, reason: str).
    blocked=True si hay un evento USD de alto impacto en la ventana de bloqueo.

    Ventana de bloqueo:
      - Desde: ahora hasta evento + BLOCK_MINUTES_BEFORE minutos adelante
      - Post-evento: 15 minutos después del evento
    """
    if not NEWS_FILTER_ENABLED:
        return False, ""

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    events     = _get_events_cached()
    window_end = now_utc + timedelta(minutes=BLOCK_MINUTES_BEFORE)

    for ev in events:
        ev_time     = ev["time_utc"]
        post_window = ev_time + timedelta(minutes=15)

        # Bloquear si el evento está próximo O si acabamos de pasar uno
        if now_utc <= ev_time <= window_end or (ev_time <= now_utc <= post_window):
            reason = (
                f"{ev['event']} ({ev['currency']}) "
                f"@ {ev_time.strftime('%Y-%m-%d %H:%M')} UTC"
            )
            return True, reason

    return False, ""


def should_close_position(current_pnl_pct: float) -> bool:
    """
    Retorna True si la posición debe cerrarse durante una noticia.

    Cierra si:
      - PnL >= 0%  (en profit)
      - PnL >= CLOSE_IF_LOSS_PCT  (ej: -1.0 → hasta 1% abajo)

    No cierra si la posición está más de 1% en negativo (deja correr el SL).
    """
    return current_pnl_pct >= CLOSE_IF_LOSS_PCT


def check_and_close_on_news(client, symbol: str, journal_load_fn, journal_close_fn,
                             get_position_fn, close_position_fn, notifier=None):
    """
    Si hay noticia bloqueante y hay posición abierta, evalúa si cerrarla.

    Cierra solo si PnL >= CLOSE_IF_LOSS_PCT (en profit o caída <= 1%).
    Compatible con get_open_position del VWAP bot:
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
                notifier._send_async(
                    f"⚠️ <b>Posición cerrada por noticias</b>\n"
                    f"📌 {symbol} | PnL: {pnl_pct:.2f}%\n"
                    f"📰 {reason}"
                )

        except Exception as e:
            logger.error(f"[NewsFilter] Error cerrando posición: {e}")
    else:
        logger.info(
            f"[NewsFilter] Noticia detectada ({reason}) pero PnL {pnl_pct:+.2f}% "
            f"< umbral {CLOSE_IF_LOSS_PCT}%. Posición continúa con SL original."
        )
