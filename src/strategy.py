import numpy as np
import pandas as pd

def calculate_vwap_bands(df, mult=2.5):
    """
    Calcula el VWAP y sus bandas de desviación estándar.
    El DataFrame debe tener columnas: high, low, close, volume y open_time.
    """
    df = df.copy()
    # Usamos la fecha para resetear el VWAP diariamente (estilo institucional)
    df['date'] = df['open_time'].dt.date
    
    # Precio Típico
    df['typ'] = (df['high'] + df['low'] + df['close']) / 3
    df['typ_vol'] = df['typ'] * df['volume']
    
    # Acumulados diarios
    df['cum_vol'] = df.groupby('date')['volume'].cumsum()
    df['cum_typ_vol'] = df.groupby('date')['typ_vol'].cumsum()
    
    # VWAP
    df['vwap'] = df['cum_typ_vol'] / df['cum_vol']
    
    # Desviación Estándar (Varianza ponderada por volumen)
    df['dev_sq'] = df['volume'] * (df['typ'] - df['vwap'])**2
    df['cum_dev_sq'] = df.groupby('date')['dev_sq'].cumsum()
    df['std_dev'] = np.sqrt(df['cum_dev_sq'] / df['cum_vol'])
    
    # Bandas
    df['upper_band'] = df['vwap'] + (mult * df['std_dev'])
    df['lower_band'] = df['vwap'] - (mult * df['std_dev'])
    
    return df

def get_vwap_signals(df):
    """Retorna el último estado de las bandas para colocar las órdenes LIMIT"""
    last_row = df.iloc[-1]
    return {
        "vwap": round(last_row['vwap'], 4),
        "upper": round(last_row['upper_band'], 4),
        "lower": round(last_row['lower_band'], 4),
        "time": last_row['open_time']
    }