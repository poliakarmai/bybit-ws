# SECURITY.md — bybit-ws

**English** · [Русский](SECURITY.md)

> Complete security documentation for the bybit-ws trading monitor.
> **Version:** 1.2 | **Date:** 2026-11-02 | **Applicable to:** bybit-ws 11.5.x

---

## 1. Where Secrets Are Stored

### 1.1 Bybit API Keys

| Resource | Path | Format | Loader |
|--------|------|------|-----------|
| bybit-cli config | `~/.config/bybit-cli/config` | `BYBIT_API_KEY=...` / `BYBIT_API_SECRET=...` (strings) | `api.py::_load_credentials()` |
| bybit-ws config | `~/.config/bybit-ws/config.yaml` | `${BYBIT_API_KEY}` / `${BYBIT_API_SECRET}` — references to env vars | `config.py::_env_subst()` |
| Environment variables | `BYBIT_API_KEY`, `BYBIT_API_SECRET` | process env | `${VAR}` substitution in YAML |

**Loading priority:**
1. `api.py` reads `~/.config/bybit-cli/config` directly on the first API call.
2. `config.py` substitutes environment variables in the YAML config using `${VAR}` syntax.

### 1.2 RPC Token

| Resource | Path | Format |
|--------|------|--------|
| SQLite kv_store | `~/.local/share/bybit-ws/state.db` → `kv_store` table → `rpc_auth_token` key | UUID v4 (string) |
| Config YAML (optional) | `~/.config/bybit-ws/config.yaml` → `rpc.auth_token` / `rpc.rpc_auth_token` | `"${RPC_TOKEN}"` — reference to an env var |

**Generation logic:**
- On the first run, the RPC server checks `config.yaml → rpc.auth_token`.
- If it contains `${RPC_TOKEN}` or is empty, it generates a UUID v4 and saves it to `state.db → kv_store → rpc_auth_token`.
- The token survives restarts (persistent in SQLite).

**Retrieving the token:**
```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('$HOME/.local/share/bybit-ws/state.db')
token = conn.execute(\"SELECT value FROM kv_store WHERE key='rpc_auth_token'\").fetchone()
print(token[0]) if token else print('No token')
"
```

### 1.3 Telegram Token

| Resource | Path | Note |
|--------|------|------------|
| Environment variable | `TELEGRAM_BOT_TOKEN` | For a custom bot |
| Hermes integration | Call `hermes send --to telegram:Poliakarm` | The token is managed by Hermes, not bybit-ws |

**`.env` file:**
- May be used to set `TELEGRAM_BOT_TOKEN` and other environment variables.
- **NEVER committed** — listed in `.gitignore`.
- Recommended path: `~/.config/bybit-ws/env`

### 1.4 Secret Summary

| Secret | Storage | Rotation | chmod |
|--------|-----------|---------|-------|
| Bybit API Key | `~/.config/bybit-cli/config` | Through Bybit UI | `600` |
| Bybit API Secret | `~/.config/bybit-cli/config` | Through Bybit UI | `600` |
| RPC Bearer Token | `state.db → kv_store` | `POST /reset-token` | `600` on state.db |
| Telegram Bot Token | `.env` / env vars | Through @BotFather | `600` on `.env` |
| Bybit WS Config | `~/.config/bybit-ws/config.yaml` | Manually | `600` |

---

## 2. Threat Model

### 2.1 Server Compromise

**Vector:** an attacker gains access to the server filesystem (shell, SSH, vulnerability).

**What will be compromised:**

| Resource | Consequences | Severity |
|---------|-------------|---------------------|
| `~/.config/bybit-cli/config` | Bybit API keys | 🔴 Critical |
| `~/.local/share/bybit-ws/state.db` | RPC token, trade history, positions, PnL | 🟠 High |
| `.env` | Telegram token | 🟡 Medium |
| `journalctl -u bybit-ws-async` | Logs (may contain prices, symbols, PnL) | 🟢 Low |

**Protection:**
- Bybit API keys have **trading permissions only** (without Wallet/Withdrawal) — funds cannot be withdrawn.
- RPC is bound to `127.0.0.1` — it is not accessible externally without port forwarding.
- `state.db` does not contain API secrets (only a UUID token).

### 2.2 Bybit API Keys: Limited Permissions

API keys are created in **Bybit → Account → API Management** with the following settings:

| Permission | Status | Reason |
|------------|--------|---------|
| **Trade** (trading) | ✅ Enabled | Required to open/close positions |
| **Read** (reading) | ✅ Enabled | Required to retrieve positions, orders, klines |
| **Wallet** (wallet) | ❌ **Disabled** | Critical! Enabling Wallet allows funds to be withdrawn |
| **Withdrawal** (withdrawal) | ❌ **Disabled** | Critical! Never enable |

> ⚠️ **Never create API keys with Wallet/Withdrawal permissions for bybit-ws.**

If the keys are compromised:
- An attacker can open/close positions.
- An attacker **CANNOT** withdraw funds.
- The maximum damage is limited to the balance in the trading account.

### 2.3 RPC Security

#### Authentication
- A **Bearer token** (UUID v4) is required for all protected endpoints.
- Public endpoints without authentication: `/health` (the only one). `/dashboard`, `/rpc/dash/*`, `/rpc/paths` require Bearer.
- The token is passed in the header: `Authorization: Bearer <uuid>`.

#### Rate Limiting
- **Token bucket**: 60 requests/min/IP (configurable in `config.yaml → rpc.rate_limit_per_min`).
- When exceeded: HTTP 429, `error_code: "rate_limit"`.
- Recovery: 1 token/sec.

#### Bind Address
- **Default is `127.0.0.1`** (localhost only).
- Setting `rpc.bind: "0.0.0.0"` — for Docker/remote access (**not recommended without a VPN**).
- Port: `8766`.

#### CORS
- Only `http://localhost` and `http://127.0.0.1` are allowed.
- External sources are blocked by the browser.

---

## 3. Best Practices

### 3.1 Permissions for Key Files

```bash
# Bybit API keys
chmod 600 ~/.config/bybit-cli/config

# bybit-ws config (may contain ${RPC_TOKEN})
chmod 600 ~/.config/bybit-ws/config.yaml

# Environment variable file
chmod 600 ~/.config/bybit-ws/env

# StateDB database (contains the RPC token)
chmod 600 ~/.local/share/bybit-ws/state.db
chmod 600 ~/.local/share/bybit-ws/state.db-wal
chmod 600 ~/.local/share/bybit-ws/state.db-shm
```

**Automation:**
```bash
# Check all secret files
find ~/.config/bybit-cli ~/.config/bybit-ws ~/.local/share/bybit-ws \
  -type f \( -name "config" -o -name "config.yaml" -o -name "state.db*" -o -name ".env" \) \
  -exec stat -c "%a %n" {} \;

# Fix permissions
chmod 600 ~/.config/bybit-cli/config
chmod 600 ~/.config/bybit-ws/config.yaml
chmod 600 ~/.local/share/bybit-ws/state.db*
```

### 3.2 No Keys in Code or Commits

**What provides protection:**
- `.gitignore` excludes: `.env`, `config.yaml`, `*.key`, `*.pem`, `credentials.json`, `secrets.yaml`, `data/*.db`.
- API keys are read from external files and environment variables and are **never hardcoded**.

**Check before every commit:**
```bash
# Search for potential keys in the staging area
git diff --cached | grep -E '(api_key|api_secret|secret_key|private_key|BYBIT_API)'

# Search the entire history
git log --all --full-history -p | grep -E '(BYBIT_API_KEY|BYBIT_API_SECRET)'
```

### 3.3 Key Rotation

#### Bybit API Keys

1. Open **Bybit → Account → API Management**.
2. Create a **new** API key (Trade + Read only, without Wallet).
3. Copy the new Key + Secret.
4. Update `~/.config/bybit-cli/config`:
   ```ini
   BYBIT_API_KEY=новый_ключ
   BYBIT_API_SECRET=новый_секрет
   ```
5. Delete the **old** API key in the Bybit UI.
6. Restart the monitor:
   ```bash
   systemctl --user restart bybit-ws-async
   ```
7. Check that the monitor is working:
   ```bash
   curl http://127.0.0.1:8766/health
   ```

#### RPC Token

**Method 1 — through the API (recommended):**
```bash
TOKEN=$(python3 -c "import sqlite3; print(sqlite3.connect('$HOME/.local/share/bybit-ws/state.db').execute(\"SELECT value FROM kv_store WHERE key='rpc_auth_token'\").fetchone()[0])")
curl -X POST -H "Authorization: Bearer *** http://127.0.0.1:8766/reset-token
```

The response contains `new_token` — **save it**.

**Method 2 — manually through SQLite:**
```bash
NEW_TOKEN=$(uuidgen)
sqlite3 ~/.local/share/bybit-ws/state.db \
  "INSERT OR REPLACE INTO kv_store (key, value) VALUES ('rpc_auth_token', '$NEW_TOKEN')"
echo "New token: $NEW_TOKEN"
```

After rotation, update the token in all clients (MCP server, dashboard, scripts).

### 3.4 Backups with age Encryption

Using `age` to encrypt backups is recommended (as in Hermes):

```bash
# Install age
sudo apt install age

# Create a key pair (first time)
age-keygen -o ~/.config/age/key.txt
chmod 600 ~/.config/age/key.txt

# Back up state.db
tar czf - ~/.local/share/bybit-ws/state.db \
  | age -r $(age-keygen -y ~/.config/age/key.txt) \
  > ~/backups/bybit-ws-state-$(date +%Y%m%d).tar.gz.age

# Restore
age -d -i ~/.config/age/key.txt \
  ~/backups/bybit-ws-state-20260101.tar.gz.age \
  | tar xzf - -C /tmp/
```

**Automated backups through cron:**
```cron
# Daily state.db backup with age encryption (02:00)
0 2 * * * tar czf - ~/.local/share/bybit-ws/state.db | age -r $(age-keygen -y ~/.config/age/key.txt) > ~/backups/bybit-ws-state-$(date +\%Y\%m\%d).tar.gz.age
```

---

## 4. Incident Response

### 4.1 If Bybit API Keys Are Compromised

**Immediate actions (in priority order):**

1. **Revoke the API keys in Bybit:**
   - Bybit → Account → API Management → Delete the compromised keys.
   - ⚡ This blocks access immediately.

2. **Stop the monitor:**
   ```bash
   systemctl --user stop bybit-ws-async
   ```

3. **Create new keys:**
   - Bybit → Account → API Management → Create New Key.
   - Trade + Read only, **without Wallet**.
   - Save the new Key + Secret.

4. **Update the config:**
   ```bash
   # Edit ~/.config/bybit-cli/config
   nano ~/.config/bybit-cli/config
   # Replace BYBIT_API_KEY and BYBIT_API_SECRET with the new values
   ```

5. **Check the trade history for unauthorized operations:**
   ```bash
   sqlite3 ~/.local/share/bybit-ws/state.db \
     "SELECT datetime(closed_at, 'unixepoch'), symbol, side, pnl FROM trade_history ORDER BY closed_at DESC LIMIT 50"
   ```

6. **Start the monitor:**
   ```bash
   systemctl --user start bybit-ws-async
   ```

7. **Check the journal for anomalies:**
   ```bash
   journalctl --user -u bybit-ws-async --since "1 hour ago" | grep -E '(ERROR|WARN|alert)'
   ```

### 4.2 If the RPC Token Is Compromised

**Indicators:** unknown requests to `/rpc/*`, suspicious activity in the logs.

1. **Reset the token:**
   ```bash
   curl -X POST -H "Authorization: Bearer $OLD_TOKEN" http://127.0.0.1:8766/reset-token
   ```

2. **Update the token in all clients:**
   - MCP server: `~/.local/bin/bybit-mcp-server.py`
   - Dashboard: `~/bybit-ws/web/proxy_server.py`
   - Cron scripts
   - AI agents

3. **Check the RPC logs for unauthorized calls:**
   ```bash
   journalctl --user -u bybit-ws-async --since "24 hours ago" | grep "rpc"
   ```

### 4.3 Restoring `state.db` from a Backup

```bash
# 1. Stop the monitor
systemctl --user stop bybit-ws-async

# 2. Make a copy of the current (compromised) database
cp ~/.local/share/bybit-ws/state.db ~/backups/state.db.compromised.$(date +%Y%m%d_%H%M%S)

# 3. Decrypt and restore the backup
age -d -i ~/.config/age/key.txt \
  ~/backups/bybit-ws-state-20260101.tar.gz.age \
  | tar xzf - -C /tmp/

cp /tmp/home/*/.local/share/bybit-ws/state.db ~/.local/share/bybit-ws/state.db
chmod 600 ~/.local/share/bybit-ws/state.db

# 4. Start the monitor
systemctl --user start bybit-ws-async
```

### 4.4 Complete Server Compromise

1. **Revoke all keys:** Bybit API keys + Telegram bot token (through @BotFather).
2. **Reinstall the OS** (the only guaranteed cleanup method).
3. **Create new keys** (see section 4.1).
4. **Restore `state.db` from a backup** (see section 4.3).
5. **Check adjacent systems** for compromise.

---

## 5. Security Audit

### 5.1 Search for Key Leaks in Git History

```bash
cd ~/bybit-ws

# Search the entire commit history
git log --all --full-history -p | grep -iE '(BYBIT_API_KEY|BYBIT_API_SECRET|api_key|api_secret|sk-[a-zA-Z0-9])'

# Search for potential private keys
git log --all --full-history -p | grep -E '-----BEGIN (RSA|EC|OPENSSH|DSA) PRIVATE KEY-----'

# Search for RPC UUID tokens
git log --all --full-history -p | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

# Search for .env or config.yaml in history (if accidentally committed)
git log --all --full-history -- '*.env' 'config.yaml' '*.key' '*.pem'
```

**If keys are found in history, see section 4.1 (revoke immediately).**

### 5.2 Check systemd Logs for Leaks

```bash
# Search for keys in systemd logs
journalctl --user -u bybit-ws-async --no-pager | grep -iE '(api_key|api_secret|token|secret|key=|Bearer)'

# Search for errors that could have disclosed sensitive data
journalctl --user -u bybit-ws-async --no-pager | grep -iE '(traceback|error|exception)' | grep -viE '(timeout|retry|connection)'

# Search for accidental secret output in logs
journalctl --user -u bybit-ws-async --no-pager | grep -E '[0-9a-f]{64}'  # HMAC-SHA256 signatures
```

### 5.3 Check Files for Secrets

```bash
# Search for unencrypted keys in working directories
grep -rE '(BYBIT_API_KEY|BYBIT_API_SECRET|private.*key)' \
  ~/.local/share/bybit-ws/ ~/.local/lib/bybit_ws/ \
  --include='*.json' --include='*.yaml' --include='*.txt' --include='*.log' \
  2>/dev/null

# Check permissions on secret files
find ~/.config/bybit-cli ~/.config/bybit-ws ~/.local/share/bybit-ws \
  -type f \( -name "config" -o -name "config.yaml" -o -name "state.db" -o -name ".env" \) \
  -exec sh -c 'perms=$(stat -c "%a" "$1"); if [ "$perms" != "600" ]; then echo "⚠️ $1 has perms $perms (should be 600)"; fi' _ {} \;
```

### 5.4 Regular Audit (Recommended Schedule)

| Frequency | Check | Command |
|---------|----------|---------|
| **Monthly** | Search for keys in Git history | `git log --all --full-history -p \| grep -iE 'api_key\|api_secret'` |
| **Monthly** | Search for secrets in logs | `journalctl --user -u bybit-ws-async --since "1 month ago" \| grep -iE '(key\|secret\|token)'` |
| **Weekly** | Check permissions | `find ~/.config -name 'config*' -exec stat -c '%a %n' {} \;` |
| **Before every commit** | Check the staging area | `git diff --cached \| grep -iE '(key\|secret\|token)'` |

---

## 6. Network Security

> ⚠️ **Open gap as of 01.11.2026:** the service environment has no `EMERGENCY_TOKEN`, so `/kill_switch`
> and `/emergency_close` are protected by the **Bearer token only** (`rpc.py:540`: the second-header check
> runs only when the variable is non-empty). While `8766` listens on loopback this is tolerable;
> the moment the port is exposed, the token is mandatory.

### 6.1 RPC — Bind to Localhost Only

> **Actual port state (verified 01.11.2026):** ufw is active; allow-list is `2091/tcp`, `1080/tcp`, `8443/tcp`;
> `127.0.0.1:8766` (RPC) listens on loopback only and is unreachable from outside; `8888` listens on `0.0.0.0`
> but is blocked by ufw (no inbound rule for it). The `iptables` rules below are an alternative for hosts that
> do not use ufw; verify with facts: `sudo ufw status` and `ss -ltn`.

By default, the RPC server listens on `127.0.0.1:8766`, making it inaccessible from the external network.

```yaml
# ~/.config/bybit-ws/config.yaml
monitoring:
  rpc_bind: "127.0.0.1"     # Localhost only
  rpc_port: 8766
  rpc_auth_token: "${RPC_TOKEN}"
  rpc_rate_limit: 60
```

**Check current listening ports:**
```bash
ss -tlnp | grep 8766
# Expected output:
# LISTEN  0  5  127.0.0.1:8766  0.0.0.0:*  users:(("python3",pid=...,fd=...))
```

### 6.2 Iptables — Firewall-Level Protection

```bash
# Allow incoming connections only from localhost for port 8766
sudo iptables -A INPUT -p tcp --dport 8766 -s 127.0.0.1 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8766 -j DROP

# Allow incoming connections only from localhost for the dashboard port (8765)
sudo iptables -A INPUT -p tcp --dport 8765 -s 127.0.0.1 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8765 -j DROP

# Save the rules
sudo iptables-save > /etc/iptables/rules.v4  # Debian/Ubuntu
# or
sudo service iptables save                     # RHEL/CentOS
```

### 6.3 VPN for Remote Access

If remote access to RPC/dashboard is required:

**WireGuard is recommended:**

```ini
# /etc/wireguard/wg0.conf (client side)
[Interface]
Address = 10.0.0.2/24
PrivateKey = <client_private_key>

[Peer]
PublicKey = <server_public_key>
Endpoint = <server_ip>:51820
AllowedIPs = 10.0.0.0/24
```

After configuring the VPN:
```yaml
# Change the bind address to the VPN address (NOT 0.0.0.0!)
monitoring:
  rpc_bind: "10.0.0.1"    # Listen only on the VPN interface
```

**Check access through the VPN:**
```bash
# From the client machine
curl -H "Authorization: Bearer *** http://10.0.0.1:8766/rpc/health
```

### 6.4 SSH Access to the Server

```bash
# Keys only, without passwords
# /etc/ssh/sshd_config:
#   PasswordAuthentication no
#   PermitRootLogin prohibit-password
#   PubkeyAuthentication yes

# Two-factor authentication (optional)
sudo apt install libpam-google-authenticator
```

---

## 7. Configuration Security

### 7.1 Check That Secrets Are Not Present in Logs

The monitor **does not log** API key or token values. The config returned by the `/rpc/config` endpoint contains masked secrets:

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

### 7.2 Protection Against Injection

- **Command injection was eliminated** in v3.0: transition from `subprocess(bybit-cli)` to native `requests` + HMAC-SHA256.
- Telegram alerts are sent through `hermes send` with message escaping.
- All order parameters are validated before being sent to the exchange.

### 7.3 Secure Logging

- `events.log` — general events (does NOT contain keys/secrets).
- `alerts.log` — alerts (may contain symbols and PnL, but not keys).
- `trades.jsonl` — trade history (prices, PnL, symbols).
- `metrics.json` — aggregated metrics.

**Log rotation:**
- `events.log`: automatic rotation at >50 MB (configurable).
- `trades.jsonl`: automatic rotation at >100 MB + archiving to `.gz`.

---

## 8. Security Checklist

### Before the First Run

- [ ] Bybit API keys created **without Wallet/Withdrawal permissions**.
- [ ] `chmod 600 ~/.config/bybit-cli/config`.
- [ ] `chmod 600 ~/.config/bybit-ws/config.yaml`.
- [ ] `.env` and `config.yaml` added to `.gitignore`.
- [ ] RPC bind set to `127.0.0.1` (not `0.0.0.0`).
- [ ] Firewall configured (iptables/ufw).

### Monthly

- [ ] Check Git history for key leaks.
- [ ] Check systemd logs for secret leaks.
- [ ] Check permissions on key files.
- [ ] Back up state.db with age encryption.
- [ ] Check that API keys are active and have not expired.

### During an Incident

- [ ] Revoke the API keys in Bybit.
- [ ] Reset the RPC token (`POST /reset-token`).
- [ ] Revoke the Telegram token (through @BotFather).
- [ ] Check trade history for unauthorized operations.
- [ ] Restore state.db from a backup.
- [ ] Update all keys and tokens.

---

## 9. GSC Security Audit (2026-06-28)

The project passed a three-level Git Security Checker audit (GSC v0.6).

| Level | Findings | Verdict |
|---------|---------|---------|
| 🔴 CRITICAL | 3 | False positives (DSPy mock code) |
| 🟠 HIGH | 32 | Generic patterns, zero real threats |
| 🟡 MEDIUM | 21 | Code style (print vs logging) |
| ⚪ LOW | 428 | Generic smells, TODOs |
| **Total** | **484** | ✅ Project is clean |

**Key results:**
- `GS001` (hardcoded secrets): **0** — no API keys in code
- `GS002` (world-readable sensitive): **0**
- `GS004` (shell=True subprocess): **0**
- `GS005` (SQL injection): **0** (in production code)
- Unsafe `pickle.load()`: **0**
- `yaml.load()` without SafeLoader: **0**

## 10. Links

- [Bybit API Management](https://www.bybit.com/app/user/api-management) — API key management
- [Bybit v5 API Docs](https://bybit-exchange.github.io/docs/v5/) — API documentation
- [age encryption](https://github.com/FiloSottile/age) — backup encryption
- [Hermes backup skill](https://hermes-agent.nousresearch.com/docs) — automated backups
- [ARCHITECTURE.md](./ARCHITECTURE.md) — bybit-ws architecture
- [API.md](./API.md) — RPC API specification
- [AGENTS.md](../AGENTS.md) — navigation for AI agents
