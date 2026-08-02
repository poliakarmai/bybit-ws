# How I Fixed a Silent Telegram Bot Using a 3-Echelon AI Audit (14 Bugs Found)

My Telegram bot stopped responding.

Not "responding slowly" — just dead silent. You tap the "Scan" button and nothing comes back. The process is running, memory's fine, logs are clean. Classic Heisenbug: it breaks when you're not watching, works when you are.

I'm an engineer with a background in industrial safety (pipeline diagnostics, corrosion monitoring), but for the past six months I've been deep in AI agents and trading infrastructure. Here's what I learned: debugging with AI isn't just "ask ChatGPT to fix the error." It's a systematic approach.

Let me show you how three AI agents scanned 2,153 lines of code in parallel, found 14 bugs of varying nastiness, and brought the bot back to life.

## The Bot That Went Silent

Context first. The bot is called GridSignal — a trading tool for Bybit futures. It scans the market using Bollinger Bands, generates entry signals, sends alerts. 2,153 lines of Python, a bunch of dependencies, its own SQLite database, subprocess calls to the Bybit CLI. Your typical "grown-up" Telegram bot.

The problem surfaced after adding a new feature: funding rate rotation. I plugged a call to `funding_rotation.py` into the "Rotation" button handler, and the bot went down. Not immediately — first it just got sluggish, then stopped responding entirely.

### The Silent Killer: Async Exceptions Without Logging

The real kicker: logs were spotless. The process showed `active (running)` in systemd. I spent an hour guessing — "maybe Telegram API is having issues? network? self-healing?" — until I ran the audit.

Here's why the logs were clean: asyncio has a nasty habit of **swallowing exceptions silently**. If a coroutine raises inside a handler that doesn't have its own try/except, and nobody's awaiting it, the exception gets logged to the event loop's default handler — which, by default, prints to stderr and nothing else. Your logging framework never sees it.

The fix is a global exception handler that plugs into your logging pipeline:

```python
loop = asyncio.get_event_loop()
loop.set_exception_handler(
    lambda loop, context: logging.error(
        f"Global asyncio error: {context.get('message')}",
        exc_info=context.get('exception')
    )
)
```

With this, every swallowed exception shows up in your logs — no more Heisenbugs hiding in the shadows.

## Three Echelons

I run my own AI agent platform called Hermes. It can spawn multiple auditors in parallel, each with a different focus. The architecture looks like this:

```
                 ┌─────────────┐
                 │  Your Code  │
                 └──────┬──────┘
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
  ┌──────────────┐ ┌──────────┐ ┌──────────────┐
  │Source-Driven │ │ Security  │ │ Adversarial  │
  │              │ │           │ │              │
  │ API docs     │ │ Secrets & │ │ Race cond.   │
  │ vs code      │ │ CVEs      │ │ Logic bugs   │
  └──────┬───────┘ └─────┬─────┘ └──────┬───────┘
         └───────────────┼──────────────┘
                         ▼
              ┌──────────────────┐
              │  Consolidated    │
              │  14 Findings     │
              └──────────────────┘
```

- **Source-Driven:** cross-references code against official documentation. Finds API calls that don't exist, parameters that aren't supported, hallucinations.
- **Security:** hunts for secrets in code, holes in `.gitignore`, command injection, CVEs in dependencies.
- **Adversarial:** finds fatal bugs in business logic. Race conditions, blocking calls, resource leaks, edge cases.

Why three instead of one? Because they have different blind spots. A security auditor will catch `subprocess.run` with f-strings beautifully, but won't notice that `_valid_symbol()` is never defined anywhere. Source-driven will find doc mismatches, but will miss a race condition in scan limits. Adversarial will spot 10 sequential subprocess calls hanging the event loop for 100 seconds — but it doesn't care about CVEs.

### Prompts That Drove the Audit

What exactly did I ask the agents? Here are the actual prompts that anyone can use with ChatGPT, Claude, or Cursor:

**Adversarial echelon prompt:**

> *"Analyze this Telegram bot handler code. Find every place where a race condition is possible — if the user taps the button twice within 100ms, or two users call this function simultaneously. Also flag all blocking calls (subprocess.run, time.sleep, file I/O) inside async handlers. For each finding, suggest an atomic fix. Prioritize by severity."*

**Source-Driven echelon prompt:**

> *"Cross-reference every API call in this code against official python-telegram-bot documentation and Bybit API v5 docs. Flag: (1) parameters not documented, (2) deprecated methods, (3) return values used incorrectly. Include links to the relevant docs for each finding."*

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

**Before:**

```python
# BLOCKS the entire event loop for up to 100 seconds
for symbol in symbols:
    result = subprocess.run(
        ['bybit', 'bb', symbol],
        capture_output=True, text=True, timeout=10
    )
    results.append(parse_output(result.stdout))
```

**After:**

```python
# Event loop stays free — other users can still interact
for symbol in symbols:
    result = await asyncio.to_thread(
        subprocess.run,
        ['bybit', 'bb', symbol],
        capture_output=True, text=True, timeout=10
    )
    results.append(parse_output(result.stdout))
```

Three lines changed, event loop freed.

### 3. Race Condition in Scan Limits

The bot has a limit: 10 scans per user per day. Abuse protection.

But `update_scan_count()` blindly did `UPDATE users SET scans_today=scans_today+1` without checking the current value. If a user managed to fire `/scan` twice in one event-loop tick (fast double tap), both requests saw `scans_today=0` and both passed.

**Before:**

```python
# Two concurrent taps → both see scans_today=0 → both pass
cursor.execute(
    "UPDATE users SET scans_today = scans_today + 1 WHERE user_id = ?",
    (user_id,)
)
```

**After — atomic check-and-increment:**

```python
cursor.execute(
    """UPDATE users SET scans_today = scans_today + 1
       WHERE user_id = ? AND scans_today < 10""",
    (user_id,)
)
if cursor.rowcount == 0:
    raise RateLimitExceeded("Daily scan limit reached")
```

No more race. The database itself enforces the constraint.

### 4. Ghost Buttons and Duplicates

The "📊 LONG" and "📉 SHORT" buttons were in the keyboard, but nobody handled the press. Users tapped them — zero response. The Adversarial echelon flagged it: "buttons exist in `MAIN_KEYBOARD`, no handler implemented."

We also found a `cmd_top` duplicate: the command was registered as a handler twice, and inside `cmd_stats` there was another `cmd_top` (leaderboard) silently overwriting the original (top gainers/losers). Split into separate functions, cleaned up.

### 5. SQLite Without WAL, and the Database Sitting in Git

The Security echelon found that `.gitignore` didn't exclude `data/*.db` — SQLite files with user data could accidentally fly into the repository. They hadn't, but they could have.

Another echelon noticed: `sqlite3.connect()` without `check_same_thread=False` and without `PRAGMA journal_mode=WAL`. Two concurrent users and you get `SQLITE_BUSY: database is locked`.

**Before:**

```python
conn = sqlite3.connect('data/bot.db')
```

**After:**

```python
conn = sqlite3.connect('data/bot.db', check_same_thread=False)
conn.execute('PRAGMA journal_mode=WAL;')
```

Two extra parameters, zero more locking errors.

### 6. Temp File Leak

The `/chart` command generates a chart image, saves it temporarily, sends it to the user, and deletes it. Simple — unless the chart generation raises an exception.

**Before:**

```python
mpf.plot(df, savefig='chart.png')
with open('chart.png', 'rb') as f:
    await update.message.reply_photo(f)
os.unlink('chart.png')  # Never runs if mpf.plot() raises!
```

**After:**

```python
from tempfile import NamedTemporaryFile

with NamedTemporaryFile(suffix='.png', delete=True) as f:
    mpf.plot(df, savefig=f.name)
    await update.message.reply_photo(open(f.name, 'rb'))
# File is auto-deleted by the context manager — even on exceptions
```

`tempfile.NamedTemporaryFile` guarantees cleanup. The OS handles it, not your error-prone manual `os.unlink()`.

## What Else We Found (Less Dramatic)

- `parse_mode='Markdown'` instead of `MarkdownV2` in three places. Old mode, deprecated. Doesn't break things, but it's an eyesore.
- `requirements.txt` incomplete — three libraries missing. Deploy on a clean machine and you won't know what's missing.
- Bare `import re` missing, even though `_valid_symbol()` (once we actually wrote it) used `re.fullmatch()`.
- Twenty-one `except:` clauses without exception type — swallowing KeyboardInterrupt and SystemExit along with real errors.

## Testing: How 45/45 Smoke Tests Passed Green

After every fix, I ran the smoke test suite. Here's the setup:

```python
# pytest with asyncio support, async fixtures, mocks
# test_smoke.py
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_scan_button_does_not_block_event_loop():
    """Verify scan handler yields control back to event loop."""
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = CompletedProcess(args=[], returncode=0, stdout='{}')
        start = time.monotonic()
        await cmd_scan(update, context)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5  # Should return fast — actual work is in to_thread()

@pytest.mark.asyncio
async def test_race_condition_scan_limit():
    """Double-tap scan must not exceed daily limit."""
    db.execute("UPDATE users SET scans_today = 9 WHERE user_id = 1")
    await cmd_scan(update, context)  # 10th scan — should pass
    with pytest.raises(RateLimitExceeded):
        await cmd_scan(update, context)  # 11th — blocked
```

Tools used: `pytest`, `pytest-asyncio`, `unittest.mock` for Telegram API calls, `sqlite3` with `:memory:` databases for isolation. No external services touched — pure deterministic tests that run in under 2 seconds.

45 tests, 0 failures. CI green.

## Lessons

**One.** A single auditor is dangerous. It will confidently say "all clear" because it's looking from one angle. Three agents with different focuses cover each other's blind spots.

**Two.** Blocking calls in async handlers aren't a "fix later" thing. They make your bot look dead in production. `subprocess.run()` inside async is a red flag. Always.

**Three.** You can't spot a race condition by eyeballing it — especially in code you wrote yourself. You need an adversarial echelon that actively asks "what if they tap twice, fast?"

**Four.** Buttons in a keyboard without handlers are negligence you never notice because you never use those buttons. Users do.

**Five.** Type annotations double your auditor's accuracy. An AI agent that sees `def handler(update: Update, context: ContextTypes.DEFAULT_TYPE)` makes 30–40% fewer false positives than one seeing `def handler(update, context)`. Add type hints — your tools get smarter.

## Bonus: What Else to Check in Your Telegram Bot

Beyond the five bugs above, here's a checklist of things that bite bots in production. Run through these — each one takes 2 minutes to verify:

- ✅ **Rate Limiting (429 Too Many Requests).** Does your bot handle Telegram API backpressure? Use aiogram's built-in retry middleware or add exponential backoff: `await asyncio.sleep(min(2 ** attempt, 60))`.
- ✅ **Type Hints.** Run `mypy --strict` on your handlers. AI auditors get 30–40% more accurate with typed code — the model understands variable context better.
- ✅ **Global asyncio exception handler.** Add the `loop.set_exception_handler()` snippet from above. Silent exceptions are the #1 cause of "it works on my machine" Heisenbugs.
- ✅ **Atomic database operations.** Any counter, limit, or state transition that can be triggered by concurrent users needs `WHERE ... AND current_state < limit` — never two-step read-then-write.
- ✅ **Temporary file cleanup.** Replace all manual `os.unlink()` with `tempfile.NamedTemporaryFile` or `try/finally`. One exception and your disk fills up.
- ✅ **Dependency completeness.** Run `pip freeze > requirements.txt` from a clean venv, not your dev environment. Three missing libraries is typical.

## The Bottom Line

After all fixes: 0 CRITICAL, 45/45 smoke tests green, bot responds instantly. Audit time: 4 minutes. Manual debugging time: 1 hour.

I'm not saying AI audits replace code review. But as a first line of defense, they're terrifyingly effective — especially when your project crosses 2,000 lines and keeping it all in your head is simply not realistic.

Got a Telegram bot that "sometimes lags"? Try running the three-echelon approach with the prompts above. Chances are, you'll find a couple of your own `_valid_symbol`s.

---

*The author is a trader and AI engineer. Writes about trading bot infrastructure, multi-agent systems, and practical production debugging.*
