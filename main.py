"""Главный цикл монитора и точка входа."""
import os, sys, time, signal
from datetime import datetime
from . import (DATA_DIR, EVENTS_LOG, ALERTS_LOG, POSITIONS_SNAPSHOT, ORDERS_SNAPSHOT,
               ORDERS_METADATA, BYBIT_CLI, HERMES_BIN, WATCHDOG_LAST, SHUTDOWN_REQUESTED,
               COVERAGE_CHECK_INTERVAL, TRAIL_CHECK_INTERVAL, METRICS_FILE)

# Health-check: файл с timestamp последнего успешного цикла
HEALTH_FILE = os.path.join(DATA_DIR, 'health.txt')
from .api import fetch_positions, fetch_orders
from .snapshot import load_json, save_json, check_position_changes, check_order_changes
from .alerts import log_event, add_alert, get_alerts, send_telegram_alert, _is_duplicate
from .auto_tp import auto_take_profit, apply_auto_tp
from .trailing_sl import trailing_sl, apply_trailing_sl
from .overbought import check_overbought, rotate_watchlist
from .pump_detect import check_pumps
from .auto_entry import auto_entry_scan
from .health import (check_liquidation, check_bb_squeeze, check_funding_flip,
                      check_daily_drawdown, check_correlation_risk, check_funding_pump)
from .rsi import check_rsi_divergence
from .squeeze import check_squeeze
from .auto_sl import check_and_fix_sl
from .dca import check_dca
from .cleanup import check_expired_orders, apply_cancel_expired, clean_stale_orders
from .reporting import (should_send_summary, send_summary, check_profit_triggers,
                         log_trade, check_strategy_compliance, check_coverage_summary)
from .metrics import record_alert, record_auto_entry
from .recycle import handle_tp_recycle, apply_recycle
from .cost_tracker import check_cycle as cost_tracker_check
from .rpc import start_rpc_server, update_health as rpc_update_health
from .sl_reentry import notify_sl_hit, check_sl_reentry
from .auto_short import check_auto_short


def main_loop():
    print(f"🔄 Bybit WS Monitor v2.6 запущен")
    print(f"   Лог: {EVENTS_LOG}")
    print(f"   Алерты: {ALERTS_LOG}")
    print(f"   Проверка каждые 30 секунд")
    print(f"   Нажми Ctrl+C для остановки")

    # Инициализация watchlist-ротации
    rotate_watchlist()

    old_positions = fetch_positions()
    old_orders = fetch_orders()

    if old_positions:
        save_json(POSITIONS_SNAPSHOT, old_positions)
    if old_orders:
        save_json(ORDERS_SNAPSHOT, old_orders)
        meta = load_json(ORDERS_METADATA)
        now = time.time()
        for key, o in old_orders.items():
            if key not in meta and o['kind'] == 'LIMIT_ENTRY':
                meta[key] = {'first_seen': now}
        save_json(ORDERS_METADATA, meta)

    log_event(f'Монитор запущен: {len(old_positions)} позиций, {len(old_orders)} ордеров')

    # Запуск RPC-сервера (порт 8766)
    try:
        rpc_server = start_rpc_server(8766)
        log_event(f'🌐 RPC-сервер: http://0.0.0.0:8766')
    except Exception as e:
        log_event(f'⚠️ RPC-сервер не запустился: {e}')

    global WATCHDOG_LAST
    WATCHDOG_LAST = time.time()
    cycle_count = 0
    first_cycle = True

    while True:
        try:
            # Быстрая остановка: проверяем SHUTDOWN_REQUESTED до sleep
            if SHUTDOWN_REQUESTED:
                log_event('Shutdown: сохраняю снепшоты')
                if old_positions:
                    save_json(POSITIONS_SNAPSHOT, old_positions)
                if old_orders:
                    save_json(ORDERS_SNAPSHOT, old_orders)
                log_event(f'Монитор остановлен (graceful)')
                sys.exit(0)

            time.sleep(30)
            now_wd = time.time()
            if now_wd - WATCHDOG_LAST > 180:
                log_event(f'🚨 Watchdog: главный цикл завис ({now_wd - WATCHDOG_LAST:.0f}с) — аварийный выход')
                os._exit(1)
            WATCHDOG_LAST = now_wd
            cycle_count += 1
            now_ts = time.time()

            # Health-check: timestamp последнего успешного цикла
            with open(HEALTH_FILE, 'w') as hf:
                hf.write(str(now_ts))

            # RPC health update
            rpc_update_health(alive=True, cycle_count=cycle_count)

            new_positions = fetch_positions()
            new_orders = fetch_orders()

            pos_changes = check_position_changes(old_positions, new_positions)
            ord_changes = check_order_changes(old_orders, new_orders)

            closed_syms = {sym for ct, sym, _ in pos_changes if ct == 'CLOSED'}
            reduced_syms = {sym for ct, sym, _ in pos_changes if ct == 'REDUCE'}
            sl_hit_syms = {sym for ct, sym, _ in ord_changes if ct == 'SL_HIT'}
            tp_hit_syms = {sym for ct, sym, _ in ord_changes if ct == 'TP_HIT'}

            if first_cycle:
                # Первый цикл после старта: только реальные изменения позиций (без ордерных алертов)
                # SL_HIT/TP_HIT/ENTRY_HIT на первом цикле — ложные (расcинхрон снапшотов после рестарта)
                for change_type, sym, msg in pos_changes + ord_changes:
                    if change_type in ('CLOSED', 'NEW', 'ADD', 'REDUCE'):
                        add_alert('INFO', msg)
                first_cycle = False
            else:
                for change_type, sym, msg in pos_changes + ord_changes:
                    if change_type == 'CLOSED' and sym in sl_hit_syms:
                        add_alert('STOP', f'🛑 {msg}')
                        record_alert('SL')
                        # SL re-entry: запомнить для лесенки
                        old_pos = old_positions.get(sym, {})
                        sl_price = old_pos.get('mark', 0)  # марка на момент SL ≈ цена SL
                        entry = old_pos.get('entry', 0)
                        notify_sl_hit(sym, sl_price, entry)
                        continue
                    if change_type == 'CLOSED' and sym in tp_hit_syms:
                        add_alert('TP', f'🎯 {msg}')
                        record_alert('TP')
                        continue
                    if change_type in ('SL_HIT', 'TP_HIT') and sym in closed_syms:
                        continue
                    if change_type == 'SL_HIT' and sym in new_positions:
                        continue
                    if change_type == 'TP_HIT' and sym in new_positions and sym not in reduced_syms:
                        continue
                    if change_type == 'TP_HIT':
                        add_alert('TP', f'🎯 {msg}')
                        record_alert('TP', is_false=(sym in new_positions and sym not in reduced_syms))
                    elif change_type == 'SL_HIT':
                        add_alert('STOP', f'🛑 {msg}')
                        record_alert('SL', is_false=(sym in new_positions))
                    elif change_type == 'ENTRY_HIT':
                        add_alert('ENTRY', f'📌 {msg}')
                        record_alert('ENTRY')
                    elif change_type == 'CLOSED':
                        add_alert('INFO', msg)
                    elif change_type == 'NEW':
                        add_alert('INFO', f'📈 {msg}')

            # Аудит стратегии
            has_pos_changes = any(ct in ('CLOSED', 'NEW', 'ADD', 'REDUCE') for ct, _, _ in pos_changes)
            if has_pos_changes and new_positions and new_orders:
                for msg in check_strategy_compliance(new_positions, new_orders):
                    add_alert('INFO', msg)

            # Trailing SL
            if cycle_count % TRAIL_CHECK_INTERVAL == 0 and new_positions:
                trail_actions = trailing_sl(new_positions)
                if trail_actions:
                    apply_trailing_sl(trail_actions)
                    for sym, idx, side, size, price in trail_actions:
                        add_alert('INFO', f'🔺 Trailing SL {sym} подтянут до ${price:.4f}')

            # Агрессивный авто-SL: каждые 4 цикла (2 мин)
            if cycle_count % 4 == 0 and new_positions:
                sl_alerts = check_and_fix_sl()
                for msg in sl_alerts:
                    if msg.startswith('🛡'):
                        add_alert('INFO', msg)
                    elif msg.startswith('⚠️'):
                        add_alert('STOP', msg)
            if cycle_count % 2 == 0 and new_orders:
                expired = check_expired_orders(new_orders, old_orders, now_ts)
                if expired:
                    apply_cancel_expired(expired)

            # Auto-TP
            if cycle_count % TRAIL_CHECK_INTERVAL == 0 and new_positions:
                recent_hit_syms = sl_hit_syms | tp_hit_syms
                tp_actions = auto_take_profit(new_positions, new_orders, skip_syms=recent_hit_syms)
                if tp_actions:
                    apply_auto_tp(tp_actions)
                    for sym, idx, side, qty, price, pos_size in tp_actions:
                        add_alert('INFO', f'🎯 Auto-TP {sym}: @ ${price:.4f}')

            # Ликвидация + просадка
            if new_positions:
                for msg in check_liquidation(new_positions):
                    add_alert('STOP', msg)
                if cycle_count % 10 == 0:
                    dd_msg = check_daily_drawdown(new_positions)
                    if dd_msg:
                        add_alert('STOP', dd_msg)

            # Профит-триггеры
            if new_positions:
                for msg in check_profit_triggers(new_positions):
                    add_alert('TP', msg)

            # Трейд-журнал
            for change_type, sym, msg in pos_changes:
                if change_type == 'CLOSED':
                    p = old_positions.get(sym, {})
                    entry = p.get('entry', 0)
                    mark = p.get('mark', 0)
                    pnl = p.get('upnl', 0)
                    side = p.get('side', 'Buy')
                    reason = 'закрыта'
                    alert_ref = ''
                    # Приоритет: SL важнее TP (если позиция закрыта по стопу, а TP просто отменился)
                    for ct2, sym2, msg2 in ord_changes:
                        if sym2 == sym:
                            if ct2 == 'SL_HIT':
                                reason = 'SL'
                                alert_ref = 'SL_HIT'
                                break  # SL — окончательный вердикт
                            elif ct2 == 'TP_HIT' and reason != 'SL':
                                reason = 'TP'
                                alert_ref = 'TP_HIT'
                    log_trade(sym, entry, mark, pnl, side, reason, alert_ref)

            # Очистка DCA-стейта для закрытых позиций
            if closed_syms:
                import json as _json
                dca_file = os.path.join(DATA_DIR, 'dca_state.json')
                if os.path.exists(dca_file):
                    try:
                        with open(dca_file) as f:
                            dca_state = _json.load(f)
                        for sym in closed_syms:
                            if sym in dca_state:
                                del dca_state[sym]
                        with open(dca_file, 'w') as f:
                            _json.dump(dca_state, f, indent=2)
                    except:
                        pass
            if reduced_syms and new_positions and new_orders:
                recycle_actions = handle_tp_recycle(reduced_syms, new_positions, new_orders)
                if recycle_actions:
                    apply_recycle(recycle_actions)

            # Блок каждые 10 циклов (5 мин)
            if cycle_count % 10 == 0:
                # Защита от перегруза: если цикл уже >90с, пропускаем тяжёлые проверки
                cycle_elapsed = time.time() - now_ts
                heavy_ok = cycle_elapsed < 90
                if not heavy_ok:
                    log_event(f'⏭️ Цикл перегружен ({cycle_elapsed:.0f}с) — тяжёлые проверки пропущены')
                
                # overbought только если есть позиции (хеждирование) и цикл не перегружен
                if heavy_ok and new_positions:
                    for msg in check_overbought(new_positions):
                        add_alert('INFO', msg)
                if heavy_ok:
                    for msg in check_pumps(new_positions or {}):
                        add_alert('STOP', msg)
                    for msg in check_rsi_divergence():
                        add_alert('STOP', msg)
                    for msg in check_squeeze():
                        add_alert('INFO', msg)
                    for msg in check_funding_pump():
                        add_alert('STOP', msg)
                for msg in check_dca():
                    add_alert('ENTRY', msg)
                # Auto-SHORT: перегретые монеты (BB > 85%)
                if heavy_ok:
                    for msg in check_auto_short(new_positions or {}):
                        pass  # алерты уже внутри
                for msg in check_bb_squeeze():
                    add_alert('INFO', msg)
                if new_orders and new_positions is not None:
                    for msg in clean_stale_orders(new_positions, new_orders):
                        add_alert('INFO', msg)
                if heavy_ok:
                    for msg in check_funding_flip():
                        add_alert('INFO', msg)
                corr_msg = check_correlation_risk(new_positions)
                if corr_msg:
                    add_alert('INFO', corr_msg)

            # Сводка TP/SL покрытия раз в 4 часа
            if cycle_count % COVERAGE_CHECK_INTERVAL == 0 and new_positions and new_orders:
                cov_msg = check_coverage_summary(new_positions, new_orders)
                if cov_msg:
                    send_telegram_alert(cov_msg)
                    add_alert('INFO', '🛡 Отправлена сводка TP/SL покрытия')

            # Cost tracking: логирование комиссий (раз в час, с внутренним троттлингом)
            cost_tracker_check()

            # Корреляция → блок авто-входа
            correlation_stop = False
            if new_positions and len(new_positions) >= 5:
                longs = sum(1 for p in new_positions.values() if p['side'] == 'Buy')
                if longs / len(new_positions) * 100 > 80:
                    correlation_stop = True
                    # Rate-limit: dedup по STOP (10 мин кулдаун) — лёгкая проверка без API
                    if not _is_duplicate(f'Correlation {int(longs/len(new_positions)*100)}% LONG', 'STOP'):
                        log_event(f'🛑 Корреляция {longs/len(new_positions)*100:.0f}% LONG — авто-вход заблокирован')
                        log_event(f'🔄 SHORT-кандидаты проверяются в основном блоке (каждые 10 циклов)')

            # Авто-вход каждые 60 циклов (30 мин)
            if cycle_count % 60 == 0 and new_positions is not None:
                if correlation_stop:
                    log_event('🛑 Авто-вход заблокирован: корреляция LONG >80%')
                else:
                    auto_entries = auto_entry_scan(new_positions)
                    for msg in auto_entries:
                        add_alert('ENTRY', msg)
                        send_telegram_alert(msg)
                        record_auto_entry(placed=True)

            # SL re-entry: лесенка после стоп-лосса (каждые 10 циклов = 5 мин)
            if cycle_count % 10 == 0:
                check_sl_reentry(new_positions or {}, correlation_stop)

            # Сводка 09:00 и 21:00
            label = should_send_summary()
            if label:
                send_summary(label)

            # Сохраняем снепшоты (всегда, даже если пустые — иначе спам алертов)
            save_json(POSITIONS_SNAPSHOT, new_positions or {})
            old_positions = new_positions or {}
            save_json(ORDERS_SNAPSHOT, new_orders or {})
            old_orders = new_orders or {}

            # Статус каждые 5 мин
            if cycle_count % 10 == 0:
                alerts = get_alerts()
                if alerts:
                    print(f"{'='*60}")
                    print(f"⚠️ АЛЕРТЫ ({len(alerts)}):")
                    for a in alerts:
                        print(f"  {a}")
                    print(f"{'='*60}")
                else:
                    pos_count = len(new_positions) if new_positions else 0
                    ord_count = len(new_orders) if new_orders else 0
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] {pos_count} поз, {ord_count} орд, всё штатно")

            # Алерты для bybit-monitor
            if cycle_count % 2 == 0:
                alerts = get_alerts()
                if alerts:
                    with open(os.path.join(DATA_DIR, 'new_alerts.txt'), 'w') as f:
                        for a in alerts:
                            f.write(a + '\n')
                else:
                    alert_file = os.path.join(DATA_DIR, 'new_alerts.txt')
                    if os.path.exists(alert_file):
                        os.remove(alert_file)

            if SHUTDOWN_REQUESTED:
                log_event('Shutdown: сохраняю снепшоты')
                if new_positions:
                    save_json(POSITIONS_SNAPSHOT, new_positions)
                if new_orders:
                    save_json(ORDERS_SNAPSHOT, new_orders)
                log_event(f'Монитор остановлен (graceful) — {len(new_positions) if new_positions else 0} позиций, {len(new_orders) if new_orders else 0} ордеров')
                sys.exit(0)

        except KeyboardInterrupt:
            print("\n⏹ Монитор остановлен")
            log_event('Монитор остановлен')
            break
        except Exception as e:
            log_event(f'Ошибка в цикле: {e}')
            time.sleep(5)
            continue


def handle_sigterm(signum, frame):
    global SHUTDOWN_REQUESTED
    from . import SHUTDOWN_REQUESTED as s
    log_event('Получен SIGTERM — graceful shutdown')
    import bybit_ws
    bybit_ws.SHUTDOWN_REQUESTED = True
    # Also update local
    globals()['SHUTDOWN_REQUESTED'] = True

signal.signal(signal.SIGTERM, handle_sigterm)


def run_once():
    """Один проход — для --once."""
    now_ts = time.time()
    new_positions = fetch_positions()
    new_orders = fetch_orders()
    old_positions = load_json(POSITIONS_SNAPSHOT)
    old_orders = load_json(ORDERS_SNAPSHOT)

    pos_changes = check_position_changes(old_positions, new_positions)
    ord_changes = check_order_changes(old_orders, new_orders)

    alerts = []
    closed_syms_once = {sym for ct, sym, _ in pos_changes if ct == 'CLOSED'}
    reduced_syms_once = {sym for ct, sym, _ in pos_changes if ct == 'REDUCE'}
    for ct, sym, msg in pos_changes + ord_changes:
        if ct == 'SL_HIT' and sym in new_positions:
            continue
        if ct == 'TP_HIT' and sym in new_positions and sym not in reduced_syms_once:
            continue
        if ct in ('SL_HIT', 'TP_HIT') and sym in closed_syms_once:
            continue
        print(msg)
        alerts.append(msg)

    if new_positions:
        trail_actions = trailing_sl(new_positions)
        if trail_actions:
            apply_trailing_sl(trail_actions)
            for sym, idx, side, size, price in trail_actions:
                print(f'🔺 {sym}: trailing SL → ${price:.4f}')
                alerts.append(f'Trailing SL {sym} @ ${price:.4f}')
        tp_actions = auto_take_profit(new_positions, new_orders)
        if tp_actions:
            apply_auto_tp(tp_actions)
            for sym, idx, side, qty, price, pos_size in tp_actions:
                print(f'🎯 {sym}: TP @ ${price:.4f}')
                alerts.append(f'Auto-TP {sym} @ ${price:.4f}')

    if new_positions:
        save_json(POSITIONS_SNAPSHOT, new_positions)
    if new_orders:
        save_json(ORDERS_SNAPSHOT, new_orders)

    if not alerts:
        print("Изменений нет")


# CLI: stats, add, drop
def run_cli():
    if '--once' in sys.argv:
        run_once()
        sys.exit(0)
    elif len(sys.argv) >= 3 and sys.argv[1] == 'add':
        sym = sys.argv[2].upper()
        if not sym.endswith('USDT'):
            sym += 'USDT'
        wl_file = os.path.join(DATA_DIR, 'watchlist_custom.txt')
        current = []
        if os.path.exists(wl_file):
            with open(wl_file) as f:
                current = [l.strip() for l in f if l.strip()]
        if sym not in current:
            current.append(sym)
            with open(wl_file, 'w') as f:
                f.write('\n'.join(current) + '\n')
            print(f'✅ {sym} добавлен в watchlist')
        else:
            print(f'ℹ️ {sym} уже в watchlist')
        sys.exit(0)
    elif len(sys.argv) >= 3 and sys.argv[1] == 'drop':
        sym = sys.argv[2].upper()
        if not sym.endswith('USDT'):
            sym += 'USDT'
        wl_file = os.path.join(DATA_DIR, 'watchlist_custom.txt')
        if os.path.exists(wl_file):
            with open(wl_file) as f:
                current = [l.strip() for l in f if l.strip()]
            if sym in current:
                current.remove(sym)
                with open(wl_file, 'w') as f:
                    f.write('\n'.join(current) + '\n')
                print(f'🗑️ {sym} удалён из watchlist')
            else:
                print(f'ℹ️ {sym} не найден в watchlist')
        else:
            print('ℹ️ Watchlist пуст')
        sys.exit(0)
    elif len(sys.argv) >= 2 and sys.argv[1] == 'stats':
        from .metrics import print_metrics
        print_metrics()
        sys.exit(0)

    main_loop()
