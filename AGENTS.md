# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Детали стратегий, параметры, runbook → [OpenWiki](openwiki/quickstart.md).
> Обновлено: 2026-09-09 (v11 — Фазы 9.1–9.3: junk-шорт фикс + FIFO-матчинг + time-exit/blacklist/candle-cache)

## Что это

Трейдинг-монитор Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT).  
Systemd-сервис `bybit-ws-async`, ~185 MB RAM (с LSTM/torch), SQLite — SSOT.

## Текущее состояние (09.09.2026)

| Метрика | Значение | Примечание |
|---------|----------|-----------|
| Закрытых сделок | 301 | 185 auto + 47 historical + 30 imported + 31 legacy + 8 x10 |
| Win Rate (все) | 65% | |
| Profit Factor (все) | 0.86 | исторический (тянет imported −$243 + старый период) |
| **PF после Фазы 9.1 (15.08+)** | **1.38** | 131 сделка, WR 77% |
| LONG после 9.1 | PF 9.64, WR 90% | +$240 — основная прибыль |
| SHORT после 9.1 | PF 0.70, WR 67% | ⚠️ avg_loss $14.11 vs avg_win $4.85 — дисбаланс вернулся (ZECUSDT) |
| Режим | TRENDING_DOWN | LSTM 82.3% точность |

> **⚠️ SHORT после 9.1 снова просел (09.09.2026).** Фаза 9.1 (15.08) убрала junk-шорты (SL +7%, DCA удалён) — на срезе 27.08 SHORT был WR 83%/PF 4.50. За следующие 13 дней SHORT дал −$101: 48 TP-выходов +$230 против 23 SL −$270 + 2 time-exit −$61. Главный виновник **ZECUSDT** (серия −$158: time-exit 80ч −$69, затем SL −$40/−$25/−$24) и USELESSUSDT (−$64 SL). LONG при этом стабилен: PF 9.64, WR 90%. Требуется разбор ZECUSDT (кандидат в blacklist).

## Структура (core)

```
bybit-ws/
├── main_async.py         ← Главный цикл (async, 30с). Self-learn: event-driven (≥10 сделок / 6ч)
├── unified_sl.py          ← Unified SL (5→1, приоритет: tight>simple>hard>BE>default)
├── auto_entry.py          ← Авто-вход (MTF + Orderbook + Volume + Entry Judge + Correlation)
├── auto_sl.py             ← ATR-adaptive SL (legacy, заменён unified_sl)
├── auto_tp.py             ← ATR-based TP (1×/2×/3× ATR)
├── risk_manager.py       ← Risk + BlackSwan (v2: alert only) + emergency_close
├── entry_judge.py        ← Cross-model judge (DeepSeek, fail-closed)
├── lstm_regime.py         ← LSTM-классификатор режима (82.3% точность, 5 классов)
├── lstm_world_model.py    ← Multi-task OHLCV prediction + entry scoring
├── rpc.py                ← JSON-RPC (:8766) + /kill_switch + /metrics + one-click
├── state_db.py           ← SQLite SSOT (WAL, busy_timeout=5с)
├── journal/              ← Самообучение v10 (20+ механик)
│   ├── self_learn.py      ← Dynamic Bandit, Pareto MC, Drift, Ensemble, Causal
│   ├── analyzer.py        ← Профиль + 4 bias-диагностики
│   └── adapter.py         ← SQLite → нормализованные сделки
├── deploy.sh             ← Атомарный деплой (smoke 52 + canary 8)
├── test_smoke.py         ← 52 интеграционных тестов
├── paper_trade.py        ← Бэктестинг на исторических данных
├── bybit_ws/experiments/ ← Experiment Tree: воспроизводимые бэктесты + sandbox
│   ├── engine.py          ← Детерминированный backtest-движок (кэш свечей, run-манифест)
│   ├── backtest_runner.py ← CLI reproducible backtest (--symbol/--days/--params)
│   ├── parallel_sandbox.py← N вариантов на одном окне, сравнительная таблица
│   ├── validation.py      ← Слой 0: DSR + PBO + permutation test (гейт анти-overfit)
│   ├── triple_barrier.py  ← Слой 1: triple-barrier лейблы {TP/SL/time} для meta-labeling
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

## Запуск

```bash
systemctl --user start bybit-ws-async     # старт
systemctl --user status bybit-ws-async    # статус
bash deploy.sh                             # деплой (smoke → logic → canary)
python3 test_smoke.py                      # тесты
```

## Где что лежит

| Данные | Путь |
|--------|------|
| Позиции (SSOT) | `~/.local/share/bybit-ws/state.db` |
| Логи | `~/.local/share/bybit-ws/events.log` |
| Конфиг | `~/.config/bybit-ws/config.yaml` |
| Креды | `~/.config/bybit-ws/env` (chmod 600) |
| RPC | `http://127.0.0.1:8766` |
| Self-learn state | `~/.local/share/bybit-ws/{canary_state,parameter_ensemble,self_learn_state,regime_params}.json` |
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
| `place_entry(symbol, side, qty)` | Вход |
| `get_journal()` | Журнал (FIFO, bias) |

## LSTM World Model (v8.1)

- **Файл:** `lstm_world_model.py`
- **Архитектура:** Multi-task LSTM — regime classification + OHLCV prediction на t+1
- **Feature flag:** `BYBIT_WORLD_MODEL=1` — добавляет World Model score (0-5) в entry scoring
- **CLI:** `python3 lstm_world_model.py --train` / `--predict BTCUSDT`

## LSTM Market Regime

- **Модель:** 82.3% точность (переобучена 01.08.2026)
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
