# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Карта проекта, команды, правила.  
> Обновлено: 2026-06-28 (13 коммитов: операционка + стратегии + observability)

## Что это

Трейдинг-монитор для Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT по BB-полосам).  
Systemd-сервис `bybit-ws-async`, ~35 MB RAM, SQLite — SSOT.

## Главный цикл (async, 30с)

```
ЦИКЛ (30с)
  ├─ Снапшот позиций (REST, 20с таймаут)
  ├─ Black Swan check (PnL 2× limit ИЛИ BTC -8%/час)
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
├── risk_manager.py       ← Risk + black swan (-8%) + emergency_close_all
├── session_params.py     ← NY/Asia/Weekend адаптивные параметры (28.06)
├── bb_prefetch.py        ← BB batch-префетчер (28.06)
├── post_trade.py         ← Кластерный анализ win rate (28.06)
├── push_notifier.py      ← Push (ntfy + Telegram)
├── journal/              ← Журнал + самообучение
│   ├── analyzer.py       ← Анализатор сделок (FIFO, bias)
│   └── self_learn.py     ← Self-learning с JSONL-логом
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

## Auto-SL v2 — ATR-adaptive

SL = entry ± k × ATR(14), capped at -50%/+50%:

| Режим | ATR/Price | k |
|-------|-----------|---|
| high_vol | >5% | 2.5 |
| trending | 3-5% | 2.0 |
| normal | 1-3% | 1.5 |
| low_vol | <1% | 1.3 |

## Auto-TP v3 — ATR-based levels (28.06)

`BYBIT_ATR_TP_ENABLED=1` — TP = entry ± k × ATR(14):

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

## Black Swan / Emergency Close

| Триггер | Порог |
|---------|-------|
| PnL | >2× max_daily_loss |
| BTC crash | -8% за час |

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
| Black swan close | 2× daily loss или BTC -8%/час |

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
