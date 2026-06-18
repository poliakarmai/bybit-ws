# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Карта проекта, команды, правила.  
> История изменений — в `docs/history.md`.

## Что это

Трейдинг-монитор для Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT по BB-полосам).  
Systemd-сервис `bybit-ws-async`, ~35 MB RAM, SQLite — единственный источник истины (SSOT).

## Структура

```
bybit-ws/
├── main_async.py         ← Главный цикл (asyncio, 30с) [в bybit_ws/]
├── api.py                ← Bybit v5 REST API + HMAC-подпись
├── rpc.py                ← JSON-RPC сервер (:8766) + /metrics
├── state_db.py           ← SQLite SSOT (8 таблиц, WAL)
├── auto_entry.py         ← Авто-вход LONG
├── auto_short.py         ← Авто-SHORT
├── auto_sl.py            ← Авто-SL + безубыток
├── auto_tp.py            ← Авто-TP
├── trailing_sl.py        ← Трейлинг-SL
├── ml_scorer.py          ← ML Gate (RF)
├── lstm_regime.py        ← LSTM-режим
├── rl_agent.py           ← RL-агент (DQN)
├── ensemble.py           ← Ансамбль ML
├── correlation.py        ← Корреляционная матрица
├── position_sizing.py    ← Динамическая маржа
├── x10_limits.py         ← Дневной лимит x10
├── web/                  ← Дашборд v5.0 (:9999)
├── deploy.sh             ← Атомарный деплой с rollback
├── test_smoke.py         ← Интеграционные тесты
└── docs/                 ← Документация
    └── history.md        ← История фаз и аудитов
```

## Как запускать

```bash
# Сервис
systemctl --user start bybit-ws-async
systemctl --user status bybit-ws-async

# Локально
cd ~/bybit-ws && source .venv/bin/activate && python -m bybit-ws

# Деплой
bash deploy.sh

# Тесты (все должны проходить перед коммитом)
python3 test_smoke.py          # 16 интеграционных
python3 test_modules.py        # 5 модульных
python3 test_ml_smoke.py       # 3 ML (HMAC, RF, LSTM)
```

## Где что лежит

| Данные | Путь |
|--------|------|
| Позиции (SSOT) | `~/.local/share/bybit-ws/state.db` |
| Резервные снепшоты | `~/.local/share/bybit-ws/positions_snapshot.json` |
| Метрики | `~/.local/share/bybit-ws/metrics.json` |
| Логи | `journalctl -u bybit-ws` |
| Конфиг | `~/.config/bybit-ws/config.yaml` |
| Креды | `~/.config/bybit-ws/env` (chmod 600) |
| RPC | `http://127.0.0.1:8766` |
| MCP-сервер | `~/.local/bin/bybit-mcp-server.py` |

**Для AI-агентов:** пути можно получить через `curl http://127.0.0.1:8766/rpc/paths` (без авторизации).

## MCP-инструменты

| Инструмент | Назначение |
|-----------|-----------|
| `scan_market(mode, interval)` | Скан Bollinger Grid сигналов |
| `get_positions()` | Текущие позиции + PnL |
| `get_metrics()` | Дневные метрики (TP/SL/входы) |
| `get_risk_status()` | Лимиты риска |
| `place_entry(symbol, side, qty)` | Вход в позицию |

**Воркфлоу:** `scan_market` → `get_risk_status` → `get_positions` → `place_entry`.

## Инварианты (что не должно ломаться)

1. **SQLite — SSOT.** Никакой JSON не может противоречить `state.db`.
2. **SL не перезатирается хуже.** Если SL уже на стороне прибыли — не трогать.
3. **ML fail-closed.** Ошибка ML → возврат 0.5 (нейтрально), не блокирует вход.
4. **HMAC подпись моделей.** Загрузка ML-модели без валидной подписи — отказ старта.
5. **Ключи только из env.** Никаких хардкодов API-ключей в коде.
6. **Feature flag `BYBIT_ML_ENABLED=0`** отключает весь ML — быстрый откат.

## Конвенции

- **Python 3.11+**, venv в `~/.local/lib/bybit_ws/.venv/`
- **Коммиты на русском**, с хешами в логах
- **Перед опасными операциями** — `hermes-backup` skill
- **После деплоя** — `systemctl --user restart bybit-ws-async`
- **Сигнатура Bybit API:** `json.dumps(body, separators=(', ', ': '))` — **с пробелами!** Компактный JSON ломает подпись
- **JUNK-стратегии** отключены (`enabled: false` в конфиге)
- **RPC-авторизация** обязательна всегда (Bearer UUID из `state.db`)

## Критерии готовности задачи

- [ ] Все тесты проходят (`test_smoke.py`, `test_modules.py`, `test_ml_smoke.py`)
- [ ] Сервис стартует без ошибок (`systemctl --user status bybit-ws-async`)
- [ ] Метрики отдаются (`curl http://localhost:8766/metrics`)
- [ ] Деплой-скрипт отрабатывает (`bash deploy.sh`)
- [ ] AGENTS.md обновлён если изменились пути/команды/инварианты

## CLAUDE.md

Для совместимости с Claude Code:

```markdown
# CLAUDE.md — bybit-ws

@AGENTS.md
```
