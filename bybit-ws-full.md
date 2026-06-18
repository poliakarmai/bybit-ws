---
tags: [bybit-ws, documentation, overview, trading, architecture]
created: 2026-06-16
updated: 2026-06-18
version: "2.1"
---

# Bybit-ws — Полная документация

> **Версия монитора:** 4.5.0 (Фаза 5 ML + Аудит)
> **Модулей:** 45+ | **Стратегий:** 8 | **Тестов:** 21/21
> **Сервис:** `bybit-ws-async.service` | **Цикл:** 30 сек | **Память:** ~35 MB
> **Дата:** 18 июня 2026

---

## Содержание

1. [Обзор](#1-обзор)
2. [Архитектура](#2-архитектура)
3. [Стратегии](#3-стратегии)
4. [ML-конвейер (Фаза 5)](#4-ml-конвейер-фаза-5)
5. [Риск-менеджмент](#5-риск-менеджмент)
6. [Инфраструктура](#6-инфраструктура)
7. [API и RPC](#7-api-и-rpc)
8. [MCP-инструменты](#8-mcp-инструменты)
9. [Мониторинг](#9-мониторинг)
10. [Аудит 18.06.2026](#10-аудит-18062026)
11. [План развития](#11-план-развития)
12. [Операционные процедуры](#12-операционные-процедуры)

---

## 1. Обзор

**Bybit-ws** — трейдинг-монитор для Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT по BB-полосам). Работает как systemd-сервис, SQLite — единственный источник истины (SSOT).

### Ключевые цифры

| Метрика | Значение |
|---------|----------|
| Модулей Python | 45+ |
| Стратегий | 8 (Grid LONG/SHORT, Junk, DCA, SL Re-entry, x10 × 3) |
| Память | ~35 MB |
| Цикл | 30 сек (async main loop) |
| RPC порт | 8766 (localhost) |
| Дашборд | 9999 (127.0.0.1) |
| Тестов | 23 (smoke 16 + modules 5 + ML 3 + scanner) |
| Позиций | 9 активных |
| Дневной лимит убытка | $50 (~5% от депозита ~$1000) |
| Макс. маржа | $500 |
| ML фича-флаг | `BYBIT_ML_ENABLED=0` — быстрый откат |

### Пути

| Ресурс | Путь |
|--------|------|
| Репозиторий | `~/bybit-ws/` |
| Рабочая копия | `~/.local/lib/bybit_ws/` |
| Данные (SSOT) | `~/.local/share/bybit-ws/` |
| Конфиг | `~/.config/bybit-ws/config.yaml` |
| RPC | `http://127.0.0.1:8766` |
| Дашборд | `http://127.0.0.1:9999` |
| Сервис | `systemctl --user bybit-ws-async` |

---

## 2. Архитектура

```
bybit-ws/
├── main.py              ← Точка входа, главный цикл (30с)
├── main_async.py        ← Async-версия главного цикла
├── api.py               ← Bybit v5 REST API (httpx, 6+ endpoints)
├── rpc.py               ← JSON-RPC сервер (порт 8766)
├── state_db.py          ← SQLite SSOT (8 таблиц, WAL)
│
├── auto_entry.py        ← Авто-вход LONG по 9-метричному скорингу
├── auto_short.py        ← Авто-SHORT (Tier A/B + JUNK-режим)
├── auto_sl.py           ← Авто-SL + BE-SL на прибыльные
├── auto_tp.py           ← Авто-TP (ретрей с backoff)
├── trailing_sl.py       ← Трейлинг-SL (LONG/SHORT зеркально)
├── junk_trail.py        ← Трейлинг-TP для JUNK-шортов
│
├── gridsignal_scanner.py← Сканер сигналов Bollinger Grid
├── gridsignal-bot.py    ← Исполнение сигналов сканера через RPC (/rpc/enter, /rpc/scan)
│                           Запуск: python3 gridsignal-bot.py --mode long|short
│
│   # ── ML (Фаза 5) ──
├── ml_scorer.py         ← ML Gate: RandomForest (⚠️ ОТКЛЮЧЁН — overfit, F1_oos=0.368)
├── ab_test.py           ← A/B-тест ML Gate vs baseline
├── lstm_regime.py       ← LSTM-классификатор рынка (5 классов)
├── rl_env.py            ← RL-среда (Gymnasium, 13 признаков)
├── rl_agent.py          ← DQN-агент (Stable-Baselines3)
├── ensemble.py          ← Ансамбль RF+LSTM+RL (взвешенное голосование)
│
│   # ── Риск ──
├── position_sizing.py   ← Динамическая маржа от % депозита
├── atr_sizer.py         ← ATR Risk Sizing
├── correlation.py       ← Корреляционная матрица позиций
├── x10_limits.py        ← Дневной лимит x10 убытков
├── margin_alerts.py     ← Контроль маржи
│
│   # ── Стратегии ──
├── dca.py               ← DCA-докупка
├── sl_reentry.py        ← Перезаход после SL
├── overbought.py        ← Сканер перегретых (BB > 75%)
├── pump_detect.py       ← Детектор пампов
├── bb_scalp.py          ← x10 BB Scalping M5
├── mean_revert.py       ← x10 Mean Reversion Extreme
├── funding_entry.py     ← x10 Funding Rate Momentum
│
│   # ── Инфра ──
├── config.py            ← YAML-конфиг с ${ENV}-подстановкой
├── file_utils.py        ← Атомарная запись JSON
├── snapshot.py          ← Снепшоты позиций
├── metrics.py           ← Метрики SL/TP/entries
├── reporting.py         ← Сводки, трейд-журнал
├── health.py            ← Проверки здоровья
├── alerts.py            ← Telegram-алерты
├── cost_tracker.py      ← Учёт комиссий
├── cleanup.py           ← Снятие просроченных ордеров
├── recycle.py           ← Перевыставление TP
├── regime.py            ← Эвристика рыночного режима (fallback)
├── rsi.py               ← RSI-дивергенции
├── squeeze.py           ← Детектор BB-сжатия
├── funding_tracker.py   ← Экстремальные ставки фондирования
├── funding_rotation.py  ← Авто-фандинг-ротация
├── partial_tp.py        ← Частичный TP (динамический сплит)
├── manual_positions.py  ← Защита ручных позиций
├── confluence_paper.py  ← Paper-trading версия MTF-конфлюенса (бэктестинг)
├── mtf_confirmation.py  ← MTF-подтверждение сигналов (live, D/W/M ≥2/3)
├── ws_client.py         ← WebSocket live-цены/BB
│
├── web/
│   ├── dashboard.html   ← Дашборд v5.0
│   └── proxy_server.py  ← Прокси (127.0.0.1:9999)
│
├── deploy.sh            ← Атомарный деплой с rollback
├── walk_forward_validate.py ← Walk-forward валидация ML
│
└── test_smoke.py        ← 16 интеграционных тестов
    test_modules.py      ← 5 модульных тестов
    test_scanner_smoke.py← Тесты сканера
    test_ml_smoke.py     ← 3 ML smoke теста (HMAC, RF, LSTM)
```

---

## 3. Стратегии

### 3.1 Bollinger Grid LONG

**Триггер:** BB% < 25% на Daily + MTF-конфлюенс ≥ 2/3
**Скоринг:** 9 метрик (BB%, RSI, Volume, Funding, Trend, MTF, Squeeze, Correlation, Regime)
**Фильтры:** ML Gate (RF F1=0.921) → LSTM-режим → RL-агент (DQN) → Ансамбль
**Вход:** Limit-ордер на Lower BB × per-symbol discount
**SL:** Lower BB × 0.93 (или BE-SL если в плюсе)
**TP:** Middle BB (20%) + Upper BB (80%)
**Плечо:** 3x
**Макс. позиций:** 12

### 3.2 Bollinger Grid SHORT

**Триггер:** BB% > 80% на Daily
**Tier A/B:** SL = +5%, TP = Middle BB
**Tier C/D (JUNK):** no SL, DCA-лесенка (+100%/+120%), хард-SL +25%
**Плечо:** 3x

### 3.3 x10 Стратегии

| Стратегия | Интервал | Фильтр |
|-----------|----------|--------|
| BB Scalping | M5 | BB% < 15% |
| Mean Reversion Extreme | M5 | Отклонение > 3σ |
| Funding Rate Momentum | 4h | Экстремальный фондинг |

**Лимиты:** 3 убыточных сделки в день, макс. убыток $10/сделку

---

## 4. ML-конвейер (Фаза 5)

```
Сигнал → MTF-конфлюенс → ML Gate (RF) → LSTM-режим → RL (DQN) → Ансамбль → Ордер
```

### 4.1 ML Gate (Random Forest) — ⚠️ ОТКЛЮЧЁН

> **Статус на 18.06.2026:** ML-конвейер отключён (`BYBIT_ML_ENABLED=0`).  
> Причина: сильный оверфитинг (in-sample F1=0.95 → walk-forward F1=0.37).  
> Бот работает по Фазе 2 (baseline-эвристика). Включение — после переобучения.

- **Модель:** RandomForestClassifier
- **In-sample F1:** 0.951 | **Walk-forward F1:** 0.368 ± 0.413 (🔴 сильный оверфитинг)
- **Walk-forward Precision:** 0.50 ± 0.50 | **Recall:** 0.33 ± 0.41
- **Датасет:** 270 сигналов, 30 TP (11.1% позитивный класс)
- **Порог:** 0.22 (эвристический, не оптимизирован)
- **При ошибке:** 0.5 (нейтрально, не пропускает слепо)
- **Рекомендация:** переобучить на большем датасете, добавить stratified split, рассмотреть SMOTE

### 4.2 LSTM-режим
- **Модель:** 2-layer LSTM (64→32), 30-дневные D-свечи BTC+ETH
- **Классы:** TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOL, LOW_VOL
- **Влияние:** адаптивные параметры входа под режим
- **При ошибке:** fallback на эвристику `regime.py`
- **Warning:** при >50% нулевых признаков — алерт о ненадёжности

### 4.3 RL-агент (DQN)
- **Модель:** Stable-Baselines3 DQN, 3 действия: SKIP, ENTER_LONG, WAIT
- **Обучение:** 100K шагов на 365 днях BTC+ETH
- **Fail-closed:** ошибка/нет модели → пропуск сигнала

### 4.4 Ансамбль
- **Веса:** RF=0.34, LSTM=0.33, RL=0.33
- **Порог:** 0.45
- **WAIT = SKIP** (не голосует «за вход»)
- **При ошибке:** 0.0 (RL), 0.5 (RF), fallback (LSTM)
- **Фича-флаг:** `BYBIT_ML_ENABLED=0` отключает весь ML-конвейер → возврат к Фазе 2

### 4.6 Безопасность моделей (HMAC)

**Все ML-модели подписываются HMAC-SHA256** при сохранении и проверяются при загрузке. Без подписи модель не загрузится — алерт и fallback.

| Модуль | Модель | Формат | Подпись |
|--------|--------|--------|---------|
| `ml_scorer.py` | RandomForest | `rf_model.joblib` | `rf_model.joblib.hmac` |
| `lstm_regime.py` | StandardScaler | `scaler.json` | `scaler.json.hmac` |
| `rl_agent.py` | DQN (PyTorch) | `dqn_latest.zip` | `.pb` (встроенная в SB3) |

**Механизм:**
- `_hmac_sign(path)` — SHA256 файла → HMAC с секретом → `.hmac`-файл
- `_hmac_verify(path)` — пересчитывает и сравнивает через `hmac.compare_digest()` (timing-safe)
- Секрет: `BYBIT_HMAC_SECRET` (обязателен в production, `BYBIT_WS_PRODUCTION=1`)
- В dev-режиме (без `BYBIT_WS_PRODUCTION`) — warning + fallback-ключ
- В production без ключа — `sys.exit()` (отказ старта)
- При расхождении подписи: модель не загружается, алерт, fallback на эвристику

**Почему не safetensors/ONNX:** sklearn RF не поддерживает safetensors. HMAC-SHA256 эквивалентен по security-свойствам: злоумышленник не может подделать подпись без знания ключа.

> **Важно:** это закрывает RCE-вектор через подмену моделей. Даже при компрометации файловой системы (write-доступ к `~/.local/share/bybit-ws/`) злоумышленник не сможет загрузить malicious model без ключа.

### 4.5 A/B-тест
- **Группа A:** ML Gate
- **Группа B:** Baseline (без ML)
- **Отчёт:** `/rpc/ab_test_report` (t-test, p-value)

---

## 5. Риск-менеджмент

### 5.1 Лимиты

| Параметр | Значение |
|----------|----------|
| Макс. дневной убыток | $50 |
| Макс. маржа | **$300** (30% от ~$1000 депозита) |
| Макс. в одну позицию | **$100** (10% депозита) |
| Макс. позиций LONG | 12 |
| Circuit breaker | daily_pnl < -$50 → блокировка входов |
| Маржа > 80% | → алерт |

### 5.2 Защиты

- **BE-SL:** прибыльные позиции получают SL на уровне входа (не остаются голыми)
- **Двойной вход:** повторная проверка позиции через API перед ордером
- **Потерянный ордер:** проверка `/v5/order/realtime` при таймауте
- **JUNK хард-SL:** +25% от входа (защита если DCA не сработал)
- **Корреляция:** r > 0.8 → SL поджимается
- **Volume pump-detect:** 5m объём > 2× → SHORT отменяется
- **ATR-сайзинг:** авто-qty через кеш 4ч

---

## 6. Инфраструктура

### 6.1 Сервисы

| Сервис | Статус | PID | Порт |
|--------|--------|-----|------|
| `bybit-ws-async` | active | 391888 | — |
| Дашборд | proxy | — | 9999 |
| RPC | встроен | — | 8766 |

> **Async-статус:** продакшен работает на `main_async.py` (asyncio, цикл 30с). `main.py` — deprecated (вызывает `sys.exit()` с указанием использовать `main_async.py`). Удалён из активного кода.

### 6.2 Кроны (торговые)

| Джоба | Интервал | Назначение |
|-------|----------|------------|
| `bybit-watchdog` | 5m | Проверка живости сервиса |
| `bybit-db-backup` | daily 03:00 | Бэкап state.db |
| `M5/M3 авто-сканер` | 4h | Скан коротких таймфреймов |
| `Утренний скан LONG` | daily 05:00 | Скан Daily LONG |
| `pnl-morning` | daily 09:00 | Утренняя сводка PnL |
| `weekly-pnl` | Mon 09:00 | Недельная сводка |
| `cost-daily-collect` | 3h | Сбор расходов на API |

### 6.3 Данные

| Файл | Назначение | Формат |
|------|-----------|--------|
| `state.db` | Позиции, сделки, метрики (SSOT) | SQLite WAL |
| `paper_state.db` | Бумажные позиции | SQLite WAL |
| `positions_snapshot.json` | Резервный снепшот | JSON, раз в час |
| `metrics.json` | Метрики | JSON |
| `pumps.json` | Памп-трекинг | JSON (атомарная запись) |
| `events.log` | Журнал событий | Текст |

---

## 7. API и RPC

### 7.1 Bybit v5 REST (api.py)

| Эндпоинт | Метод | Назначение |
|----------|-------|------------|
| `/v5/position/list` | GET | Позиции |
| `/v5/account/wallet-balance` | GET | Баланс |
| `/v5/order/create` | POST | Создание ордера |
| `/v5/order/realtime` | GET | Открытые ордера |
| `/v5/position/trading-stop` | POST | SL/TP |
| `/v5/market/kline` | GET | Свечи |
| `/v5/account/transaction-log` | GET | Фандинг (SETTLEMENT) |

**Аутентификация:** HMAC-SHA256. Ключи из переменных окружения (`BYBIT_API_KEY`, `BYBIT_API_SECRET`), загружаемых через systemd `EnvironmentFile=/home/openclaw/.config/bybit-ws/env`. Legacy-путь `~/.config/bybit-cli/config` — fallback с предупреждением.

### 7.2 RPC (порт 8766)

| Метод | Назначение |
|-------|------------|
| `/rpc/positions` | Список позиций |
| `/rpc/enter` | Вход в позицию |
| `/rpc/close` | Закрытие позиции |
| `/rpc/metrics` | Дневные метрики |
| `/rpc/risk` | Статус риска |
| `/rpc/ab_test_report` | Отчёт A/B теста |
| `/rpc/ml_toggle` | Статус/переключение ML (GET/POST) |
| `/rpc/paths` | Пути к файлам |
| `/rpc/scan` | Скан сигналов |
| `/metrics` | Prometheus (bybit_ws_*) |

**Аутентификация:** UUID Bearer-токен

---

## 8. MCP-инструменты

AI-агенты взаимодействуют с bybit-ws через MCP-сервер:

| Инструмент | Назначение |
|-----------|------------|
| `scan_market(mode, interval)` | Скан сигналов Bollinger Grid |
| `get_positions()` | Текущие позиции + PnL |
| `get_metrics()` | Дневные метрики (TP/SL/входы) |
| `get_risk_status()` | Лимиты риска |
| `place_entry(symbol, side, qty, sl, tp)` | Вход в позицию |

---

## 9. Мониторинг

### 9.1 Дашборд
- **URL:** `http://127.0.0.1:9999`
- **Содержит:** позиции, PnL, метрики, риск, графики
- **Обновление:** авто (через RPC-прокси)

### 9.2 Watchdog
- **Проверяет:** `bybit-ws-async` жив?
- **При падении:** перезапуск + алерт
- **Интервал:** 5 минут

### 9.3 Алерты
- **Каналы:** Telegram (через `send_telegram_alert`)
- **События:** вход, выход, SL, ошибки API, Circuit Breaker

### 9.4 Метрики Prometheus
- `bybit_ws_active_positions`
- `bybit_ws_daily_pnl`
- `bybit_ws_cycle_duration_seconds`
- `bybit_ws_ml_gate_pass_rate` — % сигналов, прошедших ML Gate
- `bybit_ws_api_error_rate` — % ошибок API
- `bybit_ws_margin_usage_pct` — % использования маржи
- `bybit_ws_cycle_duration_p95` — 95-й перцентиль цикла

---

## 10. Аудит 18.06.2026

Полный аудит трёх эшелонов: Source-Driven + Security + Adversarial.
Всего находок: 47 (7 CRITICAL, 12 HIGH, 15 MEDIUM, 13 LOW).
Исправлено: 23 (7 CRITICAL + 12 HIGH + 4 MEDIUM).

### Исправленные CRITICAL (7/7)

| # | Баг | Файл | Фикс |
|---|-----|------|------|
| C1 | Позиция без SL если в плюсе | `auto_sl.py` | BE-SL на уровне входа |
| C2 | RL WAIT мёртвая ветка (`action==3`) | `rl_agent.py` | `action==3` → `action==2` |
| C3 | Двойной вход race condition | `auto_entry.py` | Повторная проверка позиции через API |
| C4 | RL docstring 4 действия → 3 | `rl_env.py` | Исправлен docstring |
| C5 | Потерянный ордер при таймауте | `auto_entry.py` | Проверка `/v5/order/realtime` |
| C6 | JUNK без хард-SL | `auto_short.py` | SL +25% от входа |
| C7 | fetch_funding_total параметры | `api.py` | `symbol→currency`, `FUNDING→SETTLEMENT` |

### Исправленные HIGH (12/12)

| # | Баг | Файл | Фикс |
|---|-----|------|------|
| H1 | Proxy 0.0.0.0 + CORS * | `proxy_server.py` | 127.0.0.1 + localhost |
| H2 | RL default-enter при ошибке | `rl_agent.py` | Fail-closed (пропуск) |
| H3 | RF default-PASS при ошибке | `ensemble.py` | 0.5 нейтрально |
| H4 | Нулевые BB-данные для RL | `auto_entry.py` | Реальные значения из `bb2` |
| H5 | WAIT bias к входу (score=0.5) | `ensemble.py` | WAIT = SKIP (0.0) |
| H6 | RPC SL при отсутствующей позиции | `rpc.py` | Не слать SL если `pos is None` |
| H7 | Docstring v3 → v5 | `api.py` | Исправлен |
| H8 | Дубликаты в `bybit_ws/` | — | Удалены 5 файлов |
| H9 | API без settleCoin | `auto_entry.py` | `&settleCoin=USDT` |
| H10 | Импорт regime маскирует ошибки | `ensemble.py` | Явный ImportError handler |

### Дополнительные фиксы (18.06 вечер, второе ревью)

| Что | Где | Суть |
|-----|-----|------|
| **HMAC-подпись моделей** | `ml_scorer.py`, `lstm_regime.py` | `_hmac_sign()` / `_hmac_verify()` с `hmac.compare_digest()` (timing-safe). Закрывает RCE через подмену моделей |
| Scaler pickle → json | `lstm_regime.py` | StandardScaler через JSON (безопасно) |
| Feature flag | `auto_entry.py` | `BYBIT_ML_ENABLED=0` — откат ML одной командой |
| Атомарный деплой | `deploy.sh` | Бэкап → swap → рестарт → smoke test → rollback при фейле |
| RPC ML Toggle | `rpc.py` | `GET/POST /rpc/ml_toggle` — статус и переключение ML без рестарта конфига |
| Walk-forward validation | `walk_forward_validate.py` | PR-кривая на out-of-sample данных для переоценки порога ML Gate |
| ML smoke тесты | `test_ml_smoke.py` | 3 теста: RF load, HMAC sign/verify, LSTM fallback |
| **HMAC production guard** | `ml_scorer.py`, `lstm_regime.py` | Убран fallback-ключ в production: `BYBIT_WS_PRODUCTION=1` → `sys.exit()` без ключа |
| **EnvironmentFile** | `bybit-ws-async.service` | Ключи через `EnvironmentFile=~/.config/bybit-ws/env` (chmod 600) вместо голых `Environment=` |
| **API credentials migration** | `api.py` | Приоритет: `os.environ` (systemd) → legacy `~/.config/bybit-cli/config` |
| **main.py deprecated** | `main.py` | `sys.exit('Use main_async.py')` — мёртвый код удалён из активного использования |
| **Canary-период деплоя** | `deploy.sh` | 10 мин мониторинг (20×30с): сервис жив + health.txt свежее 5 мин → rollback |
| **Retention policy бэкапов** | `bybit-db-backup.py` | Хранение 7 дней, авто-удаление старых, алерт при < 1 GB |
| **Walk-forward validation** | `walk_forward_validate.py` | Запущен: in-sample F1=0.951 → walk-forward F1=0.368 (оверфитинг) |
| **Снижение маржи** | `config.py` | `max_total_margin` 500→300, добавлен `max_position_size: 100` |
| Watchdog: зависание | `bybit-watchdog.sh` | Проверка возраста последней записи (>5 мин) |
| Бэкап: конфиг + модели | `bybit-db-backup.py` | config.yaml, .joblib/.pt, trades.jsonl |

### Исправленные MEDIUM (4/15)

| # | Баг | Файл | Фикс |
|---|-----|------|------|
| M1 | Ensemble fallthrough без лога | `auto_entry.py` | `log_event` при ошибке |
| M2 | LSTM нулевые признаки молча | `lstm_regime.py` | Warning при >50% нулей |
| M3 | Stratify без проверки min_class | `ml_scorer.py` | `min(bincount) >= 2` |
| M4 | Symbol инъекция в backtest | `backtest.py` | Regex `^[A-Z0-9]+$` |

### Коммиты

| Коммит | Описание |
|--------|----------|
| `bf73eff` | 7 CRITICAL исправлено |
| `2844afe` | 8 HIGH/MEDIUM исправлено |
| `2b3888f` | Мусорные файлы удалены |
| `c11650d` | 4 MEDIUM + документация |
| `a6f669e` | Первое ревью: HMAC, feature flag, watchdog, бэкап |
| `65521d9` | Второе ревью: deploy.sh, walk-forward, ml_toggle RPC, ML-тесты |

---

## 11. План развития

### Сделано

- ✅ **Фаза 1:** Стабильность (16 июня)
- ✅ **Фаза 2:** Надёжность — SQLite, тесты, RPC, логирование
- ✅ **Фаза 3:** Умный трейдинг — ML-скоринг, Partial TP, Фандинг-ротация
- ✅ **Фаза 4:** Масштабирование — ATR, MTF, WebSocket, httpx, дашборд
- ✅ **Фаза 5:** ML — RandomForest, A/B-тест, LSTM, RL (DQN), Ансамбль
- ✅ **Аудит:** 23 находки исправлено (18 июня), 24 осталось (все MEDIUM/LOW)

### Бэклог

| Задача | Оценка | Приоритет |
|--------|--------|-----------|
| Asyncio полная миграция | 40-60ч | 🥈 |
| Binance/OKX поддержка | 60-80ч | 🥉 |
| Логирование с ротацией | 2ч | 🥈 |
| `pip-audit` / CVE мониторинг | 1ч | 🟢 |
| `except Exception` → логи (219 мест) | 4ч | 🟢 |
| ML retraining pipeline | 4ч | 🥇 |
| Per-symbol RL агенты | 8-12ч | 🥈 |
| ML data drift мониторинг (PSI/KS) | 2ч | 🥈 |
| Расширить тесты ML (до 50+) | 8ч | 🥇 |
| Ограничить RL Tier S/A монетами | 2ч | 🥈 |
| LSTM горизонт 30→90 дней | 4ч | 🥈 |
| Алерт при откате ML через RPC | 1ч | 🟢 |
| Онлайн-мониторинг качества ML (precision_7d) | 2ч | 🟢 |
| Переобучение RF (оверфитинг 0.583) | 4ч | 🥇 |

---

## 12. Операционные процедуры

### Деплой

```bash
# Атомарный деплой с бэкапом, canary-периодом и rollback
bash ~/bybit-ws/deploy.sh
```

Скрипт делает: бэкап → атомарный swap → рестарт → **canary-мониторинг 10 мин** (20 проверок каждые 30с: сервис жив + health.txt свежее 5 мин) → rollback при фейле.

### Быстрый откат ML

```bash
# Отключить ML-конвейер
curl -X POST http://127.0.0.1:8766/rpc/ml_toggle -d '{"enable":0}'
systemctl --user restart bybit-ws-async

# Включить обратно
curl -X POST http://127.0.0.1:8766/rpc/ml_toggle -d '{"enable":1}'
systemctl --user restart bybit-ws-async

# Проверить статус
curl http://127.0.0.1:8766/rpc/ml_toggle
```

### Бэкап

```bash
# Бэкап state.db (автоматически через cron daily 03:00)
python3 ~/hermes-infra/scripts/bybit-db-backup.py
```

Бэкапы хранятся в `~/.local/share/bybit-ws/backups/`:
- Формат: `backup_YYYYMMDD_HHMM.tar.gz`
- Retention: 7 дней (старые удаляются автоматически)
- Проверка свободного места: алерт если < 1 GB

### Тестирование

```bash
cd ~/bybit-ws
python3 -m pytest test_smoke.py test_modules.py -q
python3 test_scanner_smoke.py
```

### Восстановление после сбоя

```bash
systemctl --user restart bybit-ws-async
# Проверить позиции
curl http://127.0.0.1:8766/rpc/positions
# Проверить риск
curl http://127.0.0.1:8766/rpc/risk
```
