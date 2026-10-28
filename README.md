# bybit-ws — AI-Native Trading Engine

**24/7 автотрейдинг на фьючерсах Bybit. Bollinger Grid + AI-фильтры. Подключил — зарабатывает.**

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/pytest-passing-brightgreen)](./AGENTS.md) [![Smoke](https://img.shields.io/badge/smoke-52%2F52-brightgreen)](./test_smoke.py)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](./LICENSE)
[![Phase 11](https://img.shields.io/badge/phase-11-blue)](./CHANGELOG.md)

---

## Что это

Автономный трейдинг-движок, который:
- Находит точки входа по Bollinger Bands + 9-метричному AI-скорингу
- Входит LONG/SHORT автоматически
- Управляет рисками: стоп-лоссы, тейк-профиты, трейлинг, DCA
- Защищает депозит: Circuit Breaker, BlackSwan, Anti-ludomania, корреляционный блок
- Самообучается: LSTM-классификатор рынка, Thompson Sampling, Dynamic Bandit, Drift Detector, Ensemble (20+ механик)

Работает **24/7 на VPS за $5/мес**. pytest-набор + 52 smoke-проверки, атомарный деплой.

---

## Результаты

Статистику здесь не держим: она меняется несколько раз в день, а статичный снимок в README
через сутки врёт. Живые числа — из самого бота:

```bash
curl -s localhost:8766/metrics          # сделки, входы, SL/TP за день
curl -s localhost:8766/positions        # открытые позиции + PnL (Bearer)
python3 -m bybit_ws.reconcile --days 30 # сходимость state.db с биржей
python3 -m bybit_ws.paper_trade SOLUSDT --days 90   # бэктест на своих параметрах
```

> **Не гарантия доходности.** Цифры конкретной инсталляции (`strategy='auto'`, `manual=0`) —
> не обещание. `trade_history.side` — сторона **закрытия** (`Buy`=закрыл SHORT), в `/positions`
> наоборот: сырой Bybit.

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
mkdir -p ~/.config/bybit-ws
cp config.example.yaml ~/.config/bybit-ws/config.yaml

# Создай env с API-ключами (chmod 600!)
cat > ~/.config/bybit-ws/env << 'EOF'
BYBIT_API_KEY=your_key
BYBIT_API_SECRET=your_secret
RPC_TOKEN=your_random_token_32_chars
TG_BOT_TOKEN=optional_telegram_bot_token
TG_CHAT_ID=optional_chat_id
EOF
chmod 600 ~/.config/bybit-ws/env
```

### 3. Запусти

```bash
# Тестовый прогон (без реальных сделок — убедись что всё ок)
python3 -m bybit_ws.paper_trade SOLUSDT --days 30

# Боевой запуск
python3 test_smoke.py            # 52 проверки → PASS
python3 -m pytest -q             # весь набор (должно быть зелёным)
python3 -m pytest tests/ -q      # только tests/
bash deploy.sh                 # атомарный деплой с canary-проверкой
```

Готово. Движок работает. Позиции открываются автоматически при сигналах.

---

## Возможности

### 🤖 Авто-трейдинг
- **Bollinger Grid** LONG/SHORT на дневном таймфрейме
- **9-метричный скоринг**: BB%, объём, падающие дни, фондинг, волатильность, качество + ML Gate
- **LSTM-классификатор** рынка (5 классов; 82.3% — заявлено 01.08, не перезамерялось)
- **Авто-входы** с MTF-подтверждением, orderbook-анализом и Entry Judge (DeepSeek, fail-closed, fail-open только при длительном отказе LLM)
- **Auto-TP**: ATR-adaptive, 3 ноги `[0.40, 0.35, 0.25]` (% размера), множители ATR по режиму. BB 20% Middle / 80% Upper — fallback, когда ATR недоступен
- **Unified SL**: 5 механизмов → один приоритетный (tight > simple > hard > BE > default)

### 🛡️ Защита депозита
- **Circuit Breaker** при 80% дневного лимита
- **BlackSwan v2**: 5+ красных позиций + uPnL < -$150 → алерт (без авто-закрытия)
- **Anti-ludomania**: 3 убытка за час → блок авто-входов на 30 минут
- **Корреляционный блок**: запрет входа при >80% корреляции с открытой позицией
- **Pump Detection**: детект пампов +24ч/+230% нед → блок входа
- **Kill Switch**: мгновенное закрытие всех позиций через RPC

### 📊 Бэктестинг
- **Paper Trading** на исторических данных (`paper_trade.py`)
- 6-метричный скоринг, LONG/SHORT, SL/TP с RR
- Метрики: Sharpe, макс. просадка, profit factor, винрейт

### 🔌 Интерфейсы
- **RPC API** (порт 8766): `/health`, `/scan`, `/enter`, `/close`, `/positions`, `/metrics`, `/risk`, `/balance` — полный список путей в `do_GET`/`do_POST` (`rpc.py`)
- **HTTP RPC** для ботов и интеграций (`http.server`, не JSON-RPC)
- **MCP Server** для AI-агентов (Claude Code, Hermes, Codex)
- **Telegram-алерты**: входы, SL, TP, DCA, пампы, ошибки
- **One-click trading** через Hermes-чат: «просканируй SOL» → «бери 5%»

### 🧠 AI & Self-Learning (v10 — 20+ механик)
- **Thompson Sampling**: Dynamic Bandit — авто-прунинг + генерация рук, Uncertainty-aware selection
- **Ensemble Learning**: отдельный bandit на режим — 6 (5 классов LSTM + `CHOPPY`)
- **Drift Detector**: per-regime окна с EMA baseline, Causal inference (market vs parameters)
- **Stress Testing**: 4 исторических backtest-сценария + 1000 Pareto Monte Carlo симуляций
- **Composite Score**: WR+PF+Sharpe+MaxDD+AvgHold с adaptive per-regime весами и exponential decay
- **Micro-updates**: обучение после каждой сделки с outlier-защитой (>3σ)
- **Canary mode**: Bayesian A/B тест, адаптивный 5-20%, авто-rollback
- **Entry Judge**: одна модель (DeepSeek, fail-closed); Qwen убран 17.10, Nemotron в коде вызова нет
- **LSTM Market Regime**: 5 классов, авто-адаптация LONG/SHORT (82.3% — заявлено, не перезамерялось)
- **LSTM World Model**: multi-task OHLCV prediction для скоринга входов

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
│  • time_exit.py       — Time exit (каждый цикл, макс 24ч)│
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
│  Раз в 24 часа (self-learn):                        │
│  • self_learn.py     — Dynamic Bandit, Drift, Ensemble│
│  • canary mode       — Bayesian A/B-тест параметров │
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
| [docs/INDEX.md](docs/INDEX.md) | Карта документации: что актуально, что архив |

---

## Документация

| Файл | Содержание |
|------|-----------|
| [ONBOARDING.md](ONBOARDING.md) | **Пошаговая инструкция:** от нуля до первой сделки |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Архитектура, потоки данных, Mermaid-диаграммы |
| [docs/API.md](docs/API.md) | RPC API: реальный список эндпоинтов из `rpc.py`, curl-примеры |
| [docs/INDEX.md](docs/INDEX.md) | Карта документации + статус каждого файла |
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

- API-ключи через `env` (chmod 600) — никогда в коде
- RPC write-эндпоинты требуют Bearer-токен
- IP-whitelist на уровне Bybit
- Атомарный деплой: 3 pre-deploy шага (чистота репо → logic → smoke) + 8 canary-проверок
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
