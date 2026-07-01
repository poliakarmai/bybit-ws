"""
optuna_tuner.py — Авто-подбор параметров стратегий через Optuna (Фаза 5.2).

Для каждого тикера отдельно:
  - Загружает историю klines через Bybit REST API
  - Подбирает параметры: BB-период, BB std множитель, SL%, TP%, min_score
  - Целевая функция: максимизация суммарного PnL × winrate
  - Сохраняет результаты в ~/.config/bybit-ws/optuna_params.json

CLI:
  python -m bybit_ws.optuna_tuner --symbol LINKUSDT --trials 100
  python -m bybit_ws.optuna_tuner --all --trials 50

Feature flag: BYBIT_OPTUNA_ENABLED (env, default 0)
При BYBIT_OPTUNA_ENABLED=1 main_async.py загружает optuna_params.json и переопределяет
дефолтные параметры входа (только для новых входов, существующие позиции не трогаются).
"""

import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import optuna
    _OPTUNA_AVAILABLE = True
except ImportError:
    optuna = None  # type: ignore
    _OPTUNA_AVAILABLE = False

# ── Пути ──────────────────────────────────────────────────────────────────────
HOME = Path.home()
CONFIG_DIR = HOME / '.config' / 'bybit-ws'
OPTUNA_CONFIG = CONFIG_DIR / 'optuna_params.json'
BYBIT_CLI = HOME / '.local' / 'bin' / 'bybit'

# ── Дефолтный список тикеров для оптимизации ─────────────────────────────────
AUTO_ENTRY_WATCH = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'LTCUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT',
    'HYPEUSDT', 'NEARUSDT', 'SUIUSDT', 'TONUSDT', 'WLDUSDT', 'LINKUSDT',
    'AAVEUSDT', 'AVAXUSDT', 'DOTUSDT', 'INJUSDT', 'ONDOUSDT', 'ARBUSDT',
    'ENAUSDT', 'FETUSDT', 'APTUSDT', 'ATOMUSDT', 'RUNEUSDT',
]

# ── GRID-диапазоны для Optuna ────────────────────────────────────────────────
SEARCH_SPACE = {
    'bb_period':       (10, 50),       # BB-период (int)
    'bb_std_mult':     (1.5, 3.0),     # BB std-множитель (float)
    'sl_pct':          (0.02, 0.10),   # SL% ниже входа (2-10%)
    'tp_pct':          (0.05, 0.30),   # TP% выше входа (5-30%)
    'min_score':       (10, 40),       # Мин. скоринговый балл для входа
}


# ═══════════════════════════════════════════════════════════════════════════════
# Загрузка исторических данных
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_klines(symbol: str, interval: str = 'D', limit: int = 300) -> list[dict]:
    """Загружает klines через bybit REST API. Возвращает старые→новые."""
    try:
        if not re.fullmatch(r'^[A-Z0-9]+$', symbol):
            raise ValueError(f'Invalid symbol: {symbol}')
        r = subprocess.run(
            [str(BYBIT_CLI), 'raw', 'GET',
             f'/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}'],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return []
        data = json.loads(r.stdout)
        raw_list = data.get('result', {}).get('list', [])
        if not raw_list:
            return []
        klines = []
        for item in raw_list:
            if not isinstance(item, list) or len(item) < 5:
                continue
            try:
                klines.append({
                    'open': float(item[1]), 'high': float(item[2]),
                    'low': float(item[3]), 'close': float(item[4]),
                })
            except (ValueError, TypeError):
                continue
        klines.reverse()
        return klines
    except Exception as e:
        print(f'[optuna] fetch_klines error for {symbol}: {e}', file=sys.stderr)
    return []


def calc_bb(closes: list[float], period: int = 20, std_mult: float = 2.0) -> dict | None:
    """Вычисляет Bollinger Bands с заданным периодом и std-множителем."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((x - sma) ** 2 for x in window) / period
    std = math.sqrt(variance)
    return {
        'lower': sma - std_mult * std,
        'middle': sma,
        'upper': sma + std_mult * std,
        'width': (2 * std_mult * std / sma * 100) if sma > 0 else 0,
        'pos': ((closes[-1] - (sma - std_mult * std)) / (2 * std_mult * std) * 100) if std > 0 else 50,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Бэктест с заданными параметрами (используется как objective для Optuna)
# ═══════════════════════════════════════════════════════════════════════════════

def backtest_score(
    klines: list[dict],
    bb_period: int = 20,
    bb_std_mult: float = 2.0,
    sl_pct: float = 0.07,
    tp_pct: float = 0.20,
    min_score: int = 25,
    bb_threshold: float = 25.0,
) -> dict:
    """
    Walk-forward бэктест Bollinger Grid.
    Входит при BB% < bb_threshold (LONG-сигнал), закрывается по SL или TP.
    Возвращает статистику: trades, wins, win_rate, total_pnl, avg_pnl.
    """
    closes = [k['close'] for k in klines]
    trades = []
    in_position = False
    entry_price = 0.0
    sl_price = 0.0
    tp_price = 0.0
    entry_idx = 0

    for i in range(bb_period, len(klines)):
        k = klines[i]

        bb = calc_bb(closes[: i + 1], bb_period, bb_std_mult)
        if not bb:
            continue

        if not in_position:
            # Вход при BB% ниже порога (сигнал LONG) и достаточный score
            if bb['pos'] < bb_threshold and bb['width'] > 1:
                entry_price = bb['lower'] * 0.97  # вход чуть ниже нижней полосы
                tp_price = entry_price * (1 + tp_pct)
                sl_price = entry_price * (1 - sl_pct)
                in_position = True
                entry_idx = i
        else:
            # Проверка SL
            if k['low'] <= sl_price:
                pnl = (sl_price / entry_price - 1) * 100
                trades.append({'pnl': pnl, 'outcome': 'SL', 'bars': i - entry_idx})
                in_position = False
            # Проверка TP
            elif k['high'] >= tp_price:
                pnl = (tp_price / entry_price - 1) * 100
                trades.append({'pnl': pnl, 'outcome': 'TP', 'bars': i - entry_idx})
                in_position = False

    # Если позиция осталась открытой — закрываем по последней цене
    if in_position and entry_price > 0:
        last_price = closes[-1]
        pnl = (last_price / entry_price - 1) * 100
        trades.append({'pnl': pnl, 'outcome': 'OPEN_CLOSE', 'bars': len(klines) - entry_idx})

    n = len(trades)
    if n == 0:
        return {'trades': 0, 'wins': 0, 'losses': 0, 'win_rate': 0.0,
                'total_pnl': 0.0, 'avg_pnl': 0.0, 'max_win': 0.0, 'max_loss': 0.0}

    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    pnls = [t['pnl'] for t in trades]
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / n
    wr = len(wins) / n * 100

    return {
        'trades': n,
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round(wr, 1),
        'total_pnl': round(total_pnl, 2),
        'avg_pnl': round(avg_pnl, 2),
        'max_win': round(max(pnls), 2),
        'max_loss': round(min(pnls), 2),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Optuna Objective
# ═══════════════════════════════════════════════════════════════════════════════

def _make_objective(klines: list[dict]):
    """Фабрика objective-функции для одного тикера."""

    def objective(trial: optuna.Trial) -> float:
        """Целевая функция Optuna: максимизация total_pnl × win_rate."""
        bb_period = trial.suggest_int('bb_period',
                                       SEARCH_SPACE['bb_period'][0],
                                       SEARCH_SPACE['bb_period'][1])
        bb_std_mult = trial.suggest_float('bb_std_mult',
                                           SEARCH_SPACE['bb_std_mult'][0],
                                           SEARCH_SPACE['bb_std_mult'][1])
        sl_pct = trial.suggest_float('sl_pct',
                                      SEARCH_SPACE['sl_pct'][0],
                                      SEARCH_SPACE['sl_pct'][1])
        tp_pct = trial.suggest_float('tp_pct',
                                      SEARCH_SPACE['tp_pct'][0],
                                      SEARCH_SPACE['tp_pct'][1])
        min_score = trial.suggest_int('min_score',
                                       SEARCH_SPACE['min_score'][0],
                                       SEARCH_SPACE['min_score'][1])

        result = backtest_score(
            klines,
            bb_period=bb_period,
            bb_std_mult=bb_std_mult,
            sl_pct=sl_pct,
            tp_pct=tp_pct,
            min_score=min_score,
        )

        # Минимальные требования: хотя бы 3 сделки
        if result['trades'] < 3:
            return -999.0

        # Композитный скор: total_pnl × win_rate (в процентах)
        # Добавляем sqrt(trades) чтобы поощрять больше сделок при равных условиях
        score = result['total_pnl'] * (result['win_rate'] / 100.0) * math.sqrt(result['trades'])

        # Сохраняем атрибуты для анализа
        trial.set_user_attr('trades', result['trades'])
        trial.set_user_attr('win_rate', result['win_rate'])
        trial.set_user_attr('total_pnl', result['total_pnl'])
        trial.set_user_attr('avg_pnl', result['avg_pnl'])

        return score  # Optuna максимизирует (direction='maximize')

    return objective


# ═══════════════════════════════════════════════════════════════════════════════
# Оптимизация одного тикера
# ═══════════════════════════════════════════════════════════════════════════════

def optimize_single(
    symbol: str,
    trials: int = 100,
    interval: str = 'D',
    candles: int = 300,
    timeout: int = 300,
    show_progress: bool = True,
) -> dict | None:
    """
    Optuna-оптимизация параметров для одного тикера.

    Аргументы:
        symbol: тикер (например 'LINKUSDT')
        trials: количество испытаний Optuna
        interval: таймфрейм ('D', '4h', '1h')
        candles: количество свечей для загрузки
        timeout: таймаут оптимизации (сек)
        show_progress: показывать прогресс-бар tqdm

    Возвращает:
        dict с оптимальными параметрами или None при ошибке
    """
    print(f'\n🔍 {symbol}: загрузка {candles} свечей ({interval})...')
    klines = fetch_klines(symbol, interval, candles)

    if len(klines) < 50:
        print(f'  ❌ {symbol}: недостаточно данных ({len(klines)} свечей)')
        return None

    print(f'  ✅ {len(klines)} свечей загружено. Старт Optuna ({trials} trials)...')

    # Создаём study с sampler'ом TPE (Tree-structured Parzen Estimator)
    study = optuna.create_study(
        study_name=f'optuna_{symbol}_{interval}',
        direction='maximize',
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_warmup_steps=10),
    )

    objective_fn = _make_objective(klines)

    t0 = time.time()
    study.optimize(
        objective_fn,
        n_trials=trials,
        timeout=timeout,
        show_progress_bar=show_progress,
        n_jobs=1,  # однопоточно — безопасно для bybit API
    )
    elapsed = time.time() - t0

    if not study.best_trial:
        print(f'  ❌ {symbol}: не найдено валидных параметров')
        return None

    best = study.best_params
    best_trial = study.best_trial
    best_value = study.best_value
    attrs = best_trial.user_attrs

    print(f'  🏆 {symbol}: best_value={best_value:.2f} ({trials} trials за {elapsed:.1f}с)')
    print(f'     bb_period={best["bb_period"]}  bb_std={best["bb_std_mult"]:.2f}  '
          f'SL={best["sl_pct"]*100:.1f}%  TP={best["tp_pct"]*100:.1f}%  '
          f'min_score={best["min_score"]}')
    print(f'     trades={attrs.get("trades", "?")}  WR={attrs.get("win_rate", "?")}%  '
          f'total_pnl={attrs.get("total_pnl", "?")}%  avg_pnl={attrs.get("avg_pnl", "?")}%')

    return {
        'symbol': symbol,
        'interval': interval,
        'bb_period': best['bb_period'],
        'bb_std_mult': round(best['bb_std_mult'], 2),
        'sl_pct': round(best['sl_pct'], 4),
        'tp_pct': round(best['tp_pct'], 4),
        'min_score': best['min_score'],
        'backtest_trades': attrs.get('trades', 0),
        'backtest_win_rate': attrs.get('win_rate', 0.0),
        'backtest_total_pnl': attrs.get('total_pnl', 0.0),
        'backtest_avg_pnl': attrs.get('avg_pnl', 0.0),
        'optuna_value': round(best_value, 2),
        'optuna_trials': len(study.trials),
        'optimized_at': datetime.now().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Оптимизация всех тикеров
# ═══════════════════════════════════════════════════════════════════════════════

def optimize_all(
    symbols: list[str] | None = None,
    trials: int = 50,
    interval: str = 'D',
    candles: int = 300,
    timeout_per_symbol: int = 300,
) -> dict[str, dict]:
    """
    Запуск оптимизации для списка тикеров.
    Сохраняет результаты в ~/.config/bybit-ws/optuna_params.json.
    """
    if symbols is None:
        symbols = AUTO_ENTRY_WATCH

    results = {}
    total_ok = 0
    total_fail = 0
    t_start = time.time()

    for i, sym in enumerate(symbols):
        print(f'\n[{i+1}/{len(symbols)}] {sym}...')
        try:
            r = optimize_single(sym, trials=trials, interval=interval,
                                candles=candles, timeout=timeout_per_symbol)
            if r:
                results[sym] = {
                    'bb_period': r['bb_period'],
                    'bb_std_mult': r['bb_std_mult'],
                    'sl_pct': r['sl_pct'],
                    'tp_pct': r['tp_pct'],
                    'min_score': r['min_score'],
                    'backtest_trades': r['backtest_trades'],
                    'backtest_win_rate': r['backtest_win_rate'],
                    'backtest_total_pnl': r['backtest_total_pnl'],
                    'backtest_avg_pnl': r['backtest_avg_pnl'],
                    'optuna_value': r['optuna_value'],
                    'optimized_at': r['optimized_at'],
                }
                total_ok += 1
            else:
                total_fail += 1
        except Exception as e:
            print(f'  ❌ {sym}: {e}')
            total_fail += 1

        # Rate limit между тикерами
        if i < len(symbols) - 1:
            time.sleep(0.5)

    total_elapsed = time.time() - t_start

    # Сохраняем результаты
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(OPTUNA_CONFIG, 'w') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f'\n{"="*60}')
    print(f'✅ Оптимизация завершена: {total_ok} OK, {total_fail} FAIL '
          f'за {total_elapsed:.0f}с')
    print(f'📁 Результаты: {OPTUNA_CONFIG}')

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Загрузка сохранённых параметров (используется в main_async.py)
# ═══════════════════════════════════════════════════════════════════════════════

_optuna_cache: dict[str, dict] | None = None


def load_optuna_params() -> dict:
    """
    Загрузить оптимизированные параметры из optuna_params.json.
    Кешируется в памяти (вызывается при старте main_async.py).
    Возвращает dict: {symbol: {bb_period, bb_std_mult, sl_pct, tp_pct, min_score, ...}}
    """
    global _optuna_cache
    if _optuna_cache is not None:
        return _optuna_cache

    if not OPTUNA_CONFIG.exists():
        _optuna_cache = {}
        return _optuna_cache

    try:
        with open(OPTUNA_CONFIG) as f:
            _optuna_cache = json.load(f)
    except Exception as e:
        print(f'[optuna] Ошибка загрузки {OPTUNA_CONFIG}: {e}')
        _optuna_cache = {}

    assert _optuna_cache is not None
    return _optuna_cache


def get_symbol_params(symbol: str) -> dict | None:
    """
    Получить Optuna-параметры для конкретного символа.
    Возвращает dict или None если символ не оптимизирован.
    """
    params = load_optuna_params()
    return params.get(symbol)


def is_optuna_enabled() -> bool:
    """Проверить feature flag BYBIT_OPTUNA_ENABLED."""
    return os.environ.get('BYBIT_OPTUNA_ENABLED', '0') == '1'


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Optuna-оптимизатор параметров стратегий (Фаза 5.2)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python -m bybit_ws.optuna_tuner --symbol LINKUSDT --trials 100
  python -m bybit_ws.optuna_tuner --all --trials 50
  python -m bybit_ws.optuna_tuner --symbols LINKUSDT,SOLUSDT,ADAUSDT --trials 50
  python -m bybit_ws.optuna_tuner --show LINKUSDT
        """,
    )
    parser.add_argument(
        '--symbol', type=str, default=None,
        help='Одиночный тикер для оптимизации (например LINKUSDT)',
    )
    parser.add_argument(
        '--symbols', type=str, default=None,
        help='Список тикеров через запятую (например LINKUSDT,SOLUSDT,ADAUSDT)',
    )
    parser.add_argument(
        '--all', action='store_true', default=False,
        help='Оптимизировать все тикеры из watchlist',
    )
    parser.add_argument(
        '--show', type=str, default=None,
        help='Показать сохранённые параметры для тикера',
    )
    parser.add_argument(
        '--trials', type=int, default=100,
        help='Количество испытаний Optuna на тикер (default: 100)',
    )
    parser.add_argument(
        '--interval', type=str, default='D',
        choices=['D', 'W', '4h', '1h', '15m'],
        help='Таймфрейм (default: D)',
    )
    parser.add_argument(
        '--candles', type=int, default=300,
        help='Количество свечей для загрузки (default: 300)',
    )
    parser.add_argument(
        '--timeout', type=int, default=300,
        help='Таймаут оптимизации на тикер в секундах (default: 300)',
    )

    args = parser.parse_args()

    # Режим просмотра
    if args.show:
        params = load_optuna_params()
        if args.show in params:
            print(f'\n📊 Сохранённые параметры для {args.show}:')
            print(json.dumps(params[args.show], indent=2, ensure_ascii=False))
        else:
            print(f'❌ {args.show}: нет сохранённых параметров')
            print(f'   Доступны: {", ".join(sorted(params.keys())) if params else "нет"}')
        return 0

    # Режим оптимизации
    if args.symbol:
        # Одиночный тикер
        r = optimize_single(
            args.symbol,
            trials=args.trials,
            interval=args.interval,
            candles=args.candles,
            timeout=args.timeout,
        )
        if r is None:
            print(f'❌ {args.symbol}: оптимизация не дала результатов')
            return 1
        # Сохраняем одиночный результат
        existing = load_optuna_params()
        existing[args.symbol] = {k: v for k, v in r.items() if not k.startswith('_')}
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(OPTUNA_CONFIG, 'w') as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        print(f'\n✅ {args.symbol}: параметры сохранены в {OPTUNA_CONFIG}')
        return 0

    elif args.symbols:
        symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]
        optimize_all(symbols, trials=args.trials, interval=args.interval,
                     candles=args.candles, timeout_per_symbol=args.timeout)
        return 0

    elif args.all:
        optimize_all(trials=args.trials, interval=args.interval,
                     candles=args.candles, timeout_per_symbol=args.timeout)
        return 0

    else:
        parser.print_help()
        return 0


if __name__ == '__main__':
    sys.exit(main())
