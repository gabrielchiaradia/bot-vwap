import os
from dotenv import load_dotenv

load_dotenv()

# Identificación (Viene del environment del docker-compose)
BOT_ID = os.getenv("BOT_ID", "DEV")  # ETH o BTC
BOT_NAME = os.getenv("BOT_NAME", f"VWAP_{BOT_ID}")

# Binance
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
IS_TESTNET = os.getenv("IS_TESTNET", "True").lower() == "true"

# Símbolo y Parámetros
SYMBOL = os.getenv("SYMBOL", "ETHUSDT")
LEVERAGE = int(os.getenv("LEVERAGE", "20"))

# Estrategia VWAP
BAND_MULT = float(os.getenv("BAND_MULT", "2.5"))
TP_RR_RATIO = float(os.getenv("TP_RR_RATIO", "0.4"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "4.0"))

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Rutas Dinámicas para Journal y Live Writer
# Esto asegura que cada contenedor escriba en su propio archivo
JOURNAL_FILE = f"logs/journal_{BOT_ID}.json"
STATUS_FILE = f"logs/bot_status_{BOT_ID}.json"
OPEN_POSITIONS_FILE = f"logs/open_positions_{BOT_ID}.json"
DASHBOARD_TRADES_FILE = f"logs/dashboard_trades_{BOT_ID}.json"