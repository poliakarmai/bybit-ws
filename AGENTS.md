# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Карта проекта, команды, правила.  
> Обновлено: 2026-06-27 (orderbook filter, time-based exit, emergency close, ATR-adaptive TP, atomic deploy)

## Что это

Трейдинг-монитор для Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT по BB-полосам).  
Systemd-сервис `bybit-ws-async`, ~35 MB RAM, SQLite — SSOT.

## Главный цикл (async, 30с)

```
ЦИКЛ (30с)
  ├─ Снапшот позиций (WS + REST-fallback)
  ├─ Black Swan check — emergency close при экстриме
  ├─ Лёгкие проверки (каждый цикл):
  │   ├─ SL check + fix
  │   ├─ Trailing SL
  │   ├─ Breakeven SL (каждые 4 цикла)
  │   └─ Margin utilization
  ├─ Circuit breaker check
  ├─ Тяжёлый цикл (каждые N×30с):
  │   ├─ Режим рынка (LSTM)
  │   ├─ Auto-SHORT + Dry Spell Throttle
  │   ├─ Корреляции + Пампы/Перекупленность
  │   ├─ DCA + Partial TP
  │   ├─ Auto-Entry (LONG):
  │   │   ├─ MTF Confluence
  │   │   ├─ ORDERBOOK IMBALANCE ← 27.06
  │   │   ├─ Entry Judge (Nemotron → DeepSeek)
  │   │   └─ Risk Manager
  │   ├─ Auto-TP (ATR-adaptive)
  │   ├─ TP/SL Self-Check
  │   └─ TIME-BASED EXIT ← 27.06
  ├─ SL re-entry (с режимным фильтром)
  └─ Отчётность
```

## Структура

```
bybit-ws/
├── main_async.py         ← Главный цикл (asyncio, 30с)
├── api.py                ← Bybit v5 REST API + HMAC
├── ws_client.py          ← WebSocket (публичные + приватные потоки)
├── rpc.py                ← JSON-RPC (:8766) + /metrics
├── state_db.py           ← SQLite SSOT (8 таблиц, WAL)
├── auto_entry.py         ← Авто-вход LONG + Orderbook + Entry Judge
├── auto_short.py         ← Авто-SHORT + Dry Spell Throttle
├── auto_sl.py            ← Авто-SL + безубыток
├── auto_tp.py            ← Авто-TP v2 (ATR-adaptive split)
├── trailing_sl.py        ← Трейлинг-SL
├── orderbook_filter.py   ← Фильтр стакана (27.06)
├── time_exit.py           ← Time-based exit (27.06)
├── entry_judge.py        ← Cross-model judge (Nemotron → DeepSeek)
├── ml_scorer.py          ← ML Gate (RF)
├── dspy_optimizer.py     ← DSPy-оптимизация (LLM)
├── lstm_regime.py        ← LSTM-классификатор режима
├── rl_agent.py           ← RL (DQN)
├── ensemble.py           ← Ансамбль ML
├── correlation.py        ← Корреляции
├── position_sizing.py    ← Динамическая маржа
├── x10_limits.py         ← Дневной лимит x10
├── risk_manager.py       ← Risk manager + black swan + emergency close
├── push_notifier.py      ← Push (ntfy + Telegram)
├── journal/              ← Журнал + самообучение
│   ├── analyzer.py       ← Анализатор сделок (FIFO, bias)
│   └── self_learn.py     ← Self-learning с персистентным логом
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
cp ~/bybit-ws/bybit_ws/*.py ~/.local/lib/bybit_ws/ && systemctl --user restart bybit-ws-async
# или атомарный:
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
| Логи | `journalctl --user -u bybit-ws-async` |
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

**Воркфлоу:** `scan_market` → `get_risk_status` → `get_positions` → `place_entry`

## Цепочка входа (полная)

```
BB-сигнал
  → MTF confluence (D+W+M)
  → ORDERBOOK IMBALANCE (bid/(bid+ask) >0.55 LONG, <0.45 SHORT)
  → Entry Judge (Nemotron → DeepSeek fallback)
  → Risk Manager (circuit breaker, margin, correlation, max positions)
  → Ордер
```

## Orderbook Imbalance Filter (27.06)

- Запрашивает стакан ±0.5% от mid
- bid_vol/(bid_vol+ask_vol) >0.55 → LONG OK (покупатели давят)
- <0.45 → SHORT OK (продавцы давят)
- Середина → блок (~40-60% ложных входов на пробоях отсекается)
- API недоступен → pass (не блокируем)

## Time-Based Exit (27.06)

- Позиция >6 часов без движения
- PnL < 0% от входа
- Нет частичного TP (значит не дошла даже до middle BB)
- → MARKET close, освобождает слот

## Entry Judge (27.06)

Двухэтапная проверка:
1. **Nemotron** (OpenRouter, бесплатный) → verdict pass/revise
2. **DeepSeek** fallback если Nemotron недоступен

Таймаут: 15 сек, fail-open (оба упали → pass)

## Auto-TP v2 — ATR-adaptive (27.06)

| Волатильность | ATR/Price | Ближний TP | Дальний TP |
|-------------|-----------|-----------|-----------|
| Высокая | >5% | 40% | 60% |
| Нормальная | 2-5% | 25% | 75% |
| Низкая | <2% | 15% | 85% |

PERM_SKIP с time-decay 24ч (автосброс).

## Black Swan / Emergency Close (27.06)

Триггеры:
1. PnL > 2× max_daily_loss
2. BTC упал >15% за час

Действие: MARKET close ВСЕХ позиций, пауза до конца цикла.

## Self-Learning (27.06)

- `self_learn.py` — адаптивный min_score, TP/SL ratio
- Каждое изменение пишется в `self_learn.jsonl` (дата, параметр, старое/новое, причина)
- Просмотр: `python3 ~/.hermes/scripts/trading-self-learn-view.py`

## Risk Manager

| Параметр | Значение |
|----------|---------|
| Max позиций | 12 (5 при высокой волатильности, BTC ATR proxy) |
| Max дневной убыток | -$50 |
| Max маржа | $300 |
| Circuit breaker | 80% от max_daily_loss |
| Black swan close | 2× max_daily_loss или BTC -15%/час |

## Feature Flags

| Флаг | Default | Что делает |
|------|---------|-----------|
| `BYBIT_ML_ENABLED` | 0 | RF ML Gate |
| `BYBIT_DSPY_ENABLED` | 1 | DSPy Gate (LLM: GPT-4o-mini) |
| `BYBIT_ENTRY_JUDGE_ENABLED` | 1 | Entry Judge (Nemotron→DeepSeek) |
| `BYBIT_WS_FULL_ENABLED` | 0 | Полный WS (приватные потоки) |
| `BYBIT_AB_ENABLED` | 0 | A/B-тест стратегий |
| `BYBIT_REGIME_AUTO` | 0 | Авто LONG/SHORT по режиму |
| `BYBIT_OPTUNA_ENABLED` | 0 | Optuna-параметры |

## Инварианты

1. SQLite — SSOT
2. SL не перезатирается хуже (только в сторону прибыли)
3. Все фильтры fail-open: ошибка → пропускаем, не блокируем вход
4. HMAC-подпись ML-моделей
5. API-ключи только из env
6. Circuit breaker — только новые входы
7. Black swan — закрытие ВСЕХ позиций
8. Auto-TP + Time-Exit + Self-Check каждый тяжёлый цикл
