# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Детали стратегий, параметры, runbook → [OpenWiki](openwiki/quickstart.md).
> **Обновлено: 2026-10-18** — числа сверены с `state.db`, `verify.sh`, `test_smoke.py`, `pytest` (замеры ниже с датой).

## Что это

Трейдинг-монитор Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT).  
Systemd-сервис `bybit-ws-async` (user unit, `ExecStart=/usr/bin/python3 -m bybit_ws.main_async`),
**RSS ~50-55 MB** (замер 18.10: 54 576 KB; значение плавает — прежние 185 MB в доке не подтверждаются), SQLite — SSOT.

## Текущее состояние (замер 2026-10-18)

> **Снимок на 2026-10-18.** Числа — снапшот `state.db`; живой режим/PnL всегда из RPC (`/metrics`, `/rpc/all`, `/positions`) + `state.db`, не из этой таблицы.
> (`/journal` и `/rpc/regime` в коде **отсутствуют** — отдают 404; ссылки на них были ошибкой доки.)

| Метрика | Значение | Примечание |
|---------|----------|-----------|
| Закрытых сделок (все) | 457 | auto 341 + historical 47 + NULL/legacy 31 + imported 30 + x10 6 + x10:scalp 2 |
| Авто (`strategy='auto'`, любой manual) | 341 | все авто-входы |
| **Авто self-learn (`manual=0`)** | **310 / +$146.61 / WR 74.8% / PF 1.19** | ручные входы исключены из обучения |
| Ручные (`manual=1`) | 31 / −$410.38 | входы пользователя, не бот |
| SHORT (авто, `manual=0`) | 207 / +$206.57 / WR 77.8% | **прибыльная сторона** |
| LONG (авто, `manual=0`) | 103 / −$59.96 / WR 68.9% | ⚠️ убыточная сторона (было наоборот 14.10) |
| Режим | live → `GET /metrics` (Prometheus) или `/rpc/all` | не хранить в доке (мгновенно устаревает) |

> ⚠️ **Числа меняются каждую сессию — не копировать без перезамера.** Снапшот 14.10 говорил
> «LONG +$248.98 / SHORT −$74.27»; за 4 дня стороны поменялись местами. Живой источник:
> `sqlite3 ~/.local/share/bybit-ws/state.db` + RPC `/metrics`, `/rpc/all`, `/positions`
> (`/journal` и `/rpc/regime` из прежней версии доки не существуют — 404, проверено 18.10).

> **manual-фильтр:** `trade_history.manual` отделяет авто-входы от ручных. Ручной вход через `/enter` → `mark_manual_position()` → при закрытии `manual=1` → self-learn исключает. Адаптер self-learn грузит `manual=0`.

## Структура (core)

> ⚠️ **`bybit_ws/*.py` — это симлинки на корневые модули** (замер 2026-10-18: `find -type l` → 74 шт.,
> напр. `bybit_ws/rpc.py -> ../rpc.py`). Дублей кода нет: правка корневого файла действует и в пакете.
> Файлов `.py` всего 2130 (1920 из них — `.venv`), вне виртуального окружения — 135, из них 74 симлинка.
> Побочный эффект для сканеров: один и тот же файл виден по двум путям → GSC считает находку дважды
> (`api.py` + `bybit_ws/api.py`).
>
> **Версии сведены 18.10:** `pyproject.toml` → `11.4.0` (было 3.10.1 — стейл-наследство), `CHANGELOG.md` → `[11.4.x]`.
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
├── rpc.py                ← JSON-RPC (:8766) + /kill_switch + /metrics + one-click
├── state_db.py           ← SQLite SSOT (WAL, busy_timeout=5с)
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
    ├── SELF_LEARN.md       ← Документация модуля самообучения (v10)
    ├── history.md          ← История фаз
    ├── PRD-one-click.md    ← One-click trading архитектура
    ├── experiment-tree-design.md ← Дизайн experiment tree (модель, DDL, связь с кодом)
    └── experiment-tree-TZ.md     ← ТЗ на реализацию
```

## Experiment Tree (эксперименты, v1)

Воспроизводимый стенд для поиска эджа стратегии офлайн (по мотивам OpenResearch).
Выносит проверку гипотез с live-аккаунта на детерминированные бэктесты + sandbox.

- **Дизайн:** `docs/experiment-tree-design.md`, ТЗ: `docs/experiment-tree-TZ.md`.
- **Модель:** `experiment` (узел дерева, parent-лайндж) → `run` (immutable прогон
  backtest/canary/live с run-манифестом). БД: `~/.local/share/bybit-ws/experiments/experiments.db`
  (sandbox — `sandbox.db`). Боевой `state.db` НЕ трогается.
- **Воспроизводимость:** фиксированное окно (`--end-date`) + кэш свечей по ключу
  `(symbol, interval, start_ms, end_ms)` → байт-в-байт одинаковые метрики.

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
bash deploy.sh                             # деплой (smoke → logic → canary)
python3 test_smoke.py                      # тесты: 52 проверки (PASS=52/FAIL=0)
```

> ✅ **`python3 -m pytest` починен 18.10** — раньше падал `INTERNALERROR … SystemExit` (скриптовые
> smoke-файлы вызывают `sys.exit()` на импорте). Теперь `conftest.py` вычисляет их и игнорирует
> автоматически; сами скрипты по-прежнему запускаются вручную (`python3 test_ml_smoke.py`).
> Текущий прогон: **200 тестов — 198 passed, 2 failed**. Два падения — `test_mtf.py::TestCheckConfluence`
> (`check_confluence('BTCUSDT', …)` → `None`): тесты бьют в живой API Bybit, в изоляции проходят 16/16,
> в полном прогоне упираются в rate-limit. Это флак от сети, а не поломка логики.
> Быстрый набор (то, что гоняют verify.sh и deploy.sh): `python3 test_smoke.py` → 52/52.

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
| `/enter` | Вход в позицию | `{symbol, side, qty, sl?, tp?, confirm}` |
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

`scan_market` → `get_risk_status` → `get_positions` → `place_entry`

| Инструмент | Назначение |
|-----------|-----------|
| `scan_market(mode, interval)` | Скан Bollinger Grid |
| `get_positions()` | Позиции + PnL |
| `get_metrics()` | TP/SL/входы за день |
| `get_risk_status()` | Лимиты + CB |
| `place_entry(symbol, side, qty, sl?, tp?, order_type?, price?)` | Вход. **Плечо не задаётся:** в конфиге стратегии `leverage: 3` (`config.py:44,56`), но MCP-вход его не применяет → позиция открывается на биржевом дефолте символа (прецедент 18.10: QUSDT — 10x, хотя просили 5x; стоп 0.158 оказался за ликвидацией ~0.1483). Проверять `get_positions` сразу после входа |
| `get_journal()` | Журнал (FIFO, bias) |

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
