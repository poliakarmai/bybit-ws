# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Карта проекта, команды, правила.  
> Обновлено: 2026-06-27 (авто-TP fix, Entry Judge DeepSeek fallback, TP/SL self-check)

## Что это

Трейдинг-монитор для Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT по BB-полосам).  
Systemd-сервис `bybit-ws-async`, ~35 MB RAM, SQLite — SSOT.

## Главный цикл (async, 30с)

```
ЦИКЛ (30с)
  ├─ Снапшот позиций (WS + REST-fallback)
  ├─ Лёгкие проверки (каждый цикл):
  │   ├─ SL check + fix
  │   ├─ Trailing SL
  │   └─ Funding rotation
  ├─ Тяжёлый цикл (каждые N×30с):
  │   ├─ Режим рынка (LSTM)
  │   ├─ Auto-SHORT
  │   ├─ Корреляции
  │   ├─ Пампы / перекупленность
  │   ├─ DCA
  │   ├─ Partial TP
  │   ├─ Auto-Entry (LONG) + Entry Judge + Risk Manager
  │   ├─ Auto-TP ← (добавлен 27.06)
  │   └─ TP/SL Self-Check ← (добавлен 27.06)
  ├─ SL re-entry
  └─ Отчётность
```

## Структура

```
bybit-ws/
├── main_async.py         ← Главный цикл (asyncio, 30с)
├── main.py               ← Старый синхронный цикл (не используется)
├── api.py                ← Bybit v5 REST API + HMAC
├── ws_client.py          ← WebSocket (публичные + приватные потоки)
├── rpc.py                ← JSON-RPC (:8766) + /metrics
├── state_db.py           ← SQLite SSOT (8 таблиц, WAL)
├── auto_entry.py         ← Авто-вход LONG + Entry Judge
├── auto_short.py         ← Авто-SHORT + Dry Spell Throttle
├── auto_sl.py            ← Авто-SL + безубыток
├── auto_tp.py            ← Авто-TP (20% middle BB + 80% upper BB)
├── trailing_sl.py        ← Трейлинг-SL
├── entry_judge.py        ← Cross-model judge (Nemotron → DeepSeek fallback)
├── ml_scorer.py          ← ML Gate (RF)
├── dspy_optimizer.py     ← DSPy-оптимизация (LLM)
├── lstm_regime.py        ← LSTM-классификатор режима
├── rl_agent.py           ← RL (DQN)
├── ensemble.py           ← Ансамбль ML
├── correlation.py        ← Корреляции
├── position_sizing.py    ← Динамическая маржа
├── x10_limits.py         ← Дневной лимит x10
├── risk_manager.py       ← Глобальный risk-менеджмент + circuit breaker
├── push_notifier.py      ← Push (ntfy + Telegram)
├── web/                  ← Дашборд v5.0 (:9999)
├── deploy.sh             ← Атомарный деплой
└── test_smoke.py         ← Интеграционные тесты
```

## Как запускать

```bash
# Сервис
systemctl --user start bybit-ws-async
systemctl --user status bybit-ws-async

# Деплой
cp ~/bybit-ws/bybit_ws/*.py ~/.local/lib/bybit_ws/ && systemctl --user restart bybit-ws-async

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
| Токен RPC | `SELECT value FROM kv_store WHERE key='rpc_auth_token'` из state.db |

## MCP-инструменты

| Инструмент | Назначение |
|-----------|-----------|
| `scan_market(mode, interval)` | Скан Bollinger Grid сигналов |
| `get_positions()` | Текущие позиции + PnL |
| `get_metrics()` | Дневные метрики (TP/SL/входы) |
| `get_risk_status()` | Лимиты риска + circuit breaker |
| `place_entry(symbol, side, qty)` | Вход в позицию |

**Воркфлоу:** `scan_market` → `get_risk_status` → `get_positions` → `place_entry`

## Entry Judge (27.06.2026)

Двухэтапная проверка перед каждым входом:
1. **Nemotron** (OpenRouter, бесплатный) → verdict pass/revise
2. **DeepSeek** fallback если Nemotron недоступен

Скрипт: `~/.hermes/scripts/cross-model-judge.py`  
Feature flag: `BYBIT_ENTRY_JUDGE_ENABLED=1`  
Таймаут: 15 сек (entry_judge.py), 120 сек (API call)

Логика: если оба судьи недоступны → `pass` (консервативно, не блокируем вход)

## Auto-TP (27.06.2026 — ИСПРАВЛЕНО)

**Баг:** `main_async.py` импортировал `auto_tp`, но не вызывал. TP не выставлялись.  
**Фикс:** добавлен вызов `auto_take_profit()` + `apply_auto_tp()` в тяжёлый цикл.

Стратегия TP:
- LONG: 20% на middle BB, 80% на upper BB
- SHORT: 20% на middle BB, 80% на lower BB
- Min qty < 0.5 → весь объём на дальний TP
- 3 фейла → PERM_SKIP (ждут докупки, сбрасывается при росте позиции на 20%+)

Файл skip-листа: `~/.local/share/bybit-ws/tp_skip.json`

## TP/SL Self-Check (27.06.2026)

Каждый тяжёлый цикл проверяет: у всех позиций есть SL и TP ордера.  
При отсутствии — `🔴 TP/SL ALERT` в лог + попытка авто-исправления через auto_tp.

## ML Gate / DSPy

| Feature flag | Default | Что делает |
|-------------|---------|-----------|
| `BYBIT_ML_ENABLED` | 0 | RF ML Gate (F1=0.921) |
| `BYBIT_DSPY_ENABLED` | 1 | DSPy Gate (LLM: GPT-4o-mini) |
| `BYBIT_ENTRY_JUDGE_ENABLED` | 1 | Entry Judge (Nemotron→DeepSeek) |
| `BYBIT_WS_FULL_ENABLED` | 0 | Полный WS (приватные потоки) |
| `BYBIT_AB_ENABLED` | 0 | A/B-тест стратегий |
| `BYBIT_REGIME_AUTO` | 0 | Авто LONG/SHORT по режиму |
| `BYBIT_OPTUNA_ENABLED` | 0 | Optuna-параметры |

## Risk Manager

| Параметр | Значение |
|----------|---------|
| Max позиций | 12 (5 при высокой волатильности) |
| Max дневной убыток | -$50 |
| Max маржа | $300 |
| Circuit breaker | авто при превышении лимитов |

## Инварианты

1. SQLite — SSOT. JSON не может противоречить state.db
2. SL не перезатирается хуже (только в сторону прибыли)
3. ML fail-closed: ошибка → нейтрально, не блокирует вход
4. HMAC-подпись ML-моделей
5. API-ключи только из env
6. Circuit breaker — только новые входы, существующие позиции не трогает
7. Entry Judge fail-open: ошибка → pass (не блокирует вход)
8. Auto-TP + Self-Check каждый тяжёлый цикл
