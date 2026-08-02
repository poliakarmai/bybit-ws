# bybit-ws — Документация

Трейдинг-монитор для Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT по BB-полосам).  
Работает как systemd-сервис, ~25 MB RAM, SQLite — единственный источник истины (SSOT).

## 📚 Полный комплект документации

| Документ | Описание |
|----------|----------|
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | Архитектура: компоненты, потоки данных, модель данных, сетевая схема, Mermaid-диаграммы |
| **[API.md](API.md)** | API Reference: 12 GET + 8 POST RPC-эндпоинтов, 6 MCP-инструментов, аутентификация, примеры |
| **[SECURITY.md](SECURITY.md)** | Безопасность: секреты, модель угроз, best practices, инциденты, аудит, network security |
| **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** | Решение проблем: 13 категорий, 60+ причин, команды диагностики |
| **[ROADMAP.md](ROADMAP.md)** | План развития: Фазы 4.3/5/6, приоритеты, сроки |
| **[../CHANGELOG.md](../CHANGELOG.md)** | История версий: v1.0 → v4.1, Keep a Changelog |
| **[../config.example.yaml](../config.example.yaml)** | Полный конфиг с комментариями (734 строки, все параметры) |
| **[../AGENTS.md](../AGENTS.md)** | Навигация для AI-агентов: структура, RPC, авто-обнаружение путей |
| **[../DESIGN-STRATEGIES.md](../DESIGN-STRATEGIES.md)** | Дизайн стратегий: Bollinger Grid, scoring, риск-менеджмент |

---

## Быстрый старт

---

## 1. Установка

### Требования
- Linux (протестировано: Ubuntu 22.04+, Debian 12)
- Python 3.11+
- Bybit API-ключи (только торговые права, без Wallet)
- systemd (user-level)

### Быстрая установка

```bash
# 1. Клонировать репо
git clone https://github.com/poliakarmai/bybit-ws ~/bybit-ws
cd ~/bybit-ws

# 2. Создать venv и установить зависимости
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Создать конфиг API-ключей
mkdir -p ~/.config/bybit-cli
cat > ~/.config/bybit-cli/config << EOF
BYBIT_API_KEY=your_api_key
BYBIT_API_SECRET=your_api_secret
EOF
chmod 600 ~/.config/bybit-cli/config

# 4. Создать конфиг монитора
mkdir -p ~/.config/bybit-ws
cp config.example.yaml ~/.config/bybit-ws/config.yaml
# Отредактировать: tiers, risk limits, стратегии

# 5. Создать data-директорию
mkdir -p ~/.local/share/bybit-ws

# 6. Установить systemd-сервис
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/bybit-ws.service << 'UNIT'
[Unit]
Description=Bybit WebSocket Monitor
After=network.target

[Service]
Type=simple
ExecStart=%h/bybit-ws/.venv/bin/python3 -m bybit_ws
WorkingDirectory=%h/bybit-ws
Restart=always
RestartSec=5
Environment=HOME=%h

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now bybit-ws
```

### Структура файлов после установки

| Путь | Назначение |
|------|-----------|
| `~/bybit-ws/` | Репозиторий (исходники) |
| `~/.local/lib/bybit_ws/` | Рабочая копия (исполняемая) |
| `~/.local/share/bybit-ws/` | Данные (SSOT, логи, снепшоты) |
| `~/.config/bybit-ws/config.yaml` | Конфигурация монитора |
| `~/.config/bybit-cli/config` | API-ключи Bybit |

### Авто-обнаружение путей для AI-агентов

```bash
# Способ 1 — RPC (рекомендуемый)
curl http://127.0.0.1:8766/rpc/paths

# Способ 2 — AGENTS.md в репо
cat ~/bybit-ws/AGENTS.md

# Способ 3 — переменные окружения
export BYBIT_WS_DATA_DIR=~/.local/share/bybit-ws
export BYBIT_WS_CONFIG=~/.config/bybit-ws/config.yaml
```

---

## 2. Работа

### Запуск/остановка

```bash
systemctl --user status bybit-ws    # статус
systemctl --user restart bybit-ws   # перезапуск
systemctl --user stop bybit-ws      # остановка
journalctl --user -u bybit-ws -f    # логи
```

### Главный цикл

Монитор работает циклически:
1. **Лёгкий цикл** (~2 сек): проверка WebSocket, обновление цен
2. **Тяжёлый цикл** (~2 мин): позиции, SL/TP, авто-шорты, фандинг, корреляции, трейлинг-стопы
3. **x10 цикл** (~5 мин): проверка x10 позиций (скальп, mean reversion, funding momentum)

### Обновление после правок в репо

```bash
# После коммитов в ~/bybit-ws/:
cp ~/bybit-ws/bybit_ws/*.py ~/.local/lib/bybit_ws/
systemctl --user restart bybit-ws

# Проверка: файлы НЕ должны быть симлинками
ls -la ~/.local/lib/bybit_ws/main.py  # должен быть обычный файл, не lrwxrwxrwx
```

**Питфол:** `pip install -e` сломан (flat-layout), симлинки битые. Всегда использовать ручную синхронизацию.

### RPC-сервер

Порт: `8766`, хост: `127.0.0.1`.  
Токен авторизации: `SELECT value FROM kv_store WHERE key='rpc_auth_token'` из `state.db`.

Публичные эндпоинты (без авторизации):
- `/health` — статус монитора
- `/rpc/paths` — все пути установки

Защищённые эндпоинты (Bearer-токен):
- `/rpc/positions`, `/rpc/orders`, `/rpc/metrics`, `/rpc/risk`, `/rpc/signals`, `/rpc/config`
- `POST /scan`, `POST /enter`, `POST /close`

---

## 3. Возможности

### Торговые стратегии

| Стратегия | Направление | Плечо | Маржа | Условия |
|-----------|------------|-------|-------|--------|
| **Bollinger Grid LONG** | LONG | 3× | ~$12 | BB Daily < 15%, Score ≥ 4 |
| **Bollinger Grid SHORT** | SHORT | 3× | ~$12 | BB Daily > 85%, Score ≥ 4 |
| **Junk SHORT** | SHORT | 3× | ~$12 | Дневной рост ≥ 80%, BB > 70%, без SL, DCA +100%/+120% |
| **x10 Scalp** | LONG/SHORT | 10× | ~$10 | BB M5, RSI, объём |
| **x10 Mean Revert** | LONG/SHORT | 10× | ~$10 | Отскок от BB-полос |
| **x10 Funding** | LONG/SHORT | 10× | ~$10 | Фандинг-моментум |

### Tier-система

| Tier | Монеты | Особенности |
|------|--------|------------|
| A | BTC, ETH, SOL, UNI, LINK | Флагманы, полный доступ |
| B | AVAX, DOT, ADA, WLD, ENA | Стабильные альты |
| C/D | Всё остальное | Шлак-режим: без SL, DCA, max loss 15%, авто-закрытие 48ч |

### Особые режимы

- **One-Way исключения:** XRP, ONDO, WLFI, ENJ, ESPORTS, AVAX, APT, SUI — нельзя SHORT
- **Banned symbols:** настраивается в `config.yaml → risk.banned_symbols`

### Защитные механизмы

| Механизм | Описание |
|----------|----------|
| **Корреляционный стоп** | Блокировка LONG-входов при >80% LONG в портфеле |
| **Авто-безубыток** | SL → entry + 1% при профите >10% цены |
| **Трейлинг-стоп** | Поджим SL при движении в прибыль (BB + PnL фильтры) |
| **Памп-детектор** | Обнаружение пампов, приостановка авто-действий |
| **Кулдауны** | 2ч на монету после SL, 4ч на re-entry |
| **Watchdog** | Аварийный выход при зависании цикла > N секунд |

### Метрики и мониторинг

- **RPC `/rpc/metrics`** — JSON с TP/SL/входами/циклами
- **Prometheus `/metrics`** — bybit_ws_active_positions, bybit_ws_daily_pnl, bybit_ws_cycle_duration_seconds
- **Cron-watchdog** (`bybit-watchdog.sh`) — каждые 30 мин, silent when OK

### AI-агенты

MCP-сервер (`bybit-mcp-server.py`) предоставляет инструменты:
- `scan_market(mode, interval)` — сканирование сигналов
- `get_positions()` — текущие позиции + PnL
- `get_metrics()` — дневные метрики
- `get_risk_status()` — лимиты риска
- `place_entry()` — вход в позицию (Market/Limit)

---

## 4. Ошибки

### Типичные ошибки и их причины

| Код | Сообщение | Причина | Решение |
|-----|----------|---------|---------|
| HTTP 404 | `funding-history` | Эндпоинт удалён в Bybit v5 | Используется `transaction-log`, 404 логируется без ретраев |
| 10001 | `position idx not match` | Неверный positionIdx | Пробовать 0→1→2 |
| 110017 | `cannot fix reduce-only` | Попытка reduce-only без позиции | Пропустить idx, попробовать следующий |
| 110094 | `minimum order value 5USDT` | Ордер < $5 | Увеличить qty |
| `Address already in use` | RPC не поднялся | Старый процесс держит порт | `pkill -f bybit-ws`, подождать 5с, рестарт |
| `🚨 Watchdog` | Аварийный выход | Цикл завис > N секунд | Проверить логи, обычно 404-флуд или сетевые таймауты |

### Известные баги (июнь 2026)

1. **UNI positionIdx=2** — работает нестабильно, требует проверки перед входом
2. **ALLO/STG SL re-entry** — цена > current * 1.01 → fallback на current * 0.999
3. **Медленные циклы** (>100s) — SL re-entry ладдеры + BB-запросы для авто-шортов
4. **RPC после watchdog** — порт занят старым процессом, ручной `pkill`

---

## 5. Логирование

### Файлы логов

| Файл | Содержание |
|------|-----------|
| `~/.local/share/bybit-ws/events.log` | Все события: входы, выходы, SL/TP, ошибки, watchdog, циклы |
| `~/.local/share/bybit-ws/alerts.log` | Только алерты (ENTRY, STOP, ALERT) |
| `journalctl --user -u bybit-ws` | Systemd-логи (старт/стоп/крэши) |

### Формат логов

```
[2026-06-18 05:41:25] Монитор запущен: 7 позиций, 22 ордеров
[2026-06-18 05:42:31] 🛑 Корреляция 86% LONG — авто-вход заблокирован
[2026-06-18 05:43:55] ⚠️ Цикл 105.9s — превышен порог 20s
```

Все записи имеют временную метку `[YYYY-MM-DD HH:MM:SS]`.

### Уровни событий

| Префикс | Значение |
|---------|----------|
| `✅` | Успех: SL/TP поставлен, ордер создан |
| `❌` | Ошибка: ордер отклонён, API error |
| `⚠️` | Предупреждение: медленный цикл, ошибка авто-шорта |
| `🛑` | Блокировка: корреляция, риск-лимит, banned |
| `🚨` | Критическое: watchdog, аварийный выход |
| `📌` | Информация: re-entry лимитка, DCA |
| `🔴` | SHORT-вход / junk-шорт |
| `🟢` | LONG-вход |
| `⏳` | Ожидание: SL re-entry не удался, ждём |
| `⏱️` | Time budget: авто-шорт не успел проверить всех кандидатов |
| `📊` | Рыночная информация: режим рынка, BTC/ETH |

### Просмотр логов

```bash
# Последние 50 событий
tail -50 ~/.local/share/bybit-ws/events.log

# Ошибки за сегодня
grep "❌\|⚠️\|🚨" ~/.local/share/bybit-ws/events.log | grep "$(date +%Y-%m-%d)"

# Watchdog-аварии
grep "🚨 Watchdog" ~/.local/share/bybit-ws/events.log

# 404-флуд
grep "404.*funding" ~/.local/share/bybit-ws/events.log | wc -l

# Алерты
tail -50 ~/.local/share/bybit-ws/alerts.log
```

---

## 6. Проверки

### Health-check (ручной)

```bash
# 1. Сервис жив?
systemctl --user is-active bybit-ws

# 2. RPC отвечает?
curl -s http://127.0.0.1:8766/health

# 3. Позиции
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/rpc/positions

# 4. Риск-статус
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/rpc/risk

# 5. Метрики за сегодня
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/rpc/metrics
```

### Watchdog (автоматический)

Скрипт `~/.hermes/scripts/bybit-watchdog.sh` проверяет каждые 30 минут:
- Сервис жив
- Нет 404-флуда (>5 за 15 мин)
- Нет watchdog-аварий
- Нет ошибок Auto-SHORT
- Нет фейлов SL re-entry
- Циклы не превышают порог
- RPC доступен

При проблемах — алерт в Telegram. Всё чисто — молчит.

### Дымовые тесты

```bash
cd ~/bybit-ws
source .venv/bin/activate
python test_smoke.py          # 45 тестов
python test_scanner_smoke.py  # Тесты сканера
python test_modules.py        # Модульные тесты
```

### Восстановление после сбоя

```bash
# 1. Проверить состояние
systemctl --user status bybit-ws
tail -20 ~/.local/share/bybit-ws/events.log

# 2. Если процесс зомби — убить
pkill -f bybit_ws

# 3. Очистить старый RPC-порт (если нужно)
fuser -k 8766/tcp

# 4. Синхронизировать из репо
cp ~/bybit-ws/bybit_ws/*.py ~/.local/lib/bybit_ws/

# 5. Перезапустить
systemctl --user restart bybit-ws

# 6. Проверить
sleep 5 && curl -s http://127.0.0.1:8766/health
```

### Бэкап и восстановление

Бэкапы SSOT (`state.db`) создаются автоматически каждый час в `~/.local/share/bybit-ws/backups/`.  
Для ручного бэкапа:
```bash
cp ~/.local/share/bybit-ws/state.db ~/.local/share/bybit-ws/backups/state_$(date +%Y%m%d_%H%M%S).db
```

Восстановление:
```bash
systemctl --user stop bybit-ws
cp backups/state_YYYYMMDD_HHMMSS.db ~/.local/share/bybit-ws/state.db
systemctl --user start bybit-ws
```
