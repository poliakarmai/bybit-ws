# SECURITY.md — bybit-ws

> Полная документация по безопасности трейдинг-монитора bybit-ws.
> **Версия:** 1.0 | **Дата:** 2026-06-18 | **Актуально для:** bybit-ws v4.0+

---

## 1. Где лежат секреты

### 1.1 Bybit API-ключи

| Ресурс | Путь | Формат | Загрузчик |
|--------|------|--------|-----------|
| Конфиг bybit-cli | `~/.config/bybit-cli/config` | `BYBIT_API_KEY=...` / `BYBIT_API_SECRET=...` (строки) | `api.py::_load_credentials()` |
| Конфиг bybit-ws | `~/.config/bybit-ws/config.yaml` | `${BYBIT_API_KEY}` / `${BYBIT_API_SECRET}` — ссылки на env vars | `config.py::_env_subst()` |
| Переменные окружения | `BYBIT_API_KEY`, `BYBIT_API_SECRET` | process env | Подстановка `${VAR}` в YAML |

**Приоритет загрузки:**
1. `api.py` напрямую читает `~/.config/bybit-cli/config` при первом вызове API.
2. `config.py` подставляет переменные окружения в YAML-конфиг через `${VAR}` синтаксис.

### 1.2 RPC-токен

| Ресурс | Путь | Формат |
|--------|------|--------|
| SQLite kv_store | `~/.local/share/bybit-ws/state.db` → таблица `kv_store` → ключ `rpc_auth_token` | UUID v4 (строка) |
| Config YAML (опционально) | `~/.config/bybit-ws/config.yaml` → `rpc.auth_token` / `rpc.rpc_auth_token` | `"${RPC_TOKEN}"` — ссылка на env var |

**Логика генерации:**
- При первом запуске RPC-сервер проверяет `config.yaml → rpc.auth_token`.
- Если там `${RPC_TOKEN}` или пусто — генерирует UUID v4 и сохраняет в `state.db → kv_store → rpc_auth_token`.
- Токен выживает перезапуски (persistent в SQLite).

**Получение токена:**
```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('$HOME/.local/share/bybit-ws/state.db')
token = conn.execute(\"SELECT value FROM kv_store WHERE key='rpc_auth_token'\").fetchone()
print(token[0]) if token else print('No token')
"
```

### 1.3 Telegram-токен

| Ресурс | Путь | Примечание |
|--------|------|------------|
| Переменная окружения | `TELEGRAM_BOT_TOKEN` | Для собственного бота |
| Hermes-интеграция | Вызов `hermes send --to telegram:Poliakarm` | Токен управляется Hermes, не bybit-ws |

**Файл `.env`:**
- Может использоваться для установки `TELEGRAM_BOT_TOKEN` и других переменных окружения.
- **НИКОГДА не коммитится** — прописан в `.gitignore`.
- Рекомендуемый путь: `~/bybit-ws/.env`

### 1.4 Сводная таблица секретов

| Секрет | Хранилище | Ротация | chmod |
|--------|-----------|---------|-------|
| Bybit API Key | `~/.config/bybit-cli/config` | Через Bybit UI | `600` |
| Bybit API Secret | `~/.config/bybit-cli/config` | Через Bybit UI | `600` |
| RPC Bearer Token | `state.db → kv_store` | `POST /reset-token` | `600` на state.db |
| Telegram Bot Token | `.env` / env vars | Через @BotFather | `600` на `.env` |
| Bybit WS Config | `~/.config/bybit-ws/config.yaml` | Вручную | `600` |

---

## 2. Модель угроз

### 2.1 Компрометация сервера

**Вектор:** злоумышленник получает доступ к файловой системе сервера (shell, SSH, уязвимость).

**Что будет скомпрометировано:**

| Ресурс | Последствия | Степень критичности |
|--------|-------------|---------------------|
| `~/.config/bybit-cli/config` | API-ключи Bybit | 🔴 Критическая |
| `~/.local/share/bybit-ws/state.db` | RPC-токен, история сделок, позиции, PnL | 🟠 Высокая |
| `.env` | Telegram-токен | 🟡 Средняя |
| `journalctl -u bybit-ws` | Логи (могут содержать цены, символы, PnL) | 🟢 Низкая |

**Защита:**
- API-ключи Bybit имеют **только торговые права** (без Wallet/Withdrawal) — нельзя вывести средства.
- RPC привязан к `127.0.0.1` — недоступен извне без проброса портов.
- `state.db` не содержит API-секретов (только UUID-токен).

### 2.2 Bybit API-ключи: ограниченные права

API-ключи создаются в **Bybit → Account → API Management** со следующими настройками:

| Разрешение | Статус | Причина |
|------------|--------|---------|
| **Trade** (торговля) | ✅ Включено | Необходимо для открытия/закрытия позиций |
| **Read** (чтение) | ✅ Включено | Необходимо для получения позиций, ордеров, klines |
| **Wallet** (кошелёк) | ❌ **Выключено** | Критично! Включение Wallet позволяет вывод средств |
| **Withdrawal** (вывод) | ❌ **Выключено** | Критично! Никогда не включать |

> ⚠️ **Никогда не создавайте API-ключи с правами Wallet/Withdrawal для bybit-ws.**

При компрометации ключей:
- Злоумышленник может открывать/закрывать позиции.
- Злоумышленник **НЕ может** вывести средства.
- Максимальный ущерб ограничен балансом на торговом счёте.

### 2.3 RPC-безопасность

#### Аутентификация
- **Bearer-токен** (UUID v4) обязателен для всех защищённых эндпоинтов.
- Публичные эндпоинты без авторизации: `/health`, `/rpc/paths`.
- Токен передаётся в заголовке: `Authorization: Bearer <uuid>`.

#### Rate-limiting
- **Token bucket**: 60 запросов/мин/IP (настраивается в `config.yaml → rpc.rate_limit_per_min`).
- При превышении: HTTP 429, `error_code: "rate_limit"`.
- Восстановление: 1 токен/сек.

#### Bind-адрес
- **По умолчанию `127.0.0.1`** (только localhost).
- Настройка `rpc.bind: "0.0.0.0"` — для Docker/удалённого доступа (**не рекомендуется без VPN**).
- Порт: `8766`.

#### CORS
- Разрешён только `http://localhost` и `http://127.0.0.1`.
- Внешние источники блокируются браузером.

---

## 3. Лучшие практики (Best Practices)

### 3.1 Права доступа на файлы с ключами

```bash
# API-ключи Bybit
chmod 600 ~/.config/bybit-cli/config

# Конфиг bybit-ws (может содержать ${RPC_TOKEN})
chmod 600 ~/.config/bybit-ws/config.yaml

# Файл с переменными окружения
chmod 600 ~/bybit-ws/.env

# База данных StateDB (содержит RPC-токен)
chmod 600 ~/.local/share/bybit-ws/state.db
chmod 600 ~/.local/share/bybit-ws/state.db-wal
chmod 600 ~/.local/share/bybit-ws/state.db-shm
```

**Автоматизация:**
```bash
# Проверить все секретные файлы
find ~/.config/bybit-cli ~/.config/bybit-ws ~/.local/share/bybit-ws \
  -type f \( -name "config" -o -name "config.yaml" -o -name "state.db*" -o -name ".env" \) \
  -exec stat -c "%a %n" {} \;

# Исправить permissions
chmod 600 ~/.config/bybit-cli/config
chmod 600 ~/.config/bybit-ws/config.yaml
chmod 600 ~/.local/share/bybit-ws/state.db*
```

### 3.2 Никаких ключей в коде или коммитах

**Что защищает:**
- `.gitignore` исключает: `.env`, `config.yaml`, `*.key`, `*.pem`, `credentials.json`, `secrets.yaml`, `data/*.db`.
- API-ключи читаются из внешних файлов и переменных окружения, **никогда не хардкодятся**.

**Проверка перед каждым коммитом:**
```bash
# Поиск потенциальных ключей в staging area
git diff --cached | grep -E '(api_key|api_secret|secret_key|private_key|BYBIT_API)'

# Поиск по всей истории
git log --all --full-history -p | grep -E '(BYBIT_API_KEY|BYBIT_API_SECRET)'
```

### 3.3 Ротация ключей

#### Bybit API-ключи

1. Зайти в **Bybit → Account → API Management**.
2. Создать **новый** API-ключ (только Trade + Read, без Wallet).
3. Скопировать новый Key + Secret.
4. Обновить `~/.config/bybit-cli/config`:
   ```ini
   BYBIT_API_KEY=новый_ключ
   BYBIT_API_SECRET=новый_секрет
   ```
5. Удалить **старый** API-ключ в Bybit UI.
6. Перезапустить монитор:
   ```bash
   systemctl --user restart bybit-ws
   ```
7. Проверить, что монитор работает:
   ```bash
   curl http://127.0.0.1:8766/health
   ```

#### RPC-токен

**Способ 1 — через API (рекомендуемый):**
```bash
TOKEN=$(python3 -c "import sqlite3; print(sqlite3.connect('$HOME/.local/share/bybit-ws/state.db').execute(\"SELECT value FROM kv_store WHERE key='rpc_auth_token'\").fetchone()[0])")
curl -X POST -H "Authorization: Bearer *** http://127.0.0.1:8766/reset-token
```

Ответ содержит `new_token` — **сохранить его**.

**Способ 2 — вручную через SQLite:**
```bash
NEW_TOKEN=$(uuidgen)
sqlite3 ~/.local/share/bybit-ws/state.db \
  "INSERT OR REPLACE INTO kv_store (key, value) VALUES ('rpc_auth_token', '$NEW_TOKEN')"
echo "New token: $NEW_TOKEN"
```

После ротации — обновить токен во всех клиентах (MCP-сервер, дашборд, скрипты).

### 3.4 Бэкапы с age-шифрованием

Рекомендуется использовать `age` для шифрования бэкапов (как в Hermes):

```bash
# Установка age
sudo apt install age

# Создание ключевой пары (первый раз)
age-keygen -o ~/.config/age/key.txt
chmod 600 ~/.config/age/key.txt

# Бэкап state.db
tar czf - ~/.local/share/bybit-ws/state.db \
  | age -r $(age-keygen -y ~/.config/age/key.txt) \
  > ~/backups/bybit-ws-state-$(date +%Y%m%d).tar.gz.age

# Восстановление
age -d -i ~/.config/age/key.txt \
  ~/backups/bybit-ws-state-20260101.tar.gz.age \
  | tar xzf - -C /tmp/
```

**Автоматизация бэкапов через cron:**
```cron
# Ежедневный бэкап state.db с age-шифрованием (02:00)
0 2 * * * tar czf - ~/.local/share/bybit-ws/state.db | age -r $(age-keygen -y ~/.config/age/key.txt) > ~/backups/bybit-ws-state-$(date +\%Y\%m\%d).tar.gz.age
```

---

## 4. Реагирование на инциденты

### 4.1 При компрометации API-ключей Bybit

**Немедленные действия (в порядке приоритета):**

1. **Отозвать API-ключи в Bybit:**
   - Bybit → Account → API Management → Delete скомпрометированные ключи.
   - ⚡ Это мгновенно блокирует доступ.

2. **Остановить монитор:**
   ```bash
   systemctl --user stop bybit-ws
   ```

3. **Создать новые ключи:**
   - Bybit → Account → API Management → Create New Key.
   - Только Trade + Read, **без Wallet**.
   - Сохранить новый Key + Secret.

4. **Обновить конфиг:**
   ```bash
   # Редактировать ~/.config/bybit-cli/config
   nano ~/.config/bybit-cli/config
   # Заменить BYBIT_API_KEY и BYBIT_API_SECRET на новые
   ```

5. **Проверить историю сделок на предмет несанкционированных операций:**
   ```bash
   sqlite3 ~/.local/share/bybit-ws/state.db \
     "SELECT datetime(closed_at, 'unixepoch'), symbol, side, pnl FROM trade_history ORDER BY closed_at DESC LIMIT 50"
   ```

6. **Запустить монитор:**
   ```bash
   systemctl --user start bybit-ws
   ```

7. **Проверить журнал на предмет аномалий:**
   ```bash
   journalctl --user -u bybit-ws --since "1 hour ago" | grep -E '(ERROR|WARN|alert)'
   ```

### 4.2 При компрометации RPC-токена

**Признаки:** неизвестные запросы к `/rpc/*`, подозрительная активность в логах.

1. **Сбросить токен:**
   ```bash
   curl -X POST -H "Authorization: Bearer $OLD_TOKEN" http://127.0.0.1:8766/reset-token
   ```

2. **Обновить токен во всех клиентах:**
   - MCP-сервер: `~/.local/bin/bybit-mcp-server.py`
   - Дашборд: `~/bybit-ws/web/proxy_server.py`
   - Cron-скрипты
   - AI-агенты

3. **Проверить логи RPC на предмет несанкционированных вызовов:**
   ```bash
   journalctl --user -u bybit-ws --since "24 hours ago" | grep "rpc"
   ```

### 4.3 Восстановление state.db из бэкапа

```bash
# 1. Остановить монитор
systemctl --user stop bybit-ws

# 2. Сделать копию текущей (скомпрометированной) базы
cp ~/.local/share/bybit-ws/state.db ~/backups/state.db.compromised.$(date +%Y%m%d_%H%M%S)

# 3. Расшифровать и восстановить бэкап
age -d -i ~/.config/age/key.txt \
  ~/backups/bybit-ws-state-20260101.tar.gz.age \
  | tar xzf - -C /tmp/

cp /tmp/home/*/.local/share/bybit-ws/state.db ~/.local/share/bybit-ws/state.db
chmod 600 ~/.local/share/bybit-ws/state.db

# 4. Запустить монитор
systemctl --user start bybit-ws
```

### 4.4 Полная компрометация сервера

1. **Отозвать все ключи:** Bybit API keys + Telegram bot token (через @BotFather).
2. **Переустановить ОС** (единственный гарантированный способ очистки).
3. **Создать новые ключи** (см. раздел 4.1).
4. **Восстановить state.db из бэкапа** (см. раздел 4.3).
5. **Проверить смежные системы** на предмет компрометации.

---

## 5. Аудит безопасности

### 5.1 Поиск утечек ключей в Git-истории

```bash
cd ~/bybit-ws

# Поиск по всей истории коммитов
git log --all --full-history -p | grep -iE '(BYBIT_API_KEY|BYBIT_API_SECRET|api_key|api_secret|sk-[a-zA-Z0-9])'

# Поиск потенциальных приватных ключей
git log --all --full-history -p | grep -E '-----BEGIN (RSA|EC|OPENSSH|DSA) PRIVATE KEY-----'

# Поиск UUID-токенов RPC
git log --all --full-history -p | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

# Поиск .env или config.yaml в истории (если случайно закоммитили)
git log --all --full-history -- '*.env' 'config.yaml' '*.key' '*.pem'
```

**Если ключи найдены в истории — см. раздел 4.1 (немедленно отозвать).**

### 5.2 Проверка systemd-логов на утечки

```bash
# Поиск ключей в логах systemd
journalctl --user -u bybit-ws --no-pager | grep -iE '(api_key|api_secret|token|secret|key=|Bearer)'

# Поиск ошибок, которые могли раскрыть чувствительные данные
journalctl --user -u bybit-ws --no-pager | grep -iE '(traceback|error|exception)' | grep -viE '(timeout|retry|connection)'

# Поиск случайного вывода секретов в логи
journalctl --user -u bybit-ws --no-pager | grep -E '[0-9a-f]{64}'  # HMAC-SHA256 подписи
```

### 5.3 Проверка файлов на наличие секретов

```bash
# Поиск незашифрованных ключей в рабочих директориях
grep -rE '(BYBIT_API_KEY|BYBIT_API_SECRET|private.*key)' \
  ~/.local/share/bybit-ws/ ~/.local/lib/bybit_ws/ \
  --include='*.json' --include='*.yaml' --include='*.txt' --include='*.log' \
  2>/dev/null

# Проверка permissions на секретных файлах
find ~/.config/bybit-cli ~/.config/bybit-ws ~/.local/share/bybit-ws \
  -type f \( -name "config" -o -name "config.yaml" -o -name "state.db" -o -name ".env" \) \
  -exec sh -c 'perms=$(stat -c "%a" "$1"); if [ "$perms" != "600" ]; then echo "⚠️ $1 has perms $perms (should be 600)"; fi' _ {} \;
```

### 5.4 Регулярный аудит (рекомендуемый график)

| Частота | Проверка | Команда |
|---------|----------|---------|
| **Ежемесячно** | Поиск ключей в Git-истории | `git log --all --full-history -p \| grep -iE 'api_key\|api_secret'` |
| **Ежемесячно** | Поиск секретов в логах | `journalctl --user -u bybit-ws --since "1 month ago" \| grep -iE '(key\|secret\|token)'` |
| **Еженедельно** | Проверка permissions | `find ~/.config -name 'config*' -exec stat -c '%a %n' {} \;` |
| **Перед каждым коммитом** | Проверка staging area | `git diff --cached \| grep -iE '(key\|secret\|token)'` |

---

## 6. Сетевая безопасность

### 6.1 RPC — bind только localhost

По умолчанию RPC-сервер слушает `127.0.0.1:8766`, что делает его недоступным из внешней сети.

```yaml
# ~/.config/bybit-ws/config.yaml
monitoring:
  rpc_bind: "127.0.0.1"     # Только localhost
  rpc_port: 8766
  rpc_auth_token: "${RPC_TOKEN}"
  rpc_rate_limit: 60
```

**Проверка текущих слушающих портов:**
```bash
ss -tlnp | grep 8766
# Ожидаемый вывод:
# LISTEN  0  5  127.0.0.1:8766  0.0.0.0:*  users:(("python3",pid=...,fd=...))
```

### 6.2 Iptables — защита на уровне фаервола

```bash
# Разрешить входящие только на localhost для порта 8766
sudo iptables -A INPUT -p tcp --dport 8766 -s 127.0.0.1 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8766 -j DROP

# Разрешить входящие только на порт дашборда (8765) с localhost
sudo iptables -A INPUT -p tcp --dport 8765 -s 127.0.0.1 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8765 -j DROP

# Сохранить правила
sudo iptables-save > /etc/iptables/rules.v4  # Debian/Ubuntu
# или
sudo service iptables save                     # RHEL/CentOS
```

### 6.3 VPN для удалённого доступа

Если необходим удалённый доступ к RPC/дашборду:

**Рекомендуется WireGuard:**

```ini
# /etc/wireguard/wg0.conf (клиентская сторона)
[Interface]
Address = 10.0.0.2/24
PrivateKey = <client_private_key>

[Peer]
PublicKey = <server_public_key>
Endpoint = <server_ip>:51820
AllowedIPs = 10.0.0.0/24
```

После настройки VPN:
```yaml
# Меняем bind на VPN-адрес (НЕ 0.0.0.0!)
monitoring:
  rpc_bind: "10.0.0.1"    # Слушать только на VPN-интерфейсе
```

**Проверка доступа через VPN:**
```bash
# С клиентской машины
curl -H "Authorization: Bearer *** http://10.0.0.1:8766/rpc/health
```

### 6.4 SSH-доступ к серверу

```bash
# Только ключи, без паролей
# /etc/ssh/sshd_config:
#   PasswordAuthentication no
#   PermitRootLogin prohibit-password
#   PubkeyAuthentication yes

# Двухфакторная аутентификация (опционально)
sudo apt install libpam-google-authenticator
```

---

## 7. Безопасность конфигурации

### 7.1 Проверка конфига на отсутствие секретов в логах

Монитор **не логирует** значения API-ключей и токенов. Конфиг в RPC-эндпоинте `/rpc/config` возвращается с замаскированными секретами:

```json
{
  "api": {
    "key": "***",
    "secret": "***"
  },
  "rpc": {
    "auth_token": "***"
  }
}
```

### 7.2 Защита от инъекций

- **Command injection устранена** в v3.0: переход с `subprocess(bybit-cli)` на нативные `requests` + HMAC-SHA256.
- Telegram-алерты отправляются через `hermes send` с экранированием сообщения.
- Все параметры ордеров валидируются перед отправкой на биржу.

### 7.3 Безопасное логирование

- `events.log` — общие события (НЕ содержит ключей/секретов).
- `alerts.log` — алерты (могут содержать символы и PnL, но не ключи).
- `trades.jsonl` — история сделок (цены, PnL, символы).
- `metrics.json` — агрегированные метрики.

**Ротация логов:**
- `events.log`: авто-ротация при >50 MB (настраивается).
- `trades.jsonl`: авто-ротация при >100 MB + архивация в `.gz`.

---

## 8. Контрольный список безопасности (Security Checklist)

### Перед первым запуском

- [ ] API-ключи Bybit созданы **без прав Wallet/Withdrawal**.
- [ ] `chmod 600 ~/.config/bybit-cli/config`.
- [ ] `chmod 600 ~/.config/bybit-ws/config.yaml`.
- [ ] `.env` и `config.yaml` добавлены в `.gitignore`.
- [ ] RPC bind установлен в `127.0.0.1` (не `0.0.0.0`).
- [ ] Настроен фаервол (iptables/ufw).

### Ежемесячно

- [ ] Проверить Git-историю на утечки ключей.
- [ ] Проверить systemd-логи на утечки секретов.
- [ ] Проверить permissions на файлах с ключами.
- [ ] Сделать бэкап state.db с age-шифрованием.
- [ ] Проверить, что API-ключи активны и не просрочены.

### При инциденте

- [ ] Отозвать API-ключи в Bybit.
- [ ] Сбросить RPC-токен (`POST /reset-token`).
- [ ] Отозвать Telegram-токен (через @BotFather).
- [ ] Проверить историю сделок на несанкционированные операции.
- [ ] Восстановить state.db из бэкапа.
- [ ] Обновить все ключи и токены.

---

## 9. Ссылки

- [Bybit API Management](https://www.bybit.com/app/user/api-management) — управление API-ключами
- [Bybit v5 API Docs](https://bybit-exchange.github.io/docs/v5/) — документация API
- [age encryption](https://github.com/FiloSottile/age) — шифрование бэкапов
- [Hermes backup skill](https://hermes-agent.nousresearch.com/docs) — автоматические бэкапы
- [ARCHITECTURE.md](./ARCHITECTURE.md) — архитектура bybit-ws
- [API.md](./API.md) — спецификация RPC API
- [AGENTS.md](../AGENTS.md) — навигация для AI-агентов
