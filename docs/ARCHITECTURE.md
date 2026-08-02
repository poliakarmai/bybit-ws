# ARCHITECTURE.md — bybit-ws v7.1

> **Назначение:** КАК устроен бот (внутренняя архитектура).
> Для функциональности: [CAPABILITIES.md](CAPABILITIES.md). Для API: [API.md](API.md).

> **Версия:** 7.1 | **Дата:** 2026-06-28 | **Python 3.11+** | **asyncio** | **SQLite SSOT**

## SSOT (Single Source of Truth)

**SQLite state.db — единственный источник истины.** Все JSON-файлы (`metrics.json`, `positions_snapshot.json`, `short_positions.json`) — read-only кэши/резервные копии. Данные пишутся ТОЛЬКО в SQLite. При старте JSON-кэши пересоздаются из SQLite. Dual-write не применяется.

## Главный цикл (async, 30с)

```
ЦИКЛ (30s)
 ├─ Снапшот позиций + ордеров (REST, 20s таймаут)
 ├─ Лёгкие проверки (каждый цикл):
 │   ├─ SL check + fix (ATR-adaptive, capped -50%/+50%)
 │   ├─ Trailing SL
 │   ├─ Breakeven SL (каждые 4 цикла)
 │   └─ Margin check + Circuit Breaker
 ├─ Тяжёлый цикл (каждые 10 циклов = 5 мин):
 │   ├─ BB pre-fetch (batch, кеш 5 мин)
 │   ├─ Режим рынка (LSTM → NEUTRAL fallback)
 │   ├─ Black Swan check (PnL 2× limit ИЛИ BTC -8%/час)
 │   ├─ Auto-SHORT + Dry Spell Throttle
 │   ├─ Корреляции + Пампы/Перекупленность
 │   ├─ DCA + Partial TP
 │   ├─ Auto-Entry (LONG) — 7 фильтров:
 │   │   ├─ MTF Confluence (D+W+M)
 │   │   ├─ Orderbook Imbalance
 │   │   ├─ Volume Confirmation
 │   │   ├─ Entry Judge (LLM: Nemotron → DeepSeek, fail-closed)
 │   │   ├─ Correlation sizing (r>0.85→блок, r>0.70→×0.5)
 │   │   ├─ Post-trade cluster check (<40% WR→блок)
 │   │   └─ Risk Manager (CB, margin, max pos, banned)
 │   ├─ Auto-TP (ATR-based: 1.0×/2.0×/3.0× ATR)
 │   ├─ TP/SL Self-Check (прямой REST)
 │   ├─ Time-Based Exit (6h/48h)
 │   ├─ Post-trade cluster analysis (раз в сутки)
 │   └─ Journal insights (раз в сутки)
 ├─ Self-learning (каждые 2880 циклов = ~24ч)
 │   ├─ journal/analyzer.py — FIFO + bias-диагностика
 │   ├─ journal/self_learn.py — адаптация min_score, SL/TP
 │   └─ post_trade.py — кластерный анализ, блок <40% WR
 ├─ pump_state авто-очистка (каждый цикл)
 ├─ Heartbeat (каждые 12ч)
 └─ Отчётность
```

## Параметры (prod)

| Параметр | v7.1 |
|----------|------|
| Max позиций | 12 (session: NY=5, Asia=10, Weekend=3) |
| Max маржа | $300 |
| Max дневной убыток | -$50 |
| RAM (среднее) | ~70 MB |
| RAM (peak) | ~104 MB |
| Cycle time | 30s |
| Heavy cycle | 300s (10 циклов) |
| Healthcheck | `GET /health` — timestamp последнего цикла |

## Структура проекта

```
bybit-ws/
├── main_async.py         ← Главный цикл (asyncio)
├── api.py                ← Bybit v5 REST API + HMAC
├── ws_client.py          ← WebSocket (kline, BB-кеш)
├── rpc.py                ← REST API (:8766) + /metrics
├── state_db.py           ← SQLite SSOT (WAL)
├── auto_entry.py         ← Авто-вход LONG + 7 фильтров
├── auto_short.py         ← Авто-SHORT + JUNK + DCA
├── auto_sl.py            ← ATR-adaptive SL, capped ±50%
├── auto_tp.py            ← ATR-based TP: 3 уровня
├── trailing_sl.py        ← Трейлинг-SL
├── orderbook_filter.py   ← Orderbook imbalance
├── volume_filter.py      ← Volume confirmation
├── time_exit.py          ← Time-based exit (6h/48h)
├── entry_judge.py        ← Cross-model LLM judge
├── correlation.py        ← Корреляции
├── position_sizing.py    ← Kelly sizing
├── risk_manager.py       ← Risk + black swan + CB
├── session_params.py     ← NY/Asia/Weekend адаптация
├── bb_prefetch.py        ← BB batch-префетчер
├── post_trade.py         ← Кластерный анализ win rate
├── push_notifier.py      ← Push (ntfy + Telegram)
├── journal/
│   ├── analyzer.py       ← FIFO-анализ, bias
│   └── self_learn.py     ← Self-learning (2880 циклов)
├── docs/                 ← Документация
├── scripts/              ← DSPy, walkforward
├── deploy.sh             ← Атомарный деплой
└── test_smoke.py         ← 45/45 тестов
```

## Auto-SL — ATR-adaptive

| Режим | ATR/Price | k (множитель) |
|-------|-----------|---------------|
| high_vol | >5% | 2.5 |
| trending | 3-5% | 2.0 |
| normal | 1-3% | 1.5 |
| low_vol | <1% | 1.3 |

SL = entry ± k × ATR(14), capped at -50%/+50% от входа.

## Auto-TP — ATR-based 3 уровня

| Уровень | k | % объёма |
|---------|---|----------|
| TP1 | 1.0 | 40% |
| TP2 | 2.0 | 35% |
| TP3 | 3.0 | 25% |

PERM_SKIP с time-decay 24ч. Флаг: `BYBIT_ATR_TP_ENABLED=1`.

## JUNK-режим (шлак-шорты)

Для символов с пампами >80%:
- **SL:** -15% жёсткий стоп (не «без SL»)
- **Max позиция:** не более $5 маржи
- **Max одновременно:** не более 2 JUNK-позиций
- **Circuit breaker:** 2 JUNK-закрытия по -15% → режим отключается на 24ч
- **pump_state:** авто-очистка при закрытии позиции

## ML/LLM-компоненты

| Компонент | Файл | Назначение | Fail mode |
|-----------|------|-----------|-----------|
| Entry Judge | entry_judge.py | LLM-вердикт pass/revise | fail-closed |
| ML Gate (RF) | ml_scorer.py | Random Forest фильтр | fail-open |
| DSPy Gate | dspy_optimizer.py | Оптимизация промптов | fail-open |
| LSTM Regime | lstm_regime.py | Режим рынка | fallback NEUTRAL |
| Optuna | optuna_tuner.py | Гиперпараметры | использование defaults |

### Entry Judge — hard gate

1. Nemotron (OpenRouter) → verdict pass/revise
2. DeepSeek fallback
3. Оба упали → revise (блок)
4. 3 падения подряд → отключение на 1 час (Judge CB)
5. Кэш: 300s TTL
6. Таймаут: 5s

## Self-Learning (2880 циклов = ~24ч)

- journal/analyzer.py — FIFO-матчинг, win rate, P/L ratio, 4 bias-диагностики
- journal/self_learn.py — адаптация min_score (+30% при WR<40%), SL (+20%), bias-флаги
- post_trade.py — кластерный анализ (symbol×режим×сессия), блок <40% WR
- Лог: self_learn.jsonl — audit trail всех изменений

## Сессионная адаптация

| Сессия | BB period | SL mult | TP mult | Max pos | Entry bonus |
|--------|-----------|---------|---------|---------|-------------|
| NY open | +5 | 0.7 | 1.2 | 5 | +10 |
| Asia | -5 | 1.3 | 1.0 | 10 | -5 |
| Weekend | +10 | 0.8 | 0.8 | 3 | +15 |
| Normal | 0 | 1.0 | 1.0 | 8 | 0 |

## Feature Flags

| Флаг | Prod | Что делает |
|------|------|-----------|
| BYBIT_WS_BB_ENABLED | 1 | WS для kline/BB-кеша |
| BYBIT_WS_FULL_ENABLED | 0 | Real-time позиции через WS |
| BYBIT_ML_ENABLED | 1 | RF ML Gate |
| BYBIT_DSPY_ENABLED | 0 | DSPy-гейт (нужен LLM) |
| BYBIT_OPTUNA_ENABLED | 0 | Optuna-параметры |
| BYBIT_REGIME_AUTO | 0 | LSTM → авто LONG/SHORT |
| BYBIT_AB_ENABLED | 0 | A/B-тестирование |
| BYBIT_ATR_TP_ENABLED | 1 | ATR-based TP |

## Circuit Breakers (два независимых)

**Risk CB** — по дневному PnL: срабатывает при >80% от max_daily_loss.
**Judge CB** — по ошибкам LLM: 3 падения подряд → отключение Entry Judge на 1 час.

## Graceful Shutdown

При SIGTERM: `log_event` → выход. Без блокирующих SL-проверок.

## Backup & Recovery

```
# Автоматически: cron каждые 6 часов
sqlite3 ~/.local/share/bybit-ws/state.db ".backup '/backup/state_$(date +%Y%m%d_%H%M).db'"

# При старте: PRAGMA integrity_check
# WAL recovery: автоматический (SQLite)
# Retention: 7 дней
# Ручной: cp state.db state.db.bak перед деплоем
```

## Health Check (Watchdog)

`GET /health` — возвращает timestamp последнего успешного цикла.
Внешний cron-watchdog опрашивает каждые 90 секунд.
Если /health не отвечает >2 минут — алерт.

## Data Flow

```
Bybit API ←→ api.py (REST, HMAC)
              ↕
Bybit WS  ←→ ws_client.py (kline, tickers, orderbook)
              ↕
main_async.py ←→ state_db.py (SQLite WAL)
   ↓                  ↓
rpc.py (:8766)    journal/
   ↓                  ↓
MCP/Hermes        self_learn.jsonl
Grafana           post_trade_features.jsonl
Android App
```

## Связь дашборда с RPC

```
web/proxy_server.py (:8765)
    → HTTP GET /rpc/positions
        → rpc.py (:8766)
            → SQLite query
                → state.db
```

## Деплой

```bash
1. cp state.db state.db.bak           # backup
2. git pull                            # new code
3. bash deploy.sh --force              # atomic symlink swap + tests
4. systemctl --user restart bybit-ws-async
5. curl http://127.0.0.1:8766/health  # verify
```

## Retry Policy (Bybit API)

| Ошибка | Стратегия |
|--------|----------|
| 5xx | Retry 3× (1s, 2s, 4s backoff) |
| 429 (rate limit) | Wait 60s + retry |
| Network error | Retry 5× (1s, 2s, 4s, 8s, 16s) |
| Bybit API CB | 5 ошибок подряд → пауза 5 мин; 10 ошибок/час → пауза 30 мин |

## SQLite Connection

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
```

Все модули используют общий connection pool (`check_same_thread=False`).

## Logging

Формат: `[2026-06-28 12:00:00] LEVEL module: message`.

| Уровень | Назначение |
|---------|-----------|
| DEBUG | Детали циклов |
| INFO | Входы, выходы, TP/SL |
| WARNING | Пропуски, CB |
| ERROR | Ошибки API, таймауты |
| CRITICAL | Black swan, kill switch |

## Prometheus Metrics

| Метрика | Тип | Описание |
|---------|-----|---------|
| `bybit_ws_cycle_duration_seconds` | histogram | Длительность цикла |
| `bybit_ws_positions_total` | gauge | Открытых позиций |
| `bybit_ws_pnl_total` | gauge | Нереализованный PnL |
| `bybit_ws_trades_total` | counter | Закрытых сделок |
| `bybit_ws_errors_total` | counter | Ошибок (labels: module, error_type) |
| `bybit_ws_api_latency_seconds` | histogram | Latency Bybit API |
| `bybit_ws_circuit_breaker_state` | gauge | Статус CB |
| `bybit_ws_entry_judge_latency_seconds` | histogram | Latency Judge |

## Alerting Rules

| Уровень | Условие | Канал |
|---------|---------|-------|
| CRITICAL | Black swan (BTC -8%/1h) | ntfy + Telegram |
| CRITICAL | Risk CB triggered | ntfy + Telegram |
| WARNING | Judge down >30min | Telegram |
| WARNING | Bybit API errors >5/min | Telegram |
| INFO | New position | Telegram |
| INFO | Position closed (PnL) | Telegram |
| INFO | Self-learning applied | events.log |

## Testing Strategy

| Тип | Описание | Покрытие |
|-----|---------|---------|
| Unit | mocks для API, SQLite in-memory | target >80% |
| Integration | реальный SQLite, fake Bybit API | 45 smoke-тестов |
| Smoke | deploy validation (deploy.sh) | 45/45 |
| CI | GitHub Actions, GSC security scan | на каждом push |

## Security

| Уровень | Механизм |
|---------|---------|
| state.db | chmod 600, владелец openclaw |
| API keys | .env (chmod 600), не в логах |
| RPC token | UUID v4, ротация через /reset-token |
| Emergency token | отдельный в .env (EMERGENCY_TOKEN) |
| Firewall | только :8766 (RPC), :8888 (Grafana) |
| HTTPS | nginx + Let's Encrypt (продакшен) |

## Disaster Recovery

| Параметр | Значение |
|----------|---------|
| RTO | <15 минут |
| RPO | <1 час (бэкап каждый час) |
| Бэкап | cron ежечасно + event-driven |
| Recovery | restore state.db + systemctl restart |
| DR drill | ежеквартально |

## Troubleshooting

**Бот не стартует:**
```bash
systemctl --user status bybit-ws-async
journalctl --user -u bybit-ws-async -f
# SQLite lock: rm state.db-wal state.db-shm (safe)
```

**Позиции не закрываются:**
```bash
curl http://127.0.0.1:8766/positions
# Проверить Bybit напрямую — позиция всё ещё открыта?
```

**Judge не работает:**
```bash
curl http://127.0.0.1:8766/circuit_breaker
# Сбросить если нужно: POST /circuit_breaker {"action":"reset"}
```

**High memory:**
```bash
ps aux | grep bybit-ws
systemctl --user restart bybit-ws-async
```
