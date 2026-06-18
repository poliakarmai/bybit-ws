#!/usr/bin/env python3
"""
Walk-forward validation для ML Gate (RandomForest).
Проверяет out-of-sample F1 с time-series split'ом.
Запуск: python3 walk_forward_validate.py
"""
import json, os, sys, time
from pathlib import Path
import numpy as np

DATA_DIR=Path.home()/'.local'/'share'/'bybit-ws'
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from ml_scorer import _extract_features, MODEL_PATH
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import f1_score, precision_score, recall_score
except ImportError as e:
    print(f'❌ Зависимости не установлены: {e}')
    sys.exit(1)

# Загружаем сигналы из БД
try:
    import sqlite3
    db = Path.home()/'.local'/'share'/'gridsignal-bot'/'users.db'
    conn = sqlite3.connect(str(db))
    rows = conn.execute('SELECT signal_data, outcome FROM trade_signals WHERE outcome IS NOT NULL ORDER BY created_at').fetchall()
    conn.close()
except Exception:
    rows = []

if len(rows) < 20:
    print(f'⚠️ Недостаточно данных: {len(rows)} сделок (нужно ≥20)')
    sys.exit(0)

signals = []
for row in rows:
    try:
        sig = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        sig['outcome'] = 1 if row[1] == 'TP' else 0
        signals.append(sig)
    except Exception:
        pass

X, y = _extract_features(signals)
print(f'📊 Данные: {len(signals)} сделок, TP={sum(y)}/{len(y)} ({sum(y)/len(y)*100:.1f}%)')

# Walk-forward: 5 фолдов по времени
tscv = TimeSeriesSplit(n_splits=min(5, len(signals)//5))
f1_scores = []
pr_scores = []
re_scores = []

for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
    Xt, Xv = X[train_idx], X[test_idx]
    yt, yv = y[train_idx], y[test_idx]

    if len(set(yv)) < 2:
        continue

    model = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42, class_weight='balanced')
    model.fit(Xt, yt)
    yp = model.predict(Xv)

    f1 = f1_score(yv, yp)
    pr = precision_score(yv, yp, zero_division=0)
    re = recall_score(yv, yp, zero_division=0)
    f1_scores.append(f1)
    pr_scores.append(pr)
    re_scores.append(re)
    print(f'  Fold {fold+1}: F1={f1:.3f} P={pr:.3f} R={re:.3f} (train={len(train_idx)} test={len(test_idx)})')

print(f'\n📈 Walk-Forward F1: {np.mean(f1_scores):.3f} ± {np.std(f1_scores):.3f}')
print(f'   Precision:     {np.mean(pr_scores):.3f} ± {np.std(pr_scores):.3f}')
print(f'   Recall:        {np.mean(re_scores):.3f} ± {np.std(re_scores):.3f}')

# Сравнение с in-sample
if MODEL_PATH.exists():
    import joblib
    model_full = joblib.load(MODEL_PATH)
    yp_full = model_full.predict(X)
    f1_full = f1_score(y, yp_full)
    print(f'\n⚠️ In-sample F1 (обученная модель): {f1_full:.3f}')
    gap = abs(np.mean(f1_scores) - f1_full)
    if gap > 0.15:
        print(f'   🔴 Overfitting! Gap = {gap:.3f} (>0.15)')
    elif gap > 0.08:
        print(f'   🟡 Возможен overfitting. Gap = {gap:.3f}')
    else:
        print(f'   🟢 Gap OK ({gap:.3f})')
