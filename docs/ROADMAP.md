# ROADMAP — bybit-ws

> План развития. Версия: 11.4.x. Обновлено: 2026-10-25 (внешний аудит, см. Фазу 11).
> Актуальный roadmap также в Obsidian: `bybit-ws/short-analysis-2026-10-19.md` и `bybit-ws/docs-audit-lena-2026-10-19.md` (`hermes/bybit-ws-roadmap.md` отсутствует).

## Текущий статус (v11.4.x)

| Компонент | Статус |
|-----------|--------|
| Bollinger Grid LONG/SHORT | ✅ prod |
| SHORT-оптимизация: World Model 33.1% + ML-фильтр | ✅ prod |
| Android: серверная часть (JWT + /set-tp + /generate-jwt) | ✅ prod |
| ATR-adaptive SL + ATR-based TP (3 уровня) | ✅ prod |
| 7 фильтров входа (MTF, Orderbook, Volume, Entry Judge, Correlation, Post-trade, Risk) | ✅ prod |
| LSTM Market Regime (5 режимов, авто LONG/SHORT; 82.3% — заявлено 01.08, не перезамерялось) | ✅ prod |
| LSTM World Model (multi-task OHLCV, entry scoring) | ✅ prod |
| REGIME_AUTO: блокировка входов по режиму рынка | ✅ prod |
| Адаптивный TP/SL по LSTM-режиму (v9) | ✅ prod |
| Self-learning (event-driven: ≥10 сделок и ≥6ч с прошлого прогона, иначе fallback через 24ч) | ✅ prod |
| Anti-ludomania: 3 убытка/час → блок 30 мин | ✅ prod |
| BlackSwan v2: корреляционный алерт (без авто-закрытия) | ✅ prod |
| Symbol blacklist (перманентный, auto_short + auto_entry) | ✅ prod |
| MTF candle cache (TTL 300с, режет REST-нагрузку) | ✅ prod |
| Time-exit SHORT по opened_at (не createdTime) | ✅ prod |
| Paper Trading (исторический бэктест) | ✅ prod |
| Веб-дашборд (SVG + HTML/Chart.js) | ✅ prod |
| MCP-инструменты (6 шт.) | ✅ prod |
| RPC REST API (20 эндпоинтов) | ✅ prod |
| One-click trading через Hermes-чат | ✅ prod |
| Алерты (ntfy + Telegram) | ✅ prod |
| Тесты (16 тестов в test_smoke.py; 16 файлов / 171 тест всего) | ✅ prod |

## Дорожная карта

### Фаза 8.0 — Документация + Paper Trade + Дашборд ✅ (01-02.08.2026)

| Задача | Статус |
|--------|--------|
| Paper Trading на исторических данных | ✅ |
| Веб-дашборд (SVG + HTML/Chart.js) | ✅ |
| Продуктовый README + ONBOARDING | ✅ |
| Graphify-driven рефакторинг (циклы импортов) | ✅ |
| Лицензия MIT → AGPL-3.0 | ✅ |

### Фаза 8.1 — LSTM World Model + Anti-ludomania ✅ (01-04.08.2026)

| Задача | Статус |
|--------|--------|
| LSTM World Model (multi-task OHLCV) | ✅ |
| World Model в LONG + SHORT скоринг | ✅ |
| Fix exit_reason (SL/TP по движению цены) | ✅ |
| Anti-ludomania (3 убытка → блок) | ✅ |
| REGIME_AUTO: LSTM блокирует входы | ✅ |
| Адаптивный TP/SL по режиму (v9) | ✅ |
| BlackSwan v2 (только алерт, без авто-закрытия) | ✅ |
| MTF-дыра закрыта (блок без данных D-TF) | ✅ |
| SL floor: 2% → 5% для LONG | ✅ |
| Тяжёлый цикл: параллелизация (56→44s) | ✅ |

### Фаза 8.2 — Systemd Hardening ✅ (04.08.2026)

| Задача | Статус |
|--------|--------|
| MemoryDenyWriteExecute vs PyTorch fix | ✅ |
| LSTM HMAC mismatch fix | ✅ |
| TimeoutStopSec=10 (зависание рестарта) | ✅ |
| lstm_world_model симлинк в bybit_ws/ | ✅ |
| MTF-скидка TRENDING_DOWN (min_tfs=1) | ✅ |
| log_event fallback на stderr | ✅ |
| AGENTS.md: Systemd Pitfalls | ✅ |
| CHANGELOG v8.0-v8.2 | ✅ |

### Фаза 9 — SHORT-оптимизация ✅ (08.08.2026)

| Задача | Приоритет | Статус |
|--------|-----------|--------|
| SHORT в TRENDING_DOWN: калибровка параметров | 🔴 | ✅ max_loss 15→10%, max_hold 48→24ч |
| Time-based SL: закрытие убыточных >12ч | 🔴 | ✅ check_short_time_sl() в основном цикле |
| Исключение imported-сделок из self-learn | 🔴 | ✅ strategy != 'imported' + manual=0 (v11.4) |
| Винрейт SHORT: анализ + улучшение | 🔴 | ✅ анализ: без STGUSDT +$146 |
| World Model качество > 33% | 🟡 | ✅ 22.3% → 33.1% (400 эпох, λ=0.01) |

### Фаза 9.1 — Risk-фикс junk-шортов ✅ (15.08.2026)

| Задача | Статус |
|--------|--------|
| Junk-шорты (Tier C/D) SL +7% (sl_tier_cd) через trading-stop | ✅ |
| DCA-мартингейл удалён (check_auto_short + check_junk_dca) | ✅ |
| Мёртвый код вычищен (JUNK_DCA_LEVELS, short_margin, SHORT_LEVERAGE) | ✅ |
| exit_reason детект по знаку closedPnl (не по цене) | ✅ |

### Фаза 9.2 — FIFO-матчинг self-learn фикс ✅ (20.08.2026)

| Задача | Статус |
|--------|--------|
| adapter.load_from_sqlite строит RoundTrip напрямую из pnl/hold_hours | ✅ |
| analyzer.compute_profile_from_roundtrips (без пересчёта FIFO) | ✅ |
| WR 28% → 66% (совпадает с прямым SQL) | ✅ |
| Каскад canary-проблемы (ложный WR 28%) устранён | ✅ |

### Фаза 9.3 — Time-exit + Blacklist + Candle-cache ✅ (27.08.2026)

| Задача | Статус |
|--------|--------|
| check_short_time_sl: createdTime → opened_at/entry_ts (pitfall #14) | ✅ |
| symbol_blacklist.py — перманентный чёрный список (auto_short + auto_entry) | ✅ |
| candle_cache.py — TTL-кэш MTF-свечей (300с), deadline 20→30с | ✅ |

### Фаза 10 — Android приложение 🟡

| Задача | Статус |
|--------|--------|
| **Код (отдельный репо `bybit-ws-android`):** Kotlin + Compose + Hilt, MVVM, 18 .kt / 1124 строк | ✅ |
| 5 экранов: Dashboard, Detail, Scan, Alerts, Settings | ✅ |
| Серверная часть: JWT, nginx :4444→8766 + rate-limit, /set-tp, /generate-jwt | ✅ |
| Global Kill-Switch из приложения | ✅ |
| **Сборка APK (assembleDebug/Release)** | ⬜ не проверена — нужен Android SDK + Gradle |
| Auto-refresh по таймеру | ⬜ |
| LONG-скан (сейчас только SHORT) | ⬜ |
| График PnL / виджет / фильтрация алертов | ⬜ |
| Установка на телефон | ⬜ |

### Multi-exchange 🟡

| Задача | Приоритет | Статус |
|--------|-----------|--------|
| ExchangeAdapter (ABC + Bybit/Binance/OKX адаптеры) | 🟡 | ✅ написан (Phase 6.5) |
| Интеграция в main_async/auto_entry/auto_short | 🟡 | ⬜ подключён только в api.py (get_bb) |

### Техдолг — зачистка 27.08.2026

| Задача | Статус |
|--------|--------|
| CI: workflow master→main + logic/regression тесты | ✅ |
| Legacy-мусор: .bak ×2, deploy.sh.old, =6.0 | ✅ удалены |
| Risk/reward SHORT: подтверждён Фазой 9.1 (PF 0.59→2.36) | ✅ диагноз |
| Дубли модулей: auto_sl↔unified_sl, main↔main_async (docker-entrypoint) | ✅ 25.09.2026 — мёртвые импорты убраны, docker → main_async, модули @deprecated |

### Долгий срок

| Задача | Приоритет |
|--------|-----------|
| DQN → PPO | 🟢 |
| — Feature Store / Data Pipeline для RL | 📋 (pre-req) |
| Grafana HTTPS (нужен домен) | 🟢 |
| Межбиржевой funding-спред (Bybit vs Binance/OKX) → сигнал в check_funding_signals | 🟡 |

### Фаза 11 — внешний аудит и контур исполнения (25.10.2026)

Источник: стратегический аудит «как превратить проект в продаваемый продукт» (ChatGPT, 1331 строка).
Аудит писался по README и части кода, поэтому **каждый пункт проверен по репозиторию**, а формулировки
прогнаны через Петра в режиме `verify` (контракт: вердикт + цитата + основание).
**Вердикт Петра: CONDITIONAL — 8 из 10 находок подтверждены дословными цитатами, GO после закрытия трёх верхних.**

#### P0 — деньги и риск

| # | Задача | Доказательство | Пётр | Статус |
|---|--------|----------------|------|--------|
| 1 | Fail-closed в риск-проверках LONG | `auto_entry.py:785` корреляция — `except Exception: pass`, вход идёт с `corr_sizer=1.0`; `:798` концентрация — только лог, без `continue`; `:810` chase guard и `:821` дневной лимит — то же | ПОДТВЕРЖДАЮ (ВЫСОКАЯ) | ⬜ |
| 2 | Корреляционная проверка для SHORT | `grep -n "corr" auto_short.py` → 0 строк; LONG блокируется при r>0.85, SHORT — никогда | ПОДТВЕРЖДАЮ (ВЫСОКАЯ) | ⬜ |
| 3 | Kill switch до конца: cancel-all + верификация | `rpc.py:1970` ставит pause и зовёт `emergency_close_all`, который закрывает позиции MARKET IOC, но не отменяет открытые лимитки, не делает повторный `fetch_positions`, нет эскалации при `failed>0` | ПОДТВЕРЖДАЮ (ВЫСОКАЯ) | ✅ `aa9bea9` |
| 4 | `orderLinkId` во всех точках входа | 18 вызовов `/v5/order/create` в 16 файлах; `orderLinkId` только `dca.py:193` и `recycle.py:108` → при таймауте возможен дубль | ПОДТВЕРЖДАЮ (ВЫСОКАЯ) | ⬜ |

#### P1 — доказательность и наблюдаемость

| # | Задача | Доказательство | Пётр | Статус |
|---|--------|----------------|------|--------|
| 5 | Издержки в бэктест-движке (fee/funding/slippage) | `bybit_ws/experiments/engine.py` (347 строк): 0 упоминаний fee/funding/slippage/commission | ПОДТВЕРЖДАЮ (ВЫСОКАЯ) | ⬜ |
| 6 | Торговая готовность в `/health` (`TRADING_DISABLED`/`DEGRADED`) | `rpc.py:998` отдаёт status/alive/uptime/cycle/paused, признака готовности нет | ПОДТВЕРЖДАЮ (СРЕДНЯЯ) | ⬜ |
| 7 | Журнал решений в БД (отказы риск-проверок) | отказы идут только в `events.log` (`alerts.log_event`, файл с ротацией); в БД нет | ПОДТВЕРЖДАЮ (уточнено: лог есть, БД нет) | ⬜ |
| 8 | Кластерный риск: `shadow` → `enforce` | `check_cluster_risk` вызывается (`risk_manager.py:867`), но в `shadow` всегда возвращает True. **Решение Алексея:** сначала замер, сколько входов это резало | ПОДТВЕРЖДЕНО мной (Петру не хватило фрагмента) | ⬜ |

### Фаза 11.2 — глубокий code audit (25.10, второй заход)

Второй разбор читал исходники (`risk_manager.py`, `position_sizing.py`, `api.py`, `state_db.py`, `rpc.py`),
15 претензий. Проверено по коду и по живому состоянию машины, прогнано через Петра:
**9 из 10 ПОДТВЕРЖДАЮ, 1 «нельзя проверить» — проверил сам, вышло сильнее, чем в аудите.**

#### Что чинить в первую очередь (деньги)

| # | Дефект | Доказательство | Пётр |
|---|--------|----------------|------|
| 11 | **Дневной circuit breaker не видит реализованный убыток** | `get_daily_pnl()` (`risk_manager.py:218`) берёт `metrics.json['pnl_total']`, а писателей этого ключа нет: `grep -rn pnl_total` → 4 чтения, 0 записей; в `metrics.json` ключ есть у 0 из 130 дней; блок `trades.jsonl` в той же функции — `pass`. Замер: сегодня 41 запись в `trades.jsonl`, Σ pnl = −$2.00, `get_daily_pnl()['realized_pnl']` = 0.0 | ПОДТВЕРЖДАЮ |
| 12 | **Повтор POST без идемпотентности** | `api.py:146-215`: цикл `for attempt in range(retries+1)` ретраит и `session.post` на `TimeoutException`/HTTP-ошибке; `orderLinkId` только в `dca.py:193`, `recycle.py:108` из 18 вызовов `/v5/order/create` | ПОДТВЕРЖДАЮ |
| 13 | **Проверка концентрации разрешает вход, когда проверить не может** | `risk_manager.py:114-124`: `if not wallet: return True, '', 0.0`; невалидный `equity` и `equity<=0` — тоже `True` | ПОДТВЕРЖДАЮ |
| 14 | **`suggest_qty` раздувает лот** | `position_sizing.py:264` `return max(qty, lot_step)`: при qty ниже шага возвращается шаг (маржа $1, 10x, цена $1000, шаг 0.1 → номинал $100 вместо $10). Прод-вызов один: `mcp_server.py:427` | ПОДТВЕРЖДАЮ |
| 15 | **Circuit breaker живёт только в памяти** | `risk_manager.py:600`: модульный глобал `_circuit_breaker_active`, персиста нет (`risk_state.json` отсутствует), при смене даты сбрасывается сам. Рестарт сервиса снимает блокировку | ПОДТВЕРЖДАЮ |

#### Что реально, но вторично

| # | Дефект | Уточнение | Пётр |
|---|--------|-----------|------|
| 16 | `atr_margin`: докстринг обещает `margin = risk_budget/(ATR*mult)`, фактически `margin = risk_per_trade` (`position_sizing.py:230`) | В прод-путь не попадает: `use_atr=True` нигде не вызывается, только MCP `place_entry`. Это обманка в документации, не потеря денег | ПОДТВЕРЖДАЮ |
| 17 | Сайзинг живёт на fallback-константах | `_ps_cfg()` → `{}` **на живой машине**: секции `position_sizing` в `config.yaml` нет вообще, так что `_FB_*` (0.20 / 5 позиций / $5 floor / 0.40 cap) — это не аварийный режим, а фактическая модель. Депозит $70.68 → budget $14.14 → на позицию $2.83 → упирается в floor $5 | Проверил сам (Пётр: «нельзя проверить») |
| 18 | `/rpc/dash/*` и `/dashboard` без Bearer | `rpc.py:545` PUBLIC_PATHS; `curl -sk https://127.0.0.1:8443/dash/rpc/dash/all` без токена отдаёт позиции с entry/mark/size/leverage/SL/upnl/liq. **Но снаружи закрыто:** с публичного IP `https://2.27.48.142:8443/` → connection refused (4444 отвечает 401). Риск — локальный/LAN, не интернет | ПОДТВЕРЖДАЮ (я уточнил периметр) |
| 19 | Одно sqlite-соединение `check_same_thread=False` без лока | `state_db.py:155`; `Lock(` в файле только `asyncio.Lock` (другой класс). Пишут и asyncio-цикл, и поток RPC-сервера (`HTTPServer`, `rpc.py:1981`) | ПОДТВЕРЖДАЮ |
| 20 | Дедуп `trade_history` по `external_id` не гарантирован | В `CREATE TABLE` — `UNIQUE`, но `_migrate()` добавляет колонку `ALTER TABLE ... ADD COLUMN external_id TEXT` без UNIQUE; в живой БД ограничения нет, фактически 1 дубль (2 строки), 8 строк с NULL | ПОДТВЕРЖДАЮ |

#### Статус на 25.10 (вечер)

| Задача | Итог |
|--------|------|
| 11 realized PnL | ✅ `a6d79cf` — `SUM(pnl)` из `trade_history` за сегодня; живой замер realized −1.66 при 41 закрытии |
| 12 orderLinkId | ✅ `a6d79cf` — генерится один раз на вызов в `api.bybit()`, покрывает все 25 точек отправки (инвентаризация: `max-order-sites.md`) |
| 13 fail-closed | ✅ `a6d79cf` + `fed63f5` — плюс найден корень: парсер баланса искал `coin=='USDT'` на уровне счёта, а монеты в `result.list[0]['coin']` → всегда `None`, из-за чего проверка молча разрешала всё |
| 15 персист CB | ✅ `a6d79cf` — `risk_state.json`, восстановление после рестарта |
| min-lot vs кап | ✅ `fed63f5` — при equity $62.8 кап 5% = $3.15 < `min_margin` $5; минимальный лот разрешён всегда, процент применяется к сумме сверх него |
| 5 (SHORT-корреляция) | 🟡 shadow-режим: пишется в лог, вход не блокируется — ждём замера |
| 3 (kill switch) | ✅ `aa9bea9` — cancel-all до закрытия, повторный fetch_positions+fetch_orders, эскалация STOP-алертом при failed>0 или остатках. Тесты `tests/test_kill_switch.py` (4). Живой замер до фикса: 22 открытых ордера (16 TP, 5 SL, 1 LIMIT_ENTRY LTCUSDT) — все бы пережили старый kill switch |
| 18 (дашборд), 14 (`suggest_qty`), 19 (лок sqlite), 20 (`external_id` UNIQUE) | ⬜ открыты |

Тесты: 343 passed → после #3 (kill switch) **143 passed** (добавлены `tests/test_kill_switch.py` — 4 проверки; `test_audit2_fixes.py` — 13 проверок).

#### Порядок работ (обновлён после второго аудита)

1. **11** — realized PnL из `trades.jsonl` (там `pnl` есть в каждой записи) или из `state.db`; убрать `pass`. Полчаса.
2. **13** — fail-closed в `check_symbol_concentration` + `continue` в `auto_entry` на исключении. Полчаса.
3. **12** — `orderLinkId` во всех точках входа (общий хелпер) + повтор POST только при наличии ключа. Полдня.
4. **15** — персист `_circuit_breaker_active` в `risk_state.json` (атомарная запись уже есть в `_save_risk_state`). Час.
5. **14**, **18**, **19**, **20** — по одной задаче, после P0.

#### P2 — продукт и передача

| # | Задача | Статус |
|---|--------|--------|
| 9 | Единый execution-контур (OrderIntent → RiskDecision → Executor → OrderRepository) — рефакторинг на недели, трогает live, только после P0 и прогона на paper | ⬜ |
| 10 | CI: добавить lint/типы к текущим py_compile + smoke + modules + logic + regression | ⬜ |
| 11 | Отчёт бэктестов: commit SHA, хеш конфига, версия данных, комиссии, equity curve | ⬜ |
| 12 | Лицензия: репозиторий под **AGPL-3.0** (`LICENSE`, `pyproject.toml`). Смена лицензии не отменяет права по опубликованным версиям — вопрос юридический, решать до коммерческой упаковки | ⬜ |

#### Снято с аудита (проверено, не берём)

- **«AI-агенту не давать прямой доступ к торговле»** — уже есть: write-эндпоинты под Bearer, аварийные
  (`/kill_switch`, `/emergency_close`) требуют **Bearer + X-Emergency-Auth**, `/enter` — `confirm:true`.
- **«`emergency_close_all` падает при `positions=None`»** — неверно: функция сама делает `fetch_positions()`
  и возвращает ошибку при сбое (`risk_manager.py:1046`).
- **Единый конфиг / реестр моделей / SBOM / onboarding / doctor** — продуктовые обёртки, в бэклог.
- **Новые ML-механики и стратегии** — аудит против, согласен.

## История фаз

| Фаза | Статус | Дата |
|------|--------|------|
| Фаза 1 — Базовая | ✅ | 07.06.2026 |
| Фаза 2 — SQLite + RPC | ✅ | 16.06.2026 |
| Фаза 3 — ML-скоринг | ✅ | 17.06.2026 |
| Фаза 4 — Алерты + Дашборд + MTF | ✅ | 18.06.2026 |
| Фаза 5 — DSPy + A/B + LSTM + RL | ✅ | 18.06.2026 |
| Фаза 6 — WebSocket full + Risk + Push | ✅ | 21.06.2026 |
| Фаза 7 — Graceful shutdown + Heavy cycle opt + Kelly | ✅ | 27.06.2026 |
| Фаза 7.1 — Стабилизация | ✅ | 28.06.2026 |
| Фаза 8.0 — Документация + Paper Trade + Дашборд | ✅ | 02.08.2026 |
| Фаза 8.1 — LSTM World Model + Anti-ludomania | ✅ | 04.08.2026 |
| Фаза 8.2 — Systemd Hardening | ✅ | 04.08.2026 |
| Фаза 9 — SHORT-оптимизация | ✅ | 08.08.2026 |
| Фаза 9.1 — Risk-фикс junk-шортов | ✅ | 15.08.2026 |
| Фаза 9.2 — FIFO-матчинг фикс | ✅ | 20.08.2026 |
| Фаза 9.3 — Time-exit + Blacklist + Candle-cache | ✅ | 27.08.2026 |
| Фаза 10 — Android приложение | 🟡 серверная часть в prod, APK не собран | — |
| Фаза 11 — внешний аудит, контур исполнения | ⬜ P0/P1 открыты | 25.10.2026 |
| Фаза 11.2 — глубокий code audit (9/10 подтверждено) | ⬜ P0 11–15 | 25.10.2026 |

## Принципы

1. **Сначала защита, потом атака** — лимиты и CB важнее стратегий
2. **Тесты перед деплоем** — 16 файлов / 171 тест (16 в test_smoke.py)
3. **Документация в актуальном состоянии** — docs/ всегда соответствует коду
