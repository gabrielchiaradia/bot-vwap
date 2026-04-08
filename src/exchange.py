# src/exchange.py
import time
import pandas as pd
from binance.client import Client
from binance.enums import *
from src.config import BINANCE_API_KEY, BINANCE_API_SECRET, IS_TESTNET, LEVERAGE, BOT_ID
from src.logger import logger

def get_client():
    return Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=IS_TESTNET)

def set_leverage(client, symbol):
    try:
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        logger.info(f"  [{BOT_ID}] Apalancamiento configurado a {LEVERAGE}x")
        try:
            client.futures_change_margin_type(symbol=symbol, marginType='ISOLATED')
            logger.info(f"  [{BOT_ID}] Margen configurado: ISOLATED")
        except Exception as e:
            if "No need to change margin type" in str(e) or "-4046" in str(e):
                logger.info(f"  [{BOT_ID}] El margen ya era ISOLATED. Continuando...")
            else:
                raise e
    except Exception as e:
        logger.error(f"Error crítico configurando apalancamiento/margen: {e}")

def get_account_status(client):
    try:
        acc = client.futures_account()
        wallet    = float(acc.get('totalWalletBalance', 0.0))
        pnl       = float(acc.get('totalUnrealizedProfit', 0.0))
        margin    = float(acc.get('totalMarginBalance', 0.0))
        available = float(acc.get('availableBalance', 0.0))
        return {
            "wallet_balance": wallet,
            "unrealized_pnl": pnl,
            "margin_balance": margin,
            "available": available
        }
    except Exception as e:
        logger.error(f"Error crítico obteniendo cuenta: {e}")
        return {
            "wallet_balance": 0.0,
            "unrealized_pnl": 0.0,
            "margin_balance": 0.0,
            "available": 0.0
        }

def cancel_all_open_orders(client, symbol):
    """Cancela todas las órdenes LIMIT abiertas para un símbolo."""
    try:
        client.futures_cancel_all_open_orders(symbol=symbol)
        logger.debug(f"[{BOT_ID}] Órdenes abiertas canceladas en {symbol}")
    except Exception as e:
        pass

def place_limit_order(client, symbol, side, price, quantity):
    """Coloca una orden LIMIT (Maker estricto). Si el precio ya cruzó, aborta."""
    try:
        tick     = get_tick_size(client, symbol)
        price    = _round_tick(float(price), tick)
        step     = get_step_size(client, symbol)
        quantity = _round_tick(float(quantity), step)

        position_side = "LONG" if side == SIDE_BUY else "SHORT"
        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            positionSide=position_side,
            type=FUTURE_ORDER_TYPE_LIMIT,
            timeInForce=TIME_IN_FORCE_GTX,
            quantity=quantity,
            price=price
        )

        logger.info(f"[{BOT_ID}] ✅ Orden LIMIT {side} colocada en {price}")
        return order

    except Exception as e:
        error_str = str(e)
        if "5022" in error_str or "immediately trigger" in error_str or "Post Only" in error_str:
            logger.warning(
                f"[{symbol}] ⏳ Oportunidad perdida: El precio ya cruzó la banda ({price}). "
                f"Abortando entrada para proteger el Risk/Reward y evitar Taker Fees."
            )
            return None
        logger.error(f"❌ Error colocando LIMIT: {e}")
        return None

def place_market_order(client, symbol, side, quantity):
    """
    Coloca una orden MARKET para cerrar una posición por timeout.
    side: 'BUY' o 'SELL' (lado de cierre, opuesto a la dirección del trade)
    """
    try:
        step     = get_step_size(client, symbol)
        quantity = _round_tick(float(quantity), step)

        # En Hedge Mode, el positionSide es el lado de la posición que queremos cerrar
        # Si cerramos con SELL → estamos cerrando un LONG
        # Si cerramos con BUY  → estamos cerrando un SHORT
        position_side = "LONG" if side == SIDE_SELL else "SHORT"

        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            positionSide=position_side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=quantity,
        )

        logger.info(f"[{BOT_ID}] ✅ Orden MARKET {side} colocada (timeout) qty={quantity}")
        return order

    except Exception as e:
        logger.error(f"[{BOT_ID}] ❌ Error colocando MARKET order: {e}")
        return None

def verificar_y_rescatar_sl_tp(client, symbol, current_trade):
    """
    Verifica si una posición abierta tiene sus órdenes de protección (SL/TP).
    Si faltan, las coloca usando los datos del trade guardado.
    """
    try:
        open_orders = client.futures_get_open_orders(symbol=symbol)

        exit_orders = [
            o for o in open_orders
            if o['type'] in ['STOP_MARKET', 'TAKE_PROFIT_MARKET', 'TAKE_PROFIT', 'STOP']
            or (o.get('closePosition') == True)
        ]

        solo_tp_presente = (
            len(exit_orders) == 1 and
            exit_orders[0]['type'] == 'TAKE_PROFIT_MARKET' and
            exit_orders[0].get('closePosition') == True
        )
        if solo_tp_presente:
            logger.debug(f"[{symbol}] SL condicional detectado. Proteccion completa.")

    except Exception as e:
        logger.error(f"Error en verificar_y_rescatar_sl_tp: {e}")

def _round_tick(value: float, tick: float) -> float:
    """Redondea un valor al tick/step size más cercano."""
    if tick <= 0:
        return value
    precision = len(str(tick).rstrip('0').split('.')[-1]) if '.' in str(tick) else 0
    return round(round(value / tick) * tick, precision)

def get_tick_size(client, symbol) -> float:
    """Obtiene el tick size (precisión de precio) para un símbolo de futuros."""
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'PRICE_FILTER':
                        return float(f['tickSize'])
    except Exception as e:
        logger.warning(f"No se pudo obtener tick size para {symbol}: {e}")
    return 0.01  # fallback

def get_step_size(client, symbol) -> float:
    """Obtiene el step size (precisión de cantidad) para un símbolo de futuros."""
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        return float(f['stepSize'])
    except Exception as e:
        logger.warning(f"No se pudo obtener step size para {symbol}: {e}")
    return 0.001  # fallback

def place_sl_tp(client, symbol, side, qty, sl_price, tp_price):
    """Coloca las órdenes de protección una vez entramos al trade."""
    try:
        tick       = get_tick_size(client, symbol)
        close_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
        sl_price   = _round_tick(float(sl_price), tick)
        tp_price   = _round_tick(float(tp_price), tick)

        open_orders = client.futures_get_open_orders(symbol=symbol)
        exit_types  = {'STOP_MARKET', 'TAKE_PROFIT_MARKET', 'TAKE_PROFIT', 'STOP'}
        for o in open_orders:
            if o['type'] in exit_types:
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=o['orderId'])
                except Exception:
                    pass

        position_side = "LONG" if side == SIDE_BUY else "SHORT"
        client.futures_create_order(
            symbol=symbol,
            side=close_side,
            positionSide=position_side,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=sl_price,
            closePosition=True
        )
        client.futures_create_order(
            symbol=symbol,
            side=close_side,
            positionSide=position_side,
            type=FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
            stopPrice=tp_price,
            closePosition=True
        )
        logger.info(f"[{BOT_ID}] ✅ Protección colocada: SL {sl_price} | TP {tp_price} (tick {tick})")
    except Exception as e:
        logger.error(f"Error colocando SL/TP: {e}")

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

def close_market_position(client, symbol):
    """
    Cierra la posición abierta a mercado (MARKET reduceOnly).
    Cancela SL/TP pendientes antes de cerrar para evitar órdenes huérfanas.
    Usado por el filtro de noticias para cerrar posiciones en profit o hasta -1%.
    """
    try:
        # 1. Obtener posición actual
        pos_info = client.futures_position_information(symbol=symbol)
        amt  = 0.0
        side = None
        for p in pos_info:
            if p['symbol'] == symbol:
                amt = float(p['positionAmt'])
                if amt != 0:
                    side = "LONG" if amt > 0 else "SHORT"
                    break

        if amt == 0 or side is None:
            logger.info(f"[{BOT_ID}] close_market_position: no hay posición abierta en {symbol}.")
            return False

        # 2. Cancelar SL/TP pendientes para evitar órdenes huérfanas
        try:
            open_orders = client.futures_get_open_orders(symbol=symbol)
            exit_types  = {'STOP_MARKET', 'TAKE_PROFIT_MARKET', 'TAKE_PROFIT', 'STOP'}
            for o in open_orders:
                if o['type'] in exit_types or o.get('closePosition'):
                    client.futures_cancel_order(symbol=symbol, orderId=o['orderId'])
            logger.info(f"[{BOT_ID}] Órdenes SL/TP canceladas antes de cierre por noticias.")
        except Exception as e:
            logger.warning(f"[{BOT_ID}] Error cancelando SL/TP pre-cierre: {e}")

        # 3. Cerrar a mercado
        close_side    = SIDE_SELL if side == "LONG" else SIDE_BUY
        position_side = side  # "LONG" o "SHORT"
        qty  = abs(amt)
        step = get_step_size(client, symbol)
        qty  = _round_tick(qty, step)
        client.futures_create_order(
            symbol=symbol,
            side=close_side,
            positionSide=position_side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=qty,
        )
        logger.info(
            f"[{BOT_ID}] ✅ Posición {side} cerrada a mercado por filtro de noticias "
            f"({symbol}, qty={qty})"
        )
        return True

    except Exception as e:
        logger.error(f"[{BOT_ID}] Error en close_market_position: {e}")
        return False

def get_klines_rest(client, symbol, interval, limite=100):
    """
    Descarga el historial inicial de velas vía REST API
    para 'cebar' el buffer del WebSocket.
    """
    try:
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=limite)

        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
        ])

        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)

        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)

        return df
    except Exception as e:
        logger.error(f"Error descargando historial REST: {e}")
        return None
