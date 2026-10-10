# Experiment Tree — дизайн воспроизводимых экспериментов (bybit-ws)

**Автор:** Макс · **Дата:** 2026-09-16 · **Статус:** реализовано (прототип)

## 1. Зачем

Walk-Forward (16.09.2026) показал, что стратегия `auto` в ноль (PF 1.01, 216
сделок), а self-learning v10 переобучается на малой выборке. Поиск эджа вёлся
прямо на live-аккаунте, где каждый прогон неповторим (живые данные, живые
позиции). Это ТЗ выносит поиск эджа в **изолированное, воспроизводимое дерево
экспериментов** (по мотивам OpenResearch: experiment tree + reproducible
backtest + parallel hypothesis exploration).

Принципы:

1. **Каждый прогон = immutable запись.** Параметры, диапазон данных, git-commit,
   время, отпечаток входных свечей (`data_hash`) фиксируются в манифесте.
2. **Воспроизводимость = кэш + фиксированное окно.** Два прогона с одинаковыми
   inputs читают один и тот же кэшированный массив свечей → байт-в-байт
   идентичные метрики.
3. **Изоляция от live-контура.** Всё живёт в `bybit_ws/experiments/` и в
   собственной БД; боевой `~/.local/share/bybit-ws/state.db` не пишется.

## 2. Модель узлов

### 2.1 `experiment` — узел дерева

| Поле | Тип | Описание |
|------|-----|----------|
| `id` | INTEGER PK | авто-инкремент |
| `parent_id` | INTEGER FK→experiment(id) | родитель в дереве (вариант-потомок) |
| `name` | TEXT UNIQUE | человекочитаемое имя (напр. `SOLUSDT_365d__rr3_sl5`) |
| `params_json` | TEXT (JSON) | параметры варианта (`{rr_ratio, sl_pct, min_score, …}`) |
| `hypothesis` | TEXT | гипотеза («SL уже → чаще TP, выше PF») |
| `status` | TEXT | `active` / `archived` / `discarded` |
| `created_at` | INTEGER | unix-секунды |

### 2.2 `run` — прогон

| Поле | Тип | Описание |
|------|-----|----------|
| `id` | INTEGER PK | авто-инкремент |
| `experiment_id` | INTEGER FK→experiment(id) | владелец |
| `kind` | TEXT | `backtest` / `canary` / `live` |
| `inputs_manifest` | TEXT (JSON) | `params`, `data_range`, `git_commit`, `timestamp`, `data_hash` |
| `metrics_json` | TEXT (JSON) | `WR, PF, avg_win, avg_loss, max_dd, n_trades` (+ PnL, Sharpe) |
| `verdict` | TEXT | `promote` / `discard` / `inconclusive` / NULL |
| `created_at` | INTEGER | unix-секунды |

`kind` разводит три уровня доверия: `backtest` (исторические свечи, этот пакет),
`canary` (A/B на live, уже есть в self_learn), `live` (полный контур). Текущий
прототип пишет только `backtest`; `canary`/`live` — интерфейс под будущее.

## 3. SQLite DDL (отдельная `experiments.db`)

DDL реализован в `bybit_ws/experiments/store.py` (класс `ExperimentStore`,
паттерн скопирован из `state_db.py::StateDB` — WAL, `busy_timeout=5000`,
`executescript(SCHEMA)`):

```sql
CREATE TABLE IF NOT EXISTS experiment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES experiment(id) ON DELETE SET NULL,
    name TEXT NOT NULL UNIQUE,
    params_json TEXT NOT NULL,
    hypothesis TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiment(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    inputs_manifest TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    verdict TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_experiment ON run(experiment_id);
CREATE INDEX IF NOT EXISTS idx_run_kind ON run(kind);
CREATE INDEX IF NOT EXISTS idx_experiment_parent ON experiment(parent_id);
```

### Почему отдельная БД, а не `state.db`

1. **SSOT не трогаем на запись.** `state.db` — источник истины live-контура
   (`~/.local/share/bybit-ws/state.db`, пишется `state_db.py`). Эксперименты
   append-only и шумные: тысячи run-строк раздуют WAL и добавят write-контеншн
   главному циклу.
2. **Разный жизненный цикл.** Experiment-tree можно архивировать/дропать целиком,
   не рискуя боевыми данными.
3. **Прямое требование ТЗ** (жёсткое ограничение №5): боевой `state.db` на запись
   не трогать.

Путь: `~/.local/share/bybit-ws/experiments/experiments.db` (backtest_runner) и
`.../experiments/sandbox.db` (parallel_sandbox). Переопределяется `--db`.

## 4. Связь с существующим кодом

| Существующий модуль | Функция/объект | Роль в дереве |
|---------------------|----------------|----------------|
| `paper_trade.py` | `PaperExchange`, `calc_bb()`, `calc_atr()`, `score_coin()`, `MIN_SCORE`, `DEFAULT_SL_PCT`, `TAKER_FEE`, `SLIPPAGE` | **Источник backtest-run'ов.** `engine.simulate()` переиспользует эти строительные блоки (вход на Lower BB по 6-метричному скорингу, SL/TP, fee 0.055% + slippage 0.05%). НЕ вызывает `paper_trade.run_backtest()` напрямую — та берёт `end_ms = time.time()` (недетерминированно). |
| `bybit_ws/journal/self_learn.py` | `apply_journal_insights(journal, cfg)` | **Потребитель вердиктов.** Хук для промоушена победившего варианта: после того как `run.verdict = 'promote'`, параметры варианта уходят в `apply_journal_insights` (или напрямую в `save_params_version`). Сейчас вызывается из `main_async.py`; встраивание хука experiment-tree — следующий шаг, не в этом прототипе (live-контур не трогаем). |
| `bybit_ws/journal/self_learn.py` | `walk_forward_validation(new_params, historical_trades, db_path)` | 70/30 сплит-валидация новых параметров на `state.db` — аналог backtest-прогона, но на сделках. Experiment-tree даёт более строгий источник `historical_trades`. |
| `bybit_ws/journal/self_learn.py` | `save_params_version(params, reason, parent_version)` + `PARAMS_HISTORY_DIR` | **Legacy-версионирование** `~/.local/share/bybit-ws/params_history/v*.json` (`{version, parent, timestamp, params, reason}`) + `HEAD.json`. Experiment-tree повторяет эту же `parent`-семантику, но в реляционной форме и с привязкой run-метрик. |
| `bybit_ws/journal/adapter.py` | `load_from_sqlite(db_path)` | Читает `state.db → trade_history` (стратегия `auto`) и строит roundtrips. Read-only источник для сравнения backtest-метрик с реальными. |
| `state_db.py` | `StateDB` | Паттерн SQLite (WAL / busy_timeout / executescript), скопирован в `ExperimentStore`. |

### Данные (read-only)

- Свечи: `bybit_ws.api.bybit('GET', '/v5/market/kline?...')` — те же данные, что
  тянет `paper_trade.py`. Кэш в `~/.local/share/bybit-ws/experiments/klines_cache/`.
- Сделки для сравнения: `bybit_ws/journal/adapter.load_from_sqlite()`.

## 5. Поток данных (backtest)

```
backtest_runner CLI
  └─ resolve_end_ms(end_date)            # фиксированное окно
  └─ engine.fetch_klines(...)            # Bybit → кэш (ключ: symbol/interval/start/end)
  └─ engine.simulate(klines, params)     # PaperExchange + calc_bb/calc_atr/score_coin
  └─ engine.compute_metrics(...)         # WR, PF, avg_win, avg_loss, max_dd, n_trades
  └─ build_manifest(...)                 # params + data_range + git_commit + timestamp + data_hash
  └─ ExperimentStore.create_experiment() + add_run(kind='backtest')
  └─ engine.write_run_file()             # JSON-артефакт runs/run_{id}.json
```

## 6. Механизм воспроизводимости

1. **Фиксированное окно.** `--end-date YYYY-MM-DD` → `end_ms` = 00:00 следующего
   дня UTC; без него — floor «сейчас» до часа. `start_ms = end_ms − days·86400e3`.
2. **Кэш свечей.** `fetch_klines` читает/пишет JSON по ключу
   `{symbol}_{interval}_{start_ms}_{end_ms}`. Повторный прогон с теми же inputs
   читает кэш → идентичные входные данные.
3. **Детерминированная симуляция.** Чистый Python, без `random`, без ML, без
   живых ордеров. `PaperExchange` и скоринг не имеют скрытого состояния.
4. **Отпечаток.** `data_hash = sha256(свечей)` попадает в манифест — прогоны с
   одинаковым hash обязаны давать одинаковые метрики.

Проверено: два прогона `backtest_runner --symbol SOLUSDT --days 365 --end-date
2026-09-15` дали байт-в-байт идентичные метрики (см. вывод CLI).

## 7. Параллельный sandbox

`parallel_sandbox.py` берёт N вариантов параметров (SL/TP мультипликаторы,
порог скоринга), гоняет на **одном и том же** диапазоне истории (общий
`fetch_klines`), каждый вариант — изолированное состояние (свой `experiment` +
`run`, никакого общего мутабельного состояния; симуляция в памяти). Вывод —
сравнительная таблица (WR, PF, PnL, avg_win/avg_loss, max_dd, Sharpe),
ранжированная по `--rank-by` (по умолчанию PF).

Изоляция БД: песочница пишет в `sandbox.db` (отдельно от `experiments.db` и от
боевого `state.db`).

## 8. Что НЕ сделано (границы прототипа)

- Хук «промоушен победителя → self_learn» (`run.verdict` → `apply_journal_insights`
  / `save_params_version`) — отложен: требует правки live-контура, запрещённой ТЗ.
- `canary`/`live` kind — только в DDL, интерфейс под будущее.
- Оптимизация (Optuna/grid-search поверх дерева) — дерево даёт substrate, сам
  поиск — следующий шаг.
