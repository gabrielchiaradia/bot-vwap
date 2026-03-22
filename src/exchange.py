import time
from binance.client import Client
from binance.enums import *
from src.config import BINANCE_API_KEY, BINANCE_API_SECRET, IS_TESTNET, LEVERAGE, BOT_ID
from src.logger import logger

def get_client():
    return Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=IS_TESTNET)

def set_leverage(client, symbol):
    try:
        # 1. Intentar cambiar el apalancamiento
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        logger.info(f"  [{BOT_ID}] Apalancamiento configurado a {LEVERAGE}x")
        # 2. Intentar cambiar el tipo de margen
        try:
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
        
        # .get('llave', valor_por_defecto) evita que el bot se detenga si la llave falta
        wallet = float(acc.get('totalWalletBalance', 0.0))
        pnl = float(acc.get('totalUnrealizedProfit', 0.0))
        margin = float(acc.get('totalMarginBalance', 0.0))
        available = float(acc.get('availableBalance', 0.0))
        
        return {
            "wallet_balance": wallet,
            "unrealized_pnl": pnl,
            "margin_balance": margin,
            "available": available
        }
    except Exception as e:
        logger.error(f"Error crítico obteniendo cuenta: {e}")
        # Retornamos ceros para que el resto del bot no explote
        return {
            "wallet_balance": 0.0,
            "unrealized_pnl": 0.0,
            "margin_balance": 0.0,
            "available": 0.0
        }
    
def cancel_all_open_orders(client, symbol):
    """Cancela todas las órdenes LIMIT abiertas para un símbolo"""
    try:
        orders = client.futures_get_open_orders(symbol=symbol)
        if orders:
            client.futures_cancel_all_open_orders(symbol=symbol)
            logger.debug(f"[{BOT_ID}] Órdenes abiertas canceladas en {symbol}")
    except Exception as e:
        logger.error(f"Error cancelando órdenes: {e}")

def place_limit_order(client, symbol, side, price, quantity):
    """Coloca una orden LIMIT (Maker)"""
    try:
        price = round(float(price), 2) 
        quantity = round(float(quantity), 3)
        # Usamos Post Only para asegurar que siempre seamos MAKER (menos fees)
        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type=FUTURE_ORDER_TYPE_LIMIT,
            timeInForce=TIME_IN_FORCE_GTX, # Post Only
            quantity=quantity,
            price=price
        )
        logger.info(f"[{BOT_ID}] Orden LIMIT {side} colocada en {price}")
        return order
    except Exception as e:
        # Buscamos el error -5022 (Post-Only rejected) o el mensaje clásico
        error_str = str(e)
        if "5022" in error_str or "immediately trigger" in error_str or "Post Only" in error_str:
            logger.warning(f"[{symbol}] Precio cruzó la banda (Post-Only rechazado). Entrando por MARKET...")
            try:
                return client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type=FUTURE_ORDER_TYPE_MARKET,
                    quantity=quantity # Asegurate que la variable se llame qty o quantity según tu código
                )
            except Exception as market_e:
                logger.error(f"Error en entrada Market de respaldo: {market_e}")
                return None
        
        logger.error(f"Error colocando LIMIT: {e}")
        return None

def place_sl_tp(client, symbol, side, qty, sl_price, tp_price):
    """Coloca las órdenes de protección una vez entramos al trade"""
    try:
        # El lado de cierre es el opuesto al de entrada
        close_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
        sl_price = round(float(sl_price), 2)
        tp_price = round(float(tp_price), 2)
        
        # Stop Loss (Market)
        client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=sl_price,
            closePosition=True
        )
        
        # Take Profit (Limit)
        client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type=FUTURE_ORDER_TYPE_LIMIT,
            timeInForce=TIME_IN_FORCE_GTC,
            price=tp_price,
            quantity=qty,
            reduceOnly=True
        )
        logger.info(f"[{BOT_ID}] Protección colocada: SL {sl_price} | TP {tp_price}")
    except Exception as e:
        logger.error(f"Error colocando SL/TP: {e}")

def get_open_position(client, symbol):
    """Verifica si tenemos una posición abierta actualmente"""
    try:
        pos = client.futures_position_information(symbol=symbol)
        for p in pos:
            if p['symbol'] == symbol:
                amt = float(p['positionAmt'])
                if amt != 0:
                    return {
                        "size": abs(amt),
                        "side": "LONG" if amt > 0 else "SHORT",
                        "entry": float(p['entryPrice'])
                    }
        return None
    except Exception as e:
        logger.error(f"Error obteniendo posición: {e}")
        return None