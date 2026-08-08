# ROADMAP — bybit-ws

> План развития. Версия: 8.2. Обновлено: 2026-08-04.
> Актуальный roadmap также в Obsidian: `hermes/bybit-ws-roadmap.md`.

## Текущий статус (v8.2)

| Компонент | Статус |
|-----------|--------|
| Bollinger Grid LONG/SHORT | ✅ prod |
| ATR-adaptive SL + ATR-based TP (3 уровня) | ✅ prod |
| 7 фильтров входа (MTF, Orderbook, Volume, Entry Judge, Correlation, Post-trade, Risk) | ✅ prod |
| LSTM Market Regime (82.3% точность, авто LONG/SHORT) | ✅ prod |
| LSTM World Model (multi-task OHLCV, entry scoring) | ✅ prod |
| REGIME_AUTO: блокировка входов по режиму рынка | ✅ prod |
| Адаптивный TP/SL по LSTM-режиму (v9) | ✅ prod |
| Self-learning (FIFO + bias + post-trade, каждые 6ч) | ✅ prod |
| Anti-ludomania: 3 убытка/час → блок 30 мин | ✅ prod |
| BlackSwan v2: корреляционный алерт (без авто-закрытия) | ✅ prod |
| Paper Trading (исторический бэктест) | ✅ prod |
| Веб-дашборд (SVG + HTML/Chart.js) | ✅ prod |
| MCP-инструменты (6 шт.) | ✅ prod |
| RPC REST API (15 эндпоинтов) | ✅ prod |
| One-click trading через Hermes-чат | ✅ prod |
| Алерты (ntfy + Telegram) | ✅ prod |
| Тесты (52/52 интеграционных) | ✅ prod |

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

### Фаза 9 — SHORT-оптимизация 🟢 (08.08.2026)

| Задача | Приоритет | Статус |
|--------|-----------|--------|
| SHORT в TRENDING_DOWN: калибровка параметров | 🔴 | ✅ max_loss 15→10%, max_hold 48→24ч |
| Time-based SL: закрытие убыточных >12ч | 🔴 | ✅ check_short_time_sl() в основном цикле |
| Исключение imported-сделок из self-learn | 🔴 | ✅ WHERE strategy != 'imported' |
| Винрейт SHORT: анализ + улучшение | 🔴 | ✅ анализ: без STGUSDT +$146 |
| World Model качество > 33% | 🟡 | ✅ 22.3% → 33.1% (400 эпох, λ=0.01) |

### Фаза 10 — Android приложение ⬜

| Задача | Статус |
|--------|--------|
| **Стек:** Kotlin (нативный, только Android) | 📋 |
| Дашборд, алерты, управление SL/TP, скан SHORT | ⬜ |
| **Security (обязательно перед стартом):** | |
| — nginx/HTTPS перед RPC :8766 | ⬜ |
| — JWT-аутентификация | ⬜ |
| — Rate limiting | ⬜ |
| Global Kill-Switch из мобильного приложения | ⬜ |
| Спецификация: `docs/android/SPEC.md` | ✅ |

### Долгий срок

| Задача | Приоритет |
|--------|-----------|
| Multi-exchange (Binance, OKX) | 🟡 |
| — ExchangeAdapter — абстракция бирж | 📋 (pre-req) |
| DQN → PPO | 🟢 |
| — Feature Store / Data Pipeline для RL | 📋 (pre-req) |
| Grafana HTTPS (нужен домен) | 🟢 |

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
| Фаза 9 — SHORT-оптимизация | 🟢 | 08.08.2026 |
| Фаза 10 — Android приложение | ⬜ | — |

## Принципы

1. **Сначала защита, потом атака** — лимиты и CB важнее стратегий
2. **Тесты перед деплоем** — 52/52 smoke-тестов
3. **Документация в актуальном состоянии** — docs/ всегда соответствует коду
