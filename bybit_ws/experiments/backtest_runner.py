"""Reproducible backtest runner — пункт 2 ТЗ (experiment tree).

Обёртка над paper_trade: принимает вариант параметров + диапазон данных, пишет
run-манифест (params, data_range, git_commit, timestamp) и метрики
(WR, PF, avg_win, avg_loss, max_dd, n_trades) в БД experiment tree + JSON-файл.

Использование:
    python3 -m bybit_ws.experiments.backtest_runner --symbol SOLUSDT --days 60
    python3 -m bybit_ws.experiments.backtest_runner --symbol SOLUSDT --days 90 \
        --params params.json --end-date 2026-09-15
    python3 -m bybit_ws.experiments.backtest_runner --symbol SOLUSDT --days 60 --json

Критерий воспроизводимости: два прогона с одинаковыми inputs (symbol, days,
interval, end-date, params) дают идентичные метрики — благодаря фиксированному
окну и кэшу свечей в engine.fetch_klines().
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
from bybit_ws.experiments.store import ExperimentStore  # noqa: E402


def load_params(path: str | None) -> dict:
    if not path:
        return dict(engine.DEFAULT_PARAMS)
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError("params.json должен быть JSON-объектом {param: value}")
    merged = dict(engine.DEFAULT_PARAMS)
    merged.update(raw)
    return merged


def run_one(symbol: str, days: int, interval: str, end_date: str | None,
            params: dict, balance: float) -> dict:
    """Один воспроизводимый прогон: данные → симуляция → метрики → манифест."""
    end_ms = engine.resolve_end_ms(end_date)
    start_ms = end_ms - days * 86400 * 1000

    klines = engine.fetch_klines(symbol, interval, start_ms, end_ms)
    trades, equity_curve = engine.simulate(klines, symbol, balance, params)
    metrics = engine.compute_metrics(trades, equity_curve, balance, interval)

    data_range = {
        "symbol": symbol,
        "interval": interval,
        "days": days,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "start_date": engine._fmt_ms(start_ms),
        "end_date": engine._fmt_ms(end_ms),
        "n_candles": len(klines),
    }
    manifest = engine.build_manifest(
        params=params,
        data_range=data_range,
        commit=engine.git_commit(),
        kline_hash=engine.data_hash(klines),
        timestamp_iso=datetime.now(timezone.utc).isoformat(),
    )
    return {"manifest": manifest, "metrics": metrics}


def print_metrics(symbol: str, manifest: dict, metrics: dict, elapsed: float):
    dr = manifest["data_range"]
    pf = metrics["profit_factor"]
    pf_s = "∞" if pf is None and metrics["wins"] > 0 else (f"{pf:.2f}" if pf is not None else "0.00")
    print(f"\n{'='*64}")
    print(f"  📊 {symbol} | {dr['days']}д | {dr['interval']} | {dr['start_date']}..{dr['end_date']}")
    print(f"{'='*64}")
    print(f"  git_commit:   {manifest['git_commit'] or 'n/a'}")
    print(f"  data_hash:    {manifest['data_hash'][:16]}… (свечей: {dr['n_candles']})")
    print(f"  {'─'*60}")
    print(f"  Сделок:       {metrics['n_trades']} ({metrics['wins']}W / {metrics['losses']}L)")
    print(f"  Win Rate:     {metrics['win_rate']:.2f}%")
    print(f"  Profit Factor:{pf_s}")
    print(f"  PnL:          ${metrics['total_pnl']:+,.2f} ({metrics['total_pnl_pct']:+.2f}%)")
    print(f"  avg_win:      ${metrics['avg_win']:+,.4f} ({metrics['avg_win_pct']:+.2f}%)")
    print(f"  avg_loss:     ${metrics['avg_loss']:+,.4f} ({metrics['avg_loss_pct']:+.2f}%)")
    print(f"  Max DD:       {metrics['max_drawdown_pct']:.2f}%")
    print(f"  Sharpe:       {metrics['sharpe_ratio']:.2f}")
    print(f"  ⏱️  {elapsed:.1f}с")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Reproducible backtest — обёртка над paper_trade")
    p.add_argument("--symbol", required=True, help="Тикер (SOLUSDT, BTCUSDT, ...)")
    p.add_argument("--days", type=int, default=60, help="Глубина истории (дней)")
    p.add_argument("--interval", default="D", help="D, 240, 60, ...")
    p.add_argument("--end-date", default=None,
                   help="Конец окна YYYY-MM-DD (детерминированный; по умолчанию — floor сейчас)")
    p.add_argument("--params", default=None, help="Путь к params.json (вариант параметров)")
    p.add_argument("--balance", type=float, default=1000.0, help="Начальный депозит")
    p.add_argument("--experiment", default=None, help="Имя узла experiment (по умолчанию авто)")
    p.add_argument("--hypothesis", default=None, help="Гипотеза эксперимента")
    p.add_argument("--parent", type=int, default=None, help="parent_id узла experiment")
    p.add_argument("--db", default=None, help="Путь к experiments.db (по умолчанию дефолтная)")
    p.add_argument("--no-db", action="store_true", help="Не писать в БД (только вывод)")
    p.add_argument("--json", action="store_true", help="JSON-вывод результата")
    args = p.parse_args(argv)

    params = load_params(args.params)
    exp_name = args.experiment or f"{args.symbol}_{args.days}d_{args.interval}"

    start = time.time()
    result = run_one(args.symbol, args.days, args.interval,
                     args.end_date, params, args.balance)
    elapsed = time.time() - start

    manifest, metrics = result["manifest"], result["metrics"]

    run_id = None
    record = None
    exp_id = None
    manifest_path = None
    if not args.no_db:
        store = ExperimentStore(args.db)
        exp_id = store.create_experiment(
            name=exp_name, params=params,
            hypothesis=args.hypothesis, parent_id=args.parent,
        )
        run_id = store.add_run(exp_id, kind="backtest",
                               inputs_manifest=manifest, metrics=metrics)
        record = {
            "run_id": run_id,
            "experiment_id": exp_id,
            "experiment": exp_name,
            "kind": "backtest",
            "inputs_manifest": manifest,
            "metrics": metrics,
            "verdict": None,
        }
        manifest_path = engine.write_run_file(run_id, record)
        store.close()

    if args.json:
        print(json.dumps({"experiment": exp_name, "run_id": run_id,
                          "manifest": manifest, "metrics": metrics},
                         indent=2, ensure_ascii=False))
    else:
        print_metrics(args.symbol, manifest, metrics, elapsed)
        if record is not None:
            print(f"\n  💾 run #{run_id} записан → {manifest_path}")
            print(f"     (эксперимент: {exp_name}, id={exp_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
