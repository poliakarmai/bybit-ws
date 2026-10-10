# ТЗ: Experiment Tree + Reproducible Backtest + Parallel Sandbox (bybit-ws)

**Исполнитель:** Макс
**Дата:** 2026-09-16
**Статус:** в работе

## Контекст

bybit-ws торгует по Bollinger Grid, есть self-learning v10 (Thompson Sampling, bandit,
drift, ensemble, canary A/B). Walk-Forward (16.09.2026) показал: стратегия `auto`
в ноль (PF 1.01, 216 сделок), self-learn переобучается на малой выборке, асимметрия
SL/TP 1:2.4. Нужно вынести поиск эджа с live-аккаунта на воспроизводимое дерево
экспериментов (по мотивам OpenResearch: experiment tree + reproducible backtest +
parallel hypothesis exploration).

## Жёсткие ограничения

1. **НЕ трогать live-контур** (read-only): `main_async.py`, `auto_entry.py`,
   `auto_short.py`, `unified_sl.py`, `auto_tp.py`, `risk_manager.py`, `rpc.py`.
2. Всё новое — в отдельном пакете `bybit_ws/experiments/`.
3. Python 3.12, SQLite (паттерн как в `state_db.py`). Без новых тяжёлых зависимостей.
4. Никаких реальных ордеров — только история/бэктест.
5. Боевой `~/.local/share/bybit-ws/state.db` НЕ трогать на запись. Для экспериментов —
   отдельная БД или read-only.

## Пункт 1 — Дизайн-док «experiment tree»

Файл: `bybit-ws/docs/experiment-tree-design.md`

Содержание:
- **Модель узлов:** `experiment` (id, parent_id, params_json, hypothesis, status, created_at).
- **Модель прогонов:** `run` (id, experiment_id, kind ∈ {backtest,canary,live},
  inputs_manifest, metrics_json, verdict, created_at).
- **SQLite DDL** для новых таблиц (отдельный `experiments.db` или в state.db — обосновать выбор).
- **Связь с существующим:** `params_history/v*.json` (legacy), `journal/self_learn.py`
  (потребитель вердиктов), `paper_trade.py` (источник backtest-runs). Указать конкретные
  функции/файлы, где встраиваются хуки.

## Пункт 2 — Прототип reproducible backtest

Файл: `bybit_ws/experiments/backtest_runner.py`

- Обёртка над `paper_trade.py`: принимает параметры-вариант + диапазон данных, пишет
  run-манифест `(params, data_range, git_commit, timestamp)`.
- Каждый прогон = immutable запись в БД (run-строка) + метрики
  `(WR, PF, avg_win, avg_loss, max_drawdown, n_trades)`.
- CLI: `python3 -m bybit_ws.experiments.backtest_runner --symbol X --days 90 --params params.json`
- **Критерий воспроизводимости:** два прогона с одинаковыми inputs дают идентичные метрики.

## Пункт 3 — Параллельный sandbox-прогон

Файл: `bybit_ws/experiments/parallel_sandbox.py`

- Берёт N вариантов параметров (напр. разные SL/TP мультипликаторы, пороги скоринга,
  RSI-фильтры), гоняет на одинаковом диапазоне истории.
- Каждый вариант — изолированное состояние (свой workdir/БД, не боевой state.db).
- Вывод: сравнительная таблица (WR, PF, PnL, avg_win/avg_loss, max_dd по каждому варианту),
  ранжированная по PF/Sharpe.

## Критерии готовности

- [ ] Дизайн-док ссылается на реальные файлы/функции, DDL валиден.
- [ ] `backtest_runner` запускается, пишет run-манифест, воспроизводим.
- [ ] `parallel_sandbox` выдаёт сравнительную таблицу N вариантов.
- [ ] `git diff` не трогает боевые модули (только `docs/` + `bybit_ws/experiments/`).
- [ ] `python3 -m py_compile` проходит для новых файлов.

## Результат

Прислать: список созданных файлов, вывод CLI (таблица сравнения вариантов),
подтверждение что live-контур не изменён (git status).
