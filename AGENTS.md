# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Карта документации → [docs/INDEX.md](docs/INDEX.md).
> **Обновлено: 2026-10-28** — живые числа вынесены в команды (§ «Живые числа»), в тексте остались только инварианты и структура.
> Документы старше 25.10, помеченные «архив», не читать: они описывают удалённое поведение.

## Что это

Трейдинг-монитор Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT).  
Systemd-сервис `bybit-ws-async` (user unit, `ExecStart=/usr/bin/python3 -m bybit_ws.main_async`),
RSS и прочие метрики — в `GET /metrics` (значение плавает, в доке не хранится), SQLite — SSOT.

## Живые числа — командами, не из доки

Цифры сделок, PnL и комиссий меняются ежедневно, поэтому здесь их нет: снимок устаревает
за сутки и начинает врать. Любое число берётся командой в момент вопроса:

| Что | Откуда |
|-----|--------|
| Позиции, equity, режим | `GET /positions`, `/balance`, `/metrics` (Bearer из `kv_store['rpc_auth_token']`) |
| Позиции vs БД | `python3 reconcile.py --days N` → `~/.local/share/bybit-ws/reconcile-<дата>-<N>d.json` |
| PnL по сделкам | `sqlite3 ~/.local/share/bybit-ws/state.db "SELECT count(*), round(sum(pnl),2) FROM trade_history"` |
| Комиссии, maker-доля | `python3 -m bybit_ws.fee_stats --days 30` |
| Тесты | `python3 -m pytest -q` (весь проект) · `python3 test_smoke.py` (52 проверки) |
| Здоровье | `bash verify.sh` (11 проверок, exit-код) |

> **Правило:** число без команды и даты — не число. Пишешь цифру в док — рядом команда, которой её проверить.

## Инварианты учёта (не протухают)

> **Депозит и свободные средства — уровень СЧЁТА, не монеты USDT.**
> В UTA per-coin USDT-баланс уходит в минус, когда средства лежат в других монетах
> (30.10.2026: USDT −5.58 при equity $500.86) → бот 1.5 суток видел отрицательный
> депозит и молча блокировал ВСЕ входы (`💸 Депозит $-5.6 < $30`). Источник один —
> `api.account_wallet()` (`totalWalletBalance`/`totalEquity`/`totalAvailableBalance`);
> по монете не считать. Тесты: `tests/test_position_sizing_config.py::test_deposit_uses_account_total_not_usdt_coin`,
> `tests/test_audit2_fixes.py::test_wallet_balance_uses_account_totals`.

> ⚠️ **`side` в `trade_history` — сторона ЗАКРЫВАЮЩЕГО ордера, а не позиции.**
> `side='Buy'` закрывает SHORT, `side='Sell'` закрывает LONG. В таблице `positions`
> (`/positions`) наоборот — сырой Bybit: `Buy`=LONG, `Sell`=SHORT. Фильтр шортов —
> `auto_short.py:357` (`side=='Buy'`).

> 🧾 **Три денежных потока — не складывать вслепую.**
> | источник | что это |
> |---|---|
> | `trade_history.pnl` | Bybit `closedPnl`: нетто по сделке, **без фандинга и без комиссии открытия** |
> | `funding_log.cash_flow` | фандинг из `transaction-log` `SETTLEMENT` (сумма в поле `change`, `cashFlow='0'`) |
> | `transaction-log.cashFlow` тип `TRADE` | **gross** PnL по сделкам (без комиссий) |
> | `transaction-log.change` тип `TRADE` | **net**: gross минус комиссии открытия и закрытия |
>
> Тождество проверяется точно: `change` = `cashFlow` − комиссии. «PnL аккаунта» =
> `change` TRADE + фандинг. `Σ pnl` из БД ≠ `change` — это разные величины; вычитать
> оценочные комиссии из `pnl` нельзя (двойной счёт, закрывающая комиссия уже внутри).

> **manual-фильтр:** `trade_history.manual` отделяет авто-входы от ручных. `/enter` →
> `mark_manual_position()` → при закрытии `manual=1` → self-learn исключает (адаптер грузит
> `manual=0`). Ручные входы — сделки Алексея; исторически они дают основную часть минуса
> аккаунта, и бот их не считает.

> **Плечо важнее стороны.** Исторические крупные убытки — на 10–25x. Авто-ветка выставляет
> конфижное плечо через `api.set_leverage()` перед ордером; ручной `/enter` навязывает плечо
> только если оно передано явно (без капов — решение Алексея).

> **Входы — post-only (с 23.10).** Авто-ветка (`auto_entry.py:843`, `auto_short.py:685`) ставит
> лимитки `timeInForce: PostOnly`: maker 0.02% вместо taker 0.055%; если ордер пересекает спред,
> биржа его отклоняет (в лог `⛔ POST-ONLY ...`), вход теряется — лучше, чем молчаливый taker.
> Ручные входы (`/enter`, MCP `place_entry`) не тронуты — Market.

> 📊 **Телеметрия сделки (с 18.10).** `trade_history.leverage` + `external_id` (миграция
> `state_db.py:_migrate()`, идемпотентна). Живой цикл пишет `fees`, `leverage`, `orderId` из
> уже получаемого `/v5/position/closed-pnl` — новых запросов нет. Ретро-заполнение старых
> строк сознательно не делалось: `pnl` уже net, а матчинг без уникального id ненадёжен.

## Структура (core)

> ⚠️ **`bybit_ws/*.py` — это симлинки на корневые модули** (`bybit_ws/rpc.py -> ../rpc.py`).
> Дублей кода нет: правка корневого файла действует и в пакете. Текущее число — `find -type l | wc -l`.
> Побочный эффект для сканеров: один и тот же файл виден по двум путям → GSC считает находку дважды
> (`api.py` + `bybit_ws/api.py`).
>
> **Версии:** `pyproject.toml` → `11.5.0`, `CHANGELOG.md` → `[11.5.0]` (25.10; до 18.10 стояло 3.10.1 — стейл-наследство).
> `rpc.py` отдаёт `api_version: "v1"` — это версия HTTP-API, не продукта, их не путать.

```
bybit-ws/
├── main_async.py         ← Главный цикл (async, 30с). Self-learn: ≥10 сделок за ≥6ч, либо fallback 24ч
├── unified_sl.py          ← Unified SL (слияние 5 модулей → 1; приоритет: tight>simple>hard>BE>auto>default)
├── auto_entry.py          ← Авто-вход (MTF + Orderbook + Volume + Entry Judge + Correlation)
├── auto_sl.py             ← ATR-adaptive SL (legacy, заменён unified_sl)
├── auto_tp.py             ← ATR-based TP (1×/2×/3× ATR)
├── volatility_filter.py   ← Volatility Regime Filter (off-by-default: блок входа при ATR-скачке + vol-scaled sizing)
├── risk_manager.py       ← Risk + BlackSwan (v2: alert only) + emergency_close
├── entry_judge.py        ← Cross-model judge (DeepSeek, fail-closed)
├── lstm_regime.py         ← LSTM-классификатор режима (82.3% — заявлено 01.08, не перезамерялось; 5 классов)
├── lstm_world_model.py    ← Multi-task OHLCV prediction + entry scoring
├── rpc.py                ← HTTP RPC (:8766, http.server, не JSON-RPC) + /kill_switch + /metrics + one-click
├── state_db.py           ← SQLite SSOT (WAL, busy_timeout=5с). `trade_history`: pnl (net), fees, leverage, external_id, manual; миграции — `_migrate()`
├── file_utils.py         ← Атомарная запись: `safe_json_write` (temp + os.replace + filelock), `locked_open`
├── tests/                ← pytest-набор (запуск: `python3 -m pytest -q`)
├── bybit_ws/journal/     ← Самообучение v10 (20+ механик). В корне папки `journal/` НЕТ
│   ├── self_learn.py      ← Dynamic Bandit, Pareto MC, Drift, Ensemble, Causal
│   ├── analyzer.py        ← Профиль + 4 bias-диагностики
│   └── adapter.py         ← SQLite → нормализованные сделки
├── deploy.sh             ← Деплой: test_logic_integrity (8) → test_smoke (52) → рестарт → canary (8 health-проверок)
├── test_smoke.py         ← 52 проверки (PASS=52/FAIL=0, замер 2026-10-18) — то, что гоняют verify.sh и deploy.sh
├── paper_trade.py        ← Бэктестинг на исторических данных
├── bybit_ws/experiments/ ← Experiment Tree: воспроизводимые бэктесты + sandbox
│   ├── engine.py          ← Детерминированный backtest-движок (кэш свечей, run-манифест)
│   ├── backtest_runner.py ← CLI reproducible backtest (--symbol/--days/--params)
│   ├── parallel_sandbox.py← N вариантов на одном окне, сравнительная таблица
│   ├── validation.py      ← Слой 0: DSR + PBO + permutation test (гейт анти-overfit)
│   ├── triple_barrier.py  ← Слой 1: triple-barrier лейблы {TP/SL/time} для meta-labeling
│   ├── regime.py          ← Слой 2: Gaussian HMM (режимы) + BOCPD (смена режима)
│   ├── bandit.py          ← Слой 3: regime-aware Thompson Sampling (reset на change-point)
│   ├── meta_label.py      ← Слой 4: meta-labeling (логистическая регрессия поверх сигналов)
│   └── store.py           ← ExperimentStore (таблицы experiment/run, experiments.db)
└── docs/
    ├── INDEX.md            ← КАРТА ДОКУМЕНТАЦИИ (единственная, читать первой)
    ├── index.md            ← лендинг GitHub Pages (Jekyll), НЕ документация
    ├── SELF_LEARN.md       ← Документация модуля самообучения (v10)
    ├── history.md          ← История фаз
    ├── PRD-one-click.md    ← One-click trading архитектура
    ├── experiment-tree-design.md ← Дизайн experiment tree (модель, DDL, связь с кодом)
    └── experiment-tree-TZ.md     ← ТЗ на реализацию
```

> 🔒 **Правило записи JSON-состояния: только `file_utils.safe_json_write(path, data)`.**
> Он пишет во временный файл и делает `os.replace` — читатель видит либо старый файл, либо
> новый, но никогда усечённый до нуля. Прямой `with open(path, 'w') + json.dump` в живом
> контуре запрещён: 28.10 на полном диске (`ENOSPC`) такой записью убили
> `lstm_regime_cache.json` и `sl_reentry.json`, и потребители получили
> `Expecting value: line 1 column 1`. Читать можно как обычно. Проверка: `tests/test_regime_cache_atomic.py`.

## Experiment Tree (эксперименты, v1)

Воспроизводимый стенд для поиска эджа стратегии офлайн (по мотивам OpenResearch).
Выносит проверку гипотез с live-аккаунта на детерминированные бэктесты + sandbox.

- **Дизайн:** `docs/experiment-tree-design.md`, ТЗ: `docs/experiment-tree-TZ.md`.
- **Модель:** `experiment` (узел дерева, parent-лайндж) → `run` (immutable прогон
  backtest/canary/live с run-манифестом). БД: `~/.local/share/bybit-ws/experiments/experiments.db`
  (sandbox — `sandbox.db`). Боевой `state.db` НЕ трогается.
- **Воспроизводимость:** фиксированное окно (`--end-date`) + кэш свечей по ключу
  `(symbol, interval, start_ms, end_ms)` → байт-в-байт одинаковые метрики.
- **Артефакт run (с 31.10):** `--out путь.json` пишет самодостаточный отчёт: манифест
  (`git_commit`, `engine_version`, `params`+`params_hash`, `config_hash` боевого
  `~/.config/bybit-ws/config.yaml`, `data_hash` свечей, `fees_model`, `funding_model`),
  метрики (`total_fees`, `total_funding`, …), `equity_curve` и `equity_hash`.
  Два прогона с одинаковыми входами → одинаковый `equity_hash` (тест
  `tests/test_backtest_artifact.py`). Без `--out` артефакт пишется в `runs/run_<id>.json`
  при записи в БД.

```bash
python3 -m bybit_ws.experiments.backtest_runner --symbol SOLUSDT --days 180 --no-db
python3 -m bybit_ws.experiments.parallel_sandbox --symbol SOLUSDT --days 180 --no-db
```

> **Граница v1:** хук «промоушен победителя → self_learn» не реализован (требует
> правки live-контура). Сейчас стенд — офлайн-поиск, не автопилот.

## Self-Learning v10 (ключевое)

Модуль автономно адаптирует стратегию без участия человека. 20+ механик:

> **Feature flags:** v10 self-learn работает независимо от `BYBIT_AB_ENABLED`, `BYBIT_REGIME_AUTO`, `BYBIT_OPTUNA_ENABLED`. Эти флаги управляют внешними фичами (A/B в auto_entry, авто-переключение LONG/SHORT, Optuna-тюнинг). Self-learn использует собственные внутренние механизмы (Thompson Sampling, regime detection через LSTM-кеш, composite score).

| Группа | Механики |
|--------|----------|
| **Подбор параметров** | Thompson Sampling, Dynamic Bandit (auto-prune), Uncertainty-aware selection |
| **Режимы рынка** | Per-regime params, Ensemble (6 bandits), Coordinated transition handover |
| **Защита** | Drift Detector (per-regime), Stress test (4 исторических backtest-сценария + 1000 Pareto MC симуляций), Causal inference |
| **Метрики** | Composite Score (WR+PF+Sharpe+DD+Hold), Exponential decay, Adaptive weights |
| **Качество данных** | Anomaly detection (IQR), Robust updates (>3σ outlier skip) |
| **Обучение** | Micro-updates (per trade), Canary (Bayesian A/B), Walk-forward validation |
| **Инфраструктура** | Git-like param versioning, Self-learn JSONL лог, Symbol profiles |

Детали: `docs/SELF_LEARN.md`.

> ⚠️ **Частота переобучения (разобрано 18.10).** Живой триггер — `should_run_self_learn_v6(min_trades=10,
> min_hours=6, fallback_hours=24)` из `main_async.py`: запуск при **≥10 сделках за ≥6ч**, либо принудительно
> через **24ч**. Мёртвая функция `should_run_self_learn()` и её константа `SELF_LEARN_INTERVAL_SEC = 24ч`
> **удалены** — они нигде не вызывались, но именно из-за них доки выглядели противоречиво.
> `docs/SELF_LEARN.md` («раз в 6 часов / ≥10 сделок / 24ч fallback») соответствует коду.

## Запуск

```bash
systemctl --user start bybit-ws-async     # старт
systemctl --user status bybit-ws-async    # статус
bash deploy.sh                             # деплой: чистота → logic → smoke → рестарт → canary
python3 test_smoke.py                      # тесты: 52 проверки (PASS=52/FAIL=0)
```

> ✅ **`python3 -m pytest` починен 18.10** — раньше падал `INTERNALERROR … SystemExit` (скриптовые
> smoke-файлы вызывают `sys.exit()` на импорте). Теперь `conftest.py` вычисляет их и игнорирует
> автоматически; сами скрипты по-прежнему запускаются вручную (`python3 test_ml_smoke.py`).
> Гонять системным `python3`, не `.venv/bin/python`. Число тестов не фиксируем — смотри вывод прогона.
> 25.10 закрыты 4 красных теста Фазы 11: `test_pump_state_clean` (бил в прод `state.db` — теперь
> БД инжектится) и `test_risk_journal` (фикстура без `size` игнорировала D1-правило «заглушка ≠ позиция»).
> Быстрый набор (то, что гоняют verify.sh и deploy.sh): `python3 test_smoke.py` → 52/52.

## Watchdog входов (с 31.10.2026)

Инцидент 31.10: бот 1.5 суток повторял `💸 Депозит $-5.6 < $30 — входы заблокированы`,
входов не было, узнали случайно. Теперь это ловит сторож:

```bash
python3 -m bybit_ws.watchdog                 # окно 6ч, порог 20 блокировок
python3 -m bybit_ws.watchdog --hours 1 --dry-run --json
```

- Входы — по SSOT `trade_history.entry_at`, блокировки — по `events.log`
  (`вход(ы) заблокирован|⛔|🛑|💸|STOP: … маржи`). Строки `… пропускаю`
  (dry-spell throttle, BB% < порога) — рабочая фильтрация кандидатов, НЕ блокировка:
  их тысячи в час, по ним алерт звонил бы всегда.
- Алерт: 0 входов за окно и ≥20 блокировок → `alerts.send_telegram_alert(level="STOP")`.
- Крон: Hermes `a954882a4f59`, ежечасно, `no_agent`, `deliver=local`
  (сообщение уходит своим каналом, дубля в чат нет). Скрипт-обёртка:
  `~/.hermes/scripts/bybit_watchdog.sh`.
- Тест: `tests/test_watchdog.py` (сценарий инцидента 31.10 + что фильтрация не блокировка).

## Проверка работоспособности (обязательно для AI-агентов)

> **Правило:** после ЛЮБОГО изменения кода/конфига/рестарта — `bash verify.sh`.
> Не «кажется, работает», а 11 проверок с exit-кодом. Проза врёт молча, скрипт — нет.

```bash
bash verify.sh        # exit 0 = всё зелёное; exit 1 = есть FAIL
```

| # | Проверка | Что ловит |
|---|----------|-----------|
| 1 | `systemctl --user is-active` | сервис упал |
| 2 | `/health` → `alive: true` | RPC мёртв |
| 3 | `cycle_count` растёт (2 замера) | цикл завис (цикл 30с) |
| 4 | зависимости **сервисного** `/usr/bin/python3` | пакет есть в `.venv`, но не у сервиса — прецедент: `websocket-client` отсутствовал 13 дней |
| 5 | WS `connected` в events.log (последняя запись) | WS не поднялся или отвалился |
| 6 | свежесть `correlation.json` (≤15м) / `positions.json` (≤5м) | данные протухли |
| 7 | позиции: RPC `/positions` == `state.db` | рассинхрон |
| 8 | `PRAGMA quick_check` | битая БД |
| 9 | ошибки/traceback в events.log за сегодня | скрытые падения |
| 10 | `py_compile` ключевых модулей | синтаксис |
| 11 | `test_smoke.py` | регрессия |

**Ловушки, на которых уже спотыкались:**
- Зависимости проверять **сервисным** интерпретатором из `ExecStart` (`/usr/bin/python3`), а не `.venv` — иначе «у меня работает».
- `/positions`, `/balance`, `/risk` требуют Bearer-токен из `kv_store['rpc_auth_token']`; `401 Unauthorized` без токена — это НЕ поломка.
- Рестарт: `systemctl --user restart bybit-ws-async`. SIGTERM может не уложиться в 90с → SIGKILL — норма, но после рестарта обязан расти `cycle_count`.
- `deploy.sh` требует чистого дерева или `--force`.
- После деплоя/рестарта: `bash verify.sh` — и только потом докладывать «работает».

## Где что лежит

| Данные | Путь |
|--------|------|
| Позиции (SSOT) | `~/.local/share/bybit-ws/state.db` |
| Логи | `~/.local/share/bybit-ws/events.log` |
| Конфиг | `~/.config/bybit-ws/config.yaml` |
| Креды | `~/.config/bybit-ws/env` (chmod 600) |
| RPC | `http://127.0.0.1:8766` |
| Self-learn state | `~/.local/share/bybit-ws/{canary_state,parameter_ensemble,self_learn_state}.json` (`regime_params.json` ожидается кодом (`self_learn.py:1505`), но на диске **отсутствует** — создастся при первом срабатывании) |
| Param versions | `~/.local/share/bybit-ws/params_history/v*.json` |
| LSTM модель | `~/.local/share/bybit-ws/models/lstm_regime.pt` |

## Аварийные ситуации

```bash
# Kill switch — закрыть ВСЕ позиции
curl -X POST http://127.0.0.1:8766/kill_switch -H "Authorization: Bearer TOKEN"

# Emergency close — без паузы
curl -X POST http://127.0.0.1:8766/emergency_close -H "Authorization: Bearer TOKEN"

# Сбросить LLM Circuit Breaker
systemctl --user restart bybit-ws-async
```

## RPC-эндпоинты

`/scan` → `/calc_qty` → `/enter` → `/positions`

### GET

| Эндпоинт | Назначение |
|----------|-----------|
| `/health` | Статус (alive, uptime, cycle_count) |
| `/positions` | Открытые позиции + PnL |
| `/balance` | USDT баланс (walletBalance, available, equity) |
| `/metrics` | Prometheus-метрики |
| `/risk` | Лимиты риска + circuit breaker |

### POST (Bearer-токен обязателен)

| Эндпоинт | Назначение | Параметры |
|----------|-----------|-----------|
| `/scan` | GridSignal-скан | `{mode, interval, symbol?}` |
| `/enter` | Вход в позицию | `{symbol, side, qty, sl?, tp?, leverage?, confirm}` — `leverage` (1–100) необязателен: передан → ставится ровно он (решение Алексея, без капов), не передан → плечо не навязывается |
| `/close` | Закрыть позицию | `{symbol}` |
| `/calc_qty` | Расчёт размера позиции | `{symbol, risk_pct, leverage}` |
| `/move_sl` | Передвинуть SL | `{symbol, stop_loss}` |
| `/kill_switch` | Закрыть всё + пауза | — |
| `/emergency_close` | Закрыть всё без паузы | — |

### Воркфлоу one-click (через Hermes-чат)

```
«просканируй SOL» → /scan → сигнал с BB%/RSI
«бери SOL 5%»     → /calc_qty → qty + SL/TP
                   → /enter confirm:true → позиция открыта
«позиции»         → /positions → live PnL + кнопка закрыть
```

## MCP-инструменты

`scan_market` → `get_positions` → `get_metrics` → `vpn_status` → `get_risk_status` → `get_journal` → `place_entry` — всего **7** инструментов, объявлены вручную в `@app.list_tools()` (`mcp_server.py`), не через `@mcp.tool`

| Инструмент | Назначение |
|-----------|-----------|
| `scan_market(mode, interval)` | Скан Bollinger Grid |
| `get_positions()` | Позиции + PnL |
| `get_metrics()` | TP/SL/входы за день |
| `get_risk_status()` | Лимиты + CB |
| `place_entry(symbol, side, qty, sl?, tp?, order_type?, price?, leverage?)` | Вход. **Плечо:** параметр `leverage` (1–100), если не задан — берётся из конфига стратегии (`strategy.long/short.leverage`, `config.py:44,56`). После входа плечо читается обратно из ответа `/enter` (fallback — `/rpc/positions`) и печатается `Leverage: Nx (requested Mx)`; при расхождении — `⚠️ LEVERAGE MISMATCH`. Авто-ветка (`auto_entry`/`auto_short`) выставляет плечо сама через `api.set_leverage()` перед ордером. Ручной `/enter` плечо навязывает только если оно передано явно |
| `get_journal()` | Журнал (FIFO, bias) |
| `vpn_status()` | VPN health + трафик |

## LSTM World Model (v8.1)

- **Файл:** `lstm_world_model.py`
- **Архитектура:** Multi-task LSTM — regime classification + OHLCV prediction на t+1
- **Feature flag:** `BYBIT_WORLD_MODEL=1` — добавляет World Model score (0-5) в entry scoring
- **CLI:** `python3 lstm_world_model.py --train` / `--predict BTCUSDT`

## LSTM Market Regime

- **Модель:** 82.3% точность — **заявлено 01.08.2026, повторно не замерялось** (число из истории обучения, не из живого прогона). Живой источник: `lstm_hmac.json` / логи переобучения.
- **Классы:** TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOL, LOW_VOL
- **Feature flag:** `BYBIT_REGIME_AUTO=1` — авто-переключение LONG/SHORT
- **CLI:** `python3 -m bybit_ws.lstm_regime --predict`

## Pre-deploy checklist

- [ ] `python3 test_smoke.py` → PASS (52/52)
- [ ] `sqlite3 ~/.local/share/bybit-ws/state.db "PRAGMA integrity_check"` → ok
- [ ] `curl -s http://127.0.0.1:8766/health` → alive
- [ ] `grep Heartbeat ~/.local/share/bybit-ws/events.log | tail -1` → свежий
- [ ] `bash deploy.sh`
- [ ] `grep "MemoryDenyWriteExecute" deploy/bybit-ws-async.service` — должен быть закомментирован
- [ ] `grep "TimeoutStopSec" deploy/bybit-ws-async.service` — должен быть =10

## Systemd Pitfalls (v8.2, 04.08.2026)

### MemoryDenyWriteExecute vs PyTorch

`MemoryDenyWriteExecute=true` блокирует PyTorch — LSTM-модель падает с «could not create a primitive».
**Решение:** закомментировать в unit-файле, добавить `Environment=PYTORCH_JIT=0` и `TORCH_COMPILE_DISABLE=1`.

### LSTM HMAC mismatch после переобучения

**Решение:** переподписать модель:
```bash
cd ~/bybit-ws && python3 -c "
import os, hmac, hashlib
with open(os.path.expanduser('~/.config/bybit-ws/env')) as f:
    for line in f:
        if line.startswith('BYBIT_HMAC_SECRET='):
            key = line.strip().split('=',1)[1].encode()
            break
for fname in ['lstm_regime.pt', 'lstm_regime_scaler.pkl']:
    path = f'/home/openclaw/.local/share/bybit-ws/models/{fname}'
    with open(path, 'rb') as fh: sha = hashlib.sha256(fh.read()).hexdigest()
    sig = hmac.new(key, sha.encode(), hashlib.sha256).hexdigest()
    with open(path + '.hmac', 'w') as fh: fh.write(sig)
"
```

### TimeoutStopSec — зависание на SIGTERM

**Решение:** `TimeoutStopSec=10` в unit-файле.

### lstm_world_model — симлинк

**Решение:** симлинк `bybit_ws/lstm_world_model.py -> ../lstm_world_model.py`.

## Paper Trading

```bash
python3 -m bybit_ws.paper_trade SOLUSDT --days 30
python3 -m bybit_ws.paper_trade SOLUSDT --days 90 --interval 240 --json
python3 -m bybit_ws.paper_trade BTCUSDT --days 180 --risk 3 --rr 1.5
```

## Известные не-баги

| Проявление | Причина | Влияние |
|-----------|--------|---------|
| `check_weekly_pumps` timeout 10s | Защита от зависаний, результат отбрасывается, цикл работает | Нет |
| SSRF в bybit_ws_sdk.py | SDK hardcoded на localhost:8766, не принимает external input | Internal dev tool |
| YAML-UPLOAD001 (MIME validation) | ML-модели загружаются из локальных файлов, не HTTP | Локальный ML-пайплайн |
| ~~RVN partial qty spam~~ | **Fixed 10.10**: убран лог при qty < qtyStep | ✅ Устранено |
