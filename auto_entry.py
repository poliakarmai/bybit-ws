"""Авто-вход по scoring (BB Daily < 25%)."""
import json, math, os, time
from . import BYBIT_CLI, DATA_DIR, MAX_POSITION_VALUE
from . import safe_run
from .api import bybit, get_bb_data
from .alerts import log_event
from .config import Config

AUTO_ENTRY_WATCH = [
    'BTCUSDT','ETHUSDT','SOLUSDT','LTCUSDT','XRPUSDT','ADAUSDT','DOGEUSDT',
    'HYPEUSDT','NEARUSDT','SUIUSDT','TONUSDT','WLDUSDT','LINKUSDT',
    'AAVEUSDT','AVAXUSDT','DOTUSDT','INJUSDT','ONDOUSDT','ARBUSDT',
    'ENAUSDT','FETUSDT','APTUSDT','ATOMUSDT','RUNUSDT',
]

COOLDOWN_FILE = os.path.join(DATA_DIR, 'cooldown.json')


def _load_cooldown():
    """Загрузить cooldown-трекер (sym → timestamp последнего SL)."""
    if os.path.exists(COOLDOWN_FILE):
        try:
            with open(COOLDOWN_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def record_sl_hit(sym: str):
    """Записать что символ получил SL — для cooldown перед повторным входом."""
    cd = _load_cooldown()
    cd[sym] = time.time()
    try:
        with open(COOLDOWN_FILE, 'w') as f:
            json.dump(cd, f)
    except Exception:
        pass

def quick_score_bb(bb_pos):
    if bb_pos <= 10: return 15
    if bb_pos <= 25: return 12
    if bb_pos <= 40: return 8
    if bb_pos <= 60: return 5
    if bb_pos <= 75: return 3
    if bb_pos <= 100: return 1
    return 0

def auto_entry_scan(positions):
    """Быстрый скрининг и авто-вход при BB < 25%."""
    entries = []
    active = set(positions.keys())
    all_watch = list(AUTO_ENTRY_WATCH)
    wl_file = os.path.join(DATA_DIR, 'watchlist_custom.txt')
    if os.path.exists(wl_file):
        with open(wl_file) as f:
            custom = [l.strip() for l in f if l.strip() and l.strip().endswith('USDT')]
            all_watch.extend([s for s in custom if s not in all_watch])

    try:
        r = safe_run([BYBIT_CLI, 'tickers'], timeout=15)
        if r.returncode != 0:
            return entries
    except:
        return entries

    candidates = []
    for sym in all_watch:
        if sym in active:
            continue
        for line in r.stdout.split('\n'):
            if sym in line:
                candidates.append(sym)
                break

    scored_candidates = []
    for sym in candidates:
        bb = get_bb_data(sym, 'D')
        if not bb:
            continue
        bb_pos = bb['bb_pos']
        scored_candidates.append((bb_pos, sym, bb['lower'], bb['upper'], bb['cur']))

    scored_candidates.sort()
    for bb_pos, sym, lower, upper, cur in scored_candidates:
        try:
            # Cooldown после SL: не входить повторно N часов
            cooldown = _load_cooldown()
            cfg = Config()
            cooldown_sl = cfg.strategy.long.get('cooldown_after_sl', 14400)
            if sym in cooldown:
                elapsed = time.time() - cooldown[sym]
                if elapsed < cooldown_sl:
                    continue  # ещё не прошло достаточно времени

            if bb_pos < 25 and bb_pos > 0:
                price = round(lower, 4)
                qty = math.ceil(10 / price * 3)
                if qty < 1:
                    continue
                if sym in positions:
                    existing_val = positions[sym]['mark'] * positions[sym]['size']
                    if existing_val + price * qty > MAX_POSITION_VALUE:
                        continue
                pos_data = bybit('GET', f'/v5/position/list?category=linear&symbol={sym}')
                idx = 0
                if pos_data and pos_data.get('retCode') == 0:
                    for p in pos_data['result'].get('list', []):
                        if float(p.get('size', 0)) > 0:
                            idx = int(p.get('positionIdx', 0))
                            break
                body = {'category': 'linear', 'symbol': sym, 'side': 'Buy',
                        'orderType': 'Limit', 'qty': str(qty), 'price': str(price),
                        'positionIdx': idx, 'timeInForce': 'GTC'}
                result = bybit('POST', '/v5/order/create', body)
                if result and result.get('retCode') == 0:
                    entries.append(f'🤖 Авто-вход {sym} @ ${price:.4f} x{qty} (BB={bb_pos:.0f}%)')
                    log_event(f'Авто-вход {sym} @ ${price:.4f} BB={bb_pos:.0f}%')
        except:
            continue
    return entries
