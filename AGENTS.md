# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Карта проекта, команды, правила.  
> Обновлено: 2026-06-29 (v7.3 — ATR-based TP, POSITION_IDX auto-detect, dead code audit, deploy simplification, GSC clean)

## Что это

Трейдинг-монитор для Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT по BB-полосам).  
Systemd-сервис `bybit-ws-async`, ~35 MB RAM, SQLite — SSOT.

## Главный цикл (async, 30с)

```
ЦИКЛ (30с)
  ├─ Снапшот позиций (REST, 20с таймаут)
  ├─ Black Swan check (3-tier: -3%/15min→50%, -5%/30min→80%, -8%/1h→100%)
  ├─ Лёгкие проверки (каждый цикл):
  │   ├─ SL check + fix (ATR-adaptive, capped -50%/+50%)
  │   ├─ Trailing SL
  │   ├─ Breakeven SL (каждые 4 цикла)
  │   └─ Margin utilization
  ├─ Circuit breaker check
  ├─ Тяжёлый цикл (каждые 10 циклов = 5 мин):
  │   ├─ BB pre-fetch (batch-загрузка, кеш 5мин)
  │   ├─ Режим рынка (LSTM, fallback NEUTRAL)
  │   ├─ Auto-SHORT + Dry Spell Throttle
  │   ├─ Корреляции + Пампы/Перекупленность
  │   ├─ DCA + Partial TP
  │   ├─ Auto-Entry (LONG):
  │   │   ├─ MTF Confluence (D+W+M)
  │   │   ├─ Orderbook Imbalance (bid/ask ratio)
  │   │   ├─ Volume Confirmation (vol vs SMA)
  │   │   ├─ Entry Judge (Nemotron → DeepSeek, 5с, hard gate)
  │   │   ├─ Correlation sizing (r>0.85→блок, r>0.70→×0.5)
  │   │   └─ Risk Manager (CB, margin, max pos, banned clusters)
  │   ├─ Auto-TP (ATR-adaptive: 1.0×/2.0×/3.0× ATR)
  │   ├─ TP/SL Self-Check (прямой REST-запрос)
  │   ├─ Time-Based Exit (6ч без PnL / 48ч абсолют)
  │   ├─ Post-trade cluster analysis (раз в сутки)
  │   └─ Journal insights (раз в сутки)
  ├─ SL re-entry (с режимным фильтром)
  ├─ Heartbeat (каждые 12ч)
  └─ Отчётность
```

## Структура

```
bybit-ws/
├── main_async.py         ← Главный цикл (asyncio, 30с)
├── api.py                ← Bybit v5 REST API + fetch_open_orders
├── ws_client.py          ← WebSocket (публичные потоки: kline, BB-кеш)
├── rpc.py                ← JSON-RPC (:8766) + /emergency_close + /kill_switch + /metrics
├── state_db.py           ← SQLite SSOT (WAL, 5с busy_timeout)
├── auto_entry.py         ← Авто-вход + Orderbook + Volume + Entry Judge + Correlation
├── auto_short.py         ← Авто-SHORT + Orderbook + Volume + Entry Judge
├── auto_sl.py            ← ATR-adaptive SL v2 (capped -50%/+50%)
├── auto_tp.py            ← ATR-based TP: 1.0×/2.0×/3.0× ATR
├── trailing_sl.py        ← Трейлинг-SL
├── orderbook_filter.py   ← Orderbook imbalance filter
├── volume_filter.py      ← Volume confirmation filter (28.06)
├── time_exit.py          ← Time exit (6ч/48ч)
├── entry_judge.py        ← Cross-model judge (fail-closed + кэш + circuit breaker)
├── correlation.py        ← Корреляции + max_corr_with_open
├── position_sizing.py    ← Динамическая маржа
├── risk_manager.py       ← Risk + BlackSwan multi-tier (3 уровня) + emergency_close
├── session_params.py     ← NY/Asia/Weekend адаптивные параметры (28.06)
├── bb_prefetch.py        ← BB batch-префетчер (28.06)
├── post_trade.py         ← Кластерный анализ win rate (28.06)
├── paper_trading.py 🆕   ← Paper Trading (PaperExchange + RPC, v7.2)
├── structured_log.py 🆕  ← JSON-логи в events.jsonl (v7.2)
├── push_notifier.py      ← Push (ntfy + Telegram)
├── journal/              ← Журнал + самообучение
│   ├── analyzer.py       ← Анализатор сделок (FIFO, bias)
│   └── self_learn.py     ← Self-learning + Canary mode v3
├── .github/workflows/    ← CI/CD (28.06)
│   └── test.yml          ← Smoke-тесты + GSC на push/PR
├── scripts/
│   └── walkforward_rf.py ← Walk-forward RF валидация
├── deploy.sh             ← Атомарный деплой (SIGTERM→SIGKILL)
└── test_smoke.py         ← Интеграционные тесты (45/45)
```

## Как запускать

```bash
# Сервис
systemctl --user start bybit-ws-async
systemctl --user status bybit-ws-async

# Деплой
bash deploy.sh

# Тесты
python3 test_smoke.py          # 45 интеграционных
python3 test_modules.py        # 5 модульных
python3 test_ml_smoke.py       # 3 ML
```

## Где что лежит

| Данные | Путь |
|--------|------|
| Позиции (SSOT) | `~/.local/share/bybit-ws/state.db` |
| Метрики | `~/.local/share/bybit-ws/metrics.json` |
| Логи | `~/.local/share/bybit-ws/events.log` |
| Конфиг | `~/.config/bybit-ws/config.yaml` |
| Креды | `~/.config/bybit-ws/env` (chmod 600) |
| RPC | `http://127.0.0.1:8766` |
| Prometheus | `http://127.0.0.1:8766/metrics` (Bearer auth) |
| Self-learning лог | `~/.local/share/bybit-ws/self_learn.jsonl` |
| Post-trade лог | `~/.local/share/bybit-ws/post_trade_features.jsonl` |
| Blocked clusters | `~/.local/share/bybit-ws/blocked_clusters.json` |
| Дорожная карта | `obsidian-vault/hermes/bybit-ws-roadmap.md` |

## MCP-инструменты

| Инструмент | Назначение |
|-----------|-----------|
| `scan_market(mode, interval)` | Скан Bollinger Grid сигналов |
| `get_positions()` | Текущие позиции + PnL |
| `get_metrics()` | Дневные метрики (TP/SL/входы) |
| `get_risk_status()` | Лимиты риска + circuit breaker |
| `place_entry(symbol, side, qty)` | Вход в позицию |
| `get_journal()` | Анализ торгового журнала (FIFO, bias) |

**Воркфлоу:** `scan_market` → `get_risk_status` → `get_positions` → `place_entry`

## Цепочка входа

```
BB-сигнал
  → MTF confluence (D+W+M)
  → Orderbook Imbalance (bid/ask >0.55 LONG, <0.45 SHORT)
  → Volume Confirmation (vol/SMA не в шумовой зоне)
  → Entry Judge (Nemotron → DeepSeek, 5с, fail-closed + кэш 300с)
  → Correlation sizing (r>0.85→блок, r>0.70→×0.5)
  → Post-trade cluster check (блок если кластер <40% WR)
  → Risk Manager (CB, margin, max pos, banned)
  → Ордер
```

## Аварийные ситуации

```bash
# Закрыть ВСЕ позиции (kill switch)
curl -X POST http://127.0.0.1:8766/kill_switch \
  -H "Authorization: Bearer TOKEN"

# Только emergency close (без паузы)
curl -X POST http://127.0.0.1:8766/emergency_close \
  -H "Authorization: Bearer TOKEN"

# Сбросить LLM Circuit Breaker
systemctl --user restart bybit-ws-async

# Проверить heartbeat (каждые 12ч)
grep "Heartbeat" ~/.local/share/bybit-ws/events.log | tail -1

# Prometheus метрики
curl -H "Authorization: Bearer TOKEN" http://127.0.0.1:8766/metrics
```

## Фильтры входа

| Фильтр | Условие | Тип |
|--------|---------|-----|
| MTF Confluence | D+W+M score ≥2 | fail-open |
| Orderbook | bid/(bid+ask) >0.55 / <0.45 | fail-open |
| Volume | vol/SMA(20) не 0.7-1.3 | fail-open |
| Entry Judge | LLM: Nemotron→DeepSeek | fail-closed |
| Correlation | r<0.85 с открытыми | fail-open |
| Post-trade | кластер WR<40% | fail-open |
| Risk Manager | CB, margin, max pos | fail-closed |

## Entry Judge — hard gate + circuit breaker

1. **Nemotron** (OpenRouter) → verdict pass/revise
2. **DeepSeek** fallback
3. Оба упали → **revise** (блок)
4. **3 падения подряд** → отключение на 1 час
5. **Кэш вердиктов**: 300с TTL
6. **Таймаут**: 5 секунд

## Auto-SL v2 — ATR-adaptive + 2% Floor (29.06)

SL = entry ± k × ATR(14), capped at -50%/+50%:

| Режим | ATR/Price | k |
|-------|-----------|---|
| high_vol | >5% | 2.5 |
| trending | 3-5% | 2.0 |
| normal | 1-3% | 1.5 |
| low_vol | <1% | 1.3 |

**SL 2% Floor:** SL не ближе 2% от входа — защита от выбивания шумом.
**Аварийный SL:** при ATR/BB SL выше рынка → mark × 0.95.

## Auto-TP v3 — ATR-based levels (29.06)

`BYBIT_ATR_TP_ENABLED=1` — `_get_atr_value(sym)` → TP = entry ± k × ATR(14):

| Уровень | k | % объёма |
|---------|---|---------|
| TP1 | 1.0 | 40% |
| TP2 | 2.0 | 35% |
| TP3 | 3.0 | 25% |

PERM_SKIP с time-decay 24ч.

## Time-Based Exit

| Триггер | Действие |
|---------|---------|
| >6ч, PnL < 0%, нет частичного TP | MARKET close |
| >48ч абсолют | MARKET close всегда |

## Black Swan — Multi-Tier Emergency Close (v7.1)

| Tier | Триггер | Действие |
|------|---------|---------|
| 1 ⚠️ | BTC -3% за 15 мин | Закрыть 50% позиций (худшие по PnL) |
| 2 🔥 | BTC -5% за 30 мин | Закрыть 80% позиций |
| 3 🚨 | BTC -8% за час ИЛИ PnL >2× daily_loss | Закрыть 100% (kill switch) |

Сортировка по unrealized PnL: самые убыточные закрываются первыми.

## Session Params (28.06)

| Сессия | BB period | SL mult | TP mult | Max pos | Entry bonus |
|--------|-----------|---------|---------|---------|-------------|
| NY open | +5 | 0.7 | 1.2 | 5 | +10 |
| Asia | -5 | 1.3 | 1.0 | 10 | -5 |
| Weekend | +10 | 0.8 | 0.8 | 3 | +15 |
| Normal | 0 | 1.0 | 1.0 | 8 | 0 |

## Risk Manager

| Параметр | Значение |
|----------|---------|
| Max позиций | 12 (5 high vol, session-dependent) |
| Max дневной убыток | -$50 |
| Max маржа | $300 |
| Circuit breaker | 80% от max_daily_loss |
| Black swan close | 2× daily loss или BTC -8%/час (Tier 3) |

## Canary Mode (v7.1)

Self-learning с защитой от переобучения:
- 10% входов используют новые параметры (canary group)
- 48ч окно оценки: сравнение WR canary vs baseline
- Падение WR >10% → авто-rollback
- WR canary ≥ baseline → promote на все входы
- Состояние: `canary_state.json` в `~/.local/share/bybit-ws/`

## Paper Trading (v7.2)

`BYBIT_PAPER_ENABLED=1` — симуляция всех сделок без риска:
- PaperExchange: отдельная БД `paper_state.db`
- Mark-цены из WS-кеша, PnL в реальном времени
- RPC: `/paper/balance`, `/paper/positions`, `/paper/summary`

## Structured Logging (v7.2)

`STRUCTURED_LOGGING=1` — JSON-логи в `events.jsonl`:
- `log_info/warn/error/critical` + `log_cycle`
- Ротация 50 MB, совместимость с Grafana Loki

## GSC Security Audit (v7.2)

| Уровень | Находок |
|---------|---------|
| 🔴 CRITICAL | 3 (DSPy mock — ложные) |
| 🟠 HIGH | 32 (generic patterns) |
| 🟡 MEDIUM | 21 |
| ⚪ LOW | 428 |
| **Вердикт** | ✅ Чистый — ноль секретов, ноль инъекций, ноль shell=True |

## Feature Flags

| Флаг | Prod | Что делает |
|------|------|-----------|
| `BYBIT_WS_BB_ENABLED` | 1 | Публичный WS (kline, BB-кеш) |
| `BYBIT_WS_FULL_ENABLED` | 0 | Приватный WS (не нужен) |
| `BYBIT_ML_ENABLED` | 0 | RF ML Gate |
| `BYBIT_DSPY_ENABLED` | 1 | DSPy Gate |
| `BYBIT_ENTRY_JUDGE_ENABLED` | 1 | Entry Judge (hard gate) |
| `BYBIT_ATR_TP_ENABLED` | 1 | ATR-based TP levels |
| `BYBIT_AB_ENABLED` | 0 | A/B-тест |
| `BYBIT_REGIME_AUTO` | 0 | Авто LONG/SHORT |
| `BYBIT_OPTUNA_ENABLED` | 0 | Optuna |
| `BYBIT_PAPER_ENABLED` | 0 | Paper Trading (без риска) |
| `STRUCTURED_LOGGING` | 0 | JSON-логи в events.jsonl |

## Известные не-баги

| Проявление | Причина | Влияние |
|-----------|--------|---------|
| `check_regime LSTM: name 'nn'` | torch не установлен | Режим → NEUTRAL |
| `ab_status log: NoneType` | A/B-тест выключен | Нет |
| Heavy cycle 67-77с | REST-запросы | В бюджете 300с |
| DOGE/STG/ADA NO TP | qty < мин. ордера | PERM_SKIP, поставится при DCA |
| `orderQty truncated` | позиция мелкая | TP не ставится |

## Инварианты

1. SQLite — SSOT (WAL, synchronous=NORMAL, busy_timeout=5000)
2. SL не перезатирается хуже, capped -50%/+50%
3. Фильтры входа: fail-open
4. Entry Judge: fail-closed
5. LLM Circuit Breaker: 3 падения → отключение на 1ч
6. Circuit breaker риск-менеджера: только новые входы
7. Black swan: MARKET close ВСЕХ позиций
8. Heartbeat: каждые 12ч (отсутствие >13ч → бот мёртв)
9. Graceful shutdown: SIGTERM → SIGKILL fallback 10с
10. Max Holding: 48ч абсолют, 6ч по PnL
11. BB pre-fetch: кеш 5мин в начале тяжёлого цикла
12. Prometheus /metrics: Bearer auth
13. CI/CD: GitHub Actions smoke + GSC на push/PR

## Технические характеристики

| Метрика | Значение |
|---------|----------|
| RAM | ~35 MB |
| Main cycle | 30 seconds |
| Heavy cycle | 67-77 seconds |
| Entry Judge timeout | 5 seconds |
| LLM Circuit Breaker cooldown | 1 hour |
| БД | SQLite WAL (synchronous=NORMAL, busy_timeout=5000) |
| Деплой | Atomic (symlink swap, zero-downtime) |
| Мониторинг | Prometheus /metrics, Heartbeat 12ч, Telegram/ntfy |
| Тесты | 45 smoke + 5 module + 3 ML |

## Требования

| Компонент | Минимум | Рекомендуется |
|-----------|---------|---------------|
| OS | Ubuntu 20.04 / Debian 11 | Ubuntu 22.04 |
| RAM | 512 MB | 1 GB |
| CPU | 1 core | 2 cores |
| Storage | 1 GB | 5 GB |
| Python | 3.9+ | 3.11+ |
| Bybit | API ключи (read+write+trade) | Subaccount для изоляции |

## Зависимости

```
aiohttp, aiosqlite          — async HTTP + SQLite
numpy, pandas               — вычисления
scikit-learn                — ML (Random Forest)
torch (optional)            — LSTM regime detection
dspy-ai (optional)          — LLM optimization
websocket-client (optional) — WS-потоки
```

## Что включено

| Компонент | Описание |
|-----------|----------|
| 25+ модулей | Asyncio, type hints, документированы |
| Feature flags | 10+ переключателей стратегий |
| Session params | NY/Asia/Weekend адаптация |
| Риск-менеджмент | Multi-layer: 7 фильтров, circuit breaker, black swan |
| ML Stack | LSTM + RF + DSPy + post-trade cluster analysis |
| Self-learning | JSONL-журнал, адаптивные пороги |
| Тесты | 53 теста (smoke + module + ML) |
| CI/CD | GitHub Actions |
| Документация | AGENTS.md, roadmap, troubleshooting |

## ═══════════════════════════════════════
## НАВИГАЦИЯ ДЛЯ AI-АГЕНТОВ
## ═══════════════════════════════════════

### Если нужно найти — смотри

| Задача | Файл | Строка/функция |
|--------|------|---------------|
| Почему позиция без SL? | `auto_sl.py` + `events.log` | `check_and_fix_sl()` + grep "NO SL" |
| Почему позиция без TP? | `auto_tp.py` + `events.log` | `auto_take_profit()` + grep "NO TP" |
| Почему нет новых входов? | grep "OB BLOCK\|VOL BLOCK\|ENTRY JUDGE BLOCK\|CORR BLOCK" | `events.log` |
| Где логика входа? | `auto_entry.py` | `auto_entry_scan()` |
| Где риск-менеджмент? | `risk_manager.py` | `check_risk_limits()`, `check_black_swan()` |
| Где стоп-лоссы? | `auto_sl.py` | `check_and_fix_sl()`, `check_breakeven_sl()` |
| Где тейк-профиты? | `auto_tp.py` | `auto_take_profit()` |
| Где самообучение? | `journal/self_learn.py` | `apply_journal_insights()` |
| Где кластерный анализ? | `post_trade.py` | `analyze_clusters()`, `is_cluster_blocked()` |
| Где сессии? | `session_params.py` | `get_session()` |
| Где конфиг? | `~/.config/bybit-ws/config.yaml` | `~/.config/bybit-ws/env` |
| Где API-ключи? | `~/.config/bybit-ws/env` | chmod 600 |
| Как деплоить? | `deploy.sh` | атомарный symlink swap |

### Быстрый поиск по коду

```bash
# Найти все вызовы конкретной функции
grep -rn "check_black_swan\|emergency_close" ~/bybit-ws/bybit_ws/

# Найти где используется фича-флаг
grep -rn "BYBIT_WS_FULL\|BYBIT_ENTRY_JUDGE\|BYBIT_ATR_TP" ~/bybit-ws/bybit_ws/

# Найти все импорты модуля
grep -rn "from .auto_sl import\|import.*auto_sl" ~/bybit-ws/bybit_ws/

# Найти все места записи в БД
grep -rn "execute\|commit\|INSERT\|UPDATE" ~/bybit-ws/bybit_ws/state_db.py

# Найти все HTTP-запросы к Bybit
grep -rn "bybit(\|fetch_\|place_\|cancel_" ~/bybit-ws/bybit_ws/api.py
```

### Точки входа

```
python -m bybit_ws.main_async    ← Основной процесс (сервис)
python -m bybit_ws.rpc           ← RPC сервер (:8766)
deploy.sh                        ← Скрипт деплоя
test_smoke.py                    ← Тесты (запускать перед коммитом)
backtest/__init__.py             ← Backtesting engine
```

### Граф зависимостей (кто кого импортит)

```
main_async.py
  ├── api.py (REST)
  ├── ws_client.py (WebSocket)
  ├── auto_entry.py
  │   ├── orderbook_filter.py
  │   ├── volume_filter.py
  │   ├── entry_judge.py
  │   └── correlation.py
  ├── auto_short.py
  │   ├── orderbook_filter.py
  │   ├── volume_filter.py
  │   └── entry_judge.py
  ├── auto_sl.py (ATR-adaptive)
  ├── auto_tp.py (ATR-based TP)
  ├── trailing_sl.py
  ├── time_exit.py
  ├── risk_manager.py (black swan + emergency)
  ├── bb_prefetch.py
  ├── session_params.py
  ├── post_trade.py
  └── journal/self_learn.py

api.py ── самостоятельный (REST-клиент Bybit v5)
state_db.py ── самостоятельный (SQLite SSOT)
rpc.py ── самостоятельный (HTTP-сервер)
```

### Data flow

```
Bybit API ──REST──▶ main_async ──▶ SQLite (state.db)
                  │
                  ├──▶ auto_entry ──▶ Entry Judge (LLM) ──▶ ордер
                  ├──▶ auto_sl ──▶ trading-stop
                  ├──▶ auto_tp ──▶ limit-ордера
                  └──▶ time_exit ──▶ market-close

События ──▶ events.log ──▶ grep для отладки
Метрики ──▶ metrics.json ──▶ Prometheus /metrics
Сделки  ──▶ trade_history (SQLite) ──▶ journal analyzer
```

### Принятие решений

```
Позиция без SL?
  ├─ is_manual_position? → skip
  ├─ mark > entry (LONG)? → BE-SL = entry
  ├─ ATR available? → SL = entry ± k×ATR (capped -50%)
  └─ no ATR? → SL = BB-based fallback

Позиция без TP?
  ├─ is_manual_position? → skip
  ├─ qty < min_order? → PERM_SKIP (поставится при DCA)
  ├─ PERM_SKIP active? → skip (time-decay 24ч)
  └─ OK → TP1=entry+1.0×ATR(40%), TP2=2.0×ATR(35%), TP3=3.0×ATR(25%)

Новый сигнал?
  ├─ MTF confluence < 2? → skip
  ├─ orderbook imbalance нейтрален? → skip
  ├─ volume в шумовой зоне (0.7-1.3×SMA)? → skip
  ├─ Entry Judge → revise? → skip
  ├─ correlation > 0.85 с открытой? → skip
  ├─ post-trade cluster WR < 40%? → skip
  ├─ circuit breaker active? → skip
  └─ все OK → вход!

Black swan?
  ├─ BTC -8% за час? → emergency close ALL
  └─ PnL > 2× daily limit? → emergency close ALL
```

## ═══════════════════════════════════════
## СХЕМА БД (state.db)
## ═══════════════════════════════════════

| Таблица | Назначение | Ключевые поля |
|---------|-----------|--------------|
| `positions` | Открытые позиции (SSOT) | symbol, side, qty, entry, mark, stopLoss, upnl, opened_at |
| `trade_history` | Закрытые сделки | symbol, side, entry_price, exit_price, size, pnl, fees, strategy, entry_at, closed_at |
| `orders` | История ордеров | orderId, symbol, status, kind, price, qty, side, createdTime |
| `circuit_breakers` | Состояние CB | name, failures, cooldown_until |
| `cooldowns` | Кулдауны SL re-entry | symbol, side, until_ts |
| `short_state` | SHORT позиции | symbol, last_short_ts, entry_price |
| `pump_state` | Памп-монеты | symbol, first_seen_ts, alerts |
| `x10_limits` | Дневные лимиты x10 | date, pnl, margin_used, entries |
| `kv_store` | Ключ-значение | key, value (rpc_auth_token, config) |
| `metrics_daily` | Дневные метрики | date, tp_count, sl_count, pnl, win_rate |

### Быстрые запросы к БД

```bash
DB=~/.local/share/bybit-ws/state.db

# Открытые позиции с PnL
sqlite3 $DB "SELECT symbol, side, entry, mark, upnl FROM positions"

# Последние 10 сделок
sqlite3 $DB "SELECT symbol, side, pnl, datetime(closed_at,'unixepoch') FROM trade_history ORDER BY closed_at DESC LIMIT 10"

# Статус circuit breaker
sqlite3 $DB "SELECT * FROM circuit_breakers"

# Токен для RPC
sqlite3 $DB "SELECT value FROM kv_store WHERE key='rpc_auth_token'"

# Проверка целостности
sqlite3 $DB "PRAGMA integrity_check"

# Размер БД
ls -lh $DB
```

## ═══════════════════════════════════════
## ДИАГНОСТИКА ПО ЛОГАМ (events.log)
## ═══════════════════════════════════════

```bash
LOG=~/.local/share/bybit-ws/events.log

# Последние ошибки
grep -iE "error|exception|traceback" $LOG | tail -20

# Почему бот не входит (какой фильтр сработал)
grep -E "OB BLOCK|VOL BLOCK|ENTRY JUDGE BLOCK|CORR BLOCK|CB BLOCK|CLUSTER BLOCK" $LOG | tail -30

# Все срабатывания Black Swan
grep "BLACK SWAN\|emergency close" $LOG

# Время тяжёлого цикла (должно быть <100с)
grep "heavy cycle.*done" $LOG | tail -5

# TP/SL алерты
grep "TP/SL ALERT" $LOG | tail -5

# Новые входы
grep "ENTRY:" $LOG | tail -10

# LLM fallback
grep "ENTRY JUDGE" $LOG | grep -E "fallback|timeout|fail|circuit" | tail -10

# Позиции через время (сколько часов висят)
grep "max holding\|TIME EXIT" $LOG | tail -5

# BB prefetch статистика
grep "BB prefetch" $LOG | tail -5

# Heartbeat (должен быть <13ч назад)
grep "Heartbeat" $LOG | tail -1
```

## ═══════════════════════════════════════
## ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ (~/.config/bybit-ws/env)
## ═══════════════════════════════════════

| Переменная | Обязательная | Назначение |
|-----------|-------------|-----------|
| `BYBIT_API_KEY` | ✅ | Bybit API ключ (read+write+trade) |
| `BYBIT_API_SECRET` | ✅ | Bybit секрет для HMAC-подписи |
| `BYBIT_TESTNET` | — | 0=mainnet, 1=testnet |
| `TELEGRAM_BOT_TOKEN` | ⚠️ | Telegram бот для алертов |
| `TELEGRAM_CHAT_ID` | ⚠️ | Куда слать (супергруппа, топик) |
| `NTFY_URL` | ⚠️ | ntfy URL для push |
| `OPENROUTER_KEY` | ⚠️ | Entry Judge LLM (Nemotron) |
| `DEEPSEEK_API_KEY` | ⚠️ | Entry Judge fallback |
| `GEMINI_API_KEY` | ⚠️ | Vision (скриншоты) |
| `BYBIT_WS_BB_ENABLED` | — | Публичный WS (default: 1) |
| `BYBIT_WS_FULL_ENABLED` | — | Приватный WS (default: 0) |
| `BYBIT_ML_ENABLED` | — | RF ML Gate (default: 0) |
| `BYBIT_DSPY_ENABLED` | — | DSPy Gate (default: 1) |
| `BYBIT_ENTRY_JUDGE_ENABLED` | — | Entry Judge (default: 1) |
| `BYBIT_ATR_TP_ENABLED` | — | ATR-based TP (default: 1) |
| `BYBIT_AB_ENABLED` | — | A/B-тест (default: 0) |
| `BYBIT_REGIME_AUTO` | — | Авто LONG/SHORT (default: 0) |
| `BYBIT_OPTUNA_ENABLED` | — | Optuna (default: 0) |

## ═══════════════════════════════════════
## RUNBOOK: ТИПИЧНЫЕ ИНЦИДЕНТЫ
## ═══════════════════════════════════════

### Бот не отвечает >15 минут

```bash
systemctl --user status bybit-ws-async
journalctl --user -u bybit-ws-async --since "15 min ago" -n 50
# Если завис — SIGKILL + старт:
systemctl --user kill -s SIGKILL bybit-ws-async
systemctl --user start bybit-ws-async
# Проверить heartbeat:
grep "Heartbeat" ~/.local/share/bybit-ws/events.log | tail -1
```

### Entry Judge постоянно в fallback/circuit

```bash
# Посмотреть что с LLM
grep "ENTRY JUDGE" ~/.local/share/bybit-ws/events.log | tail -20
# Проверить OpenRouter API
curl -s https://openrouter.ai/api/v1/models | head -5
# Сбросить CB: systemctl --user restart bybit-ws-async
# Если API мёртв — отключить judge временно:
# BYBIT_ENTRY_JUDGE_ENABLED=0 → restart
```

### Внезапный рост маржи >$250

```bash
# Срочно закрыть всё
curl -X POST http://127.0.0.1:8766/emergency_close \
  -H "Authorization: Bearer *** ~/.local/share/bybit-ws/state.db \"SELECT value FROM kv_store WHERE key='rpc_auth_token'\")"
# Проверить логи на пам-сигналы
grep "PUMP\|margin\|ALERT" ~/.local/share/bybit-ws/events.log | tail -30
```

### SQLite locked

```bash
# busy_timeout=5000 уже стоит. Проверить WAL:
DB=~/.local/share/bybit-ws/state.db
sqlite3 $DB "PRAGMA wal_checkpoint(TRUNCATE);"
# Проверить другие процессы:
lsof $DB 2>/dev/null || fuser $DB
# Проверить размер WAL:
ls -lh ${DB}-wal
```

### Позиция без TP/SL (самодиагностика)

```bash
# 1. Ручная ли позиция?
grep "manual_position\|is_manual" ~/.local/share/bybit-ws/events.log | tail -5
# 2. qty < min_order?
grep "truncated\|PERM_SKIP" ~/.local/share/bybit-ws/events.log | tail -5
# 3. Поставить вручную через Bybit UI или curl
```

## ═══════════════════════════════════════
## PRE-DEPLOY CHECKLIST
## ═══════════════════════════════════════

- [ ] `python3 test_smoke.py` → 45/45 PASS
- [ ] `python3 test_modules.py` → 5/5 PASS
- [ ] `python3 test_ml_smoke.py` → 3/3 PASS
- [ ] `sqlite3 ~/.local/share/bybit-ws/state.db "PRAGMA integrity_check"` → ok
- [ ] `curl -s http://127.0.0.1:8766/health` → {"status":"alive"}
- [ ] `grep Heartbeat ~/.local/share/bybit-ws/events.log | tail -1` → свежий (<13ч)
- [ ] `df -h ~/.local/share/bybit-ws/` → >500MB свободно
- [ ] `free -h` → >200MB RAM свободно
- [ ] `bash deploy.sh` → атомарный swap

## ═══════════════════════════════════════
## ГЛОССАРИЙ
## ═══════════════════════════════════════

| Термин | Расшифровка |
|--------|------------|
| BB | Bollinger Bands (полосы Боллинджера) |
| ATR | Average True Range — мера волатильности |
| MTF | Multi-Timeframe — свёртка D+W+M таймфреймов |
| DCA | Dollar Cost Averaging — усреднение позиции |
| CB | Circuit Breaker — предохранитель |
| SSOT | Single Source of Truth — единственный источник (SQLite) |
| WAL | Write-Ahead Log — режим SQLite для concurrent access |
| Fail-open | При ошибке фильтр пропускает (безопасно) |
| Fail-closed | При ошибке фильтр блокирует (консервативно) |
| Dry Spell | Период без входов после серии убытков |
| PERM_SKIP | Перманентный пропуск TP (time-decay 24ч) |
