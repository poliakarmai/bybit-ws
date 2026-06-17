# How I Fixed a Silent Telegram Bot Using a 3-Echelon AI Audit (14 Bugs Found)

My Telegram bot stopped responding.

Not "responding slowly" — just dead silent. You tap the "Scan" button and nothing comes back. The process is running, memory's fine, logs are clean. Classic Heisenbug: it breaks when you're not watching, works when you are.

I'm an engineer with a background in industrial safety (pipeline diagnostics, corrosion monitoring), but for the past six months I've been deep in AI agents and trading infrastructure. Here's what I learned: debugging with AI isn't just "ask ChatGPT to fix the error." It's a systematic approach.

Let me show you how three AI agents scanned 2,153 lines of code in parallel, found 14 bugs of varying nastiness, and brought the bot back to life.

## The Bot That Went Silent

Context first. The bot is called GridSignal — a trading tool for Bybit futures. It scans the market using Bollinger Bands, generates entry signals, sends alerts. 2,153 lines of Python, a bunch of dependencies, its own SQLite database, subprocess calls to the Bybit CLI. Your typical "grown-up" Telegram bot.

The problem surfaced after adding a new feature: funding rate rotation. I plugged a call to `funding_rotation.py` into the "Rotation" button handler, and the bot went down. Not immediately — first it just got sluggish, then stopped responding entirely.

The real kicker: logs were spotless. The process showed `active (running)` in systemd. I spent an hour guessing — "maybe Telegram API is having issues? network? self-healing?" — until I ran the audit.

## Three Echelons

I run my own AI agent platform called Hermes. It can spawn multiple auditors in parallel, each with a different focus. I used three echelons:

- **Source-Driven:** cross-references code against official documentation. Finds API calls that don't exist, parameters that aren't supported, hallucinations.
- **Security:** hunts for secrets in code, holes in `.gitignore`, command injection, CVEs in dependencies.
- **Adversarial:** finds fatal bugs in business logic. Race conditions, blocking calls, resource leaks, edge cases.

Why three instead of one? Because they have different blind spots. A security auditor will catch `subprocess.run` with f-strings beautifully, but won't notice that `_valid_symbol()` is never defined anywhere. Source-driven will find doc mismatches, but will miss a race condition in scan limits. Adversarial will spot 10 sequential subprocess calls hanging the event loop for 100 seconds — but it doesn't care about CVEs.

Three agents, parallel execution. Four minutes later, I had the consolidated report: 14 findings. Five CRITICAL, four HIGH.

## What We Found

### 1. The Function That Doesn't Exist

The bot calls `_valid_symbol(symbol)` in three places: during `/alert`, during background alert checking, and during inline queries (`@Gridbolbot BTCUSDT`). But the function is never defined. Not in the main file, not in any import, not in any adjacent module.

When a user tapped `/alert BTCUSDT`, the bot crashed with `NameError: name '_valid_symbol' is not defined`. The handler died, no alert was set, and I sat there scratching my head at the clean logs.

The Source-Driven echelon found this. Interestingly, the Security echelon also found `_valid_symbol` — but from a different angle: "symbol validation function undefined, potential command injection via subprocess if it existed."

Two echelons, two different reasons to worry about the same non-existent function.

### 2. Nine Blocking Calls in Async

The Adversarial echelon walked through every handler and built a table. The worst offender: `cmd_fear`. The "Fear" button made 10 sequential `subprocess.run(['bybit', 'bb', ...])` calls. Each with a 10-second timeout. Ten coins, ten calls, 100 seconds of event-loop blockage.

Async Python doesn't forgive this. While `cmd_fear` waits for Bybit's response on the tenth coin, every other user sees "typing..." and gets nothing. If two people hit buttons at the same time — the bot just freezes.

Fix: `asyncio.to_thread(subprocess.run, ...)`. Three lines, event loop freed.

### 3. Race Condition in Scan Limits

The bot has a limit: 10 scans per user per day. Abuse protection.

But `update_scan_count()` blindly did `UPDATE users SET scans_today=scans_today+1` without checking the current value. If a user managed to fire `/scan` twice in one event-loop tick (fast double tap), both requests saw `scans_today=0` and both passed.

Fix — atomic UPDATE:

```sql
UPDATE users SET scans_today=scans_today+1
WHERE user_id=? AND scans_today < 10
```

No more race.

### 4. Ghost Buttons and Duplicates

The "📊 LONG" and "📉 SHORT" buttons were in the keyboard, but nobody handled the press. Users tapped them — zero response. The Adversarial echelon flagged it: "buttons exist in `MAIN_KEYBOARD`, no handler implemented."

We also found a `cmd_top` duplicate: the command was registered as a handler twice, and inside `cmd_stats` there was another `cmd_top` (leaderboard) silently overwriting the original (top gainers/losers). Split into separate functions, cleaned up.

### 5. SQLite Without WAL, and the Database Sitting in Git

The Security echelon found that `.gitignore` didn't exclude `data/*.db` — SQLite files with user data could accidentally fly into the repository. They hadn't, but they could have.

Another echelon noticed: `sqlite3.connect()` without `check_same_thread=False` and without `PRAGMA journal_mode=WAL`. Two concurrent users and you get `SQLITE_BUSY: database is locked`.

## What Else We Found (Less Dramatic)

- `parse_mode='Markdown'` instead of `MarkdownV2` in three places. Old mode, deprecated. Doesn't break things, but it's an eyesore.
- `requirements.txt` incomplete — three libraries missing. Deploy on a clean machine and you won't know what's missing.
- Temp file leak in `/chart` — if `mpf.plot()` raises, `os.unlink()` never runs.
- Bare `import re` missing, even though `_valid_symbol()` (once we actually wrote it) used `re.fullmatch()`.

## Lessons

**One.** A single auditor is dangerous. It will confidently say "all clear" because it's looking from one angle. Three agents with different focuses cover each other's blind spots.

**Two.** Blocking calls in async handlers aren't a "fix later" thing. They make your bot look dead in production. `subprocess.run()` inside async is a red flag. Always.

**Three.** You can't spot a race condition by eyeballing it — especially in code you wrote yourself. You need an adversarial echelon that actively asks "what if they tap twice, fast?"

**Four.** Buttons in a keyboard without handlers are negligence you never notice because you never use those buttons. Users do.

## The Bottom Line

After all fixes: 0 CRITICAL, 45/45 smoke tests green, bot responds instantly. Audit time: 4 minutes. Manual debugging time: 1 hour.

I'm not saying AI audits replace code review. But as a first line of defense, they're terrifyingly effective — especially when your project crosses 2,000 lines and keeping it all in your head is simply not realistic.

Got a Telegram bot that "sometimes lags"? Try running it through three questions: any undefined functions? Any `subprocess.run` in async? Any non-atomic UPDATEs where they should be? Chances are, you'll find a couple of your own `_valid_symbol`s.

---

*The author is a trader and AI engineer. Writes about trading bot infrastructure, multi-agent systems, and practical production debugging.*
