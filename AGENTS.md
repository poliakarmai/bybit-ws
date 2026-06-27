# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Карта проекта, команды, правила.  
> Обновлено: 2026-06-27 (10 коммитов за день: operational excellence + стратегии)

## Что это

Трейдинг-монитор для Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT по BB-полосам).  
Systemd-сервис `bybit-ws-async`, ~35 MB RAM, SQLite — SSOT.

## Главный цикл (async, 30с)

```
ЦИКЛ (30с)
  ├─ Снапшот позиций (REST, 20с таймаут)
  ├─ Black Swan check (PnL 2× limit ИЛИ BTC -8%/час)
  ├─ Лёгкие проверки (каждый цикл):
  │   ├─ SL check + fix (ATR-adaptive)
  │   ├─ Trailing SL
  │   ├─ Breakeven SL (каждые 4 цикла)
  │   └─ Margin utilization
  ├─ Circuit breaker check
  ├─ Тяжёлый цикл (каждые 10 циклов = 5 мин):
  │   ├─ Режим рынка (LSTM, fallback NEUTRAL)
  │   ├─ Auto-SHORT + Dry Spell Throttle
  │   ├─ Корреляции + Пампы/Перекупленность
  │   ├─ DCA + Partial TP
  │   ├─ Auto-Entry (LONG):
  │   │   ├─ MTF Confluence (D+W+M)
  │   │   ├─ Orderbook Imbalance (bid/ask ratio)
  │   │   ├─ Entry Judge (Nemotron → DeepSeek, hard gate, 5с таймаут)
  │   │   ├─ Correlation sizing (r>0.85→блок, r>0.70→×0.5)
  │   │   └─ Risk Manager (CB, margin, max pos, banned)
  │   ├─ Auto-TP (ATR-adaptive split)
  │   ├─ TP/SL Self-Check (прямой REST-запрос)
  │   └─ Time-Based Exit (6ч без PnL или 48ч абсолют)
  ├─ SL re-entry (с режимным фильтром)
  ├─ Heartbeat (каждые 12ч)
  └─ Отчётность
```

## Структура

```
bybit-ws/
├── main_async.py         ← Главный цикл (asyncio, 30с)
├── api.py                ← Bybit v5 REST API + HMAC + fetch_open_orders
├── ws_client.py          ← WebSocket (только публичные потоки: kline, BB-кеш)
├── rpc.py                ← JSON-RPC (:8766) + /emergency_close + /kill_switch
├── state_db.py           ← SQLite SSOT (WAL, 5с busy_timeout)
├── auto_entry.py         ← Авто-вход LONG + Orderbook + Entry Judge + Correlation
├── auto_short.py         ← Авто-SHORT + Dry Spell Throttle + Orderbook
├── auto_sl.py            ← Авто-SL v2 (ATR-adaptive)
├── auto_tp.py            ← Авто-TP v2 (ATR-adaptive split, time-decay 24ч)
├── trailing_sl.py        ← Трейлинг-SL
├── orderbook_filter.py   ← Фильтр стакана (27.06)
├── time_exit.py          ← Time-based exit: 6ч/48ч (27.06)
├── entry_judge.py        ← Cross-model judge: fail-closed + кэш + circuit breaker
├── ml_scorer.py          ← ML Gate (RF)
├── dspy_optimizer.py     ← DSPy-оптимизация (LLM)
├── lstm_regime.py        ← LSTM-классификатор режима
├── correlation.py        ← Корреляции + max_corr_with_open
├── position_sizing.py    ← Динамическая маржа
├── risk_manager.py       ← Risk + black swan (-8%) + emergency_close_all
├── push_notifier.py      ← Push (ntfy + Telegram)
├── journal/              ← Журнал + самообучение
│   ├── analyzer.py       ← Анализатор сделок (FIFO, bias)
│   └── self_learn.py     ← Self-learning с JSONL-логом
├── scripts/
│   └── walkforward_rf.py ← Walk-forward RF валидация
├── deploy.sh             ← Атомарный деплой (symlink swap, SIGTERM→SIGKILL)
└── test_smoke.py         ← Интеграционные тесты (45/45)
```

## Как запускать

```bash
# Сервис
systemctl --user start bybit-ws-async
systemctl --user status bybit-ws-async

# Деплой (ручной)
cp ~/bybit-ws/bybit_ws/*.py ~/.local/lib/bybit_ws/ && \
cp ~/bybit-ws/*.py ~/.local/lib/bybit_ws/ 2>/dev/null; \
systemctl --user stop bybit-ws-async; sleep 3; \
systemctl --user start bybit-ws-async

# Атомарный деплой (с тестами, SIGTERM, canary, rollback)
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
| Self-learning лог | `~/.local/share/bybit-ws/self_learn.jsonl` |
| Walk-forward RF | `~/.local/share/bybit-ws/rf_walkforward.json` |
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

## Цепочка входа (полная)

```
BB-сигнал
  → MTF confluence (D+W+M)
  → Orderbook Imbalance (bid/ask >0.55 LONG, <0.45 SHORT)
  → Entry Judge (Nemotron → DeepSeek, 5с, fail-closed + кэш 300с)
  → Correlation sizing (r>0.85→блок, r>0.70→×0.5)
  → Risk Manager (CB, margin, max pos, banned)
  → Ордер
```

## Аварийные ситуации

```bash
# Закрыть ВСЕ позиции (kill switch)
curl -X POST http://127.0.0.1:8766/kill_switch \
  -H "Authorization: Bearer $(sqlite3 ~/.local/share/bybit-ws/state.db \"SELECT value FROM kv_store WHERE key='rpc_auth_token'\")"

# Только emergency close (без паузы)
curl -X POST http://127.0.0.1:8766/emergency_close \
  -H "Authorization: Bearer TOKEN"

# Сбросить LLM Circuit Breaker
systemctl --user restart bybit-ws-async

# Проверить heartbeat (должен приходить каждые 12ч)
grep "Heartbeat" ~/.local/share/bybit-ws/events.log | tail -1
```

## Entry Judge — hard gate + circuit breaker

1. **Nemotron** (OpenRouter, бесплатный) → verdict pass/revise
2. **DeepSeek** fallback если Nemotron недоступен
3. Оба упали → **revise** (блок)
4. **3 падения подряд** → отключение на 1 час (circuit breaker)
5. **Кэш вердиктов**: 300с TTL по хэшу контекста
6. **Таймаут**: 5 секунд на вызов

## Auto-SL v2 — ATR-adaptive

SL = entry ± k × ATR(14), где k от волатильности:

| Режим | ATR/Price | Множитель k |
|-------|-----------|------------|
| high_vol | >5% | 2.5 |
| trending | 3-5% | 2.0 |
| normal | 1-3% | 1.5 |
| low_vol | <1% | 1.3 |

Fallback на BB-based если ATR недоступен.

## Auto-TP v2 — ATR-adaptive split

| Волатильность | ATR/Price | Ближний TP | Дальний TP |
|-------------|-----------|-----------|-----------|
| Высокая | >5% | 40% | 60% |
| Нормальная | 2-5% | 25% | 75% |
| Низкая | <2% | 15% | 85% |

PERM_SKIP с time-decay 24ч (автосброс).

## Time-Based Exit

| Триггер | Действие |
|---------|---------|
| >6ч, PnL < 0%, нет частичного TP | MARKET close |
| >48ч (абсолютный максимум) | MARKET close в любом случае |

## Black Swan / Emergency Close

| Триггер | Порог |
|---------|-------|
| PnL | >2× max_daily_loss |
| BTC crash | -8% за час |

Действие: MARKET close ВСЕХ позиций, пауза до конца цикла.

## Correlation-Adjusted Sizing

- max_corr > 0.85 с любой открытой позицией → блок
- max_corr > 0.70 → размер × 0.5
- Окно корреляции: 24 × 1h свечей

## Self-Learning

- `self_learn.py` — адаптивный min_score, TP/SL ratio
- Все изменения → `self_learn.jsonl` (timestamp, параметр, старое/новое, причина)
- Просмотр: `python3 ~/.hermes/scripts/trading-self-learn-view.py`

## WebSocket

Публичные потоки (kline, BB-кеш) включены (`BYBIT_WS_BB_ENABLED=1`).  
Приватные потоки выключены (`BYBIT_WS_FULL_ENABLED=0`).  
**Причина:** стратегия на дневных свечах — задержка REST-поллинга 30с некритична.

## Risk Manager

| Параметр | Значение |
|----------|---------|
| Max позиций | 12 (5 при высокой волатильности, BTC ATR proxy) |
| Max дневной убыток | -$50 |
| Max маржа | $300 |
| Circuit breaker | 80% от max_daily_loss |
| Black swan close | 2× max_daily_loss или BTC -8%/час |

## Feature Flags (актуальные)

| Флаг | Prod | Что делает |
|------|------|-----------|
| `BYBIT_WS_BB_ENABLED` | 1 | Публичный WS (kline, BB-кеш) |
| `BYBIT_WS_FULL_ENABLED` | 0 | Приватный WS (не нужен для дневных свечей) |
| `BYBIT_ML_ENABLED` | 0 | RF ML Gate |
| `BYBIT_DSPY_ENABLED` | 1 | DSPy Gate (GPT-4o-mini) |
| `BYBIT_ENTRY_JUDGE_ENABLED` | 1 | Entry Judge (hard gate + circuit breaker) |
| `BYBIT_AB_ENABLED` | 0 | A/B-тест |
| `BYBIT_REGIME_AUTO` | 0 | Авто LONG/SHORT по LSTM |
| `BYBIT_OPTUNA_ENABLED` | 0 | Optuna-параметры |

## Известные не-баги

| Проявление | Причина | Влияние |
|-----------|--------|---------|
| `check_regime LSTM: name 'nn' is not defined` | torch не установлен | Режим → NEUTRAL (безопасно) |
| `ab_status log: 'NoneType' has no attribute 'get'` | A/B-тест выключен | Нет влияния |
| Heavy cycle 70-80с | Много REST-запросов | В пределах бюджета 300с |
| `orderQty truncated to zero` (STG/ADA) | Позиция слишком мелкая | TP не ставится (PERM_SKIP) |

## Инварианты

1. SQLite — SSOT (WAL, synchronous=NORMAL, busy_timeout=5000)
2. SL не перезатирается хуже
3. Фильтры входа: fail-open (ошибка → пропускаем)
4. Entry Judge: fail-closed (ошибка → блок)
5. LLM Circuit Breaker: 3 падения → отключение на 1ч
6. Circuit breaker риск-менеджера: только новые входы
7. Black swan: закрытие ВСЕХ позиций
8. Heartbeat: каждые 12ч (отсутствие >13ч → бот мёртв)
9. Graceful shutdown: SIGTERM → SIGKILL fallback через 10с
10. Max Holding: 48ч абсолют, 6ч для убыточных без TP
