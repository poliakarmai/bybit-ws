# Self-Learning Module — bybit-ws v9

> Автономное самообучение торговой стратегии. Без участия человека.
> Обновлено: 2026-08-04.

## Как это работает (человеческим языком)

Бот торгует 24/7. Раз в 6 часов (или когда накопится 10+ сделок) он анализирует свою историю и решает: «Что улучшить?» После каждой закрытой сделки — микро-обучение.

### Что происходит после каждой сделки

- Обновляется статистика по монете
- Обновляется bandit posterior (Thompson Sampling)
- Детектор дрейфа проверяет: не сломалась ли стратегия?
- Если 3 убытка подряд — снижается размер позиций

### Что происходит раз в 6 часов

**1. Считает Composite Score** — одну цифру от 0 до 1, которая говорит «насколько хорошо я торгую». Учитывает:
- Win Rate (30% веса)
- Profit Factor — сколько заработал на каждый потерянный доллар (25%)
- Sharpe — стабильность прибыли (20%)
- Max Drawdown — максимальная просадка (15%)
- Avg Hold — среднее время удержания (10%)

Веса адаптивные: в тренде важнее WR, в кризис/чоппи — MaxDD.

**2. Смотрит какие фильтры полезны**
MTF, Orderbook, Volume, EntryJudge — для каждого считает WR pass vs fail.

**3. Анализирует по режимам рынка**
LSTM определяет режим (тренд вверх/вниз/боковик/чоппи). Для каждого — своя статистика.

**4. Режим рынка меняет настройки**

| Режим | min_score | SL | TP | Направление |
|-------|-----------|-----|------|-------------|
| Тренд ВВЕРХ | 25 | 5% | ×1.5 | Только LONG |
| Тренд ВНИЗ | 35 | 5% | ×0.8 | Только SHORT |
| Боковик | 30 | 5% | ×1.0 | LONG+SHORT |
| Чоппи | 40 | 4% | ×0.7 | Ничего |

**5. Thompson Sampling — подбор параметров**

Вместо ручных настроек — 5 «рук» с разными параметрами. Бот пробует все, чаще дёргает лучшие, иногда — новые (вдруг стали лучше?). Раз в сутки выбрасывает 2 худшие руки и генерит 2 вариации лучшей. Отдельный bandit для каждого режима рынка.

**6. Drift Detector — «стратегия сломалась?»**

Отслеживает падение WR отдельно для каждого режима. Если в каком-то режиме WR упал на 10%+ → авто-консервативный режим (×1.5 min_score, ×0.5 размер).

**7. Стресс-тесты**

- 4 исторических сценария: COVID 2020, FTX 2022, China Ban 2021, Luna 2022
- 1000 случайных crash-симуляций (Pareto heavy-tail — крупные крахи чаще чем кажется)

Если вероятность разорения >5% — параметры не принимаются.

**8. Canary — тест на мышах**

Новые параметры применяются только к 10% входов (canary % адаптивный: 20% когда WR стабилен, 5% когда нестабилен). Через 6 часов сравнение: лучше → применяем, хуже → откат. Если 3 часа без сделок → авто-откат.

## Архитектура (код)

```
main_async.py (event-driven: ≥10 сделок / 6ч или 24ч fallback)
  └─ should_run_self_learn_v6()
       └─ load_from_sqlite() → analyze()
            └─ apply_journal_insights()
                 ├─ on_trade_closed()        ← микро-обучение (v9)
                 ├─ DynamicParameterBandit   ← авто-прунинг рук (v9)
                 ├─ ParameterEnsemble        ← per-regime bandits (v9)
                 ├─ monte_carlo_v9           ← Pareto MC (v9)
                 ├─ RegimeAwareDriftDetector ← per-regime окна (v9)
                 ├─ composite_score_v8       ← adaptive weights (v8)
                 ├─ detect_anomalous_trades  ← IQR filter (v8)
                 ├─ walk_forward_validation  ← out-of-sample (v6)
                 ├─ bayesian_ab_test         ← Beta posterior (v6)
                 └─ stress_test_params       ← 4 crash scenarios (v7)
```

## Файлы состояния

| Файл | Назначение |
|------|-----------|
| `canary_state.json` | Состояние canary |
| `self_learn.jsonl` | Лог всех корректировок |
| `symbol_profiles.json` | Статистика по монетам |
| `exit_stats.jsonl` | Причины закрытия |
| `loss_streak.json` | Серии убытков |
| `self_learn_state.json` | Таймер запуска |
| `regime_params.json` | Параметры по режимам |
| `parameter_ensemble.json` | Состояние всех bandits |
| `params_history/v*.json` | Git-like версии параметров |

## Диагностика

```bash
# Все логи v9
grep -E "Composite|Bandit|MC-v9|Regime|Micro|Anomaly" ~/.local/share/bybit-ws/events.log | tail -10

# Состояние ensemble (все bandits)
python3 -c "
from bybit_ws.journal.self_learn import load_ensemble
ens = load_ensemble()
for r, b in ens.get_all_best().items():
    if b.get('trades', 0) > 0:
        print(f'{r}: {b[\"params\"]} WR={b[\"wr\"]:.0%} trades={b[\"trades\"]}')
"

# Drift detector
python3 -c "
from bybit_ws.journal.self_learn import get_regime_drift_detector
import json
print(json.dumps(get_regime_drift_detector().get_status(), indent=2))
"

# Pareto Monte Carlo
python3 -c "
from bybit_ws.journal.self_learn import monte_carlo_stress_test_v9, get_params_for_regime
import json
print(json.dumps(monte_carlo_stress_test_v9(get_params_for_regime(), 500), indent=2))
"

# Ручной сброс canary (если завис)
echo '{"active":false}' > ~/.local/share/bybit-ws/canary_state.json
```

## Эволюция версий

| v | Ключевые механики |
|----|-------------------|
| v4 | Per-symbol profiles, exit tracking, streak protection |
| v5 | Wall-clock trigger, regime stats, cluster learning |
| v6 | Bayesian A/B, shrinkage, SL time, walk-forward, feature importance |
| v7 | Composite score, regime params, stress test, drawdown, adaptive canary |
| v8 | Thompson Sampling, Monte Carlo, Drift, Anomaly, adaptive weights, versioning |
| v9 | Dynamic Bandit, Pareto MC, Regime Drift, Ensemble, Micro-updates |
