# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Детали стратегий, параметры, runbook → [OpenWiki](openwiki/quickstart.md).
> Обновлено: 2026-08-04 (v8.1 — LSTM World Model: multi-task OHLCV prediction + entry scoring)

## Что это

Трейдинг-монитор Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT).  
Systemd-сервис `bybit-ws-async`, ~45 MB RAM, SQLite — SSOT.

## Структура (core)

```
bybit-ws/
├── main_async.py         ← Главный цикл (async, 30с). Self-learn каждые 720 циклов (6ч)
├── unified_sl.py          ← Unified SL (5→1, приоритет: tight>simple>hard>BE>default)
├── auto_entry.py          ← Авто-вход (MTF + Orderbook + Volume + Entry Judge + Correlation)
├── auto_sl.py             ← ATR-adaptive SL (legacy, заменён unified_sl)
├── auto_tp.py             ← ATR-based TP (1×/2×/3× ATR)
├── risk_manager.py       ← Risk + BlackSwan (3-tier) + emergency_close
├── entry_judge.py        ← Cross-model judge (Nemotron→DeepSeek, fail-closed)
├── lstm_regime.py         ← LSTM-классификатор режима (82.3% точность, 5 классов)
├── rpc.py                ← JSON-RPC (:8766) + /kill_switch + /metrics + one-click
├── state_db.py           ← SQLite SSOT (WAL, busy_timeout=5с)
├── journal/              ← Самообучение + Canary mode
├── deploy.sh             ← Атомарный деплой (pre-deploy 6 checks + canary 8 checks)
├── test_smoke.py         ← 52 интеграционных тестов
├── paper_trade.py        ← 🆕 Бэктестинг на исторических данных
└── docs/
    ├── history.md         ← История фаз
    └── PRD-one-click.md   ← One-click trading архитектура
```

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
| Трейды | `~/.local/share/bybit-ws/trades.jsonl` |
| Canary state | `~/.local/share/bybit-ws/canary_state.json` |
| LSTM модель | `~/.local/share/bybit-ws/models/lstm_regime.pt` |
| LSTM скейлер | `~/.local/share/bybit-ws/models/lstm_regime_scaler.pkl` |

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
- **Идея:** ECHO (Anthropic, 2026) — каждая свеча = training sample через world modeling
- **Датасет:** 5 символов × 2 года (~3,445 сэмплов)
- **World MSE:** ~0.045 (≈2% ошибка дневных Δ)
- **Feature flag:** `BYBIT_WORLD_MODEL=1` — добавляет World Model score (0-5) в entry scoring
- **Кеш:** `get_cached_world_prediction(symbol)` — 1-часовой TTL, batch-запрос для всех AUTO_ENTRY_WATCH
- **CLI:** `python3 lstm_world_model.py --train` / `--predict BTCUSDT`

## LSTM Market Regime

- **Модель:** 82.3% точность (переобучена 01.08.2026)
- **Классы:** TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOL, LOW_VOL
- **Feature flag:** `BYBIT_REGIME_AUTO=0` (default) — авто-переключение LONG/SHORT выключено
- **CLI:** `python3 lstm_regime.py --predict` — текущий режим
- **Обучение:** `python3 lstm_regime.py --train` (100 эпох, ~3 мин)

## Codebase-memory MCP — вместо grep

Проект проиндексирован: 2686 узлов, 8640 рёбер.

| Задача | Инструмент |
|--------|-----------|
| Найти определение | `search_graph(query="canary")` |
| Граф вызовов | `trace_path(function_name="auto_entry_scan")` |
| Хотспоты/O(n²) | `query_graph` → `transitive_loop_depth >= 3` |
| Фрагмент кода | `get_code_snippet(qualified_name="...")` |
| Что сломал коммит | `detect_changes(since="HEAD~1")` |

## Pre-deploy checklist

- [ ] `python3 test_smoke.py` → PASS (52/52)
- [ ] `sqlite3 ~/.local/share/bybit-ws/state.db "PRAGMA integrity_check"` → ok
- [ ] `curl -s http://127.0.0.1:8766/health` → alive
- [ ] `grep Heartbeat ~/.local/share/bybit-ws/events.log | tail -1` → свежий
- [ ] `bash deploy.sh`

## Paper Trading

```bash
# Бэктест на истории (без реальных сделок)
python3 -m bybit_ws.paper_trade SOLUSDT --days 30
python3 -m bybit_ws.paper_trade SOLUSDT --days 90 --interval 240 --json
python3 -m bybit_ws.paper_trade BTCUSDT --days 180 --risk 3 --rr 1.5
```

Файл: `bybit_ws/paper_trade.py` (506 строк).

## Детали

Архитектура цикла, параметры стратегий, фильтры входа, Black Swan, сессии,
runbook инцидентов, схема БД, диагностика по логам → [OpenWiki](openwiki/quickstart.md).
