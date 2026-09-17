"""
Volatility Filter — ATR regime gate + volatility-scaled position sizing.

Блокирует новые входы при аномальном всплеске волатильности и уменьшает
размер позиции пропорционально росту ATR относительно базовой линии.

Конфиг: секция ``volatility_filter`` в config.yaml.
По умолчанию **выключен** (enabled=false) — текущее поведение не меняется.

Фаза: safety gate (2026-09).
"""

from .alerts import log_event
from .api import bybit, fetch_atr
from .config import Config


def _vf_cfg() -> dict:
    """Загрузить секцию volatility_filter из конфига."""
    try:
        return Config().cfg.get('volatility_filter', {}) or {}
    except Exception:
        return {}


def _baseline_atr(symbol: str, interval: str = 'D', n: int = 20) -> float | None:
    """Средний True Range за последние N свечей (simple average, не Wilder).

    Fetches N+1 candles so we can compute N true-range values.
    Returns None on any API / parse error.
    """
    try:
        data = bybit(
            'GET',
            f'/v5/market/kline?category=linear&symbol={symbol}'
            f'&interval={interval}&limit={n + 1}',
        )
        if not data or data.get('retCode') != 0:
            return None

        candles = data['result'].get('list', [])
        if len(candles) < n:
            return None

        # API returns newest first → reverse for chronological order
        candles = candles[:n + 1][::-1]

        tr_values = []
        for i in range(1, len(candles)):
            high = float(candles[i][2])
            low = float(candles[i][3])
            prev_close = float(candles[i - 1][4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_values.append(tr)

        if not tr_values:
            return None

        return sum(tr_values) / len(tr_values)
    except Exception as e:
        log_event(f'⚠️ volatility_filter baseline_atr({symbol}): {e}')
        return None


def is_high_volatility(symbol: str, interval: str = 'D') -> tuple:
    """Проверить, находится ли символ в режиме аномальной волатильности.

    Returns:
        (blocked: bool, ratio: float, reason: str)
        - blocked=True  → входы следует заблокировать
        - ratio         = current_atr / baseline_atr (0.0 если disabled/error)
        - reason        = человекочитаемое объяснение
    """
    cfg = _vf_cfg()
    if not cfg.get('enabled', False):
        return (False, 0.0, 'disabled')

    try:
        threshold = float(cfg.get('atr_ratio_threshold', 2.0))
        baseline_days = int(cfg.get('baseline_days', 20))

        current_atr = fetch_atr(symbol, interval, period=14)
        if current_atr is None or current_atr <= 0:
            return (False, 0.0, f'error: ATR unavailable for {symbol}')

        base = _baseline_atr(symbol, interval, n=baseline_days)
        if base is None or base <= 0:
            return (False, 0.0, f'error: baseline unavailable for {symbol}')

        ratio = current_atr / base
        blocked = ratio > threshold
        reason = f'ATR {ratio:.1f}x baseline (threshold {threshold:.1f}x)'
        if blocked:
            reason = f'BLOCKED — {reason}'

        return (blocked, round(ratio, 4), reason)

    except Exception as e:
        log_event(f'⚠️ is_high_volatility({symbol}): {e}')
        return (False, 0.0, f'error: {e}')


def volatility_scale(symbol: str, interval: str = 'D') -> float:
    """Множитель маржи (0.0, 1.0] — уменьшает позицию при высокой волатильности.

    Formula: scale = min(1.0, 1.0 / ratio), floored at config min_scale.
    Returns 1.0 when filter is disabled or on any error (fail-open).
    """
    cfg = _vf_cfg()
    if not cfg.get('enabled', False):
        return 1.0

    try:
        min_scale = float(cfg.get('min_scale', 0.5))
        baseline_days = int(cfg.get('baseline_days', 20))

        current_atr = fetch_atr(symbol, interval, period=14)
        if current_atr is None or current_atr <= 0:
            return 1.0

        base = _baseline_atr(symbol, interval, n=baseline_days)
        if base is None or base <= 0:
            return 1.0

        ratio = current_atr / base
        scale = min(1.0, 1.0 / ratio)
        return max(scale, min_scale)

    except Exception as e:
        log_event(f'⚠️ volatility_scale({symbol}): {e}')
        return 1.0
