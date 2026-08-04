# Self-Learning Module — bybit-ws v6

> Автономное самообучение торговой стратегии. Без участия человека.
> Обновлено: 2026-08-04.

## Архитектура

```
main_async.py (event-driven: ≥10 сделок за 6ч или 24ч fallback)
  └─ should_run_self_learn_v6()     ← проверка условий
       └─ mark_self_learn_run()     ← запись времени
            └─ load_from_sqlite()    ← adapter.py: SQLite → analyzer
                 └─ analyze()         ← analyzer.py: профиль + bias
                      └─ apply_journal_insights()  ← коррекция параметров
                           ├─ Bayesian shrinkage ← сдвиг к cluster WR
                           ├─ SL time analysis   ← quick vs slow SL
                           ├─ Walk-forward validation ← проверка на истории
                           ├─ Global rollback check ← авто-откат при падении WR
                           ├─ Feature importance ← вклад фильтров
                           └─ canary: Bayesian A/B testing
```

## V6 — Новое (04.08.2026)

| # | Механика | Описание |
|---|----------|----------|
| 1 | **Bayesian A/B testing** | P(canary > baseline) через Beta-распределение. Порог: >0.95 promote, <0.05 rollback |
| 2 | **Bayesian shrinkage** | Per-symbol → cluster mean. 5 сделок = 25% веса, 20 сделок = 100% |
| 3 | **SL time analysis** | Quick SL (<30мин) = плохие входы. Slow SL (>4ч) = tight SL |
| 4 | **Event-driven trigger** | ≥10 сделок за 6ч ИЛИ 24ч fallback |
| 5 | **Global rollback** | WR drop >15% → авто-откат к предыдущим параметрам |
| 6 | **Feature importance** | WR pass vs fail по каждому фильтру (MTF, Orderbook, Volume...) |
| 7 | **Confidence intervals** | Wilson score CI для WR (95%) |
| 8 | **Walk-forward validation** | 70/30 split, проверка параметров на out-of-sample |
| 9 | **Human-readable explanations** | «min_score 15→19: wr=0.35» вместо JSON |

## Файлы

| Файл | Назначение |
|------|-----------|
| `journal/self_learn.py` | Ядро: canary, per-symbol, exit tracking, streak |
| `journal/analyzer.py` | Анализ истории: профиль, 4 bias-диагностики |
| `journal/adapter.py` | Загрузка из SQLite → нормализованные сделки |
| `canary_state.json` | Состояние canary-эксперимента |
| `canary_entries.jsonl` | Канареечные входы для матчинга |
| `self_learn.jsonl` | Лог всех корректировок |
| `symbol_profiles.json` | Per-symbol параметры |
| `exit_stats.jsonl` | Статистика причин закрытия |
| `loss_streak.json` | Счётчик серий убытков |
| `self_learn_state.json` | Таймер последнего запуска |

## Механики

### 1. Canary Mode

10% входов используют экспериментальные параметры. Остальные 90% — baseline.

```
Запуск: apply_journal_insights() генерирует adjustments
  → canary active=true, started_at=now
  → 6 часов или 10 сделок
  → Финал: promote (WR ок) или rollback (WR упал >10%)
  → NEW v5: idle timeout 3ч (0 сделок → авто-откат)
```

**Параметры:**
- `CANARY_ENTRY_PCT = 0.10` — доля канареечных входов
- `CANARY_WINDOW_HOURS = 6` — длительность эксперимента
- `CANARY_WR_DROP_THRESHOLD = 0.10` — порог отката
- `CANARY_IDLE_TIMEOUT_HOURS = 3` — авто-откат без сделок

### 2. Wall-Clock Trigger (v5)

Самообучение запускается по реальному времени, а не по cycle count.
Cycle count сбрасывается при рестартах → self-learn мог не запускаться сутками.

```
should_run_self_learn():
  читает self_learn_state.json
  если прошло < 6ч → skip
  иначе → run + mark_self_learn_run()
```

### 3. Per-Symbol Profiles

Каждая монета получает индивидуальные параметры на основе своей истории:

| Параметр | Логика |
|----------|--------|
| `min_score` | WR < 30% → +5, макс 35 |
| `sl_pct` | avg_hold < 2ч → tighter (-1), > 24ч → wider (+1) |
| `max_hold_hours` | avg_hold × 1.5 |

**Порог:** ≥ 5 сделок на символ (v5, было 8).

### 4. Cluster-Aware Learning (v5)

Если на символ < 5 сделок — используем статистику кластера волатильности:

| Кластер | Монеты |
|---------|--------|
| `high_vol` | BTC, ETH, SOL |
| `mid_vol` | AVAX, LINK, ADA, DOT, MATIC |
| `low_vol` | XRP, LTC, BNB, TRX |

### 5. Exit Reason Tracking

Анализирует ПОЧЕМУ закрылись позиции:

- `SL > 60%` → SL слишком tight → +1.5% к sl_pct
- `TP > 50%` → можно расширить TP → +0.2 к tp_mult

### 6. Regime-Aware Stats (v5)

Раздельная статистика по рыночным режимам (LSTM):

```
📊 Regime stats: TRENDING_DOWN:150t/35%WR, TRENDING_UP:60t/68%WR, RANGING:34t/41%WR
```

Позволяет понять что WR=35% в TRENDING_DOWN — возможно норма для SHORT, а не баг.

### 7. Streak Protection

- 3 убытка подряд → cooldown 4ч, половинный размер
- 5 убытков подряд → cooldown 24ч, блок входов

### 8. Session Modifiers

| Сессия | min_score | max_positions | tp_mult |
|--------|-----------|---------------|---------|
| Asia | ×1.0 | ×1.0 | ×0.8 |
| Europe | ×0.9 | ×1.0 | ×1.0 |
| US | ×0.85 | ×1.2 | ×1.2 |

## Диагностика

```bash
# Состояние canary
cat ~/.local/share/bybit-ws/canary_state.json | python3 -m json.tool

# Последние корректировки
tail -5 ~/.local/share/bybit-ws/self_learn.jsonl

# Статистика закрытий
tail -5 ~/.local/share/bybit-ws/exit_stats.jsonl

# Per-symbol профили
cat ~/.local/share/bybit-ws/symbol_profiles.json | python3 -m json.tool

# Таймер последнего запуска
cat ~/.local/share/bybit-ws/self_learn_state.json

# Regime-aware stats (из Python)
python3 -c "
from bybit_ws.journal.self_learn import get_regime_aware_stats
import json
print(json.dumps(get_regime_aware_stats(), indent=2))
"

# Кластерная статистика
python3 -c "
from bybit_ws.journal.self_learn import get_cluster_stats
import json
print(json.dumps(get_cluster_stats(), indent=2))
"
```

## Ручной сброс canary

```bash
echo '{"active":false,"params":{},"baseline":{},"started_at":null,"canary_trades":0,"canary_wins":0,"baseline_wr":0,"promoted":false,"rolled_back":false,"history":[],"symbol_params":{},"session_params":{}}' > ~/.local/share/bybit-ws/canary_state.json
```

## Версионирование

| Версия | Дата | Изменения |
|--------|------|-----------|
| v6 | 04.08.2026 | Bayesian A/B, shrinkage, SL time, event-driven trigger, global rollback, feature importance, CI, walk-forward, explanations |
| v5 | 04.08.2026 | Wall-clock trigger, regime-aware stats, cluster learning, idle timeout |
| v4 | 01.08.2026 | Per-symbol profiles, exit tracking, session params, streak guard |
| v3 | 07.2026 | Canary mode, post-trade clustering |
