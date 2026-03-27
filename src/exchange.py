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
        client.futures_cancel_all_open_orders(symbol=symbol)
        logger.debug(f"[{BOT_ID}] Órdenes abiertas canceladas en {symbol}")
    except Exception as e:
        pass

def place_limit_order(client, symbol, side, price, quantity):
    """Coloca una orden LIMIT (Maker estricto). Si el precio ya cruzó, aborta."""
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
            timeInForce=TIME_IN_FORCE_GTX, # Post Only
            quantity=quantity,
            price=price
        )
        logger.info(f"[{BOT_ID}] ✅ Orden LIMIT {side} colocada en {price}")
        return order
        
    except Exception as e:
        error_str = str(e)
        
        # Si Binance rechaza por Post-Only, llegamos tarde al rebote.
        if "5022" in error_str or "immediately trigger" in error_str or "Post Only" in error_str:
            logger.warning(f"[{symbol}] ⏳ Oportunidad perdida: El precio ya cruzó la banda ({price}). Abortando entrada para proteger el Risk/Reward y evitar Taker Fees.")
            return None # Devolvemos None, la operación se cancela limpiamente.
            
        # Si es cualquier otro error (falta de saldo, desconexión, etc)
        logger.error(f"❌ Error colocando LIMIT: {e}")
        return None

def verificar_y_rescatar_sl_tp(client, symbol, current_trade):
    """
    Verifica si una posición abierta tiene sus órdenes de protección (SL/TP).
    Si faltan, las coloca usando los datos del trade guardado.
    """
    
    try:
        # 1. Obtener órdenes abiertas de Binance
        open_orders = client.futures_get_open_orders(symbol=symbol)
        logger.info(f"[{symbol}] Órdenes abiertas raw: {open_orders}")
        # 2. Filtramos SL y TP — incluye LIMIT reduceOnly (usado como TP)
        exit_orders = [
            o for o in open_orders
            if o['type'] in ['STOP_MARKET', 'TAKE_PROFIT_MARKET', 'TAKE_PROFIT', 'STOP']
            or (o['type'] == 'LIMIT' and (o.get('reduceOnly') == True or o.get('closePosition') == True))
        ]
        
        if len(exit_orders) < 2:
            logger.warning(f"[{symbol}] Protección incompleta ({len(exit_orders)}/2). Rescatando...")

            side_bot = current_trade.get('direction') or current_trade.get('side')
            side_entry = "BUY" if side_bot == "LONG" else "SELL"
            close_side = SIDE_SELL if side_entry == "BUY" else SIDE_BUY

            qty = float(current_trade['quantity'])
            sl = float(current_trade['sl_price'])
            tp = float(current_trade['tp_price'])

            tick = get_tick_size(client, symbol)
            sl = _round_tick(sl, tick)
            tp = _round_tick(tp, tick)

            # Identificar qué órdenes faltan por tipo
            tipos_presentes = {o['type'] for o in exit_orders}
            tiene_sl = any(
                o['type'] in {'STOP_MARKET', 'STOP'} and (o.get('reduceOnly') == True or o.get('closePosition') == True)
                for o in open_orders
            )
            tiene_tp = bool({'TAKE_PROFIT_MARKET', 'TAKE_PROFIT'} & tipos_presentes) \
                       or any(o['type'] == 'LIMIT' and o.get('reduceOnly') == True for o in exit_orders)

            # Colocar solo lo que falta, sin cancelar lo que ya existe
            if not tiene_sl:
                try:
                    client.futures_create_order(
                        symbol=symbol,
                        side=close_side,
                        type=FUTURE_ORDER_TYPE_STOP_MARKET,
                        stopPrice=sl,
                        quantity=qty,
                        reduceOnly=True
                    )
                    logger.info(f"[{symbol}] SL rescatado en {sl}")
                except Exception as e:
                    logger.error(f"[{symbol}] Error rescatando SL: {e}")

            if not tiene_tp:
                try:
                    client.futures_create_order(
                        symbol=symbol,
                        side=close_side,
                        type=FUTURE_ORDER_TYPE_LIMIT,
                        timeInForce=TIME_IN_FORCE_GTC,
                        price=tp,
                        quantity=qty,
                        reduceOnly=True
                    )
                    logger.info(f"[{symbol}] TP rescatado en {tp}")
                except Exception as e:
                    logger.error(f"[{symbol}] Error rescatando TP: {e}")

            logger.info(f"[{symbol}] ✅ Órdenes de protección re-sincronizadas.")
            return True
            
        return False 
    except Exception as e:
        logger.error(f"Error en verificar_y_rescatar_sl_tp: {e}")
        return False
    
def _round_tick(price: float, tick: float) -> float:
    """Redondea el precio al tick size más cercano hacia abajo."""
    import math
    # Usamos math para evitar errores de punto flotante
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

def place_sl_tp(client, symbol, side, qty, sl_price, tp_price):
    """Coloca las órdenes de protección una vez entramos al trade."""
    try:
        tick = get_tick_size(client, symbol)
        close_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
        sl_price = _round_tick(float(sl_price), tick)
        tp_price = _round_tick(float(tp_price), tick)

        # --- LIMPIEZA SELECTIVA: solo cancela órdenes de salida existentes ---
        # No cancelamos todo para no borrar órdenes de otro bot en la misma cuenta
        open_orders = client.futures_get_open_orders(symbol=symbol)
        exit_types = {'STOP_MARKET', 'TAKE_PROFIT_MARKET', 'TAKE_PROFIT', 'STOP'}
        for o in open_orders:
            if o['type'] in exit_types:
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=o['orderId'])
                except Exception:
                    pass  # Si ya se ejecutó, ignoramos

        # Stop Loss (Market)
        client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=sl_price,
            quantity=qty,
            reduceOnly=True
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
        logger.info(f"[{BOT_ID}] ✅ Protección colocada: SL {sl_price} | TP {tp_price} (tick {tick})")
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

def get_klines_rest(client, symbol, interval, limite=100):
    """
    Descarga el historial inicial de velas vía REST API 
    para 'cebar' el buffer del WebSocket.
    """
    import pandas as pd
    
    try:
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=limite)
        
        # Binance devuelve una lista de listas, la convertimos a DataFrame
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
        ])
        
        # Formateamos el tiempo para que el WebSocket lo entienda
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        # Convertimos los textos a números
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
            
        return df
    except Exception as e:
        from src.logger import logger
        logger.error(f"Error descargando historial REST: {e}")
        return None