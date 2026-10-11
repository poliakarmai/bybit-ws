"""SHORT-side parameter sweep — разовый эксперимент (задача: SHORT PF 0.65).

Гоняет engine.simulate по вариантам параметров, режет trades по side=='Sell',
считает PF/WR/avg только по шортам. Боевой state.db не трогается.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bybit_ws.experiments import engine

SYMBOLS = ["STXUSDT", "DOTUSDT", "ONDOUSDT", "SOLUSDT"]  # худшие SHORT из trade_history + ликвидный бенчмарк
DAYS = 180
VARIANTS = [
    {"name": "base_rr2_sl5", "rr_ratio": 2.0, "sl_pct": 0.05, "min_score": 20},
    {"name": "rr2_sl6", "rr_ratio": 2.0, "sl_pct": 0.06, "min_score": 20},  # текущий live SHORT SL
    {"name": "rr3_sl5", "rr_ratio": 3.0, "sl_pct": 0.05, "min_score": 20},  # чинит avg_loss>avg_win
    {"name": "rr3_sl6", "rr_ratio": 3.0, "sl_pct": 0.06, "min_score": 20},
    {"name": "rr2_sl5_s25", "rr_ratio": 2.0, "sl_pct": 0.05, "min_score": 25},  # реже, но качественнее
    {"name": "rr3_sl5_s25", "rr_ratio": 3.0, "sl_pct": 0.05, "min_score": 25},
]


def short_stats(trades):
    s = [t for t in trades if t.get("side") == "Sell"]
    if not s:
        return None
    wins = [t["pnl"] for t in s if t["pnl"] > 0]
    losses = [t["pnl"] for t in s if t["pnl"] <= 0]
    gp, gl = sum(wins), abs(sum(losses))
    return {
        "n": len(s),
        "wr": 100 * len(wins) / len(s),
        "pf": gp / gl if gl > 0 else float("inf"),
        "pnl": sum(t["pnl"] for t in s),
        "avg_win": gp / len(wins) if wins else 0,
        "avg_loss": gl / len(losses) if losses else 0,
    }


def main():
    end_ms = engine.resolve_end_ms(None)
    from datetime import timedelta
    start_ms = end_ms - int(timedelta(days=DAYS).total_seconds() * 1000)

    agg: dict[str, dict] = {v["name"]: {"n": 0, "wins": 0, "gp": 0.0, "gl": 0.0, "pnl": 0.0} for v in VARIANTS}

    for sym in SYMBOLS:
        klines = engine.fetch_klines(sym, "240", start_ms, end_ms)
        if not klines:
            print(f"{sym}: нет данных", file=sys.stderr)
            continue
        for v in VARIANTS:
            params = {k: v[k] for k in ("rr_ratio", "sl_pct", "min_score")}
            trades, _eq = engine.simulate(klines, sym, 500.0, params)
            st = short_stats(trades)
            if not st:
                continue
            a = agg[v["name"]]
            a["n"] += st["n"]
            a["wins"] += round(st["wr"] * st["n"] / 100)
            a["pnl"] += st["pnl"]
            s = [t for t in trades if t.get("side") == "Sell"]
            a["gp"] += sum(t["pnl"] for t in s if t["pnl"] > 0)
            a["gl"] += abs(sum(t["pnl"] for t in s if t["pnl"] <= 0))
        print(f"  {sym} done", file=sys.stderr)

    print(f"\n{'variant':<16}{'n':>4}{'WR%':>7}{'PF':>7}{'PnL$':>9}{'avgW':>7}{'avgL':>7}")
    rows = []
    for name, a in agg.items():
        if a["n"] == 0:
            print(f"{name:<16}{'0':>4}   — нет шорт-сделок")
            continue
        wr = 100 * a["wins"] / a["n"]
        pf = a["gp"] / a["gl"] if a["gl"] > 0 else float("inf")
        aw = a["gp"] / a["wins"] if a["wins"] else 0
        al = a["gl"] / (a["n"] - a["wins"]) if a["n"] > a["wins"] else 0
        rows.append((name, a["n"], wr, pf, a["pnl"], aw, al))
    for r in sorted(rows, key=lambda x: -x[3]):
        print(f"{r[0]:<16}{r[1]:>4}{r[2]:>7.1f}{r[3]:>7.2f}{r[4]:>9.2f}{r[5]:>7.2f}{r[6]:>7.2f}")


if __name__ == "__main__":
    main()
