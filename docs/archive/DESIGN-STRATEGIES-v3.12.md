# Bybit WS Monitor — Архитектура и Стратегии

> **Версия:** 3.12.0 | **Дата:** 16.06.2026 | **Фаза 2 завершена**
> **Репозиторий:** [github.com/poliakarmai/bybit-ws](https://github.com/poliakarmai/bybit-ws) (AGPL-3.0)
> **Строк кода:** ~9500 Python (47 модулей) | **Тесты:** 45 smoke (test_smoke.py)
> **Модель угроз:** реальные деньги на Bybit, 24/7 автоторговля, circuit breaker + risk limits

---

## 1. Архитектура

### 1.1. Главный цикл (main.py, ~1000 строк)

```
┌─────────────────────────────────────────────────┐
│              main.py — цикл 30 сек               │
│                                                  │
│  ЛЁГКИЙ ЦИКЛ (каждые 30 сек)                     │
│  ├── fetch_positions() + fetch_orders()          │
│  ├── auto_sl() — поставить SL где нет            │
│  ├── auto_tp() — тейк-профит LONG + SHORT        │
│  ├── trailing_sl() — подтяжка SL LONG + SHORT    │
│  ├── junk_trail() — трейлинг-TP JUNK-шортов      │
│  ├── dca() — лесенка                             │
│  ├── recycle() — перезаход после SL              │
│  ├── health() — ликвидация, просадка, фандинг     │
│  └── check_strategy_compliance() — аудит TP/SL   │
│                                                  │
│  ТЯЖЁЛЫЙ ЦИКЛ (каждые 10 циклов = 5 мин)          │
│  ├── auto_entry() — LONG scoring                 │
│  ├── auto_short() — SHORT + JUNK                 │
│  ├── x10: bb_scalp, mean_revert, funding_entry    │
│  ├── pump_detect() — дневной/недельный памп       │
│  ├── correlation() — матрица корреляций          │
│  └── overbought(), squeeze(), regime()            │
│                                                  │
│  Каждые 480 циклов (4 часа)                       │
│  └── check_coverage_summary() — сводка TP/SL     │
└─────────────────────────────────────────────────┘
```

### 1.2. Дерево модулей

```
bybit-ws/
├── main.py              # главный цикл, оркестрация
├── api.py               # нативный REST-клиент Bybit (requests + HMAC)
├── state_db.py           # SQLite (WAL, 8 таблиц) — замена 15 JSON-файлов
├── config.py             # YAML-конфигурация
├── alerts.py             # алерты + дедупликация
│
├── Стратегии LONG:
│   ├── auto_entry.py     # LONG вход по 9-метричному scoring
│   ├── auto_tp.py        # авто-TP (LONG + SHORT, сплит 20/80)
│   ├── dca.py            # DCA-лесенка (-5/-10/-15%)
│   └── sl_reentry.py     # перезаход после SL
│
├── Стратегии SHORT:
│   ├── auto_short.py     # SHORT вход (перегрев BB)
│   ├── auto_sl.py        # авто-SL (BB-based LONG, tier-based SHORT)
│   ├── trailing_sl.py    # трейлинг-SL (LONG + SHORT)
│   └── junk_trail.py     # трейлинг-TP для JUNK-шортов
│
├── x10 стратегии (плечо 10x):
│   ├── bb_scalp.py       # BB Scalping M5
│   ├── mean_revert.py    # Mean Reversion
│   ├── funding_entry.py  # Funding Momentum
│   ├── atr_sizer.py      # ATR Risk Sizing
│   ├── x10_limits.py     # Дневной лимит убытков
│   └── position_sizing.py# Динамическая маржа
│
├── Риск-менеджмент:
│   ├── health.py         # ликвидация, просадка, squeeze
│   ├── correlation.py    # корреляционная матрица
│   ├── margin_alerts.py  # контроль маржи
│   ├── cost_tracker.py   # учёт комиссий и PnL
│   ├── funding_tracker.py# экстремальные ставки фандинга
│   └── regime.py         # классификация рыночного режима
│
├── Аналитика:
│   ├── pump_detect.py    # детектор пампов (24ч + недельные)
│   ├── overbought.py     # детектор перегрева BB
│   ├── rsi.py            # RSI-дивергенции
│   ├── squeeze.py        # BB-сжатие
│   ├── reporting.py      # сводки + compliance-аудит (LONG+SHORT)
│   └── metrics.py        # метрики успешности
│
├── Инфраструктура:
│   ├── rpc.py            # HTTP-RPC сервер (:8766)
│   ├── dashboard.py      # SVG-дашборд
│   ├── cleanup.py        # авто-снятие просроченных ордеров
│   ├── snapshot.py       # снепшоты позиций/ордеров
│   ├── utils.py          # tier, lot, tick
│   ├── file_utils.py     # атомарная запись JSON
│   └── manual_positions.py# трекинг ручных позиций
│
├── Сканер (отдельный):
│   └── gridsignal_scanner.py  # GridSignal v4.1 — scoring LONG/SHORT/x10
│
└── Тесты:
    ├── test_smoke.py     # 45 smoke-тестов (trailing_sl, state_db, auto_sl, api)
    ├── test_modules.py   # модульные тесты (health, auto_sl, pump_detect)
    └── test_scanner_smoke.py  # RSI/BB smoke tests
```

### 1.3. Данные

```
~/.local/share/bybit-ws/
├── state.db            # SQLite (WAL) — единое хранилище
│   ├── trade_history   # аудит сделок (PnL, комиссии, стратегия)
│   ├── positions       # кэш открытых позиций
│   ├── short_state     # состояние автошорта
│   ├── pump_state      # трекинг пампов
│   ├── x10_limits      # дневной лимит x10
│   ├── x10_positions   # трекинг x10 позиций
│   ├── cooldowns       # кулдауны (SL/TP/входы)
│   ├── alert_dedup     # дедупликация алертов
│   └── kv_store        # key-value (daily_equity, etc.)
├── events.log          # основной лог (ротация 50MB × 7)
├── trades.jsonl        # журнал закрытых сделок
├── positions.json      # снепшот позиций (JSON, dual-write)
├── orders.json         # снепшот ордеров
├── metrics.json        # дневные метрики
├── correlation.json    # последний снепшот корреляций
└── health.txt          # timestamp последнего цикла
```

### 1.4. API-клиент (api.py)

```
До Фазы 2 (v3.10):   subprocess(BYBIT_CLI) → CLI-вызовы, 50-500ms latency
После Фазы 2 (v3.12): requests.Session() → прямой HTTP, 5-50ms, connection reuse

Характеристики:
├── HMAC-SHA256 аутентификация (X-BAPI-* заголовки)
├── Retry: 3 попытки с backoff (1s/3s/5s), 429 → exponential
├── Session reuse (keep-alive)
├── Timeout: 15s
└── Все 6 endpoint-ов задокументированы со ссылками на Bybit v5 docs
```

### 1.5. RPC-сервер (rpc.py, порт 8766)

```
До Фазы 2:   subprocess(BYBIT_CLI) для API-запросов
             subprocess(python3 SCANNER) для сигналов
После Фазы 2: api.bybit() напрямую
             gridsignal_scanner.scan() напрямую

Endpoints:
├── GET  /health, /positions, /orders, /metrics, /risk, /signals, /config
├── GET  /rpc/all — все данные одним запросом
├── POST /scan   — запуск сканера (long/short/scalp/mean_revert/funding)
├── POST /enter  — ручной вход с preview + confirm
├── POST /close  — закрытие позиции рынком
├── POST /pause, /resume, /reload-config
├── Rate limiting: 60 req/min per IP
└── Auth: Bearer-токен (опционально, обязателен при bind=0.0.0.0)
```

---

## 2. Стратегии

### 2.1. Bollinger Grid LONG (основная)

| Параметр | Значение |
|----------|---------|
| Вход | Лимитный ордер на −3-5% ниже Lower BB Daily |
| TP | 20% Middle BB + 80% Upper BB (два ордера) |
| SL | −7% от Lower BB Daily |
| Trailing SL | Weekly BB > 75% + PnL > 15% → SL = entry + 15% прибыли |
| Плечо | 3x |
| Маржа | Динамическая (% депозита × score_multiplier) |
| Скоринг | 9 метрик: Tier, BB%, Volume, Down Days, Weekly/Monthly BB, Funding, RSI, Volatility, Quality |
| Макс позиций | 12 |
| Cooldown SL | 4 часа после SL |

### 2.2. Bollinger Grid SHORT (хедж)

| Параметр | Значение |
|----------|---------|
| Вход | Лимитный Sell на +2% выше рынка, BB Daily > 85% |
| TP | Middle BB |
| SL | +5% (Tier A/B), +7% (Tier C/D) |
| Trailing SL | Weekly BB < 25% + PnL > 15% → SL = entry − 15% прибыли |
| Плечо | 3x |
| Маржа | $10 |
| Макс позиций | 3 |
| ONE_WAY фильтр | XRP, ONDO, WLFI, ENJ, ESPORTS, AVAX, APT, SUI — исключены |

### 2.3. JUNK-шорт (экспериментальный)

| Параметр | Значение |
|----------|---------|
| Триггер | Рост ≥ 80% за 24ч И BB Daily ≥ 70% |
| Вход | Лимитный Sell на +2% выше рынка |
| Hard stop | −15% убытка по марже (market close) |
| Max hold | 48 часов |
| TP | Middle BB (трейлинг: 70% при +15% PnL, 85% при +30%) |
| DCA | +100% и +120% от входа |
| Плечо | 3x |
| Макс позиций | 2 |
| Недельный памп | Рост ≥ 230% за 7д → market SHORT, без SL/TP |

### 2.4. DCA (лесенка)

| Параметр | Значение |
|----------|---------|
| Уровни | −5%, −10%, −15% от входа |
| Множитель | ×2 на каждом уровне ($10 → $20 → $40) |
| Max маржа | $80 на монету |
| Max добавок | 2 |
| Circuit breaker | DCA блокируется при превышении daily_loss |

### 2.5. SL Re-entry

| Параметр | Значение |
|----------|---------|
| Триггер | SL сработал, score ≥ 6 |
| Задержка | 4 часа |
| Новый вход | Текущий Lower BB Daily |
| Маржа | ×0.5 от предыдущей |
| Максимум | 2 re-entry за 24ч на монету |

### 2.6. x10 Стратегии (высокий риск)

| Стратегия | ТФ | Вход | SL | TP | Плечо |
|-----------|----|------|----|----|-------|
| **BB Scalping** | M5 | Касание BB + RSI | 3% | Middle BB | 10x |
| **Mean Reversion** | D | BB% < 5% / > 95% | 5% | Middle BB | 10x |
| **Funding Momentum** | D | Фондинг ±0.1% + BB + тренд | 4% | Middle BB | 10x |

Общие правила x10:
- ATR Risk Sizing: размер позиции = risk_amount / (ATR × multiplier)
- Дневной лимит убытков: max_daily_loss (настраивается)
- Корреляционный фильтр: не > 2 связанных позиций
- Валидация входа: ATR check + margin check

### 2.7. Tier-классификация

```
Tier S:  BTC, ETH
Tier A:  SOL, LTC, XRP*, ADA, DOT, LINK, UNI, AVAX*, SUI*, NEAR, APT*
Tier B:  ARB, OP, AAVE, INJ, ONDO, ENA, FET, WLD, ATOM, ALGO, RUNE
Tier C/D: всё остальное (шлак)
* = ONE_WAY (SHORT невозможен)
```

---

## 3. Риск-менеджмент

### 3.1. Circuit Breaker

```
daily_loss < -max_daily_loss → блокировка ВСЕХ новых входов
total_margin >= max_total_margin → блокировка
Сброс: ежедневно в 00:00 UTC
```

### 3.2. Защиты

| Механизм | Описание |
|----------|----------|
| **Position sizing** | Динамическая маржа (% депозита × score) |
| **Max positions** | LONG: 12, SHORT: 3, JUNK: 2 |
| **Корреляционный фильтр** | Не > 2 позиций с корреляцией > 0.8 |
| **ONE_WAY фильтр** | 8 монет исключены из SHORT |
| **Sector limits** | Не > 3 позиций в одном секторе |
| **Cooldowns** | SL: 4ч, алерт: 5мин (persistent через SQLite) |
| **DCA circuit breaker** | Блокировка при превышении daily_loss |
| **Trailing SL** | LONG + SHORT: защита прибыли при движении |
| **JUNK hard stop** | −15% маржи → market close |
| **x10 daily stop** | Дневной лимит убытков для x10 стратегий |
| **Margin alerts** | Предупреждение при приближении к ликвидации |

### 3.3. Аудит compliance

```
check_strategy_compliance() — каждый цикл:
├── Проверка TP покрытия ≥ 90% (LONG + SHORT)
├── Проверка наличия SL (LONG + SHORT)
└── Алерт при нарушениях

check_coverage_summary() — каждые 4 часа:
└── Сводка: X/Y позиций защищены
```

---

## 4. Фаза 2 — что изменилось (v3.10 → v3.12)

### 4.1. SQLite (state_db.py)

```
Было:   15 JSON-файлов, гонки данных, ручная сериализация
Стало:  SQLite WAL-mode, транзакционность, 8 таблиц, dual-write JSON+SQLite
```

### 4.2. RPC без subprocess (rpc.py)

```
Было:   _run_bybit() → subprocess(BYBIT_CLI) — 50-500ms latency
        _handle_scan → subprocess(python3 SCANNER) — отдельный процесс
Стало:  _api_call() → api.bybit() — 5-50ms, connection reuse
        _handle_scan → gridsignal_scanner.scan() — прямой вызов
        -99 строк кода, убран import subprocess
```

### 4.3. Trailing SL для SHORT (trailing_sl.py)

```
Было:   Только LONG (BB > 75%, PnL > 15% → SL вверх)
Стало:  LONG + SHORT (BB < 25%, PnL > 15% → SL вниз)
        Зеркальная логика: entry - 0.15 × (entry - mark)
```

### 4.4. Compliance LONG + SHORT (reporting.py)

```
Было:   check_strategy_compliance только для Buy-позиций
        check_coverage_summary только для Buy-позиций
Стало:  Обе функции проверяют LONG и SHORT
        side_icon в сообщениях (🟢 LONG / 🔴 SHORT)
```

### 4.5. Smoke-тесты (test_smoke.py)

```
45 проверок:
├── trailing_sl: 8 тестов (LONG/SHORT/пороги/ручные/дедупликация)
├── state_db: 20 тестов (CRUD всех таблиц)
├── auto_sl: 5 тестов (tier-логика, manual)
└── api: 12 тестов (структура, retry, 429, no subprocess)
```

### 4.6. API-документация (api.py)

```
Все 6 endpoint-ов с docstrings и ссылками на Bybit v5 docs:
├── fetch_positions    → /v5/position/list
├── fetch_orders       → /v5/order/realtime
├── place_stop_loss    → /v5/position/trading-stop
├── place_take_profit  → /v5/order/create
├── cancel_order       → /v5/order/cancel
└── get_bb_data        → /v5/market/kline
```

---

## 5. Конфигурация

### 5.1. config.yaml (ключевые секции)

```yaml
api:
  key: "${BYBIT_API_KEY}"
  secret: "${BYBIT_API_SECRET}"
  base_url: "https://api.bytick.com"

risk:
  max_daily_loss: 50        # circuit breaker: блокировка при -$50
  max_total_margin: 500     # максимум суммарной маржи
  max_long_positions: 12
  max_short_positions: 3

strategy:
  long:
    tp_split: [20, 80]      # 20% Middle BB, 80% Upper BB
    sl_buffer: 0.93          # -7% от Lower BB
    min_score: 3.5
  short:
    sl_tier_ab: 1.05         # +5% для Tier A/B
    sl_tier_cd: 1.07         # +7% для шлака
    bb_entry_threshold: 85
  junk:
    enabled: false
    min_pump_pct: 80
    max_loss_pct: 15
    max_hold_hours: 48
  trailing_sl:
    percent: 0.15            # сохраняем 15% прибыли как буфер
    check_interval: 5

tiers:
  A: [SOL, LTC, ADA, DOT, LINK, UNI, NEAR]
  B: [ARB, OP, AAVE, INJ, ENA, FET, WLD, ATOM, ALGO, RUNE]
  one_way: [XRP, ONDO, WLFI, ENJ, ESPORTS, AVAX, APT, SUI]

x10:
  max_daily_loss: 25
  max_positions: 5
  atr_risk_percent: 2.0

rpc:
  port: 8766
  bind: "127.0.0.1"
  auth_token: "${RPC_TOKEN}"
  rate_limit_per_min: 60
```

### 5.2. Systemd unit

```ini
[Unit]
Description=Bybit WS Monitor
After=network.target

[Service]
Type=simple
User=openclaw
WorkingDirectory=/home/openclaw/bybit-ws
ExecStart=/home/openclaw/bybit-ws/.venv/bin/python -m bybit_ws
Restart=always
RestartSec=10

# Лимиты
MemoryMax=256M
CPUQuota=50%
TasksMax=32

[Install]
WantedBy=multi-user.target
```

---

## 6. Статистика

| Метрика | Значение |
|---------|----------|
| Модулей Python | 47 |
| Строк кода | ~9,500 |
| Стратегий | 8 (включая 3× x10) |
| Smoke-тестов | 45 (PASS) |
| Endpoint-ов RPC | 15 |
| Таблиц SQLite | 8 |
| JSON-файлов (legacy) | 5 (дублируются в SQLite) |
| Потребление RAM | ~24 MB |
| Цикл | 30 сек |
| API latency | 5-50ms (прямые вызовы) |

---

## 7. Дорожная карта

### ✅ Фаза 1: Стабильность (завершена)
Circuit breaker fix, hallucinated params, watchdog, JUNK-защита

### ✅ Фаза 2: Надёжность (завершена — 16.06.2026)
SQLite, RPC без subprocess, trailing_sl SHORT, compliance LONG+SHORT, smoke-тесты (45), API-документация

### 🟡 Фаза 3: Умный трейдинг (в плане)
Trailing Stop x10, ML-скоринг, авто-фандинг-ротация, Partial TP, бэктестинг, paper-trading

### 🟢 Фаза 4: Масштабирование (в плане)
WebSocket, мульти-аккаунт, Prometheus/Grafana, Binance/OKX, asyncio
