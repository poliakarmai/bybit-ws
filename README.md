# bybit-ws — AI-Native Trading Engine

**24/7 автотрейдинг на фьючерсах Bybit. Bollinger Grid + AI-фильтры. Подключил — зарабатывает.**

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-52%2F52-brightgreen)](./test_smoke.py)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](./LICENSE)
[![Phase 8.0](https://img.shields.io/badge/phase-8.0-blue)](./CHANGELOG.md)

---

## Что это

Автономный трейдинг-движок, который:
- Находит точки входа по Bollinger Bands + 9-метричному AI-скорингу
- Входит LONG/SHORT автоматически
- Управляет рисками: стоп-лоссы, тейк-профиты, трейлинг, DCA
- Защищает депозит: Circuit Breaker, BlackSwan 3-tier, корреляционный блок
- Самообучается: LSTM-классификатор рынка, canary-режим для новых стратегий

Работает **24/7 на VPS за $5/мес**. 52 smoke-теста, атомарный деплой.

---

## Результаты (на v8.0)

> **Важно:** это результаты конкретной инсталляции при конкретных параметрах. Не гарантия доходности. Backtest на своих параметрах через `paper_trade`.

| Период | Сделок | Винрейт | PnL |
|--------|--------|---------|-----|
| Июнь 2026 | 89 | 71% | +$340 |
| Июль 2026 | 112 | 68% | +$410 |

*Цифры для стратегии Bollinger Grid LONG, риск 5% на сделку, плечо 3x*

---

## Быстрый старт (3 шага)

### 1. Клонируй и настрой

```bash
git clone https://github.com/poliakarmai/bybit-ws.git
cd bybit-ws
pip install -r requirements.txt
```

### 2. Добавь ключи Bybit

```bash
cp config.example.yaml ~/.config/bybit-ws/config.yaml
mkdir -p ~/.config/bybit-ws

# Создай .env с API-ключами (chmod 600!)
cat > ~/.config/bybit-ws/.env << 'EOF'
BYBIT_API_KEY=your_key
BYBIT_API_SECRET=your_secret
RPC_TOKEN=your_random_token_32_chars
TG_BOT_TOKEN=optional_telegram_bot_token
TG_CHAT_ID=optional_chat_id
EOF
chmod 600 ~/.config/bybit-ws/.env
```

### 3. Запусти

```bash
# Тестовый прогон (без реальных сделок — убедись что всё ок)
python3 -m bybit_ws.paper_trade SOLUSDT --days 30

# Боевой запуск
python3 test_smoke.py          # 52 теста → PASS
bash deploy.sh                 # атомарный деплой с canary-проверкой
```

Готово. Движок работает. Позиции открываются автоматически при сигналах.

---

## Возможности

### 🤖 Авто-трейдинг
- **Bollinger Grid** LONG/SHORT на дневном таймфрейме
- **9-метричный скоринг**: BB%, объём, падающие дни, фондинг, волатильность, качество + ML Gate
- **LSTM-классификатор** рынка (82.3% точность, 5 режимов)
- **Авто-входы** с MTF-подтверждением, orderbook-анализом и Entry Judge (DeepSeek)
- **Auto-TP**: 20% на Middle BB + 80% на Upper BB
- **Unified SL**: 5 механизмов → один приоритетный (tight > simple > hard > BE > default)

### 🛡️ Защита депозита
- **Circuit Breaker** при 80% дневного лимита
- **BlackSwan 3-tier**: BTC −3% → 50% закрытие, −5% → 80%, −8% → 100%
- **Корреляционный блок**: запрет входа при >80% корреляции с открытой позицией
- **Pump Detection**: детект пампов +24ч/+230% нед → блок входа
- **Kill Switch**: мгновенное закрытие всех позиций через RPC

### 📊 Бэктестинг
- **Paper Trading** на исторических данных (`paper_trade.py`)
- 6-метричный скоринг, LONG/SHORT, SL/TP с RR
- Метрики: Sharpe, макс. просадка, profit factor, винрейт

### 🔌 Интерфейсы
- **RPC API** (порт 8766): `/scan`, `/enter`, `/close`, `/positions`, `/metrics`
- **JSON-RPC** для ботов и интеграций
- **MCP Server** для AI-агентов (Claude Code, Hermes, Codex)
- **Telegram-алерты**: входы, SL, TP, DCA, пампы, ошибки
- **One-click trading** через Hermes-чат: «просканируй SOL» → «бери 5%»

### 🧠 AI & Self-Learning
- **Entry Judge**: cross-model validation (DeepSeek), fail-closed
- **LSTM Market Regime**: 5 классов, авто-адаптация параметров
- **Canary mode**: 10% сделок с новыми self-learned параметрами, авто-rollback
- **Post-trade анализ**: кластерный анализ убыточных сделок → блок паттернов

---

## Для кого

| Ты | Как использовать |
|----|-----------------|
| 🧑‍💻 **Трейдер** | Поставил на VPS, подключил алерты в Telegram — двигатель крутит 24/7 |
| 🤖 **Квант/разработчик** | Форкни, подключи свою модель, используй RPC API |
| 💼 **Провайдер сигналов** | Подключи @Gridbolbot к RPC — клиенты получают сигналы |
| 🏢 **Фонд/команда** | Multi-instance, общий RPC, shared мониторинг |

---

## Архитектура

```
┌───────────── MAIN LOOP (async, 30s) ─────────────┐
│  Каждый цикл:                                      │
│  • unified_sl.py     — Unified SL (5 механизмов)   │
│  • auto_tp.py        — ATR-based TP                │
│  • risk_manager.py   — Circuit Breaker + BlackSwan │
│                                                     │
│  Каждые 10 циклов (HEAVY, ~5 мин):                 │
│  • auto_entry.py     — LONG с AI-скорингом         │
│  • auto_short.py     — SHORT + JUNK tier           │
│  • pump_detect.py    — детект пампов               │
│  • correlation.py    — корреляционная матрица      │
│  • dca.py            — DCA-докупки                 │
│                                                     │
│  Раз в 6 часов (720 циклов):                       │
│  • self_learn.py     — самообучение                │
│  • canary mode       — A/B-тест новых параметров   │
└─────────────────────────────────────────────────────┘
```

[Подробная архитектура →](docs/ARCHITECTURE.md)

---

## Проекты экосистемы

| Проект | Назначение |
|--------|-----------|
| **bybit-ws** | Трейдинг-движок (этот репо) |
| [@Gridbolbot](https://t.me/Gridbolbot) | Telegram-бот сигналов (Pro-подписка: безлимит + алерты) |
| [hermes-trader](https://github.com/poliakarmai/hermes-trader) | Встроенный трейдинг в Hermes Agent |
| [Paper Trading](bybit_ws/paper_trade.py) | Бэктестинг на истории |
| [OpenWiki](openwiki/quickstart.md) | Детальная документация |

---

## Документация

| Файл | Содержание |
|------|-----------|
| [ONBOARDING.md](ONBOARDING.md) | **Пошаговая инструкция:** от нуля до первой сделки |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Архитектура, потоки данных, Mermaid-диаграммы |
| [docs/API.md](docs/API.md) | RPC API: 20 эндпоинтов, curl-примеры |
| [docs/SECURITY.md](docs/SECURITY.md) | Безопасность: секреты, модель угроз, аудит |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Решение проблем: 60+ причин |
| [docs/PRD-one-click.md](docs/PRD-one-click.md) | One-click trading через чат |
| [AGENTS.md](AGENTS.md) | Навигация для AI-агентов |
| [CHANGELOG.md](CHANGELOG.md) | История версий |

---

## Требования

- **Python 3.11+**
- Bybit Unified Trading Account + API ключи (read/write + trading)
- Linux-сервер (VPS $5/мес) или Docker
- Минимальный депозит: $50 на testnet, $200 на mainnet

---

## Безопасность

- API-ключи через `.env` (chmod 600) — никогда в коде
- RPC write-эндпоинты требуют Bearer-токен
- IP-whitelist на уровне Bybit
- Атомарный деплой: 6 pre-deploy проверок + 8 canary checks
- Kill Switch: мгновенное закрытие всех позиций

[Подробнее →](docs/SECURITY.md)

---

## Дисклеймер

**Этот софт торгует реальными деньгами на фьючерсах с плечом до 10x. Можно потерять весь депозит.** Авторы не несут ответственности за финансовые потери. Начинайте с testnet. Бэктестите через `paper_trade.py`. Прошлые результаты не гарантируют будущих.

---

## Лицензия

AGPL-3.0

---

*Built for traders. Hardened by AI. Вопросы: [@Poliakarm](https://t.me/Poliakarm).*
