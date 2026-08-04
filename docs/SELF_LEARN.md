# Self-Learning Module — bybit-ws v10

> Автономное самообучение торговой стратегии. Без участия человека.
> Обновлено: 2026-08-04. Верифицировано на реальных данных: 246 trades, WR=57%, DD=$284.

## Как это работает

Бот торгует 24/7. После каждой сделки — микро-обучение. Раз в 6 часов (или ≥10 сделок) — полный анализ с генерацией корректировок. Модуль прошёл путь от простого анализа (v4) до production-grade adaptive системы с 20+ механиками (v10).

### Микро-обучение (после каждой сделки)

| Действие | Зачем | v |
|----------|-------|---|
| Bandit posterior update | Thompson Sampling учится какие параметры лучше | v9 |
| Outlier protection (>3σ) | Flash crash не ломает обучение | v10 |
| Regime-aware drift detector | Проверка: стратегия не сломалась? | v9 |
| Streak protection | 3 убытка → половинный размер | v4 |
| Symbol profile update | Статистика по каждой монете | v4 |

### Полный анализ (раз в 6 часов)

**1. Composite Score** — одна цифра (0-1): «насколько хорошо я торгую»

| Метрика | Вес | Текущее значение |
|---------|-----|-----------------|
| Win Rate | 30% | 57% (weighted, decay) |
| Profit Factor | 25% | 0.75 |
| Sharpe | 20% | -0.045 |
| Max Drawdown | 15% | $284 |
| Avg Hold | 10% | 12h |

> Веса адаптивные: в тренде важнее WR, в кризис/чоппи — MaxDD (30-40%).
> Exponential decay: старые сделки весят меньше (100 дней = 37% веса).

**2. Режим рынка меняет настройки**

| Режим | min_score | SL | TP | Направление |
|-------|-----------|-----|------|-------------|
| Тренд ВВЕРХ | 25 | 5% | ×1.5 | LONG |
| Тренд ВНИЗ | 35 | 5% | ×0.8 | SHORT |
| Боковик | 30 | 5% | ×1.0 | BOTH |
| Чоппи | 40 | 4% | ×0.7 | NONE |

**3. Thompson Sampling** — 5 рук с разными параметрами. Dynamic Bandit: авто-прунинг худших + генерация вариаций лучшей каждые 24ч. Uncertainty-aware: explore при высокой variance.

**4. Drift Detector** — per-regime окна (HIGH_VOL=30, RANGING=100). EMA baseline. Causal inference: если BTC упал + WR упал → MARKET, если BTC стабилен → PARAMETERS.

**5. Стресс-тесты** — 4 исторических + 1000 Pareto MC (α=2.5, heavy-tail). Порог: prob_ruin < 5%.

**6. Canary** — 10% входов (адаптивный 5-20%). Bayesian A/B тест. Idle timeout 3ч.

## Архитектура

```
main_async.py (event-driven: ≥10 сделок / 6ч или 24ч fallback)
  └─ should_run_self_learn_v6()
       └─ load_from_sqlite() → analyze()
            └─ apply_journal_insights()
                 │
                 ├─ ON EACH TRADE (micro):
                 │    robust_bandit_update()          ← outlier 3σ (v10)
                 │    RegimeAwareDriftDetector         ← per-regime (v9)
                 │    symbol_profiles update           ← incremental (v9)
                 │
                 └─ EVERY 6H (batch):
                      weighted_composite_score()       ← decay (v10)
                      composite_score_v8()             ← adaptive weights (v8)
                      causal_analysis()                ← market vs params (v10)
                      detect_anomalous_trades()        ← IQR filter (v8)
                      monte_carlo_stress_test_v9()     ← Pareto MC (v9)
                      stress_test_params()             ← 4 crash scenarios (v7)
                      DynamicParameterBandit           ← auto-prune (v9)
                      CoordinatedEnsemble              ← regime handover (v10)
                      select_arm_with_uncertainty()    ← explore/exploit (v10)
                      walk_forward_validation()        ← out-of-sample (v6)
                      bayesian_ab_test()               ← Beta posterior (v6)
```

## Файлы

| Файл | Назначение |
|------|-----------|
| `journal/self_learn.py` | Ядро v10: Dynamic Bandit, Pareto MC, Drift, Ensemble, Causal |
| `journal/analyzer.py` | Профиль + 4 bias-диагностики |
| `journal/adapter.py` | SQLite → нормализованные сделки |
| `canary_state.json` | Состояние canary-эксперимента |
| `self_learn.jsonl` | Лог всех корректировок (98 записей) |
| `symbol_profiles.json` | Per-symbol статистика |
| `exit_stats.jsonl` | Причины закрытия (86 записей) |
| `parameter_ensemble.json` | Состояние 6 bandits |
| `params_history/v*.json` | Git-like версии параметров |
| `global_params_log.json` | Снепшоты для rollback |

## Диагностика

```bash
# Все self-learn логи
grep -E "Self-learn|Composite|Bandit|MC-v9|Causal|Anomaly|Decay" \
  ~/.local/share/bybit-ws/events.log | tail -10

# Ensemble состояние (все 6 bandits)
python3 -c "
from bybit_ws.journal.self_learn import load_ensemble
for r, b in load_ensemble().get_all_best().items():
    if b.get('trades', 0) > 0:
        print(f'{r}: {b[\"params\"]} WR={b[\"wr\"]:.0%} trades={b[\"trades\"]}')
"

# Drift detector (per-regime)
python3 -c "
from bybit_ws.journal.self_learn import get_regime_drift_detector
import json; print(json.dumps(get_regime_drift_detector().get_status(), indent=2))
"

# Pareto Monte Carlo
python3 -c "
from bybit_ws.journal.self_learn import monte_carlo_stress_test_v9, get_params_for_regime
import json; print(json.dumps(monte_carlo_stress_test_v9(get_params_for_regime(), 500), indent=2))
"

# Causal analysis
python3 -c "
from bybit_ws.journal.self_learn import causal_analysis
import sqlite3
db='/home/openclaw/.local/share/bybit-ws/state.db'
conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
trades=[dict(r) for r in conn.execute('SELECT pnl,closed_at FROM trade_history WHERE closed_at IS NOT NULL ORDER BY entry_at')]
conn.close()
print(causal_analysis(trades))
"

# Exponential decay WR
python3 -c "
from bybit_ws.journal.self_learn import weighted_wr, weighted_composite_score
import sqlite3
db='/home/openclaw/.local/share/bybit-ws/state.db'
conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
trades=[dict(r) for r in conn.execute('SELECT pnl,closed_at,hold_hours FROM trade_history WHERE closed_at IS NOT NULL ORDER BY entry_at')]
conn.close()
print(f'Weighted WR: {weighted_wr(trades):.1%}')
cs=weighted_composite_score(trades)
print(f'Composite: score={cs[\"score\"]} WR={cs[\"wr\"]:.1%} PF={cs[\"pf\"]} DD=\${cs[\"max_dd\"]:.0f}')
"

# Ручной сброс canary
echo '{"active":false}' > ~/.local/share/bybit-ws/canary_state.json
```

## Эволюция версий

| v | Дата | Ключевые механики | Строк кода |
|---|------|-------------------|-----------|
| v4 | 01.08 | Per-symbol profiles, exit tracking, streak guard | ~650 |
| v5 | 04.08 | Wall-clock trigger, regime stats, cluster | +200 |
| v6 | 04.08 | Bayesian A/B, shrinkage, SL time, walk-forward, feature importance | +300 |
| v7 | 04.08 | Composite score, regime params, stress test, drawdown, adaptive canary | +250 |
| v8 | 04.08 | Thompson Sampling, Monte Carlo, Drift, Anomaly, adaptive weights, versioning | +430 |
| v9 | 04.08 | Dynamic Bandit, Pareto MC, Regime Drift, Ensemble, Micro-updates | +470 |
| v10 | 04.08 | Robust updates, Exponential decay, Uncertainty Thompson, Coordinated, Causal | +200 |

## Сводная таблица механик

| Механика | v4 | v5 | v6 | v7 | v8 | v9 | v10 |
|---|---|---|---|---|---|---|---|
| A/B тест | — | — | Bayesian | Bayesian | Thompson | Thompson | +Uncertainty |
| Canary | 10% | 10% | 10% | Adaptive | Bandit | Dynamic | Dynamic |
| Стресс-тест | — | — | — | 4 hist | 1000 MC | Pareto MC | Pareto MC |
| Дрифт | — | — | — | — | ADWIN | Regime | +Causal |
| Скор | — | — | WR+CI | Fixed w | Adaptive | Adaptive | +Decay |
| Режим | — | Stats | Stats | Params | Weights | Ensemble | +Coordinated |
| Обновления | Batch | Batch | Batch | Batch | Batch | Micro | +Robust |
