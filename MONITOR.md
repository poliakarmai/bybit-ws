# MONITOR.md — bybit-ws

> Полная документация трейдинг-монитора Bybit.  
> **Версия:** 3.12.0+ | **Дата:** 18.06.2026 | **Автор:** Поляков А.Ю.
> 
> ⚠️ **Раздел 10 (Текущее состояние)** — исторический снимок. Актуальные данные: `mcp_bybit_ws_get_positions()`.

---

## 1. Общая информация

**bybit-ws** — AI-Native Trading Engine для фьючерсов Bybit.  
Стратегия: **Bollinger Grid** (LONG/SHORT по BB-полосам) × 8 стратегий.

| Параметр | Значение |
|----------|---------|
| Язык | Python 3.11+ |
| База данных | SQLite (SSOT, WAL-режим) |
| API биржи | Bybit v5 REST |
| RPC-сервер | `127.0.0.1:8766` (JSON-RPC, Bearer auth) |
| MCP-сервер | Hermes MCP (stdio, порт 8766) |
| Сервис | systemd (`bybit-ws.service`) |
| Потребление | ~36 MB RAM (пик ~251 MB) |
| Репозиторий | `github.com/poliakarmai/bybit-ws` |

## 2. Архитектура

```
┌─────────────────────────────────────────────────────────┐
│                    AI-агент (Hermes)                     │
│  MCP: scan_market | place_entry | get_positions | ...   │
└──────────────────────┬──────────────────────────────────┘
                       │ stdio (MCP)
┌──────────────────────▼──────────────────────────────────┐
│              bybit-mcp-server.py                         │
│  Проксирует запросы → RPC на 127.0.0.1:8766             │
└──────────────────────┬──────────────────────────────────┘
                       │ HTTP + Bearer token
┌──────────────────────▼──────────────────────────────────┐
│              rpc.py (JSON-RPC сервер)                    │
│  /enter  /close  /scan  /positions  /metrics  /risk     │
│  /reload-config  /pause  /resume  /logs                 │
└──────────────────────┬──────────────────────────────────┘
                       │ bybit() вызовы
┌──────────────────────▼──────────────────────────────────┐
│              api.py (Bybit v5 REST)                      │
│  place_order | fetch_positions | place_stop_loss | ...  │
│  Ретрай: 429→exp backoff, 500/503→3 попытки             │
└──────────────────────┬──────────────────────────────────┘
                       │ HTTPS
┌──────────────────────▼──────────────────────────────────┐
│                 Bybit API (api.bybit.com)                │
└─────────────────────────────────────────────────────────┘
```

### Хранилище данных

| Данные | Путь | Формат |
|--------|------|--------|
| Позиции (SSOT) | `data/state.db` | SQLite, WAL |
| Резервные снепшоты | `data/positions_snapshot.json` | JSON (раз в час) |
| Метрики | `data/metrics.json` | JSON |
| Пампы | `data/pumps.json` | JSON |
| Бумажные позиции | `data/paper_state.db` | SQLite, WAL |
| Логи сервиса | `journalctl -u bybit-ws` | systemd journal |
| RPC-токен | SQLite `kv_store.rpc_auth_token` | UUID |

## 3. Модули

### Ядро трейдинга

| Файл | Строк | Назначение |
|------|-------|-----------|
| `main.py` | 464 | Главный цикл: `_run_heavy_cycle()`, `_run_x10_cycle()`, `_run_safety_checks()` |
| `api.py` | 372 | Bybit v5 REST: 6 эндпоинтов, HMAC-подпись, ретрай |
| `config.py` | 479 | Все константы, пороги, лимиты, флаги стратегий |
| `state_db.py` | 390 | SQLite SSOT: 8 таблиц, WAL, потокобезопасность |

### Стоп-лоссы и трейлинг

| Файл | Строк | Назначение |
|------|-------|-----------|
| `auto_sl.py` | ~240 | Авто-стоплоссы + защита от перезатирания + безубыток (+10%) |
| `trailing_sl.py` | ~260 | Трейлинг-стопы: LONG↑ / SHORT↓, порог 0.5% |
| `auto_tp.py` | 152 | Авто-тейк-профиты: динамический сплит 20/80→50/50 |

### Входы и стратегии

| Файл | Строк | Назначение |
|------|-------|-----------|
| `auto_short.py` | 498 | Авто-шорты по BB-сигналам + DCA + закрытие |
| `auto_entry.py` | 261 | Скоринг монет + авто-входы по сигналам |
| `gridsignal_scanner.py` | ~900 | Сканер сигналов Bollinger Grid |
| `bb_scalp.py` | 228 | Скальпинг по BB-полосам |
| `dca.py` | 170 | DCA-стратегия, усреднение позиций |
| `mean_revert.py` | ~200 | Mean reversion на дневных BB |

### Риск-менеджмент

| Файл | Строк | Назначение |
|------|-------|-----------|
| `risk/` | — | Риск-менеджмент (отдельная папка) |
| `pump_detect.py` | ~280 | Детектор пампов: -46% за 15m → шорт |
| `overbought.py` | ~100 | Детектор перекупленности: BB% > 100% |
| `correlation.py` | 203 | Корреляционный анализ, дедупликация алертов (12ч) |
| `atr_sizer.py` | 168 | ATR-based расчёт размера позиции |
| `margin_alerts.py` | ~100 | Алерты по марже |

### Фандинг и ротация

| Файл | Строк | Назначение |
|------|-------|-----------|
| `funding_rotation.py` | 350 | Авто-ротация по фандингу |
| `funding_entry.py` | 225 | Входы по фандинг-сигналам |
| `funding_tracker.py` | 234 | Трекинг ставок фандинга, история |

### Инфраструктура

| Файл | Строк | Назначение |
|------|-------|-----------|
| `rpc.py` | 934 | JSON-RPC сервер + Prometheus /metrics |
| `alerts.py` | 177 | JSON-алерты, ротация, Telegram |
| `reporting.py` | ~200 | Ежедневные отчёты |
| `snapshot.py` | ~100 | Снепшоты позиций (JSON, резерв) |
| `metrics.py` | ~100 | Сбор и хранение метрик |
| `paper_api.py` | ~600 | Paper Trading: симулятор биржи |
| `backtest.py` | ~300 | Бэктестинг: walk-forward на klines |
| `web/proxy_server.py` | 40 | Веб-интерфейс (прокси) |

### ML

| Файл | Строк | Назначение |
|------|-------|-----------|
| `ml_scorer.py` | ~400 | ML-скоринг сигналов: RandomForest F1=0.69 |

## 4. Стратегии

### Активные (8 стратегий)

1. **Bollinger Grid LONG** — лимитные входы на нижней BB, TP на верхней
2. **Bollinger Grid SHORT** — лимитные входы на верхней BB, TP на нижней
3. **Auto-Entry** — скоринг монет (ML + Tier + BB% + RSI), авто-входы
4. **DCA** — усреднение позиций при падении цены
5. **Mean Reversion** — отскок от дневных BB-полос
6. **BB Scalp** — скальпинг по BB-полосам
7. **Funding Rotation** — ротация позиций по ставкам фандинга
8. **Pump Detect** — детектор пампов: резкое падение → шорт

### Неактивные (feature flag `enabled: false` в `config.yaml` → `strategy.junk`)

- JUNK-стратегии (DCA junk, junk trail)

### Параметры стратегий

| Параметр | Значение | Примечание |
|----------|---------|-----------|
| Плечо по умолчанию | 10x | |
| Интервал BB | D (дневной) | |
| BB-период | 20 свечей | |
| ML-скоринг | RandomForest, F1=0.69, вес 70/30 | Модель: `data/ml_scorer.pkl` |
| Порог безубытка | +10% профита → SL = entry × 1.01 | |
| Порог корреляции | r > ±0.8 → алерт | |
| Дедупликация корреляций | 12 часов | |
| Макс. дневной убыток | -$50 | Блокирует *новые* входы, не закрывает текущие позиции |
| Макс. общая маржа | $500 | |
| ATR-множитель | 2.0 | Стоп-дистанция = ATR × multiplier |
| ATR риск на сделку | $5 | Бюджет риска для ATR-сайзинга |
| ATR кеш | 4 часа | ~/.local/share/bybit-ws/atr_cache.json |

### Поведение при рисках

| Ситуация | Действие |
|----------|---------|
| Достигнут `max_daily_loss` (-$50) | 🛑 Блокировка новых входов. Текущие позиции остаются, SL/TP работают. `get_risk_status` → `blocked: true, reasons: ["daily_loss_limit"]` |
| Достигнут `max_total_margin` ($500) | 🛑 Блокировка новых входов. `get_risk_status` → `blocked: true, reasons: ["margin_limit"]` |
| Корреляция r > ±0.8 | ⚠️ Алерт. SL поджимается на 1% ближе к марку. Пример: SL был $5.00, стал $5.05 (LONG) |
| Памп -46% за 15m | 🚀 Сигнал на шорт. Защита от flash crash: шорт только если объём > среднего за 4h. Ждём подтверждения на 5m таймфрейме перед входом |

## 5. MCP-инструменты

MCP-сервер: `/home/openclaw/.local/bin/bybit-mcp-server.py`

### scan_market
Сканирование рынка на сигналы Bollinger Grid.

| Параметр | Тип | Описание |
|----------|-----|---------|
| `mode` | `"long" \| "short"` | Направление сканирования |
| `interval` | `"D" \| "W" \| "4h" \| "1h" \| "15m" \| "5m"` | Таймфрейм |
| `limit` | `int` | Макс. результатов (default: 10) |

**Пример ответа:**
```
ADAUSDT  Score=6.6  Tier=A  BB=35%  RSI=33  $0.1697 → entry $0.1249
```

### get_positions
Текущие позиции с нереализованным PnL, стоп-лоссами и плечом.

**Пример ответа:**
```json
[
  {
    "symbol": "LINKUSDT",
    "side": "Buy",
    "entry": 8.289,
    "mark": 8.315,
    "upnl": 0.36,
    "size": 14.0,
    "stopLoss": 5.311,
    "leverage": 10,
    "positionIdx": 1,
    "liqPrice": 0.01,
    "positionIM": 11.61,
    "cumRealisedPnl": 0.0,
    "openTime": 1781711576
  }
]
```

### get_metrics
Дневные метрики: TP/SL счёт, входы, авто-входы.

**Пример ответа:**
```json
{
  "tp_real": 1,
  "sl_real": 4,
  "entry": 0,
  "auto_entry_filled": 0,
  "auto_entry_pnl": 0.0
}
```

### get_risk_status
Лимиты риска: дневной PnL, маржа, блокировки.

**Пример ответа (норма):**
```json
{
  "blocked": false,
  "daily_loss": 0.0,
  "max_daily_loss": 50,
  "total_margin": 84.96,
  "max_total_margin": 500,
  "position_count": 8,
  "remaining_daily_loss": 50.0,
  "remaining_margin": 415.04,
  "reasons": []
}
```

**Пример ответа (блокировка):**
```json
{
  "blocked": true,
  "daily_loss": -52.30,
  "max_daily_loss": 50,
  "total_margin": 140.0,
  "max_total_margin": 500,
  "position_count": 5,
  "remaining_daily_loss": 0.0,
  "remaining_margin": 360.0,
  "reasons": ["daily_loss_limit"]
}
```

### place_entry
Вход в позицию (Market или Limit). **Важно:** если позиция по `symbol` уже существует, RPC вернёт ошибку `409 Conflict` и не удвоит объём.

### place_entry
Вход в позицию (Market или Limit).

| Параметр | Тип | Обязательный | Описание |
|----------|-----|-------------|---------|
| `symbol` | `str` | ✅ | Торговая пара (e.g. LINKUSDT) |
| `side` | `"Buy" \| "Sell"` | ✅ | Buy=LONG, Sell=SHORT |
| `qty` | `float` | ✅ | Количество в базовых единицах |
| `sl` | `float` | ❌ | Стоп-лосс |
| `tp` | `float` | ❌ | Тейк-профит |
| `order_type` | `"Market" \| "Limit"` | ❌ | Тип ордера (default: Market) |
| `price` | `float` | ❌ | Цена для Limit-ордера |

**Market** — мгновенное исполнение. **Limit** — GTC-ордер по указанной цене.
SL/TP ставятся автоматически после исполнения.

### Типичный воркфлоу AI-агента
```
1. scan_market(mode)     → выбрать кандидатов
2. get_risk_status()      → проверить лимиты
3. get_positions()        → убедиться, что позиции нет
4. place_entry(...)       → войти
```

## 6. RPC API

JSON-RPC сервер на `127.0.0.1:8766`. Авторизация: `Authorization: Bearer <token>`.

| Метод | Тип | Путь | Описание |
|-------|-----|------|---------|
| index | GET | `/` или `/rpc` | Список эндпоинтов |
| health | GET | `/health` | Статус сервиса |
| positions | GET | `/positions` | Текущие позиции |
| orders | GET | `/orders` | Активные ордера |
| metrics | GET | `/metrics` | Prometheus-метрики |
| risk | GET | `/risk` | Лимиты риска |
| signals | GET | `/signals` | Активные сигналы |
| config | GET | `/config` | Конфигурация (без секретов) |
| all | GET | `/rpc/all` | Все данные разом |
| scan | POST | `/scan` | Скан BB-сигналов |
| **enter** | **POST** | **`/enter`** | **Вход в позицию (Market/Limit)** |
| close | POST | `/close` | Закрыть позицию |
| reload-config | POST | `/reload-config` | Перечитать config.yaml |
| pause | POST | `/pause` | Приостановить торговлю |
| resume | POST | `/resume` | Возобновить торговлю |
| logs | POST | `/logs` | Последние строки events.log |

### `/enter` — тело запроса

```json
{
  "symbol": "LINKUSDT",
  "side": "Buy",
  "qty": 14,
  "sl": 5.31,
  "tp": null,
  "order_type": "Limit",
  "price": 6.774,
  "confirm": true
}
```

### Коды ошибок RPC

| HTTP | Код | Описание | Действие AI-агента |
|------|-----|---------|-------------------|
| 400 | `invalid_symbol` | Неверный символ | Проверить тикер |
| 400 | `invalid_side` | side не Buy/Sell | Исправить параметр |
| 400 | `order_failed` | Биржа отклонила ордер | Проверить qty/price, повторить |
| 401 | `unauthorized` | Неверный Bearer-токен | Проверить `RPC_TOKEN` |
| 402 | `insufficient_margin` | Недостаточно маржи | Уменьшить qty или закрыть позицию |
| 404 | `symbol_not_found` | Тикер не найден | Проверить тикер |
| 409 | `position_exists` | По этому символу уже есть позиция | Не входить повторно |
| 422 | `invalid_qty` | Неверное количество | Проверить lot size |
| 429 | `rate_limit` | Слишком много запросов | Подождать 1 сек |

## 7. Конфигурация

### Файлы

| Файл | Назначение |
|------|-----------|
| `~/.config/bybit-ws/config.yaml` | Основной конфиг |
| `~/.config/bybit-cli/config` | API-ключи Bybit |
| `~/.config/bybit-ws/.env` | Переменные окружения (Telegram, RPC) |

### Ключевые параметры

```yaml
# config.yaml
risk:
  max_daily_loss: 50        # Макс. дневной убыток ($)
  max_total_margin: 500     # Макс. общая маржа ($)

rpc:
  port: 8766
  bind: "127.0.0.1"
  auth_token: "${RPC_TOKEN}"

strategy:
  bollinger_grid:
    interval: "D"           # Таймфрейм BB
    period: 20              # Период BB
    entry_offset: 0.95      # Вход на 95% нижней BB
```

### Переменные окружения

| Переменная | Назначение |
|-----------|-----------|
| `BYBIT_API_KEY` | API-ключ Bybit |
| `BYBIT_API_SECRET` | API-секрет Bybit |
| `RPC_TOKEN` | Bearer-токен для RPC |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота |
| `TELEGRAM_CHAT_ID` | ID чата для алертов |
| `DEEPSEEK_API_KEY` | API-ключ DeepSeek (для ML-скоринга и graphify) |

### Бэкап SQLite (SSOT)

```bash
# Создать резервную копию
sqlite3 ~/.local/share/bybit-ws/state.db ".backup ~/bybit-ws/backups/state_$(date +%Y%m%d_%H%M).db"

# Или через WAL-чекпоинт + копирование
sqlite3 ~/.local/share/bybit-ws/data/bybit_state.db "PRAGMA wal_checkpoint(TRUNCATE)"
cp ~/.local/share/bybit-ws/data/bybit_state.db ~/backups/
```

JSON-снепшоты (`positions_snapshot.json`) — резервный канал. SSOT — SQLite. Бэкап обеих копий рекомендуется.

### Генерация/сброс RPC-токена

```bash
# Посмотреть текущий токен
python3 -c "import sqlite3; print(sqlite3.connect('$HOME/.local/share/bybit-ws/state.db').execute(\"SELECT value FROM kv_store WHERE key='rpc_auth_token'\").fetchone()[0])"

# Сбросить (сгенерировать новый)
python3 -c "
import sqlite3, uuid
conn = sqlite3.connect('$HOME/.local/share/bybit-ws/state.db')
conn.execute(\"INSERT OR REPLACE INTO kv_store (key, value) VALUES ('rpc_auth_token', ?)\", (str(uuid.uuid4()),))
conn.commit()
print('New token generated. Restart bybit-ws and hermes gateway.')
"

## 8. Запуск и управление

```bash
# Сервис
sudo systemctl start bybit-ws
sudo systemctl stop bybit-ws
sudo systemctl restart bybit-ws
sudo systemctl status bybit-ws

# Логи
# events.log — для AI-агентов и RPC (/logs)
# journalctl — для системного администрирования
journalctl -u bybit-ws -f        # follow (в реальном времени)
journalctl -u bybit-ws --since "1 hour ago"
tail -f ~/.local/share/bybit-ws/events.log  # RPC-лог (тот же что в /logs)

# Тесты (venv2)
cd ~/bybit-ws && source .venv2/bin/activate
python test_smoke.py              # 45 тестов
python test_scanner_smoke.py      # Тесты сканера
python test_modules.py            # Модульные тесты

# Метрики Prometheus
curl http://localhost:8766/metrics

# RPC (требуется Bearer-токен)
TOKEN=$(python3 -c "import sqlite3; print(sqlite3.connect('$HOME/.local/share/bybit-ws/state.db').execute(\"SELECT value FROM kv_store WHERE key='rpc_auth_token'\").fetchone()[0])")
curl -H "Authorization: Bearer $TOKEN" http://localhost:8766/health
```

## 9. Мониторинг

### Prometheus-метрики (порт 8766)

| Метрика | Тип | Описание |
|---------|-----|---------|
| `bybit_ws_active_positions` | gauge | Активные позиции (long/short) |
| `bybit_ws_unrealized_pnl` | gauge | Нереализованный PnL |
| `bybit_ws_uptime_seconds` | gauge | Аптайм монитора |
| `bybit_ws_cycle_duration_seconds` | gauge | Длительность последнего цикла |
| `bybit_ws_cycle_count` | counter | Всего циклов |
| `bybit_ws_daily_pnl` | gauge | Дневной реализованный PnL |

### Алерты

| Тип | Триггер |
|-----|--------|
| 🛑 SL Hit | Сработал стоп-лосс |
| ⚠️ Корреляция | r > ±0.8 между позициями |
| 🚀 Памп | -46% за 15 минут |
| 💸 Маржа | Маржа > лимита |
| 📌 Защита SL | SL зафиксирован в прибыли |
| 🎯 Partial TP | Частичный тейк-профит |
| 📈📉 Вход | Новая позиция |

## 10. Текущее состояние

> Снимок: **17.06.2026 19:25 MSK**

### Позиции (8 шт, PnL: **+$6.43**)

| Символ | Тип | Плечо | Вход | Марк | PnL | SL |
|--------|-----|-------|------|------|-----|-----|
| XLM 🟢 | LONG | 10x | $0.1866 | $0.2276 | **+$12.68** | $0.222 🔒 |
| ADA 🟢 | LONG | 10x | $0.1686 | $0.1717 | +$2.43 | $0.1221 |
| DOGE 🟢 | LONG | 10x | $0.0862 | $0.0873 | +$1.54 | $0.0717 |
| DOT 🟢 | LONG | 10x | $1.0108 | $1.0360 | +$0.69 | $0.7848 |
| LINK 🟢 | LONG | 10x | $8.2890 | $8.3150 | +$0.36 | $5.311 |
| AVAX 🔴 | LONG | 10x | $6.9680 | $6.9630 | -$0.04 | $5.139 |
| WLD 🔴 | SHORT | 10x | $0.6585 | $0.6630 | -$0.72 | $0.725 |
| MOVE 🔴 | LONG | 10x | $0.0134 | $0.0128 | **-$10.51** | $0.009 |

### Риски

| Параметр | Значение |
|----------|---------|
| Статус | 🟢 OK |
| Дневной PnL | $0.00 / -$50 |
| Маржа | $84.96 / $500 (17%) |
| Блокировки | Нет |

### Метрики за сегодня

| Метрика | Значение |
|---------|---------|
| TP | 1 |
| SL | 4 |
| Входы | 0 |
| Авто-входы | 0 |

### Корреляции (r > ±0.8)

| Пары | r | Действие |
|------|---|---------|
| AVAX ↔ LINK | +0.948 | Ужесточить SL |
| DOGE ↔ LINK | +0.947 | Ужесточить SL |
| AVAX ↔ BTC | +0.828 | Ужесточить SL |
| ADA ↔ LINK | +0.884 | Ужесточить SL |

## 11. Фазы разработки

### Фаза 1 ✅ — Базовая инфраструктура
- Bollinger Grid LONG/SHORT
- SQLite SSOT
- systemd-сервис
- Telegram-алерты

### Фаза 2 ✅ — Умные стопы
- SQLite миграция
- SHORT-трейлинг (зеркальный LONG)
- Защита SL от перезатирания
- Авто-безубыток (+10%)
- Paper Trading API
- Prometheus /metrics
- 45 smoke-тестов

### Фаза 3 ✅ — ML и оптимизация
- ML-скоринг сигналов (RandomForest F1=0.69)
- Trailing Stop для x10
- Partial TP (динамический сплит 20/80→50/50)
- Бэктестинг (walk-forward)
- Авто-фандинг-ротация
- MCP-сервер для AI-агентов
- RPC `/enter` с поддержкой Limit-ордеров

### Фаза 4 🔜 — Продвинутый риск-менеджмент
- [ ] ATR-based риск-сайзинг
- [ ] Multi-timeframe конфлюенс (D/W/M)
- [ ] Алерты в Telegram при входе/выходе
- [ ] Дашборд Grafana
- [ ] Полный Understand Anything анализ кодовой базы

## 12. Интеграция с Hermes

### Оркестрация

bybit-ws — часть экосистемы Hermes (Море):

```
Море (default) ──MCP──▶ bybit-ws (трейдинг)
    │                       │
    ├── Morearbot           ├── RPC :8766
    └── Apolai              ├── Prometheus :8766/metrics
                            └── systemd
```

### Email-оповещения

Два пути к почте:
- **himalaya** (IMAP/SMTP) — 🟢 основной, без OAuth
- **gws** (Google Workspace CLI) — 🔴 ожидает OAuth-реавторизации

### Крон-джобы Hermes

| Джоб | Интервал | Описание |
|------|---------|---------|
| `mail-sort` | 10 мин | Автосортировка писем по папкам |
| `crypto-daily-digest` | 09:00 MSK | Ежедневный крипто-дайджест |
| `system-improvement-loop` | 09:00 MSK | Анализ логов → предложения по скиллам |

## 14. Словарь терминов

| Термин | Расшифровка |
|--------|------------|
| **BB** | Bollinger Bands (полосы Боллинджера): SMA ± 2σ |
| **BB%** | Позиция цены внутри BB-полос: 0% = нижняя, 100% = верхняя |
| **SSOT** | Single Source of Truth — единственный источник истины (SQLite) |
| **WAL** | Write-Ahead Logging — режим SQLite для конкурентного доступа |
| **MCP** | Model Context Protocol — протокол для AI-инструментов |
| **RPC** | Remote Procedure Call — JSON-RPC сервер на :8766 |
| **GTC** | Good-Til-Cancelled — ордер активен пока не отменят |
| **IOC** | Immediate-Or-Cancel — исполнить немедленно или отменить |
| **DCA** | Dollar Cost Averaging — усреднение позиции докупками |
| **TP** | Take Profit — тейк-профит |
| **SL** | Stop Loss — стоп-лосс |
| **heavy_cycle** | Основной цикл (120 сек): SL/TP, трейлинг, пампы |
| **x10_cycle** | Цикл для плеча ≥10x (отдельная логика) |
| **Tier** | Тир ликвидности монеты (S/A/B/C/D): S = BTC/ETH, A = топ-альты |
| **Score** | ML-скор (RandomForest) × вес 0.7 + Tier-бонус × 0.3 |
| **PnL** | Profit and Loss — прибыль/убыток |
| **upnl** | Unrealized PnL — нереализованный (бумажный) |
| **cumRealisedPnl** | Накопленный реализованный PnL по позиции |

## 15. Жизненный цикл сделки (Lifecycle of a Trade)

```
1. СИГНАЛ
   gridsignal_scanner.py → сканирует рынок (BB, RSI, объём)
   ml_scorer.py → применяет RandomForest + Tier-бонус
   → Candidate: {symbol, score, tier, entry_price, sl, tp}

2. ВХОД
   auto_entry.py → проверяет риск-лимиты (max_daily_loss, margin)
   → Если блокировка: пропускает
   api.py → place_order(Limit, entry_price)
   → Ордер размещён на бирже (GTC)

3. МОНИТОРИНГ (каждые 120 сек — heavy_cycle)
   main.py → _run_heavy_cycle():
   ├── api.py → fetch_positions() → получает текущие позиции
   ├── auto_sl.py → check_and_fix_sl() → ставит SL
   ├── trailing_sl.py → trailing_sl() → поджимает SL
   ├── partial_tp.py → check_partial_tp() → частичный TP
   ├── pump_detect.py → check_pumps() → детектирует пампы
   └── correlation.py → проверяет корреляции

4. ВЫХОД
   Вариант А (TP): цена достигает TP-ордера → биржа закрывает → reporting.py логирует
   Вариант Б (SL): цена достигает SL → биржа закрывает → reporting.py логирует
   Вариант В (Ручной): RPC /close → api.py → close_position()

5. ПОСТ-АНАЛИЗ
   metrics.py → обновляет дневную статистику (TP/SL счёт)
   reporting.py → проверяет триггеры (daily PnL alert)
   state_db.py → сохраняет историю сделки в trades
```

## 13. Устранение неисправностей

| Проблема | Решение |
|----------|---------|
| RPC возвращает 401 | Проверить токен: `SELECT value FROM kv_store WHERE key='rpc_auth_token'` |
| MCP не отвечает | `systemctl --user restart hermes` |
| Сервис не стартует | `journalctl -u bybit-ws -n 50`, проверить `.env` |
| SL не обновляется | Проверить защиту: SL уже в профите? |
| API 429 (rate limit) | Авто-reconnect с экспоненциальной задержкой |
| Позиция не открылась | Проверить `positionIdx` (hedge mode → 1 или 0) |
| gws 401 | Переключиться на himalaya; gws ждёт браузерной OAuth |

---

> **Конвенции:** Python 3.11+, ключи в `.env`, SQLite — SSOT, RPC-auth обязателен.  
> **Коммиты:** на русском, с хешами. Перед опасными операциями — `hermes-backup`.  
> **AGENTS.md** — навигация для AI-агентов. **DESIGN.md** — архитектурные решения.
