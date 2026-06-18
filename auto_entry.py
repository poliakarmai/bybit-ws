"""Авто-вход по 9-метричному скорингу (v4.0)."""
import json, math, os, time
from . import BYBIT_CLI, DATA_DIR, MAX_POSITION_VALUE
from . import safe_run
from .api import bybit, get_bb_data
from .alerts import log_event, add_alert
from .config import Config
from .position_sizing import margin_for_strategy

AUTO_ENTRY_WATCH = [
    'BTCUSDT','ETHUSDT','SOLUSDT','LTCUSDT','XRPUSDT','ADAUSDT','DOGEUSDT',
    'HYPEUSDT','NEARUSDT','SUIUSDT','TONUSDT','WLDUSDT','LINKUSDT',
    'AAVEUSDT','AVAXUSDT','DOTUSDT','INJUSDT','ONDOUSDT','ARBUSDT',
    'ENAUSDT','FETUSDT','APTUSDT','ATOMUSDT','RUNUSDT',
]

COOLDOWN_FILE = os.path.join(DATA_DIR, 'cooldown.json')
MIN_SCORE = 25  # порог для авто-входа (из 50)


def _filter_by_mtf_confluence(scored: list, direction: str) -> list:
    """Фаза 4.3.1: фильтровать сигналы без D/W/M конфлюенса (≥2/3)."""
    from .mtf_confirmation import check_confluence

    filtered = []
    for s in scored:
        sym = s['symbol']
        conf = check_confluence(sym, direction)
        if conf is None:
            filtered.append(s)
            continue
        if conf['approved']:
            s['mtf'] = conf
            filtered.append(s)

            # ── Фаза 4.3.5: paper-трекинг конфлюенса ──
            try:
                from .confluence_paper import track_signal
                cur = s.get('cur', 0)
                track_signal(sym, direction, cur, cur,
                           conf['confluence'], s.get('score'))
            except Exception:
                pass

            # ── Фаза 4.3.4: алерт при конфлюенсе 3/3 (ДО входа) ──
            if conf['confluence'] == 3:
                from .alerts import add_alert as _add_alert
                _add_alert('ENTRY',
                    f'🔥 STRONG CONFLUENCE: {sym} {direction} D+W+M | '
                    f'score={s["score"]}/{s["max_score"]} BB={s["bb_pos"]:.0f}% — '
                    f'ручной вход или увеличенная позиция!'
                )
        else:
            from .alerts import log_event
            log_event(
                f'🚫 MTF filter: {sym} {direction} score={s["score"]} '
                f'confluence={conf["confluence"]}/3 ({conf["filter_reason"]})'
            )
    return filtered


def _load_cooldown():
    if os.path.exists(COOLDOWN_FILE):
        try:
            with open(COOLDOWN_FILE) as f:
                return json.load(f)
        except Exception as e:
            log_event(f'⚠️ auto_entry: {e}')
    return {}


def record_sl_hit(sym: str):
    cd = _load_cooldown()
    cd[sym] = time.time()
    try:
        with open(COOLDOWN_FILE, 'w') as f:
            json.dump(cd, f)
    except Exception as e:
        log_event(f'⚠️ auto_entry: {e}')


def _parse_ticker_line(line: str) -> dict:
    """Парсит строку тикера из bybit-cli tickers в словарь."""
    parts = line.strip().split()
    if len(parts) < 7:
        return {}
    try:
        return {
            'symbol': parts[0],
            'last': float(parts[1].replace(',','')),
            'turnover24h': float(parts[-1].replace(',','')) if parts[-1].replace(',','').replace('.','').isdigit() else 0,
            'fundingRate': 0.0,  # bybit-cli tickers не показывает funding, берём из REST
        }
    except Exception:
        return {}


def _count_down_days(sym: str) -> int:
    """Считает количество последовательных дней падения."""
    try:
        r = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit=8')
        if r and r.get('retCode') == 0:
            candles = r['result'].get('list', [])
            closes = [float(c[4]) for c in reversed(candles)]
            down = 0
            for i in range(len(closes) - 1, 0, -1):
                if closes[i] < closes[i-1]:
                    down += 1
                else:
                    break
            return down
    except Exception:
        pass  # ticker parse error — return 0 (no down days detected)
    return 0


def full_score_coin(sym: str, bb_data: dict, ticker_line: str) -> dict:
    """9-метричный LONG-скоринг (v4.0). Возвращает {score, breakdown, ...} или None если не подходит."""
    if not bb_data:
        return None

    bb_pos = bb_data['bb_pos']
    bb_width = bb_data.get('bb_width', 0)
    cur = bb_data['cur']

    # 1. BB score (0-15)
    if bb_pos <= 10:    bb_score = 15
    elif bb_pos <= 25:  bb_score = 12
    elif bb_pos <= 40:  bb_score = 8
    elif bb_pos <= 60:  bb_score = 5
    elif bb_pos <= 75:  bb_score = 3
    else:               bb_score = 1

    # Auto-skip: BB > 80% — неинтересно
    if bb_pos > 80:
        return None

    # 2. Volume score (0-10)
    t = _parse_ticker_line(ticker_line)
    turnover = t.get('turnover24h', 0)
    if turnover < 1_000_000:
        return None  # слишком мелкий
    if turnover > 500_000_000:    vol_score = 10
    elif turnover > 100_000_000:  vol_score = 8
    elif turnover > 50_000_000:   vol_score = 7
    elif turnover > 20_000_000:   vol_score = 6
    elif turnover > 10_000_000:   vol_score = 5
    elif turnover > 5_000_000:    vol_score = 4
    else:                         vol_score = 2

    # 3. Down days (0-10)
    down = _count_down_days(sym)
    if down >= 5:     down_score = 10
    elif down >= 3:   down_score = 8
    elif down >= 2:   down_score = 5
    elif down >= 1:   down_score = 3
    else:             down_score = 1

    # 4. Funding (0-5) — проверяем через REST
    fund_score = 3  # default если не получили
    try:
        fr = bybit('GET', f'/v5/market/tickers?category=linear&symbol={sym}')
        if fr and fr.get('retCode') == 0:
            tickers = fr['result'].get('list', [])
            if tickers:
                funding = float(tickers[0].get('fundingRate', 0))
                abs_f = abs(funding)
                if abs_f < 0.00005:        fund_score = 5
                elif abs_f < 0.0001:       fund_score = 4
                elif abs_f < 0.0002:       fund_score = 3
                elif abs_f < 0.0004:       fund_score = 2
                else:                      fund_score = 0
    except Exception:
        pass  # funding rate parse error — default score 0

    # 5. BB Width / Volatility (0-5)
    if 3 <= bb_width <= 8:    vola_score = 5
    elif 1 <= bb_width < 3:   vola_score = 3
    elif 8 < bb_width <= 15:  vola_score = 3
    else:                     vola_score = 1

    # 6. Quality score (0-5) — позиция на BB × ширина
    quality = (bb_pos / 100) * bb_width
    if quality <= 0.5:       qscore = 5
    elif quality <= 1.5:     qscore = 4
    elif quality <= 3.0:     qscore = 3
    elif quality <= 5.0:     qscore = 2
    else:                    qscore = 1

    total = bb_score + vol_score + down_score + fund_score + vola_score + qscore

    return {
        'symbol': sym,
        'score': total,
        'max_score': 50,
        'bb_pos': bb_pos,
        'bb_width': bb_width,
        'cur': cur,
        'breakdown': f'BB={bb_score} Vol={vol_score} Down={down_score} Fund={fund_score} Vola={vola_score} Q={qscore}',
    }


def auto_entry_scan(positions):
    """Скрининг и авто-вход по 9-метричному скорингу (v4.0)."""
    entries = []
    active = set(positions.keys())
    all_watch = list(AUTO_ENTRY_WATCH)
    wl_file = os.path.join(DATA_DIR, 'watchlist_custom.txt')
    if os.path.exists(wl_file):
        with open(wl_file) as f:
            custom = [l.strip() for l in f if l.strip() and l.strip().endswith('USDT')]
            all_watch.extend([s for s in custom if s not in all_watch])

    try:
        cfg = Config()
        banned = set(cfg.risk.get('banned_symbols', []))
        all_watch = [s for s in all_watch if s not in banned]
    except Exception as e:
        log_event(f'⚠️ auto_entry: {e}')

    try:
        r = safe_run([BYBIT_CLI, 'tickers'], timeout=15)
        if r.returncode != 0:
            return entries
        ticker_lines = r.stdout.split('\n')
    except Exception:
        return entries

    candidates = []
    for sym in all_watch:
        if sym in active:
            continue
        for line in ticker_lines:
            if sym in line:
                candidates.append((sym, line))
                break

    scored = []
    for sym, ticker_line in candidates:
        bb = get_bb_data(sym, 'D')
        if not bb:
            continue
        result = full_score_coin(sym, bb, ticker_line)
        if result and result['score'] >= MIN_SCORE:
            scored.append(result)

    # ── Фаза 4.3.1: Multi-TF конфлюенс-фильтр ──
    scored = _filter_by_mtf_confluence(scored, 'LONG')

    scored.sort(key=lambda x: x['score'], reverse=True)

    for s in scored:
        try:
            sym = s['symbol']
            cooldown = _load_cooldown()
            cfg = Config()
            cooldown_sl = cfg.strategy.long.get('cooldown_after_sl', 14400)
            if sym in cooldown:
                elapsed = time.time() - cooldown[sym]
                if elapsed < cooldown_sl:
                    continue

            # Маржа от score (нормализовано к 7.0 для совместимости)
            normalized_score = min(10, s['score'] / 5)  # 25→5, 50→10
            margin = margin_for_strategy('long', score=normalized_score)
            if margin <= 0:
                continue

            # Получаем BB для уровней входа
            bb2 = get_bb_data(sym, 'D')
            if not bb2:
                continue
            price = round(bb2['lower'], 4)
            qty = math.ceil(margin * 3 / price)
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
                # ── Фаза 4.3.2: Telegram-алерт при входе ──
                mtf_info = ''
                if 'mtf' in s:
                    mtf = s['mtf']
                    mtf_info = f' | MTF:{mtf["confluence"]}/3({mtf["strength"]})'

                entries.append(
                    f'🤖 Авто-вход {sym} @ ${price:.4f} x{qty} '
                    f'(score={s["score"]}/{s["max_score"]} BB={s["bb_pos"]:.0f}% {s["breakdown"]}{mtf_info})'
                )
                add_alert('ENTRY',
                    f'🚀 LONG {sym}: вход ${price:.4f} ×{qty} ({3}x) | '
                    f'score={s["score"]}/{s["max_score"]} BB={s["bb_pos"]:.0f}%{mtf_info}'
                )
                log_event(f'Авто-вход {sym} @ ${price:.4f} score={s["score"]} BB={s["bb_pos"]:.0f}%')
        except Exception as e:
            log_event(f'⚠️ auto_entry {sym}: {e}')
            continue

    return entries
