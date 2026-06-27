"""
risk_manager.py — Глобальный risk-менеджмент (Фаза 6.7).

Модули:
  - Глобальный daily PnL (сумма по всем позициям + закрытые сделки за сегодня)
  - Корреляционная матрица позиций (запрет входа если новый тикер коррелирует >0.8)
  - Circuit breaker: если daily PnL достиг 80% лимита — только закрытие, без новых входов
  - Dynamic max_positions: авто-ограничение от волатильности рынка
"""

import json
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

_log = logging.getLogger('bybit.risk_manager')

# ── Config singleton (ленивая загрузка, failsafe) ─────────────────────────
_cfg_instance = None


class _FailsafeConfig:
    """Заглушка Config с дефолтными значениями risk — fallback при ошибке импорта."""
    @property
    def risk(self):
        return {
            'banned_symbols': [],
            'max_daily_loss': 50,
        }


def _get_config():
    """Ленивая загрузка Config. При ошибке — логирует и возвращает _FailsafeConfig."""
    global _cfg_instance
    if _cfg_instance is not None:
        return _cfg_instance
    try:
        from .config import Config
        _cfg_instance = Config()
    except Exception as e:
        _log.error(f'risk_manager: Config load failed, using failsafe defaults: {e}')
        _cfg_instance = _FailsafeConfig()
    return _cfg_instance


# ── Пути ──────────────────────────────────────────────────────────────────
DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws"
RISK_STATE_FILE = DATA_DIR / "risk_manager.json"


def _get_wallet_balance() -> Optional[dict]:
    """Получить баланс кошелька: сначала WS-кеш, затем REST API."""
    # 1. WS-кеш (если BYBIT_WS_FULL_ENABLED=1)
    try:
        from .ws_client import get_wallet, is_full_enabled, is_private_connected
        if is_full_enabled() and is_private_connected():
            w = get_wallet()
            if w:
                return w
    except Exception:
        pass
    
    # 2. REST API fallback
    try:
        from .api import bybit
        data = bybit('GET', '/v5/account/wallet-balance?accountType=UNIFIED&coin=USDT')
        if data and isinstance(data, dict) and data.get('retCode') == 0:
            coins = data['result'].get('list', [])
            for c in coins:
                if c.get('coin') == 'USDT':
                    return {
                        'availableBalance': c.get('availableToWithdraw', '0'),
                        'equity': c.get('equity', '0'),
                        'totalWalletBalance': c.get('walletBalance', '0'),
                    }
    except Exception:
        pass
    
    return None

# ── Константы ─────────────────────────────────────────────────────────────
CORRELATION_THRESHOLD = 0.80       # порог корреляции для блокировки входа
CIRCUIT_BREAKER_PCT = 0.80         # 80% от дневного лимита → circuit breaker
DEFAULT_MAX_POSITIONS = 12          # базовый лимит позиций
HIGH_VOLATILITY_MAX_POSITIONS = 5   # лимит при высокой волатильности
VOLATILITY_WINDOW_DAYS = 7         # окно для расчёта волатильности

# ── Внутреннее состояние ─────────────────────────────────────────────────
_circuit_breaker_active = False
_circuit_breaker_ts = 0.0
_circuit_breaker_reason = ""


def _load_risk_state() -> dict:
    """Загрузить состояние risk-менеджера из JSON."""
    try:
        if RISK_STATE_FILE.exists():
            with open(RISK_STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        _log.warning(f'risk_manager load: {e}')
    return {}


def _save_risk_state(state: dict):
    """Сохранить состояние risk-менеджера в JSON."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = str(RISK_STATE_FILE) + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, str(RISK_STATE_FILE))
    except Exception as e:
        _log.warning(f'risk_manager save: {e}')


def _today_key() -> str:
    """Ключ сегодняшнего дня (YYYY-MM-DD)."""
    return datetime.now().strftime('%Y-%m-%d')


# ═══════════════════════════════════════════════════════════════════════════
# Daily PnL
# ═══════════════════════════════════════════════════════════════════════════

def get_daily_pnl() -> dict:
    """Рассчитать daily PnL: unrealized + realized за сегодня.

    Returns:
        {
            'date': 'YYYY-MM-DD',
            'unrealized_pnl': float,   # сумма unrealized PnL по всем позициям
            'realized_pnl': float,     # закрытые сделки за сегодня
            'total_pnl': float,        # unrealized + realized
            'position_count': int,
        }
    """
    result = {
        'date': _today_key(),
        'unrealized_pnl': 0.0,
        'realized_pnl': 0.0,
        'total_pnl': 0.0,
        'position_count': 0,
    }

    # Unrealized PnL: из positions.json
    try:
        positions_file = DATA_DIR / "positions.json"
        if positions_file.exists():
            with open(positions_file) as f:
                positions = json.load(f)
            if isinstance(positions, dict):
                for sym, p in positions.items():
                    if isinstance(p, dict):
                        result['unrealized_pnl'] += float(p.get('upnl', 0) or 0)
                result['position_count'] = len(positions)
    except Exception as e:
        _log.debug(f'get_daily_pnl positions: {e}')

    # Realized PnL: из metrics.json
    try:
        metrics_file = DATA_DIR / "metrics.json"
        if metrics_file.exists():
            with open(metrics_file) as f:
                metrics = json.load(f)
            today = _today_key()
            # Ищем ключ за сегодня
            for k in sorted(metrics.keys(), reverse=True):
                if k.startswith(today) or (len(k) >= 10 and k[:10] == today):
                    result['realized_pnl'] = float(metrics[k].get('pnl_total', 0) or 0)
                    break
    except Exception as e:
        _log.debug(f'get_daily_pnl metrics: {e}')

    # Также проверяем trades.jsonl за сегодня
    try:
        trades_file = DATA_DIR / "trades.jsonl"
        today_str = _today_key()
        if trades_file.exists():
            with open(trades_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        trade = json.loads(line)
                        trade_ts = trade.get('ts', '')
                        if isinstance(trade_ts, (int, float)):
                            trade_date = datetime.fromtimestamp(trade_ts).strftime('%Y-%m-%d')
                        else:
                            trade_date = str(trade_ts)[:10]
                        if trade_date == today_str:
                            # Учитываем только если ещё не подсчитано через metrics
                            pass  # metrics уже содержит сумму
                    except Exception:
                        pass
    except Exception as e:
        _log.debug(f'get_daily_pnl trades: {e}')

    result['total_pnl'] = result['realized_pnl']  # только realized, unrealized — из старых позиций
    result['total_pnl'] = round(result['total_pnl'], 2)
    result['unrealized_pnl'] = round(result['unrealized_pnl'], 2)
    result['realized_pnl'] = round(result['realized_pnl'], 2)

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Корреляционная матрица
# ═══════════════════════════════════════════════════════════════════════════

def check_new_entry_correlation(
    positions: dict,
    new_symbol: str,
    new_side: str = 'Buy',
) -> Tuple[bool, str]:
    """Проверить, можно ли войти в новый тикер по корреляции.

    Запрещает вход, если новый тикер коррелирует >0.8 с существующей
    позицией того же направления (LONG+LONG или SHORT+SHORT).

    Args:
        positions: dict {symbol: position_data}
        new_symbol: тикер для входа
        new_side: 'Buy' (LONG) или 'Sell' (SHORT)

    Returns:
        (allowed: bool, reason: str)
    """
    if not positions or len(positions) < 1:
        return True, ''

    # Загружаем последний correlation snapshot
    corr_snapshot = _load_correlation_snapshot()
    if not corr_snapshot:
        return True, ''  # нет данных — разрешаем

    flagged = corr_snapshot.get('flagged', [])
    if not flagged:
        return True, ''

    # Ищем корреляции с new_symbol
    blocked_pairs = []
    for s1, s2, r in flagged:
        if new_symbol in (s1, s2):
            other = s2 if new_symbol == s1 else s1
            # Проверяем, есть ли позиция по другому символу
            if other in positions:
                other_pos = positions[other]
                other_side = other_pos.get('side', '')
                # Блокируем только если направления совпадают
                if other_side == new_side:
                    blocked_pairs.append((other, r))

    if blocked_pairs:
        details = ', '.join(
            f'{s} (r={r:+.3f})' for s, r in blocked_pairs
        )
        return False, (
            f'Корреляция >{CORRELATION_THRESHOLD}: '
            f'{new_symbol} коррелирует с {details} — вход запрещён'
        )

    return True, ''


def _load_correlation_snapshot() -> Optional[dict]:
    """Загрузить последний correlation snapshot."""
    corr_file = DATA_DIR / "correlation.json"
    if not corr_file.exists():
        return None
    try:
        with open(corr_file) as f:
            return json.load(f)
    except Exception:
        return None


def get_correlation_matrix() -> dict:
    """Получить текущую корреляционную матрицу позиций.

    Returns:
        {
            'pairs': [...],
            'flagged': [...],
            'position_count': int,
            'timestamp': str,
            'threshold': float,
        }
    """
    snapshot = _load_correlation_snapshot()
    if snapshot:
        return {
            'pairs': snapshot.get('pairs', []),
            'flagged': snapshot.get('flagged', []),
            'position_count': snapshot.get('position_count', 0),
            'timestamp': snapshot.get('timestamp', ''),
            'threshold': snapshot.get('threshold', CORRELATION_THRESHOLD),
        }
    return {
        'pairs': [],
        'flagged': [],
        'position_count': 0,
        'timestamp': '',
        'threshold': CORRELATION_THRESHOLD,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Circuit Breaker
# ═══════════════════════════════════════════════════════════════════════════

def check_circuit_breaker(config=None) -> Tuple[bool, str]:
    """Проверить состояние circuit breaker.

    Если daily PnL >= 80% от max_daily_loss — активируем circuit breaker.
    В режиме circuit breaker: только закрытие позиций, без новых входов.

    Args:
        config: Config-объект (опционально)

    Returns:
        (breaker_active: bool, reason: str)
    """
    global _circuit_breaker_active, _circuit_breaker_ts, _circuit_breaker_reason

    # Загружаем конфиг
    try:
        if config is None:
            from .config import Config
            config = Config()
        risk_cfg = config.risk if hasattr(config, 'risk') else {}
        max_daily_loss = float(risk_cfg.get('max_daily_loss', 50))
    except Exception:
        max_daily_loss = 50.0

    # Если уже активен — не сбрасываем до конца дня
    if _circuit_breaker_active:
        # Проверяем: новый день — автосброс
        today = _today_key()
        breaker_date = datetime.fromtimestamp(_circuit_breaker_ts).strftime('%Y-%m-%d')
        if breaker_date != today:
            _circuit_breaker_active = False
            _circuit_breaker_reason = ''
            _log.info('Circuit breaker: новый день — автосброс')
            return False, ''
        return True, _circuit_breaker_reason

    # Проверяем daily PnL
    daily = get_daily_pnl()
    total_pnl = daily['total_pnl']
    threshold = max_daily_loss * CIRCUIT_BREAKER_PCT  # 80% лимита

    if total_pnl <= -threshold:
        _circuit_breaker_active = True
        _circuit_breaker_ts = time.time()
        _circuit_breaker_reason = (
            f'Circuit Breaker: daily PnL ${total_pnl:.2f} достиг '
            f'{CIRCUIT_BREAKER_PCT*100:.0f}% лимита (${threshold:.2f} из ${max_daily_loss:.2f}). '
            f'Только закрытие позиций, новые входы запрещены.'
        )
        _log.warning(_circuit_breaker_reason)
        return True, _circuit_breaker_reason

    return False, ''


def is_circuit_breaker_active() -> bool:
    """Активен ли circuit breaker прямо сейчас."""
    return _circuit_breaker_active


# ── Black Swan / Emergency Close ───────────────────────────────────────────
BLACK_SWAN_PNL_PCT = 2.0          # 2x daily loss limit → black swan
BLACK_SWAN_PRICE_DROP = 0.08      # 8% drop in BTC за 1 час → black swan (was 15%)
_emergency_close_active = False


def check_black_swan(positions: dict) -> Tuple[bool, str]:
    """Проверить условия black swan — экстремальные события.

    Триггеры:
    1. PnL > 2x от max_daily_loss
    2. BTC упал >15% за последний час

    Returns:
        (black_swan: bool, reason: str)
    """
    # Триггер 1: PnL
    daily = get_daily_pnl()
    try:
        max_loss = float(_get_config().risk.get('max_daily_loss', 50))
    except Exception:
        max_loss = 50.0

    if daily['total_pnl'] <= -max_loss * BLACK_SWAN_PNL_PCT:
        pnl_limit = max_loss * BLACK_SWAN_PNL_PCT
        return True, (
            f'BLACK SWAN: PnL ${daily["total_pnl"]:.2f} > '
            f'{BLACK_SWAN_PNL_PCT}x loss limit (${pnl_limit:.0f})'
        )

    # Триггер 2: BTC crash >15% за час
    try:
        from .correlation import fetch_klines
        btc = fetch_klines('BTCUSDT', interval='15', limit=4)  # 4 × 15min = 1 hour
        if btc and len(btc) >= 2:
            drop = (btc[0] - btc[-1]) / btc[0]
            if drop > BLACK_SWAN_PRICE_DROP:
                return True, (
                    f'BLACK SWAN: BTC упал на {drop*100:.1f}% за час '
                    f'(> {BLACK_SWAN_PRICE_DROP*100:.0f}%)'
                )
    except Exception:
        pass

    return False, ''


def emergency_close_all(reason: str, positions: dict) -> dict:
    """Закрыть ВСЕ позиции по рынку.

    Вызывается при black swan — не ждёт, не обсуждает.
    Использует MARKET ордера с reduceOnly=True.

    Returns:
        {'closed': int, 'failed': int, 'errors': [...]}
    """
    global _emergency_close_active
    _emergency_close_active = True

    from .api import bybit
    result = {'closed': 0, 'failed': 0, 'errors': []}

    _log.critical(f'🚨 EMERGENCY CLOSE: {reason}')

    for sym, p in positions.items():
        if not isinstance(p, dict):
            continue
        size = float(p.get('size', 0))
        if size <= 0:
            continue

        side = 'Sell' if p.get('side') == 'Buy' else 'Buy'
        idx = p.get('positionIdx', 0)
        qty = str(size)

        try:
            order = bybit('POST', '/v5/order/create', {
                'category': 'linear',
                'symbol': sym,
                'side': side,
                'orderType': 'Market',
                'qty': qty,
                'positionIdx': idx,
                'reduceOnly': True,
                'timeInForce': 'IOC',
            })
            if order and order.get('retCode') == 0:
                result['closed'] += 1
                _log.critical(f'🚨 EMERGENCY CLOSE {sym}: {qty} @ MARKET')
            else:
                result['failed'] += 1
                err = order.get('retMsg', '?') if order else 'no response'
                result['errors'].append(f'{sym}: {err}')
                _log.error(f'🚨 EMERGENCY CLOSE {sym} FAILED: {err}')
        except Exception as e:
            result['failed'] += 1
            result['errors'].append(f'{sym}: {e}')
            _log.error(f'🚨 EMERGENCY CLOSE {sym} EXCEPTION: {e}')

    return result


def is_emergency_close_active() -> bool:
    """Активен ли режим emergency close."""
    return _emergency_close_active


def reset_circuit_breaker() -> dict:
    """Сбросить circuit breaker (ручной сброс).

    Returns:
        {'status': 'ok', 'was_active': bool, 'message': str}
    """
    global _circuit_breaker_active, _circuit_breaker_reason
    was_active = _circuit_breaker_active
    _circuit_breaker_active = False
    _circuit_breaker_reason = ''
    _log.info('Circuit breaker: ручной сброс')
    return {
        'status': 'ok',
        'was_active': was_active,
        'message': 'Circuit breaker сброшен. Новые входы разрешены.',
    }


def get_circuit_breaker_status() -> dict:
    """Получить статус circuit breaker.

    Returns:
        {
            'active': bool,
            'reason': str,
            'activated_at': float (timestamp) or None,
            'max_daily_loss': float,
            'daily_pnl': float,
            'threshold_pct': float,
            'can_auto_reset': bool,
        }
    """
    try:
        from .config import Config
        risk_cfg = Config().risk
        max_daily_loss = float(risk_cfg.get('max_daily_loss', 50))
    except Exception:
        max_daily_loss = 50.0

    daily = get_daily_pnl()

    # Проверяем авто-сброс (новый день)
    can_auto_reset = False
    if _circuit_breaker_active and _circuit_breaker_ts > 0:
        breaker_date = datetime.fromtimestamp(_circuit_breaker_ts).strftime('%Y-%m-%d')
        if breaker_date != _today_key():
            can_auto_reset = True

    return {
        'active': _circuit_breaker_active,
        'reason': _circuit_breaker_reason if _circuit_breaker_active else '',
        'activated_at': _circuit_breaker_ts if _circuit_breaker_active else None,
        'max_daily_loss': max_daily_loss,
        'daily_pnl': daily['total_pnl'],
        'daily_unrealized_pnl': daily['unrealized_pnl'],
        'daily_realized_pnl': daily['realized_pnl'],
        'threshold_pct': CIRCUIT_BREAKER_PCT,
        'threshold_value': round(max_daily_loss * CIRCUIT_BREAKER_PCT, 2),
        'can_auto_reset': can_auto_reset,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Dynamic Max Positions
# ═══════════════════════════════════════════════════════════════════════════

# Кеш для dynamic_max_positions
_VOLATILITY_CACHE = {'value': None, 'ts': 0, 'ttl': 3600}  # кеш 1 час


def get_dynamic_max_positions() -> dict:
    """Рассчитать динамический лимит позиций от волатильности рынка.

    Использует BTC как прокси волатильности рынка.
    Высокая волатильность → меньше позиций.
    Низкая волатильность → стандартный лимит.

    Returns:
        {
            'max_positions': int,
            'base_max': int,
            'high_volatility_max': int,
            'volatility': float | None,
            'volatility_level': 'low' | 'normal' | 'high',
            'method': str,
        }
    """
    now = time.time()
    cache = _VOLATILITY_CACHE

    # Проверяем кеш
    if (cache['value'] is not None and
            now - cache['ts'] < cache['ttl']):
        return dict(cache['value'])

    # Загружаем конфиг
    try:
        from .config import Config
        cfg = Config()
        base_max = int(cfg.risk.get('max_long_positions', DEFAULT_MAX_POSITIONS))
    except Exception:
        base_max = DEFAULT_MAX_POSITIONS

    volatility = None
    volatility_level = 'normal'
    max_pos = base_max

    # Пытаемся получить волатильность BTC
    try:
        from .correlation import fetch_klines
        # Используем daily свечи за VOLATILITY_WINDOW_DAYS дней
        btc_prices = fetch_klines('BTCUSDT', interval='D', limit=VOLATILITY_WINDOW_DAYS + 1)
        if btc_prices and len(btc_prices) >= 3:
            # Рассчитываем дневную волатильность (среднее |return|)
            returns = [
                abs(math.log(btc_prices[i] / btc_prices[i - 1]))
                for i in range(1, len(btc_prices))
            ]
            volatility = sum(returns) / len(returns)  # средняя abs log-return

            # Классификация волатильности
            if volatility > 0.05:       # >5% средняя дневная волатильность
                volatility_level = 'high'
                max_pos = HIGH_VOLATILITY_MAX_POSITIONS
            elif volatility > 0.02:     # 2-5%
                volatility_level = 'normal'
                max_pos = base_max
            else:                        # <2%
                volatility_level = 'low'
                max_pos = base_max       # при низкой — стандартный лимит
    except Exception as e:
        _log.debug(f'dynamic_max_positions BTC fetch: {e}')

    result = {
        'max_positions': max_pos,
        'base_max': base_max,
        'high_volatility_max': HIGH_VOLATILITY_MAX_POSITIONS,
        'volatility': round(volatility, 6) if volatility is not None else None,
        'volatility_level': volatility_level,
        'method': 'btc_proxy' if volatility is not None else 'default',
    }

    # Кешируем
    cache['value'] = result
    cache['ts'] = now

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Главная проверка risk_manager
# ═══════════════════════════════════════════════════════════════════════════

def check(
    positions: dict,
    new_symbol: Optional[str] = None,
    new_side: Optional[str] = None,
    config=None,
) -> Tuple[bool, str]:
    """Главная проверка риск-менеджера.

    Вызывается:
    - Перед каждым авто-входом (с new_symbol и new_side)
    - В начале каждого цикла (без new_symbol — только circuit breaker)

    Args:
        positions: dict {symbol: position_data}
        new_symbol: тикер для нового входа (None = проверка без входа)
        new_side: 'Buy' или 'Sell' для нового входа
        config: Config-объект (опционально)

    Returns:
        (allowed: bool, reason: str)
    """
    # ── 1. Circuit breaker ──
    breaker_active, breaker_reason = check_circuit_breaker(config)
    if breaker_active:
        # Если проверка без нового входа — просто возвращаем статус
        if new_symbol is None:
            return False, breaker_reason
        # Новый вход — запрещён
        return False, breaker_reason

    # Если нет нового входа — всё ок
    if new_symbol is None:
        return True, ''

    # ── 2. Проверка banned_symbols ──
    cfg = config if config is not None else _get_config()
    banned = cfg.risk.get('banned_symbols', []) if hasattr(cfg, 'risk') else []
    if new_symbol in banned:
        return False, f'{new_symbol} в бан-листе — вход запрещён'

    # ── 3. Проверка max positions ──
    dyn_max = get_dynamic_max_positions()
    max_pos = dyn_max['max_positions']
    current_count = len(positions) if positions else 0
    if current_count >= max_pos:
        return False, (
            f'Достигнут лимит позиций: {current_count}/{max_pos} '
            f'(волатильность: {dyn_max["volatility_level"]})'
        )

    # ── 4. Проверка корреляции ──
    if new_symbol and positions:
        corr_ok, corr_reason = check_new_entry_correlation(
            positions, new_symbol, new_side or 'Buy'
        )
        if not corr_ok:
            return False, corr_reason

    # ── 5. Проверка daily PnL (лимит) ──
    daily = get_daily_pnl()
    cfg = config if config is not None else _get_config()
    max_daily_loss = float(cfg.risk.get('max_daily_loss', 50))

    if daily['total_pnl'] <= -max_daily_loss:
        return False, (
            f'Дневной лимит убытка: ${daily["total_pnl"]:.2f} '
            f'(лимит ${max_daily_loss:.2f})'
        )

    # ── 6. Проверка максимальной маржи (динамическая) ──
    try:
        max_total_margin = float(config.risk.get('max_total_margin', 300))
        dynamic_pct = float(config.risk.get('dynamic_margin_pct', 0))
        
        # Динамический лимит: % от available balance
        if dynamic_pct > 0:
            wallet = _get_wallet_balance()
            if wallet and wallet.get('availableBalance'):
                avail = float(wallet['availableBalance'])
                dynamic_limit = round(avail * dynamic_pct / 100, 2)
                max_total_margin = max(max_total_margin, dynamic_limit)
        
        total_margin = sum(
            float(p.get('margin', 0) or p.get('positionIM', 0))
            for p in positions.values()
        ) if positions else 0
        if total_margin >= max_total_margin:
            return False, (
                f'Лимит маржи: ${total_margin:.2f} / ${max_total_margin:.2f}'
            )
    except Exception:
        pass

    return True, ''


# ═══════════════════════════════════════════════════════════════════════════
# Полный отчёт
# ═══════════════════════════════════════════════════════════════════════════

def get_risk_full(positions: Optional[dict] = None) -> dict:
    """Полный отчёт risk-менеджера.

    Returns:
        {
            'daily_pnl': {...},
            'circuit_breaker': {...},
            'correlation': {...},
            'dynamic_max_positions': {...},
            'limits': {...},
            'positions_summary': {...},
            'timestamp': str,
        }
    """
    if positions is None:
        try:
            positions_file = DATA_DIR / "positions.json"
            if positions_file.exists():
                with open(positions_file) as f:
                    positions = json.load(f)
            else:
                positions = {}
        except Exception:
            positions = {}

    # Конфиг
    try:
        from .config import Config
        cfg = Config()
        risk_cfg = cfg.risk if hasattr(cfg, 'risk') else {}
    except Exception:
        risk_cfg = {}

    # Секторный анализ
    sectors = risk_cfg.get('sectors', {})
    sector_counts = {}
    if positions and sectors:
        for sym in positions:
            for sector_name, sector_symbols in sectors.items():
                if sym in sector_symbols:
                    sector_counts[sector_name] = sector_counts.get(sector_name, 0) + 1
                    break

    # Сводка по позициям
    pos_values = positions.values() if positions else []
    long_count = sum(1 for p in pos_values if isinstance(p, dict) and p.get('side') == 'Buy')
    short_count = sum(1 for p in pos_values if isinstance(p, dict) and p.get('side') == 'Sell')
    total_margin = sum(
        float(p.get('margin', 0) or p.get('positionIM', 0))
        for p in pos_values
        if isinstance(p, dict)
    )

    return {
        'daily_pnl': get_daily_pnl(),
        'circuit_breaker': get_circuit_breaker_status(),
        'correlation': get_correlation_matrix(),
        'dynamic_max_positions': get_dynamic_max_positions(),
        'limits': {
            'max_daily_loss': risk_cfg.get('max_daily_loss', 50),
            'max_total_margin': risk_cfg.get('max_total_margin', 300),
            'max_position_size': risk_cfg.get('max_position_size', 100),
            'max_drawdown_pct': risk_cfg.get('max_drawdown_pct', 15),
            'max_long_positions': risk_cfg.get('max_long_positions', 12),
            'max_per_sector': risk_cfg.get('max_per_sector', 3),
            'banned_symbols': risk_cfg.get('banned_symbols', []),
        },
        'positions_summary': {
            'total_count': len(positions) if isinstance(positions, dict) else 0,
            'long_count': long_count,
            'short_count': short_count,
            'total_margin': round(total_margin, 2),
            'sector_counts': sector_counts,
        },
        'timestamp': datetime.now().isoformat(),
    }
