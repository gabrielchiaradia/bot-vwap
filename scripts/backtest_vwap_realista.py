"""
scripts/backtest_vwap.py — Scalping Institucional: VWAP + Desviación Estándar
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from binance.client import Client
from dotenv import load_dotenv

load_dotenv()

INITIAL_CAPITAL = 1000.0

# Comisiones (Usamos Maker para entrar en la banda y para TP en el VWAP)
TAKER_FEE = 0.0005  # 0.05% (Stop Loss a mercado)
MAKER_FEE = 0.0002  # 0.02% (Entrada Limit y Take Profit Limit)

# ── Colores terminal ─────────────────────────────────────────────────────────
class K:
    G  = "\033[92m"   # green
    R  = "\033[91m"   # red
    Y  = "\033[93m"   # yellow
    C  = "\033[96m"   # cyan
    B  = "\033[1m"    # bold
    D  = "\033[2m"    # dim
    X  = "\033[0m"    # reset


# ── Ventanas horarias ─────────────────────────────────────────────────────────
def parse_window(w: str):
    """
    Parsea una ventana horaria.
    Formatos: '13-16', '8-12', '24h', '0-24'
    Retorna: (hora_inicio, hora_fin) o None si es 24h
    """
    w = w.strip()
    if w in ('24h', '24', '0-24', 'all'):
        return None  # Sin filtro
    if '-' in w:
        parts = w.split('-')
        return (int(parts[0]), int(parts[1]))
    raise ValueError(f"Formato de ventana invalido: {w}")

def in_workday(open_time, workdays_only: bool) -> bool:
    """Retorna True si la vela esta en un dia habil (lunes-viernes)."""
    if not workdays_only:
        return True
    import pandas as pd
    dow = pd.Timestamp(open_time).weekday()  # 0=lunes, 6=domingo
    return dow < 5

def in_window(open_time, window):
    """Verifica si un timestamp esta dentro de la ventana horaria UTC."""
    if window is None:
        return True
    h = open_time.hour if hasattr(open_time, 'hour') else None
    if h is None:
        # numpy datetime64
        import pandas as pd
        h = pd.Timestamp(open_time).hour
    start, end = window
    if start < end:
        return start <= h < end
    else:  # overnight wrap ej: 22-6
        return h >= start or h < end

# ── Data ──────────────────────────────────────────────────────────────────────
def fetch_candles(client, symbol, interval, days):
    cache_dir = "backtest/data"
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{symbol}_{interval}_{days}d_cache.csv")
    
    # 1. Comprobar si existe el archivo y qué tan viejo es
    if os.path.exists(cache_file):
        file_mtime = os.path.getmtime(cache_file)
        current_time = time.time()
        age_in_days = (current_time - file_mtime) / (24 * 3600)
        
        if age_in_days > 10:
            print(f"  {K.Y}⚠ Caché antiguo detectado ({round(age_in_days, 1)} días). Borrando y actualizando...{K.X}")
            os.remove(cache_file)
        else:
            print(f"  {K.G}📦 ✓ Cargando caché local ({round(age_in_days, 1)} días de antigüedad):{K.X} {cache_file}")
            df = pd.read_csv(cache_file)
            df["open_time"] = pd.to_datetime(df["open_time"]) 
            return df

    # 2. Descargar desde Binance
    chunk_days = 5
    all_dfs = []
    total_chunks = max(1, (days + chunk_days - 1) // chunk_days)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    chunk_start = start

    for idx in range(total_chunks):
        chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
        pct = round((idx + 1) / total_chunks * 100)
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"\r  {K.C}[{bar}]{K.X} {K.B}{pct}%{K.X} —🌐 descargando {symbol} {interval}...", end="", flush=True)

        start_str = chunk_start.strftime("%d %b %Y %H:%M:%S")
        end_str = chunk_end.strftime("%d %b %Y %H:%M:%S")
        raw = client.futures_historical_klines(symbol, interval, start_str, end_str)

        if raw:
            df = pd.DataFrame(raw, columns=[
                "open_time","open","high","low","close","volume",
                "close_time","qav","trades","tbbav","tbqav","ignore",
            ])
            for col in ["open","high","low","close","volume"]:
                df[col] = df[col].astype(float)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            all_dfs.append(df[["open_time","open","high","low","close","volume"]])

        chunk_start = chunk_end

    print(f"\r  {K.G}[{'█'*20}] 100%{K.X} — {symbol} {interval} {K.G}listo!{K.X}          ")
    if not all_dfs: return pd.DataFrame()
    
    result = pd.concat(all_dfs, ignore_index=True)
    result = result.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
    
    # 3. Guardar el nuevo caché en disco
    print(f"  {K.Y}Guardando nuevo caché local en:{K.X} {cache_file}")
    result.to_csv(cache_file, index=False)
    
    return result


# ── Filtro de noticias (backtest) ─────────────────────────────────────────────
def load_news_windows(symbol: str, dias: int) -> list:
    """
    Descarga eventos USD de alto impacto de Forex Factory.
    Cachea en backtest/data/news_events_ff.json por 12 horas.
    Retorna lista de tuplas (datetime_inicio, datetime_fin) en UTC.
    """
    cache_path = "backtest/data/news_events_ff.json"
    events = []

    if os.path.exists(cache_path):
        file_age = (time.time() - os.path.getmtime(cache_path)) / 3600
        if file_age < 12:
            with open(cache_path) as f:
                raw = json.load(f)
        if raw:  # solo usar cache si tiene datos
            events = raw
            print(f"  {K.G}📰 News cache cargado:{K.X} {len(events)} eventos ({cache_path})")
        else:
            print(f"  {K.Y}📰 News cache expirado. Descargando...{K.X}")

    if not events:
        FF_URLS = [
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
        ]
        for url in FF_URLS:
            try:
                resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code == 429:
                    print(f"  {K.Y}📰 FF rate limit (429). Esperá unos minutos y reintentá.{K.X}")
                    continue
                if resp.status_code == 404:
                    continue  # nextweek normal si es antes del jueves
                if resp.status_code != 200:
                    print(f"  {K.Y}📰 FF {url} → HTTP {resp.status_code}{K.X}")
                    continue
                for item in resp.json():
                    if item.get("impact", "").lower() != "high":
                        continue
                    if item.get("country", "").upper() != "USD":
                        continue
                    events.append({
                        "date":     item.get("date", ""),
                        "currency": item.get("currency", "USD").upper(),
                        "event":    item.get("title", ""),
                    })
            except Exception as e:
                print(f"  {K.R}📰 Error descargando {url}: {e}{K.X}")

        os.makedirs("backtest/data", exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(events, f, indent=2)
        print(f"  {K.G}📰 {len(events)} eventos guardados en {cache_path}{K.X}")

    # Construir ventanas: [evento - 120min, evento + 15min]
    windows = []
    for ev in events:
        try:
            dt = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
            windows.append((
                dt - timedelta(minutes=120),
                dt + timedelta(minutes=15),
            ))
        except Exception:
            continue

    print(f"  {K.C}📰 {len(windows)} ventanas de bloqueo construidas.{K.X}")
    return windows


def in_news_window(candle_time, news_windows: list) -> bool:
    """Retorna True si la vela cae dentro de alguna ventana de bloqueo."""
    if not news_windows:
        return False
    ts = pd.Timestamp(candle_time)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    dt = ts.to_pydatetime()
    return any(start <= dt <= end for start, end in news_windows)


def resample_candles(df_1m, tf: str) -> pd.DataFrame:
    """
    Resamplea velas de 1m a un timeframe superior.
    Si tf == '1m', retorna el df tal cual.
    Soporta: 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h.
    """
    if tf == "1m":
        return df_1m.copy()

    # Mapeo de nombre a offset de pandas
    tf_map = {
        "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min",
        "1h": "1h", "2h": "2h", "4h": "4h",
    }
    offset = tf_map.get(tf)
    if offset is None:
        raise ValueError(f"Timeframe no soportado: {tf}. Usa: 1m,3m,5m,15m,30m,1h,2h,4h")

    df = df_1m.copy()
    df = df.set_index("open_time")

    resampled = df.resample(offset, label="left", closed="left").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna(subset=["open"]).reset_index()

    return resampled


def calculate_vwap_bands(df, mult):
    df['date'] = df['open_time'].dt.date
    df['typ'] = (df['high'] + df['low'] + df['close']) / 3
    df['typ_vol'] = df['typ'] * df['volume']
    
    df['cum_vol'] = df.groupby('date')['volume'].cumsum()
    df['cum_typ_vol'] = df.groupby('date')['typ_vol'].cumsum()
    
    df['vwap'] = df['cum_typ_vol'] / df['cum_vol']
    
    df['dev_sq'] = df['volume'] * (df['typ'] - df['vwap'])**2
    df['cum_dev_sq'] = df.groupby('date')['dev_sq'].cumsum()
    df['std_dev'] = np.sqrt(df['cum_dev_sq'] / df['cum_vol'])
    
    df['upper_band'] = df['vwap'] + (mult * df['std_dev'])
    df['lower_band'] = df['vwap'] - (mult * df['std_dev'])
    
    df['bar_num'] = df.groupby('date').cumcount()
    return df


def run_vwap_backtest(df_1m, symbol, rr=1.0, band_mult=2.5, min_profit_pct=0.20, max_duration=60, risk_pct=1, window=None, workdays_only=False, news_windows=None, tf="1m"):
    trades = []
    capital = INITIAL_CAPITAL

    # ── Resamplear para señales ──────────────────────────────────────────────
    df_signal = resample_candles(df_1m, tf)
    df_signal = calculate_vwap_bands(df_signal, band_mult)

    # Si tf > 1m, usamos las velas de 1m para resolver trades con precisión
    use_1m_resolution = (tf != "1m")
    if use_1m_resolution:
        df_exec = df_1m.copy()
        exec_times  = df_exec["open_time"].values
        exec_opens  = df_exec["open"].values
        exec_highs  = df_exec["high"].values
        exec_lows   = df_exec["low"].values
        exec_closes = df_exec["close"].values

    # Señales del TF
    sig_times    = df_signal["open_time"].values
    sig_opens    = df_signal["open"].values
    sig_highs    = df_signal["high"].values
    sig_lows     = df_signal["low"].values
    sig_closes   = df_signal["close"].values
    sig_vwaps    = df_signal["vwap"].values
    sig_uppers   = df_signal["upper_band"].values
    sig_lowers   = df_signal["lower_band"].values
    sig_bar_nums = df_signal["bar_num"].values

    last_trade_time = pd.Timestamp("2000-01-01", tz="UTC")

    # Mapeo de tf a minutos para cooldown y max_duration
    tf_minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240}
    tf_min = tf_minutes.get(tf, 1)
    cooldown_bars = 10  # en unidades del TF de señal
    # max_duration se escala: en TF señal son N barras, en 1m son N * tf_min velas
    max_dur_1m = max_duration * tf_min if use_1m_resolution else max_duration

    for i in range(10, len(df_signal)):
        # Cooldown: al menos 10 barras del TF señal desde el último trade
        sig_time_i = pd.Timestamp(sig_times[i])
        if sig_time_i.tzinfo is None:
            sig_time_i = sig_time_i.tz_localize("UTC")
        minutes_since_last = (sig_time_i - last_trade_time).total_seconds() / 60
        if minutes_since_last < cooldown_bars * tf_min:
            continue

        if sig_bar_nums[i] < 120 // tf_min:
            continue

        # Filtro de ventana horaria
        if window is not None and not in_window(sig_times[i], window):
            continue

        # Filtro de dias de semana
        if not in_workday(sig_times[i], workdays_only):
            continue

        # Filtro de noticias
        if news_windows and in_news_window(sig_times[i], news_windows):
            continue

        c, l, h = sig_closes[i], sig_lows[i], sig_highs[i]

        # Bandas de la vela ANTERIOR del TF señal
        vwap_target  = sig_vwaps[i-1]
        lower_limit  = sig_lowers[i-1]
        upper_limit  = sig_uppers[i-1]

        direction = None
        entry = 0.0

        if l <= lower_limit:
            direction = "LONG"
            entry = lower_limit
            tp = vwap_target
        elif h >= upper_limit:
            direction = "SHORT"
            entry = upper_limit
            tp = vwap_target

        if not direction:
            continue

        reward = abs(tp - entry)
        profit_pct = (reward / entry) * 100
        if profit_pct < min_profit_pct:
            continue

        sl_dist = reward / rr
        if direction == "LONG":
            sl = entry - sl_dist
        else:
            sl = entry + sl_dist

        result, exit_p = None, None
        trade_end_time = sig_times[i]

        if use_1m_resolution:
            # ── Resolver trade en velas de 1m ────────────────────────────────
            # Encontrar el índice de 1m correspondiente al inicio de esta vela señal
            sig_open_time = sig_times[i]
            # Buscar la primera vela de 1m >= sig_open_time
            idx_start = np.searchsorted(exec_times, sig_open_time, side="left")

            # Evaluar intra-vela: primero la propia vela de entrada (1m)
            for j in range(idx_start, min(idx_start + max_dur_1m, len(df_exec))):
                h1 = exec_highs[j]
                l1 = exec_lows[j]

                if direction == "LONG":
                    # Primero verificar que la orden limit se llena
                    if j == idx_start and l1 > entry:
                        break  # En 1m nunca llegó al entry
                    hit_sl = l1 <= sl
                    hit_tp = h1 >= tp
                    if hit_sl and hit_tp:
                        result, exit_p = "LOSS", sl
                    elif hit_sl:
                        result, exit_p = "LOSS", sl
                    elif hit_tp:
                        result, exit_p = "WIN", tp
                else:
                    if j == idx_start and h1 < entry:
                        break
                    hit_sl = h1 >= sl
                    hit_tp = l1 <= tp
                    if hit_sl and hit_tp:
                        result, exit_p = "LOSS", sl
                    elif hit_sl:
                        result, exit_p = "LOSS", sl
                    elif hit_tp:
                        result, exit_p = "WIN", tp

                if result:
                    trade_end_time = exec_times[j]
                    break
        else:
            # ── TF 1m: lógica original ───────────────────────────────────────
            if direction == "LONG":
                hit_sl = l <= sl
                hit_tp = h >= tp
                if hit_sl and hit_tp: result, exit_p = "LOSS", sl
                elif hit_sl: result, exit_p = "LOSS", sl
                elif hit_tp: result, exit_p = "WIN", tp
            else:
                hit_sl = h >= sl
                hit_tp = l <= tp
                if hit_sl and hit_tp: result, exit_p = "LOSS", sl
                elif hit_sl: result, exit_p = "LOSS", sl
                elif hit_tp: result, exit_p = "WIN", tp

            if not result:
                for j in range(i+1, min(i + max_duration, len(df_signal))):
                    trade_end_time = sig_times[j]
                    if direction == "LONG":
                        hit_sl = sig_lows[j] <= sl
                        hit_tp = sig_highs[j] >= tp
                        if hit_sl and hit_tp: result, exit_p = "LOSS", sl; break
                        elif hit_sl: result, exit_p = "LOSS", sl; break
                        elif hit_tp: result, exit_p = "WIN", tp; break
                    else:
                        hit_sl = sig_highs[j] >= sl
                        hit_tp = sig_lows[j] <= tp
                        if hit_sl and hit_tp: result, exit_p = "LOSS", sl; break
                        elif hit_sl: result, exit_p = "LOSS", sl; break
                        elif hit_tp: result, exit_p = "WIN", tp; break

        if not result:
            # Expiró sin tocar SL ni TP — cerrar al último precio disponible
            if use_1m_resolution:
                last_idx = min(idx_start + max_dur_1m, len(df_exec) - 1)
                exit_p = float(exec_closes[last_idx])
                trade_end_time = exec_times[last_idx]
            else:
                last_idx = min(i + max_duration, len(df_signal) - 1)
                exit_p = float(sig_closes[last_idx])
                trade_end_time = sig_times[last_idx]
            result = "TIMEOUT"

        riesgo = capital * risk_pct
        qty = riesgo / sl_dist if sl_dist > 0 else 0

        pnl_bruto = (exit_p - entry)*qty if direction=="LONG" else (entry - exit_p)*qty

        fee_entrada = (entry * qty) * MAKER_FEE
        fee_salida = (exit_p * qty) * MAKER_FEE if result == "WIN" else (exit_p * qty) * TAKER_FEE

        total_fees = fee_entrada + fee_salida
        pnl_neto = pnl_bruto - total_fees
        capital += pnl_neto
        last_trade_time = pd.Timestamp(trade_end_time)
        if last_trade_time.tzinfo is None:
            last_trade_time = last_trade_time.tz_localize("UTC")

        trades.append({
            "time": str(sig_times[i]), "close_time": str(trade_end_time),
            "symbol": symbol, "direction": direction,
            "entry": round(entry,4), "sl": round(sl,4), "tp": round(tp,4),
            "exit": round(exit_p,4), "result": result,
            "pnl_bruto": round(pnl_bruto, 4), "fees": round(total_fees, 4),
            "pnl": round(pnl_neto,4), "capital": round(capital,2),
            "score": 100, "vol_ratio": 1.0, "rsi": 50.0, "bias": "MEAN_REV",
            "ob_zone": f"Band_{band_mult}s",
            "duration_min": round((pd.Timestamp(trade_end_time) - pd.Timestamp(sig_times[i])).total_seconds() / 60, 1),
            "tf": tf,
        })

    return trades, capital

# ── Reporte ───────────────────────────────────────────────────────────────────
def summary_dict(trades, initial, final, symbol, dias, label, band_mult, rr, risk, tf="1m"):
    total    = len(trades)
    wins     = sum(1 for t in trades if t["result"] == "WIN")
    losses   = sum(1 for t in trades if t["result"] == "LOSS")
    timeouts = sum(1 for t in trades if t["result"] == "TIMEOUT")
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pnl_bruto = sum(t.get("pnl_bruto", 0) for t in trades)
    fees = sum(t.get("fees", 0) for t in trades)

    peak = initial
    mdd = 0
    for t in trades:
        if t["capital"] > peak: peak = t["capital"]
        dd = (peak - t["capital"]) / peak
        if dd > mdd: mdd = dd

    return {
        "label": label,
        "symbol": symbol,
        "ltf": "1m",
        "htf": "VWAP",
        "timeframe": tf,
        "dias": dias,
        "band_mult": band_mult,
        "rr": rr,
        "risk_pct": risk,
        "total": total, "wins": wins,
        "losses": losses, "timeouts": timeouts,
        "winrate": round(wins / total * 100, 1) if total else 0,
        "profit_factor": round(gp / gl, 2) if gl > 0 else 0,
        "pnl_bruto": round(pnl_bruto, 2), "fees_totales": round(fees, 2),
        "pnl_total": round(sum(t["pnl"] for t in trades), 4),
        "capital_final": round(final, 2), "retorno_pct": round((final - initial) / initial * 100, 2),
        "max_drawdown": round(mdd * 100, 2), "trades_per_day": round(total / max(dias, 1), 1),
    }
def print_summary(s):
    ret_c = K.G if s['retorno_pct'] >= 0 else K.R
    pf_c  = K.G if s['profit_factor'] >= 1.3 else K.Y if s['profit_factor'] >= 1.0 else K.R
    wr_c  = K.G if s['winrate'] >= 50 else K.Y if s['winrate'] >= 40 else K.R
    print(f"\n{K.C}{'='*62}{K.X}\n  {K.B}📊 BACKTEST VWAP — {s['symbol']} | {s['timeframe']} | {s['dias']} días [Band: {s['band_mult']} RR: {s['rr']} Risk: {s['risk_pct']}%{K.X}]\n{K.C}{'='*62}{K.X}")
    timeouts = s.get('timeouts', 0)
    print(f"💹 Trades:        {K.B}{s['total']}{K.X} ({K.G}{s['wins']}W{K.X} / {K.R}{s['losses']}L{K.X} / {K.Y}{timeouts}T{K.X})")
    print(f"📈 Win rate:      {wr_c}{s['winrate']}%{K.X}")
    print(f"⚖️ Profit factor: {pf_c}{s['profit_factor']}{K.X}")
    print(f"💰 PnL NETO:      {ret_c}{s['pnl_total']:+.2f} USDT{K.X} (Fees: {s['fees_totales']:.2f})")
    print(f"💲 Capital final: {ret_c}{s['capital_final']:.2f} USDT ({s['retorno_pct']:+.1f}%){K.X}")
    print(f"📉 Max Drawdown:  {K.Y}{s['max_drawdown']}%{K.X}")
    print(f"🤑 Trades/día:    {K.C}{s['trades_per_day']}{K.X}")
    print(f"{K.C}{'='*62}{K.X}")
def print_monthly(trades):
    if not trades:
        print(f"  {K.D}Sin trades.{K.X}"); return
    meses = defaultdict(list)
    for t in trades:
        meses[t["time"][:7]].append(t)
    print(f"\n📅 {'MES':<10} {'TRADES':>7} {'WR':>7} {'PF':>6} {'PnL NETO':>10} {'RES'}")
    print(f"  {K.D}{'─'*55}{K.X}")
    mp = mt = 0
    for m in sorted(meses.keys()):
        ts = meses[m]
        n = len(ts)
        w = sum(1 for t in ts if t["result"]=="WIN")
        gp = sum(t["pnl"] for t in ts if t["pnl"]>0)
        gl = abs(sum(t["pnl"] for t in ts if t["pnl"]<0))
        pnl = sum(t["pnl"] for t in ts)
        wr = round(w/n*100,1) if n else 0
        pf = round(gp/gl,2) if gl>0 else (round(100, 2) if gp > 0 else 0)
        mt += 1
        if pnl>0: mp += 1
        pnl_c = K.G if pnl > 0 else K.R
        wr_c = K.G if wr >= 50 else K.Y if wr >= 40 else K.R
        pf_c = K.G if pf >= 1.3 else K.Y if pf >= 1.0 else K.R
        e = f"{K.G}✓{K.X}" if pnl > 0 else f"{K.R}✗{K.X}"
        icon = f"📈" if pnl > 0 else f"📉"
        print(f"{icon} {m:<10} {n:>7} {wr_c}{wr:>6.1f}%{K.X} {pf_c}{pf:>6.2f}{K.X} {pnl_c}{pnl:>+10.2f}{K.X}  {e}")
    print(f"  {K.D}{'─'*55}{K.X}")
    mp_c = K.G if mp/max(mt,1) >= 0.7 else K.Y if mp/max(mt,1) >= 0.5 else K.R
    print(f"💎  Meses positivos: {mp_c}{mp}/{mt} ({round(mp/max(mt,1)*100)}%){K.X}")



def main():
    p = argparse.ArgumentParser(description="Backtest VWAP Reversion")
    p.add_argument("--symbol",     default="BTCUSDT")
    p.add_argument("--dias",       type=int, default=90)
    p.add_argument("--rr",         type=str, default='0.5')
    p.add_argument("--band-mult",  type=str, default='2.5')
    p.add_argument("--min-profit", type=float, default=0.20)
    p.add_argument("--risk",       type=str, default='1')
    p.add_argument("--sweep-rr",   action="store_true")
    p.add_argument("--scan",       action="store_true")
    p.add_argument("--windows",    type=str, default="24h")
    p.add_argument("--days",       type=str, default="allweek",
                   help="Dias a operar: allweek (default) | workdays (lun-vie) | allweek;workdays para comparar")
    p.add_argument("--no-news",    action="store_true",
                   help="Simular filtro de noticias: excluir velas en ventana de eventos high-impact (FCS API)")
    p.add_argument("--tf",           type=str, default="1m",
                   help="Timeframe(s) para señales VWAP. Ej: 1m | 5m | 15m,1h | 1m;5m;15m;1h (semicolon=sweep)")
    p.add_argument("--max-duration", type=int, default=60,
                   help="Duración máxima de un trade en barras del TF señal (default: 60). "
                        "Ej: 60=1h, 1440=1día, 2880=2días. Al expirar se cierra al precio actual.")
    args = p.parse_args()

    client = Client(os.getenv("BINANCE_API_KEY",""), os.getenv("BINANCE_API_SECRET",""))
    symbols = ["BTCUSDT", "ETHUSDT"] if args.scan else [args.symbol]

    if args.sweep_rr:
        rrs = [0.2, 0.3, 0.4, 0.5, 0.7]
    else:
        rrs = [float(x) for x in args.rr.split(',')]

    windows_raw = [w.strip() for w in args.windows.split(';')]
    windows = [(w, parse_window(w)) for w in windows_raw]

    days_raw = [d.strip() for d in args.days.split(';')]
    days_list = [(d, d == 'workdays') for d in days_raw]

    band_mults = [float(x) for x in args.band_mult.split(',')]
    risks      = [float(x) for x in args.risk.split(',')]

    # ── Timeframes ──────────────────────────────────────────────────────────
    timeframes = [tf.strip() for tf in args.tf.replace(',', ';').split(';') if tf.strip()]
    if not timeframes:
        timeframes = ["1m"]

    # ── Cargar ventanas de noticias una sola vez (si --no-news activo) ────────
    # Se cachea por símbolo; si hay sweep multi-symbol, se carga una vez por cada uno
    news_windows_cache = {}
    if args.no_news:
        print(f"\n{K.Y}📰 Modo --no-news activo. Cargando eventos de alto impacto...{K.X}")
        for sym in symbols:
            news_windows_cache[sym] = load_news_windows(sym, args.dias)
    # ─────────────────────────────────────────────────────────────────────────

    total_combos = len(symbols) * len(band_mults) * len(rrs) * len(risks) * len(windows) * len(days_list) * len(timeframes)
    if total_combos > 1:
        print(f"\n{K.C}{'═'*62}{K.X}")
        print(f"  {K.B}SWEEP: {total_combos} combinaciones{K.X}  "
              f"symbols={symbols}  tf={timeframes}  band={band_mults}  rr={rrs}  risk={risks}  windows={windows_raw}  days={days_raw}")
        print(f"{K.C}{'═'*62}{K.X}\n")

    all_summaries = []
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("backtest/results", exist_ok=True)

    for symbol in symbols:
        df_1m = fetch_candles(client, symbol, "1m", args.dias)
        nw = news_windows_cache.get(symbol, None)
        for tf_val in timeframes:
            for band_val in band_mults:
                for rr_val in rrs:
                    for risk_val in risks:
                        for win_label, win_parsed in windows:
                          for day_label, workdays_only in days_list:
                            news_tag = "_NEWS" if args.no_news else ""
                            label = f"VWAP_{tf_val}_B{band_val}_RR{rr_val}_RSK{risk_val}_W{win_label}_D{day_label}{news_tag}"
                            risk_decimal = risk_val / 100.0

                            trades, final = run_vwap_backtest(
                                df_1m, symbol,
                                rr=rr_val, band_mult=band_val,
                                min_profit_pct=args.min_profit,
                                risk_pct=risk_decimal,
                                window=win_parsed,
                                workdays_only=workdays_only,
                                news_windows=nw,
                                tf=tf_val,
                                max_duration=args.max_duration,
                            )

                            s = summary_dict(trades, INITIAL_CAPITAL, final, symbol, args.dias, label, band_val, rr_val, risk_val, tf=tf_val)
                            s["trades"] = trades
                            print_summary(s)
                            print_monthly(trades)
                            all_summaries.append(s)

    # Nombre descriptivo: single → detallado, multi → SWEEP
    news_suffix = "_NEWS" if args.no_news else ""
    if len(all_summaries) == 1:
        s = all_summaries[0]
        win_str = windows_raw[0].replace('-','_')
        day_str = days_raw[0]
        tf_str = timeframes[0]
        filename = f"backtest_{s['symbol']}_{tf_str}_B{s['band_mult']}_RR{s['rr']}_RSK{s['risk_pct']}_W{win_str}_D{day_str}{news_suffix}_{ts_str}.json"
    else:
        tf_tag = "_".join(timeframes) if len(timeframes) <= 3 else f"{len(timeframes)}TFs"
        filename = f"backtest_SWEEP_{args.symbol}_{tf_tag}{news_suffix}_{ts_str}.json"
    if len(all_summaries) > 1:
        ranked = sorted(all_summaries, key=lambda x: x["profit_factor"], reverse=True)
        print(f"\n{K.C}{'═'*62}{K.X}")
        print(f"  {K.B}RANKING por Profit Factor:{K.X}")
        print(f"{K.D}{'─'*62}{K.X}")
        for idx, s in enumerate(ranked, 1):
            pf_c = K.G if s['profit_factor'] >= 1.3 else K.Y if s['profit_factor'] >= 1.0 else K.R
            pnl_c = K.G if s['pnl_total'] >= 0 else K.R
            medal = " 🥇" if idx == 1 else " 🥈" if idx == 2 else " 🥉" if idx == 3 else f" {idx}."
            print(f"   {medal} {K.B}{s['symbol']}{K.X} {K.C}{s['timeframe']}{K.X} [{K.Y}R/R: {s['rr']}{K.X}] "
                  f"Band={s['band_mult']}{K.X} "
                  f"PF={pf_c}{s['profit_factor']}{K.X} "
                  f"WR={s['winrate']}% "
                  f"PnL ={pnl_c}{s['retorno_pct']:+.0f}%{K.X} "
                  f"DD={s['max_drawdown']}%{K.X} "
                  f"Risk={s['risk_pct']}")
        print(f"{K.C}{'═'*62}{K.X}")    
        ranked = sorted(all_summaries, key=lambda x: x["pnl_total"], reverse=True)
        print(f"\n{K.C}{'═'*62}{K.X}")
        print(f"  {K.B}RANKING por Retorno:{K.X}")
        print(f"{K.D}{'─'*62}{K.X}")
        for idx, s in enumerate(ranked, 1):
            pf_c = K.G if s['profit_factor'] >= 1.3 else K.Y if s['profit_factor'] >= 1.0 else K.R
            pnl_c = K.G if s['pnl_total'] >= 0 else K.R
            medal = " 🥇" if idx == 1 else " 🥈" if idx == 2 else " 🥉" if idx == 3 else f" {idx}."
            print(f"   {medal} {K.B}{s['symbol']}{K.X} {K.C}{s['timeframe']}{K.X} [{K.Y}R/R: {s['rr']}{K.X}] "
                  f"Band={s['band_mult']}{K.X} "
                  f"PF={pf_c}{s['profit_factor']}{K.X} "
                  f"WR={s['winrate']}% "
                  f"PnL={pnl_c}{s['retorno_pct']:+.0f}%{K.X} "
                  f"DD={s['max_drawdown']}%{K.X} "
                  f"Risk={s['risk_pct']}")
        print(f"{K.C}{'═'*62}{K.X}")

    out = os.path.join("backtest/results", filename)

    # Preparar data para JSON
    data_out = {"summaries": []}
    for s in all_summaries:
        s_copy = {k: v for k, v in s.items() if k != "trades"}
        s_copy["trades"] = s.get("trades", [])
        data_out["summaries"].append(s_copy)

    # Guardar archivo
    with open(out, "w") as f:
        json.dump(data_out, f, indent=2)
        
    print(f"\n{K.G}JSON guardado para Dashboard en:{K.X} {out}")

if __name__ == "__main__":
    main()