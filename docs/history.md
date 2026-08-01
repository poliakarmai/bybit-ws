# История разработки bybit-ws

> Вынесено из AGENTS.md для соблюдения лимита 80-200 строк.

## Фаза 1–2 (стабильность + надёжность) ✅

- SQLite миграция (SSOT)
- SHORT-трейлинг (зеркальный LONG)
- Защита SL от перезатирания
- Авто-безубыток (+10%)
- Paper Trading API
- Prometheus /metrics
- main_loop разбит на 3 функции

## Фаза 3 (умный трейдинг) ✅

- ML-скоринг сигналов — RandomForest F1=0.69 → F1=0.921
- Trailing Stop для x10
- Partial TP — динамический сплит 20/80→50/50
- Авто-фандинг-ротация

## Фаза 4 (масштабирование) ✅

- ATR-based риск-сайзинг
- Multi-timeframe конфлюенс (D/W/M, ≥2/3)
- Telegram-алерты
- WebSocket live-цены/BB
- httpx (подготовка к asyncio)
- Дашборд v5.0 (127.0.0.1:9999)

## Фаза 5 (ML) ✅

- RandomForest ML Gate
- LSTM-классификатор рыночного режима (5 классов)
- RL-агент (DQN, Stable-Baselines3)
- Ансамбль RF+LSTM+RL (веса 0.34/0.33/0.33)
- A/B-тест ML Gate vs baseline
- HMAC-подпись всех моделей
- Feature flag `BYBIT_ML_ENABLED=0`

## Аудит 18.06.2026

Полный аудит трёх эшелонов (Source-Driven + Security + Adversarial).  
Исправлено: 23 находки (7 CRITICAL + 12 HIGH + 4 MEDIUM). 24 MEDIUM/LOW осталось.  
HMAC-подпись моделей закрывает RCE-вектор.  
Атомарный деплой с rollback.  
RPC `/rpc/ml_toggle` — быстрый откат ML.  
Watchdog с проверкой зависания цикла.
Walk-forward валидация ML.

## Фаза 6 (WebSocket-интеграция) 🔄

**19.06.2026** — WebSocket BB-кеш для всех стратегий:

- `ws_client.py`: добавлена подписка `kline.W` (недельные свечи для trailing_sl)
- `ws_client.py`: алиасы ключей `bb_pos`/`cur`/`bb_width` для REST-совместимости
- `ws_client.py`: `is_stale(max_age_sec)` — защита от устаревшего кеша (>300с → REST fallback)
- `ws_client.py`: `batch_size` 10→5, подписки разбиты на tickers+kline.D и kline.W (лимит 10 args Bybit v5)
- `trailing_sl.py`, `auto_sl.py`, `auto_entry.py`, `auto_short.py`: `_get_bb_ws()` — сначала WS-кеш, fallback на REST
- Feature flag: `BYBIT_WS_BB_ENABLED` (env, default 1)
- Аудит 3 эшелона: source-driven (3C→fixed), security (PASS), adversarial (2C→fixed)
- Коммиты: `8acad92`, `f98b55e`

## 2026-06-21 — Фаза 6.8: Dry Spell Throttle + Push-уведомления

### Dry Spell Throttle (auto_short.py)
- Добавлен throttle для SHORT-символов: 3+ холостых цикла → пропуск на 30 мин
- Константы: DRY_SPELL_THRESHOLD=3, DRY_SPELL_COOLDOWN=1800
- Экономит ~80% холостых BB-запросов на «мёртвых» символах

### Push-уведомления (push_notifier.py + main_async.py)
- Модуль push_notifier.py: ntfy (primary) + Telegram fallback
- Подключено в main_async.py: CRITICAL (STOP/pump) + HIGH (ENTRY/TP)
- Топик: bybit-alerts-335c1721
- Без дублирования Telegram-алертов (telegram_fallback=False)

## 2026-06-29 — Фаза 7 завершена: ATR-TP, SL 2% floor, Dead Code Audit, Deploy Simplify

### ATR-based TP (auto_tp.py)
- `_get_atr_value(sym)` — расчёт ATR(14) через REST API
- TP = entry ± k × ATR, 3 уровня: 1.0× (40%), 2.0× (35%), 3.0× (25%)
- Feature flag: `BYBIT_ATR_TP_ENABLED=1`

### SL 2% Floor (auto_sl.py)
- SL не ближе 2% от входа — защита от выбивания шумом
- Аварийный SL при ATR/BB SL выше рынка: mark × 0.95

### Dead Code Audit
- Удалён `execute_rotation` (мёртвый импорт)
- `for idx in (0,1)` заменён на `POSITION_IDX` авто-определение
- GSC GS008 детектор мёртвого кода

### Deploy Simplification
- Убрано `.local/lib` staging, явное указание пакетов
- `deploy.sh` упрощён: копирование → тесты → рестарт
- bb_scalp + funding_rotation активированы
- risk_check gate для mean_revert и funding_entry

## Фаза 8: One-Click Trading + LSTM Fix (01.08.2026)

### One-Click Trading Infrastructure
- RPC: `POST /calc_qty` — расчёт размера позиции по % риска (balance × risk% × leverage / entry)
- RPC: `GET /balance` — USDT баланс (walletBalance, available, equity)
- Воркфлоу: «просканируй» → «бери X%» → `/calc_qty` → `/enter` → unified_sl подхватывает
- Изоляция: one-click позиции = обычные, не manual, авто-стратегии не ломаются

### SL Throttle Fix
- Throttle (120с) теперь для ВСЕХ типов SL, включая tight_trail
- `not modified` от Bybit больше не treated as error

### LSTM Regime Classifier
- Переобучен: 33.1% → 82.3% точность (100 эпох)
- Починен `--predict` (import json в функции)
- Текущий режим: RANGING (92% confidence)

### Self-Learn
- Интервал: 720 циклов (каждые 6 часов) — уменьшено с 2880

### Gateway Stability
- deprovision.py: `restart` → `reload` (больше не роняет gateway каждый час)

## Фаза 9 (начало 01.08.2026)

### v8.1 — Журнал + LSTM-фильтр
- MEAN-REVERT отключён (сливал на BB%=0-5%)
- trade_history: контекст входа (BB%, RSI, MTF, regime), DCA/partial TP счётчики, exit_reason
- LSTM: RANGING и CHOPPY блокируют входы, BYBIT_REGIME_AUTO=1

### v9 — Адаптивный TP/SL по LSTM-режиму
- TP: RANGING→ближе (0.7×/1.2×/1.8×), TRENDING→дальше (1.5×/2.5×/3.5×)
- SL: RANGING→×0.7 (быстрее режем), TRENDING→×1.2 (даём пространство)
- MTF-фикс: блокировать входы без данных D-TF (дыра закрыта)
