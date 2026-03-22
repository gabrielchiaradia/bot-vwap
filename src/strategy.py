import numpy as np
import pandas as pd

def calculate_vwap_bands(df, mult=2.5):
    """
    Cálculo de VWAP Institucional Anclado (Daily Reset)
    con Desviación Estándar Ponderada por Volumen.
    
    ATENCIÓN: Para que las bandas coincidan con el backtest y TradingView,
    el DataFrame DEBE contener la vela de las 00:00 UTC del día en curso.
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
    
    # ELIMINADO: Filtro EMA 200. Ahora es pura reversión a la media.
	df['bar_num'] = df.groupby('date').cumcount()												
    
    return df

def get_vwap_signals(df):
    """
    Evalúa la última vela cerrada replicando la lógica EXACTA del backtest:
    Entrada al tocar la banda (High/Low) buscando reversión al VWAP.
    """
    # Necesitamos datos suficientes
    if len(df) < 2: 
        return None, None, None
    
    # Evaluamos la última vela que acaba de cerrar
    last = df.iloc[-1]
					  
    # FILTRO DE INICIO DE SESIÓN: 
    # Ignorar las primeras 120 velas del día porque las bandas están colapsadas.
    if last['bar_num'] < 120:
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
        
  
    