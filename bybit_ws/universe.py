"""Единый источник символов: скан, WS-подписка, /scan.

Раньше было три списка: AUTO_ENTRY_WATCH в auto_entry.py, watchlist_custom.txt
и WATCH_SYMBOLS в ws_client.py. Они разъехались: 21 символ сканировался через
REST (скан 100-127с вместо ~40с), а символы вне WATCH_SYMBOLS не имели WS-кеша.
"""
import os

from .config import Config
from .constants import DATA_DIR, ORDERS_SNAPSHOT, POSITIONS_SNAPSHOT
from .snapshot import load_json

# Скан авто-входа (auto_entry.py)
AUTO_ENTRY_WATCH = [
    'BTCUSDT', 'ETHUSDT', 'LTCUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT',
    'NEARUSDT', 'SUIUSDT', 'WLDUSDT', 'LINKUSDT',
    'AAVEUSDT', 'AVAXUSDT', 'INJUSDT', 'ONDOUSDT', 'ARBUSDT',
    'ENAUSDT', 'FETUSDT', 'APTUSDT', 'ATOMUSDT',
]

# Жидкое ядро для /scan (rpc.py:1395 читает WS-кеш по топ-50 оборота)
LIQUID_CORE = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'LTCUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT',
    'HYPEUSDT', 'NEARUSDT', 'SUIUSDT', 'TONUSDT', 'WLDUSDT', 'LINKUSDT',
    'AAVEUSDT', 'AVAXUSDT', 'DOTUSDT', 'INJUSDT', 'ONDOUSDT', 'ARBUSDT',
    'ENAUSDT', 'FETUSDT', 'APTUSDT', 'ATOMUSDT', 'RUNEUSDT',
    'UNIUSDT', 'OPUSDT', 'ALGOUSDT', 'BCHUSDT', 'FILUSDT', 'VETUSDT',
    'SANDUSDT', 'MANAUSDT', 'GALAUSDT', 'PEPEUSDT', 'SHIBUSDT',
    'EGLDUSDT', 'ZECUSDT', 'BONKUSDT', 'FLOKIUSDT', 'WIFUSDT',
    'TIAUSDT', 'SEIUSDT', 'STRKUSDT', 'JUPUSDT', 'PYTHUSDT',
    'WUSDT', 'MOVEUSDT', 'STGUSDT', 'ESPORTSUSDT',
]


def _dedup(symbols) -> list[str]:
    """Убрать дубли, сохранив порядок."""
    return list(dict.fromkeys(symbols))


def _custom_watchlist() -> list[str]:
    """watchlist_custom.txt: строки, оканчивающиеся на USDT. Нет файла → []."""
    try:
        with open(os.path.join(DATA_DIR, 'watchlist_custom.txt')) as f:
            return [line.strip() for line in f if line.strip().endswith('USDT')]
    except OSError:
        return []


def _banned() -> set[str]:
    """Config().risk['banned_symbols']; любая ошибка → пустое множество (fail-open)."""
    try:
        return set(Config().risk.get('banned_symbols', []))
    except Exception:
        return set()


def scan_universe() -> list[str]:
    """AUTO_ENTRY_WATCH + custom − banned. Порядок сохранён, дубли убраны."""
    banned = _banned()
    return [s for s in _dedup(AUTO_ENTRY_WATCH + _custom_watchlist()) if s not in banned]


def watch_symbols() -> list[str]:
    """scan_universe() ∪ LIQUID_CORE ∪ символы живых позиций ∪ символы открытых ордеров.

    Позиции/ордера читаются из снапшотов (positions.json / orders.json, локальные файлы,
    без сети). Символ, появившийся в позиции ПОСЛЕ коннекта, получит WS только на
    следующем реконнекте — до этого работает REST-fallback в _get_bb_ws.
    """
    # positions.json: ключ = символ, поля 'symbol' в значении может не быть
    positions = list(load_json(POSITIONS_SNAPSHOT))
    # orders.json: ключ = SYMBOL_uuid, символ в поле 'symbol'
    orders = [o.get('symbol') for o in load_json(ORDERS_SNAPSHOT).values() if isinstance(o, dict)]
    return _dedup(scan_universe() + LIQUID_CORE + positions + [o for o in orders if o])
