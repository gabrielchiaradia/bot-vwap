"""
analyze_by_session.py
Analiza trades del journal VWAP filtrando por franja horaria UTC.
Uso:
    python3 analyze_by_session.py                          # todos los archivos
    python3 analyze_by_session.py --file journal.json      # archivo específico
    python3 analyze_by_session.py --bias MEAN_REV          # filtrar por estrategia
    python3 analyze_by_session.py --session ny             # filtrar por sesión
"""

import json
import glob
import argparse
from datetime import datetime, timezone
from pathlib import Path

# ── Colores terminal ──────────────────────────────────────────────────────────
class K:
    G  = "\033[92m"   # green
    R  = "\033[91m"   # red
    Y  = "\033[93m"   # yellow
    C  = "\033[96m"   # cyan
    B  = "\033[1m"    # bold
    D  = "\033[2m"    # dim
    X  = "\033[0m"    # reset

# ── Sesiones UTC ──────────────────────────────────────────────────────────────
SESSIONS = {
    "asia":   (0,  8),
    "london": (8,  13),
    "ny":     (13, 20),
    "off":    (20, 24),
}

SESSION_ICONS = {
    "asia":   "🌏",
    "london": "🇬🇧",
    "ny":     "🗽",
    "off":    "🌙",
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
    wins     = [t for t in trades if t["result"] == "WIN"]
    losses   = [t for t in trades if t["result"] == "LOSS"]
    pnl      = sum(t["pnl"] for t in trades)
    win_pnl  = sum(t["pnl"] for t in wins)
    loss_pnl = abs(sum(t["pnl"] for t in losses))
    pf       = round(win_pnl / loss_pnl, 2) if loss_pnl > 0 else float("inf")
    wr       = round(len(wins) / len(trades) * 100, 1)
    avg_dur  = round(sum(t.get("duration_min", 0) for t in trades) / len(trades), 1)
    return {
        "trades":      len(trades),
        "wins":        len(wins),
        "losses":      len(losses),
        "winrate":     wr,
        "PF":          pf,
        "pnl":         round(pnl, 2),
        "avg_dur_min": avg_dur,
    }

# ── Colores dinámicos ─────────────────────────────────────────────────────────
def wr_color(wr):   return K.G if wr >= 55 else (K.Y if wr >= 45 else K.R)
def pf_color(pf):   return K.G if pf >= 1.3 else (K.Y if pf >= 1.0 else K.R)
def pnl_color(pnl): return K.G if pnl > 0 else (K.Y if pnl == 0 else K.R)

# ── Tablas ────────────────────────────────────────────────────────────────────
def print_session_table(title: str, rows: dict, icon_map: dict = None):
    print(f"\n  {K.B}{title}{K.X}")
    print(f"  {K.D}{'─'*62}{K.X}")
    print(f"  {'':14} {'TRADES':>6}  {'W/L':>7}  {'WR':>7}  {'PF':>6}  {'PnL NETO':>10}  {'DUR avg'}")
    print(f"  {K.D}{'─'*62}{K.X}")
    any_row = False
    for key, m in rows.items():
        if not m:
            continue
        any_row = True
        icon  = (icon_map or {}).get(key, "  ")
        wrc   = wr_color(m["winrate"])
        pfc   = pf_color(m["PF"])
        pnlc  = pnl_color(m["pnl"])
        pf_s  = f"{m['PF']:.2f}" if m["PF"] != float("inf") else "∞"
        print(
            f"  {icon} {K.B}{key:<12}{K.X}"
            f" {K.C}{m['trades']:>6}{K.X}"
            f"  {K.G}{m['wins']}W{K.X}/{K.R}{m['losses']}L{K.X}"
            f"  {wrc}{m['winrate']:>6.1f}%{K.X}"
            f"  {pfc}{pf_s:>6}{K.X}"
            f"  {pnlc}{m['pnl']:>+10.2f}{K.X}"
            f"  {K.D}{m['avg_dur_min']:>5.1f}m{K.X}"
        )
    if not any_row:
        print(f"  {K.D}  Sin trades.{K.X}")
    print(f"  {K.D}{'─'*62}{K.X}")

# ── Análisis ──────────────────────────────────────────────────────────────────
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
        print(f"\n  {K.Y}⚠ No hay trades en los archivos cargados.{K.X}")
        return

    # Parsear hora UTC de apertura
    for t in all_trades:
        dt = datetime.fromisoformat(t["time"])
        t["_hour"]    = dt.astimezone(timezone.utc).hour
        t["_session"] = get_session(t["_hour"])

    # Filtros opcionales
    if bias_filter:
        all_trades = [t for t in all_trades if t.get("bias", "").upper() == bias_filter.upper()]
        if not all_trades:
            print(f"\n  {K.R}✗ No hay trades con bias={bias_filter}.{K.X}")
            return
    if session_filter:
        all_trades = [t for t in all_trades if t["_session"] == session_filter.lower()]
        if not all_trades:
            print(f"\n  {K.R}✗ No hay trades en sesión={session_filter}.{K.X}")
            return

    bots = sorted(set(t["_bot"] for t in all_trades))

    for bot in bots:
        bt = [t for t in all_trades if t["_bot"] == bot]
        total_m = calc_metrics(bt)
        pnlc = pnl_color(total_m["pnl"])
        pfc  = pf_color(total_m["PF"])
        pf_s = f"{total_m['PF']:.2f}" if total_m["PF"] != float("inf") else "∞"

        print(f"\n{K.C}{'═'*66}{K.X}")
        print(f"  {K.B}🤖 {bot}{K.X}   {K.D}({len(bt)} trades totales){K.X}")
        print(f"  {K.D}PnL total: {K.X}{pnlc}{total_m['pnl']:+.2f} USDT{K.X}  "
              f"WR: {wr_color(total_m['winrate'])}{total_m['winrate']}%{K.X}  "
              f"PF: {pfc}{pf_s}{K.X}")
        print(f"{K.C}{'═'*66}{K.X}")

        # ── Por sesión ────────────────────────────────────────────────────────
        rows_ses = {s: calc_metrics([t for t in bt if t["_session"] == s]) for s in SESSIONS}
        print_session_table("📅  Por sesión UTC", rows_ses, SESSION_ICONS)

        # ── Por bias ──────────────────────────────────────────────────────────
        biases = sorted(set(t.get("bias", "?") for t in bt))
        if len(biases) > 1:
            bias_icons = {"MEAN_REV": "↩️ ", "CROSS": "➡️ "}
            rows_bias = {b: calc_metrics([t for t in bt if t.get("bias") == b]) for b in biases}
            print_session_table("📊  Por estrategia (bias)", rows_bias, bias_icons)

        # ── Sesión × bias ─────────────────────────────────────────────────────
        for b in biases:
            rows_sb = {s: calc_metrics([t for t in bt if t["_session"] == s and t.get("bias") == b]) for s in SESSIONS}
            if any(rows_sb.values()):
                print_session_table(f"🔀  Sesión × {b}", rows_sb, SESSION_ICONS)

    # ── Total global ──────────────────────────────────────────────────────────
    if len(bots) > 1:
        gm = calc_metrics(all_trades)
        pnlc = pnl_color(gm["pnl"])
        pfc  = pf_color(gm["PF"])
        pf_s = f"{gm['PF']:.2f}" if gm["PF"] != float("inf") else "∞"

        print(f"\n{K.C}{'═'*66}{K.X}")
        print(f"  {K.B}🌐 TOTAL GLOBAL{K.X}   {K.D}({len(all_trades)} trades){K.X}")
        print(f"  PnL: {pnlc}{gm['pnl']:+.2f} USDT{K.X}  "
              f"WR: {wr_color(gm['winrate'])}{gm['winrate']}%{K.X}  "
              f"PF: {pfc}{pf_s}{K.X}")
        print(f"{K.C}{'═'*66}{K.X}")
        rows_total = {s: calc_metrics([t for t in all_trades if t["_session"] == s]) for s in SESSIONS}
        print_session_table("📅  Por sesión (todos los bots)", rows_total, SESSION_ICONS)

    print()

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Análisis de trades VWAP por sesión horaria")
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
        print(f"\n  {K.R}✗ No se encontraron archivos JSON.{K.X}\n")
    else:
        print(f"\n  {K.D}Archivos: {', '.join(files)}{K.X}")
        analyze(files, bias_filter=args.bias, session_filter=args.session)
