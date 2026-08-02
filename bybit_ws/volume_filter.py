"""
Volume Confirmation Filter — отсев BB-сигналов без объёма (28.06.2026).

Сравнивает текущий 5m volume с SMA(20):
- vol > 1.3×SMA → подтверждённый сигнал (пропускаем)
- vol < 0.7×SMA → тихий отскок (mean-reversion, пропускаем)
- Середина → блок (шум, нет уверенности)
"""
import time
from .api import bybit

# Кеш
_vol_cache: dict[str, tuple[float, float, float, int]] = {}  # symbol → (sma_vol, last_vol, ts, count)
_VOL_CACHE_TTL = 300  # 5 минут


def _get_volume_stats(symbol: str) -> tuple[float, float]:
    """Получить SMA(20) и последний volume для 5m свечей.

    Returns:
        (sma_vol, last_vol) — оба в USD, или (0, 0) при ошибке
    """
    now = time.time()
    if symbol in _vol_cache:
        sma_v, last_v, ts, _ = _vol_cache[symbol]
        if now - ts < _VOL_CACHE_TTL:
            return sma_v, last_v

    try:
        kline = bybit('GET',
            f'/v5/market/kline?category=linear&symbol={symbol}&interval=5&limit=25')
        if not kline or kline.get('retCode') != 0:
            return 0.0, 0.0

        candles = kline['result']['list'][:25]  # newest first
        if len(candles) < 21:
            return 0.0, 0.0

        # volume = строка, нужно в float
        volumes = [float(c[5]) for c in candles[:21]]
        sma_vol = sum(volumes[:-1]) / 20  # SMA(20) без последней свечи
        last_vol = volumes[0]  # самая свежая

        _vol_cache[symbol] = (sma_vol, last_vol, now, len(candles))
        return sma_vol, last_vol
    except Exception:
        return 0.0, 0.0


def volume_ok(symbol: str) -> tuple[bool, str]:
    """Проверить объём для входа.

    Returns:
        (ok, reason)
    """
    sma_vol, last_vol = _get_volume_stats(symbol)

    if sma_vol <= 0 or last_vol <= 0:
        return True, 'no_volume_data'  # нет данных → не блокируем

    ratio = last_vol / sma_vol

    if ratio > 1.3:
        return True, f'vol_confirm_{ratio:.1f}x'  # подтверждение
    elif ratio < 0.7:
        return True, f'vol_low_{ratio:.1f}x'  # тихий отскок
    else:
        return False, f'vol_noise_{ratio:.1f}x'  # шум → блок
