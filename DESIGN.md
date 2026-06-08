# Bybit Bollinger Grid Monitor — DESIGN.md

> **Версия:** 3.4 | **Дата:** 08.06.2026 | **Автор:** Alexey Polyakov
>
> Автономный трейдинг-монитор для AI-агентов. Стратегия Bollinger Grid (LONG + SHORT), 24/7 без присмотра, REST API для внешнего управления.

---

## 1. Архитектура

```
                          ┌──────────────────────────┐
                          │      AI Agent (вы)        │
                          │  Claude / GPT / Hermes    │
                          └─────┬────────────────────┘
                                │ REST API (порт 8766)
                                ▼
┌───────────────────────────────────────────────────────┐
│                   bybit-ws monitor                     │
│                                                       │
│  main.py ──► главный цикл (30 сек)                    │
│    ├── api.py          bybit-cli wrapper               │
│    ├── auto_entry.py   LONG вход по scoring            │
│    ├── auto_short.py   SHORT вход при перегреве        │
│    ├── auto_tp.py      авто-TP при профите             │
│    ├── auto_sl.py      авто-SL для позиций без стопа   │
│    ├── trailing_sl.py  подтягивание SL за ценой        │
│    ├── dca.py          DCA-добавка при падении         │
│    ├── sl_reentry.py   перезаход после SL              │
│    ├── overbought.py   детектор перегрева BB           │
│    ├── pump_detect.py  детектор пампов                 │
│    ├── rsi.py          RSI-дивергенции                 │
│    ├── squeeze.py      BB-сжатие (squeeze)             │
│    ├── health.py       ликвидация, корреляция, фандинг │
│    ├── rpc.py          HTTP-RPC сервер (:8766)         │
│    ├── cost_tracker.py учёт комиссий и PnL             │
│    └── reporting.py    сводки и трейд-журнал           │
│                                                       │
│  Данные: ~/.local/share/bybit-ws/                     │
│    ├── events.log      основной лог                    │
│    ├── trades.jsonl    журнал закрытых сделок          │
│    ├── health.txt      timestamp последнего цикла      │
│    └── positions.json  снепшот позиций                 │
└──────────────────────┬────────────────────────────────┘
                       │
                       ▼
              ┌─────────────────┐
              │   Bybit API     │
              │  api.bytick.com │
              └─────────────────┘
```

### Поток данных (главный цикл, каждые 30 сек)

```
fetch_positions() ──► fetch_orders() ──► detect_changes()
                                              │
                         ┌────────────────────┘
                         ▼
                  check_correlation()
                  check_auto_sl()
                  check_auto_tp()
                         │
              каждые 10 циклов (5 мин):
              ├── check_overbought()    → SHORT-кандидаты
              ├── check_auto_short()    → вход в SHORT
              ├── check_pumps()         → детектор пампов
              ├── check_rsi_divergence()→ медвежьи сигналы
              ├── check_squeeze()       → BB-сжатие
              └── check_funding_flip()  → разворот фандинга
```

---

## 2. Стратегия

### LONG (основная)
| Параметр | Значение |
|----------|---------|
| Вход | Лимитный ордер на −3-5% ниже Lower BB (Daily) |
| TP | Middle или Upper BB |
| SL | −7% от Lower BB |
| Плечо | 3x |
| Маржа | $15 (score ≥7), $10 (≥5.5), $5 (<5.5) |
| Скоринг | 9 метрик: Tier, BB%, объём, дни падения, Weekly/Monthly BB, фандинг, RSI |

### SHORT (хедж)
| Параметр | Значение |
|----------|---------|
| Вход | Лимитный ордер Sell на +2% выше рынка (ждём отскока) |
| TP | Middle BB |
| SL | +5% для Tier A/B, +7% для шлака C/D |
| Плечо | 3x |
| Маржа | $10 |
| Порог BB | >85% (перегрев) |
| Макс позиций | 3 |
| ONE_WAY фильтр | XRP, ONDO, WLFI, ENJ, ESPORTS, AVAX, APT, SUI — исключены |
| DCA-шорт | На аномальных пампах (>120% за 24ч) |

### Tier-классификация
```
Tier S: BTC, ETH
Tier A: SOL, LTC, XRP, ADA, DOT, LINK, UNI, AVAX, SUI, NEAR, APT
Tier B: ARB, OP, AAVE, INJ, ONDO, ENA, FET, WLD, ATOM, ALGO, RUNE
Tier C/D: всё остальное (шлак)
```

---

## 3. REST API (порт 8766)

> Все ответы содержат `api_version: "v1"`.
> При ошибке: `{"error": "...", "detail": "...", "api_version": "v1", "status": код}`.
> Аутентификация: Bearer-токен через заголовок `Authorization: Bearer <RPC_TOKEN>` (опционально, настраивается в конфиге).
> Rate limiting: 60 запросов/мин на IP (настраивается).

### GET /health
Статус монитора.

```json
// GET http://0.0.0.0:8766/health
{
  "status": "alive",
  "cycle_count": 12345,
  "uptime_seconds": 36000,
  "last_cycle_age_sec": 12,
  "watchdog_ok": true
}
```

### GET /positions
Текущие позиции.

```json
// GET http://0.0.0.0:8766/positions
{
  "count": 7,
  "long": 5,
  "short": 2,
  "total_pnl": "+20.52",
  "positions": [
    {
      "symbol": "SUIUSDT",
      "side": "Buy",
      "size": 40,
      "entry": 0.6876,
      "mark": 0.7505,
      "pnl": "+2.52",
      "pnl_pct": "+9.1",
      "sl": 0.7370,
      "tp": 0.9249
    }
  ]
}
```

### GET /orders
Активные лимитные ордера.

```json
// GET http://0.0.0.0:8766/orders
{
  "count": 7,
  "orders": [
    {
      "symbol": "SUIUSDT",
      "side": "Sell",
      "type": "Limit",
      "qty": 40,
      "price": 0.9249,
      "reduceOnly": true
    }
  ]
}
```

### GET /metrics
Метрики за сегодня.

```json
// GET http://0.0.0.0:8766/metrics
{
  "alerts": {"INFO": 15, "STOP": 3, "ENTRY": 2},
  "auto_entries": 2,
  "sl_hits": 2,
  "tp_hits": 0,
  "watchdog_restarts": 0
}
```

### GET /signals
Текущие сигналы (LONG + SHORT кандидаты).

```json
// GET http://localhost:8766/signals
{
  "api_version": "v1",
  "long": [
    {"symbol": "ADAUSDT", "score": 7.2, "tier": "A", "bb_pct": 25, "raw": "..."}
  ],
  "short": [
    {"symbol": "SIRENUSDT", "bb_pct": 118, "price": 1.305, "upper": 1.124}
  ]
}
```

### GET /config
Текущая конфигурация (без секретов).

```json
// GET http://localhost:8766/config
{
  "api_version": "v1",
  "strategy": {"long": {...}, "short": {...}},
  "risk": {"max_drawdown_pct": 15, "max_total_margin": 500, ...},
  ...
}
```

### POST /scan
Запустить полный скан (LONG + SHORT). Возвращает сигналы GridSignal-сканера.

```
POST /scan
Content-Type: application/json

{"mode": "short", "limit": 5}
```

### POST /enter
Ручной вход в позицию.

```
POST /enter
Content-Type: application/json

{
  "symbol": "WLDUSDT",
  "side": "Sell",
  "qty": 50,
  "sl": 0.52,
  "tp": 0.35
}
```

### POST /close
Закрыть позицию.

```
POST /close
Content-Type: application/json

{"symbol": "SIRENUSDT"}
```

---

## 4. Конфиг-схема (план)

```yaml
# bybit-ws-config.yaml — внешний конфиг (MVP)
# Путь: ~/.config/bybit-ws/config.yaml

api:
  key: "${BYBIT_API_KEY}"
  secret: "${BYBIT_API_SECRET}"
  base_url: "https://api.bytick.com"

strategy:
  long:
    leverage: 3
    margin_tiers: {7: 15, 5.5: 10, 0: 5}   # score → $margin
    entry_offset: 0.03                        # -3% ниже Lower BB
    sl_offset: 0.07                           # -7% от Lower BB
    tp_middle_pct: 0.20                       # 20% на Middle
    tp_upper_pct: 0.80                        # 80% на Upper
    max_positions: 0                           # 0 = безлимитно

  short:
    leverage: 3
    margin: 10
    entry_offset: 0.02                        # +2% выше рынка
    sl_tier_ab: 0.05                          # +5% для Tier A/B
    sl_tier_cd: 0.07                          # +7% для шлака
    bb_threshold: 85                           # BB% > порога
    max_positions: 3
    cooldown_seconds: 7200

  dca:
    enabled: true
    levels: [0.95, 0.90, 0.85]               # уровни добавки (от входа)
    multiplier: 2                              # ×2 к марже на каждом уровне

watchlist:
  mode: "top"                                  # top | fixed
  top_n: 50                                    # топ-N по обороту
  exclude: ["BTCUSDT", "ETHUSDT"]              # исключённые пары
  # fixed: ["SOLUSDT", "ADAUSDT", ...]         # если mode=fixed

tiers:
  S: ["BTCUSDT", "ETHUSDT"]
  A: ["SOLUSDT", "LTCUSDT", "XRPUSDT", ...]
  one_way: ["XRPUSDT", "ONDOUSDT", ...]        # SHORT невозможен

monitor:
  cycle_seconds: 30
  heavy_cycle: 10                              # тяжёлые проверки каждые N циклов
  watchdog_seconds: 180

rpc:
  port: 8766
  bind: "127.0.0.1"                            # или 0.0.0.0 для внешнего доступа
  auth_token: "${RPC_TOKEN}"                   # Bearer-токен (пусто = без auth)
  rate_limit_per_min: 60                       # лимит запросов/IP/мин

risk:
  max_drawdown_pct: 15                          # стоп всего при -15% депозита
  max_total_margin: 500                         # не более $500 суммарно в позициях
  max_daily_loss: 50                            # стоп на день при -$50
  max_long_positions: 12                         # лимит LONG позиций
  emergency_close_all: true                      # закрыть всё при max_drawdown

logging:
  max_size_mb: 50
  max_files: 7
  format: "json"                                 # json | text

alerts:
  telegram_enabled: false
  correlation_threshold: 0.80                  # алерт при >80% LONG
  sl_alert: true
  tp_alert: true
```

---

## 5. Быстрый старт (для AI-агента)

### 1. Клонировать
```bash
git clone <repo> /opt/bybit-ws
cd /opt/bybit-ws
pip install -r requirements.txt
```

### 2. Настроить
```bash
cp config.example.yaml ~/.config/bybit-ws/config.yaml
# Прописать BYBIT_API_KEY и BYBIT_API_SECRET
```

### 3. Запустить
```bash
# Через systemd
cp bybit-ws.service ~/.config/systemd/user/
systemctl --user enable --now bybit-ws

# Или вручную
python3 -m bybit_ws.main
```

### 4. Проверить
```bash
curl http://localhost:8766/health
# {"status": "alive", ...}
```

### 5. Использовать из AI-агента
```python
import requests

# Получить позиции
r = requests.get('http://localhost:8766/positions')
positions = r.json()

# Войти в SHORT
requests.post('http://localhost:8766/enter', json={
    'symbol': 'WLDUSDT',
    'side': 'Sell',
    'qty': 50,
    'sl': 0.52,
    'tp': 0.35
})

# Закрыть
requests.post('http://localhost:8766/close', json={'symbol': 'WLDUSDT'})
```

---

## 6. Scoring (формула)

Это ядро стратегии — как оцениваются кандидаты на вход.

```
Score = Σ w_i × metric_i   (диапазон 0–10)

Метрики LONG:
  1. Tier-бонус          ×2.0    S=10, A=7, B=4, C/D=1
  2. BB% (положение)     ×1.5    0% = нижняя полоса, 100% = верхняя. Идеал <30%
  3. Объём (24ч)         ×1.0    нормализованный log(volume)
  4. Дней падения        ×1.0    до 7 дней подряд
  5. Недельный BB%       ×1.0    Weekly BB для тренда
  6. Месячный BB%        ×1.0    Monthly BB для контекста
  7. Фандинг             ×0.5    отрицательный фандинг = бонус
  8. RSI(14)             ×1.0    RSI < 30 = 10, RSI > 70 = 0
  9. BB Squeeze          ×1.0    узкие полосы перед расширением

Метрики SHORT:
  1. BB% > threshold (85%)   — перегрев верхней полосы
  2. RSI(14) > 70             — перекупленность
  3. Объём                   — подтверждение
  4. Tier                     — A/B предпочтительнее C/D
```

### Ограничения

- M5/M3 BB% > 100% → вход блокируется (LONG на перегреве — нет)
- correlation_stop (>80% LONG) → LONG-вход блокируется
- max_long_positions достигнут → пропуск
- paused = true → все авто-входы блокируются

---

## 7. Error Handling

### Формат ошибок API

Все ошибки возвращают унифицированный JSON:

```json
{
  "error": "Краткое описание",
  "detail": "Развёрнутое описание",
  "api_version": "v1",
  "status": 400
}
```

### HTTP-коды

| Код | Значение | Пример |
|-----|----------|--------|
| 200 | OK | Успешный GET/POST |
| 400 | Bad Request | Невалидный symbol/side/qty |
| 401 | Unauthorized | Неверный Bearer-токен |
| 402 | Payment Required | Недостаточно маржи |
| 404 | Not Found | Нет позиции для закрытия |
| 409 | Conflict | Позиция уже существует |
| 422 | Unprocessable | Неизвестный символ (код 110001) |
| 429 | Rate Limited | Превышен лимит запросов |
| 500 | Internal Error | Ошибка сканера |
| 504 | Timeout | Сканер не ответил за 120с |

### Retry-логика

API-запросы к Bybit: 3 попытки с exponential backoff [1, 3, 10] секунд.
GET-запросы (positions, orders): backoff [1, 3, 5] секунд.

### Поведение при сбоях

| Сбой | Поведение |
|------|-----------|
| Bybit API 429 | Retry с backoff до 3 попыток |
| Bybit API 503 | Retry, затем пропуск цикла |
| get_bb_data() завис | _timed_call 25с → пропуск |
| Главный цикл >90с | Тяжёлые проверки пропущены, лёгкие продолжаются |
| Watchdog >180с | Аварийный os._exit(1), systemd перезапустит |
| SIGTERM | Проверить SL на всех позициях → сохранить снепшоты → exit 0 |

### Edge Cases

- **Flash crash -20%:** BB расширяются, lower уходит глубоко вниз. DCA включается. SL защищает каждую позицию.
- **Памп +50%:** SHORT не входит на пике (entry_offset +2% даёт буфер). BB% зашкаливает — сигнал пропускается.
- **Фандинг раз в 8 часов:** при 3x может сжирать 0.3-1% на шорте в бычий рынок. Учитывается в PnL через cost_tracker.
- **Пустой API-ответ:** fetch_positions возвращает {} — цикл продолжается с последним снепшотом.
- **Множественные SL за цикл:** check_and_fix_sl обрабатывает все позиции без дублирования.

---

## 8. Deployment

### Systemd (рекомендуется)

```ini
# ~/.config/systemd/user/bybit-ws.service
[Unit]
Description=Bybit WS Bollinger Grid Monitor
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m bybit_ws.main
Restart=always
RestartSec=10
Environment=BYBIT_API_KEY=...
Environment=BYBIT_API_SECRET=...
Environment=RPC_TOKEN=your-secret-token

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now bybit-ws
```

### Docker

```bash
docker-compose up -d
# Порт 8766 проброшен на хост
```

### Переменные окружения

| Переменная | Назначение | По умолчанию |
|-----------|-----------|-------------|
| BYBIT_API_KEY | Ключ Bybit API | (обязательно) |
| BYBIT_API_SECRET | Секрет Bybit API | (обязательно) |
| RPC_TOKEN | Bearer-токен для API | (пусто = без auth) |

---

## 9. Changelog

- **v3.4** (08.06.2026): RPC auth (Bearer), rate limiting, GET /config, GET /signals (LONG+SHORT), risk-лимиты (max_drawdown, max_total_margin), graceful shutdown (SIGTERM → check SL), log rotation, error-формат v1, confirm:true для /enter, документация (scoring, edge cases, deployment)
- **v3.3:** YAML-конфиг, REST API 8 эндпоинтов, _timed_call, Docker, SDK, OpenAPI
- **v3.2:** auto_short исправлен (4 бага), SHORT лимитный вход +2%, SL +5%/+7% по Tier
- **v3.1:** cost_tracker (PnL + комиссии), SL re-entry лесенка
- **v3.0:** модульная архитектура, watchdog, авто-SL/TP/DCA

---

## 10. Известные ограничения

- Только Bybit USDT фьючерсы (linear perpetual)
- Один аккаунт на инстанс (мульти-аккаунт в v4.0)
- Нет бэктестинга (планируется v4.0)
- Нет spot-поддержки
- Нет веб-дашборда (планируется v4.0)

---

## 11. Файлы проекта

```
~/.local/
├── bin/
│   ├── bybit-ws              точка входа (CLI)
│   └── bybit-cli             низкоуровневый API-клиент
├── lib/
│   └── bybit_ws/
│       ├── main.py           главный цикл (481 строк)
│       ├── api.py            API-обёртка, fetch_positions/orders
│       ├── auto_entry.py     LONG авто-вход
│       ├── auto_short.py     SHORT авто-вход (218 строк)
│       ├── auto_tp.py        авто-TP
│       ├── auto_sl.py        авто-SL
│       ├── trailing_sl.py    трейлинг-стоп
│       ├── dca.py            DCA
│       ├── sl_reentry.py     перезаход после SL
│       ├── overbought.py     детектор перегрева
│       ├── pump_detect.py    детектор пампов
│       ├── rsi.py            RSI-дивергенции
│       ├── squeeze.py        BB-сжатие
│       ├── health.py         ликвидация/корреляция/фандинг
│       ├── rpc.py            HTTP-RPC
│       ├── cost_tracker.py   учёт комиссий
│       ├── cleanup.py        очистка ордеров
│       ├── recycle.py        перевыставление TP
│       ├── reporting.py      сводки
│       ├── metrics.py        счётчик алертов
│       └── alerts.py         логирование
└── share/
    └── bybit-ws/
        ├── events.log        основной лог
        ├── trades.jsonl      журнал сделок
        ├── health.txt        последний ping
        └── positions.json    снепшот
```

---

## 12. Pitfalls

1. **Watchdog (180с)** убивает процесс если главный цикл завис. Причина: API-запросы (get_bb_data) внутри цикла по символам. Решение: guard `heavy_ok` (пропускать тяжёлые проверки если цикл >90с) + `_timed_call` с таймаутом 25с.
2. **positionIdx** зависит от режима аккаунта: one-way mode → idx=0, hedge mode → idx=0/1. Проверять перед входом.
3. **get_bb_data()** возвращает ключи `upper`/`middle`/`lower` (lowercase), не `Upper Band`.
4. **BB lower** может быть отрицательным у новых/волатильных монет — это нормально.
5. **ONE_WAY монеты** — SHORT невозможен (XRP, ONDO, WLFI, ENJ, ESPORTS, AVAX, APT, SUI).
6. **correlation_stop** (>80% LONG) блокирует LONG-вход, но разрешает SHORT.
7. **RPC без auth** — если RPC_TOKEN не задан, API открыт для любого процесса на localhost.
8. **DCA multiplier 2x** — при 3 уровнях: $15 → $30 → $60 = $105 на монету. При 10 монетах = $1050 — весь депозит.
9. **Фандинг не в PnL** — авто-TP/закрытия не учитывают фандинг. Реальный PnL чуть ниже при долгом держании.
10. **events.log** ротируется при 50 МБ, но trades.jsonl — нет (следить вручную).
