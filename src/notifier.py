import requests
import time
from threading import Thread
from src.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, BOT_NAME
from src.logger import logger

_ultimo_heartbeat = 0
_balance_ayer = 0.0

class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, bot_tag: str):
        self.token = token
        self.chat_id = chat_id
        self.bot_tag = bot_tag
        self.enabled = bool(token and chat_id)

    def _send_async(self, text: str):
        if not self.enabled: return
        def task():
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            try:
                resp = requests.post(url, json=payload, timeout=10)
                resp.raise_for_status()
            except Exception as e:
                logger.warning(f"⚠️ Error Telegram: {e}")
        Thread(target=task, daemon=True).start()

    def _tag(self, msg: str) -> str:
        return f"🤖 <b>[{self.bot_tag}]</b>\n{msg}"

    def alert_startup(self, symbol: str, risk: float, rr: float, multiplier: float, balance: float):
        msg = self._tag(
            f"🚀 <b>Bot Iniciado</b>\n"
            f"───────────────────\n"
            f"📍 <b>Par:</b> <code>{symbol}</code>\n"
            f"🛡️ <b>Riesgo:</b> <code>{risk}%</code> | RR: <code>{rr}</code>\n"
            f"📊 <b>Bands Mult:</b> <code>{multiplier}</code>\n"
            f"💰 <b>Balance Inicial:</b> <code>{balance:.2f} USDT</code>\n"
            f"───────────────────\n"
            f"📡 <i>Escuchando mercado en tiempo real...</i>"
        )
        self._send_async(msg)

    def alert_trade_open(self, symbol, direction, entry, sl, tp, qty, risk_pct, strategy: str = ""):
        emoji = "🚀" if direction == "LONG" else "📉"
        dist_tp = abs((tp - entry) / entry) * 100
        strategy_label = f"\n📊 <b>Estrategia:</b> <code>{strategy}</code>" if strategy else ""

        msg = self._tag(
            f"{emoji} <b>NUEVA POSICIÓN ABIERTA</b>\n"
            f"───────────────────\n"
            f"💎 <b>Instrumento:</b> <code>{symbol}</code>\n"
            f"↔️ <b>Dirección:</b> <b>{direction}</b>\n"
            f"💰 <b>Cantidad:</b> <code>{qty}</code>\n"
            f"───────────────────\n"
            f"📥 <b>Entrada:</b> <code>{entry:.2f}</code>\n"
            f"🎯 <b>Take Profit:</b> <code>{tp:.2f}</code> (<i>+{dist_tp:.1f}%</i>)\n"
            f"🛑 <b>Stop Loss:</b> <code>{sl:.2f}</code>\n"
            f"🛡️ <b>Riesgo:</b> <code>{risk_pct}%</code> del Capital"
            f"{strategy_label}\n"
            f"───────────────────\n"
            f"⏳ <i>Esperando ejecución de órdenes...</i>"
        )
        self._send_async(msg)

    def alert_trade_close(self, symbol, pnl, result, qty, entry_price, exit_price, balance_at_open: float = 0.0):
        if result == "WIN":
            emoji, title = "✅", "POSICIÓN CERRADA (PROFIT)"
        elif result == "BREAKEVEN":
            emoji, title = "⚖️", "POSICIÓN CERRADA (BE)"
        else:
            emoji, title = "🏁", "POSICIÓN CERRADA (LOSS)"

        pnl_emoji = "💵" if pnl > 0 else "💸"
        pnl_perc = (pnl / balance_at_open) * 100 if balance_at_open > 0 else 0

        msg = self._tag(
            f"{emoji} <b>{title}</b>\n"
            f"───────────────────\n"
            f"💎 <b>Par:</b> <code>{symbol}</code>\n"
            f"🏁 <b>Resultado:</b> <b>{result}</b>\n"
            f"───────────────────\n"
            f"🛫 <b>In:</b> <code>{entry_price:.2f}</code>\n"
            f"🛬 <b>Out:</b> <code>{exit_price:.2f}</code>\n"
            f"{pnl_emoji} <b>PnL Neto:</b> <code>{pnl:+.2f} USDT</code>\n"
            f"📈 <b>Rendimiento:</b> <code>{pnl_perc:+.2f}%</code> <i>sobre balance al abrir</i>\n"
            f"───────────────────\n"
            f"✅ <i>Libro de órdenes actualizado.</i>"
        )
        self._send_async(msg)

    def alert_error(self, context, error):
        msg = self._tag(
            f"🚨 <b>ALERTA DE ERROR</b>\n"
            f"───────────────────\n"
            f"🔧 <b>Contexto:</b> {context}\n"
            f"❌ <b>Detalle:</b> <code>{str(error)[:150]}</code>\n"
            f"───────────────────\n"
            f"⚠️ <i>Revisar logs en el servidor inmediatamente.</i>"
        )
        self._send_async(msg)

    def heartbeat_si_corresponde(self, client, cycle_count: int):
        global _ultimo_heartbeat
        from datetime import datetime, timezone, date

        ahora_utc = datetime.now(timezone.utc)
        ultimo_dt = datetime.fromtimestamp(_ultimo_heartbeat, tz=timezone.utc)

        if ahora_utc.hour == 19 and ahora_utc.minute == 0 and ultimo_dt.date() < ahora_utc.date():
            _ultimo_heartbeat = ahora_utc.timestamp()
            try:
                from src.exchange import get_account_status
                from src.journal import _load
                from src.config import BOT_ID

                account      = get_account_status(client)
                pnl_abierto  = account['unrealized_pnl']

                # PnL del día desde el journal de este bot (no del balance compartido)
                hoy = ahora_utc.date().isoformat()
                trades = _load()
                pnl_diario = sum(
                    float(t.get('pnl_usdt', 0))
                    for t in trades
                    if t.get('bot_id') == BOT_ID
                    and t.get('status') == 'CLOSED'
                    and t.get('close_time', '')[:10] == hoy
                )
                trades_hoy = [
                    t for t in trades
                    if t.get('bot_id') == BOT_ID
                    and t.get('status') == 'CLOSED'
                    and t.get('close_time', '')[:10] == hoy
                ]
                wins   = sum(1 for t in trades_hoy if float(t.get('pnl_usdt', 0)) > 0)
                losses = sum(1 for t in trades_hoy if float(t.get('pnl_usdt', 0)) <= 0)

                pnl_open_emoji = "📈" if pnl_abierto >= 0 else "📉"
                pnl_day_emoji  = "💰" if pnl_diario >= 0 else "🧧"

                msg = self._tag(
                    f"📊 <b>REPORTE DIARIO | 19:00 UTC</b>\n"
                    f"───────────────────\n"
                    f"{pnl_day_emoji} <b>PnL Hoy:</b> <code>{pnl_diario:+.2f} USDT</code>\n"
                    f"📋 <b>Trades:</b> {len(trades_hoy)} ({wins}W / {losses}L)\n"
                    f"{pnl_open_emoji} <b>PnL Abierto:</b> <code>{pnl_abierto:+.2f} USDT</code>\n"
                    f"───────────────────\n"
                    f"🕒 <b>Uptime:</b> {cycle_count} min\n"
                    f"✅ <i>Bot operando sin interrupciones.</i>"
                )

                self._send_async(msg)
                logger.info(f"✅ Reporte diario enviado. PnL hoy: {pnl_diario:+.2f} USDT ({len(trades_hoy)} trades)")

            except Exception as e:
                logger.warning(f"⚠️ Error enviando heartbeat: {e}")
# ══════════════════════════════════════════════════════════
#  FACTORY (Para que el resto del bot lo use fácil)
# ══════════════════════════════════════════════════════════
def crear_notifier() -> TelegramNotifier:
    return TelegramNotifier(token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID, bot_tag=BOT_NAME)
