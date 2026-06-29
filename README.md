# bybit-ws — AI-Native Trading Engine

**Bollinger Grid с авто-входами, трейлингом и DCA. 8 стратегий. MCP-сервер для AI-агентов. Telegram-алерты.**

[![Version](https://img.shields.io/badge/version-7.0.0-blue)](./CHANGELOG.md) [![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org) [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE) [![Tests](https://img.shields.io/badge/tests-45%2F45-brightgreen)](./test_smoke.py) [![Phase 7](https://img.shields.io/badge/phase_7-✓-green)](./AGENTS.md)

---

## Архитектура модулей (v6.0.0)

```
┌─────────────────── MAIN LOOP (30s) ───────────────────┐
│                                                        │
│  Каждый цикл:                                          │
│    • state_db.py        — SQLite (WAL, 8 таблиц)      │
│    • auto_sl.py         — проверка/фикс стоп-лоссов    │
│    • trailing_sl.py     — подтяжка SL (LONG + SHORT)   │
│    • junk_trail.py      — трейлинг TP (JUNK-шорты)    │
│    • auto_tp.py         — авто-TP на Middle/Upper BB   │
│    • dca.py             — DCA-докупки                  │
│    • reporting.py       — compliance-аудит (LONG+SHORT)│
│                                                        │
│  Каждые 10 циклов (HEAVY):                              │
│    • auto_entry.py      — авто-входы (LONG scoring)    │
│    • auto_short.py      — авто-SHORT + JUNK-шорты      │
│    • pump_detect.py     — детект пампов (24ч/нед)      │
│    • correlation.py     — корреляционная матрица       │
│                                                        │
│  Мониторинг:                                           │
│    • health.py          — ликвидации, сквизы, фондинг  │
│    • overbought.py      — детект перегрева             │
│    • cost_tracker.py    — учёт комиссий                │
│    • cleanup.py         — чистка просроченных ордеров  │
│    • ws_client.py       — WebSocket live цены/BB       │
│    • mtf_confirmation.py— D/W/M конфлюенс              │
│                                                        │
└────────────────────────────────────────────────────────┘
         │
         ▼ RPC (порт 8766) + Web (порт 9999)
    • rpc.py              — JSON-RPC, /metrics Prometheus
    • web/proxy_server.py — прокси дашборда
    • web/dashboard.html  — дашборд v5.0 (риски, позиции, сигналы)
```

### Полный список файлов движка

| Файл | Назначение |
|------|-----------|
| `state_db.py` | SQLite (WAL, 8 таблиц) — trade_history, cooldowns, alert_dedup |
| `main.py` | Главный цикл, RPC-сервер, оркестрация модулей |
| `api.py` | Bybit REST API: позиции, ордера, SL/TP, BB-данные |
| `config.py` | Конфиг: 8 стратегий, риск-менеджмент, tiers |
| `position_sizing.py` | Динамический сайзинг (% депозита × score × multiplier) |
| `auto_short.py` | Авто-SHORT: Tier A/B (обычный) + JUNK C/D (памп-шорты без SL) |
| `auto_entry.py` | Авто-LONG: BB < порога, score ≥ мин |
| `auto_tp.py` | Авто-TP: 20% на Middle BB + 80% на Upper BB (LONG + SHORT) |
| `auto_sl.py` | Авто-SL: проверка и фикс стоп-лоссов (LONG + SHORT, tier-based) |
| `trailing_sl.py` | Трейлинг-SL: LONG (BB>75%) + SHORT (BB<25%), PnL>15% |
| **`junk_trail.py`** | Трейлинг-TP для JUNK-шортов: фиксация 70% при +15%, 85% при +30% |
| `pump_detect.py` | Детект пампов: 24ч ≥80%, недельный ≥230% |
| `dca.py` | DCA-докупки LONG при −5/−10/−15% от входа |
| `correlation.py` | Корреляционная матрица: блок при >80% корреляции |
| `health.py` | Мониторинг: ликвидации, BB-сквизы, фондинг, дневная просадка |
| `overbought.py` | Ротация вотчлиста перегретых монет |
| `rsi.py` | RSI-дивергенции |
| `squeeze.py` | BB-сквиз детектор |
| `reporting.py` | Сводки, compliance-аудит (LONG + SHORT), coverage summary |
| `cleanup.py` | Чистка просроченных/старых ордеров |
| `cost_tracker.py` | Учёт торговых комиссий |
| `recycle.py` | Рециркуляция TP → ре-вход |
| `metrics.py` | Запись метрик (алерты, авто-входы) |
| `alerts.py` | Telegram-алерты, дедупликация |
| `snapshot.py` | Снапшоты позиций/ордеров, детект изменений |
| `rpc.py` | HTTP-RPC сервер (:8766) — прямые вызовы api.bybit() |

---

## Режимы работы

### LONG (BB Grid)
- **Триггер**: BB Daily < порога (Tier A: <15%, B: <25%, C: <40%, D: <65%)
- **Плечо**: 3x
- **Маржа**: динамическая (депозит × 20% / макс_позиций × score_multiplier)
- **SL**: −7% от Lower BB (trading-stop)
- **TP**: 20% на Middle BB + 80% на Upper BB (лимитные reduceOnly)
- **Трейлинг SL**: подтягивается при BB Weekly >75% и профите >15%
- **DCA**: докупка при −5/−10/−15% от входа, макс 2 добавки
- **Макс позиций**: 12

### SHORT (хедж)
- **Триггер**: BB Daily > 85%, Tier A/B/C/D монеты
- **Плечо**: 3x
- **SL**: +5% (Tier A/B), +7% (Tier C/D)
- **Trailing SL**: BB Weekly < 25% + PnL > 15% → SL ползёт вниз
- **TP**: Middle BB
- **Макс позиций**: 3 (общий лимит с JUNK)

### SHORT JUNK Tier C/D (памп-шорты) 🆕
- **Триггер**: 24ч рост ≥ 80% + BB Daily > 70%
- **Плечо**: 3x
- **SL**: **НЕТ** (JUNK слишком волатильный — SL только жрёт маржу)
- **Защита**: max_loss −15% маржи (hard market-close), max_hold 48ч (авто-закрытие)
- **Вход**: лимитка Sell на +2% выше рынка (ждём отскока)
- **TP #1**: Middle BB (лимитный reduceOnly Buy, ставится при входе)
- **TP #2 (трейлинг)**: при профите >15% → подтягивает TP, фиксируя 70% прибыли
- **TP #3 (трейлинг)**: при профите >30% → затягивает до 85% прибыли
- **DCA-лесенка**: лимитки Sell на +100% и +120% от входа
- **Макс позиций**: 3 (общий лимит с Tier A/B)

### JUNK-шорт: полный жизненный цикл
```
1. pump_detect.py → памп +80% за 24ч
2. auto_short.py → лимитка Sell +2%, TP Middle BB, DCA +100%/+120%
3. check_junk_dca() → проверка max_loss/max_hold/DCA-уровней
4. junk_trail.py → профит >15%: TP подтянут (фиксация 70%)
5. junk_trail.py → профит >30%: TP затянут (фиксация 85%)
6. TP срабатывает → позиция закрыта с прибылью
```

### SL Re-entry
- После срабатывания SL — лесенка re-entry (price / 1.05, price / 1.10, price / 1.15)
- До 3 попыток на монету

### x10 Стратегии (высокий риск)

| Стратегия | TF | Триггер | SL | TP | Макс поз |
|-----------|-----|---------|-----|-----|---------|
| Scalp M5 | M5 | Касание BB + RSI | −3% | Middle | 3 |
| Mean Revert | D | BB <5% или >95% | −5% | Middle | 5 |
| Funding Momentum | D | Фондинг ±0.1% + BB + тренд | −4% | Middle | 3 |

⚠️ **x10 плечо — ликвидация при ~10% движения против позиции.**

---

## Формат уведомлений

```
🔴 SHORT JUNK HMSTR: вход $0.000373 лимит $0.000380 ×664190 (3x) | памп +120% | TP $0.000112 | DCA: +100% @ $0.000746, +120% @ $0.000821

🔴 SHORT MOVE: вход $0.0143 лимит $0.0146 ×700 (3x) | BB=92% | SL $0.0150 (+5%) | TP $0.0120

🔴 DCA JUNK HMSTR: +100% @ $0.000746 ×664190 | вход $0.000373 → сейчас $0.000750

🔒 JUNK Trail TP HMSTR: фиксация 70% при +22.5% | вход $0.000373 → TP $0.000210 (был $0.000112)

🛑 STOP JUNK HMSTR: убыток -16.2% > лимит 15% | вход $0.000373 → выход $0.000433 | PnL $-12.40

⏰ TIMEOUT JUNK HMSTR: 49ч > 48ч лимит | выход $0.000380 | PnL $-1.20
```

---

## Risk Management

| Механизм | Детали |
|----------|--------|
| Динамический сайзинг | депозит × risk% / max_positions × score_multiplier |
| Просадка депозита | Alert + optional emergency close при −3% дневной / −15% общей (configurable) |
| Дневной лимит убытка | Остановка торгов при −$50/день |
| Корреляционный блок | Отказ от входа если ≥2 позиции с корреляцией >0.8 |
| x10 защита | Макс 3 убыточных x10 сделки → кулдаун 24ч |
| Каскадная защита | Market-close если цена в 2× ближе к ликвидации чем к SL |
| Бан-лист | config-driven, постоянный |
| JUNK защита | max_loss −15% маржи, max_hold 48ч, без SL |
| **BlackSwan multi-tier** 🆕 | Tier 1: BTC -3%/15min → 50% | Tier 2: BTC -5%/30min → 80% | Tier 3: BTC -8%/1h → 100% |
| **Canary mode** 🆕 | 10% входов с новыми self-learned параметрами, авто-rollback при падении WR |

---

## Интерфейсы

### REST API (порт 8766)
```bash
curl http://localhost:8766/health          # статус
curl http://localhost:8766/positions       # позиции (read)
curl http://localhost:8766/scan?mode=short # сканирование рынка
curl -H "Authorization: Bearer $TOKEN" \
     -X POST http://localhost:8766/enter   # вход (write, требует auth)
```

### MCP Server
```python
# AI-агенты (Claude Code, Codex, Cursor, Hermes) подключаются напрямую
mcp_bybit_ws_get_positions()   # позиции + PnL + SL
mcp_bybit_ws_scan_market()     # BB-сигналы LONG/SHORT
mcp_bybit_ws_get_metrics()     # дневная статистика
mcp_bybit_ws_vpn_status()      # VPN + трафик
```

### Python SDK

```python
from bybit_ws_sdk import Monitor
m = Monitor("http://localhost:8766", token="your-token")

# Сканирование рынка
signals = m.scan(mode="short", limit=5)
for s in signals["signals"]:
    preview = m.enter(s["symbol"], "Sell", s["qty"], confirm=False)
    print(f"Preview {s['symbol']}: sell {preview['qty']} @ market")
    m.enter(s["symbol"], "Sell", s["qty"], confirm=True)

# Позиции и метрики
positions = m.positions()          # все позиции + PnL + SL
metrics = m.metrics()              # дневная статистика (SL/TP/entries)
pnl = sum(p["upnl"] for p in positions["positions"])  # общий PnL

# Управление
m.set_sl("DOGEUSDT", 0.075)        # set stop-loss
m.close("APTUSDT")                 # закрыть позицию по рынку
m.cancel_all()                     # отменить все ордера
```

---

## Быстрый старт

### Установка

```bash
git clone https://github.com/poliakarmai/bybit-ws.git
cd bybit-ws
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml ~/.config/bybit-ws/config.yaml
cp .env.example ~/.config/bybit-ws/.env
nano ~/.config/bybit-ws/.env    # BYBIT_API_KEY, BYBIT_API_SECRET
python3 -m bybit_ws              # запуск
```

### systemd (рекомендуется)

```bash
mkdir -p ~/.config/systemd/user
cp bybit-ws.service ~/.config/systemd/user/
# Убедись что путь в ExecStart ведёт к bybit-ws:
which bybit-ws                    # должно быть ~/.local/bin/bybit-ws
systemctl --user daemon-reload
systemctl --user enable --now bybit-ws
systemctl --user status bybit-ws  # проверка
journalctl --user -u bybit-ws -f  # логи
```

### Docker

```bash
docker compose up -d
```

## Troubleshooting

| Симптом | Причина | Решение |
|---------|---------|--------|
| `ModuleNotFoundError: bybit_ws` | Не установлен пакет | `pip install -e .` из корня проекта |
| `bybit-ws: command not found` | Нет entry point | `pip install -e .` или `python3 -m bybit_ws` |
| RPC возвращает 401 | Неверный токен | Проверить `RPC_TOKEN` в `.env` |
| Сервер не запускается | Пустой RPC_TOKEN + bind не localhost | Задать `RPC_TOKEN` в `.env` |
| Bybit: «invalid API key» | Неверные ключи или testnet/mainnet | Проверить `api.base_url` и ключи |
| Позиции не открываются | Депозит < $30 или banned | Проверить `get_deposit()` логи |
| Telegram-алерты не приходят | Не задан бот или чат | Проверить `TG_BOT_TOKEN`, `TG_CHAT_ID` |
| `Connection refused` к RPC | Сервер не запущен или порт занят | `systemctl --user status bybit-ws`, `ss -tlnp :8766` |
| Высокий PnL минус при запуске | Нет стоп-лоссов | Дождаться первого цикла (30с), auto_sl выставит SL |

---

## Конфигурация

Ключевые параметры `~/.config/bybit-ws/config.yaml`:

```yaml
api:
  key: "${BYBIT_API_KEY}"
  secret: "${BYBIT_API_SECRET}"
  base_url: "https://api-testnet.bybit.com"

strategy:
  long:
    leverage: 3
    max_positions: 12           # макс одновременных LONG-позиций (hard limit)
    sl_offset: 0.07
  short:
    leverage: 3
    max_positions: 3            # макс одновременных SHORT (включая JUNK)
    bb_threshold: 85
  junk:                          # 🆕 JUNK-шорты
    daily_pump_threshold: 0.80   # 80% рост за 24ч
    weekly_pump_threshold: 2.30  # 230% за неделю
    dca_levels: [1.0, 1.2]     # DCA на +100% и +120%
    max_loss_pct: 15            # hard stop при −15% маржи
    max_hold_hours: 48          # авто-закрытие через 48ч

risk:
  max_drawdown_pct: 15
  max_daily_loss: 50
  emergency_close_all: true

position_sizing:
  long_risk_pct: 0.20           # 20% депозита в риске
  max_positions: 5              # база для расчёта маржи: депозит × risk% / N позиций
  max_position_share: 0.40      # не более 40% бюджета на позицию

rpc:
  port: 8766
  auth_token: "${RPC_TOKEN}"
```

---

## Требования

- Python 3.11+
- `requests`, `pyyaml`, `websocket-client`, `numpy`
- Bybit Unified Trading Account + API ключи (read/write + trading)
- Linux (VPS $5/мес) или Docker

---

## Безопасность

**API-ключи:**
- Ключи через `~/.config/bybit-ws/.env` → читаются как `${BYBIT_API_KEY}`
- Никогда не коммить `.env` (добавлен в `.gitignore`)
- `chmod 600 ~/.config/bybit-ws/.env ~/.config/bybit-ws/config.yaml`

**RPC-сервер:**
- Write-эндпоинты (`/enter`, `/close`, `/set-sl`) требуют `Authorization: Bearer <token>
- ⚠️ Если `RPC_TOKEN` не задан и `bind ≠ 127.0.0.1` — сервер откажется запускаться
- Bind: `127.0.0.1` по умолчанию. Для внешнего доступа — `0.0.0.0`

**Bybit:**
- IP-whitelist для API-ключей (Bybit → Account → API Management)
- Регулярная ротация ключей (раз в 90 дней)
- Отдельные ключи для чтения и торговли (принцип минимальных прав)

---

## Статистика

Реальные цифры зависят от рыночных условий. Прошлые результаты не гарантируют будущих.

---

## 🤖 GridSignal Bot (@Gridbolbot)

Telegram-бот сигналов Bollinger Grid.

```
Команды:
  /scan              Топ-5 LONG-сигналов
  /scan short        Топ-5 SHORT
  /scan scalp        BB Scalping x10
  /scan mean         Mean Reversion x10
  /pro               GridSignal Pro (безлимит)
```

**Подписка:**
| Тариф | Сканы | Цена |
|-------|-------|------|
| 🆓 Бесплатно | 3 /scan в день | 0₽ |
| ⭐ Pro | Безлимит + алерты | 300 Stars (~400₽) или ~2 TON |

**Оплата:** Telegram Stars + TON (CryptoBot).  
Бот: [@Gridbolbot](https://t.me/Gridbolbot)  
Код: `~/.local/bin/gridsignal-bot.py`

---

## Файлы документации

| Файл | Содержание |
|------|-----------|
| [`docs/README.md`](docs/README.md) | **Полная документация:** установка, работа, возможности, ошибки, логирование, проверки |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Архитектура: компоненты, потоки данных, модель данных, Mermaid-диаграммы |
| [`docs/API.md`](docs/API.md) | API Reference: 20 RPC-эндпоинтов, 6 MCP-инструментов, curl-примеры |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Безопасность: секреты, модель угроз, инциденты, аудит |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | Решение проблем: 13 категорий, 60+ причин |
| [`DESIGN-STRATEGIES.md`](DESIGN-STRATEGIES.md) | Дизайн стратегий: Bollinger Grid, scoring, риск-менеджмент |
| [`AGENTS.md`](AGENTS.md) | Навигация для AI-агентов, авто-обнаружение путей |
| [`CHANGELOG.md`](CHANGELOG.md) | История версий (Keep a Changelog) |
| [`config.example.yaml`](config.example.yaml) | Полный конфиг с комментариями |

---

## Дисклеймер

**Этот софт торгует реальными деньгами на фьючерсах с плечом до 10x. Можно потерять весь депозит.** Авторы не несут ответственности за финансовые потери. Начинайте с testnet.

---

## Лицензия

MIT

---

*Built for AI agents. Ready for yours. Вопросы? Пиши [@Poliakarm](https://t.me/Poliakarm).*
