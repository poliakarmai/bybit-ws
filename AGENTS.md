# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Не пересказывает код — даёт контекст.

## Что это

Трейдинг-монитор для Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT по BB-полосам).  
Работает как systemd-сервис, ~23.5 MB RAM, SQLite — единственный источник истины (SSOT).

## Структура

```
bybit-ws/
├── main.py              ← Точка входа, главный цикл
├── api.py               ← Bybit v5 REST API (6 endpoints)
├── config.py            ← Все константы, пороги, лимиты
├── state_db.py          ← SQLite SSOT (8 таблиц, WAL)
├── trailing_sl.py       ← Трейлинг-стопы (LONG/SHORT зеркально)
├── auto_sl.py           ← Авто-стоплоссы + защита от перезатирания + безубыток
├── auto_short.py        ← Авто-шорты по BB-сигналам
├── gridsignal_scanner.py← Сканер сигналов Bollinger Grid
├── gridsignal-bot.py    ← Исполнение сигналов (бывший CLI, сейчас через RPC)
├── rpc.py               ← JSON-RPC сервер + /metrics Prometheus
├── paper_api.py         ← Paper Trading API (PaperExchange) для бэктеста
├── metrics.py           ← Сбор и хранение метрик
├── pump_detect.py       ← Детектор пампов
├── overbought.py        ← Детектор перекупленности (BB% > 100%)
├── alerts.py            ← Telegram-алерты (через переменные окружения)
├── reporting.py         ← Ежедневные отчёты
├── test_smoke.py        ← 45 smoke-тестов
├── snapshot.py          ← Снепшоты позиций (JSON, резерв)
├── margin_alerts.py     ← Алерты по марже
├── risk/                ← Риск-менеджмент (отдельная папка)
├── web/                 ← Веб-интерфейс (proxy_server.py)
└── DESIGN-STRATEGIES.md ← Архитектура стратегий
```

## Ключевые файлы и что в них

### `main.py` — главный цикл (464 строки)
- `_run_heavy_cycle()` — основные проверки (каждые 120 сек)
- `_run_x10_cycle()` — проверка x10 позиций
- `_run_safety_checks()` — проверка безопасности
- `check_breakeven_sl()` — авто-безубыток при +10% профита (каждые 4 цикла)
- Вызывает: `auto_sl`, `auto_short`, `trailing_sl`, `pump_detect`

### `state_db.py` — база данных (390 строк)
- 8 таблиц: positions, trades, alerts, short_positions, pumps, x10_limits, paper_positions, paper_trades
- WAL-режим, потокобезопасность
- Методы: `get_positions()`, `update_position()`, `log_trade()`, `get_metrics()`

### `auto_sl.py` — стоп-лоссы (+103 строки)
- **Правило 1:** запрет перезатирания SL, если SL уже на стороне прибыли (SL > entry для LONG)
- **Правило 2:** авто-безубыток — при профите >10% ставится SL = entry × 1.01 (LONG) или entry × 0.99 (SHORT)

### `trailing_sl.py` — трейлинг-стопы
- LONG: поджимает SL ВВЕРХ при W-BB < 25% + PnL > 15%
- SHORT: поджимает SL ВНИЗ (зеркальная логика к LONG)
- Порог обновления: 0.5% от mark price

### `api.py` — Bybit v5 REST API
- 6 endpoints: get_positions, get_wallet_balance, place_order, set_stop_loss, close_position, get_klines
- Все методы с docstrings и ссылками на Bybit docs
- Ретрай-логика на 429/500/503/504

### `rpc.py` — JSON-RPC сервер
- `/rpc` — вызов методов
- `/metrics` — Prometheus (bybit_ws_active_positions, bybit_ws_daily_pnl, bybit_ws_cycle_duration_seconds)
- Auth: UUID-токен, обязателен всегда

### `paper_api.py` — Paper Trading
- Класс `PaperExchange` — симулятор биржи
- База `paper_state.db` (WAL, 4 таблицы)
- Проскальзывание 0.05%, комиссия taker 0.06%
- Ликвидация ±10% от входа
- Интерфейс совместим с `api.py`

### `pump_detect.py` — детектор пампов
- Отслеживает pumps.json
- Флаг `manual_sl` — ручной стоплосс не перезатирается авто-SL

## Как запускать

```bash
# Локально
cd ~/bybit-ws
source .venv/bin/activate
python -m bybit-ws

# Сервис
sudo systemctl start bybit-ws
sudo systemctl status bybit-ws

# Тесты
python test_smoke.py          # 45 тестов
python test_scanner_smoke.py  # Тесты сканера
python test_modules.py        # Модульные тесты

# Метрики
curl http://localhost:8380/metrics
```

## Как тестировать

- `test_smoke.py` — 45 интеграционных тестов (trailing_sl 8, state_db 20, auto_sl 5, api 12)
- `test_scanner_smoke.py` — тесты сканера сигналов
- `test_modules.py` — модульные тесты
- Все тесты должны проходить перед коммитом

## Где что лежит

| Данные | Место |
|--------|-------|
| Позиции (SSOT) | `~/.local/share/bybit-ws/state.db` (SQLite, WAL) |
| Резервные снепшоты | `~/.local/share/bybit-ws/positions_snapshot.json` (раз в час) |
| Метрики | `~/.local/share/bybit-ws/metrics.json` |
| Пампы | `~/.local/share/bybit-ws/pumps.json` |
| Бумажные позиции | `~/.local/share/bybit-ws/paper_state.db` |
| Логи сервиса | `journalctl -u bybit-ws` |

## Для AI-агентов: авто-обнаружение путей

**Способ 1 — RPC (рекомендуемый):**
```bash
curl http://127.0.0.1:8766/rpc/paths
```
Возвращает JSON со всеми путями: `state_db`, `events_log`, `config_file`, `repo`, `install_dir`, команды синхронизации и рестарта. Не требует авторизации.

**Способ 2 — AGENTS.md (этот файл):**
Все пути захардкожены в таблице выше. Если RPC недоступен — использовать их.

**Способ 3 — переменные окружения:**
Монитор читает `BYBIT_WS_DATA_DIR`, `BYBIT_WS_CONFIG`. При кастомной установке — задать их.

**Стандартные пути установки:**
| Ресурс | Путь |
|--------|------|
| Репозиторий | `~/bybit-ws/` |
| Рабочая копия | `~/.local/lib/bybit_ws/` |
| Данные (SSOT) | `~/.local/share/bybit-ws/` |
| Конфиг | `~/.config/bybit-ws/config.yaml` |
| RPC | `http://127.0.0.1:8766` |
| Сервис | `systemctl --user bybit-ws` |
| Синхронизация | `cp ~/bybit-ws/{file}.py ~/.local/lib/bybit_ws/` |

## Конвенции

- **Python 3.11+**, зависимости через `pip`
- **Никаких ключей в коде** — всё из `.env`
- **SQLite — SSOT** (единственный источник истины), JSON — резерв
- **RPC-авторизация** обязательна всегда
- **JUNK-стратегии** отключены через feature flag `enabled: false`
- **Коммиты:** осмысленные на русском, с хешами в логах
- **Перед опасными операциями** — бэкап через `hermes-backup` skill

## Что уже реализовано (Фаза 2 завершена)

- SQLite миграция (SSOT)
- SHORT-трейлинг (зеркальный LONG)
- Защита SL от перезатирания
- Авто-безубыток (+10%)
- Paper Trading API
- Prometheus /metrics
- main_loop разбит на 3 функции
- Smoke-тесты 45/45

## Что сделано (Фаза 3 ✅ завершена)

- [x] ML-скоринг сигналов — RandomForest F1=0.69, 70/30 вес
- [x] Trailing Stop для x10 — HEAVY_CYCLE, фильтр leverage≥10
- [x] Partial TP — динамический сплит 20/80→50/50, без numpy
- [x] Бэктестинг — walk-forward на исторических klines (REST API)
- [x] Авто-фандинг-ротация — check + execute + алерты

## Что сделано (Фаза 4 ✅ завершена, кроме asyncio)

- [x] ATR-based риск-сайзинг — `position_sizing.atr_margin()`, кеш 4ч
- [x] Multi-timeframe конфлюенс — `mtf_confirmation.py` + `confluence_paper.py` (D/W/M, ≥2/3)
- [x] Алерты в Telegram при входе/выходе — `alerts.py` → `hermes send --to telegram:Poliakarm`
- [x] WebSocket live-цены/BB — `ws_client.py` (50+ тикеров, kline-потоки)
- [x] httpx вместо requests — `api.py` мигрирован (подготовка к asyncio)
- [x] Дашборд v5.0 — `web/dashboard.html` + `proxy_server.py` (порт 9999)
- [~] asyncio — `api.py` (async-дубликаты), `state_db.py` (AsyncStateDB/aiosqlite), `main_async.py` (скелет цикла). Осталось: RPC→aiohttp, ws_client→async, полный цикл, тесты, деплой (≈30-40ч)

## MCP-инструменты (как AI-агенты взаимодействуют с bybit-ws)

MCP-сервер: `/home/openclaw/.local/bin/bybit-mcp-server.py` (порт 8766 через RPC)

| Инструмент | Назначение | Пример |
|-----------|-----------|--------|
| `scan_market` | Скан сигналов Bollinger Grid | `scan_market(mode="long", interval="D")` |
| `get_positions` | Текущие позиции + PnL | `get_positions()` |
| `get_metrics` | Дневные метрики (TP/SL/входы) | `get_metrics()` |
| `get_risk_status` | Лимиты риска (маржа, дневной убыток) | `get_risk_status()` |
| **`place_entry`** | **Вход в позицию (Market/Limit)** | `place_entry(symbol="LINKUSDT", side="Buy", qty=14)` |

### `place_entry` — полная сигнатура

```python
mcp_bybit_ws_place_entry(
    symbol="LINKUSDT",   # Торговая пара
    side="Buy",          # Buy=LONG, Sell=SHORT
    qty=14,              # Количество в базовых единицах
    sl=5.31,             # Стоп-лосс (опционально)
    tp=None,             # Тейк-профит (опционально)
    order_type="Market", # Market или Limit
    price=6.774,         # Цена для Limit-ордера (опционально)
)
```

**Market** — мгновенное исполнение. **Limit** — GTC-ордер по указанной цене (обычно на BB-полосе).
SL и TP ставятся автоматически после исполнения.

### Типичный воркфлоу для AI-агента
1. `scan_market` → выбрать кандидатов
2. `get_risk_status` → проверить лимиты
3. `get_positions` → нет ли уже позиции по символу
4. `place_entry` → войти (Market — сразу, Limit — на BB-полосе)
