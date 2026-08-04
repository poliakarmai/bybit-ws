# Self-Learning Module — bybit-ws v8

> Автономное самообучение торговой стратегии. Без участия человека.
> Обновлено: 2026-08-04.

## Архитектура

```
main_async.py (event-driven: ≥10 сделок / 6ч или 24ч fallback)
  └─ should_run_self_learn_v6()          ← проверка условий
       └─ mark_self_learn_run()          ← запись времени
            └─ load_from_sqlite()         ← adapter.py: SQLite → analyzer
                 └─ analyze()              ← analyzer.py: профиль + bias
                      └─ apply_journal_insights()
                           ├─ anomaly filter        ← IQR outlier detection (v8)
                           ├─ composite_score_v8    ← adaptive per-regime weights (v8)
                           ├─ Thompson Sampling     ← multi-armed bandit (v8)
                           ├─ Monte Carlo stress    ← 1000 random crash sims (v8)
                           ├─ drift detector        ← ADWIN-based (v8)
                           ├─ param versioning      ← Git-like snapshots (v8)
                           ├─ walk-forward          ← out-of-sample (v6)
                           ├─ Bayesian A/B          ← Beta posterior (v6)
                           ├─ regime params         ← per-regime tuning (v7)
                           ├─ drawdown adjustment   ← risk-aware (v7)
                           └─ stress test           ← 4 historical scenarios (v7)
```

## V8 — Новое (04.08.2026)

| # | Механика | Как работает |
|---|----------|-------------|
| 1 | **Thompson Sampling** | 3 руки Beta(α,β). Выбор по max sampled reward. Авто-balance explore/exploit |
| 2 | **Monte Carlo Stress** | 1000 синтетических crash: BTC -20..-60%, alt 1.2-2×, vol 2-8× |
| 3 | **Drift Detector** | Скользящее окно 100. WR drop >5% + подтверждение 20 → DRIFT |
| 4 | **Anomaly Detection** | IQR 3×: экстремальный PnL или hold >100ч → исключение из обучения |
| 5 | **Adaptive Weights** | 6 режимов × свои веса composite_score (TRENDING_DOWN: dd=40%) |
| 6 | **Param Versioning** | `params_history/v001.json` → `v002.json` → HEAD.json |

### Thompson Sampling

```python
bandit = ParameterBandit([
    {"min_score": 25, "sl_pct": 5.0, "tp_mult": 1.5},  # рука 0
    {"min_score": 30, "sl_pct": 5.0, "tp_mult": 1.2},  # рука 1
    {"min_score": 35, "sl_pct": 4.0, "tp_mult": 1.0},  # рука 2
])
params, idx = bandit.select_arm()   # Thompson sample
bandit.update(idx, win=True)        # обновить Beta posterior
best = bandit.get_best_arm()        # лучшая рука по mean
```

### Monte Carlo Stress Test

| Параметр | Распределение |
|----------|--------------|
| BTC drop | U(−20%, −60%) |
| Alt/BTC ratio | U(1.2×, 2.0×) |
| Duration | U(1h, 72h) |
| Vol spike | U(2×, 8×) |

Результат: `prob_ruin` — вероятность потери >50% капитала. Порог: <5%.

### Drift Detector

```
окно 100 сделок → current_wr
базовый wr (при инициализации)
если baseline - current > 5% И последние 20 сделок WR < 30% → DRIFT
действие: ×1.5 min_score, ×0.5 position size, canary=0%
авто-сброс через 48ч если восстановились
```

### Adaptive Composite Weights

| Режим | WR вес | PF вес | Sharpe | MaxDD | Hold |
|-------|--------|--------|--------|-------|------|
| TRENDING_UP | 35% | 20% | 20% | 15% | 10% |
| TRENDING_DOWN | 20% | 25% | 15% | **30%** | 10% |
| RANGING | 25% | **30%** | 20% | 15% | 10% |
| HIGH_VOL | 15% | 20% | 15% | **40%** | 10% |
| LOW_VOL | **30%** | 25% | **25%** | 10% | 10% |
| CHOPPY | 20% | 20% | 15% | **35%** | 10% |

> В тренде — WR важнее. Во флэте — Profit Factor. В кризис/чоппи — MaxDD.

## Полная таблица механик (v4→v8)

| Механика | v4 | v5 | v6 | v7 | v8 |
|---|---|---|---|---|---|
| A/B тест | — | — | Bayesian Beta | Bayesian Beta | **Thompson Sampling** |
| Canary % | 10% fixed | 10% fixed | 10% fixed | Adaptive 5-20% | **Bandit explore/exploit** |
| Стресс-тест | — | — | — | 4 historical | **1000 Monte Carlo** |
| Дрифт | — | — | — | — | **ADWIN detector** |
| Аномалии | — | — | — | — | **IQR filter** |
| Скор | — | — | WR+CI | Fixed weights | **Adaptive per-regime** |
| Версионирование | — | — | — | — | **Git-like snapshots** |
| Режим | — | Stats only | Stats only | Per-regime params | **Per-regime weights** |
| Валидация | — | — | Walk-forward | +Stress test | **+Monte Carlo** |

## Диагностика

```bash
# Thompson Sampling — состояние bandit
python3 -c "
from bybit_ws.journal.self_learn import load_bandit
b = load_bandit()
for i, a in enumerate(b.arms):
    wr = a['alpha']/(a['alpha']+a['beta'])
    print(f'arm {i}: {a[\"params\"]} | WR={wr:.1%} trades={a[\"trades\"]}')
print('best:', b.get_best_arm())
"

# Monte Carlo stress test
python3 -c "
from bybit_ws.journal.self_learn import monte_carlo_stress_test, get_params_for_regime
import json
params = get_params_for_regime()
print(json.dumps(monte_carlo_stress_test(params, 500), indent=2))
"

# Drift detector status
python3 -c "
from bybit_ws.journal.self_learn import get_drift_detector
print(get_drift_detector().get_status())
"

# Anomaly detection
python3 -c "
from bybit_ws.journal.self_learn import detect_anomalous_trades
import sqlite3, json
db = '/home/openclaw/.local/share/bybit-ws/state.db'
rows = [dict(r) for r in sqlite3.connect(db).execute(
    'SELECT pnl, hold_hours FROM trade_history WHERE closed_at IS NOT NULL LIMIT 100'
).fetchall()]
result = detect_anomalous_trades(rows)
print(f'normal={result[\"normal_count\"]} anomalous={result[\"anomalous_count\"]}')
"

# Adaptive composite score
python3 -c "
from bybit_ws.journal.self_learn import composite_score_v8
trades = [{'pnl':5},{'pnl':-3},{'pnl':8},{'pnl':-2},{'pnl':10}]
print(composite_score_v8(trades))
"

# Param version history
python3 -c "
from bybit_ws.journal.self_learn import get_params_history
import json
for v in get_params_history(10):
    print(f'{v[\"version\"]}: {v[\"reason\"][:60]}')
"
```

## Версионирование

| Версия | Дата | Ключевые изменения |
|--------|------|-------------------|
| v8 | 04.08.2026 | Thompson Sampling, Monte Carlo stress, Drift Detector, Anomaly filter, Adaptive weights, Param versioning |
| v7 | 04.08.2026 | Composite score, regime params, stress testing, drawdown, adaptive canary |
| v6 | 04.08.2026 | Bayesian A/B, shrinkage, SL time, event trigger, rollback, feature importance |
| v5 | 04.08.2026 | Wall-clock trigger, regime-aware stats, cluster learning, idle timeout |
| v4 | 01.08.2026 | Per-symbol profiles, exit tracking, session params, streak guard |
