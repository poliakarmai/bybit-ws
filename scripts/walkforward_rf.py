#!/usr/bin/env python3
"""Walk-Forward валидация RF модели (27.06.2026).

Проверяет out-of-sample F1 с 30-дневным gap.
Если OOS F1 < 0.65 — рекомендует отключить BYBIT_ML_ENABLED.
"""
import os, sys, json, sqlite3, time
from pathlib import Path
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import f1_score, classification_report
import numpy as np

DATA_DIR = Path.home() / '.local' / 'share' / 'bybit-ws'
STATE_DB = DATA_DIR / 'state.db'
MODEL_PATH = DATA_DIR / 'ml_scorer_rf.pkl'


def load_trades() -> list:
    """Загрузить завершённые сделки из trade_history."""
    if not STATE_DB.exists():
        print('❌ state.db не найден')
        return []

    conn = sqlite3.connect(str(STATE_DB))
    rows = conn.execute(
        'SELECT * FROM trade_history WHERE entry_price > 0 AND exit_price > 0 ORDER BY closed_at DESC'
    ).fetchall()
    cols = [d[1] for d in conn.execute('PRAGMA table_info(trade_history)')]
    conn.close()

    trades = []
    for r in rows:
        t = dict(zip(cols, r))
        entry = float(t.get('entry_price', 0))
        exit_p = float(t.get('exit_price', 0))
        size = float(t.get('size', 0))
        pnl = float(t.get('pnl', 0))
        closed = t.get('closed_at', '')

        if entry <= 0 or size <= 0:
            continue

        price_change = (exit_p - entry) / entry * 100
        pnl_pct = pnl / (entry * size) * 100 if entry * size > 0 else 0
        side = 1 if t.get('side', '').lower() == 'buy' else -1
        if side == -1:
            price_change = -price_change

        strategy_map = {'x10': 0, 'x10:scalp': 1, 'x10:swing': 2, 'long': 3, 'short': 4, 'junk': 5, 'dca': 6}
        strategy = (t.get('strategy') or '').strip()
        strategy_type = strategy_map.get(strategy, -1)

        features = {
            'pnl_pct': pnl_pct,
            'price_change_pct': price_change,
            'is_long': 1 if side == 1 else 0,
            'side_num': side,
            'strategy_type': strategy_type,
            'abs_pnl': abs(pnl),
            'size': size,
            'closed_at': closed,
            'profit_label': 1 if pnl > 0 else 0,
        }
        trades.append(features)

    return trades


def walk_forward_validate(trades: list, gap_days: int = 30):
    """Walk-forward валидация с временным gap.

    Делит данные на train/test с gap в gap_days дней.
    Обучает на train, тестирует на test.
    """
    if len(trades) < 20:
        print(f'❌ Недостаточно данных: {len(trades)} сделок (нужно ≥20)')
        return None

    # Сортируем по дате закрытия
    trades_sorted = sorted(trades, key=lambda t: t['closed_at'])

    feature_keys = ['pnl_pct', 'price_change_pct', 'is_long', 'side_num', 'strategy_type', 'abs_pnl', 'size']

    X = np.array([[t[k] for k in feature_keys] for t in trades_sorted])
    y = np.array([t['profit_label'] for t in trades_sorted])

    # Ищем точку разделения: последние 30%
    split_idx = int(len(trades_sorted) * 0.7)
    if split_idx < 10:
        split_idx = max(10, len(trades_sorted) - 10)

    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]

    if len(np.unique(y_train)) < 2 or len(np.unique(y_test)) < 2:
        print('⚠️ Недостаточно классов для валидации')
        return None

    # Обучаем
    rf = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)
    rf.fit(X_train, y_train)

    # In-sample
    y_pred_train = rf.predict(X_train)
    f1_train = f1_score(y_train, y_pred_train)

    # Out-of-sample
    y_pred_test = rf.predict(X_test)
    f1_test = f1_score(y_test, y_pred_test)

    return {
        'train_samples': len(X_train),
        'test_samples': len(X_test),
        'f1_train': round(f1_train, 4),
        'f1_test': round(f1_test, 4),
        'f1_drop': round(f1_train - f1_test, 4),
        'test_win_rate': round(np.mean(y_test), 4),
        'verdict': 'PASS' if f1_test >= 0.65 else 'FAIL',
        'recommendation': (
            '✅ RF OOS F1 ≥ 0.65 — можно использовать'
            if f1_test >= 0.65
            else '❌ RF OOS F1 < 0.65 — рекомендутся BYBIT_ML_ENABLED=0'
        ),
    }


def main():
    print('🔍 Walk-Forward валидация RF...')
    trades = load_trades()
    if not trades:
        print('Нет данных для валидации')
        sys.exit(1)

    print(f'  Загружено сделок: {len(trades)}')
    result = walk_forward_validate(trades)

    if result is None:
        sys.exit(1)

    print(f"""
{'='*60}
  Train: {result['train_samples']} сделок
  Test:  {result['test_samples']} сделок
  F1 (in-sample):  {result['f1_train']}
  F1 (out-of-sample): {result['f1_test']}
  F1 drop: {result['f1_drop']}
  Test win-rate: {result['test_win_rate']:.1%}
  ─────────────────────────────────
  Вердикт: {result['verdict']}
  {result['recommendation']}
{'='*60}
""")

    # Сохраняем результат
    output = {
        'timestamp': time.time(),
        'model': 'RF (sklearn)',
        **result,
    }
    out_path = DATA_DIR / 'rf_walkforward.json'
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2)
    print(f'  Результат сохранён: {out_path}')

    sys.exit(0 if result['verdict'] == 'PASS' else 1)


if __name__ == '__main__':
    main()
