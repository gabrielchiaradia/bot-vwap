import hmac
import hashlib
import time
import json
import math
import urllib.request
import urllib.parse
import urllib.error
from binance.client import Client
from binance.enums import *
from src.config import BINANCE_API_KEY, BINANCE_API_SECRET, IS_TESTNET, LEVERAGE, BOT_ID
from src.logger import logger

# ── URL base según entorno ────────────────────────────────────────────────────
_BASE_URL = (
    "https://testnet.binancefuture.com"
    if IS_TESTNET
    else "https://fapi.binance.com"
)


def get_client():
    return Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=IS_TESTNET)


def set_leverage(client, symbol):
    try:
        # 1. Intentar cambiar el apalancamiento
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        logger.info(f"  [{BOT_ID}] Apalancamiento configurado a {LEVERAGE}x")
        try:
            # 2. Intentar cambiar el tipo de margen
            client.futures_change_margin_type(symbol=symbol, marginType='ISOLATED')
            logger.info(f"  [{BOT_ID}] Margen configurado: ISOLATED")
        except Exception as e:
            # Si el error es el -4046, lo ignoramos porque ya está en ISOLATED
            if "No need to change margin type" in str(e) or "-4046" in str(e):
                logger.info(f"  [{BOT_ID}] El margen ya era ISOLATED. Continuando...")
            else:
                raise e # Si es otro error distinto, que explote para avisar
    except Exception as e:
        logger.error(f"Error crítico configurando apalancamiento/margen: {e}")


def get_account_status(client):
    try:
        acc = client.futures_account()
        return {
            # .get('llave', valor_por_defecto) evita que el bot se detenga si la llave falta
            "wallet_balance": float(acc.get('totalWalletBalance', 0.0)),
            "unrealized_pnl": float(acc.get('totalUnrealizedProfit', 0.0)),
            "margin_balance": float(acc.get('totalMarginBalance', 0.0)),
            "available":      float(acc.get('availableBalance', 0.0)),
        }
    except Exception as e:
        logger.error(f"Error crítico obteniendo cuenta: {e}")
        return {"wallet_balance": 0.0, "unrealized_pnl": 0.0,
                "margin_balance": 0.0, "available": 0.0}


def cancel_all_open_orders(client, symbol):
    #Cancela todas las órdenes LIMIT abiertas para un símbolo
    try:
        client.futures_cancel_all_open_orders(symbol=symbol)
        logger.debug(f"[{BOT_ID}] Órdenes abiertas canceladas en {symbol}")
    except Exception:
        pass


def place_limit_order(client, symbol, side, price, quantity):
    #Coloca una orden LIMIT (Maker estricto). Si el precio ya cruzó, aborta.
    try:
        # 1. Redondeo dinámico de PRECIO (Tick Size)
        tick = get_tick_size(client, symbol)
        price = _round_tick(float(price), tick)
        # 2. Redondeo dinámico de CANTIDAD (Step Size)
        step = get_step_size(client, symbol)
        quantity = _round_tick(float(quantity), step)
        # 3. Enviar la orden
        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type=FUTURE_ORDER_TYPE_LIMIT,
            timeInForce=TIME_IN_FORCE_GTX,
            quantity=quantity,
            price=price
        )
        logger.info(f"[{BOT_ID}] ✅ Orden LIMIT {side} colocada en {price}")
        return order

    except Exception as e:
        error_str = str(e)
        # Si Binance rechaza por Post-Only, llegamos tarde al rebote.
        if "5022" in error_str or "immediately trigger" in error_str or "Post Only" in error_str:
            logger.warning(f"[{symbol}] ⏳ Oportunidad perdida: El precio ya cruzó la banda ({price}). Abortando entrada.")
            return None
            
        # Si es cualquier otro error (falta de saldo, desconexión, etc)
        logger.error(f"❌ Error colocando LIMIT: {e}")
        return None


# ── Algo Order API (reemplaza STOP_MARKET / TAKE_PROFIT_MARKET) ───────────────

def _algo_order(symbol: str, close_side: str, qty: float,
                stop_price: float, tipo: str) -> bool:
    """
    Coloca una orden condicional via Algo API (/fapi/v1/algoOrder).
    tipo: "STOP" para SL, "TAKE_PROFIT" para TP.
    Firma con HMAC-SHA256 igual que el SMC bot.
    """
    params = {
        "symbol":       symbol,
        "side":         close_side,
        "quantity":     str(qty),
        "triggerprice": str(stop_price),
        "price":        str(stop_price),
        "type":         tipo,
        "algoType":     "CONDITIONAL",
        "workingType":  "MARK_PRICE",
        "reduceOnly":   "true",
        "timestamp":    str(int(time.time() * 1000)),
    }
    query = urllib.parse.urlencode(params)
    sig = hmac.new(
        BINANCE_API_SECRET.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()
    url = f"{_BASE_URL}/fapi/v1/algoOrder?{query}&signature={sig}"
    req = urllib.request.Request(
        url,
        headers={"X-MBX-APIKEY": BINANCE_API_KEY},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode())
            logger.info(f"[{BOT_ID}] AlgoOrder {tipo} OK: algoId={resp.get('algoId')}")
            return True
    except urllib.error.HTTPError as e:
        logger.error(f"[{BOT_ID}] AlgoOrder {tipo} HTTP {e.code}: {e.read().decode()}")
        return False
    except Exception as e:
        logger.error(f"[{BOT_ID}] AlgoOrder {tipo} error: {e}")
        return False


def place_sl_tp(client, symbol, side, qty, sl_price, tp_price):
    """
    Coloca SL y TP via Algo API.
    side: el side de ENTRADA ("BUY" o "SELL") — se invierte internamente para cerrar.
    """
    try:
        tick = get_tick_size(client, symbol)
        close_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
        sl_price = _round_tick(float(sl_price), tick)
        tp_price = _round_tick(float(tp_price), tick)

        # Cancelar órdenes de salida previas (limpieza selectiva)
        open_orders = client.futures_get_open_orders(symbol=symbol)
        exit_types = {'STOP_MARKET', 'TAKE_PROFIT_MARKET', 'TAKE_PROFIT', 'STOP'}
        for o in open_orders:
            if o['type'] in exit_types:
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=o['orderId'])
                except Exception:
                    pass

        sl_ok = _algo_order(symbol, close_side, qty, sl_price, "STOP")
        tp_ok = _algo_order(symbol, close_side, qty, tp_price, "TAKE_PROFIT")

        if sl_ok and tp_ok:
            logger.info(f"[{BOT_ID}] ✅ Protección colocada: SL {sl_price} | TP {tp_price} (tick {tick})")
        elif sl_ok:
            logger.warning(f"[{BOT_ID}] ⚠️ Solo SL colocado. Falló TP {tp_price}")
        elif tp_ok:
            logger.warning(f"[{BOT_ID}] ⚠️ Solo TP colocado. Falló SL {sl_price}")
        else:
            logger.error(f"[{BOT_ID}] ❌ Falló colocación de SL y TP")

    except Exception as e:
        logger.error(f"Error en place_sl_tp: {e}")


def verificar_y_rescatar_sl_tp(client, symbol, current_trade):
    """
    Verifica si una posición abierta tiene sus órdenes de protección (SL/TP).
    Si faltan, las coloca via Algo API.
    """
    try:
        # 1. Obtener órdenes abiertas de Binance
        open_orders = client.futures_get_open_orders(symbol=symbol)

        # 2. Filtramos SL y TP — incluye LIMIT reduceOnly (usado como TP)
        exit_orders = [
            o for o in open_orders
            if o['type'] in ['STOP_MARKET', 'TAKE_PROFIT_MARKET', 'TAKE_PROFIT', 'STOP']
            or (o.get('closePosition') == True)
        ]

        # Si hay exactamente 1 TP sin SL, puede ser que el SL esté como algo
        # No recolocamos nada para no duplicar
        solo_tp_presente = (
            len(exit_orders) == 1 and
            exit_orders[0]['type'] == 'TAKE_PROFIT_MARKET' and
            exit_orders[0].get('closePosition') == True
        )
        if solo_tp_presente:
            logger.debug(f"[{symbol}] SL condicional detectado. Proteccion completa.")
            return False

        if len(exit_orders) >= 2:
            logger.debug(f"[{symbol}] Proteccion completa.")
            return False

        # Faltan órdenes — rescatar via Algo API
        side_bot = current_trade.get('direction') or current_trade.get('side')
        side_entry = "BUY" if side_bot == "LONG" else "SELL"
        close_side = SIDE_SELL if side_entry == "BUY" else SIDE_BUY

        qty = float(current_trade['quantity'])
        sl  = float(current_trade['sl_price'])
        tp  = float(current_trade['tp_price'])

        tick = get_tick_size(client, symbol)
        sl = _round_tick(sl, tick)
        tp = _round_tick(tp, tick)

        tiene_sl = any(
            o['type'] in {'STOP_MARKET', 'STOP'} and
            (o.get('reduceOnly') == True or o.get('closePosition') == True)
            for o in open_orders
        )
        tiene_tp = any(
            o.get('closePosition') == True and
            o['type'] in {'TAKE_PROFIT_MARKET', 'TAKE_PROFIT'}
            for o in open_orders
        )

        rescatados = 0
        if not tiene_sl:
            if _algo_order(symbol, close_side, qty, sl, "STOP"):
                logger.info(f"[{symbol}] SL rescatado en {sl}")
                rescatados += 1

        if not tiene_tp:
            if _algo_order(symbol, close_side, qty, tp, "TAKE_PROFIT"):
                logger.info(f"[{symbol}] TP rescatado en {tp}")
                rescatados += 1

        if rescatados > 0:
            logger.warning(f"[{symbol}] Protección incompleta ({len(exit_orders)}/2). Rescatando...")
            logger.info(f"[{symbol}] ✅ Órdenes de protección re-sincronizadas.")
            return True

        return False

    except Exception as e:
        logger.error(f"Error en verificar_y_rescatar_sl_tp: {e}")
        return False


def get_open_position(client, symbol):
    """Verifica si tenemos una posición abierta actualmente."""
    try:
        pos = client.futures_position_information(symbol=symbol)
        for p in pos:
            if p['symbol'] == symbol:
                amt = float(p['positionAmt'])
                if amt != 0:
                    return {
                        "size":  abs(amt),
                        "side":  "LONG" if amt > 0 else "SHORT",
                        "entry": float(p['entryPrice'])
                    }
        return None
    except Exception as e:
        logger.error(f"Error obteniendo posición: {e}")
        return None


def get_klines_rest(client, symbol, interval, limite=100):
    """Descarga el historial inicial de velas vía REST API para cebar el buffer del WebSocket."""
    import pandas as pd
    try:
        candles = client.futures_klines(symbol=symbol, interval=interval, limit=limite)
        df = pd.DataFrame(candles, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'ct', 'qav', 'tr', 'tba', 'tqa', 'i'
        ])
        df['open_time'] = pd.to_datetime(df['timestamp'], unit='ms')
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        logger.error(f"Error descargando klines REST: {e}")
        return None


def _round_tick(price: float, tick: float) -> float:
    """Redondea el precio al tick size más cercano hacia abajo."""
    precision = len(str(tick).rstrip('0').split('.')[-1]) if '.' in str(tick) else 0
    return round(math.floor(float(price) / tick) * tick, precision)


def get_tick_size(client, symbol) -> float:
    """Obtiene el tick size del símbolo desde Binance."""
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'PRICE_FILTER':
                        return float(f['tickSize'])
    except Exception as e:
        logger.warning(f"No se pudo obtener tick size para {symbol}: {e}")
    return 0.1  # fallback seguro para BTC


def get_step_size(client, symbol) -> float:
    """Obtiene el step size (salto de cantidad) del símbolo desde Binance."""
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        return float(f['stepSize'])
    except Exception as e:
        logger.warning(f"No se pudo obtener step size para {symbol}: {e}")
    return 0.001  # fallback por defecto
