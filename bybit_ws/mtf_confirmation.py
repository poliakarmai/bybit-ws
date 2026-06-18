"""
Multi-Timeframe Confirmation (v1.0) — Фаза 4.3.1

Проверяет согласованность D/W/M таймфреймов для LONG и SHORT сигналов.
Возвращает confluence score (0-3) и флаг approved (2/3 или 3/3).

Логика:
- LONG: цена ниже Middle BB (BB pos < 50) на каждом ТФ
- SHORT: цена выше Middle BB (BB pos > 50) на каждом ТФ
- Конфлюенс 2/3 = approved, 3/3 = strong
"""

import math
from typing import Optional, Dict, List, Tuple

from .api import bybit

# Конфигурация
CONFLUENCE_MIN_TFS = 2       # минимум ТФ для одобрения (2/3)
TF_LIST: Tuple[str, ...] = ('D', 'W', 'M')
TF_LABELS = {'D': 'day', 'W': 'week', 'M': 'month'}


def _fetch_candles(symbol: str, interval: str = 'D', limit: int = 30) -> Optional[list]:
    """Получить свечи (от старых к новым) через REST API."""
    r = bybit('GET', f'/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}')
    if not r or r.get('retCode') != 0:
        return None
    try:
        candles = r.get('result', {}).get('list', [])
        if candles:
            return list(reversed(candles))  # Bybit: новые→старые, нам надо старые→новые
    except (KeyError, IndexError, TypeError):
        pass
    return None


def _calc_bb(candles: list) -> Optional[dict]:
    """Рассчитать Bollinger Bands (20, 2)."""
    closes = [float(c[4]) for c in candles[-20:]]
    if len(closes) < 20:
        return None
    sma = sum(closes) / 20
    variance = sum((x - sma) ** 2 for x in closes) / 20
    std = math.sqrt(variance)
    return {
        'upper': sma + 2 * std,
        'middle': sma,
        'lower': sma - 2 * std,
        'current': closes[-1],
        'pos': ((closes[-1] - (sma - 2 * std)) / (4 * std)) * 100 if std > 0 else 50,
        'width': ((4 * std) / sma) * 100 if sma > 0 else 10,
    }


def _bb_signal(bb: Optional[dict], direction: str) -> Optional[dict]:
    """Определить, есть ли сигнал на одном ТФ по BB-полосам.

    Returns dict с деталями или None если данных недостаточно.
    """
    if not bb:
        return None

    pos = bb['pos']
    current = bb['current']
    lower = bb['lower']
    upper = bb['upper']
    middle = bb['middle']

    bb_range = upper - lower
    if bb_range <= 0:
        bb_range = 0.0001  # safety

    if direction == 'LONG':
        signal = pos < 50  # цена ниже середины BB → потенциал роста
        dist_to_band = (current - lower) / bb_range  # 0 = на нижней, 1 = на верхней
    else:  # SHORT
        signal = pos > 50  # цена выше середины BB → потенциал падения
        dist_to_band = (upper - current) / bb_range  # 0 = на верхней, 1 = на нижней

    return {
        'pos': round(pos, 1),
        'signal': signal,
        'current': round(current, 8),
        'bb_lower': round(lower, 8),
        'bb_upper': round(upper, 8),
        'bb_middle': round(middle, 8),
        'distance_to_band': round(dist_to_band, 3),
    }


def check_confluence(symbol: str, direction: str = 'LONG') -> Optional[dict]:
    """Проверить D/W/M конфлюенс для торгового сигнала.

    Args:
        symbol: торговая пара (LINKUSDT)
        direction: 'LONG' или 'SHORT'

    Returns:
        dict с результатами или None если не удалось получить данные D-ТФ
    """
    tf_results = {}
    approved_tfs: List[str] = []

    for tf in TF_LIST:
        candles = _fetch_candles(symbol, tf, 30)
        if not candles or len(candles) < 20:
            tf_results[tf] = None
            continue

        bb = _calc_bb(candles)
        if not bb:
            tf_results[tf] = None
            continue

        signal_info = _bb_signal(bb, direction)
        if signal_info is None:
            tf_results[tf] = None
            continue

        tf_results[tf] = signal_info
        if signal_info['signal']:
            approved_tfs.append(tf)

    # Если нет данных по D-ТФ — бесполезно
    if tf_results.get('D') is None:
        return None

    confluence = len(approved_tfs)
    approved = confluence >= CONFLUENCE_MIN_TFS

    return {
        'symbol': symbol,
        'direction': direction,
        'timeframes': tf_results,
        'confluence': confluence,
        'confluence_tfs': approved_tfs,
        'approved': approved,
        'strength': 'strong' if confluence == 3 else ('normal' if confluence == 2 else 'weak'),
        'filter_reason': None if approved else _filter_reason(tf_results, direction),
    }


def _filter_reason(tf_results: dict, direction: str) -> str:
    """Сформировать читаемую причину фильтрации."""
    disagreeing = []
    for tf, info in tf_results.items():
        if info is None:
            disagreeing.append(f'{TF_LABELS.get(tf, tf)}=no_data')
        elif not info['signal']:
            disagreeing.append(f'{TF_LABELS.get(tf, tf)}=disagree(pos={info["pos"]})')

    return ', '.join(disagreeing) if disagreeing else 'unknown'


def format_confluence(conf: dict) -> str:
    """Форматировать результат конфлюенса для логов/алертов."""
    if conf is None:
        return '⛔ MTF: no data'

    emoji = '🔥' if conf['strength'] == 'strong' else ('✅' if conf['approved'] else '❌')
    tfs_str = '+'.join(conf['confluence_tfs']) if conf['confluence_tfs'] else 'none'
    detail = []
    for tf in TF_LIST:
        info = conf['timeframes'].get(tf)
        if info is not None and isinstance(info, dict) and 'signal' in info:
            icon = '🟢' if info['signal'] else '🔴'
            detail.append(f'{icon}{tf}:{info["pos"]}%')
        else:
            detail.append(f'⚪{tf}:—')

    msg = f'{emoji} MTF [{conf["symbol"]}] {conf["direction"]}: {conf["confluence"]}/3 ({tfs_str})'
    msg += f' — {conf["strength"]}'
    msg += '\n   ' + ' '.join(detail)

    if not conf['approved']:
        msg += f'\n   ↳ Filter: {conf["filter_reason"]}'

    return msg
