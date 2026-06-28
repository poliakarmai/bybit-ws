"""
Paper Trading module — интеграция PaperExchange в основной цикл.

Feature flag: BYBIT_PAPER_ENABLED=1
DB: ~/.local/share/bybit-ws/paper_state.db (отдельно от state.db)

Даёт:
  - Обкатку ML-моделей без риска
  - A/B-тестирование с paper-позициями
  - RPC: /paper/enter, /paper/close, /paper/positions, /paper/balance
"""
import logging
import os
from typing import Dict, Optional

logger = logging.getLogger('bybit.paper')

PAPER_ENABLED = os.environ.get('BYBIT_PAPER_ENABLED', '0') == '1'
_paper_exchange = None


def is_paper_enabled() -> bool:
    """Активен ли paper-режим."""
    return PAPER_ENABLED


def get_paper_exchange():
    """Ленивая инициализация PaperExchange."""
    global _paper_exchange
    if not PAPER_ENABLED:
        return None
    if _paper_exchange is None:
        from paper_api import PaperExchange
        _paper_exchange = PaperExchange()
        logger.info('📝 Paper Trading ENABLED — все сделки симулируются')
    return _paper_exchange


def paper_positions_snapshot():
    """Снапшот paper-позиций в формате, совместимом с основным циклом."""
    px = get_paper_exchange()
    if not px:
        return {}, {}
    return px.fetch_positions(), px.fetch_orders()


def paper_update_mark_prices(price_map: Dict[str, float]):
    """Обновить mark-цены для paper-позиций."""
    px = get_paper_exchange()
    if px:
        px.update_mark_prices(price_map)


def paper_get_balance() -> float:
    """Получить paper-баланс."""
    px = get_paper_exchange()
    return px.get_balance() if px else 0.0


def paper_get_summary() -> dict:
    """Получить сводку paper-торговли."""
    px = get_paper_exchange()
    if not px:
        return {'enabled': False}
    pnl = px.get_pnl_summary()
    positions = px.fetch_positions()
    upnl = sum(p.get('upnl', 0) for p in positions.values())
    return {
        'enabled': True,
        'balance': px.get_balance(),
        'positions': len(positions),
        'total_pnl': pnl['total_pnl'],
        'total_fees': pnl['total_fees'],
        'trades': pnl['trades'],
        'upnl': upnl,
    }
