import numpy as np
import pandas as pd
from datetime import datetime, timezone
from src.config import SYMBOL, BAND_MULT, TRADING_DAYS
from src.logger import logger


def obtener_señal_actual(client):
    """
    Centraliza la descarga de datos, el cálculo de bandas y la generación de señal.
    Retorna: (signal, entry_price, tp_vwap) o (None, None, None) si falla.
    NOTA: Solo se usa en modo polling. En modo WebSocket usar
          actualizar_bandas() + evaluar_precio_intra_vela().
    """
    try:
        candles = client.futures_klines(symbol=SYMBOL, interval='1m', limit=1500)
        df = pd.DataFrame(candles, columns=['timestamp','open','high','low','close','volume','ct','qav','tr','tba','tqa','i'])
        df['open_time'] = pd.to_datetime(df['timestamp'], unit='ms')
        cols = ['open','high','low','close','volume']
        df[cols] = df[cols].astype(float)
        df_bands = calculate_vwap_bands(df, mult=BAND_MULT)
        signal, entry_price, tp_vwap = get_vwap_signals(df_bands)
        return signal, entry_price, tp_vwap
    except Exception as e:
        logger.error(f"Error procesando estrategia: {e}")
        return None, None, None


def calculate_vwap_bands(df, mult=2.5):
    """
    Cálculo de VWAP Institucional Anclado (Daily Reset)
    con Desviación Estándar Ponderada por Volumen.
    """
    df = df.copy()
    df['date'] = pd.to_datetime(df['open_time'], utc=True).dt.date
    df['typ'] = (df['high'] + df['low'] + df['close']) / 3
    df['typ_vol'] = df['typ'] * df['volume']
    df['cum_vol'] = df.groupby('date')['volume'].cumsum()
    df['cum_typ_vol'] = df.groupby('date')['typ_vol'].cumsum()
    df['vwap'] = df['cum_typ_vol'] / df['cum_vol']
    df['dev_sq'] = df['volume'] * (df['typ'] - df['vwap'])**2
    df['cum_dev_sq'] = df.groupby('date')['dev_sq'].cumsum()
    df['std_dev'] = np.sqrt(df['cum_dev_sq'] / df['cum_vol'])
    df['upper'] = df['vwap'] + (df['std_dev'] * mult)
    df['lower'] = df['vwap'] - (df['std_dev'] * mult)
    df['band_width'] = df['upper'] - df['lower']
    df['avg_band_width'] = df['band_width'].rolling(window=20).mean()
    df['bar_num'] = df.groupby('date').cumcount()
    return df


def actualizar_bandas(df_velas: pd.DataFrame) -> dict | None:
    """
    Llamado al cierre de cada vela. Calcula las bandas sobre velas cerradas
    y retorna un dict con los niveles clave para usar intra-vela.
    Retorna None si no hay suficientes datos o los filtros no pasan.
    """
    try:
        # Reconstruir open_time desde el índice si es necesario
        df = df_velas.copy()
        if 'open_time' not in df.columns:
            df['open_time'] = pd.to_datetime(df.index, utc=True)
        else:
            df['open_time'] = pd.to_datetime(df['open_time'], utc=True)

        df_bands = calculate_vwap_bands(df, mult=BAND_MULT)
        last = df_bands.iloc[-1]
        
        # Debug temporal: ver cuántas velas del día actual hay
        fecha_hoy = df_bands['open_time'].dt.date.iloc[-1]
        velas_hoy = (df_bands['open_time'].dt.date == fecha_hoy).sum()
        logger.info("Velas de hoy (%s): %d | bar_num último: %d",
                    fecha_hoy, velas_hoy, int(last['bar_num']))

        # Filtro de inicio de sesión
        if last['bar_num'] < 120:
            return None

        # Filtro de día de semana
        if TRADING_DAYS == 'WORKDAYS' and pd.Timestamp(last['open_time']).weekday() >= 5:
            return None

        # Filtro anti-latigazo VWAP
        vwap_now  = last['vwap']
        vwap_prev = df_bands['vwap'].iloc[-4]
        vwap_change = abs((vwap_now - vwap_prev) / vwap_prev)
        if vwap_change > 0.005:
            logger.debug("Bandas no actualizadas: salto VWAP (%.4f)", vwap_change)
            return None

        # Filtro anti-expansión de bandas
        if not pd.isna(last['avg_band_width']):
            if last['band_width'] > last['avg_band_width'] * 2.0:
                logger.debug("Bandas no actualizadas: expansión violenta")
                return None

        bandas = {
            "upper":     float(last['upper']),
            "lower":     float(last['lower']),
            "vwap":      float(last['vwap']),
            "bar_num":   int(last['bar_num']),
            "updated_at": datetime.now(timezone.utc),
        }
        logger.debug("Bandas actualizadas | Upper: %.2f | Lower: %.2f | VWAP: %.2f",
                     bandas["upper"], bandas["lower"], bandas["vwap"])
        return bandas

    except Exception as e:
        logger.error("Error en actualizar_bandas: %s", e)
        return None


def evaluar_precio_intra_vela(mark_price: float, bandas: dict) -> tuple:
    """
    Llamado en cada tick de mark price. Evalúa si el precio actual
    toca una banda y genera señal de entrada inmediata.

    Retorna: (signal, entry_price, tp_vwap) o (None, None, None)

    A diferencia de get_vwap_signals(), NO usa high/low de la vela cerrada
    sino el precio en tiempo real — esto replica la lógica del backtest
    donde la entrada ocurre al momento del toque, no al cierre.
    """
    if not bandas:
        return None, None, None

    upper = bandas["upper"]
    lower = bandas["lower"]
    vwap  = bandas["vwap"]

    # SHORT: precio toca o supera banda superior
    if mark_price >= upper:
        entry_price = upper
        tp = vwap
        reward = abs(entry_price - tp)
        profit_pct = (reward / entry_price) * 100
        if profit_pct > 0.15:
            # Verificar que el precio no se alejó demasiado de la banda
            # (evita entrar cuando ya rebotó mucho)
            if mark_price <= upper * 1.002:
                return "SHORT", entry_price, tp
            else:
                logger.debug("SHORT bloqueado: precio muy lejos de banda (%.4f > upper*1.002)", mark_price)
        else:
            logger.debug("SHORT bloqueado: premio muy chico (%.3f%%)", profit_pct)

    # LONG: precio toca o cae bajo banda inferior
    elif mark_price <= lower:
        entry_price = lower
        tp = vwap
        reward = abs(tp - entry_price)
        profit_pct = (reward / entry_price) * 100
        if profit_pct > 0.15:
            # Verificar que el precio no se alejó demasiado
            if mark_price >= lower * 0.998:
                return "LONG", entry_price, tp
            else:
                logger.debug("LONG bloqueado: precio muy lejos de banda (%.4f < lower*0.998)", mark_price)
        else:
            logger.debug("LONG bloqueado: premio muy chico (%.3f%%)", profit_pct)

    return None, None, None


def actualizar_bandas_cross(df_velas: pd.DataFrame) -> dict | None:
    """
    Igual que actualizar_bandas() pero guarda también el close anterior
    para detectar el cruce del VWAP en el siguiente tick.
    Retorna None si no hay suficientes datos o los filtros no pasan.
    """
    try:
        from src.config import TRADING_WINDOW
        df = df_velas.copy()
        if 'open_time' not in df.columns:
            df['open_time'] = pd.to_datetime(df.index, utc=True)
        else:
            df['open_time'] = pd.to_datetime(df['open_time'], utc=True)

        df_bands = calculate_vwap_bands(df, mult=BAND_MULT)
        last = df_bands.iloc[-1]
        prev = df_bands.iloc[-2]

        fecha_hoy = df_bands['open_time'].dt.date.iloc[-1]
        velas_hoy = (df_bands['open_time'].dt.date == fecha_hoy).sum()
        logger.info("Velas de hoy (%s): %d | bar_num último: %d",
                    fecha_hoy, velas_hoy, int(last['bar_num']))

        # Filtro de inicio de sesión — primeras 5 velas del TF
        if last['bar_num'] < 5:
            return None

        # Filtro de día de semana
        if TRADING_DAYS == 'WORKDAYS' and pd.Timestamp(last['open_time']).weekday() >= 5:
            return None

        # Filtro de ventana horaria
        hora_utc = pd.Timestamp(last['open_time']).hour
        if TRADING_WINDOW and TRADING_WINDOW != "0-24":
            partes = TRADING_WINDOW.split("-")
            h_ini, h_fin = int(partes[0]), int(partes[1])
            if not (h_ini <= hora_utc < h_fin):
                return None

        bandas = {
            "upper":      float(last['upper']),
            "lower":      float(last['lower']),
            "vwap":       float(last['vwap']),
            "vwap_prev":  float(prev['vwap']),
            "close_prev": float(prev['close']) if 'close' in prev else float(last['close']),
            "bar_num":    int(last['bar_num']),
            "updated_at": datetime.now(timezone.utc),
        }
        logger.info("Bandas Cross | Upper: %.2f | Lower: %.2f | VWAP: %.2f",
                    bandas["upper"], bandas["lower"], bandas["vwap"])
        return bandas

    except Exception as e:
        logger.error("Error en actualizar_bandas_cross: %s", e)
        return None


def evaluar_cruce_vwap(mark_price: float, bandas: dict, precio_anterior: float) -> tuple:
    """
    Detecta cruce del VWAP en tiempo real comparando el precio anterior
    con el precio actual.

    LONG:  precio_anterior < vwap y mark_price >= vwap
    SHORT: precio_anterior > vwap y mark_price <= vwap

    TP = banda opuesta, SL = banda del mismo lado.
    Retorna: (signal, entry_price, tp_price, sl_price) o (None, None, None, None)
    """
    if not bandas or precio_anterior <= 0:
        return None, None, None, None

    upper = bandas["upper"]
    lower = bandas["lower"]
    vwap  = bandas["vwap"]

    # LONG: cruce hacia arriba
    if precio_anterior < vwap and mark_price >= vwap:
        entry = vwap
        tp    = upper
        sl    = lower
        reward = abs(tp - entry)
        if reward / entry > 0.001:  # mínimo 0.1% de reward
            return "LONG", entry, tp, sl

    # SHORT: cruce hacia abajo
    elif precio_anterior > vwap and mark_price <= vwap:
        entry = vwap
        tp    = lower
        sl    = upper
        reward = abs(entry - tp)
        if reward / entry > 0.001:
            return "SHORT", entry, tp, sl

    return None, None, None, None


def _cooldown_activo() -> bool:
    """Verifica si el cooldown post-trade está activo."""
    try:
        from src.journal import _load
        from src.config import BOT_ID
        trades = _load()
        mis_trades = [t for t in trades if t.get('bot_id') == BOT_ID and t.get('status') == 'CLOSED']
        if mis_trades:
            ultimo_trade = mis_trades[-1]
            if ultimo_trade.get('close_time'):
                last_time = datetime.fromisoformat(ultimo_trade['close_time'])
                now = datetime.now(timezone.utc)
                minutos_pasados = (now - last_time).total_seconds() / 60.0
                if minutos_pasados < 10:
                    logger.debug("Cooldown activo. Faltan %.1f minutos.", 10 - minutos_pasados)
                    return True
    except Exception:
        pass
    return False


def get_vwap_signals(df):
    """
    Evalúa la última vela cerrada replicando la lógica EXACTA del backtest.
    Mantenida para compatibilidad con modo polling.
    En modo WebSocket usar evaluar_precio_intra_vela() en su lugar.
    """
    if len(df) < 20:
        return None, None, None

    last = df.iloc[-1]

    if last['bar_num'] < 120:
        return None, None, None

    if TRADING_DAYS == 'WORKDAYS' and last['open_time'].weekday() >= 5:
        return None, None, None

    # Cooldown
    if _cooldown_activo():
        return None, None, None

    # Anti-latigazos
    vwap_now  = last['vwap']
    vwap_prev = df['vwap'].iloc[-4]
    vwap_change = abs((vwap_now - vwap_prev) / vwap_prev)
    if vwap_change > 0.005:
        print(f"Bloqueo: Salto gigante de VWAP detectado ({vwap_change:.4f})")
        return None, None, None

    if not pd.isna(last['avg_band_width']):
        if last['band_width'] > (last['avg_band_width'] * 2.0):
            print(f"Bloqueo: Expansión violenta de bandas (Ancho: {last['band_width']:.2f})")
            return None, None, None

    tp = last['vwap']

    if last['high'] >= last['upper'] and last['close'] < (last['upper'] * 1.002):
        entry_price = last['upper']
        reward = abs(entry_price - tp)
        profit_pct = (reward / entry_price) * 100
        if profit_pct > 0.15:
            return "SHORT", entry_price, tp
        else:
            print(f"Bloqueo SHORT: Premio muy chico ({profit_pct:.3f}%)")

    if last['low'] <= last['lower'] and last['close'] > (last['lower'] * 0.998):
        entry_price = last['lower']
        reward = abs(tp - entry_price)
        profit_pct = (reward / entry_price) * 100
        if profit_pct > 0.15:
            return "LONG", entry_price, tp
        else:
            print(f"Bloqueo LONG: Premio muy chico ({profit_pct:.3f}%)")

    return None, None, None
