#!/usr/bin/env python3
"""
GridSignal Bot v4.0 — Telegram-бот сигналов Bollinger Grid.
Фичи: /scan, /scan short, /rules, /help, /stats, /setrisk, /history, /subscribe, /alert, /chart
v4.0: SHORT-сигналы, Multi-TF (D/5/3/W/M), RSI(14), батчевый check_outcomes, /lang en/ru

Бесплатная версия: до 2 /scan в сутки на пользователя.
"""

import os, sys, json, time, asyncio, re, sqlite3, subprocess, threading, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, InlineQueryHandler, CallbackQueryHandler, PreCheckoutQueryHandler
from telegram import InlineQueryResultArticle, InputTextMessageContent

SCANNER_SCRIPT = os.path.expanduser('~/.local/bin/gridsignal_scanner.py')
DB_PATH = os.path.expanduser('~/.local/share/gridsignal-bot/users.db')
BYBIT_CLI = os.path.expanduser('~/.local/bin/bybit')

# RPC endpoint (bybit-ws v6.0 — WS-кеш, без CLI)
RPC_URL = "http://localhost:8766/scan"
RPC_TOKEN = None  # загружается при старте из state.db
def _load_rpc_token():
    """Загрузить RPC токен из state.db (bybit-ws)."""
    global RPC_TOKEN
    try:
        import sqlite3
        db_path = os.path.expanduser("~/.local/share/bybit-ws/state.db")
        db = sqlite3.connect(db_path)
        row = db.execute("SELECT value FROM kv_store WHERE key='rpc_auth_token'").fetchone()
        if row:
            RPC_TOKEN = row[0]
            print("[GridSignal] ✅ RPC token loaded — using direct RPC (fast)")
        else:
            print("[GridSignal] ⚠️ RPC token NOT FOUND in state.db — scans will fall back to CLI (slow)")
    except Exception as e:
        print(f"[GridSignal] ❌ RPC token load FAILED: {e} — scans will use CLI fallback (slow, may break)")


BOT_VERSION = "5.0"

# Pro subscription
PRO_PRICE_STARS = 300  # ~400 ₽
PAYMENTS_DB = os.path.expanduser("~/.local/share/gridsignal-bot/pro_users.db")
TON_PRICE = 2.0  # TON
CRYPTOBOT_TOKEN = os.environ.get("CRYPTOBOT_TOKEN", "")
# M8: remove from process environ to prevent leak via /proc/PID/environ
os.environ.pop("CRYPTOBOT_TOKEN", None)
TON_INVOICES_DB = os.path.expanduser("~/.local/share/gridsignal-bot/ton_invoices.db")
GRIDSIGNAL_ADMIN_ID = int(os.environ.get("GRIDSIGNAL_ADMIN_ID", "319665243"))
GRIDSIGNAL_ADMIN_NOTIFY_CHAT = int(os.environ.get("GRIDSIGNAL_ADMIN_NOTIFY_CHAT", "5529208670"))

def _now() -> str:
    """Единый формат даты: SQLite-совместимый (без 'T')."""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def _expires(days: int = 30) -> str:
    """Дата истечения через N дней."""
    from datetime import timedelta
    return (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

def init_ton_db():
    conn = sqlite3.connect(TON_INVOICES_DB, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute("""CREATE TABLE IF NOT EXISTS ton_invoices (
        invoice_id INTEGER PRIMARY KEY, user_id INTEGER,
        status TEXT DEFAULT 'pending', created_at TEXT)""")
    conn.commit()
    return conn

async def create_ton_invoice(user_id: int):
    """Создать счёт в CryptoBot. Возвращает (url, invoice_id) или (None, None)."""
    if not CRYPTOBOT_TOKEN:
        return None, None
    try:
        body = json.dumps({
            "asset": "TON", "amount": str(TON_PRICE),
            "description": "GridSignal Pro 30d",
            "payload": "gridsignal_pro_30d",
            "allow_comments": False, "allow_anonymous": False
        }).encode()
        req = urllib.request.Request(
            "https://pay.crypt.bot/api/createInvoice",
            data=body,
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN, "Content-Type": "application/json"}
        )
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=15).read())
        data = json.loads(resp)
        if data.get("ok") and data.get("result"):
            inv = data["result"]
            conn = init_ton_db()
            conn.execute("INSERT OR REPLACE INTO ton_invoices VALUES (?,?,?,?)",
                         (inv["invoice_id"], user_id, "pending", _now()))
            conn.commit()
            conn.close()
            return inv["bot_invoice_url"], inv["invoice_id"]
    except Exception as e:
        print(f"[TON] Invoice error: {e}")
    return None, None

async def check_ton_payment(invoice_id: int) -> bool:
    """Проверить статус платежа в CryptoBot. True = paid."""
    if not CRYPTOBOT_TOKEN:
        return False
    try:
        req = urllib.request.Request(
            f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}",
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        )
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10).read())
        data = json.loads(resp)
        if data.get("ok") and data.get("result", {}).get("items"):
            status = data["result"]["items"][0].get("status", "")
            if status == "paid":
                conn = init_ton_db()
                conn.execute("UPDATE ton_invoices SET status='paid' WHERE invoice_id=?", (invoice_id,))
                conn.commit()
                conn.close()
                return True
    except Exception as e:
        print(f"[TON] Check error: {e}")
    return False

def init_pro_db():
    conn = sqlite3.connect(PAYMENTS_DB, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute("""CREATE TABLE IF NOT EXISTS pro_users (
        user_id INTEGER PRIMARY KEY, username TEXT, paid_at TEXT,
        expires_at TEXT, active INTEGER DEFAULT 1, charge_id TEXT)""")
    conn.execute("ALTER TABLE pro_users ADD COLUMN charge_id TEXT", [])
    conn.commit()
    return conn

def activate_pro(user_id: int, username: str, charge_id: str = "") -> None:
    """Активировать Pro-подписку на 30 дней."""
    conn = init_pro_db()
    conn.execute(
        "INSERT OR REPLACE INTO pro_users (user_id,username,paid_at,expires_at,active,charge_id) VALUES (?,?,?,?,1,?)",
        (user_id, username, _now(), _expires(30), charge_id))
    conn.commit()
    conn.close()

def is_pro(user_id: int) -> bool:
    conn = init_pro_db()
    row = conn.execute(
        "SELECT 1 FROM pro_users WHERE user_id=? AND active=1 AND expires_at > datetime('now')",
        (user_id,)
    ).fetchone()
    conn.close()
    return row is not None

_cache = {}  # {data_D_long: [...], data_5_long: [...], data_D_short: [...], ...}
_inline_cache = {}
CACHE_TTL = 120
INLINE_CACHE_TTL = 300

def _escape_mdv2(text: str) -> str:
    """Экранирует спецсимволы Telegram MarkdownV2."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
SCAN_COOLDOWN = 60
MAX_SCANS_PER_DAY = 2
MAX_ALERTS_PER_USER = 5
_ton_cooldown = {}  # user_id → last check timestamp (rate limit)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📊 Скан"), KeyboardButton("🔴 Шорт"), KeyboardButton("📈 График")],
        [KeyboardButton("⚡ Скальп x10"), KeyboardButton("🔄 Mean Revert"), KeyboardButton("💰 Фандинг")],
        [KeyboardButton("🔄 Ротация"), KeyboardButton("📊 LONG"), KeyboardButton("📉 SHORT")],
        [KeyboardButton("🟢 Покупка"), KeyboardButton("📋 История"), KeyboardButton("🔔 Алерты")],
        [KeyboardButton("🌐 Язык"), KeyboardButton("📐 Правила"), KeyboardButton("😱 Страх")],
        [KeyboardButton("💳 Оплата"), KeyboardButton("🔮 Гороскоп"), KeyboardButton("💬 Связь"), KeyboardButton("❓ Помощь")],
        [KeyboardButton("🔍 DEX")],
    ],
    resize_keyboard=True,
)


# ═══════════════════════════════════════════════════════════════
# DB
# ═══════════════════════════════════════════════════════════════

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS users (\n        user_id INTEGER PRIMARY KEY,
        username TEXT, first_name TEXT, deposit REAL DEFAULT 0,
        scans_today INTEGER DEFAULT 0, last_scan_ts REAL DEFAULT 0,
        subscribed INTEGER DEFAULT 0,
        joined_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, event TEXT NOT NULL, ts REAL NOT NULL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS signals (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL, symbol TEXT NOT NULL, score REAL,
        price REAL, lower_bb REAL, upper_bb REAL, middle_bb REAL,
        entry REAL, tp1 REAL, tp2 REAL, sl REAL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts)')
    try:
        conn.execute('ALTER TABLE signals ADD COLUMN outcome TEXT DEFAULT NULL')
    except Exception:
        pass  # уже есть
    try:
        conn.execute('ALTER TABLE signals ADD COLUMN outcome_ts REAL DEFAULT NULL')
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE signals ADD COLUMN pnl_pct REAL DEFAULT NULL')
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE signals ADD COLUMN timeframe TEXT DEFAULT "D"')
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE signals ADD COLUMN user_id INTEGER DEFAULT NULL')
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE users ADD COLUMN lang TEXT DEFAULT "ru"')
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE signals ADD COLUMN mode TEXT DEFAULT "long"')
    except Exception:
        pass
    conn.execute('''CREATE TABLE IF NOT EXISTS alerts (\n        user_id INTEGER NOT NULL, symbol TEXT NOT NULL,
        lower_bb REAL DEFAULT 0, active INTEGER DEFAULT 1,
        PRIMARY KEY (user_id, symbol)
    )''')
    try:
        conn.execute('ALTER TABLE alerts ADD COLUMN bb_zone REAL DEFAULT 100')
    except Exception:
        pass  # already exists
    conn.commit()
    return conn


def get_user(conn, user_id: int) -> dict:
    row = conn.execute('SELECT user_id, username, first_name, scans_today, last_scan_ts, deposit, subscribed FROM users WHERE user_id = ?', (user_id,)).fetchone()
    if not row:
        conn.execute('INSERT INTO users (user_id) VALUES (?)', (user_id,))
        conn.commit()
        return {'user_id': user_id, 'deposit': 0, 'scans_today': 0, 'last_scan_ts': 0, 'subscribed': 0}
    return {
        'user_id': row[0], 'username': row[1], 'first_name': row[2],
        'scans_today': row[3], 'last_scan_ts': float(row[4] or 0),
        'deposit': float(row[5] or 0), 'subscribed': int(row[6] or 0),
    }


def log_event(conn, user_id: int, event: str, update: Update = None):
    conn.execute('INSERT INTO events (user_id, event, ts) VALUES (?, ?, ?)', (user_id, event, time.time()))
    if update and update.effective_user:
        u = update.effective_user
        conn.execute('UPDATE users SET username=COALESCE(?,username), first_name=COALESCE(?,first_name) WHERE user_id=?',
                     (u.username, u.first_name, user_id))
    conn.commit()


def check_scan_allowed(user: dict) -> tuple:
    now = time.time()
    if now - user['last_scan_ts'] < SCAN_COOLDOWN:
        remaining = SCAN_COOLDOWN - int(now - user['last_scan_ts'])
        return False, f"⏳ Следующий скан через {remaining} сек"
    if user['scans_today'] >= MAX_SCANS_PER_DAY:
        return False, "📊 Лимит на сегодня исчерпан (10 сканов). Завтра будет снова."
    return True, ""


def update_scan_count(conn, user_id: int):
    """Atomically increment scan count — prevents race condition in limits."""
    conn.execute(
        'UPDATE users SET scans_today=scans_today+1, last_scan_ts=? WHERE user_id=? AND scans_today < ?',
        (time.time(), user_id, MAX_SCANS_PER_DAY)
    )
    conn.commit()


def reset_daily_counts(conn):
    """Сброс счётчиков сканов. Сбрасывает если последний скан был не сегодня."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    conn.execute("UPDATE users SET scans_today=0 WHERE last_scan_ts < ?", (today_start,))
    conn.commit()


async def _reset_counts_job():
    """Job queue wrapper: открыть коннект, сбросить, закрыть."""
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        reset_daily_counts(conn)
        conn.close()
    except Exception as e:
        print(f"[GridSignal] _reset_counts_job failed: {e}")


def save_signals(signals: list, user_id: int = None, timeframe: str = 'D', mode: str = 'long'):
    """Сохранить сигналы в БД для истории. mode: 'long' или 'short'."""
    if not signals:
        return
    conn = init_db()
    now = time.time()
    for s in signals:
        lower, upper, middle = s['lower_bb'], s['upper_bb'], s['middle_bb']
        sig_mode = s.get('mode', mode)
        if sig_mode == 'short':
            # SHORT: entry above market, TP on middle/lower, SL above entry
            entry = s['price'] * 1.02
            tp1, tp2 = middle, lower
            sl = entry * 1.05  # +5% SL for SHORT
        else:
            # LONG: entry below market, TP on middle/upper, SL below entry
            entry = lower * 0.97
            tp1, tp2 = middle, upper
            sl = lower * 0.93
        conn.execute('''INSERT INTO signals (ts, symbol, score, price, lower_bb, upper_bb, middle_bb, entry, tp1, tp2, sl, timeframe, user_id)\n            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (now, s['symbol'], s['score'], s['price'], lower, upper, middle,
             entry, tp1, tp2, sl, timeframe, user_id))
    conn.commit()
    conn.close()


async def get_cached_scan(interval: str = 'D', user_id: int = None, symbol: str = None,
                     green_only: bool = False, mode: str = 'long') -> list:
    """Получить сигналы через RPC /scan (WS-кеш, без CLI)."""
    global _cache
    cache_key = f'data_{interval}_{mode}'
    now = time.time()
    if cache_key in _cache and (now - _cache.get(f'{cache_key}_ts', 0)) < CACHE_TTL:
        data = _cache[cache_key]
    else:
        try:
            if not RPC_TOKEN:
                _load_rpc_token()
            if not RPC_TOKEN:
                print('[GridSignal] No RPC token — scan unavailable')
                return []

            body = json.dumps({"mode": mode, "interval": interval, "limit": 5}).encode()
            req = urllib.request.Request(
                RPC_URL, data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {RPC_TOKEN}"
                }
            )

            loop = asyncio.get_event_loop()
            resp_data = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=60).read()
            )
            data = json.loads(resp_data)
            _cache[cache_key] = data
            _cache[f'{cache_key}_ts'] = now
            save_signals(data, user_id, interval, mode)

        except urllib.error.HTTPError as e:
            print(f'[GridSignal] RPC HTTP {e.code}: {e.reason}')
            return []
        except json.JSONDecodeError as e:
            print(f'[GridSignal] JSON parse error: {e}')
            return []
        except Exception as e:
            print(f'[GridSignal] Scan error: {type(e).__name__}: {e}')
            return []

    # Фильтры
    if symbol:
        data = [s for s in data if s['symbol'] == symbol]
    if green_only:
        data = [s for s in data if s.get('bb_pos', 100) < 25 or s.get('bb_pct', 100) < 25]
    return data



# ═══════════════════════════════════════════════════════════════
# i18n
# ═══════════════════════════════════════════════════════════════

def get_lang(conn, user_id: int) -> str:
    row = conn.execute('SELECT lang FROM users WHERE user_id=?', (user_id,)).fetchone()
    return (row[0] if row and row[0] else 'ru')


def _valid_symbol(symbol: str) -> str:
    """Validate and normalize trading symbol. Returns safe symbol or None."""
    if not symbol or not isinstance(symbol, str):
        return None
    sym = symbol.strip().upper()
    if not sym.endswith('USDT'):
        sym += 'USDT'
    if not re.fullmatch(r'^[A-Z0-9]+$', sym):
        return None
    return sym


T = {
    'ru': {
        'lang_set': '🇷🇺 Язык: **Русский**',
        'start': (
            f"🚀 **GridSignal Bot v{BOT_VERSION}**\n\n"
            f"Сигналы по Bollinger Grid: полосы Боллинджера + 8-метричный скоринг + RSI.\n\n"
            f"📊 `/scan` — топ-5 LONG-сигналов\n"
            f"🔴 `/scan short` — топ-5 SHORT-сигналов\n"
            f"⚡ `/scan scalp` — BB Scalping x10\n"
            f"🔄 `/scan mean` — Mean Reversion x10\n"
            f"💰 `/scan funding` — Funding Momentum x10\n"
            f"🔄 `/scan rotation` — Funding rotation\n"
            f"🏆 `/top` — лидерборд по винрейту\n"
            f"📊 `/scan TIAUSDT` — скан по тикеру\n"
            f"🟢 `/scan green` — только зона покупки\n"
            f"📈 `/chart BTCUSDT` — график BB (таймфрейм по умолч.)\n"
            f"📈 `/chart BTCUSDT 5` — BB на 5-минутках\n"
            f"📈 `/chart BTCUSDT D` — BB на дневках\n"
            f"🔔 `/alert TIAUSDT` — алерт на Lower BB\n"
            f"🔔 `/alert TIAUSDT bb25` — алерт на BB-зону\n"
            f"⭐ `/pro` — GridSignal Pro (безлимит)\n"
            f"🌐 `/lang ru` — Русская версия\n"
            f"📊 `/stats` — статистика и винрейт\n"
            f"📐 `/rules` — стратегия\n\n"
            "⚡ Бесплатно: до 2 сканов в сутки.\n\n"
            "💰 **Реферальная ссылка Bybit:**\n"
            "`https://www.bybit.com/invite?ref=DQ0EAQ`\n\n"
            "⚠️ **Дисклеймер:** Бот даёт торговые сигналы на основе Bollinger Bands. Это не финансовая рекомендация. "
            "Торговля фьючерсами с плечом (до 10x) сопряжена с высоким риском — вы можете потерять весь депозит. "
            "Всегда тестируйте стратегию на тестнете Bybit перед реальной торговлей. "
            "Автор не несёт ответственности за ваши торговые решения."
        ),
        'scan_limit': '📊 Лимит на сегодня исчерпан (2 скана). Завтра будет снова.',
        'scan_cooldown': '⏳ Следующий скан через {} сек',
        'scanning': '🔍 Сканирую рынок...',
        'scan_header': '🚀 **GRID SIGNALS**',
        'scan_header_short': '🔻 **SHORT SIGNALS**',
        'scan_fail': '❌ Не удалось получить данные. Попробуй позже.',
        'scan_remaining': '📊 Осталось: {}/{}',
        'scan_footer': '⚡ _Лимитки на −3% ниже Lower BB_',
        'history_empty': '📈 История пуста. Сделай `/scan` чтобы появились записи.',
        'history_title': '📈 **История сигналов**',
        'history_footer': '💡 _Актуальны на момент скана._',
        'subscribe_on': '📬 **Подписка активирована!**\n\nКаждое утро в 10:00 МСК — топ-3 сигнала на день.\nОтключить: `/subscribe` ещё раз.',
        'subscribe_off': '📭 Подписка отключена. Утренней сводки не будет.',
        'alert_limit': '🚫 Лимит {} алертов. Отключи старый: /delalert SYM',
        'alert_added': 'добавлен',
        'alert_updated': 'обновлён',
        'alert_disabled': '🔕 Алерт **{}** отключён.',
        'alert_none': '🔔 Нет активных алертов.\n`/alert BTCUSDT` — добавить.',
        'alert_hit': '🔔 **{}** у Lower BB!\nЦена: `${}` · Lower: `${}`\nТочка входа: `${}`\n\n⚡ `/scan` — проверить рынок',
        'chart_usage': '❌ `/chart BTCUSDT` — укажи символ.\n`/chart BTCUSDT 5` — с таймфреймом.',
        'chart_building': '📈 Строю график',
        'chart_no_data': '❌ {} — нет данных для {}.',
        'chart_error': '❌ Ошибка: {}',
        'stats_forbidden': '🚫 Доступ запрещён.',
        'daily_header': '🌅 **Доброе утро!** Топ-3 сигнала на {}',
        'daily_yesterday': '📊 Вчера: {}/{} в плюс ({:.0f}% взвеш.), средний PnL: {:+.1f}%',
        'daily_footer': '⚡ `/scan` — обновить сигналы',
        'contact_text': (
            '💬 **Связь с автором**\n\n'
            'Есть вопрос, идея или баг? Пиши:\n'
            '• Telegram: **@Poliakarm**\n'
            '• Канал: скоро\n\n'
            'Бот развивается — твой фидбек важен! 🚀'
        ),
        'deposit_none': '💰 Депозит не задан. `/setrisk 500` — установить размер депозита.',
        'deposit_min': '❌ Минимальный депозит: $10',
        'deposit_usage': '❌ `/setrisk 500` — укажи число (депозит в USDT)',
        'deposit_set': '✅ Депозит: **${:,.0f}**\nРиск на сигнал: **${:.0f}** (2%)\nПлечо: 3x\n\nТеперь `/scan` показывает размер позиции под твой банк.',
        'setrisk_help': '💰 `/setrisk 500` — установить размер депозита.',
    },
    'en': {
        'lang_set': '🇬🇧 Language: **English**',
        'start': (
            f"🚀 **GridSignal Bot v{BOT_VERSION}**\n\n"
            "Bollinger Grid signals: Bollinger Bands + 8-metric scoring + RSI.\n\n"
            "📊 `/scan` — top-5 LONG signals\n"
            "🔴 `/scan short` — top-5 SHORT signals\n"
            "📈 `/chart BTCUSDT` — BB chart\n"
            "📈 `/chart BTCUSDT 5` — BB on 5-min TF\n"
            "💰 `/setrisk 500` — set deposit (position sizing)\n"
            "📋 `/history` — signal history\n"
            "🔔 `/alert BTCUSDT` — alert on Lower BB breach\n"
            "📬 `/subscribe` — daily top-3 digest\n"
            "🌐 `/lang ru` — Русская версия\n"
            "📐 `/rules` — strategy rules\n"
            "📊 `/stats` — statistics\n\n"
            "⚡ Free: up to 2 scans/day.\n\n"
            "💰 Trade on Bybit: [bybit.com/invite?ref=DQ0EAQ](https://www.bybit.com/invite?ref=DQ0EAQ&medium=referral&utm_campaign=evergreen)\n\n"
            "⚠️ **Disclaimer:** This bot provides trading signals based on Bollinger Bands. This is not financial advice. "
            "Futures trading with leverage (up to 10x) carries high risk — you can lose your entire deposit. "
            "Always test your strategy on Bybit testnet before trading real funds. "
            "The author is not responsible for your trading decisions."
        ),
        'scan_limit': '📊 Daily limit reached (2 scans). Come back tomorrow.',
        'scan_cooldown': '⏳ Next scan in {} sec',
        'scanning': '🔍 Scanning market...',
        'scan_header': '🚀 **GRID SIGNALS**',
        'scan_header_short': '🔻 **SHORT SIGNALS**',
        'scan_fail': '❌ Failed to get data. Try again later.',
        'scan_remaining': '📊 Remaining: {}/{}',
        'scan_footer': '⚡ _Limit orders at −3% below Lower BB_',
        'history_empty': '📈 History is empty. Run `/scan` to populate.',
        'history_title': '📈 **Signal History**',
        'history_footer': '💡 _Accurate at the time of scan._',
        'subscribe_on': '📬 **Subscribed!**\n\nDaily top-3 digest at 10:00 MSK.\nUnsubscribe: `/subscribe` again.',
        'subscribe_off': '📭 Subscription disabled. No daily digest.',
        'alert_limit': '🚫 Max {} alerts. Remove one: /delalert SYM',
        'alert_added': 'added',
        'alert_updated': 'updated',
        'alert_disabled': '🔕 Alert **{}** disabled.',
        'alert_none': '🔔 No active alerts.\n`/alert BTCUSDT` — add one.',
        'alert_hit': '🔔 **{}** at Lower BB!\nPrice: `${}` · Lower: `${}`\nEntry: `${}`\n\n⚡ `/scan` — check market',
        'chart_usage': '❌ `/chart BTCUSDT` — specify symbol.\n`/chart BTCUSDT 5` — with timeframe.',
        'chart_building': '📈 Building chart',
        'chart_no_data': '❌ {} — no data for {}.',
        'chart_error': '❌ Error: {}',
        'stats_forbidden': '🚫 Access denied.',
        'daily_header': '🌅 **Good morning!** Top-3 signals for {}',
        'daily_yesterday': '📊 Yesterday: {}/{} wins ({:.0f}% weighted), avg PnL: {:+.1f}%',
        'daily_footer': '⚡ `/scan` — refresh signals',
        'contact_text': (
            '💬 **Contact**\n\n'
            'Questions, ideas, or bugs? Reach out:\n'
            '• Telegram: **@Poliakarm**\n'
            '• Channel: coming soon\n\n'
            'The bot is evolving — your feedback matters! 🚀'
        ),
        'deposit_none': '💰 Deposit not set. `/setrisk 500` — set your deposit.',
        'deposit_min': '❌ Minimum deposit: $10',
        'deposit_usage': '❌ `/setrisk 500` — enter a number (deposit in USDT)',
        'deposit_set': '✅ Deposit: **${:,.0f}**\nRisk per signal: **${:.0f}** (2%)\nLeverage: 3x\n\nNow `/scan` shows position size for your bank.',
        'setrisk_help': '💰 `/setrisk 500` — set deposit size.',
    }
}


def t(key: str, lang: str = 'ru', *args) -> str:
    """Перевод строки по ключу. args — для .format()."""
    text = T.get(lang, T['ru']).get(key, T['ru'].get(key, key))
    if args and text:
        try:
            text = text.format(*args)
        except Exception:
            pass
    return text


# ═══════════════════════════════════════════════════════════════
# Форматирование (с учётом риск-профиля)
# ═══════════════════════════════════════════════════════════════

def fmt_vol(v: float) -> str:
    """Объём в читаемом виде: 5.4B, 98M, 20M."""
    if v >= 1_000_000_000:
        return f"${v/1e9:.1f}B"
    if v >= 1_000_000:
        return f"${v/1e6:.0f}M"
    return f"${v/1e3:.0f}K"


def format_signal_short(s: dict) -> str:
    """Короткий формат для inline / daily digest (LONG)."""
    entry = s['lower_bb'] * 0.97
    fire = "🔥" if s['score'] >= 7 else ("✅" if s['score'] >= 5.5 else "⚠️")
    return (
        f"{fire} **{s['symbol']}** · {s['score']}/10\n"
        f"Вход: `${entry:.4f}` · TP: `${s['middle_bb']:.4f}`\n"
        f"BB: {s['bb_pos']}% · Vol: `${s['turnover']:,.0f}`"
    )


def format_signal_full(s: dict, n: int, deposit: float = 0) -> str:
    """Полный формат сигнала с учётом режима LONG/SHORT/X10."""
    mode = s.get('mode', 'LONG')
    if mode == 'SHORT':
        return format_signal_short_full(s, n, deposit)
    if mode.startswith('SCALP') or mode.startswith('MEAN') or mode.startswith('FUNDING'):
        return format_signal_x10_full(s, n, deposit)
    return format_signal_long_full(s, n, deposit)


def format_signal_long_full(s: dict, n: int, deposit: float = 0) -> str:
    """Полный формат LONG-сигнала."""
    price = s.get('price', 0)
    lower = s.get('lower_bb', 0)
    upper = s.get('upper_bb', 0)
    middle = s.get('middle_bb', 0)
    entry = lower * 0.97
    tp1, tp2 = middle, upper
    sl = lower * 0.93

    score = s['score']
    fire = "🔥" if score >= 7 else ("✅" if score >= 5.5 else "⚠️")
    rsi_str = f" · RSI {s['rsi']}" if s.get('rsi') is not None else ""

    lines = [
        f"{fire} **{s['symbol']}**",
        f"{score}/10",
        f"Цена: `${price:.4f}`",
        f"Вход: `${entry:.4f}`",
        f"TP: `${tp1:.4f}` / `${tp2:.4f}`  SL: `${sl:.4f}`",
        f"BB {s['bb_pos']}% · {s['down_days']}д↓ · {fmt_vol(s['turnover'])}{rsi_str}",
    ]

    if deposit > 0:
        margin = round(deposit * 0.02, 1)
        qty_raw = (margin * 3) / entry if entry > 0 else 0
        if qty_raw >= 1:
            qty_str = f"{qty_raw:.0f}"
        elif qty_raw >= 0.01:
            qty_str = f"{qty_raw:.2f}"
        else:
            qty_str = f"{qty_raw:.4f}"
        lines.append(f"💼 {qty_str} шт · ${margin:.0f} маржи")

    return "\n".join(lines)


def format_signal_x10_full(s: dict, n: int, deposit: float = 0) -> str:
    """Полный формат x10-сигнала (scalp/mean_revert/funding)."""
    mode = s.get('mode', 'X10').replace('_', ' ')
    direction = s.get('direction', 'LONG')
    x10_tags = {'scalp': '⚡ СКАЛЬП', 'mean_revert': '🔄 MEAN REVERT', 'funding_momentum': '💰 FUNDING'}
    tag = '⚡ X10'
    for k, v in x10_tags.items():
        if mode.lower().startswith(k) or s.get('mode', '').startswith(k.upper()):
            tag = v
            break

    emoji = '🟢' if direction == 'LONG' else '🔴'
    score = s['score']
    fire = "🔥" if score >= 8 else ("✅" if score >= 6 else "⚠️")

    lines = [
        f"{fire} {emoji} **{s['symbol']}**  {tag}",
        f"{score:.1f}/10 · {direction} · x10",
        f"Цена: `${s['price']:.4f}`",
        f"Вход: `${s.get('entry', 0):.4f}`",
        f"TP: `${s.get('tp', 0):.4f}`  SL: `${s.get('sl', 0):.4f}`",
        f"BB {s.get('bb_pos', 0)}% · {s.get('tier', 'C')}",
    ]

    if s.get('rsi') is not None:
        lines[-1] += f" · RSI {s['rsi']}"

    if deposit > 0:
        margin = round(deposit * 0.01, 1)  # 1% риска на x10
        entry = s.get('entry') or s.get('price') or 0
        qty_raw = (margin * 10) / entry if entry > 0 else 0
        qty_str = f"{qty_raw:.0f}" if qty_raw >= 1 else f"{qty_raw:.2f}"
        lines.append(f"💼 {qty_str} шт · ${margin:.0f} маржи (x10)")

    return "\n".join(lines)


def format_signal_short_full(s: dict, n: int, deposit: float = 0) -> str:
    """Полный формат SHORT-сигнала."""
    price = s.get('price', 0)
    upper = s.get('upper_bb', 0)
    middle = s.get('middle_bb', 0)
    entry = price * 1.02 if price > 0 else 0  # лимитка +2% выше рынка (ждём отскока)
    tp1, tp2 = middle, s.get('lower_bb', 0)  # TP на Middle и Lower
    tier = s.get('tier', 'C')
    sl_pct = 0.05 if tier in ('C', 'D') else 0.07  # tighter SL for junk, wider for quality (inverted risk)
    sl = entry * (1 + sl_pct)

    score = s['score']
    fire = "🔥" if score >= 7 else ("✅" if score >= 5.5 else "⚠️")
    up_days = s.get('up_days', 0)
    rsi_str = f" · RSI {s['rsi']}" if s.get('rsi') is not None else ""

    lines = [
        f"{fire} 🔻 **{s['symbol']}**",
        f"{score}/10",
        f"Цена: `${price:.4f}`",
        f"Вход: `${entry:.4f}` (+2% рынка)",
        f"TP: `${tp1:.4f}` / `${tp2:.4f}`  SL: `${sl:.4f}` (+{sl_pct*100:.0f}%)",
        f"BB {s['bb_pos']}% · {up_days}д↑ · {fmt_vol(s['turnover'])}{rsi_str}",
    ]

    if deposit > 0:
        margin = round(deposit * 0.02, 1)
        qty_raw = (margin * 3) / entry if entry > 0 else 0
        if qty_raw >= 1:
            qty_str = f"{qty_raw:.0f}"
        elif qty_raw >= 0.01:
            qty_str = f"{qty_raw:.2f}"
        else:
            qty_str = f"{qty_raw:.4f}"
        lines.append(f"💼 {qty_str} шт · ${margin:.0f} маржи")

    return "\n".join(lines)


def share_keyboard(symbol: str) -> InlineKeyboardMarkup:
    """Кнопка «Поделиться сигналом» — switch_inline_query."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Поделиться", switch_inline_query=symbol.replace('USDT', ''))
    ]])


# ═══════════════════════════════════════════════════════════════
# Команды
# ═══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = init_db()
    get_user(conn, user.id)
    lang = get_lang(conn, user.id)
    log_event(conn, user.id, 'start', update)
    conn.close()
    await update.message.reply_text(
        t('start', lang),
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = init_db()
    lang = get_lang(conn, update.effective_user.id)
    log_event(conn, update.effective_user.id, 'help', update)
    conn.close()

    if lang == 'en':
        text = (
            "📖 **Help v4.1**\n\n"
            "**Strategy:** Bollinger Grid (LONG + SHORT)\n"
            "• LONG: limit order at −3% below Lower BB\n"
            "• SHORT: limit order at +2% above market (waiting for bounce)\n"
            "• TP: Middle BB (SHORT) / Middle or Upper (LONG)\n"
            "• SL: +5% Tier A/B, +7% junk C/D (SHORT) / −7% from Lower BB (LONG)\n"
            "• Leverage: 3x (scalp/mean_revert/funding: 10x ⚡)\n\n"
            "**Scoring (9 metrics):**\n"
            "• Tier (S/A/B/C/D)\n"
            "• BB Daily position\n"
            "• 24h Volume\n"
            "• Days falling\n"
            "• Weekly + Monthly BB\n"
            "• Funding rate\n"
            "• Volatility\n"
            "• Bounce quality\n"
            "• RSI(14)\n\n"
            "**Timeframes:**\n"
            "• `/chart SYM` — daily (default)\n"
            "• `/chart SYM 5` — 5-min\n"
            "• `/chart SYM 3` — 3-min\n"
            "• `/chart SYM W` — weekly\n"
            "• `/chart SYM M` — monthly\n\n"
            "**Score Zones:**\n"
            "🔥 ≥7 — excellent entry\n"
            "✅ 5.5–6.9 — good entry\n"
            "⚠️ 3.5–5.4 — with caution\n\n"
            "⚠️ **x10 strategies** (scalp/mean_revert/funding) use 10x leverage — "
            "liquidation at ~10% adverse move. Only for experienced traders.\n\n"
            "⚠️ **Disclaimer:** This bot provides trading signals based on Bollinger Bands. This is not financial advice. "
            "Futures trading with leverage carries high risk — you can lose your entire deposit. "
            "Always start with Bybit testnet. The author is not responsible for your trading decisions."
        )
    else:
        text = (
            "📖 **Справка v4.1**\n\n"
            "**Стратегия:** Bollinger Grid (LONG + SHORT)\n"
            "• LONG: лимитный ордер на −3% ниже Lower BB\n"
            "• SHORT: лимитный ордер на +2% выше рынка (ждём отскока)\n"
            "• TP: Middle BB (SHORT) / Middle или Upper (LONG)\n"
            "• SL: +5% Tier A/B, +7% шлак C/D (SHORT) / −7% от Lower BB (LONG)\n"
            "• Плечо: 3x (scalp/mean_revert/funding: 10x ⚡)\n\n"
            "**Скоринг (9 метрик):**\n"
            "• Фундамент (Tier S/A/B/C/D)\n"
            "• BB Daily позиция\n"
            "• Объём 24ч\n"
            "• Дни падения\n"
            "• Weekly + Monthly BB\n"
            "• Фандинг\n"
            "• Волатильность\n"
            "• Качество отскока\n"
            "• RSI(14)\n\n"
            "**Таймфреймы:**\n"
            "• `/chart SYM` — дневной (по умолч.)\n"
            "• `/chart SYM 5` — 5-минутный\n"
            "• `/chart SYM 3` — 3-минутный\n"
            "• `/chart SYM W` — недельный\n"
            "• `/chart SYM M` — месячный\n\n"
            "**Зоны Score:**\n"
            "🔥 ≥7 — отличный вход\n"
            "✅ 5.5–6.9 — хороший вход\n"
            "⚠️ 3.5–5.4 — с осторожностью\n\n"
            "⚠️ **x10-стратегии** (scalp/mean_revert/funding) используют плечо 10x — "
            "ликвидация при ~10% движении против позиции. Только для опытных.\n\n"
            "⚠️ **Дисклеймер:** Бот даёт торговые сигналы на основе Bollinger Bands. Это не финансовая рекомендация. "
            "Торговля фьючерсами с плечом сопряжена с высоким риском — вы можете потерять весь депозит. "
            "Начинайте с тестнета Bybit. Автор не несёт ответственности за ваши торговые решения."
        )

    await update.message.reply_text(text, parse_mode='MarkdownV2')


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn_tmp = init_db(); log_event(conn_tmp, update.effective_user.id, 'rules', update); conn_tmp.close()
    await update.message.reply_text(
        "📐 **Правила размещения ордеров**\n\n"
        "⏱ **Срок жизни:**\n"
        "• Daily лимитка — GTC (пока не исполнится), но проверять раз в сутки\n"
        "• M5 Grid — **2 часа** макс. Не исполнилась → снять\n"
        "• M3 Turbo — **30 минут** макс.\n\n"
        "🚫 **Когда снимать:**\n"
        "• Цена ушла выше Lower BB >1 часа\n"
        "• BB позиция стала >40% (рынок развернулся)\n"
        "• Прошло 2ч (M5) / 30мин (M3) без исполнения\n\n"
        "📉 **При падении:**\n"
        "• Не усреднять убытки!\n"
        "• 3 сделки подряд в минус → стоп на день\n"
        "• Просадка −3% от утра → закрыть всё\n\n"
        "📈 **При росте:**\n"
        "• Не входить если Daily BB >80%\n"
        "• Weekly >75% → подтянуть SL\n"
        "• >90% Weekly → фиксировать\n\n"
        "🔒 **Риск-менеджмент:**\n"
        "• SL обязателен (−7% от Lower BB)\n"
        "• Не больше 5 позиций\n"
        "• Маржа на монету: макс $40\n"
        "• Плечо: 3x (Daily), 10x (M3)\n\n"
        "_Терпение > скорость._"
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = init_db()
    user = get_user(conn, user_id)
    lang = get_lang(conn, user_id)
    log_event(conn, user_id, 'scan', update)

    # Парсим аргументы: /scan m5, /scan w, /scan TIAUSDT, /scan short
    interval = 'D'
    single_symbol = None
    green_only = False
    mode = 'long'
    if context.args:
        for arg in context.args:
            a = arg.lower()
            if a in ('m5', '5m', '5'):
                interval = '5m'
            elif a in ('m3', '3m', '3'):
                interval = '3m'
            elif a in ('w', 'week', 'weekly'):
                interval = 'W'
            elif a in ('m', 'month', 'monthly'):
                interval = 'W'  # M not supported, fallback to W
            elif a in ('green', 'buy', 'green-only', 'only-green', 'grn'):
                green_only = True
            elif a in ('short', 'sell', 'short-sell', 'sh', 'shortonly'):
                mode = 'short'
            elif a in ('scalp', 'sc'):
                mode = 'scalp'
            elif a in ('mean', 'mr', 'mean_revert', 'mean-revert'):
                mode = 'mean_revert'
            elif a in ('funding', 'fund', 'fm'):
                mode = 'funding'
            elif a in ('rotation', 'rot', 'rotate'):
                mode = 'rotation'
            elif a == 'long':
                mode = 'long'
            else:
                # Может быть тикер
                sym = arg.upper()
                if not sym.endswith('USDT'):
                    sym += 'USDT'
                single_symbol = sym

    allowed, msg = check_scan_allowed(user)
    if not allowed:
        # cooldown message
        if msg.startswith('⏳'):
            parts = msg.split()
            secs = parts[-2] if len(parts) >= 2 else '?'
            await update.message.reply_text(t('scan_cooldown', lang, secs))
        else:
            await update.message.reply_text(t('scan_limit', lang))
        conn.close()
        return

    tf_label = {'D': 'Daily', 'W': 'Weekly', 'M': 'Monthly', '5m': 'M5', '3m': 'M3'}.get(interval, interval)
    mode_label = {'long': 'LONG', 'short': 'SHORT', 'scalp': 'SCALP x10', 'mean_revert': 'MEAN REVERT x10', 'funding': 'FUNDING x10', 'rotation': '🔄 РОТАЦИЯ'}.get(mode, mode)
    scanning_text = t('scanning', lang)
    if mode == 'short':
        scanning_text = scanning_text.replace('сканирую', 'сканирую SHORT').replace('Scanning', 'Scanning SHORT')
    elif mode == 'rotation':
        scanning_text = scanning_text.replace('сканирую', 'проверяю ротации').replace('Scanning', 'Checking rotations')
        status_msg = await update.message.reply_text(f"{scanning_text}...")
        from funding_rotation import check_funding_rotation
        from api import fetch_positions
        pos = await asyncio.to_thread(fetch_positions)
        rotations = await asyncio.to_thread(check_funding_rotation, pos or {})
        if not rotations:
            await status_msg.edit_text('✅ Нет позиций с невыгодным фандингом. Все ок.')
        else:
            lines = ['💰 **Ротации фандинга:**\n']
            for i, r in enumerate(rotations, 1):
                side_emoji = '🟢' if r['side'] == 'Buy' else '🔴'
                _from = r['from']; _to = r['to']; _cf = r['current_funding']
                _nf = r['new_funding']; _d = r['delta']; _bb = r['bb_pct']; _p = r['price']
                lines.append(
                    f"{i}. {side_emoji} {_from} → **{_to}**\n"
                    f"   Фандинг: {_cf}% → {_nf}% (Δ{_d}%)\n"
                    f"   BB: {_bb}% · Цена: ${_p:.4f}"
                )
            lines.append(f"\n⚡ Всего кандидатов: {len(rotations)}. `/scan rotation` — обновить.")
            await status_msg.edit_text('\n'.join(lines), parse_mode='MarkdownV2')
        conn.close()
        return
    elif mode in ('scalp', 'mean_revert', 'funding'):
        mode_names = {'scalp': 'SCALP x10', 'mean_revert': 'MEAN REVERT x10', 'funding': 'FUNDING x10'}
        scanning_text = scanning_text.replace('сканирую', f'сканирую {mode_names[mode]}').replace('Scanning', f'Scanning {mode_names[mode]}')
    status_msg = await update.message.reply_text(f"{scanning_text} ({tf_label})...")
    signals = await get_cached_scan(interval, user_id, symbol=single_symbol, green_only=green_only, mode=mode)
    if not signals:
        await status_msg.edit_text(t('scan_fail', lang))
        conn.close()
        return

    now = datetime.now().strftime('%d.%m %H:%M')
    header_key = 'scan_header_short' if mode == 'short' else 'scan_header'
    scan_header = t(header_key, lang) if header_key in T.get(lang, {}) else t('scan_header', lang)
    lines = [f"{scan_header} · {now} · {tf_label}\n"]
    dep = user.get('deposit', 0)
    for i, s in enumerate(signals, 1):
        lines.append(format_signal_full(s, i, dep))
        lines.append("")

    remaining = MAX_SCANS_PER_DAY - user['scans_today'] - 1
    lines.append(t('scan_remaining', lang, max(0, remaining), MAX_SCANS_PER_DAY))
    lines.append(t('scan_footer', lang))

    await status_msg.edit_text("\n".join(lines), reply_markup=share_keyboard(signals[0]['symbol']))
    update_scan_count(conn, user_id)
    conn.close()


# ══ #5 /setrisk ══

async def cmd_setrisk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        conn = init_db()
        user = get_user(conn, user_id)
        lang = get_lang(conn, user_id)
        d = user.get('deposit', 0)
        conn.close()
        if d > 0:
            await update.message.reply_text(f"💰 Текущий депозит: **${d:,.0f}**\n2% риска = ${d*0.02:.0f} на сигнал")
        else:
            await update.message.reply_text(t('deposit_none', lang))
        return
    try:
        deposit = float(args[0])
        if deposit < 10:
            conn = init_db()
            lang = get_lang(conn, user_id)
            conn.close()
            await update.message.reply_text(t('deposit_min', lang))
            return
    except ValueError:
        conn = init_db()
        lang = get_lang(conn, user_id)
        conn.close()
        await update.message.reply_text(t('deposit_usage', lang))
        return

    conn = init_db()
    lang = get_lang(conn, user_id)
    get_user(conn, user_id)
    conn.execute('UPDATE users SET deposit=? WHERE user_id=?', (deposit, user_id))
    log_event(conn, user_id, 'setrisk', update)
    conn.commit()
    conn.close()
    margin = round(deposit * 0.02, 0)
    await update.message.reply_text(t('deposit_set', lang, deposit, margin))


# ══ #4 /history ══

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = init_db()
    user_id = update.effective_user.id
    lang = get_lang(conn, user_id)
    log_event(conn, user_id, 'history', update)

    rows = conn.execute(
        'SELECT ts, symbol, score, price, entry, tp1, tp2, sl FROM signals ORDER BY id DESC LIMIT 10'
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(t('history_empty', lang))
        return

    lines = [t('history_title', lang) + "\n"]
    for ts, sym, score, price, entry, tp1, tp2, sl in rows:
        dt = datetime.fromtimestamp(ts).strftime('%d.%m %H:%M')
        fire = "🔥" if score >= 7 else ("✅" if score >= 5.5 else "⚠️")
        lines.append(
            f"`{dt}`\n"
            f"{fire} **{sym}**\n"
            f"{score}/10\n"
            f"Цена: `${price:.4f}`\n"
            f"Вход: `${entry:.4f}`\n"
            f"TP: `${tp1:.4f}` / `${tp2:.4f}`  SL: `${sl:.4f}`"
        )

    lines.append("\n" + t('history_footer', lang))
    # Кнопка «Поделиться» для последнего сигнала
    kb = share_keyboard(rows[0][1]) if rows else None
    await update.message.reply_text("\n".join(lines), reply_markup=kb)


# ══ #2 /subscribe ══

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_lang(init_db(), user_id)

    if is_pro(user_id):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Сканировать", callback_data="scan"),
            InlineKeyboardButton("📋 Статус", callback_data="status")
        ]])
        await update.message.reply_text(
            "✅ **GridSignal Pro** активен\n"
            "Безлимитные сканы 24/7, алерты в личку\n\n"
            "/scan — новый скан\n/status — статус подписки",
            reply_markup=kb
        )
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⭐ Stars ({PRO_PRICE_STARS})", callback_data="pro_buy")],
            [InlineKeyboardButton(f"💎 TON (~400₽)", callback_data="pro_buy_ton")]
        ])
        await update.message.reply_text(
            "🔔 **GridSignal** — Bollinger Grid сигналы\n\n"
            "🆓 **Бесплатно:** 3 /scan в день\n"
            f"⭐ **Pro:** безлимит, алерты в личку\n\n"
            "💳 **Способы оплаты:**\n"
            f"• Telegram Stars — {PRO_PRICE_STARS} Stars\n"
            "• TON (CryptoBot) — ~2 TON\n\n"
            "Выбери способ:",
            reply_markup=kb
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = init_pro_db()
    row = conn.execute(
        "SELECT paid_at, expires_at, active FROM pro_users WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()

    if row and row[2]:
        paid, exp, _ = row
        await update.message.reply_text(
            f"✅ **GridSignal Pro**\nОплачен: {paid[:10]}\nИстекает: {exp[:10]}"
        )
    else:
        await update.message.reply_text("Нет активной подписки.\n⭐ /subscribe — купить Pro")

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка платежа: только grid signal pro."""
    query = update.pre_checkout_query
    if query.invoice_payload != "gridsignal_pro_30d":
        await query.answer(ok=False, error_message="Unknown product")
        return
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Активация Pro после оплаты Stars."""
    user_id = update.effective_user.id
    username = update.effective_user.username or str(user_id)
    payload = update.message.successful_payment.invoice_payload

    if payload != "gridsignal_pro_30d":
        return

    # Idempotency: skip already-processed payments (scout audit fix #3)
    charge_id = update.message.successful_payment.telegram_payment_charge_id
    conn = init_pro_db()
    existing = conn.execute(
        "SELECT 1 FROM pro_users WHERE user_id=? AND charge_id=?", (user_id, charge_id)
    ).fetchone()
    conn.close()
    if existing:
        return  # duplicate callback — already processed

    activate_pro(user_id, username, charge_id)

    await update.message.reply_text(
        "🎉 **GridSignal Pro активирован!**\n"
        "30 дней полного доступа.\n\n"
        "/scan — безлимитные сканы\n"
        "/status — статус подписки\n"
        "🔔 Алерты будут приходить автоматически"
    )

    # Notify admin
    try:
        await context.bot.send_message(
            chat_id=GRIDSIGNAL_ADMIN_NOTIFY_CHAT,
            text=f"🎉 Новый GridSignal Pro!\n@{username} ({user_id})\n⭐ {PRO_PRICE_STARS} Stars"
        )
    except Exception:
        pass

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка инлайн-кнопок."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "pro_buy":
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title="GridSignal Pro",
            description="Bollinger Grid сигналы 24/7\nLONG+SHORT • Алерты • Приватный канал\n30 дней доступа",
            payload="gridsignal_pro_30d",
            currency="XTR",
            prices=[{"label": "Pro (30 дней)", "amount": PRO_PRICE_STARS}],
            provider_token="",
            start_parameter="grid_pro",
        )
    elif data == "pro_buy_ton":
        await query.message.reply_text("⏳ Создаю счёт...")
        invoice_url, invoice_id = await create_ton_invoice(query.from_user.id)
        if invoice_url:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("💎 Оплатить TON", url=invoice_url),
                InlineKeyboardButton("✅ Я оплатил", callback_data=f"check_ton_{invoice_id}")
            ]])
            await query.message.reply_text(
                "💎 **Оплата через CryptoBot**\n\n"
                "1. Нажми «Оплатить TON»\n"
                "2. Оплати ~2 TON в @CryptoBot\n"
                "3. Вернись и нажми «Я оплатил»\n\n"
                "После проверки — Pro активируется автоматически.",
                reply_markup=kb
            )
        else:
            await query.message.reply_text("❌ Ошибка создания счёта. Попробуй Stars или позже.")
    elif data == "scan":
        await cmd_scan(update, context)
    elif data == "status":
        await cmd_status(update, context)
    elif data.startswith("check_ton_"):
        invoice_id = int(data.split("_")[-1])

        # Rate limit: не чаще 10 сек между проверками TON на пользователя
        now_ts = time.time()
        last = _ton_cooldown.get(query.from_user.id, 0)
        if now_ts - last < 10:
            await query.answer("⏳ Подождите 10 сек между проверками", show_alert=False)
            return
        _ton_cooldown[query.from_user.id] = now_ts

        await query.message.reply_text("⏳ Проверяю платёж...")

        # Проверить что инвойс принадлежит этому пользователю
        ton_conn = init_ton_db()
        inv_row = ton_conn.execute(
            "SELECT user_id FROM ton_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
        ton_conn.close()
        if not inv_row or inv_row[0] != query.from_user.id:
            await query.message.reply_text("❌ Этот счёт не ваш.")
            return

        if await check_ton_payment(invoice_id):
            activate_pro(query.from_user.id,
                        query.from_user.username or str(query.from_user.id))
            await query.message.reply_text(
                "🎉 **TON-платёж получен! GridSignal Pro активирован!**\n\n"
                "/scan — безлимитные сканы\n/status — статус подписки"
            )
        else:
            await query.message.reply_text("❌ Платёж не найден. Оплати счёт в @CryptoBot и нажми кнопку снова.")


async def deprovision_expired(context: ContextTypes.DEFAULT_TYPE):
    """Деактивировать истекшие Pro-подписки."""
    conn = init_pro_db()
    now = _now()
    expired = conn.execute(
        "SELECT user_id FROM pro_users WHERE active=1 AND expires_at < ?", (now,)
    ).fetchall()
    for (uid,) in expired:
        conn.execute("UPDATE pro_users SET active=0 WHERE user_id=?", (uid,))
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="⚠️ GridSignal Pro истёк. Продлить: /subscribe"
            )
        except Exception:
            pass
    conn.commit()
    conn.close()
    return len(expired)

async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE):
    """Рассылка утренней сводки подписчикам."""
    conn = init_db()
    subs = conn.execute('SELECT user_id, lang FROM users WHERE subscribed=1').fetchall()
    if not subs:
        conn.close()
        return

    signals = await get_cached_scan()
    conn.close()

    if not signals:
        return

    now = datetime.now().strftime('%d.%m %H:%M')

    # Open one connection for all subscribers
    conn2 = init_db()
    day_ago = time.time() - 86400
    # Каждому подписчику — на его языке
    for uid, lang_row in subs:
        lang = lang_row if lang_row else 'ru'
        lines = [t('daily_header', lang, now) + "\n"]

        # Винрейт за вчера
        yday_sigs = conn2.execute(
            "SELECT outcome FROM signals WHERE outcome_ts > ? AND outcome IS NOT NULL",
            (day_ago,)
        ).fetchall()
        if yday_sigs:
            y_total = len(yday_sigs)
            y_wins = sum(1 for r in yday_sigs if r[0] in ('TP1', 'TP2'))
            y_weighted = sum(1.0 if r[0] == 'TP2' else (0.2 if r[0] == 'TP1' else 0) for r in yday_sigs)
            y_pnl_rows = conn2.execute(
                "SELECT pnl_pct FROM signals WHERE outcome_ts > ? AND pnl_pct IS NOT NULL",
                (day_ago,)
            ).fetchall()
            avg_yday_pnl = sum(r[0] for r in y_pnl_rows) / len(y_pnl_rows) if y_pnl_rows else 0
            lines.append(t('daily_yesterday', lang, y_wins, y_total, y_weighted/y_total*100, avg_yday_pnl))

        for s in signals[:3]:
            lines.append(format_signal_short(s))
            lines.append("")

        # Personal 24h stats
        my_sigs = conn2.execute(
            'SELECT outcome, pnl_pct FROM signals WHERE user_id=? AND outcome IS NOT NULL AND outcome_ts > ?',
            (uid, day_ago)
        ).fetchall()
        if my_sigs:
            my_total = len(my_sigs)
            my_wins = sum(1 for r in my_sigs if r[0] in ('TP1', 'TP2'))
            pnl_vals = [r[1] for r in my_sigs if r[1] is not None]
            my_avg = sum(pnl_vals)/len(pnl_vals) if pnl_vals else 0
            lines.append(f"🎯 Твой винрейт за 24ч: {my_wins}/{my_total} ({my_wins/my_total*100:.0f}%) · avg PnL: {my_avg:+.1f}%" if lang == 'ru'
                        else f"🎯 Your 24h winrate: {my_wins}/{my_total} ({my_wins/my_total*100:.0f}%) · avg PnL: {my_avg:+.1f}%")

        lines.append(t('daily_footer', lang))

        msg = "\n".join(lines)
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
        except Exception:
            pass

    conn2.close()


# ══ #3 /alert ══

async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    conn = init_db()
    get_user(conn, user_id)
    lang = get_lang(conn, user_id)

    if not args:
        # Показать список алертов с кнопками удаления
        alerts = conn.execute('SELECT symbol FROM alerts WHERE user_id=? AND active=1', (user_id,)).fetchall()
        conn.close()
        if alerts:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f'❌ {a[0]}', callback_data=f'delalert:{a[0]}')]
                for a in alerts
            ])
            syms = ", ".join(a[0] for a in alerts)
            await update.message.reply_text(
                f"🔔 Активные алерты:\n{syms}\n\nНажми для удаления:",
                reply_markup=keyboard
            )
        else:
            await update.message.reply_text(t('alert_none', lang))
        return

    symbol = args[0].upper()
    if not symbol.endswith('USDT'):
        symbol += 'USDT'

    # /alert TIAUSDT bb25 — алерт на BB-зону
    bb_zone = None
    if len(args) > 1:
        for a in args[1:]:
            if a.lower().startswith('bb'):
                try:
                    bb_zone = float(a[2:])
                except ValueError:
                    pass

    # Лимит алертов
    alert_count = conn.execute('SELECT COUNT(*) FROM alerts WHERE user_id=? AND active=1', (user_id,)).fetchone()[0]
    if alert_count >= MAX_ALERTS_PER_USER:
        conn.close()
        await update.message.reply_text(t('alert_limit', lang, MAX_ALERTS_PER_USER))
        return

    # Validate symbol
    safe_sym = _valid_symbol(symbol)
    if not safe_sym:
        conn.close()
        await update.message.reply_text(f"❌ Некорректный символ: {symbol}")
        return
    # Получить текущий Lower BB (и BB-позицию для zone-алертов)
    lower_bb = 0
    cur_bb_pos = 100
    try:
        result = subprocess.run(['python3', '-c', f'''
import json, subprocess, re
r = subprocess.run(["{BYBIT_CLI}", "bb", "{safe_sym}", "D"], capture_output=True, text=True, timeout=20)
m_lower = re.search(r"Lower:.*?\\$([0-9.]+)", r.stdout)
m_pos = re.search(r"Позиция:\\s*([0-9.]+)%", r.stdout)
lower = float(m_lower.group(1)) if m_lower else 0
pos = float(m_pos.group(1)) if m_pos else 100
print(f"{{lower}}\\n{{pos}}")
'''], capture_output=True, text=True, timeout=25)
        parts = result.stdout.strip().split('\n')
        lower_bb = float(parts[0]) if parts[0] else 0
        cur_bb_pos = float(parts[1]) if len(parts) > 1 else 100
    except Exception:
        lower_bb = 0

    if lower_bb <= 0:
        conn.close()
        await update.message.reply_text(f"❌ Не удалось получить данные для {symbol}")
        return

    # Проверить нет ли уже
    existing = conn.execute('SELECT 1 FROM alerts WHERE user_id=? AND symbol=?', (user_id, symbol)).fetchone()
    if existing:
        conn.execute('UPDATE alerts SET active=1, lower_bb=?, bb_zone=? WHERE user_id=? AND symbol=?',
                     (lower_bb, bb_zone, user_id, symbol))
        action = t('alert_updated', lang)
    else:
        conn.execute('INSERT INTO alerts (user_id, symbol, lower_bb, bb_zone) VALUES (?,?,?,?)',
                     (user_id, symbol, lower_bb, bb_zone))
        action = t('alert_added', lang)

    log_event(conn, user_id, 'alert', update)
    conn.commit()
    conn.close()

    entry = lower_bb * 0.97
    if bb_zone is not None:
        msg = (f"🔔 Алерт {action}: **{symbol}**\n"
               f"BB-зона: < **{bb_zone:.0f}%** (сейчас {cur_bb_pos:.0f}%)\n"
               f"Lower BB: `${lower_bb:.4f}`\n"
               f"Точка входа: `${entry:.4f}` (−3%)\n\n"
               f"Бот проверит BB-позицию и пришлёт уведомление когда BB ≤ {bb_zone:.0f}%.")
    else:
        msg = (f"🔔 Алерт {action}: **{symbol}**\n"
               f"Lower BB: `${lower_bb:.4f}`\n"
               f"Точка входа: `${entry:.4f}` (−3%)\n\n"
               f"Бот проверит цену и пришлёт уведомление.")
    await update.message.reply_text(msg)


async def cmd_delalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        conn = init_db()
        alerts = conn.execute('SELECT symbol FROM alerts WHERE user_id=? AND active=1', (user_id,)).fetchall()
        conn.close()
        if alerts:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f'❌ {a[0]}', callback_data=f'delalert:{a[0]}')]
                for a in alerts
            ])
            await update.message.reply_text("🗑 Выбери алерт для удаления:", reply_markup=keyboard)
        else:
            await update.message.reply_text(t('alert_none', lang))
        return

    symbol = args[0].upper()
    if not symbol.endswith('USDT'):
        symbol += 'USDT'

    conn = init_db()
    lang = get_lang(conn, user_id)
    conn.execute('UPDATE alerts SET active=0 WHERE user_id=? AND symbol=?', (user_id, symbol))
    conn.commit()
    conn.close()
    await update.message.reply_text(t('alert_disabled', lang, symbol))


async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Фоновый цикл: проверка алертов каждые 2 минуты."""
    conn = init_db()
    alerts = conn.execute('SELECT user_id, symbol, lower_bb FROM alerts WHERE active=1').fetchall()
    if not alerts:
        conn.close()
        return

    # Группируем по символам чтобы не дёргать API на каждого юзера
    alert_rows = conn.execute('SELECT user_id, symbol, lower_bb, bb_zone FROM alerts WHERE active=1').fetchall()
    if not alert_rows:
        conn.close()
        return

    symbols = set(a[1] for a in alert_rows)
    prices = {}
    bb_positions = {}
    for sym in symbols:
        safe_sym = _valid_symbol(sym)
        if not safe_sym:
            continue
        try:
            r = subprocess.run(['python3', '-c', f'''
import json, subprocess, re
r = subprocess.run(["{BYBIT_CLI}", "bb", "{safe_sym}", "D"], capture_output=True, text=True, timeout=20)
m_price = re.search(r"Текущая:[^$]*\\$([0-9.]+)", r.stdout)
m_lower = re.search(r"Lower:.*?\\$([0-9.]+)", r.stdout)
m_pos = re.search(r"Позиция:\\s*([0-9.]+)%", r.stdout)
price = float(m_price.group(1)) if m_price else 0
lower = float(m_lower.group(1)) if m_lower else 0
pos = float(m_pos.group(1)) if m_pos else 100
print(f"{{price}}\\n{{lower}}\\n{{pos}}")
'''], capture_output=True, text=True, timeout=25)
            parts = r.stdout.strip().split('\n')
            prices[sym] = float(parts[0]) if parts[0] else 0
            bb_positions[sym] = float(parts[2]) if len(parts) > 2 else 100
        except Exception:
            pass

    for user_id, symbol, lower_bb, bb_zone in alert_rows:
        price = prices.get(symbol, 0)
        bb_pos = bb_positions.get(symbol, 100)

        triggered = False
        trigger_msg = ""

        if bb_zone is not None and bb_zone > 0 and bb_pos <= bb_zone:
            triggered = True
            trigger_msg = f"BB вошёл в зону ≤{bb_zone:.0f}%! (сейчас {bb_pos:.0f}%)\n"
        elif bb_zone is None and price and price <= lower_bb:
            triggered = True
            trigger_msg = ""

        if triggered:
            entry = lower_bb * 0.97
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🔔 **{symbol}** — алерт!\n"
                     f"{trigger_msg}"
                     f"Цена: `${price:.4f}` · Lower: `${lower_bb:.4f}`\n"
                     f"BB: {bb_pos:.0f}% · Вход: `${entry:.4f}`\n\n"
                     f"⚡ `/scan` — проверить рынок"
            )
            conn.execute('UPDATE alerts SET active=0 WHERE user_id=? AND symbol=?', (user_id, symbol))
            conn.commit()

    conn.close()


# ══ #1 Inline ══

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик inline-запросов: @Gridbolbot BTCUSDT — только для Pro."""
    query = update.inline_query.query.strip().upper()
    user_id = update.effective_user.id

    # Gate: Pro-only (scout audit fix #1)
    if not is_pro(user_id):
        await update.inline_query.answer([], cache_time=300,
            switch_pm_text="🔒 Pro only",
            switch_pm_parameter="pro_required")
        return

    if not query:
        await update.inline_query.answer([], cache_time=10)
        return

    symbol = query if query.endswith('USDT') else query + 'USDT'

    # Inline-кэш
    global _inline_cache
    now = time.time()
    if symbol in _inline_cache and (now - _inline_cache[symbol]['ts']) < INLINE_CACHE_TTL:
        cached = _inline_cache[symbol]
        await update.inline_query.answer(cached['results'], cache_time=INLINE_CACHE_TTL)
        return
    raw_symbol = query if query.endswith('USDT') else query + 'USDT'
    symbol = _valid_symbol(raw_symbol)
    if not symbol:
        return  # silently ignore invalid symbols
    try:
        result = await asyncio.to_thread(subprocess.run,
            ['python3', '-c', f'''\nimport json, subprocess, sys
sys.path.insert(0, "{os.path.dirname(SCANNER_SCRIPT)}")
from gridsignal_scanner import score_coin, get_top_tickers

# Ищем конкретную монету
tickers = [t for t in get_top_tickers(100) if t["symbol"] == "{symbol}"]
if not tickers:
    # fallback: запросить тикер напрямую
    r = subprocess.run(["bybit", "raw", "GET", "/v5/market/tickers?category=linear&symbol={symbol}"],
                       capture_output=True, text=True, timeout=10)
    try:
        data = json.loads(r.stdout)
        tickers = data.get("result", {{}}).get("list", [])
    except Exception: pass

if tickers:
    s = score_coin("{symbol}", tickers[0])
    if s:
        print(json.dumps(s))
'''], capture_output=True, text=True, timeout=15)
        if result.stdout.strip():
            s = json.loads(result.stdout)
            entry = s['lower_bb'] * 0.97
            fire = "🔥" if s['score'] >= 7 else ("✅" if s['score'] >= 5.5 else "⚠️")
            text = (
                f"<b>🚀 {s['symbol']}</b> · Score {s['score']}/10 {fire}\n\n"
                f"💰 Цена: ${s['price']:.4f}\n"
                f"📥 Вход: ${entry:.4f} (−3% ниже Lower)\n"
                f"🎯 TP: ${s['middle_bb']:.4f} / ${s['upper_bb']:.4f}\n"
                f"🛑 SL: ${s['lower_bb']*0.93:.4f}\n"
                f"📊 BB: {s['bb_pos']}% · Vol: ${s['turnover']:,.0f}\n"
                f"📐 Фундамент: Tier {s['tier']}"
            )
            results = [
                InlineQueryResultArticle(
                    id=symbol,
                    title=f"{symbol} — Score {s['score']}/10 {fire}",
                    description=f"Вход: ${entry:.4f} | BB: {s['bb_pos']}% | Vol: ${s['turnover']:,.0f}",
                    input_message_content=InputTextMessageContent(text, parse_mode="HTML")
                )
            ]
            await update.inline_query.answer(results, cache_time=INLINE_CACHE_TTL)
            _inline_cache[symbol] = {'ts': time.time(), 'results': results}
            return
    except Exception:
        pass

    await update.inline_query.answer([
        InlineQueryResultArticle(
            id='notfound',
            title=f"❌ {query} не найден",
            description="Проверь тикер или попробуй /scan в боте",
            input_message_content=InputTextMessageContent(
                f"<b>❌ {query}</b> — монета не найдена в топ-100 по объёму.", parse_mode="HTML"
            )
        )
    ], cache_time=30)


# ═══════════════════════════════════════════════════════════════
# Обработчик кнопок + команды
# ═══════════════════════════════════════════════════════════════

async def cmd_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обратная связь."""
    conn = init_db()
    lang = get_lang(conn, update.effective_user.id)
    conn.close()
    await update.message.reply_text(t('contact_text', lang))


# ══ #4 /fear — индекс настроения рынка ══

async def cmd_fear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Индекс страха на основе BB позиций топ-10 монет."""
    import random, subprocess, json, re

    await update.message.reply_text("😱 Сканирую настроение рынка...")
    conn_tmp = init_db(); log_event(conn_tmp, update.effective_user.id, 'fear', update); conn_tmp.close()

    try:
        r = subprocess.run(['bybit', 'raw', 'GET', '/v5/market/tickers?category=linear'],
                          capture_output=True, text=True, timeout=15)
        tickers = json.loads(r.stdout).get('result', {}).get('list', [])
        tickers.sort(key=lambda t: float(t.get('turnover24h', 0)), reverse=True)
    except Exception:
        await update.message.reply_text("❌ Не удалось получить данные.")
        return

    # Берём топ-10 и считаем BB позицию
    checked = 0
    avg_bb = 0
    in_zone = 0
    for t in tickers[:20]:
        sym = t['symbol']
        if not sym.endswith('USDT'):
            continue
        if checked >= 10:
            break
        try:
            r = await asyncio.to_thread(subprocess.run, ['bybit', 'bb', sym, 'D'],
                                       capture_output=True, text=True, timeout=10)
            m = re.search(r'Позиция:\s*([0-9.]+)%', r.stdout)
            if m:
                bb = float(m.group(1))
                avg_bb += bb
                if bb < 25:
                    in_zone += 1
                checked += 1
        except Exception:
            pass

    if checked < 3:
        await update.message.reply_text("❌ Мало данных.")
        return

    avg_bb /= checked
    in_pct = in_zone / checked * 100

    # Интерпретация
    if avg_bb < 15:
        mood = "🟢 **ЖАДНОСТЬ**"
        desc = "Рынок у нижних полос. Лучшее время для входа по стратегии."
        advice = "Ставь лимитки, не жди."
    elif avg_bb < 30:
        mood = "🟡 **ОПТИМИЗМ**"
        desc = "Много монет в зоне покупки. Хорошие возможности."
        advice = "Выбирай лучших по Score, не распыляйся."
    elif avg_bb < 50:
        mood = "⚪ **НЕЙТРАЛ**"
        desc = "Рынок в середине канала. Входы есть, но выборочно."
        advice = "Только Score ≥7, остальное — мимо."
    elif avg_bb < 70:
        mood = "🟠 **ОСТОРОЖНОСТЬ**"
        desc = "Цены подтянулись к середине. Мало точек входа."
        advice = "Жди отката к Lower BB. Не FOMO-и."
    else:
        mood = "🔴 **СТРАХ**"
        desc = "Рынок у верхних полос. Перегрев. Входить опасно."
        advice = "Фиксируй профит, новые входы — только после коррекции."

    bar = "▓" * max(1, int(avg_bb / 5)) + "░" * (20 - max(1, int(avg_bb / 5)))

    await update.message.reply_text(
        f"{mood}\n\n"
        f"📊 Средняя BB позиция топ-{checked}: **{avg_bb:.0f}%**\n"
        f"`{bar}`\n\n"
        f"🟢 В зоне покупки: **{in_zone}/{checked}** ({in_pct:.0f}%)\n\n"
        f"{desc}\n\n"
        f"💡 _{advice}_"
    )


# ══ #2 /horoscope — крипто-гороскоп ══

HOROSCOPE_SIGNS = ["♈ BTC-Овен", "♉ ETH-Телец", "♊ SOL-Близнецы", "♋ XRP-Рак",
                   "♌ ADA-Лев", "♍ AVAX-Дева", "♎ DOT-Весы", "♏ LINK-Скорпион",
                   "♐ UNI-Стрелец", "♑ LTC-Козерог", "♒ SUI-Водолей", "♓ NEAR-Рыбы"]

HOROSCOPE_TEXTS = [
    "Звёзды шепчут: Lower BB близко. Готовь лимитку, смертный.",
    "День удачный... но ты всё равно продашь на хаях.",
    "Марс в ретрограде. Не входи с плечом >3x, пожалеешь.",
    "Венера шепчет: фиксируй профит. Жадность — путь к ликвидации.",
    "Юпитер благоволит. Но SL всё равно поставь.",
    "Сатурн предупреждает: три убытка подряд — стоп на день.",
    "Уран в секстиле: возможен пампинг. TP уже стоит?",
    "Луна в фазе накопления. Докупка разрешена.",
    "Меркурий в пользу трейдеров. Score сегодня злой.",
    "Плутон советует: закрой терминал, иди гулять. Рынок никуда не денется.",
    "Солнце входит в Lower BB. Идеальный момент для входа.",
    "Ретроградный Нептун: проверь SL. Дважды.",
    "Асцендент в Upper BB. Фиксируй, не жадничай.",
    "Кету в Рыбах: сегодня /scan покажет что-то интересное.",
]


# ══ /top — топ гейнеров/лузеров ══

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Топ-5 гейнеров и лузеров из watchlist."""
    conn_tmp = init_db(); log_event(conn_tmp, update.effective_user.id, 'top', update); conn_tmp.close()
    status_msg = await update.message.reply_text("📊 Смотрим топ движения...")

    try:
        r = subprocess.run([BYBIT_CLI, 'raw', 'GET', '/v5/market/tickers?category=linear'],
                          capture_output=True, text=True, timeout=15)
        tickers = json.loads(r.stdout).get('result', {}).get('list', [])
    except Exception:
        await status_msg.edit_text("❌ Не удалось получить данные.")
        return

    # Сортируем по изменению цены
    with_change = []
    for t in tickers:
        sym = t['symbol']
        if not sym.endswith('USDT'):
            continue
        try:
            chg = float(t.get('price24hPcnt', 0)) * 100
            price = float(t['lastPrice'])
            vol = float(t.get('turnover24h', 0))
            if vol > 1_000_000:  # отсекаем мусор
                with_change.append((sym, chg, price, vol))
        except Exception:
            continue

    with_change.sort(key=lambda x: x[1], reverse=True)
    gainers = with_change[:5]
    losers = with_change[-5:][::-1]

    lines = ["📊 **Топ движения за 24ч**\n"]
    lines.append("🟢 **Гейнеры:**")
    for sym, chg, price, vol in gainers:
        lines.append(f"  {sym}: `+{chg:.1f}%` · ${price:.4f} · {fmt_vol(vol)}")

    lines.append("\n🔴 **Лузеры:**")
    for sym, chg, price, vol in losers:
        lines.append(f"  {sym}: `{chg:.1f}%` · ${price:.4f} · {fmt_vol(vol)}")

    lines.append("\n💡 `/scan` — проверить точки входа")
    await status_msg.edit_text("\n".join(lines))

# ══ Трекинг исходов сигналов ══

async def check_outcomes(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая проверка: сработали ли старые сигналы.
    v4.0: батчевый запрос цен (один API-вызов вместо N)."""
    conn = init_db()
    # Берём сигналы без исхода
    rows = conn.execute(
        'SELECT id, symbol, entry, tp1, tp2, sl, ts, timeframe, user_id, mode FROM signals WHERE outcome IS NULL'
    ).fetchall()
    if not rows:
        conn.close()
        return

    now = time.time()

    # Сроки жизни по таймфрейму (в днях)
    TF_EXPIRY = {'D': 7, 'W': 14, 'M': 30, '5': 2, '3': 1}

    # Батчевый запрос: все цены за один вызов
    prices = {}
    try:
        r = subprocess.run(
            [BYBIT_CLI, 'raw', 'GET', '/v5/market/tickers?category=linear'],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(r.stdout)
        for t in data.get('result', {}).get('list', []):
            sym = t.get('symbol', '')
            if sym.endswith('USDT'):
                try:
                    prices[sym] = float(t['lastPrice'])
                except (KeyError, ValueError):
                    pass
    except Exception:
        pass

    notifications = []  # (user_id, text) для отправки

    for sid, sym, entry, tp1, tp2, sl, sig_ts, tf, uid, sig_mode in rows:
        price = prices.get(sym)
        if not price:
            continue

        outcome = None
        pnl_pct = None
        expiry_days = TF_EXPIRY.get(tf, 7) if tf else 7

        # Приоритет: TP2 > TP1 > SL > EXPIRED. SHORT: зеркальная логика
        if sig_mode == 'short':
            # SHORT: прибыль при падении цены
            if price <= tp2:
                outcome = 'TP2'
                pnl_pct = round((entry - tp2) / entry * 100, 2)
            elif price <= tp1:
                outcome = 'TP1'
                pnl_pct = round((entry - tp1) / entry * 100, 2)
            elif price >= sl:
                if (now - sig_ts) < 48 * 3600:
                    outcome = 'SL'
                    pnl_pct = round((entry - sl) / entry * 100, 2)
                else:
                    outcome = 'EXPIRED'
                    pnl_pct = 0
            elif (now - sig_ts) > expiry_days * 86400:
                outcome = 'EXPIRED'
                pnl_pct = round((entry - price) / entry * 100, 2)
        else:
            # LONG: прибыль при росте цены
            if price >= tp2:
                outcome = 'TP2'
                pnl_pct = round((tp2 - entry) / entry * 100, 2)
            elif price >= tp1:
                outcome = 'TP1'
                pnl_pct = round((tp1 - entry) / entry * 100, 2)
            elif price <= sl:
                if (now - sig_ts) < 48 * 3600:
                    outcome = 'SL'
                    pnl_pct = round((sl - entry) / entry * 100, 2)
                else:
                    outcome = 'EXPIRED'
                    pnl_pct = 0
            elif (now - sig_ts) > expiry_days * 86400:
                outcome = 'EXPIRED'
                pnl_pct = round((price - entry) / entry * 100, 2)

        if outcome:
            conn.execute(
                'UPDATE signals SET outcome=?, outcome_ts=?, pnl_pct=? WHERE id=?',
                (outcome, now, pnl_pct, sid)
            )
            conn.commit()

            # Уведомление — только для TP (позитив), SL молча в базу
            if uid and outcome in ('TP1', 'TP2'):
                emoji = {'TP2': '🎯🎯', 'TP1': '🎯', 'SL': '🛑'}
                notifications.append((uid, sym, outcome, pnl_pct, emoji.get(outcome, '📊')))

    conn.close()

    # Отправляем уведомления (группируем по user_id)
    for uid, sym, outcome, pnl, emoji in notifications:
        try:
            sign = '+' if pnl > 0 else ''
            await context.bot.send_message(
                chat_id=uid,
                text=f"{emoji} Сигнал **{sym}** отработан: **{outcome}**\n"
                     f"PnL: {sign}{pnl}%"
            )
        except Exception:
            pass  # пользователь заблокировал бота


async def cmd_horoscope(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Крипто-гороскоп на сегодня."""
    import random
    conn_tmp = init_db(); log_event(conn_tmp, update.effective_user.id, 'horoscope', update); conn_tmp.close()

    # Выбираем 3 случайных знака
    signs = random.sample(HOROSCOPE_SIGNS, 3)
    texts = random.sample(HOROSCOPE_TEXTS, 3)

    lines = ["🔮 **Крипто-гороскоп на сегодня**\n"]
    for sign, text in zip(signs, texts):
        lines.append(f"{sign}\n_{text}_\n")

    lines.append("⚡ _Гороскоп не является финансовой рекомендацией. Наверное._")
    await update.message.reply_text("\n".join(lines))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = user_id == GRIDSIGNAL_ADMIN_ID
    conn = init_db()
    lang = get_lang(conn, user_id)

    # Personal stats for all users
    if not is_admin:
        now_ts = time.time()
        day_ago = now_ts - 86400
        my_sigs = conn.execute(
            'SELECT outcome, pnl_pct FROM signals WHERE user_id=? AND outcome IS NOT NULL',
            (user_id,)
        ).fetchall()
        my_total = len(my_sigs)
        my_wins = sum(1 for r in my_sigs if r[0] in ('TP1', 'TP2'))
        my_wr = f"{my_wins/my_total*100:.0f}%" if my_total > 0 else "—"
        pnl_vals = [r[1] for r in my_sigs if r[1] is not None]
        my_avg = sum(pnl_vals)/len(pnl_vals) if pnl_vals else 0
        my_scans = conn.execute(
            'SELECT COUNT(*) FROM events WHERE user_id=? AND event="scan"',
            (user_id,)
        ).fetchone()[0]
        my_alerts = conn.execute(
            'SELECT COUNT(*) FROM alerts WHERE user_id=? AND active=1',
            (user_id,)
        ).fetchone()[0]
        avg_score = conn.execute(
            'SELECT AVG(score) FROM signals WHERE user_id=?',
            (user_id,)
        ).fetchone()[0]
        conn.close()

        lines = [
            "📊 **Твоя статистика**\n",
            f"🔍 Сканов: {my_scans} · Сигналов: {my_total} · Винрейт: {my_wr}",
        ]
        if pnl_vals:
            lines.append(f"📊 PnL: средний {my_avg:+.1f}% · лучший {max(pnl_vals):+.1f}% · худший {min(pnl_vals):+.1f}%")
        if avg_score:
            lines.append(f"⭐ Средний score сигналов: {avg_score:.1f}/10")
        if my_alerts > 0:
            lines.append(f"🔔 Активных алертов: {my_alerts}")
        lines.append(f"\n⚡ /scan — новый сигнал" if lang == 'ru' else f"\n⚡ /scan — new signal")
        await update.message.reply_text("\n".join(lines))
        return
    now = time.time()
    day_ago, week_ago, month_ago = now - 86400, now - 7*86400, now - 30*86400


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Лидерборд по винрейту (мин. 5 отработанных сигналов)."""
    user_id = update.effective_user.id
    conn_tmp = init_db()
    lang = get_lang(conn_tmp, user_id)
    log_event(conn_tmp, user_id, 'top', update)
    rows = conn_tmp.execute('''SELECT u.username, u.first_name,
        COUNT(*) as total,
        SUM(CASE WHEN s.outcome IN ("TP1","TP2") THEN 1 ELSE 0 END) as wins,
        AVG(s.pnl_pct) as avg_pnl
        FROM signals s JOIN users u ON s.user_id=u.user_id
        WHERE s.outcome IS NOT NULL
        GROUP BY s.user_id HAVING total >= 5
        ORDER BY wins*1.0/total DESC LIMIT 10''').fetchall()
    conn_tmp.close()
    if not rows:
        await update.message.reply_text(
            "🏆 Пока нет данных для рейтинга (нужно ≥5 отработанных сигналов)." if lang == 'ru'
            else "🏆 Not enough data for ranking (need ≥5 resolved signals).")
        return
    medals = ['🥇', '🥈', '🥉'] + [''] * 7
    lines = ["🏆 **Лидерборд** (мин. 5 сигналов)\n" if lang == 'ru'
             else "🏆 **Leaderboard** (min 5 signals)\n"]
    for i, row in enumerate(rows):
        name = row[1] or row[0] or f'user{row[0]}'
        total, wins, avg_pnl = row[2], row[3], row[4] or 0
        wr = wins/total*100 if total > 0 else 0
        lines.append(f"{medals[i]}{i+1}. {name}: {wr:.0f}% ({wins}/{total}) · avg {avg_pnl:+.1f}%")
    await update.message.reply_text("\n".join(lines))

    total_users = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    new_7d = conn.execute('SELECT COUNT(*) FROM users WHERE joined_at > ?',
                          (datetime.fromtimestamp(week_ago).isoformat(),)).fetchone()[0]
    active_7d = conn.execute('SELECT COUNT(DISTINCT user_id) FROM events WHERE ts > ?', (week_ago,)).fetchone()[0]
    active_30d = conn.execute('SELECT COUNT(DISTINCT user_id) FROM events WHERE ts > ?', (month_ago,)).fetchone()[0]
    scans_today = conn.execute('SELECT COUNT(*) FROM events WHERE event="scan" AND ts > ?', (day_ago,)).fetchone()[0]
    scans_week = conn.execute('SELECT COUNT(*) FROM events WHERE event="scan" AND ts > ?', (week_ago,)).fetchone()[0]
    subs = conn.execute('SELECT COUNT(*) FROM users WHERE subscribed=1').fetchone()[0]
    alert_count = conn.execute('SELECT COUNT(*) FROM alerts WHERE active=1').fetchone()[0]

    # Винрейт сигналов (PnL-взвешенный и бинарный)
    total_sigs = conn.execute('SELECT COUNT(*) FROM signals WHERE outcome IS NOT NULL').fetchone()[0]
    # Бинарный: TP1 + TP2
    wins = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome IN ('TP1','TP2')").fetchone()[0]
    # PnL-взвешенный: TP2 = 1.0, TP1 = 0.2 (20% позиции)
    weighted_wins = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE outcome='TP2'").fetchone()[0]
    weighted_tp1 = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE outcome='TP1'").fetchone()[0]
    weighted_total = weighted_wins + weighted_tp1 * 0.2
    binary_wr = f"{wins/total_sigs*100:.0f}%" if total_sigs > 0 else "—"
    weighted_wr = f"{weighted_total/total_sigs*100:.0f}%" if total_sigs > 0 else "—"

    # Медианный PnL
    pnl_rows = conn.execute(
        'SELECT pnl_pct FROM signals WHERE outcome IS NOT NULL AND pnl_pct IS NOT NULL ORDER BY pnl_pct'
    ).fetchall()
    if pnl_rows:
        pnl_vals = [r[0] for r in pnl_rows]
        n = len(pnl_vals)
        median_pnl = pnl_vals[n//2] if n % 2 == 1 else (pnl_vals[n//2-1] + pnl_vals[n//2]) / 2
        avg_pnl = sum(pnl_vals) / n
        median_str = f"{median_pnl:+.1f}%"
        avg_str = f"{avg_pnl:+.1f}%"
    else:
        median_str = avg_str = "—"

    # Среднее время до TP
    tp_rows = conn.execute(
        "SELECT outcome_ts, ts FROM signals WHERE outcome IN ('TP1','TP2') AND outcome_ts IS NOT NULL"
    ).fetchall()
    if tp_rows:
        avg_tp_hours = sum(r[0] - r[1] for r in tp_rows) / len(tp_rows) / 3600
        tp_time_str = f"{avg_tp_hours:.0f}ч"
    else:
        tp_time_str = "—"

    top5 = conn.execute('''SELECT u.username, u.first_name, COUNT(*) as cnt
        FROM events e JOIN users u ON e.user_id=u.user_id
        WHERE e.ts > ? GROUP BY e.user_id ORDER BY cnt DESC LIMIT 5''', (week_ago,)).fetchall()

    # Топ-5 тикеров по винрейту
    ticker_stats = conn.execute('''SELECT symbol, COUNT(*) as total,
        SUM(CASE WHEN outcome IN (\"TP1\",\"TP2\") THEN 1 ELSE 0 END) as wins,
        AVG(pnl_pct) as avg_pnl
        FROM signals WHERE outcome IS NOT NULL
        GROUP BY symbol HAVING total >= 3
        ORDER BY wins*1.0/total DESC LIMIT 5''').fetchall()

    # Лучший и худший сигнал
    best = conn.execute("SELECT symbol, score, pnl_pct, ts FROM signals WHERE outcome IS NOT NULL AND pnl_pct IS NOT NULL ORDER BY pnl_pct DESC LIMIT 1").fetchone()
    worst = conn.execute("SELECT symbol, score, pnl_pct, ts FROM signals WHERE outcome IS NOT NULL AND pnl_pct IS NOT NULL ORDER BY pnl_pct ASC LIMIT 1").fetchone()
    conn.close()

    lines = [
        "📊 **Статистика GridSignal Bot**\n",
        f"👥 Пользователей: **{total_users}** (+{new_7d} за 7д)",
        f"⚡ Активных: 7д **{active_7d}** · 30д {active_30d}",
        f"📬 Подписок: {subs} · 🔔 Алертов: {alert_count}",
    ]
    if total_sigs > 0:
        lines.append("")
        lines.append(f"🎯 Винрейт: **{binary_wr}** ({wins}/{total_sigs})")
        lines.append(f"📐 PnL-взвешенный: **{weighted_wr}** (TP2×1.0 + TP1×0.2)")
        lines.append(f"💵 Медианный PnL: **{median_str}** · Средний: {avg_str}")
        lines.append(f"⏱ Среднее до TP: **{tp_time_str}**")
    else:
        lines.append("🎯 Винрейт: пока нет данных")
    lines.append("")
    lines.append(f"📈 Сканов сегодня: **{scans_today}** · за неделю: {scans_week}")
    if top5:
        lines.append("\n🔥 **Топ-5 за неделю:**")
        for i, (uname, fname, cnt) in enumerate(top5, 1):
            name = fname or uname or f"user_{i}"
            lines.append(f"  {i}. {name} — {cnt} действ.")

    if ticker_stats:
        lines.append("\n🎯 **Топ-5 тикеров по винрейту:**")
        for i, (sym, total, wins, avg_pnl) in enumerate(ticker_stats, 1):
            wr = wins/total*100
            pnl_str = f"{avg_pnl:+.1f}%" if avg_pnl else "—"
            lines.append(f"  {i}. {sym}: {wr:.0f}% ({wins}/{total}) · PnL {pnl_str}")

    if best and worst:
        b_dt = datetime.fromtimestamp(best[3]).strftime('%d.%m')
        w_dt = datetime.fromtimestamp(worst[3]).strftime('%d.%m')
        lines.append(f"\n🏆 Лучший: **{best[0]}** {best[2]:+.1f}% ({b_dt}, score {best[1]:.1f})")
        lines.append(f"💀 Худший: **{worst[0]}** {worst[2]:+.1f}% ({w_dt}, score {worst[1]:.1f})")

    await update.message.reply_text("\n".join(lines))


# ══ /chart — график свечей + BB ══

async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует график свечей + Bollinger Bands."""
    user_id = update.effective_user.id
    args = context.args

    conn = init_db()
    lang = get_lang(conn, user_id)
    conn.close()

    if not args:
        await update.message.reply_text(t('chart_usage', lang))
        return

    symbol = args[0].upper().strip()
    # Validate: only alphanumeric, 4-12 chars (USDT suffix optional)
    symbol_clean = symbol.replace('USDT', '') if symbol.endswith('USDT') else symbol
    if not re.match(r'^[A-Z0-9]{2,12}$', symbol_clean):
        await update.message.reply_text(f'❌ Invalid symbol: {symbol}')
        return
    if not symbol.endswith('USDT'):
        symbol += 'USDT'

    tf = args[1] if len(args) > 1 else 'D'
    tf_map = {'D': 'D', 'W': 'W', 'M': 'M', '5': '5', '15': '15', '60': '60', '3': '3'}
    tf = tf_map.get(tf, 'D')

    tf_label = {'D': 'Daily', 'W': 'Weekly', 'M': 'Monthly', '5': 'M5', '15': 'M15', '60': 'H1', '3': 'M3'}.get(tf, tf)
    status_msg = await update.message.reply_text(f"{t('chart_building', lang)} {symbol} ({tf_label})...")

    try:
        import matplotlib
        matplotlib.use('Agg')
        import mplfinance as mpf
        import pandas as pd
        import tempfile

        r = subprocess.run(
            [BYBIT_CLI, 'raw', 'GET', f'/v5/market/kline?category=linear&symbol={symbol}&interval={tf}&limit=100'],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(r.stdout)
        candles = data['result']['list']

        if not candles:
            await status_msg.edit_text(t('chart_no_data', lang, symbol, tf))
            return

        df = pd.DataFrame(candles, columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])
        df['ts'] = pd.to_datetime(pd.to_numeric(df['ts']), unit='ms')
        df = df.sort_values('ts').set_index('ts')

        df['bb_mid'] = df['close'].rolling(20).mean()
        df['bb_std'] = df['close'].rolling(20).std()
        df['bb_upper'] = df['bb_mid'] + 2 * df['bb_std']
        df['bb_lower'] = df['bb_mid'] - 2 * df['bb_std']

        last = df.iloc[-1]
        bb_pos = round((last['close'] - last['bb_lower']) / (last['bb_upper'] - last['bb_lower']) * 100, 1) if last['bb_upper'] != last['bb_lower'] else 50

        ap = [
            mpf.make_addplot(df['bb_upper'], color='#ff6b6b', width=0.8),
            mpf.make_addplot(df['bb_mid'], color='#ffd93d', width=0.6, linestyle='--'),
            mpf.make_addplot(df['bb_lower'], color='#6bff6b', width=0.8),
        ]

        style = mpf.make_mpf_style(base_mpf_style='nightclouds', rc={'font.size': 8})

        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
            tmp_path = f.name

        mpf.plot(
            df, type='candle', style=style,
            addplot=ap,
            title=f'{symbol} · {tf_label} · BB(20,2) · BB%={bb_pos}%',
            volume=False,
            figsize=(10, 6),
            savefig=tmp_path
        )

        import matplotlib.pyplot as plt
        plt.close('all')

        if lang == 'en':
            caption = (
                f"📈 **{symbol}** · {tf_label}\n"
                f"💰 Price: `${last['close']:.4f}`\n"
                f"📊 BB: {bb_pos}%\n"
                f"🟢 Lower: `${last['bb_lower']:.4f}`\n"
                f"🟡 Middle: `${last['bb_mid']:.4f}`\n"
                f"🔴 Upper: `${last['bb_upper']:.4f}`\n"
                f"💡 Entry: `${last['bb_lower']*0.97:.4f}` (−3% Lower)"
            )
        else:
            caption = (
                f"📈 **{symbol}** · {tf_label}\n"
                f"💰 Цена: `${last['close']:.4f}`\n"
                f"📊 BB: {bb_pos}%\n"
                f"🟢 Lower: `${last['bb_lower']:.4f}`\n"
                f"🟡 Middle: `${last['bb_mid']:.4f}`\n"
                f"🔴 Upper: `${last['bb_upper']:.4f}`\n"
                f"💡 Вход: `${last['bb_lower']*0.97:.4f}` (−3% Lower)"
            )

        try:
            with open(tmp_path, 'rb') as img:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=img,
                    caption=caption
                )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        await status_msg.delete()

        conn2 = init_db()
        log_event(conn2, user_id, 'chart', update)
        conn2.close()

    except Exception as e:
        err_msg = str(e)
        # Sanitize: убираем системные пути из сообщений об ошибках
        err_msg = re.sub(r'/home/\S+', '[path]', err_msg)
        err_msg = re.sub(r'/tmp/\S+', '[tmp]', err_msg)
        await status_msg.edit_text(t('chart_error', lang, err_msg[:200]))


# ══ Обработчик инлайн-кнопок (язык) ══

async def lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    lang = query.data.split(':')[1]  # 'lang:ru' → 'ru'

    conn = init_db()
    get_user(conn, user_id)
    conn.execute('UPDATE users SET lang=? WHERE user_id=?', (lang, user_id))
    conn.commit()
    conn.close()

    await query.edit_message_text(t('lang_set', lang))


async def delalert_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    symbol = query.data.split(':', 1)[1]  # 'delalert:BTCUSDT' → 'BTCUSDT'

    conn = init_db()
    lang = get_lang(conn, user_id)
    conn.execute('UPDATE alerts SET active=0 WHERE user_id=? AND symbol=?', (user_id, symbol))
    conn.commit()
    conn.close()

    await query.edit_message_text(t('alert_disabled', lang, symbol))


# ══ /lang — смена языка ══

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    conn = init_db()
    get_user(conn, user_id)

    if not args:
        cur = get_lang(conn, user_id)
        label = '🇷🇺 Русский' if cur == 'ru' else '🇬🇧 English'
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton('🇷🇺 Русский', callback_data='lang:ru'),
             InlineKeyboardButton('🇬🇧 English', callback_data='lang:en')]
        ])
        await update.message.reply_text(f'🌐 {label}\nВыбери язык:', reply_markup=keyboard)
        conn.close()
        return

    lang_arg = args[0].lower()
    if lang_arg in ('ru', 'рус', 'русский'):
        lang = 'ru'
    elif lang_arg in ('en', 'eng', 'english'):
        lang = 'en'
    else:
        await update.message.reply_text("🌐 `/lang ru` или `/lang en`")
        conn.close()
        return

    conn.execute('UPDATE users SET lang=? WHERE user_id=?', (lang, user_id))
    conn.commit()
    conn.close()

    await update.message.reply_text(t('lang_set', lang))


async def handle_dex_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перехватывает ввод токена для DEX-поиска"""
    text = update.message.text.strip()
    # Кнопки бота — не поисковый запрос
    if text.startswith("📊") or text.startswith("🔴") or text.startswith("📈") or \
       text.startswith("⚡") or text.startswith("🔄") or text.startswith("💰") or \
       text.startswith("🟢") or text.startswith("📋") or text.startswith("🔔") or \
       text.startswith("🌐") or text.startswith("📐") or text.startswith("😱") or \
       text.startswith("🔮") or text.startswith("💬") or text.startswith("❓") or \
       text.startswith("🔍"):
        context.user_data['awaiting_dex'] = False
        return await handle_buttons(update, context)

    if context.user_data.get('awaiting_dex'):
        context.user_data['awaiting_dex'] = False
        if text:
            await _do_dex_search(update, text)
            return
    # Не DEX-режим — пропускаем в handle_buttons
    await handle_buttons(update, context)


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    # DEX-режим: пользователь вводит тикер после нажатия 🔍 DEX
    if context.user_data.get('awaiting_dex') and not any(
        text.startswith(e) for e in "📊🔴📈⚡🔄💰🟢📋🔔🌐📐😱🔮💬❓🔍"
    ):
        context.user_data['awaiting_dex'] = False
        await _do_dex_search(update, text.strip())
        return

    if text == "📊 Скан":
        await cmd_scan(update, context)
    elif text == "🔴 Шорт":
        context.args = ['short']
        await cmd_scan(update, context)
    elif text == "📈 График":
        await update.message.reply_text("📈 `/chart BTCUSDT` — график с Bollinger Bands.\nПримеры: `/chart ETHUSDT 5`, `/chart SOLUSDT W`")
    elif text == "🌐 Язык":
        await cmd_lang(update, context)
    elif text == "📐 Правила":
        await cmd_rules(update, context)
    elif text == "❓ Помощь":
        await cmd_help(update, context)
    elif text == "📋 История":
        await cmd_history(update, context)
    elif text == "🔔 Алерты":
        await cmd_alert(update, context)
    elif text == "💬 Связь":
        await cmd_contact(update, context)
    elif text == "😱 Страх":
        await cmd_fear(update, context)
    elif text == "🔮 Гороскоп":
        await cmd_horoscope(update, context)
    elif text == "🟢 Покупка":
        # Запускаем скан с green-only фильтром
        context.args = ['green']
        await cmd_scan(update, context)
    elif text == "⚡ Скальп x10":
        context.args = ['scalp']
        await cmd_scan(update, context)
    elif text == "🔄 Mean Revert":
        context.args = ['mean_revert']
        await cmd_scan(update, context)
    elif text == "💰 Фандинг":
        context.args = ['funding']
        await cmd_scan(update, context)
    elif text == "🔄 Ротация":
        context.args = ['rotation']
        await cmd_scan(update, context)
    elif text == "📊 LONG":
        context.args = ['long']
        await cmd_scan(update, context)
    elif text == "📉 SHORT":
        context.args = ['short']
        await cmd_scan(update, context)
    elif text == "💳 Оплата":
        await cmd_subscribe(update, context)
    elif text == "🔍 DEX":
        await cmd_dex_start(update, context)


# ═══════════════════════════════════════════════════════════════
# DEX Search via DexScreener
# ═══════════════════════════════════════════════════════════════

async def cmd_dex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dex SYMBOL — поиск токена через DexScreener"""
    if not context.args:
        await update.message.reply_text(
            "🔍 **DEX Search**\n"
            "Отправь тикер или название токена.\n"
            "Пример: `/dex near` или `/dex bitcoin`"
        )
        return
    query = ' '.join(context.args)
    await _do_dex_search(update, query)


async def cmd_dex_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало DEX-поиска: просим ввести тикер"""
    await update.message.reply_text(
        "🔍 **DEX Search**\n"
        "Отправь тикер или название токена для поиска через DexScreener.\n"
        "Например: `NEAR`, `SOL`, `COAI`, `bitcoin`"
    )
    # Запоминаем что юзер в режиме DEX-поиска
    context.user_data['awaiting_dex'] = True


async def _do_dex_search(update: Update, query: str):
    """Выполнить поиск через DexScreener API и показать результат"""
    import urllib.request, urllib.error
    msg = await update.message.reply_text(f"🔍 Ищу `{query}`...")

    try:
        url = f"https://api.dexscreener.com/latest/dex/search?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'GridSignalBot/4.1'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        pairs = data.get('pairs', [])
        if not pairs:
            await msg.edit_text(f"❌ Ничего не найдено по запросу `{query}`")
            return

        # L1-монеты: DexScreener не индексирует нативные цепочки, только фейки
        L1_COINS = {'BTC', 'ETH', 'LTC', 'XRP', 'ADA', 'DOT', 'AVAX', 'MATIC', 'POL', 'ATOM', 'FIL', 'LINK'}
        if query.upper() in L1_COINS:
            await msg.edit_text(
                f"⚠️ **{query.upper()}** — L1-монета, DexScreener показывает только фейковые wrapped-токены.\n\n"
                f"Попробуй `/scan {query.lower()}` — данные с Bybit (цена, BB, сигналы).\n"
                f"Или укажи токен на DEX-бирже: `COAI`, `NEAR`, `WIF`, `BONK`..."
            )
            return

        # Фильтруем фейковые пулы: объём > $100 и ≥ 50 сделок за 24h
        real_pairs = [
            p for p in pairs
            if p.get("volume", {}).get("h24", 0) >= 100
            and (p.get("txns", {}).get("h24", {}).get("buys", 0) +
                 p.get("txns", {}).get("h24", {}).get("sells", 0)) >= 50
        ]
        if not real_pairs:
            real_pairs = pairs  # fallback: если все пустые — показываем хоть что-то

        # Группируем по baseToken.address — одна пара на токен (самая ликвидная)
        seen_tokens = {}
        for p in sorted(real_pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True):
            addr = p.get('baseToken', {}).get('address', '')
            if addr not in seen_tokens:
                seen_tokens[addr] = p

        # Сортируем уникальные пары по объёму × ликвидность (настоящая активность), берём топ-1
        def real_score(p):
            liq = p.get('liquidity', {}).get('usd', 0)
            vol = p.get('volume', {}).get('h24', 0)
            return vol * (liq ** 0.5)  # объём важнее ликвидности

        unique = sorted(seen_tokens.values(), key=real_score, reverse=True)
        if not unique:
            await msg.edit_text(f"❌ Ничего не найдено по запросу `{query}`")
            return
        top = unique[0]  # только одна самая ликвидная пара

        chain = top['chainId'].upper()
        dex = top.get('dexId', '?').capitalize()
        price = top.get('priceUsd', '0')
        price_str = f"${float(price):.6f}".rstrip('0').rstrip('.')
        liq = top.get('liquidity', {}).get('usd', 0)
        vol = top.get('volume', {}).get('h24', 0)
        chg = top.get('priceChange', {}).get('h24', 0)
        fdv = top.get('fdv', 0)
        mcap = top.get('marketCap', 0)
        txns = top.get('txns', {})
        buys_24 = txns.get('h24', {}).get('buys', 0)
        sells_24 = txns.get('h24', {}).get('sells', 0)
        pair_age_days = (time.time() * 1000 - top.get('pairCreatedAt', 0)) / 86400000 if top.get('pairCreatedAt') else None

        chg_sign = "+" if chg > 0 else ""
        buy_ratio = buys_24 / (buys_24 + sells_24) * 100 if (buys_24 + sells_24) > 0 else 0
        total_txns = buys_24 + sells_24

        # Сокращённый формат: цепочка, DEX, цена, объём, сделки
        lines = [f"🔍 **{query.upper()}** — DexScreener\n"]
        vol_str = f"${vol:,.0f}" if vol >= 1000 else (f"${vol:.0f}" if vol > 0 else "—")
        age_str = f" · {pair_age_days:.0f}д" if pair_age_days and pair_age_days < 365 else ""

        lines.append(
            f"**{chain} · {dex}**{age_str}\n"
            f"💵 `${price_str}` · 24h: {chg_sign}{chg:.1f}%\n"
            f"💧 Ликв: `${liq:,.0f}` · Объём: {vol_str}\n"
            f"📊 MCap: `${mcap:,.0f}` · FDV: `${fdv:,.0f}`\n"
            f"📈 Сделок: {total_txns:,} (B:{buy_ratio:.0f}% S:{100-buy_ratio:.0f}%)\n"
        )

        await msg.edit_text('\n'.join(lines), disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text(f"⚠️ Ошибка поиска: {e}")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    token = os.environ.get('GRIDSIGNAL_BOT_TOKEN')
    if not token:
        print("❌ GRIDSIGNAL_BOT_TOKEN не задан.")
        sys.exit(1)

    conn = init_db()
    reset_daily_counts(conn)
    conn.close()

    app = Application.builder().token(token).build()

    # Команды
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('help', cmd_help))
    app.add_handler(CommandHandler('rules', cmd_rules))
    app.add_handler(CommandHandler('scan', cmd_scan))
    app.add_handler(CommandHandler('stats', cmd_stats))
    app.add_handler(CommandHandler('top', cmd_top))
    app.add_handler(CommandHandler('leaderboard', cmd_leaderboard))
    app.add_handler(CommandHandler('setrisk', cmd_setrisk))
    app.add_handler(CommandHandler('history', cmd_history))
    app.add_handler(CommandHandler('subscribe', cmd_subscribe))
    app.add_handler(CommandHandler('pro', cmd_subscribe))
    app.add_handler(CommandHandler('status', cmd_status))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r'^(pro_buy|pro_buy_ton|check_ton_\d+|scan|status)$'))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    app.add_handler(CommandHandler('alert', cmd_alert))
    app.add_handler(CommandHandler('delalert', cmd_delalert))
    app.add_handler(CommandHandler('contact', cmd_contact))
    app.add_handler(CommandHandler('fear', cmd_fear))
    app.add_handler(CommandHandler('horoscope', cmd_horoscope))
    app.add_handler(CommandHandler('chart', cmd_chart))
    app.add_handler(CommandHandler('lang', cmd_lang))
    app.add_handler(CommandHandler('dex', cmd_dex))
    app.add_handler(CallbackQueryHandler(lang_callback, pattern='^lang:'))
    app.add_handler(CallbackQueryHandler(delalert_callback, pattern='^delalert:'))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

    # Ежедневная сводка: 10:00 МСК = 07:00 UTC
    app.job_queue.run_daily(send_daily_digest, time=datetime.now().replace(hour=7, minute=0, second=0).time())

    # Сброс дневных счётчиков: полночь UTC
    app.job_queue.run_daily(lambda ctx: _reset_counts_job(), time=datetime.now().replace(hour=0, minute=0, second=5).time())

    # Страховочный сброс каждый час (для перезапусков/пропусков)
    app.job_queue.run_repeating(lambda ctx: _reset_counts_job(), interval=3600, first=30)

    # Депровижнинг Pro: каждые 30 мин
    app.job_queue.run_repeating(deprovision_expired, interval=1800, first=60)

    # Проверка алертов: каждые 2 минуты
    app.job_queue.run_repeating(check_alerts, interval=120, first=10)

    # Проверка исходов сигналов: раз в час
    app.job_queue.run_repeating(check_outcomes, interval=3600, first=60)

    print(f"🤖 GridSignal Bot v{BOT_VERSION} запущен...")
    app.run_polling()


if __name__ == '__main__':
    main()
