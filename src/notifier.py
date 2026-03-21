import requests
from threading import Thread
from src.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, BOT_NAME
from src.logger import logger

def _send_async(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
        
    def task():
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            resp = requests.post(
                url, 
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, 
                timeout=10
            )
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"Error en Telegram async: {e}")

    # Lanzamos el envío en un hilo separado
    Thread(target=task, daemon=True).start()

def _tag(msg: str) -> str:
    return f"<b>[{BOT_NAME}]</b> {msg}"

def alert_trade_open(symbol, direction, entry, sl, tp, risk):
    emoji = "🔵" if direction == "LONG" else "🟠"
    msg = _tag(
        f"{emoji} <b>TRADE ABIERTO</b>\n"
        f"Par: <b>{symbol}</b>\n"
        f"Entrada: {entry:.2f}\n"
        f"SL: {sl:.2f} | TP: {tp:.2f}\n"
        f"Riesgo: {risk}%"
    )
    _send_async(msg)

def alert_trade_close(symbol, pnl, result):
    emoji = "✅" if result == "WIN" else "❌"
    msg = _tag(
        f"{emoji} <b>TRADE CERRADO</b>\n"
        f"Par: <b>{symbol}</b>\n"
        f"Resultado: <b>{result}</b>\n"
        f"PnL: {pnl:+.2f} USDT"
    )
    _send_async(msg)

def alert_error(context, error):
    msg = _tag(f"⚠️ <b>ERROR</b> en {context}\n<code>{error}</code>")
    _send_async(msg)
    
def alert_startup(symbols: str, riskreward: str, rr: str):
    msg = _tag(
        f"🚀 <b>Bot iniciado</b>\n"
        f"Par: {symbols}\n"
        f"Risk: {riskreward} - RR: {rr}"
    )
    _send_async(msg)