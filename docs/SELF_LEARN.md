# Self-Learning Module — bybit-ws v10

> Автономное самообучение торговой стратегии. Без участия человека.
> Обновлено: 2026-08-04.

## Как это работает (человеческим языком)

Бот торгует 24/7. Раз в 6 часов анализирует свою историю и решает что улучшить. После каждой сделки — микро-обучение. Модуль прошёл путь от простого анализа до production-grade adaptive системы с 20+ механиками.

### Что происходит после каждой сделки

| Действие | Зачем |
|----------|-------|
| Обновление bandit posterior | Thompson Sampling учится какие параметры лучше |
| Защита от outlier (>3σ) | Flash crash не ломает обучение |
| Drift detector | Проверка: стратегия не сломалась? |
| Streak protection | 3 убытка → половинный размер позиций |
| Symbol profile update | Статистика по каждой монете |

### Что происходит раз в 6 часов

**1. Composite Score** — одна цифра (0-1): «насколько хорошо я торгую»

| Метрика | Вес | Что значит |
|---------|-----|-----------|
| Win Rate | 30% | Доля прибыльных сделок |
| Profit Factor | 25% | Сколько заработал на каждый потерянный доллар |
| Sharpe | 20% | Стабильность прибыли |
| Max Drawdown | 15% | Максимальная просадка |
| Avg Hold | 10% | Среднее время удержания |

> Веса адаптивные: в тренде важнее WR, в кризис/чоппи — MaxDD (30-40%).
> **v10:** старые сделки весят меньше через exponential decay (100 дней = 37% веса).

**2. Режим рынка меняет настройки**

| Режим | min_score | SL | TP | Направление |
|-------|-----------|-----|------|-------------|
| Тренд ВВЕРХ | 25 | 5% | ×1.5 | Только LONG |
| Тренд ВНИЗ | 35 | 5% | ×0.8 | Только SHORT |
| Боковик | 30 | 5% | ×1.0 | LONG+SHORT |
| Чоппи | 40 | 4% | ×0.7 | Ничего |

**3. Thompson Sampling — подбор параметров**

5 «рук» с разными параметрами. Бот пробует все, чаще дёргает лучшие. Раз в сутки выбрасывает 2 худшие и генерит 2 вариации лучшей. Отдельный bandit для каждого режима.

> **v10:** uncertainty-aware — если variance между руками высокая → explore (случайный выбор). При смене режима — blended params первые 10 сделок.

**4. Drift Detector — «стратегия сломалась?»**

Per-regime окна (HIGH_VOL=30, RANGING=100). EMA baseline. WR упал на 10%+ → консервативный режим.

> **v10:** causal inference — если BTC тоже упал → MARKET_CONDITIONS (не вина стратегии). Если BTC стабилен → PARAMETERS (надо чинить).

**5. Стресс-тесты**

- 4 исторических сценария (COVID 2020, FTX 2022, China Ban 2021, Luna 2022)
- 1000 случайных crash-симуляций (Pareto heavy-tail α=2.5)
- Порог: prob_ruin < 5% → параметры принимаются

**6. Canary — тест на мышах**

Новые параметры → только 10% входов (адаптивный: 5-20%). 6 часов → сравнение → promote или rollback. 3 часа без сделок → авто-откат.

## Архитектура (код)

```
main_async.py (event-driven: ≥10 сделок / 6ч или 24ч fallback)
  └─ should_run_self_learn_v6()
       └─ load_from_sqlite() → analyze()
            └─ apply_journal_insights()
                 ├─ ON EACH TRADE: on_trade_closed()
                 │    ├─ robust_bandit_update()        ← outlier 3σ (v10)
                 │    ├─ RegimeAwareDriftDetector       ← per-regime (v9)
                 │    └─ symbol_profiles update         ← incremental (v9)
                 ├─ DynamicParameterBandit              ← auto-prune (v9)
                 ├─ CoordinatedEnsemble                 ← regime handover (v10)
                 ├─ select_arm_with_uncertainty()       ← explore/exploit (v10)
                 ├─ weighted_composite_score()          ← decay weights (v10)
                 ├─ causal_analysis()                   ← market vs params (v10)
                 ├─ monte_carlo_stress_test_v9()        ← Pareto MC (v9)
                 ├─ composite_score_v8()                ← adaptive weights (v8)
                 ├─ detect_anomalous_trades()           ← IQR filter (v8)
                 ├─ walk_forward_validation()           ← out-of-sample (v6)
                 ├─ bayesian_ab_test()                  ← Beta posterior (v6)
                 └─ stress_test_params()                ← 4 crash scenarios (v7)
```

## Файлы состояния

| Файл | Назначение |
|------|-----------|
| `canary_state.json` | Состояние canary |
| `self_learn.jsonl` | Лог всех корректировок |
| `symbol_profiles.json` | Статистика по монетам |
| `exit_stats.jsonl` | Причины закрытия |
| `self_learn_state.json` | Таймер запуска |
| `regime_params.json` | Параметры по режимам |
| `parameter_ensemble.json` | Состояние всех bandits |
| `params_history/v*.json` | Git-like версии параметров |
| `global_params_log.json` | Снепшоты для rollback |

## Диагностика

```bash
# Все логи v10
grep -E "Composite|Bandit|MC-v9|Regime|Micro|Anomaly|Causal|Decay" ~/.local/share/bybit-ws/events.log | tail -10

# Ensemble состояние
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
import sqlite3, json
db='/home/openclaw/.local/share/bybit-ws/state.db'
trades=[dict(r) for r in sqlite3.connect(db).execute('SELECT pnl,closed_at FROM trade_history WHERE closed_at IS NOT NULL ORDER BY entry_at')]
print(causal_analysis(trades))
"

# Exponential decay WR
python3 -c "
from bybit_ws.journal.self_learn import weighted_wr, weighted_composite_score
import sqlite3
db='/home/openclaw/.local/share/bybit-ws/state.db'
trades=[dict(r) for r in sqlite3.connect(db).execute('SELECT pnl,closed_at,entry_at FROM trade_history WHERE closed_at IS NOT NULL ORDER BY entry_at')]
print(f'Weighted WR: {weighted_wr(trades):.1%}')
print(f'Weighted Composite: {weighted_composite_score(trades)[\"score\"]}')
"

# Ручной сброс canary
echo '{"active":false}' > ~/.local/share/bybit-ws/canary_state.json
```

## Полная эволюция

| v | Ключевые механики |
|----|-------------------|
| v4 | Per-symbol profiles, exit tracking, streak protection |
| v5 | Wall-clock trigger, regime stats, cluster learning |
| v6 | Bayesian A/B, shrinkage, SL time, walk-forward, feature importance |
| v7 | Composite score, regime params, stress test, drawdown, adaptive canary |
| v8 | Thompson Sampling, Monte Carlo, Drift, Anomaly, adaptive weights, versioning |
| v9 | Dynamic Bandit, Pareto MC, Regime Drift, Ensemble, Micro-updates |
| v10 | Robust updates, Exponential decay, Uncertainty Thompson, Coordinated Ensemble, Causal inference |

| Механика | v5 | v6 | v7 | v8 | v9 | v10 |
|---|---|---|---|---|---|---|
| A/B тест | — | Bayesian | Bayesian | Thompson | Thompson | +Uncertainty |
| Canary | 10% | 10% | Adaptive | Bandit | Dynamic | Dynamic |
| Стресс-тест | — | — | 4 hist | 1000 MC | Pareto MC | Pareto MC |
| Дрифт | — | — | — | ADWIN | Regime | +Causal |
| Скор | WR | WR+CI | Fixed w | Adaptive | Adaptive | +Decay |
| Режим | Stats | Stats | Params | Weights | Ensemble | +Coordinated |
| Обновления | Batch | Batch | Batch | Batch | Micro | +Robust |
