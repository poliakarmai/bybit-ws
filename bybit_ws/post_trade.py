"""
Post-Trade Analysis Loop — кластерный анализ win rate (28.06.2026).

После каждой закрытой сделки: сохраняет features + outcome.
Раз в неделю: win rate по кластерам (символ×режим×сессия).
Кластер <40% win rate за 30 дней → автоблок.
"""
import json
import os
import time
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path.home() / '.local' / 'share' / 'bybit-ws'
POST_TRADE_FILE = DATA_DIR / 'post_trade_features.jsonl'
BLOCKED_CLUSTERS_FILE = DATA_DIR / 'blocked_clusters.json'

MIN_TRADES_FOR_BLOCK = 10      # мин сделок в кластере для решения
LOW_WIN_RATE_THRESHOLD = 0.40  # <40% → блок
ANALYSIS_INTERVAL = 7 * 86400  # разбор раз в неделю


def save_trade_features(result: dict):
    """Сохранить features + outcome после закрытия сделки.

    result должен содержать:
        symbol, side, pnl, regime, session, volume_ratio, mtf_confluence,
        bb_pos, entry_price, exit_price, closed_at
    """
    try:
        entry = {
            'ts': time.time(),
            **result,
        }
        with open(POST_TRADE_FILE, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception:
        pass


def analyze_clusters() -> dict:
    """Проанализировать кластеры и вернуть блокированные.

    Returns:
        {'blocked': [(cluster_key, win_rate, trades)], 'stats': {...}}
    """
    if not POST_TRADE_FILE.exists():
        return {'blocked': [], 'stats': {}}

    now = time.time()
    cutoff = now - 30 * 86400  # последние 30 дней
    clusters = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0.0})

    try:
        with open(POST_TRADE_FILE) as f:
            for line in f:
                try:
                    t = json.loads(line)
                    if t.get('ts', 0) < cutoff:
                        continue
                    symbol = t.get('symbol', '?')
                    regime = t.get('regime', 'NEUTRAL')
                    session = t.get('session', 'normal')
                    key = f"{symbol}|{regime}|{session}"

                    if float(t.get('pnl', 0)) > 0:
                        clusters[key]['wins'] += 1
                    else:
                        clusters[key]['losses'] += 1
                    clusters[key]['pnl'] += float(t.get('pnl', 0))
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception:
        return {'blocked': [], 'stats': {}}

    blocked = []
    for key, stats in clusters.items():
        total = stats['wins'] + stats['losses']
        if total < MIN_TRADES_FOR_BLOCK:
            continue
        wr = stats['wins'] / total if total > 0 else 0
        if wr < LOW_WIN_RATE_THRESHOLD:
            blocked.append({
                'cluster': key,
                'win_rate': round(wr, 3),
                'trades': total,
                'pnl': round(stats['pnl'], 2),
            })

    # Сохранить заблокированные
    with open(BLOCKED_CLUSTERS_FILE, 'w') as f:
        json.dump({'blocked': blocked, 'updated': now}, f, indent=2)

    return {
        'blocked': blocked,
        'stats': {k: {'wr': round(v['wins']/(v['wins']+v['losses']), 2),
                       'trades': v['wins']+v['losses'],
                       'pnl': round(v['pnl'], 2)}
                  for k, v in clusters.items()},
    }


def is_cluster_blocked(symbol: str, regime: str, session: str = 'normal') -> bool:
    """Проверить заблокирован ли кластер."""
    if not BLOCKED_CLUSTERS_FILE.exists():
        return False

    try:
        with open(BLOCKED_CLUSTERS_FILE) as f:
            data = json.load(f)
    except Exception:
        return False

    key = f"{symbol}|{regime}|{session}"
    for b in data.get('blocked', []):
        if b['cluster'] == key:
            return True
    return False
