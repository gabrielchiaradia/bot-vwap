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


def run_vwap_backtest(df, symbol, rr=1.0, band_mult=2.5, min_profit_pct=0.20, max_duration=60, risk_pct=1):
    trades = []
    capital = INITIAL_CAPITAL
    df = calculate_vwap_bands(df.copy(), band_mult)
    
    times = df["open_time"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    vols = df["volume"].values
    
    vwaps = df["vwap"].values
    uppers = df["upper_band"].values
    lowers = df["lower_band"].values
    bar_nums = df["bar_num"].values

    last_trade_bar = -999

    for i in range(10, len(df)):
        if i - last_trade_bar < 10:  
            continue
            
        if bar_nums[i] < 120:
            continue
            
        c, l, h = closes[i], lows[i], highs[i]
        vwap, lower, upper = vwaps[i], lowers[i], uppers[i]
        
        direction = None
        entry = 0.0
        
        if l <= lower and c > lower * 0.998:  
            direction = "LONG"
            entry = lower
            tp = vwap
            
        elif h >= upper and c < upper * 1.002:
            direction = "SHORT"
            entry = upper
            tp = vwap

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
        for j in range(i+1, min(i + max_duration, len(df))):
            if direction == "LONG":
                if lows[j] <= sl:
                    result, exit_p = "LOSS", sl; break
                if highs[j] >= tp:
                    result, exit_p = "WIN", tp; break
            else:
                if highs[j] >= sl:
                    result, exit_p = "LOSS", sl; break
                if lows[j] <= tp:
                    result, exit_p = "WIN", tp; break

        if not result:
            continue

        # Posicionamiento dinámico usando la variable de riesgo inyectada
        riesgo = capital * risk_pct
        qty = riesgo / sl_dist if sl_dist > 0 else 0
        
        pnl_bruto = (exit_p - entry)*qty if direction=="LONG" else (entry - exit_p)*qty
        
        fee_entrada = (entry * qty) * MAKER_FEE  
        fee_salida = (exit_p * qty) * MAKER_FEE if result == "WIN" else (exit_p * qty) * TAKER_FEE
            
        total_fees = fee_entrada + fee_salida
        pnl_neto = pnl_bruto - total_fees
        capital += pnl_neto
        last_trade_bar = j

        trades.append({
            "time": str(times[i]), "close_time": str(times[j]),
            "symbol": symbol, "direction": direction,
            "entry": round(entry,4), "sl": round(sl,4), "tp": round(tp,4),
            "exit": round(exit_p,4), "result": result,
            "pnl_bruto": round(pnl_bruto, 4), "fees": round(total_fees, 4),
            "pnl": round(pnl_neto,4), "capital": round(capital,2),
            "score": 100, "vol_ratio": 1.0, "rsi": 50.0, "bias": "MEAN_REV",
            "ob_zone": f"Band_{band_mult}s", 
            "duration_min": float(j - i),
        })

    return trades, capital

# ── Reporte ───────────────────────────────────────────────────────────────────
def summary_dict(trades, initial, final, symbol, dias, label, band_mult, rr, risk):
    total = len(trades)
    wins = sum(1 for t in trades if t["result"]=="WIN")
    losses = total - wins
    gp = sum(t["pnl"] for t in trades if t["pnl"]>0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"]<0))
    pnl_bruto = sum(t.get("pnl_bruto", 0) for t in trades)
    fees = sum(t.get("fees", 0) for t in trades)
    
    peak = initial
    mdd = 0
    for t in trades:
        if t["capital"]>peak: peak=t["capital"]
        dd = (peak-t["capital"])/peak
        if dd>mdd: mdd=dd
        
    return {
        "label": label, 
        "symbol": symbol, 
        "ltf": "1m", 
        "htf": "VWAP",
        "timeframe": "1m", 
        "dias": dias, 
        "band_mult": band_mult,  # Inyectado para el JSON
        "rr": rr,                # Inyectado para el JSON
        "risk_pct": risk,        # Inyectado para el JSON
        "total": total, "wins": wins,
        "losses": losses, "winrate": round(wins/total*100,1) if total else 0,
        "profit_factor": round(gp/gl,2) if gl>0 else 0,
        "pnl_bruto": round(pnl_bruto, 2), "fees_totales": round(fees, 2),
        "pnl_total": round(sum(t["pnl"] for t in trades),4),
        "capital_final": round(final,2), "retorno_pct": round((final-initial)/initial*100,2),
        "max_drawdown": round(mdd*100,2), "trades_per_day": round(total/max(dias,1),1),
        "trades_per_day": round(total/max(dias,1),1),
    }
def print_summary(s):
    ret_c = K.G if s['retorno_pct'] >= 0 else K.R
    pf_c = K.G if s['profit_factor'] >= 1.3 else K.Y if s['profit_factor'] >= 1.0 else K.R
    wr_c = K.G if s['winrate'] >= 50 else K.Y if s['winrate'] >= 40 else K.R
    print(f"\n{K.C}{'='*62}{K.X}\n  {K.B}📊 BACKTEST VWAP — {s['symbol']} | {s['dias']} días [Band: {s['band_mult']} RR: {s['rr']} Risk: {s['risk_pct']}%{K.X}]\n{K.C}{'='*62}{K.X}")
    print(f"💹 Trades:        {K.B}{s['total']}{K.X} ({K.G}{s['wins']}W{K.X} / {K.R}{s['losses']}L{K.X})")
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
    p.add_argument("--symbol", default="BTCUSDT", help="Select SYMBOL to scan, default = BTCUSDT")
    p.add_argument("--dias", type=int, default=90, help="Dias a escanear. default = 90")
    p.add_argument("--rr", type=str, default='0.5', help="Risk/Ratio %%, default = 0.5")
    p.add_argument("--band-mult", type=float, default=2.5, help="default 2.5")
    p.add_argument("--min-profit", type=float, default=0.20, help="Porcentaje minimo de profit para ignorar fees")
    p.add_argument("--risk", type=float, default=1, help="Riesgo por trade en porcentaje (ej: 1.0 para 1%%) default = 1")
    p.add_argument("--sweep-rr", action="store_true", help="Prueba diferentes RR (0.2, 0.3, 0.4, 0.5, 0.7)")
    p.add_argument("--scan", action="store_true",help="Escanea BTCUSDT - ETHUSDT")
    args = p.parse_args()

    client = Client(os.getenv("BINANCE_API_KEY",""), os.getenv("BINANCE_API_SECRET",""))
    symbols = ["BTCUSDT", "ETHUSDT"] if args.scan else [args.symbol]
    
    # RRs ajustados para enfocarnos en los rentables (< 1.0)
    #rrs = [0.2, 0.3, 0.4, 0.5, 0.7] if args.sweep_rr else [args.rr]
    if args.sweep_rr:
        rrs = [0.2, 0.3, 0.4, 0.5, 0.7] 
    else:
        rrs = [float(x) for x in args.rr.split(',')]
    
    all_summaries = []
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("backtest/results", exist_ok=True)

    for symbol in symbols:
        df_1m = fetch_candles(client, symbol, "1m", args.dias)
        for rr_val in rrs:
            label = f"VWAP_B{args.band_mult}_RR{rr_val}_RSK{args.risk}"
            risk_decimal = args.risk / 100.0
            
            trades, final = run_vwap_backtest(
                df_1m, symbol, 
                rr=rr_val, band_mult=args.band_mult, 
                min_profit_pct=args.min_profit,
                risk_pct=risk_decimal
            )
            
            s = summary_dict(trades, INITIAL_CAPITAL, final, symbol, args.dias, label, args.band_mult, rr_val, args.risk)
            s["trades"] = trades
            print_summary(s)
            print_monthly(trades)
            all_summaries.append(s)

    data_out = {"summaries": []}
    # Generar un nombre de archivo descriptivo
    if len(all_summaries) == 1:
        s = all_summaries[0]
        # Creamos el nombre: backtest_ETHUSDT_B2.5_RR0.4_RSK4.0.json
        filename = f"backtest_{s['symbol']}_B{args.band_mult}_RR{s['rr']}_RSK{args.risk}_{ts_str}.json"
    else:
        filename = f"backtest_SCAN_{ts_str}.json"
    if len(all_summaries) > 1:
        ranked = sorted(all_summaries, key=lambda x: x["profit_factor"], reverse=True)
        print(f"\n{K.C}{'═'*62}{K.X}")
        print(f"  {K.B}RANKING por Profit Factor:{K.X}")
        print(f"{K.D}{'─'*62}{K.X}")
        for idx, s in enumerate(ranked, 1):
            pf_c = K.G if s['profit_factor'] >= 1.3 else K.Y if s['profit_factor'] >= 1.0 else K.R
            pnl_c = K.G if s['pnl_total'] >= 0 else K.R
            medal = " 🥇" if idx == 1 else " 🥈" if idx == 2 else " 🥉" if idx == 3 else f" {idx}."
            print(f"   {medal} {K.B}{s['symbol']}{K.X} [{K.Y}Risk/Reward: {s['rr']}{K.X}] "
                  f"PF={pf_c}{s['profit_factor']}{K.X} "
                  f"WR={s['winrate']}% "
                  f"PnL ={pnl_c}{s['retorno_pct']:+.2f}%{K.X} "
                  f"DD={s['max_drawdown']}%")
        print(f"{K.C}{'═'*62}{K.X}")    
        ranked = sorted(all_summaries, key=lambda x: x["pnl_total"], reverse=True)
        print(f"\n{K.C}{'═'*62}{K.X}")
        print(f"  {K.B}RANKING por Retorno:{K.X}")
        print(f"{K.D}{'─'*62}{K.X}")
        for idx, s in enumerate(ranked, 1):
            pf_c = K.G if s['profit_factor'] >= 1.3 else K.Y if s['profit_factor'] >= 1.0 else K.R
            pnl_c = K.G if s['pnl_total'] >= 0 else K.R
            medal = " 🥇" if idx == 1 else " 🥈" if idx == 2 else " 🥉" if idx == 3 else f" {idx}."
            print(f"   {medal} {K.B}{s['symbol']}{K.X} [{K.Y}Risk/Reward: {s['rr']}{K.X}] "
                  f"PF={pf_c}{s['profit_factor']}{K.X} "
                  f"WR={s['winrate']}% "
                  f"PnL ={pnl_c}{s['retorno_pct']:+.2f}%{K.X} "
                  f"DD={s['max_drawdown']}%")
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