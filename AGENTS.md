# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Не пересказывает код — даёт контекст.

## Что это

Трейдинг-монитор для Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT по BB-полосам).  
Работает как systemd-сервис (`bybit-ws-async`), ~35 MB RAM, SQLite — единственный источник истины (SSOT).

**ML-статус:** Фаза 5 завершена. Ансамбль RF+LSTM+RL. Feature flag `BYBIT_ML_ENABLED=0` → быстрый откат.
**Безопасность:** Все ML-модели подписываются HMAC-SHA256. RCE через подмену моделей закрыт.
**Аудит:** 23/47 находок исправлено (7C + 12H + 4M). 24 MEDIUM/LOW осталось.
**Документация:** `bybit-ws-full.md` — единый источник истины. `DESIGN-STRATEGIES.md` архивирован → `docs/archive/`.

## Структура (ключевые файлы)

```
bybit-ws/
├── main_async.py         ← Точка входа (продакшен, asyncio, цикл 30с)
├── main.py               ← Синхронная версия (совместимость)
├── api.py                ← Bybit v5 REST API (httpx)
├── rpc.py                ← JSON-RPC сервер + /metrics + /rpc/ml_toggle
├── state_db.py           ← SQLite SSOT (8 таблиц, WAL)
│
├── auto_entry.py         ← Авто-вход LONG + ML_ENABLED фича-флаг
├── auto_short.py         ← Авто-SHORT (Tier A/B + JUNK)
├── auto_sl.py            ← Авто-SL + BE-SL + ATR-сайзинг
├── auto_tp.py            ← Авто-TP (ретрей с backoff)
├── trailing_sl.py        ← Трейлинг-SL
│
│   # ── ML (Фаза 5) ──
├── ml_scorer.py          ← RF-модель + HMAC-подпись
├── lstm_regime.py        ← LSTM-режим + HMAC-подпись скалера
├── rl_agent.py           ← DQN-агент (SB3)
├── ensemble.py           ← Ансамбль RF+LSTM+RL (веса 0.34/0.33/0.33)
├── ab_test.py            ← A/B-тест ML Gate
│
│   # ── Риск ──
├── position_sizing.py    ← Динамическая маржа
├── correlation.py        ← Корреляционная матрица
├── x10_limits.py         ← Дневной лимит x10
│
│   # ── Инфра ──
├── deploy.sh             ← Атомарный деплой с rollback
├── walk_forward_validate.py ← Walk-forward ML-валидация
├── alerts.py             ← Telegram-алерты
├── web/dashboard.html    ← Дашборд v5.0 (127.0.0.1:9999)
│
└── test_smoke.py         ← 16 тестов
    test_modules.py       ← 5 тестов
    test_ml_smoke.py      ← 3 ML-теста (HMAC, RF, LSTM)
```

## Ключевые файлы и что в них

### `main_async.py` — главный цикл (продакшен, asyncio)
- `_run_heavy_cycle()` — основные проверки (каждые 120 сек)
- `_run_x10_cycle()` — проверка x10 позиций
- `_run_safety_checks()` — проверка безопасности
- `check_breakeven_sl()` — авто-безубыток при +10% профита (каждые 4 цикла)
- Вызывает: `auto_sl`, `auto_short`, `trailing_sl`, `pump_detect`

### `ml_scorer.py` — ML Gate (Random Forest)
- Модель: RandomForestClassifier, F1=0.921
- HMAC-подпись: `_hmac_sign()` / `_hmac_verify()` с `hmac.compare_digest()`
- Порог: probability > 0.22
- Fail-closed: ошибка → 0.5 (нейтрально)

### `ensemble.py` — Ансамбль RF+LSTM+RL
- Веса: RF=0.34, LSTM=0.33, RL=0.33
- Порог: 0.45
- WAIT = SKIP (не голосует «за вход»)
- Feature flag: `BYBIT_ML_ENABLED=0` отключает весь ML

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
systemctl --user start bybit-ws-async
systemctl --user status bybit-ws-async

# Деплой
bash deploy.sh

# Тесты
python3 test_smoke.py          # 16 тестов
python3 test_scanner_smoke.py  # Тесты сканера
python3 test_modules.py        # 5 модульных тестов
python3 test_ml_smoke.py       # 3 ML теста

# Метрики
curl http://localhost:8766/metrics
```

## Как тестировать

- `test_smoke.py` — 16 интеграционных тестов
- `test_scanner_smoke.py` — тесты сканера сигналов
- `test_modules.py` — 5 модульных тестов
- `test_ml_smoke.py` — 3 ML smoke теста (HMAC, RF load, LSTM fallback)
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
| Сервис | `systemctl --user bybit-ws-async` |
| Синхронизация | `cp ~/bybit-ws/{file}.py ~/.local/lib/bybit_ws/` |

## Конвенции

- **Python 3.11+**, зависимости через `pip`
- **Никаких ключей в коде** — всё из `.env`
- **SQLite — SSOT** (единственный источник истины), JSON — резерв
- **RPC-авторизация** обязательна всегда
- **JUNK-стратегии** отключены через feature flag `enabled: false`
- **Коммиты:** осмысленные на русском, с хешами в логах
- **Перед опасными операциями** — бэкап через `hermes-backup` skill

## Что уже реализовано

### Фаза 1–2 (стабильность + надёжность)
- SQLite миграция (SSOT)
- SHORT-трейлинг (зеркальный LONG)
- Защита SL от перезатирания
- Авто-безубыток (+10%)
- Paper Trading API
- Prometheus /metrics
- main_loop разбит на 3 функции

### Фаза 3 (умный трейдинг)
- ML-скоринг сигналов — RandomForest F1=0.69 → F1=0.921
- Trailing Stop для x10
- Partial TP — динамический сплит 20/80→50/50
- Авто-фандинг-ротация

### Фаза 4 (масштабирование)
- ATR-based риск-сайзинг
- Multi-timeframe конфлюенс (D/W/M, ≥2/3)
- Telegram-алерты
- WebSocket live-цены/BB
- httpx (подготовка к asyncio)
- Дашборд v5.0 (127.0.0.1:9999)

### Фаза 5 (ML) ✅
- RandomForest ML Gate
- LSTM-классификатор рыночного режима (5 классов)
- RL-агент (DQN, Stable-Baselines3)
- Ансамбль RF+LSTM+RL (взвешенное голосование)
- A/B-тест ML Gate vs baseline
- HMAC-подпись всех моделей
- Feature flag `BYBIT_ML_ENABLED=0`

### Аудит 18.06.2026
- 23/47 находок исправлено (7C + 12H + 4M)
- Атомарный деплой с rollback (`deploy.sh`)
- RPC `/rpc/ml_toggle` — быстрый откат ML
- Watchdog с проверкой зависания цикла
- Расширенный бэкап (конфиг + модели + трейды)
- Walk-forward валидация ML
- ML smoke-тесты (3 шт.)

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

## Аудит 18.06.2026

Полный аудит трёх эшелонов (Source-Driven + Security + Adversarial).
Исправлено: 23 находки (7 CRITICAL + 12 HIGH + 4 MEDIUM). 24 MEDIUM/LOW осталось.
HMAC-подпись моделей закрывает RCE-вектор.

**Документация:** `bybit-ws-full.md` v2.1 — единый источник истины. `DESIGN-STRATEGIES.md` архивирован.
