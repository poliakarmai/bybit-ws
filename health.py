"""Проверки здоровья: ликвидация, squeeze, funding, просадка, корреляция."""
import os, time
from . import BYBIT_CLI, DATA_DIR, DAILY_DRAWDOWN_LIMIT, DAILY_START_EQUITY
from .alerts import log_event  # для except-логирования
from . import safe_run
from .api import get_bb_data
from .snapshot import load_json, save_json

LAST_LIQ_ALERT = {}
BB_SQUEEZE_WATCH = [
    'BTCUSDT','ETHUSDT','SOLUSDT','LTCUSDT','XRPUSDT','ADAUSDT','DOGEUSDT',
    'HYPEUSDT','NEARUSDT','SUIUSDT','TONUSDT','WLDUSDT','LINKUSDT',
    'AAVEUSDT','AVAXUSDT','DOTUSDT','INJUSDT','ONDOUSDT','ARBUSDT',
]

def check_liquidation(positions):
    alerts = []
    for sym, p in positions.items():
        liq = p.get('liqPrice')
        if not liq or liq <= 0:
            continue
        mark = p['mark']
        side = p['side']
        dist_pct = (mark - liq) / mark * 100 if side == 'Buy' else (liq - mark) / mark * 100
        if dist_pct <= 20:
            prev = LAST_LIQ_ALERT.get(sym, 999)
            if dist_pct < prev - 2 or (dist_pct <= 10 and prev > dist_pct + 1):
                alerts.append(f'⚠️ {sym}: до ликвидации {dist_pct:.1f}%! Mark=${mark:.4f}, Liq=${liq:.4f}')
                LAST_LIQ_ALERT[sym] = dist_pct
    return alerts

def check_bb_squeeze():
    alerts = []
    for sym in BB_SQUEEZE_WATCH:
        bb = get_bb_data(sym, 'D')
        if not bb:
            continue
        cur = bb['cur']
        if cur <= 0:
            continue
        width = (bb['upper'] - bb['lower']) / cur * 100
        if width < 2:
            alerts.append(f'📈 {sym}: BB squeeze! Ширина {width:.1f}%, цена ${cur:.4f} — жди сильного движения')
    return alerts

FUNDING_SNAPSHOT = os.path.join(DATA_DIR, 'funding.json')
FUNDING_FLIP_THRESHOLD = 0.02

def check_funding_flip():
    alerts = []
    old_funding = load_json(FUNDING_SNAPSHOT)
    new_funding = {}
    all_watch = list(BB_SQUEEZE_WATCH)
    wl_file = os.path.join(DATA_DIR, 'watchlist_custom.txt')
    if os.path.exists(wl_file):
        with open(wl_file) as f:
            custom = [l.strip() for l in f if l.strip() and l.strip().endswith('USDT')]
            all_watch.extend([s for s in custom if s not in all_watch])
    for sym in all_watch:
        try:
            r = safe_run([BYBIT_CLI, 'ticker', sym], timeout=5)
            for line in r.stdout.split('\n'):
                if 'Funding' in line:
                    val_str = line.split(':')[-1].strip().replace('%', '')
                    try:
                        new_funding[sym] = float(val_str)
                    except Exception as e:
                        log_event(f'⚠️ health: {e}')
                    break
        except Exception:
            continue
    for sym, new_val in new_funding.items():
        old_val = old_funding.get(sym)
        if old_val is not None:
            if old_val < -FUNDING_FLIP_THRESHOLD and new_val > FUNDING_FLIP_THRESHOLD:
                alerts.append(f'💸 {sym}: funding флип {old_val:+.4f}% → {new_val:+.4f}% (long→short давление!)')
            elif old_val > FUNDING_FLIP_THRESHOLD and new_val < -FUNDING_FLIP_THRESHOLD:
                alerts.append(f'💸 {sym}: funding флип {old_val:+.4f}% → {new_val:+.4f}% (short→long давление!)')
    save_json(FUNDING_SNAPSHOT, new_funding)
    return alerts

def check_daily_drawdown(new_positions):
    global DAILY_START_EQUITY
    if not new_positions:
        return None
    
    # Кулдаун 24 часа на алерт просадки
    dd_file = os.path.join(DATA_DIR, 'drawdown_alert.json')
    try:
        dd_state = load_json(dd_file) or {}
        last_alert = dd_state.get('last_alert', 0)
        elapsed = time.time() - last_alert
        if elapsed < 86400:
            return None  # кулдаун не истёк
    except Exception as e:
        log_event(f'⚠️ drawdown load: {e}')
        dd_state = {}
    
    try:
        r = safe_run([BYBIT_CLI, 'balance'], timeout=10)
        equity = None
        for line in r.stdout.split('\n'):
            if 'Equity:' in line:
                equity = float(line.split(':')[-1].strip().replace('$','').replace(',',''))
                break
        if equity is None:
            return None
    except Exception:
        return None
    if DAILY_START_EQUITY is None:
        DAILY_START_EQUITY = equity
        return None
    drawdown = (DAILY_START_EQUITY - equity) / DAILY_START_EQUITY
    if drawdown > DAILY_DRAWDOWN_LIMIT:
        dd_state['last_alert'] = time.time()
        dd_state['drawdown'] = round(drawdown * 100, 1)
        dd_state['equity'] = equity
        try:
            save_json(dd_file, dd_state)
        except Exception as e:
            log_event(f'⚠️ drawdown save_json: {e}')
        return f'📉 Дневная просадка {drawdown*100:.1f}%! Equity ${equity:.0f} (было ${DAILY_START_EQUITY:.0f})'
    return None

def check_funding_pump():
    """Проверить монеты с выбросом фондинга (>0.1%) + перегревом BB (>75%) = SHORT сигнал."""
    alerts = []
    all_watch = list(BB_SQUEEZE_WATCH)
    wl_file = os.path.join(DATA_DIR, 'watchlist_custom.txt')
    if os.path.exists(wl_file):
        with open(wl_file) as f:
            custom = [l.strip() for l in f if l.strip() and l.strip().endswith('USDT')]
            all_watch.extend([s for s in custom if s not in all_watch])

    now = time.time()
    state = load_json(os.path.join(DATA_DIR, 'funding_pump.json'))
    if not isinstance(state, dict):
        state = {}

    for sym in all_watch:
        try:
            r = safe_run([BYBIT_CLI, 'ticker', sym], timeout=5)
            funding = None
            for line in r.stdout.split('\n'):
                if 'Funding' in line:
                    val_str = line.split(':')[-1].strip().replace('%', '')
                    try:
                        funding = float(val_str)
                    except Exception as e:
                        log_event(f'⚠️ health: {e}')
                    break
            if funding is None or funding <= 0.1:
                continue

            # Проверяем BB
            bb = get_bb_data(sym, 'D')
            if not bb or bb['bb_pos'] <= 75:
                continue

            # Дедупликация: не чаще раза в 2 часа
            st = state.get(sym, {})
            last_alert = st.get('last_alert', 0)
            if now - last_alert < 7200:
                continue

            bb_pos = bb['bb_pos']
            state[sym] = {'last_alert': now, 'funding': funding, 'bb_pos': bb_pos}
            alerts.append(
                f'💸 ФОНДИНГ-ШОРТ {sym}: funding +{funding:.3f}%, '
                f'BB {bb_pos:.0f}% (перегрев) — толпа платит за LONG! '
                f'Цена ${bb["cur"]:.4f}, Upper ${bb["upper"]:.4f}'
            )

        except Exception:
            continue

    save_json(os.path.join(DATA_DIR, 'funding_pump.json'), state)
    return alerts


def check_correlation_risk(positions):
    if not positions:
        return None
    total = len(positions)
    longs = sum(1 for p in positions.values() if p['side'] == 'Buy')
    long_pct = longs / total * 100
    if long_pct > 80 and total >= 5:
        return f'⚠️ Корреляция: {longs}/{total} ({long_pct:.0f}%) позиций в LONG. При падении убыток x{total}'
    return None
