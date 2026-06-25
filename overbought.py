"""Мониторинг перегретых монет + watchlist-ротация."""
import json, os, time
from . import safe_run
from . import BYBIT_CLI, DATA_DIR, SHORT_ALERT_LAST, SHORT_ALERT_COOLDOWN, WATCHLIST_UPDATED_FILE, _save_short_alerts
from .api import get_bb_data

# Статический watchlist (fallback)
STATIC_WATCHLIST = [
    'BTCUSDT','ETHUSDT','SOLUSDT','HYPEUSDT','NEARUSDT','ZECUSDT',
    'BSBUSDT','ONDOUSDT','SUIUSDT','TONUSDT','WLDUSDT','LINKUSDT',
    'AAVEUSDT','AVAXUSDT','DOTUSDT','BNBUSDT','PEPEUSDT','INJUSDT',
    'ENAUSDT','FARTCOINUSDT','DOGEUSDT','ADAUSDT','LTCUSDT','XRPUSDT',
    'ARBUSDT','CHZUSDT','BCHUSDT',
]

WATCHLIST_FILE = os.path.join(DATA_DIR, 'watchlist_short.json')

def _load_watchlist():
    """Загрузить watchlist (приоритет: файл > статический)."""
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE) as f:
                data = json.load(f)
                if data.get('symbols') and data.get('updated'):
                    age_h = (time.time() - data['updated']) / 3600
                    if age_h < 24:
                        return data['symbols']
        except Exception as e:
            log_event(f'⚠️ overbought: {e}')
    return list(STATIC_WATCHLIST)

def _save_watchlist(symbols):
    with open(WATCHLIST_FILE, 'w') as f:
        json.dump({'symbols': symbols, 'updated': time.time()}, f)

def rotate_watchlist():
    """Обновить watchlist из топ-30 по объёму (раз в 24ч)."""
    try:
        now = time.time()
        if os.path.exists(WATCHLIST_UPDATED_FILE):
            with open(WATCHLIST_UPDATED_FILE) as f:
                last = float(f.read().strip())
            if now - last < 86400:
                return  # уже обновляли сегодня

        r = safe_run([BYBIT_CLI, 'tickers'], timeout=15)
        if r.returncode != 0:
            return
        # Парсим топ-30 по объёму
        symbols = list(STATIC_WATCHLIST)  # базовый набор всегда
        for line in r.stdout.split('\n')[:80]:
            parts = line.split()
            for p in parts:
                if p.endswith('USDT') and len(p) > 6:
                    if p not in symbols:
                        symbols.append(p)
                        if len(symbols) >= 40:
                            break
            if len(symbols) >= 40:
                break
        _save_watchlist(symbols)
        with open(WATCHLIST_UPDATED_FILE, 'w') as f:
            f.write(str(now))
        from .alerts import log_event
        log_event(f'🔄 Watchlist обновлён: {len(symbols)} монет')
    except Exception as e:
        log_event(f'⚠️ overbought: {e}')

def check_overbought(positions=None):
    """Проверить перегретые монеты (BB > 75%).
    positions: dict символ→данные из памяти (fetch_positions), чтобы не дёргать subprocess.
    """
    alerts = []
    watchlist = _load_watchlist()
    for sym in watchlist:
        bb = get_bb_data(sym, 'D')
        if not bb:
            continue
        bb_pos = bb['bb_pos']
        if bb_pos > 75:
            # Проверяем, не в позиции ли (из памяти, без subprocess)
            if positions and sym in positions:
                continue
            # Дедупликация SHORT-алертов
            now = time.time()
            last = SHORT_ALERT_LAST.get(sym, 0)
            if now - last < SHORT_ALERT_COOLDOWN:
                continue
            SHORT_ALERT_LAST[sym] = now
            _save_short_alerts()
            alerts.append(f"🔥 {sym} на {bb_pos:.0f}% BB (${bb['cur']:.4f}, Upper ${bb['upper']:.4f}) — перегрев, кандидат на SHORT")
    return alerts
