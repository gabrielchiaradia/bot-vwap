import numpy as np
import pandas as pd
from src.config import SYMBOL, BAND_MULT
from src.logger import logger

def obtener_señal_actual(client):
    """
    Centraliza la descarga de datos, el cálculo de bandas y la generación de señal.
    Retorna: (signal, entry_price, std_dev) o (None, None, None) si falla.
    """
    try:
        # 1. Obtención de datos (Lo que antes estaba en el main)
        candles = client.futures_klines(symbol=SYMBOL, interval='1m', limit=1500)
        df = pd.DataFrame(candles, columns=['timestamp','open','high','low','close','volume','ct','qav','tr','tba','tqa','i'])
        df['open_time'] = pd.to_datetime(df['timestamp'], unit='ms')
        cols = ['open','high','low','close','volume']
        df[cols] = df[cols].astype(float)

        # 2. Cálculo de Bandas (Tus funciones existentes)
        df_bands = calculate_vwap_bands(df, mult=BAND_MULT)
        
        # 3. Generar Señales
        signal, entry_price, _ = get_vwap_signals(df_bands)
        
        # Necesitamos la std_dev para el SL dinámico que tenés en el main
        last_row = df_bands.iloc[-1]
        std_dev = last_row['std_dev']

        return signal, entry_price, std_dev

    except Exception as e:
        logger.error(f"Error procesando estrategia: {e}")
        return None, None, None

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
    
    # 6. Cálculos para Filtros de Volatilidad 
    df['band_width'] = df['upper'] - df['lower']
    # Promedio del ancho de banda de las últimas 20 velas
    df['avg_band_width'] = df['band_width'].rolling(window=20).mean()
        
    # 6. Contador de velas para el filtro de inicio de sesión
    df['bar_num'] = df.groupby('date').cumcount()
    
    return df

def get_vwap_signals(df):
    """
    Evalúa la última vela cerrada replicando la lógica EXACTA del backtest:
    Entrada al tocar la banda (High/Low) buscando reversión al VWAP.
    """
    # Necesitamos datos suficientes
    if len(df) < 20: 
        return None, None, None
    
    # Evaluamos la última vela que acaba de cerrar
    last = df.iloc[-1]
					  
    # FILTRO DE INICIO DE SESIÓN: 
    # Ignorar las primeras 120 velas del día porque las bandas están colapsadas.
    if last['bar_num'] < 120:
        return None, None, None
    # FILTROS ANTI-LATIGAZOS (VOLATILIDAD
    # 1. Cálculo de salto de VWAP (comparamos la vela actual con la de hace 3 periodos)
    vwap_now = last['vwap']
    vwap_prev = df['vwap'].iloc[-4] # -4 nos da la vela de hace 3 cierres
    vwap_change = abs((vwap_now - vwap_prev) / vwap_prev)
    
    # Parámetros (Podés ajustarlos según backtest)
    LIMITE_SALTO_VWAP = 0.005 # 0.5% de cambio en 3 velas (latigazo)
    LIMITE_EXPANSION = 2.0    # 2.0x (El doble de ancho que el promedio normal)
    
    # Verificación 1: ¿El VWAP se inclinó violentamente?
    if vwap_change > LIMITE_SALTO_VWAP:
        print(f"Bloqueo: Salto gigante de VWAP detectado ({vwap_change:.4f})")
        return None, None, None

    # Verificación 2: ¿Las bandas se abrieron como boca de cocodrilo?
    if not pd.isna(last['avg_band_width']):
        if last['band_width'] > (last['avg_band_width'] * LIMITE_EXPANSION):
            print(f"Bloqueo: Expansión violenta de bandas (Ancho: {last['band_width']:.2f})")
            return None, None, None
    
    # Lógica SHORT (Clon del backtest): 
    # El máximo (high) tocó o superó la banda superior y el cierre no se escapó demasiado.
    if last['high'] >= last['upper'] and last['close'] < (last['upper'] * 1.002):
        # Retorna: Dirección, Precio de Entrada (Cierre de la vela), y el VWAP (que es tu Take Profit en el backtest)
																										   
        return "SHORT", last['close'], last['vwap']
        
    # Lógica LONG (Clon del backtest):
    # El mínimo (low) tocó o perforó la banda inferior y el cierre no se hundió demasiado.
    if last['low'] <= last['lower'] and last['close'] > (last['lower'] * 0.998):
        return "LONG", last['close'], last['vwap']
    
    return None, None, None
        
  
    