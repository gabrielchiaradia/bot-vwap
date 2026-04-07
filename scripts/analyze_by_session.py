"""
analyze_by_session.py
Analiza trades del journal VWAP filtrando por franja horaria UTC.
Uso:
    python3 analyze_by_session.py                          # todos los archivos
    python3 analyze_by_session.py --file journal.json      # archivo específico
    python3 analyze_by_session.py --bias MEAN_REV          # filtrar por estrategia
    python3 analyze_by_session.py --session asia           # filtrar por sesión
"""

import json
import glob
import argparse
from datetime import datetime, timezone
from pathlib import Path

# ── Sesiones UTC ──────────────────────────────────────────────────────────────
SESSIONS = {
    "asia":    (0,  8),
    "london":  (8,  13),
    "ny":      (13, 20),
    "off":     (20, 24),
}

def get_session(hour: int) -> str:
    for name, (start, end) in SESSIONS.items():
        if start <= hour < end:
            return name
    return "off"

# ── Métricas ──────────────────────────────────────────────────────────────────
def calc_metrics(trades: list) -> dict:
    if not trades:
        return {}
    wins   = [t for t in trades if t["result"] == "WIN"]
    losses = [t for t in trades if t["result"] == "LOSS"]
    pnl    = sum(t["pnl"] for t in trades)
    gross  = sum(t["pnl_bruto"] for t in trades)
    win_pnl  = sum(t["pnl"] for t in wins)
    loss_pnl = abs(sum(t["pnl"] for t in losses))
    pf = round(win_pnl / loss_pnl, 2) if loss_pnl > 0 else float("inf")
    wr = round(len(wins) / len(trades) * 100, 1)
    avg_dur = round(sum(t.get("duration_min", 0) for t in trades) / len(trades), 1)
    return {
        "trades":   len(trades),
        "wins":     len(wins),
        "losses":   len(losses),
        "winrate":  f"{wr}%",
        "PF":       pf,
        "pnl":      round(pnl, 2),
        "pnl_bruto": round(gross, 2),
        "avg_dur_min": avg_dur,
    }

def print_table(title: str, rows: dict):
    print(f"\n{'═'*55}")
    print(f"  {title}")
    print(f"{'─'*55}")
    print(f"  {'Sesión':<12} {'Trades':>6} {'WR':>7} {'PF':>6} {'PnL':>10}")
    print(f"{'─'*55}")
    for session, m in rows.items():
        if not m:
            continue
        print(f"  {session:<12} {m['trades']:>6} {m['winrate']:>7} {m['PF']:>6} {m['pnl']:>10.2f}")
    print(f"{'═'*55}")

# ── Main ──────────────────────────────────────────────────────────────────────
def analyze(files: list, bias_filter: str = None, session_filter: str = None):
    all_trades = []

    for f in files:
        with open(f) as fh:
            data = json.load(fh)
        trades = data.get("trades", [])
        bot    = data.get("summary", {}).get("bot", Path(f).stem)
        for t in trades:
            t["_bot"]  = bot
            t["_file"] = f
        all_trades.extend(trades)

    if not all_trades:
        print("No hay trades.")
        return

    # Parsear hora UTC de apertura
    for t in all_trades:
        dt = datetime.fromisoformat(t["time"])
        t["_hour"]    = dt.astimezone(timezone.utc).hour
        t["_session"] = get_session(t["_hour"])

    # Filtros opcionales
    if bias_filter:
        all_trades = [t for t in all_trades if t.get("bias", "").upper() == bias_filter.upper()]
    if session_filter:
        all_trades = [t for t in all_trades if t["_session"] == session_filter.lower()]

    bots = sorted(set(t["_bot"] for t in all_trades))

    for bot in bots:
        bt = [t for t in all_trades if t["_bot"] == bot]
        print(f"\n{'#'*55}")
        print(f"  BOT: {bot}  ({len(bt)} trades totales)")

        # Por sesión
        rows = {}
        for sname in SESSIONS:
            st = [t for t in bt if t["_session"] == sname]
            rows[sname] = calc_metrics(st)
        print_table("Por sesión UTC", rows)

        # Por bias
        biases = sorted(set(t.get("bias", "?") for t in bt))
        if len(biases) > 1:
            rows_bias = {}
            for b in biases:
                rows_bias[b] = calc_metrics([t for t in bt if t.get("bias") == b])
            print_table("Por estrategia (bias)", rows_bias)

        # Por sesión + bias
        for b in biases:
            rows_sb = {}
            for sname in SESSIONS:
                st = [t for t in bt if t["_session"] == sname and t.get("bias") == b]
                rows_sb[sname] = calc_metrics(st)
            if any(rows_sb.values()):
                print_table(f"Sesión × {b}", rows_sb)

    # Total global
    print(f"\n{'#'*55}")
    print(f"  TOTAL GLOBAL ({len(all_trades)} trades)")
    rows_total = {s: calc_metrics([t for t in all_trades if t["_session"] == s]) for s in SESSIONS}
    print_table("Por sesión UTC (todos los bots)", rows_total)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",    help="Archivo JSON específico (default: todos los dashboard_trades_*.json)")
    parser.add_argument("--bias",    help="Filtrar por estrategia: MEAN_REV o CROSS")
    parser.add_argument("--session", help="Filtrar por sesión: asia / london / ny / off")
    args = parser.parse_args()

    if args.file:
        files = [args.file]
    else:
        files = glob.glob("dashboard/data/dashboard_trades_*.json")
        if not files:
            files = glob.glob("*.json")

    if not files:
        print("No se encontraron archivos JSON.")
    else:
        print(f"Archivos: {files}")
        analyze(files, bias_filter=args.bias, session_filter=args.session)
