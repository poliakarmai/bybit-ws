# Webhook & Alert Payloads

When `alerts.telegram_enabled: true`, bybit-ws emits events. This document describes the payload structure for each event type — useful for building webhook consumers, dashboards, or AI agent integrations.

---

## Alert Types

| Type | Trigger | Channel |
|------|---------|---------|
| `ENTRY` | Position opened (auto or manual) | info |
| `SL` | Stop-loss hit | warning |
| `TP` | Take-profit hit | success |
| `CLOSE` | Manual close or timeout | info |
| `STOP` | Risk breach (drawdown, margin, correlation) | critical |
| `WARN` | Soft warning (high margin, stale data) | warning |

---

## Payload Format

All alerts follow a text template with machine-parseable structure.

### ENTRY — New position

```
🤖 Авто-вход ADAUSDT @ $0.4200 x100 (BB=12.3%)
```
*or*
```
🐻 Auto-SHORT SIRENUSDT @ $0.0230 x500 (BB=118%) SL=$0.0242 TP=$0.0180
```

**Parse hint:** `entry` = price after `@`, `qty` = number after `x`, `BB%` = in parentheses.

---

### SL — Stop-loss hit

```
🔴 ADAUSDT SL −$2.50 (вход $0.4200)
```

**Fields:**
- `symbol`: ADAUSDT
- `reason`: SL
- `pnl`: -$2.50
- `entry_price`: $0.4200

---

### TP — Take-profit hit

```
🎯 ADAUSDT TP +$3.40 (вход $0.4200)
```

**Fields:**
- `symbol`: ADAUSDT
- `reason`: TP
- `pnl`: +$3.40
- `entry_price`: $0.4200

---

### CLOSE — Manual or timeout close

```
📋 ADAUSDT закрыта +$1.25 (вход $0.4200)
```
*or*
```
⏰ SIRENUSDT: SHORT закрыт по таймауту (50ч > 72ч)
```

---

### STOP — Risk/correlation alert

```
🛑 Корреляция 88% LONG — авто-вход заблокирован
```
```
⚠️ Корреляция 📈📈 DOTUSDT↔XRPUSDT: r=+0.883 (>±0.8) — концентрационный риск
```
```
🚨 Маржа >95% ($487/$500) — риск ликвидации!
```
```
🚨 Шортов 4/15 > 20% лимит
```
```
💸 Депозит $28.00 < $30 — входы заблокированы
```
```
🛑 X10: дневной лимит убытков (3/3) — все стратегии остановлены на 24ч
```

---

### WARN — Soft warning

```
⏱️ check_auto_short: таймаут — ...
```
```
⚠️ Pump-SHORT SYMUSDT: DCA +100% ($0.0460 x500)
```

---

## Journal Files (Machine-Readable)

For reliable parsing, use the structured journal files instead of parsing alert text.

### `~/.local/share/bybit-ws/trades.jsonl`

One JSON object per line, per closed trade:

```json
{
  "symbol": "ADAUSDT",
  "side": "Buy",
  "entry": 0.4200,
  "exit": 0.4450,
  "pnl": 2.50,
  "pnl_pct": 5.95,
  "reason": "TP",
  "strategy": "bb_long",
  "timestamp": "2026-06-09T14:30:00Z",
  "entry_time": "2026-06-09T12:00:00Z"
}
```

**Strategy values:** `bb_long`, `bb_short`, `junk_short`, `scalp`, `mean_revert`, `funding_momentum`

### `~/.local/share/bybit-ws/events.log`

All events, one JSON object per line:

```json
{"ts":"2026-06-09T14:30:00Z","level":"INFO","msg":"Авто-вход ADAUSDT @ $0.4200 x100 (BB=12.3%)"}
```

### `~/.local/share/bybit-ws/metrics.json`

Daily aggregated metrics:

```json
{
  "date": "2026-06-09",
  "sl_count": 3,
  "tp_count": 5,
  "entries_today": 8,
  "total_pnl": 42.50,
  "closed_trades": 8
}
```

---

## Webhook Consumption (Python)

```python
import json, os

TRADES_FILE = os.path.expanduser("~/.local/share/bybit-ws/trades.jsonl")

def watch_trades():
    """Tail trades.jsonl for new entries."""
    with open(TRADES_FILE) as f:
        f.seek(0, 2)  # end of file
        while True:
            line = f.readline()
            if line:
                trade = json.loads(line)
                yield trade

for trade in watch_trades():
    if trade["reason"] == "SL":
        print(f"⚠️ {trade['symbol']} stopped out: ${trade['pnl']:+.2f}")
    elif trade["reason"] == "TP":
        print(f"✅ {trade['symbol']} took profit: ${trade['pnl']:+.2f}")
```

---

## RPC Polling (Alternative)

If you can't tail files, poll the RPC:

```python
import requests, time

MONITOR = "http://localhost:8766"

def get_state():
    """Get current monitor state."""
    r = requests.get(f"{MONITOR}/positions")
    positions = r.json().get("positions", [])
    total_pnl = sum(p.get("unrealisedPnl", 0) for p in positions)
    metrics = requests.get(f"{MONITOR}/metrics").json()
    return positions, total_pnl, metrics

while True:
    positions, pnl, metrics = get_state()
    print(f"PnL: ${pnl:+.2f} | SL today: {metrics.get('sl_count',0)} | TP today: {metrics.get('tp_count',0)}")
    time.sleep(60)
```
