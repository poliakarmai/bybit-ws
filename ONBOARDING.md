# Onboarding — от нуля до первой сделки

> Пошаговая инструкция. 15 минут — и bybit-ws торгует за тебя.

---

## Шаг 1: Bybit API-ключи (3 минуты)

1. Зайди в [Bybit API Management](https://www.bybit.com/app/user/api-management)
2. Нажми **Create New Key** → **System-generated API Key**
3. Настрой:
   - **API Key Type**: Unified Trading Account
   - **Permissions**: Read-Write + Trade (❗ обязательно)
   - **IP Whitelist**: добавь IP твоего VPS (`curl ifconfig.me`)
   - **No withdrawal permission** — безопасность
4. Сохрани **API Key** и **API Secret** (Secret показывается только один раз!)

> **Testnet сначала!** Создай отдельные ключи на [testnet.bybit.com](https://testnet.bybit.com) для тестов.

---

## Шаг 2: Сервер (3 минуты)

```bash
# Минимальные требования: Ubuntu 22.04+, 1GB RAM, 10GB диск
# Рекомендуем: VPS за $5/мес (Hetzner, DigitalOcean, Vultr)

# Установка
ssh root@your-server
apt update && apt install -y python3 python3-pip git

# Клонирование
git clone https://github.com/poliakarmai/bybit-ws.git
cd bybit-ws
pip install -r requirements.txt

# Проверка
python3 test_smoke.py
# Ожидаем: PASS=52 FAIL=0
```

---

## Шаг 3: Настройка (4 минуты)

```bash
# Конфигурация
cp config.example.yaml ~/.config/bybit-ws/config.yaml
nano ~/.config/bybit-ws/config.yaml
```

Важные параметры в `config.yaml`:

```yaml
api:
  base_url: "https://api-testnet.bybit.com"  # testnet сначала!

risk:
  max_daily_loss: 50         # дневной лимит убытка
  emergency_close_all: true  # закрыть всё при пробое

strategy:
  long:
    max_positions: 5         # умеренно для старта
  short:
    enabled: false           # начни только с LONG

position_sizing:
  long_risk_pct: 0.10        # 10% депозита в риске
```

```bash
# Секреты (chmod 600 — обязательно!)
cat > ~/.config/bybit-ws/.env << 'EOF'
BYBIT_API_KEY=your_testnet_key
BYBIT_API_SECRET=your_testnet_secret
RPC_TOKEN=$(openssl rand -hex 16)
EOF
chmod 600 ~/.config/bybit-ws/.env
```

---

## Шаг 4: Проверка перед запуском (2 минуты)

```bash
# 1. Тесты
python3 test_smoke.py                    # должно быть 52/52 PASS

# 2. Paper trading (бэктест)
python3 -m bybit_ws.paper_trade SOLUSDT --days 30

# 3. Проверка конфига
python3 -c "from bybit_ws.config import Config; c = Config(); print('OK')"

# 4. Проверка ключей (testnet)
python3 -c "
from bybit_ws.api import bybit
r = bybit('GET', '/v5/account/wallet-balance?accountType=UNIFIED')
print(r['result']['list'][0]['totalEquity'] if r['retCode']==0 else r)
"
# Должен показать баланс
```

---

## Шаг 5: Запуск (2 минуты)

```bash
# Установка как systemd-сервис
mkdir -p ~/.config/systemd/user
cp bybit-ws-async.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bybit-ws-async

# Проверка
systemctl --user status bybit-ws-async    # active (running)
journalctl --user -u bybit-ws-async -f   # смотри логи
curl http://localhost:8766/health         # {"status":"alive"}
```

---

## Шаг 6: Мониторинг (1 минута)

```bash
# Позиции
curl http://localhost:8766/positions

# Баланс
curl http://localhost:8766/balance

# Метрики
curl http://localhost:8766/metrics
```

### Telegram-алерты

Добавь в `.env`:
```bash
TG_BOT_TOKEN=123:abc
TG_CHAT_ID=123456789
```

Будешь получать уведомления:
- 🔵 Новый вход
- 🟢 TP сработал
- 🔴 SL сработал
- ⚠️ Памп детект
- 🛑 Circuit Breaker

---

## Шаг 7: Боевой запуск (переход с testnet)

Когда убедился что всё работает на testnet:

1. Создай **новые** API-ключи на [mainnet Bybit](https://www.bybit.com/app/user/api-management)
2. Пополни депозит (мин. $200 для комфортной торговли)
3. Поменяй в `config.yaml`:
   ```yaml
   api:
     base_url: "https://api.bybit.com"  # убрал -testnet
   ```
4. Обнови `.env` с боевыми ключами
5. Перезапусти: `systemctl --user restart bybit-ws-async`

---

## Частые вопросы

### Сколько нужно денег для старта?
- Testnet: $0 (виртуальные)
- Mainnet LONG-only: $200-500
- Mainnet LONG+SHORT: $500-1000

### Какой VPS выбрать?
- Hetzner CX22 (~$4/мес) — достаточно
- Главное: стабильный пинг к Bybit API (<100ms)

### Что делать если позиции не открываются?
1. Проверь: `curl localhost:8766/positions`
2. Проверь логи: `journalctl --user -u bybit-ws-async -n 50`
3. Убедись что баланс > $30
4. Проверь Circuit Breaker: `curl localhost:8766/risk`

### Можно ли торговать вручную параллельно?
Да. Движок не мешает ручным сделкам. Но:
- Не выставляй противонаправленные ордера (движок может закрыть)
- SL/TP управляются движком — не трогай руками

### Как обновляться?
```bash
cd ~/bybit-ws
git pull
python3 test_smoke.py
bash deploy.sh        # атомарный деплой с проверками
```

---

## Следующие шаги

- [ ] Настрой Telegram-алерты
- [ ] Запусти `paper_trade` на 90 днях истории
- [ ] Оптимизируй параметры под свой риск-профиль
- [ ] Подключи @Gridbolbot для сигналов друзьям
- [ ] Настрой мониторинг (Prometheus + Grafana)

---

*Вопросы? [@Poliakarm](https://t.me/Poliakarm) в Telegram.*
