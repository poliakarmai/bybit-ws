# ARCHITECTURE.md — bybit-ws v11.5

**Русский** · [English](ARCHITECTURE.en.md)

> **Назначение:** КАК устроен бот (внутренняя архитектура).
> Для функциональности: [CAPABILITIES.md](CAPABILITIES.md). Для API: [API.md](API.md).

> **Версия:** 11.5 | **Дата:** 2026-10-25 | **Python 3.11+** | **asyncio** | **SQLite SSOT**
> **Сверено с кодом и живой системой:** 2026-11-01. Проверять факты — командами из раздела
> «Параметры» и «Prometheus Metrics», а не по памяти: таблицы ниже уже один раз разошлись с реальностью.

## SSOT (Single Source of Truth)

**SQLite state.db — единственный источник истины.** Все JSON-файлы (`metrics.json`, `short_positions.json`) — read-only кэши/резервные копии (позиции отдаёт `async_positions_snapshot_ws()`, это функция, не файл). Данные пишутся ТОЛЬКО в SQLite. При старте JSON-кэши пересоздаются из SQLite. Dual-write не применяется.

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
 │   ├─ Black Swan check (`check_correlation_panic`: 5+ красных позиций И uPnL < -$150)
 │   ├─ Auto-SHORT + Dry Spell Throttle
 │   ├─ Корреляции + Пампы/Перекупленность
 │   ├─ DCA + Partial TP
 │   ├─ Auto-Entry (LONG) — 7 фильтров:
 │   │   ├─ MTF Confluence (D+W+M)
 │   │   ├─ Orderbook Imbalance
 │   │   ├─ Volume Confirmation
 │   │   ├─ Entry Judge (LLM DeepSeek, fail-closed)
 │   │   ├─ Correlation sizing (r>0.85→блок, r>0.70→×0.5)
 │   │   ├─ Post-trade cluster check (<40% WR→блок)
 │   │   └─ Risk Manager (CB, margin, max pos, banned)
 │   ├─ Auto-TP (ATR-based: 1.0×/2.0×/3.0× ATR)
 │   ├─ TP/SL Self-Check (прямой REST)
 │   ├─ Time-Based Exit (6h/48h)
 │   ├─ Post-trade cluster analysis (раз в сутки)
 │   └─ Journal insights (раз в сутки)
 ├─ Self-learning (event-driven: ≥10 сделок и ≥6ч с прошлого прогона, иначе fallback 24ч)
 │   ├─ journal/analyzer.py — FIFO + bias-диагностика
 │   ├─ journal/self_learn.py — адаптация min_score, SL/TP
 │   └─ post_trade.py — кластерный анализ, блок <40% WR
 ├─ pump_state авто-очистка (каждый цикл)
 ├─ Heartbeat (каждые 12ч)
 └─ Отчётность
```

## Параметры (prod)

| Параметр | Значение | Откуда |
|----------|----------|--------|
| Max позиций | 5 | `position_sizing.max_positions` (дефолты: LONG 12, JUNK 2 — `config.py:199`, `config.py:103`) |
| Max суммарная маржа | $300 | `risk.max_total_margin`, дефолт `risk_manager.py:935` |
| Max дневной убыток | $200 | `risk.max_daily_loss` |
| Max gross leverage | 5.0 | `risk.max_gross_leverage` |
| Cycle time | 30s | `bybit_ws/main_async.py` |
| Heavy cycle | 300s (10 циклов) | `bybit_ws/main_async.py` |
| Healthcheck | `GET /health` — timestamp последнего цикла | `rpc.py` |
| RAM | плавает; не хранить числом | `systemctl --user show bybit-ws-async -p MemoryCurrent` |

Проверка значений (не по памяти):

```bash
python3 - <<'EOF'
import yaml, os
c = yaml.safe_load(open(os.path.expanduser('~/.config/bybit-ws/config.yaml')))
print('risk:', c.get('risk'))
print('position_sizing:', c.get('position_sizing'))
EOF
```

Сессионные поправки (`session_params.py`) меняют `max_positions` и множители SL/TP для
NY open / Asia night / Weekend — их значения живут в коде, а не в этой таблице.

## Где живёт код

Модули — в пакете `bybit_ws/`. Часть корневых файлов это симлинки на те же исходники
(`bybit_ws/rpc.py -> ../rpc.py`), часть существует только в пакете (`bybit_ws/post_trade.py`).
Искать модуль надо в `bybit_ws/`, а не в корне: `ls -l bybit_ws/*.py | grep '^l'` показывает,
что дублируется, а что нет. Полный список — `ls bybit_ws/*.py`.

```
bybit_ws/
├── main_async.py         ← главный цикл (async, 30с) + тяжёлый цикл каждые 10
├── api.py / exchange_adapter.py  ← Bybit v5 REST + HMAC + retry
├── ws_client.py          ← WebSocket: kline, тикеры, BB-кеш
├── rpc.py                ← HTTP-RPC :8766, /metrics, one-click (Bearer)
├── state_db.py           ← SQLite SSOT (WAL, busy_timeout 5с), миграции _migrate()
├── auto_entry.py         ← LONG: MTF, ордербук, объём, судья, корреляции
├── auto_short.py         ← SHORT + JUNK + DCA
├── unified_sl.py         ← единый SL (tight>simple>hard>BE>auto>default)
├── auto_tp.py / partial_tp.py / trailing_sl.py / time_exit.py
├── risk_manager.py       ← лимиты, Risk CB, BlackSwan (alert-only), emergency_close
├── entry_judge.py        ← LLM-судья входа (fail-closed) + счётчики для /metrics
├── lstm_regime.py / lstm_world_model.py / ml_scorer.py / dspy_optimizer.py
├── session_params.py     ← NY / Asia / Weekend поправки
├── watchdog.py           ← сторож входов (окно 6ч, порог 20 блокировок)
├── journal/              ← самообучение: adapter → analyzer → self_learn
└── experiments/          ← офлайн-стенд: engine, validation, triple_barrier, bandit, meta_label
```

В корне репозитория: `deploy.sh`, `verify.sh`, `backup_db.sh`, `test_smoke.py`, `reconcile.py`,
`paper_trade.py`, `tests/`, `docs/`.

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

1. Одна модель: прямой `api.deepseek.com` (`deepseek-chat`, env `DEEPSEEK_JUDGE_MODEL`) через `~/.hermes/scripts/cross-model-judge.py`, prompt-mode `signal`
2. Ответ = `content or reasoning_content` (reasoning-модели отдают пустой `content`)
3. Упало/таймаут → revise (блок, fail-closed)
4. 3 падения подряд → отключение на 1 час (Judge CB)
5. Кэш: 300s TTL, ключ включает `sl_price:tp_price`
6. Таймаут: 20s (`JUDGE_TIMEOUT`; 5s резало живые вызовы — Qwen/Nemotron убраны 17.10)

## Self-Learning (event-driven: ≥10 сделок и ≥6ч с прошлого прогона, иначе fallback 24ч)

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
| BYBIT_WS_BB_ENABLED | 1 (дефолт) | WS для kline/BB-кеша |
| BYBIT_WS_FULL_ENABLED | 0 (дефолт) | Real-time позиции через WS |
| BYBIT_ML_ENABLED | **0 (прод env)** | RF ML Gate |
| BYBIT_DSPY_ENABLED | **1 (прод env)** | DSPy-гейт |
| BYBIT_OPTUNA_ENABLED | 0 (прод env) | Optuna-параметры |
| BYBIT_REGIME_AUTO | **1 (прод env)** | LSTM → авто LONG/SHORT + `BYBIT_REGIME_MIN_CONFIDENCE` (60) |
| BYBIT_WORLD_MODEL | **1 (прод env)** | World-model score в SHORT-скоринге |
| BYBIT_AB_ENABLED | **1 (прод env)** | A/B-тестирование |
| BYBIT_ENTRY_JUDGE_ENABLED | **1 (прод env)** | LLM-судья входа |
| BYBIT_ATR_TP_ENABLED | 1 (дефолт) | ATR-based TP |

> Значения «прод env» считаны из `~/.config/bybit-ws/env` 25.10 — раньше таблица показывала дефолты
> и расходилась с реальностью (`DSPY/AB/REGIME_AUTO` фактически включены, `ML` выключен).

## Circuit Breakers (два независимых)

**Risk CB** — по дневному PnL: срабатывает при >80% от max_daily_loss.
**Judge CB** — по ошибкам LLM: 3 падения подряд → отключение Entry Judge на 1 час.

## Graceful Shutdown

При SIGTERM: `log_event` → выход. Без блокирующих SL-проверок.

## Backup & Recovery

```
# Автоматически: systemd-таймер bybit-backup.timer, OnCalendar=daily (00:00)
#   → /bin/bash ~/bybit-ws/backup_db.sh
# Проверить, когда был последний: systemctl --user list-timers bybit-backup.timer
# Ручной снимок перед деплоем: cp state.db state.db.bak
# При старте: PRAGMA integrity_check
# WAL recovery: автоматический (SQLite)
```

## Health Check и сторож входов

- `GET /health` возвращает timestamp последнего успешного цикла; `verify.sh` (проверка №3)
  берёт `cycle_count` дважды и требует роста — это ловит зависший цикл.
- Внешнего крона «каждые 90 секунд» нет. Сторож — `bybit_ws/watchdog.py`, запускается
  Hermes-кроном ежечасно: окно 6ч, входов 0 **и** ≥20 блокировок → алерт.
  Строки «… пропускаю» — рабочая фильтрация кандидатов, не блокировка.

```bash
python3 -m bybit_ws.watchdog --hours 6 --dry-run --json
```

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

`/metrics` требует Bearer-токен (как `/positions`). Токен: `SELECT value FROM kv_store WHERE key='rpc_auth_token'`.

| Серия | Тип | Что показывает |
|-------|-----|----------------|
| `bybit_ws_active_positions{side}` | gauge | открытых позиций по сторонам |
| `bybit_ws_unrealized_pnl` | gauge | нереализованный PnL |
| `bybit_ws_uptime_seconds` | gauge | аптайм процесса |
| `bybit_ws_cycle_duration_seconds` | gauge | длительность последнего цикла |
| `bybit_ws_cycle_count` | counter | номер цикла (растёт — процесс жив) |
| `bybit_ws_daily_pnl` | gauge | реализованный PnL за сегодня |
| `bybit_ws_judge_calls` | counter | вызовов судьи (после всех skip-ов) |
| `bybit_ws_judge_verdicts_total{verdict}` | counter | вердикты `pass` / `revise` |
| `bybit_ws_judge_errors_total` | counter | скрипт судьи упал или вернул мусор |
| `bybit_ws_judge_timeouts_total` | counter | таймауты судьи |
| `bybit_ws_judge_breaker_hits_total` | counter | срабатываний открытого брейкера |
| `bybit_ws_judge_fallback_open_total` | counter | входов в деградированном режиме (fail-open) |
| `bybit_ws_judge_cache_hits_total` | counter | попаданий в кэш вердиктов |
| `bybit_ws_judge_last_latency_ms` | gauge | задержка последнего вызова |

> Серии судьи считаются в памяти процесса «с запуска» и обнуляются рестартом
> (история — `events.log`). В коде с 01.11.2026, появляются после рестарта сервиса.

```bash
TOKEN=$(sqlite3 ~/.local/share/bybit-ws/state.db "SELECT value FROM kv_store WHERE key='rpc_auth_token'")
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/metrics | grep '^bybit_ws'
```

## Alerting Rules

| Уровень | Условие | Канал |
|---------|---------|-------|
| CRITICAL | Black swan (5+ красных и uPnL < -$150) | ntfy + Telegram |
| CRITICAL | Risk CB triggered | ntfy + Telegram |
| WARNING | Judge down >30min | Telegram |
| WARNING | Bybit API errors >5/min | Telegram |
| INFO | New position | Telegram |
| INFO | Position closed (PnL) | Telegram |
| INFO | Self-learning applied | events.log |

## Testing Strategy

| Тип | Описание | Покрытие |
|-----|---------|---------|
| Unit/integration | `python3 -m pytest -q` | весь набор зелёный (`--timeout=90`); число — из вывода прогона |
| Smoke | deploy validation (`test_smoke.py`) | 52/52 |
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
| RPO | <24 часа (бэкап ежедневно 00:00, `bybit-backup.timer`) |
| Бэкап | systemd-таймер ежедневно + ручной снимок перед деплоем |
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
TOKEN=$(sqlite3 ~/.local/share/bybit-ws/state.db "SELECT value FROM kv_store WHERE key='rpc_auth_token'")
curl -s http://127.0.0.1:8766/rpc/circuit_breaker -H "Authorization: Bearer $TOKEN"
systemctl --user restart bybit-ws-async   # сброс CB — только рестартом
```

**High memory:**
```bash
ps aux | grep bybit-ws
systemctl --user restart bybit-ws-async
```
