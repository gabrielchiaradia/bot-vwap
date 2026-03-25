import requests
import time
from threading import Thread
from src.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, BOT_NAME
from src.logger import logger

_ultimo_heartbeat = time.time()

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

    def alert_trade_open(self, symbol, direction, entry, sl, tp, risk, qty):
        emoji = "🚀" if direction == "LONG" else "📉"
        # Calculamos la distancia porcentual al TP/SL para dar más info
        dist_tp = abs((tp - entry) / entry) * 100
        
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
            f"🛡️ <b>Riesgo:</b> <code>{risk}%</code> del Capital\n"
            f"───────────────────\n"
            f"⏳ <i>Esperando ejecución de órdenes...</i>"
        )
        self._send_async(msg)

    def alert_trade_close(self, symbol, pnl, result, qty, entry_price, exit_price):
        if result == "WIN":
            emoji, title = "✅", "POSICIÓN CERRADA (PROFIT)"
        elif result == "BREAKEVEN":
            emoji, title = "⚖️", "POSICIÓN CERRADA (BE)"
        else:
            emoji, title = "🏁", "POSICIÓN CERRADA (LOSS)"
            
        pnl_emoji = "💵" if pnl > 0 else "💸"
        pnl_perc = (pnl / (entry_price * qty)) * 100 if entry_price and qty else 0

        msg = self._tag(
            f"{emoji} <b>{title}</b>\n"
            f"───────────────────\n"
            f"💎 <b>Par:</b> <code>{symbol}</code>\n"
            f"🏁 <b>Resultado:</b> <b>{result}</b>\n"
            f"───────────────────\n"
            f"🛫 <b>In:</b> <code>{entry_price:.2f}</code>\n"
            f"🛬 <b>Out:</b> <code>{exit_price:.2f}</code>\n"
            f"{pnl_emoji} <b>PnL Neto:</b> <code>{pnl:+.2f} USDT</code>\n"
            f"📈 <b>Rendimiento:</b> <code>{pnl_perc:+.2f}%</code>\n"
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

    def heartbeat_si_corresponde(self, client, cycle_count: int, minutos: int = 60):
        global _ultimo_heartbeat
        ahora = time.time()
        
        # 60 minutos * 60 segundos = 3600 segundos
        if ahora - _ultimo_heartbeat >= (minutos * 60):
            try:
                # Importamos acá adentro para evitar problemas cruzados
                from src.exchange import get_account_status
                balance = get_account_status(client)
                
                msg = self._tag(
                    f"🟢 <b>REPORTE DE ESTADO</b>\n"
                    f"───────────────────\n"
                    f"🕒 <b>Uptime:</b> {cycle_count} minutos\n"
                    f"💰 <b>Balance:</b> <code>{balance:.2f} USDT</code>\n"
                    f"✅ <i>El bot sigue operando correctamente.</i>"
                )
                self._send_async(msg)
                
                # Reseteamos el reloj
                _ultimo_heartbeat = ahora
            except Exception as e:
                logger.warning(f"⚠️ Error enviando heartbeat: {e}")
# ══════════════════════════════════════════════════════════
#  FACTORY (Para que el resto del bot lo use fácil)
# ══════════════════════════════════════════════════════════
def crear_notifier() -> TelegramNotifier:
    return TelegramNotifier(token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID, bot_tag=BOT_NAME)