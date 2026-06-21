# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Карта проекта, команды, правила.  
> История изменений — в `docs/history.md`.

## Что это

Трейдинг-монитор для Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT по BB-полосам).  
Systemd-сервис `bybit-ws-async`, ~35 MB RAM, SQLite — единственный источник истины (SSOT).

## Структура

```
bybit-ws/
├── main_async.py         ← Главный цикл (asyncio, 30с). WS-full: real-time позиции при BYBIT_WS_FULL_ENABLED=1
├── api.py                ← Bybit v5 REST API + HMAC-подпись
├── ws_client.py          ← WebSocket-клиент: публичные (kline, tickers, orderbook.1) + приватные (position, execution, wallet) потоки
├── rpc.py                ← JSON-RPC сервер (:8766) + /metrics
├── state_db.py           ← SQLite SSOT (8 таблиц, WAL)
├── ab_test.py            ← A/B-тестирование стратегий (Фаза 5.3)
├── auto_entry.py         ← Авто-вход LONG
├── auto_short.py         ← Авто-SHORT
├── auto_sl.py            ← Авто-SL + безубыток
├── auto_tp.py            ← Авто-TP
├── trailing_sl.py        ← Трейлинг-SL
├── ml_scorer.py          ← ML Gate (RF)
├── dspy_optimizer.py     ← DSPy-оптимизация (Фаза 5.1)
├── lstm_regime.py        ← LSTM-режим
├── rl_agent.py           ← RL-агент (DQN)
├── ensemble.py           ← Ансамбль ML
├── correlation.py        ← Корреляционная матрица
├── position_sizing.py    ← Динамическая маржа
├── x10_limits.py         ← Дневной лимит x10
├── risk_manager.py       ← Глобальный risk-менеджмент (Фаза 6.7)
├── push_notifier.py      ← Push-уведомления: ntfy + Telegram (Фаза 6.4)
├── optuna_tuner.py       ← Optuna-подбор параметров (Фаза 5.2)
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
| A/B-тест SQLite | `~/.local/share/bybit-ws/state.db` (таблица `ab_results`) |
| Optuna-параметры | `~/.config/bybit-ws/optuna_params.json` |
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

**RPC Endpoints (Фаза 6.7):**
| Endpoint | Метод | Назначение |
|----------|-------|-----------|
| `/rpc/risk_full` | GET | Полный отчёт: daily PnL, маржа, корреляции, circuit_breaker, max_positions |
| `/rpc/circuit_breaker` | GET | Статус circuit breaker |
| `/rpc/circuit_breaker` | POST | Сброс circuit breaker (`{"action": "reset"}`) |

**Воркфлоу:** `scan_market` → `get_risk_status` → `get_positions` → `place_entry`.

## ML Gate / DSPy (Фаза 5.1)

| Команда | Назначение |
|---------|-----------|
| `python3 ml_scorer.py --train` | Обучить RandomForest (RF) |
| `python3 ml_scorer.py --info` | Инфо о RF модели |
| `python3 dspy_optimizer.py --train` | Обучить DSPy (нужен LLM) |
| `python3 dspy_optimizer.py --info` | Инфо о DSPy модели |
| `python3 dspy_optimizer.py --test` | Тест на исторических данных |

**Логика:** RF (F1=0.921) + DSPy голосование.
DSPy оптимизирует взвешивание признаков через BootstrapFewShot + MIPROv2.
Результат: комбинированный score = 0.5×RF + 0.5×DSPy.

**Feature flags:**
- `BYBIT_ML_ENABLED=1` (default) — включает RF ML Gate
- `BYBIT_DSPY_ENABLED=0` (default) — включает DSPy-гейт (нужен LLM)

**Пороги:** RF threshold=0.22, DSPy threshold=50.0 (score 0-100).
Голосование: вход только если ОБА гейта пропускают.

## A/B-тестирование стратегий (Фаза 5.3)

| Команда | Назначение |
|---------|-----------|
| `python3 ab_test.py status` | Текущий статус A/B-теста |
| `python3 ab_test.py report` | Детальный отчёт с метриками |
| `python3 ab_test.py reset` | Сброс A/B-теста |
| `python3 ab_test.py enable` | Включить (BYBIT_AB_ENABLED=1) |
| `python3 ab_test.py disable` | Выключить (BYBIT_AB_ENABLED=0) |
| `curl -H 'Authorization: Bearer TOKEN' http://localhost:8766/rpc/ab_status` | RPC-статус |
| `curl -H 'Authorization: Bearer TOKEN' http://localhost:8766/rpc/ab_test_report` | RPC-отчёт |

**Логика:** для каждого сигнала случайно назначается вариант A (базовые параметры) или B (изменённые SL/TP/BB).
Paper-позиции открываются для ОБОИХ вариантов, реальная — только для назначенного.
После 30+ закрытых сделок считается статистическая значимость (bootstrap + Welch t-test).
Вердикт: «A лучше», «B лучше» или «недостаточно данных».

## Optuna-оптимизация параметров (Фаза 5.2)

| Команда | Назначение |
|---------|-----------|
| `python -m bybit_ws.optuna_tuner --symbol LINKUSDT --trials 100` | Оптимизация одного тикера |
| `python -m bybit_ws.optuna_tuner --all --trials 50` | Оптимизация всех тикеров |
| `python -m bybit_ws.optuna_tuner --show LINKUSDT` | Показать сохранённые параметры |

**Логика:** Optuna (TPE sampler) подбирает BB-период (10-50), BB std (1.5-3.0),
SL% (2-10%), TP% (5-30%), min_score (10-40) на исторических данных.
Целевая функция: max(total_pnl × win_rate × √trades).
Результаты сохраняются в `~/.config/bybit-ws/optuna_params.json`.

**Feature flag:** `BYBIT_OPTUNA_ENABLED=0` (default) — при `=1` main_async.py загружает
оптимизированные параметры, переопределяя per-symbol min_score при входе.
Существующие позиции не трогаются — только параметры новых входов.

## LSTM-классификатор рыночного режима (Фаза 5.4)

| Команда | Назначение |
|---------|-----------|
| `python3 lstm_regime.py --train` | Обучить LSTM-модель (BTC+ETH, 11 признаков) |
| `python3 lstm_regime.py --predict` | Предсказать текущий режим |
| `python3 lstm_regime.py --info` | Инфо о модели |

**Архитектура:** Input(30, 11) → LSTM(64) → LSTM(32) → Dense(32) → Dense(5) softmax.

**Признаки (11):**
- 8 технических: daily_return, hl_range, bb_pct, bb_width, RSI(14), ATR(14)/close, volume_ratio, momentum_5
- 3 макро: BTC Dominance (CoinGecko), ETH/BTC ratio (Bybit), Fear & Greed (alternative.me)

**Классы (5):** TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOL, LOW_VOL.

**Авто-переключение LONG/SHORT (BYBIT_REGIME_AUTO=1):**
- TRENDING_UP → только LONG (SHORT запрещён)
- TRENDING_DOWN → только SHORT (LONG запрещён)
- RANGING/HIGH_VOL/LOW_VOL → оба разрешены

**Feature flags:**
- `BYBIT_REGIME_AUTO=0` (default) — авто-переключение выключено (LONG и SHORT всегда разрешены)
- `BYBIT_REGIME_AUTO=1` — включает авто-переключение стратегий по предсказанному режиму

## MTF-конфлюенс 3/3 алерты (Фаза 4.3.4)

**Логика:** при MTF-конфлюенсе 3/3 (D+W+M) отправляется алерт через Telegram.
Формат: `🔥 STRONG CONFLUENCE: SYMBOL LONG/SHORT D+W+M (score=N)`.
Дедупликация: не чаще 1 алерта в 30 минут на символ (уровень CONFLUENCE, TTL=1800с).

Интегрировано в `auto_entry.py::_filter_by_mtf_confluence()` и `auto_short.py::_check_short_mtf()`.

## Push-уведомления (Фаза 6.4)

**Модуль:** `push_notifier.py` — мобильные push-уведомления через ntfy + Telegram fallback.

**Провайдеры (в порядке приоритета):**
1. **ntfy** — бесплатный, self-hosted или `ntfy.sh`. Требует топик (`NTFY_TOPIC`).
2. **Telegram** — fallback (через существующий `send_telegram_alert()`).

**Приоритеты:**
| Приоритет | Уровни алертов | Звук (ntfy) | Описание |
|-----------|----------------|-------------|----------|
| CRITICAL | STOP (SL/ликвидация) | siren | Max-приоритет, вибрация даже в DnD |
| HIGH | ENTRY, TP | arrow_up | Высокий приоритет |
| NORMAL | CONFLUENCE, INFO | bell | Обычный |

**Дедупликация:** не слать одинаковый алерт чаще 5 минут (на основе SHA256 хеша msg + приоритет).

**Конфигурация (env):**
- `PUSH_ENABLED=1` (default) — глобальный флаг включения
- `NTFY_TOPIC` — имя топика (обязательно для ntfy)
- `NTFY_SERVER` — URL сервера (default: `https://ntfy.sh`)

**Интеграция:** `main_async.py` вызывает `send_critical_alert()` / `send_high_alert()` для STOP/ENTRY/TP.
Telegram-супергруппа не дублируется: `telegram_fallback=False` для trading-алертов (супергруппа — архив, телефон — пуш).
Если модуль `push_notifier` не импортируется — используется чистый Telegram (как раньше).

**API push_notifier:**
| Функция | Назначение |
|---------|-----------|
| `send_push(msg, level, ...)` | Главная: ntfy → Telegram fallback |
| `send_critical_alert(msg)` | CRITICAL (SL/ликвидация) |
| `send_high_alert(msg, level)` | HIGH (вход/TP) |
| `send_normal_alert(msg, level)` | NORMAL (сигналы/инфо) |
| `get_push_status()` | Статус каналов (enabled, ntfy_configured, ...) |

## Dry Spell Throttle для SHORT (Фаза 6.8)

**Модуль:** `auto_short.py` — throttle холостых SHORT-циклов.

**Проблема:** `check_auto_short` гоняет бюджет на символах без сигналов (1547+ записей «budget исчерпан»). ETHPERP, BCHUSDT, XMRUSDT и др. проверяются каждые 6 минут безрезультатно.

**Решение:** если символ 3+ цикла подряд не дал ни одного входа — пропускать его 30 минут.

**Константы:**
- `DRY_SPELL_THRESHOLD = 3` — после 3 холостых проверок
- `DRY_SPELL_COOLDOWN = 1800` — пропуск 30 минут

**Логика:**
1. Каждый цикл: символ прошёл BB → `processed_syms.add(sym)`
2. После цикла: для символов без входа → `dry_spell_count += 1`
3. При `dry_spell_count >= 3` → пропуск на 30 мин
4. При любом входе → сброс `dry_spell_count = 0`

**Эффект:** экономия ~80% холостых BB-запросов на «мёртвых» символах.

## Инварианты (что не должно ломаться)

1. **SQLite — SSOT.** Никакой JSON не может противоречить `state.db`.
2. **SL не перезатирается хуже.** Если SL уже на стороне прибыли — не трогать.
3. **ML fail-closed.** Ошибка ML → возврат 0.5 (нейтрально), не блокирует вход.
4. **HMAC подпись моделей.** Загрузка ML-модели без валидной подписи — отказ старта.
5. **Ключи только из env.** Никаких хардкодов API-ключей в коде.
6. **Feature flag `BYBIT_ML_ENABLED=0`** отключает весь ML — быстрый откат.
7. **Feature flag `BYBIT_AB_ENABLED=1`** включает A/B-тест стратегий (default 0).
8. **Feature flag `BYBIT_OPTUNA_ENABLED=0`** (default) — Optuna-параметры не применяются на входе.
9. **Feature flag `BYBIT_DSPY_ENABLED=0`** отключает DSPy-оптимизацию (default 0) — независимый откат.
10. **Feature flag `BYBIT_WS_FULL_ENABLED=0`** (default) — только публичный WS (kline+BB). При `=1`: полный WS с приватными потоками (orderbook + position + execution + wallet), real-time push вместо REST-опроса позиций.
11. **Risk Manager fail-open.** Ошибка risk_manager.check() → вход разрешён (не блокирует торговлю). Circuit breaker — исключение: при активном CB все новые входы запрещены.
12. **Circuit breaker — только новые входы.** Существующие позиции не закрываются автоматически. SL/TP/трейлинг продолжают работать.
13. **Feature flag `BYBIT_REGIME_AUTO=0`** (default) — авто-переключение LONG/SHORT выключено. При `=1` режим рынка управляет разрешёнными направлениями.

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
