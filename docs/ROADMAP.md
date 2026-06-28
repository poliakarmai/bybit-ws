# ROADMAP — bybit-ws

> План развития. Версия: 7.1. Обновлено: 2026-06-28.
> Актуальный roadmap также в Obsidian: `hermes/bybit-ws-roadmap.md`.

## Текущий статус (v7.1)

| Компонент | Статус |
|-----------|--------|
| Bollinger Grid LONG/SHORT | ✅ prod |
| ATR-adaptive SL + ATR-based TP (3 уровня) | ✅ prod |
| 7 фильтров входа (MTF, Orderbook, Volume, Entry Judge, Correlation, Post-trade, Risk) | ✅ prod |
| ML Gate (RF + DSPy) | ✅ prod |
| Self-learning (FIFO + bias + post-trade) | ✅ prod |
| Сессионная адаптация (NY/Asia/Weekend) | ✅ prod |
| Grafana + Prometheus | ✅ prod |
| MCP-инструменты (6 шт.) | ✅ prod |
| RPC REST API (15 эндпоинтов) | ✅ prod |
| Алерты (ntfy + Telegram) | ✅ prod |
| Push-уведомления | ✅ prod |
| Тесты (45/45 интеграционных) | ✅ prod |

## Дорожная карта

### Фаза 7.1 — Стабилизация (28.06.2026) ✅

| Задача | Статус |
|--------|--------|
| auto_tp: orders.values() list/dict fix | ✅ |
| ab_status: NoneType check | ✅ |
| Self-learning в main loop | ✅ |
| pump_state авто-очистка | ✅ |
| gridsignal-bot: ALTER TABLE fix | ✅ |
| DSPy → DeepSeek | ✅ |
| Документация (ARCHITECTURE, CAPABILITIES, API, ROADMAP → v7.1) | ✅ |

### Фаза 7.2 — Безопасность + Данные (приоритет 🔴)

| Задача | Приоритет | Статус |
|--------|-----------|--------|
| Paper Trading — обкатка ML-моделей без риска | 🔴 | ⬜ |
| Structured Logging (structlog → JSON) | 🟡 | ⬜ |
| SQLite бэкапы (cron каждые 6ч, уже настроен) | 🟢 | ✅ |
| Global Kill-Switch (API :8766 + Telegram-бот) | 🟢 | ✅ (уже есть) |
| PR #52823 (macOS symlink fix) | 🟡 | 🟡 ждёт форк |

### Фаза 8 — Android приложение ⬜

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
| Фаза 5 — DSPy + A/B + LSTM + RL | ✅ (частично — RF+Entry Judge) | 18.06.2026 |
| Фаза 6 — WebSocket full + Risk + Push | ✅ | 21.06.2026 |
| Фаза 7 — Graceful shutdown + Heavy cycle opt + Kelly | ✅ | 27.06.2026 |
| Фаза 7.1 — Стабилизация | ✅ | 28.06.2026 |
| Фаза 8 — Android приложение | ⬜ | — |

## Принципы

1. **Сначала защита, потом атака** — лимиты и CB важнее стратегий
2. **Тесты перед деплоем** — 45/45 smoke-тестов
3. **Документация в актуальном состоянии** — docs/ всегда соответствует коду
