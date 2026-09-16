"""Parallel sandbox — пункт 3 ТЗ (experiment tree).

Гоняет N вариантов параметров на одном и том же диапазоне истории, каждый
вариант — в изолированном состоянии (собственный immutable run + experiment
в отдельной sandbox-БД, отдельной от experiments.db и от боевого state.db).
Выдаёт сравнительную таблицу, ранжированную по PF (или Sharpe).

Использование:
    python3 -m bybit_ws.experiments.parallel_sandbox --symbol SOLUSDT --days 60
    python3 -m bybit_ws.experiments.parallel_sandbox --symbol SOLUSDT --days 90 \
        --variants variants.json --rank-by sharpe

Формат variants.json (список вариантов):
    [
      {"name": "rr2_sl5", "params": {"rr_ratio": 2.0, "sl_pct": 0.05}, "hypothesis": "..."},
      {"name": "rr3_sl5", "params": {"rr_ratio": 3.0, "sl_pct": 0.05}}
    ]
или компактно {"name": {params}, ...}.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bybit_ws.experiments import engine  # noqa: E402
from bybit_ws.experiments.store import ExperimentStore, SANDBOX_DB  # noqa: E402

# Встроенные варианты — чтобы sandbox запускался без файла (для демо/проверки).
DEFAULT_VARIANTS: list[dict] = [
    {"name": "base_rr2_sl5", "params": {"rr_ratio": 2.0, "sl_pct": 0.05, "min_score": 20}},
    {"name": "rr3_sl5", "params": {"rr_ratio": 3.0, "sl_pct": 0.05, "min_score": 20}},
    {"name": "rr2_sl3", "params": {"rr_ratio": 2.0, "sl_pct": 0.03, "min_score": 20}},
    {"name": "rr2_sl7", "params": {"rr_ratio": 2.0, "sl_pct": 0.07, "min_score": 20}},
    {"name": "tight_score25", "params": {"rr_ratio": 2.0, "sl_pct": 0.05, "min_score": 25}},
]

RANK_KEYS = {
    "profit_factor": ("PF", True),   # выше — лучше
    "sharpe": ("Sharpe", True),
    "win_rate": ("WR", True),
    "total_pnl": ("PnL$", True),
    "max_drawdown_pct": ("MaxDD%", False),  # ниже — лучше
}


def load_variants(path: str | None) -> list[dict]:
    if not path:
        return list(DEFAULT_VARIANTS)
    raw = json.loads(Path(path).read_text())
    variants = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "params" in item:
                variants.append({"name": item.get("name", f"variant_{len(variants)}"),
                                 "params": item["params"],
                                 "hypothesis": item.get("hypothesis")})
            else:
                variants.append({"name": f"variant_{len(variants)}", "params": item})
    elif isinstance(raw, dict):
        for name, params in raw.items():
            variants.append({"name": name, "params": params})
    else:
        raise ValueError("variants.json должен быть списком или объектом {name: params}")
    return variants


def _rank_value(metrics: dict, rank_by: str) -> float:
    """Значение для ранжирования; None (PF=∞) → +inf (лучший)."""
    key = {"sharpe": "sharpe_ratio",
           "win_rate": "win_rate",
           "total_pnl": "total_pnl",
           "max_drawdown_pct": "max_drawdown_pct",
           "profit_factor": "profit_factor"}.get(rank_by, rank_by)
    v = metrics.get(key)
    if v is None:  # PF=∞
        return float("inf")
    return float(v)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Parallel sandbox — N вариантов на одном диапазоне истории")
    p.add_argument("--symbol", required=True, help="Тикер (SOLUSDT, BTCUSDT, ...)")
    p.add_argument("--days", type=int, default=60, help="Глубина истории (дней)")
    p.add_argument("--interval", default="D", help="D, 240, 60, ...")
    p.add_argument("--end-date", default=None, help="Конец окна YYYY-MM-DD")
    p.add_argument("--variants", default=None, help="Путь к variants.json")
    p.add_argument("--rank-by", default="profit_factor",
                   choices=list(RANK_KEYS), help="Метрика ранжирования")
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--db", default=None, help="Sandbox-БД (по умолчанию sandbox.db)")
    p.add_argument("--no-db", action="store_true", help="Не писать в БД")
    p.add_argument("--json", action="store_true", help="JSON-вывод таблицы")
    args = p.parse_args(argv)

    variants = load_variants(args.variants)
    if len(variants) < 1:
        print("Нет вариантов для прогона.", file=sys.stderr)
        return 1

    end_ms = engine.resolve_end_ms(args.end_date)
    start_ms = end_ms - args.days * 86400 * 1000
    klines = engine.fetch_klines(args.symbol, args.interval, start_ms, end_ms)
    kline_hash = engine.data_hash(klines)

    data_range = {
        "symbol": args.symbol, "interval": args.interval, "days": args.days,
        "start_ms": start_ms, "end_ms": end_ms,
        "start_date": engine._fmt_ms(start_ms), "end_date": engine._fmt_ms(end_ms),
        "n_candles": len(klines),
    }
    commit = engine.git_commit()
    ts_iso = datetime.now(timezone.utc).isoformat()

    store = None if args.no_db else ExperimentStore(args.db or SANDBOX_DB)
    rows = []
    for v in variants:
        name = v.get("name", f"variant_{len(rows)}")
        params = dict(engine.DEFAULT_PARAMS)
        params.update(v.get("params", {}))
        t0 = time.time()
        trades, equity = engine.simulate(klines, args.symbol, args.balance, params)
        metrics = engine.compute_metrics(trades, equity, args.balance, args.interval)
        elapsed = time.time() - t0

        manifest = engine.build_manifest(params, data_range, commit, kline_hash, ts_iso)
        run_id = None
        if store is not None:
            exp_id = store.create_experiment(
                name=f"{args.symbol}_{args.days}d__{name}",
                params=params, hypothesis=v.get("hypothesis"),
            )
            run_id = store.add_run(exp_id, kind="backtest",
                                   inputs_manifest=manifest, metrics=metrics)
        rows.append({
            "name": name, "params": params, "metrics": metrics,
            "run_id": run_id, "elapsed": elapsed,
        })

    # Ранжирование
    rank_key, higher_better = RANK_KEYS[args.rank_by]
    rows.sort(key=lambda r: _rank_value(r["metrics"], args.rank_by),
              reverse=higher_better)

    def pf_s(m):
        v = m["profit_factor"]
        return "∞" if v is None and m["wins"] > 0 else f"{v:.2f}"

    if args.json:
        out = {
            "symbol": args.symbol, "data_range": data_range, "rank_by": args.rank_by,
            "results": [
                {"rank": i + 1, "name": r["name"], "run_id": r["run_id"],
                 "metrics": r["metrics"], "params": r["params"]}
                for i, r in enumerate(rows)
            ],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        hdr = (f"{'#':>2}  {'вариант':<20} {'сделок':>6} {'WR%':>7} {'PF':>7} "
               f"{'PnL$':>10} {'avg_win$':>9} {'avg_loss$':>10} {'MaxDD%':>8} {'Sharpe':>7}")
        print(f"\n{'='*100}")
        print(f"  🧪 Parallel sandbox: {args.symbol} | {args.days}д | {args.interval}")
        print(f"     диапазон: {data_range['start_date']} .. {data_range['end_date']}")
        print(f"     свечей: {data_range['n_candles']} | git: {commit or 'n/a'}")
        print(f"     ранжирование: {rank_key} ({'выше лучше' if higher_better else 'ниже лучше'})")
        print(f"{'='*100}")
        print(hdr)
        print("─" * 100)
        for i, r in enumerate(rows):
            m = r["metrics"]
            print(f"{i+1:>2}  {r['name']:<20} {m['n_trades']:>6} {m['win_rate']:>6.2f}% "
                  f"{pf_s(m):>7} {m['total_pnl']:>+10.2f} {m['avg_win']:>+9.4f} "
                  f"{m['avg_loss']:>+10.4f} {m['max_drawdown_pct']:>7.2f}% {m['sharpe_ratio']:>7.2f}")
        print("─" * 100)
        print(f"  🥇 Лучший по {rank_key}: {rows[0]['name']} "
              f"(run #{rows[0]['run_id']})" if rows[0]["run_id"] is not None else
              f"  🥇 Лучший по {rank_key}: {rows[0]['name']}")

    if store is not None:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
