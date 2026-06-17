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
| Позиции (SSOT) | `data/bybit_state.db` (SQLite, WAL) |
| Резервные снепшоты | `data/positions_snapshot.json` (раз в час) |
| Метрики | `data/metrics.json` |
| Пампы | `data/pumps.json` |
| Бумажные позиции | `data/paper_state.db` |
| Логи сервиса | `journalctl -u bybit-ws` |

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

## Что дальше (Фаза 3)

- [ ] ML-скоринг сигналов
- [ ] Trailing Stop для x10
- [ ] Partial TP
- [ ] Бэктестинг на исторических данных
- [ ] Авто-фандинг-ротация
