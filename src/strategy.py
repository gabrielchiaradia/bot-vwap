import numpy as np
import pandas as pd

def calculate_vwap_bands(df, mult=2.5):
    """
    Cálculo de VWAP Institucional Anclado (Daily Reset)
    con Desviación Estándar Ponderada por Volumen.
    """
    df = df.copy()
    
    # 1. Anclaje: Resetear el cálculo cada día a las 00:00 UTC
    df['date'] = df['open_time'].dt.date
    
    # 2. Precio Típico
    df['typ'] = (df['high'] + df['low'] + df['close']) / 3
    df['typ_vol'] = df['typ'] * df['volume']
    
    # 3. VWAP (Acumulado diario)
    df['cum_vol'] = df.groupby('date')['volume'].cumsum()
    df['cum_typ_vol'] = df.groupby('date')['typ_vol'].cumsum()
    df['vwap'] = df['cum_typ_vol'] / df['cum_vol']
    
    # 4. Desviación Estándar Ponderada (Bands reales)
    df['dev_sq'] = df['volume'] * (df['typ'] - df['vwap'])**2
    df['cum_dev_sq'] = df.groupby('date')['dev_sq'].cumsum()
    df['std_dev'] = np.sqrt(df['cum_dev_sq'] / df['cum_vol'])
    
    # 5. Cálculo de Bandas
    df['upper'] = df['vwap'] + (df['std_dev'] * mult)
    df['lower'] = df['vwap'] - (df['std_dev'] * mult)
    
    # Filtro EMA 200 (Es clave mantenerlo para no operar contra micro-tendencias fuertes)
    df['ema_200'] = df['close'].ewm(span=200).mean()
    
    return df

def get_vwap_signals(df):
    """
    Evalúa las últimas dos velas cerradas para generar la señal.
    """
    # Necesitamos al menos 2 velas para comparar
    if len(df) < 2: 
        return None, None, None
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    # Lógica SHORT: 
    # - Vela anterior cerró/tocó por encima de la banda superior
    # - Vela actual cierra por debajo de la banda superior (reversión hacia adentro)
    # - El precio está POR DEBAJO de la EMA 200 (Tendencia bajista general)
    if prev['close'] > prev['upper'] and last['close'] < last['upper'] and last['close'] < last['ema_200']:
        return "SHORT", last['close'], last['upper']
        
    # Lógica LONG:
    # - Vela anterior cerró/tocó por debajo de la banda inferior
    # - Vela actual cierra por encima de la banda inferior (reversión hacia adentro)
    # - El precio está POR ENCIMA de la EMA 200 (Tendencia alcista general)
    if prev['close'] < prev['lower'] and last['close'] > last['lower'] and last['close'] > last['ema_200']:
        return "LONG", last['close'], last['lower']
        
    return None, None, None