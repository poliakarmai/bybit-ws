# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Карта проекта, команды, правила.  
> Обновлено: 2026-06-27 (16 коммитов за день)

## Что это

Трейдинг-монитор для Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT по BB-полосам).  
Systemd-сервис `bybit-ws-async`, ~35 MB RAM, SQLite — SSOT.

## Главный цикл (async, 30с)

```
ЦИКЛ (30с)
  ├─ Снапшот позиций (REST, 20с таймаут)
  ├─ Black Swan check — emergency close при экстриме
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
  │   │   ├─ Entry Judge (Nemotron → DeepSeek, hard gate)
  │   │   ├─ Correlation-adjusted sizing (corr>0.85→блок, >0.7→×0.5)
  │   │   └─ Risk Manager (CB, margin, max pos, banned)
  │   ├─ Auto-TP (ATR-adaptive split)
  │   ├─ TP/SL Self-Check (прямой REST-запрос)
  │   └─ Time-Based Exit (>6ч без движения → MARKET close)
  ├─ SL re-entry (с режимным фильтром)
  └─ Отчётность
```

## Структура

```
bybit-ws/
├── main_async.py         ← Главный цикл (asyncio, 30с)
├── api.py                ← Bybit v5 REST API + HMAC
├── ws_client.py          ← WebSocket (только публичные потоки: kline, BB-кеш)
├── rpc.py                ← JSON-RPC (:8766) + /metrics
├── state_db.py           ← SQLite SSOT (8 таблиц, WAL)
├── auto_entry.py         ← Авто-вход LONG + Orderbook + Entry Judge
├── auto_short.py         ← Авто-SHORT + Dry Spell Throttle
├── auto_sl.py            ← Авто-SL v2 (ATR-adaptive)
├── auto_tp.py            ← Авто-TP v2 (ATR-adaptive split)
├── trailing_sl.py        ← Трейлинг-SL
├── orderbook_filter.py   ← Фильтр стакана (27.06)
├── time_exit.py          ← Time-based exit (27.06)
├── entry_judge.py        ← Cross-model judge (fail-closed)
├── ml_scorer.py          ← ML Gate (RF)
├── dspy_optimizer.py     ← DSPy-оптимизация (LLM)
├── lstm_regime.py        ← LSTM-классификатор режима
├── correlation.py        ← Корреляции + sizing-adjustment
├── position_sizing.py    ← Динамическая маржа
├── risk_manager.py       ← Risk + black swan + emergency close
├── push_notifier.py      ← Push (ntfy + Telegram)
├── journal/              ← Журнал + самообучение
│   ├── analyzer.py       ← Анализатор сделок (FIFO, bias)
│   └── self_learn.py     ← Self-learning с JSONL-логом
├── scripts/
│   └── walkforward_rf.py ← Walk-forward RF валидация
├── deploy.sh             ← Атомарный деплой (symlink swap)
└── test_smoke.py         ← Интеграционные тесты
```

## Как запускать

```bash
# Сервис
systemctl --user start bybit-ws-async
systemctl --user status bybit-ws-async

# Деплой
cp ~/bybit-ws/bybit_ws/*.py ~/.local/lib/bybit_ws/ && \
cp ~/bybit-ws/*.py ~/.local/lib/bybit_ws/ 2>/dev/null; \
systemctl --user kill -s SIGKILL bybit-ws-async; sleep 2; \
systemctl --user start bybit-ws-async

# Атомарный деплой (с тестами и canary)
bash deploy.sh

# Тесты
python3 test_smoke.py          # 16 интеграционных
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
  → Entry Judge (Nemotron → DeepSeek, fail-closed)
  → Correlation sizing (r>0.85→блок, r>0.70→×0.5)
  → Risk Manager (CB, margin, max pos, banned)
  → Ордер
```

## Orderbook Imbalance Filter

- Стакан ±0.5% от mid, bid_vol/(bid_vol+ask_vol)
- >0.55 → LONG OK, <0.45 → SHORT OK, середина → блок
- API недоступен → pass (fail-open)

## Time-Based Exit

- Позиция >6ч, PnL < 0%, нет частичного TP → MARKET close
- Освобождает слоты под свежие сигналы

## Entry Judge — hard gate

Двухэтапная проверка (fail-closed):
1. **Nemotron** (OpenRouter, бесплатный) — verdict pass/revise
2. **DeepSeek** fallback если Nemorton недоступен
3. Оба упали → **revise** (блокируем вход)

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

## Black Swan / Emergency Close

Триггеры: PnL > 2× max_daily_loss ИЛИ BTC -15% за час.  
Действие: MARKET close ВСЕХ позиций.

## Correlation-Adjusted Sizing

- max_corr > 0.85 с любой открытой позицией → блок
- max_corr > 0.70 → размер × 0.5
- Защита от «одной большой позиции на BTC»

## Self-Learning

- `self_learn.py` — адаптивный min_score, TP/SL ratio
- Все изменения → `self_learn.jsonl` (timestamp, параметр, старое/новое, причина)
- Просмотр: `python3 ~/.hermes/scripts/trading-self-learn-view.py`

## WebSocket

Публичные потоки (kline, BB-кеш) включены (`BYBIT_WS_BB_ENABLED=1`).  
Приватные потоки выключены (`BYBIT_WS_FULL_ENABLED=0`).  
**Причина:** стратегия на дневных свечах — задержка REST-поллинга 30с некритична.  
Приватный WS нужен для скальпинга/HFT — не наш случай.

## Risk Manager

| Параметр | Значение |
|----------|---------|
| Max позиций | 12 (5 при высокой волатильности, BTC ATR proxy) |
| Max дневной убыток | -$50 |
| Max маржа | $300 |
| Circuit breaker | 80% от max_daily_loss |
| Black swan close | 2× max_daily_loss или BTC -15%/час |

## Feature Flags (актуальные)

| Флаг | Prod | Что делает |
|------|------|-----------|
| `BYBIT_WS_BB_ENABLED` | 1 | Публичный WS (kline, BB-кеш) |
| `BYBIT_WS_FULL_ENABLED` | 0 | Приватный WS (не нужен) |
| `BYBIT_ML_ENABLED` | 0 | RF ML Gate |
| `BYBIT_DSPY_ENABLED` | 1 | DSPy Gate (GPT-4o-mini) |
| `BYBIT_ENTRY_JUDGE_ENABLED` | 1 | Entry Judge (hard gate) |
| `BYBIT_AB_ENABLED` | 0 | A/B-тест |
| `BYBIT_REGIME_AUTO` | 0 | Авто LONG/SHORT по LSTM |
| `BYBIT_OPTUNA_ENABLED` | 0 | Optuna-параметры |

## Известные не-баги

| Проявление | Причина | Влияние |
|-----------|--------|---------|
| `check_regime LSTM: name 'nn' is not defined` | torch не установлен | Режим → NEUTRAL (безопасно) |
| `ab_status log: 'NoneType' has no attribute 'get'` | A/B-тест выключен | Нет влияния |
| Heavy cycle 70-80с | Много REST-запросов | В пределах 30с×10=300с бюджета |
| `orderQty truncated to zero` (STG/ADA) | Слишком мелкая позиция | TP не ставится (PERM_SKIP) |

## Инварианты

1. SQLite — SSOT
2. SL не перезатирается хуже
3. Фильтры входа: fail-open (ошибка → пропускаем)
4. Entry Judge: fail-closed (ошибка → блок)
5. Circuit breaker: только новые входы
6. Black swan: закрытие ВСЕХ позиций
7. API-ключи только из env
8. Auto-TP + Time-Exit + Self-Check каждый тяжёлый цикл
