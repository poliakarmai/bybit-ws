"""
heavy_cycle_opt.py — Оптимизированный тяжёлый цикл (Фаза 7: <30с).

Стратегия:
1. ВСЕ REST-запросы — параллельно через asyncio.gather
2. Единый вызов fetch_open_orders (кеш на 60с)
3. BB prefetch в одном gather с остальными задачами
4. Последовательные только: auto_entry (зависит от корреляций)
"""

import asyncio
import os
import time
from functools import partial
from datetime import datetime

from .alerts import log_event, add_alert, send_telegram_alert
from .push_notifier import send_critical_alert, send_high_alert


# ── Кеш для fetch_open_orders ──
_open_orders_cache = {'data': None, 'ts': 0.0, 'ttl': 60.0}


async def _cached_fetch_open_orders():
    """fetch_open_orders с кешем 60с — чтобы не дёргать REST дважды за цикл."""
    now = time.time()
    if _open_orders_cache['data'] is not None and (now - _open_orders_cache['ts']) < _open_orders_cache['ttl']:
        return _open_orders_cache['data']

    loop = asyncio.get_event_loop()
    from .api import fetch_open_orders
    try:
        orders = await loop.run_in_executor(None, fetch_open_orders)
        _open_orders_cache['data'] = orders
        _open_orders_cache['ts'] = now
        return orders
    except Exception:
        return _open_orders_cache['data'] or {}


async def heavy_cycle_async(cfg, positions, orders, cycle_count):
    """Оптимизированный тяжёлый цикл: все REST-запросы параллельно."""
    HEAVY_CYCLE = cfg.monitor.heavy_cycle
    if cycle_count % HEAVY_CYCLE != 0:
        return

    t0 = time.time()
    log_event(f'⚡ heavy cycle #{cycle_count} start [optimized]')

    from .rpc import rpc_state

    # ═══════════════════════════════════════════════════
    # ФАЗА 1: Параллельный запуск ВСЕХ независимых задач
    # ═══════════════════════════════════════════════════
    loop = asyncio.get_event_loop()

    tasks = {}  # name → coroutine

    # ── BB prefetch (был последовательным — теперь параллельно) ──
    async def _bb_prefetch():
        from .bb_prefetch import prefetch_bb_for_all
        tracked = list(positions.keys()) if positions else []
        if tracked:
            n = prefetch_bb_for_all(tracked)
            return f'BB: {n}/{len(tracked)}'
        return None

    tasks['bb_prefetch'] = _bb_prefetch()

    # ── Режим рынка ──
    async def _regime_check():
        from .regime import check_regime
        return await loop.run_in_executor(None, check_regime)

    tasks['regime'] = _regime_check()

    # ── LSTM Regime Auto (BYBIT_REGIME_AUTO=1) ──
    async def _regime_auto():
        if os.environ.get('BYBIT_REGIME_AUTO', '0') != '1':
            return None
        from .lstm_regime import get_current_regime_strategy
        return get_current_regime_strategy()

    tasks['regime_auto'] = _regime_auto()

    # ── Auto-SHORT ──
    if not rpc_state.get("paused"):
        async def _auto_short():
            from .auto_short import check_auto_short
            return await loop.run_in_executor(None, check_auto_short, positions or {})

        tasks['auto_short'] = _auto_short()

    # ── Корреляции ──
    async def _correlation():
        from .correlation import check_correlation
        return await loop.run_in_executor(None, check_correlation, positions)

    tasks['correlation'] = _correlation()

    # ── Overbought ──
    if positions:
        async def _overbought():
            from .overbought import check_overbought
            return await loop.run_in_executor(None, check_overbought, positions)

        tasks['overbought'] = _overbought()

    # ── Пампы ──
    if positions:
        async def _pumps():
            from .pump_detect import check_pumps
            return await loop.run_in_executor(None, check_pumps, positions)

        tasks['pumps'] = _pumps()

        async def _weekly_pumps():
            from .pump_detect import check_weekly_pumps
            return await loop.run_in_executor(None, check_weekly_pumps)

        tasks['weekly_pumps'] = _weekly_pumps()

    # ── DCA ──
    if not rpc_state.get("paused"):
        async def _dca():
            from .dca import check_dca
            return await loop.run_in_executor(None, check_dca)

        tasks['dca'] = _dca()

    # ── Partial TP (каждые 4 цикла) ──
    if cycle_count % 4 == 0:
        async def _partial_tp():
            from .partial_tp import check_partial_tp
            return await loop.run_in_executor(None, check_partial_tp)

        tasks['partial_tp'] = _partial_tp()

    # ── Единый fetch_open_orders (для auto_tp + self-check) ──
    tasks['fetch_orders'] = _cached_fetch_open_orders()

    # ── Time exit ──
    if positions:
        async def _time_exit():
            from .time_exit import check_time_exit
            return await loop.run_in_executor(None, check_time_exit, positions, orders)

        tasks['time_exit'] = _time_exit()

    # ═══════════════════════════════════════════════════
    # ЗАПУСК ВСЕГО ПАРАЛЛЕЛЬНО
    # ═══════════════════════════════════════════════════
    task_names = list(tasks.keys())
    task_coros = list(tasks.values())
    raw_results = await asyncio.gather(*task_coros, return_exceptions=True)

    results = {}
    for name, item in zip(task_names, raw_results):
        if isinstance(item, Exception):
            log_event(f'⚠️ heavy task [{name}] failed: {item}')
            results[name] = None
        else:
            results[name] = item

    # ═══════════════════════════════════════════════════
    # ФАЗА 2: Обработка результатов
    # ═══════════════════════════════════════════════════

    # ── BB prefetch log ──
    if results.get('bb_prefetch'):
        log_event(f'📊 {results["bb_prefetch"]}')

    # ── Regime Auto: переключение LONG/SHORT ──
    regime_auto_result = results.get('regime_auto')
    if regime_auto_result and cycle_count % 10 == 0:
        try:
            from . import __init__ as _pkg
            _pkg.REGIME_LONG_ENABLED = regime_auto_result['LONG_ENABLED']
            _pkg.REGIME_SHORT_ENABLED = regime_auto_result['SHORT_ENABLED']
            long_icon = 'OK' if regime_auto_result['LONG_ENABLED'] else 'OFF'
            short_icon = 'OK' if regime_auto_result['SHORT_ENABLED'] else 'OFF'
            log_event(
                f'REGIME_AUTO: regime={regime_auto_result["regime"]} '
                f'conf={regime_auto_result["confidence"]}% '
                f'LONG={long_icon} SHORT={short_icon}'
            )
        except Exception as e:
            log_event(f'REGIME_AUTO error: {e}')

    # ── Overbought alerts ──
    overbought_result = results.get('overbought')
    if overbought_result:
        overbought_msgs, _ = overbought_result if isinstance(overbought_result, tuple) else (overbought_result, None)
        for msg in (overbought_msgs or []):
            add_alert('INFO', msg)

    # ── Pump alerts ──
    pumps_result = results.get('pumps')
    if pumps_result:
        pump_msgs, _ = pumps_result if isinstance(pumps_result, tuple) else (pumps_result, None)
        for msg in (pump_msgs or []):
            add_alert('STOP', msg)
            send_critical_alert(msg)

    weekly_result = results.get('weekly_pumps')
    if weekly_result:
        weekly_msgs, _ = weekly_result if isinstance(weekly_result, tuple) else (weekly_result, None)
        for msg in (weekly_msgs or []):
            add_alert('STOP', msg)
            send_critical_alert(msg)

    # ── DCA alerts ──
    dca_result = results.get('dca')
    if dca_result:
        dca_msgs = dca_result[0] if isinstance(dca_result, tuple) else dca_result
        for msg in (dca_msgs or []):
            add_alert('ENTRY', msg)
            send_high_alert(msg, level='ENTRY')

    # ── Partial TP alerts ──
    ptp_result = results.get('partial_tp')
    if ptp_result:
        ptp_msgs = ptp_result[0] if isinstance(ptp_result, tuple) else ptp_result
        for msg in (ptp_msgs or []):
            add_alert('TP', msg)
            send_high_alert(msg, level='TP')

    # ── Auto-TP (использует единый fetch_open_orders) ──
    real_orders = results.get('fetch_orders') or orders or {}
    try:
        from .auto_tp import auto_take_profit, apply_auto_tp
        tp_actions = auto_take_profit(positions or {}, real_orders)
        if tp_actions:
            apply_auto_tp(tp_actions)
    except Exception as e:
        log_event(f'⚠️ auto_tp error: {e}')

    # ── TP/SL Self-Check (использует тот же real_orders) ──
    if positions:
        try:
            missing = []
            for sym, p in positions.items():
                side = p.get('side', 'Buy')
                tp_side = 'Sell' if side == 'Buy' else 'Buy'

                has_sl = any(
                    o.get('symbol') == sym
                    and o.get('stopOrderType') == 'StopLoss'
                    and o.get('orderStatus') in ('New', 'Untriggered')
                    for o in real_orders
                )

                has_tp = any(
                    o.get('symbol') == sym
                    and o.get('side') == tp_side
                    and o.get('orderType') == 'Limit'
                    and o.get('orderStatus') in ('New', 'Untriggered')
                    for o in real_orders
                )

                if not has_sl or not has_tp:
                    no_sl = "NO SL" if not has_sl else ""
                    no_tp = " NO TP" if not has_tp else ""
                    missing.append(f'{sym}({no_sl}{no_tp})')

            if missing:
                log_event(f'🔴 TP/SL ALERT: {", ".join(missing)}')
        except Exception as e:
            log_event(f'⚠️ TP/SL check error: {e}')

    # ── Time exit ──
    time_result = results.get('time_exit')
    if time_result:
        stale = time_result
        if stale:
            try:
                from .time_exit import apply_time_exits
                result = apply_time_exits(stale)
                if result['closed'] > 0:
                    log_event(f'⏰ Time exit: {result["closed"]} позиций закрыто')
            except Exception as e:
                log_event(f'⚠️ time_exit apply error: {e}')

    # ═══════════════════════════════════════════════════
    # ФАЗА 3: Auto-Entry (после корреляций — нужны их данные)
    # ═══════════════════════════════════════════════════
    try:
        from .auto_entry import auto_entry_scan
        from .risk_manager import check as risk_check
        import re

        entries = await loop.run_in_executor(None, auto_entry_scan, positions or {})
        entry_msgs = entries[0] if isinstance(entries, tuple) else entries
        for entry in (entry_msgs or []):
            sym_match = re.search(r'\b([A-Z]+USDT)\b', entry)
            if sym_match:
                sym = sym_match.group(1)
                side = 'Sell' if 'SHORT' in entry else 'Buy'
                entry_allowed, risk_reason = risk_check(positions or {}, new_symbol=sym, new_side=side)
                if not entry_allowed:
                    log_event(f'🛑 Risk blocked auto-entry {sym}: {risk_reason}')
                    continue
            add_alert('ENTRY', entry)
            send_telegram_alert(entry)
    except Exception as e:
        log_event(f'⚠️ auto_entry error: {e}')

    # ═══════════════════════════════════════════════════
    # ФАЗА 4: Суточные задачи (если кратно 2880 = 24ч)
    # ═══════════════════════════════════════════════════
    if cycle_count > 0 and cycle_count % 2880 == 0:
        # Post-trade cluster analysis + Self-learning (параллельно)
        async def _post_trade():
            from .post_trade import analyze_clusters
            return analyze_clusters()

        async def _self_learn():
            from .journal.adapter import load_from_sqlite
            from .journal.self_learn import apply_journal_insights
            journal = load_from_sqlite()
            if not journal or 'error' in journal:
                return None
            return apply_journal_insights(journal, cfg)

        daily_tasks = await asyncio.gather(
            _post_trade(),
            _self_learn(),
            return_exceptions=True,
        )

        cluster_result_raw = daily_tasks[0]
        if not isinstance(cluster_result_raw, Exception) and cluster_result_raw:
            try:
                if cluster_result_raw.get('blocked'):
                    log_event(f'🔬 CLUSTER BLOCK: {len(cluster_result_raw["blocked"])} кластеров заблокировано')
                    for b in cluster_result_raw['blocked']:
                        log_event(
                            f'  🚫 {b["cluster"]}: WR={b["win_rate"]:.0%} '
                            f'({b["trades"]} сделок, PnL=${b["pnl"]:.2f})'
                        )
            except Exception:
                log_event(f'⚠️ post_trade log error')

        adjustments_raw = daily_tasks[1]
        if not isinstance(adjustments_raw, Exception) and adjustments_raw:
            try:
                adjustments = adjustments_raw
                log_event(f'🧠 Self-learning: {len(adjustments)} корректировок')
                for adj in adjustments[:5]:
                    log_event(
                        f'  📐 {adj.get("param", "?")}: '
                        f'{adj.get("old", "?")} → {adj.get("new", "?")}'
                    )
            except Exception:
                log_event(f'⚠️ self_learn log error')

    elapsed = time.time() - t0
    log_event(f'⚡ heavy cycle #{cycle_count} done in {elapsed:.2f}s [optimized]')
