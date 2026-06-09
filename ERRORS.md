# Error Reference for AI Agents

Common errors from Bybit API and bybit-ws RPC — what they mean and what to do.

---

## Bybit API Errors

### `retCode: 10001` — "position idx not match position mode"

**Meaning:** You sent `positionIdx=0` but the account is in hedge mode and this symbol uses idx=1 (or vice versa).

**Fix:**
```
1. Read current positionIdx: GET /v5/position/list → check positionIdx field
2. Use the SAME idx for SL/TP/close
3. For NEW orders: try idx=0 first, on 10001 → retry with idx=1
```

**bybit-ws handles this automatically.** If you're using the RPC `/enter` endpoint, it's already handled. Only matters for raw API calls.

---

### `retCode: 10001` — "ab not enough for new order"

**Meaning:** No free margin. `totalPositionIM + totalOrderIM > walletBalance`.

**Fix:**
```
1. Check: GET /v5/account/wallet-balance?accountType=UNIFIED&coin=USDT
2. Compare walletBalance vs totalPositionIM + totalOrderIM
3. Either: close some positions to free margin, or deposit more USDT
```

**Prevention:** bybit-ws checks available balance before every entry. If you see this, the monitor is already at its position limit.

---

### `retCode: 34040` — "not modified" (trading-stop)

**Meaning:** The SL/TP you tried to set is identical to what's already there.

**Fix:** No action needed. This is a success, not an error. Position already has the correct SL/TP.

---

### `retCode: 130021` — "order does not exist"

**Meaning:** You tried to cancel/modify an order that was already filled or cancelled.

**Fix:** Ignore. The order is gone. Fetch fresh orders: `GET /v5/order/realtime`.

---

### `retCode: 20001` — "order quantity or price too low"

**Meaning:** Qty below minimum or price precision mismatch.

**Fix:**
```
1. Check lot size filter: GET /v5/market/instruments-info?category=linear&symbol=SYMUSDT
   → look for lotSizeFilter.qtyStep
2. Round qty UP to the nearest qtyStep
3. For price: use tickSize from priceFilter
```

---

## bybit-ws RPC Errors

### `{"error": "rate limit exceeded"}`

**Meaning:** More than 60 requests/minute from your IP.

**Fix:** Slow down. The monitor caches positions/orders every 30s — use the cache, don't poll live API.

---

### `{"error": "unauthorized"}`

**Meaning:** Missing or wrong `Authorization: Bearer <token>`.

**Fix:** Set `RPC_TOKEN` in your environment or pass the token header.

---

### `{"error": "method not allowed"}`

**Meaning:** Wrong HTTP method (e.g., GET instead of POST).

**Fix:** Check the endpoint method. `/enter` and `/close` are POST; `/scan`, `/positions`, `/health` are GET.

---

### `Connection refused` (curl can't connect)

**Meaning:** bybit-ws is not running, or RPC port (8766) is not bound yet.

**Fix:**
```bash
systemctl --user status bybit-ws
# If not running: systemctl --user start bybit-ws
# If running but no RPC: wait 30s (RPC starts on first cycle)
```

---

### Health check returns `{"status":"stale"}`

**Meaning:** Main loop hasn't updated `health.txt` in > 90 seconds. Monitor may be stuck.

**Fix:**
```bash
systemctl --user restart bybit-ws
# Check logs: journalctl --user -u bybit-ws --since "2 min ago"
```

---

## Runtime Crashes (systemd)

### `🚨 Watchdog: главный цикл завис (190с)`

**Meaning:** Main loop hung > 180s. Process killed by watchdog, systemd restarts.

**Root cause:** Usually Bybit API timeout storm (>100 requests queued).

**Fix:** Restart clears the queue. If recurring:
```
1. Reduce watchlist size (watchlist.top_n: 30 instead of 50)
2. Check for API rate limits from Bybit
3. Check heavy_cycle interval (increase to 15)
```

---

## Trading-Specific Errors

### Order placed but not filled after 5 minutes

**Meaning:** Limit order is sitting in the book. This is NORMAL for Bollinger Grid — we place limit orders below market to get better entry.

**Action:** None. Order stays GTC until filled or cancelled (by cleanup after timeout).

---

### Position has no SL/TP set

**Meaning:** bybit-ws sets SL/TP AFTER the limit order fills. If you entered manually (not via RPC), SL/TP won't be auto-set.

**Fix:**
```bash
# Set SL manually
curl -X POST http://localhost:8766/enter -d '{"symbol":"SYMUSDT","side":"Buy","qty":100}'
# → Monitor will auto-set SL/TP on next cycle
```

---

### Position shows "takeProfit": "" on SHORT

**Meaning:** SHORT was opened before the TP-fix (v3.5). Older positions may lack TP.

**Fix:** bybit-ws will set TP on the next heavy cycle. Or force it:
```bash
# Raw API fix
POST /v5/position/trading-stop
{"category":"linear","symbol":"SYMUSDT","positionIdx":0,
 "takeProfit":"TP_PRICE","tpTriggerBy":"MarkPrice"}
```

---

## Quick Diagnostic Commands

```bash
# All positions with SL/TP status
curl http://localhost:8766/positions | python3 -m json.tool

# Service status
systemctl --user status bybit-ws

# Recent errors in log
journalctl --user -u bybit-ws --since "10 min ago" | grep -iE "error|retCode"

# Watchdog kills
grep "Watchdog.*завис" ~/.local/share/bybit-ws/events.log | tail -5
```
