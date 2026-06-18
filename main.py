# DEPRECATED: use main_async.py for production (asyncio, цикл 30с)
# Этот файл оставлен для совместимости и будет удалён в следующей версии
import sys; sys.exit('Use main_async.py — синхронный main.py удалён. См. bybit-ws-full.md раздел 6.1')
from . import (DATA_DIR, EVENTS_LOG, ALERTS, ALERTS_LOG, POSITIONS_SNAPSHOT, ORDERS_SNAPSHOT,
               ORDERS_METADATA, BYBIT_CLI, HERMES_BIN, WATCHDOG_LAST, SHUTDOWN_REQUESTED,
               COVERAGE_CHECK_INTERVAL, TRAIL_CHECK_INTERVAL, METRICS_FILE)
from .config import Config

# Health-check: файл с timestamp последнего успешного цикла
HEALTH_FILE = os.path.join(DATA_DIR, 'health.txt')
# Дедупликация корреляционных алертов (24ч TTL, не как _is_duplicate с 5 мин)
CORR_DEDUP_FILE = os.path.join(DATA_DIR, 'corr_dedup.json')

HEAVY_CHECK_TIMEOUT = 25

def _timed_call(fn, *args, timeout=HEAVY_CHECK_TIMEOUT):
    """Вызвать fn(*args) в отдельном потоке с таймаутом. Возвращает (result, None) или ([], error_name)."""
    result = []
    def _target():
        try:
            result.append(fn(*args))
        except Exception as e:
            result.append(e)
    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return [], fn.__name__
    if result and isinstance(result[0], Exception):
        log_event(f'_timed_call {fn.__name__} error: {result[0]}')
        return [], f'{fn.__name__}({result[0]})'
    return result[0] if result else [], None
from .api import fetch_positions, fetch_orders, bybit
from .snapshot import load_json, save_json, check_position_changes, check_order_changes
from .alerts import log_event, add_alert, get_alerts, send_telegram_alert, _is_duplicate
from .auto_tp import auto_take_profit, apply_auto_tp
from .trailing_sl import trailing_sl, trailing_sl_x10, apply_trailing_sl
from .junk_trail import trailing_junk_tp
from .funding_rotation import check_funding_rotation, execute_rotation
from .overbought import check_overbought, rotate_watchlist
from .pump_detect import check_pumps, check_weekly_pumps
from .file_utils import safe_json_write, locked_open
from .auto_entry import auto_entry_scan, record_sl_hit
from .health import (check_liquidation, check_bb_squeeze, check_funding_flip,
                      check_daily_drawdown, check_funding_pump)
from .correlation import check_correlation
from .rsi import check_rsi_divergence
from .squeeze import check_squeeze
from .auto_sl import check_and_fix_sl, check_breakeven_sl
from .dca import check_dca
from .cleanup import check_expired_orders, apply_cancel_expired, clean_stale_orders
from .reporting import (should_send_summary, send_summary, check_profit_triggers,
                         log_trade, check_strategy_compliance, check_coverage_summary,
                         check_daily_pnl_alert)
from .metrics import record_alert, record_auto_entry
from .recycle import handle_tp_recycle, apply_recycle
from .cost_tracker import check_cycle as cost_tracker_check
from .funding_tracker import check_cycle as funding_tracker_check
from .margin_alerts import check_margin_utilization, get_margin_stats
from .rpc import start_rpc_server, update_health as rpc_update_health, rpc_state
from .sl_reentry import notify_sl_hit, check_sl_reentry
from .state_db import db  # SQLite trade_history
from .auto_short import check_auto_short, check_junk_dca
from .regime import check_regime
from .correlation import check_correlation, load_correlation_snapshot, tighten_correlation_sl
from .bb_scalp import check_scalp_signals, execute_scalp
from .mean_revert import check_mean_revert, execute_mean_revert
from .funding_entry import check_funding_signals, execute_funding_entry
from .atr_sizer import check_position_risk, validate_entry
from .x10_limits import record_x10_trade, x10_entry_allowed, get_x10_stats, track_x10_entry, get_x10_strategy, clear_x10_position
from .manual_positions import is_manual_position, mark_manual_position
from .partial_tp import check_partial_tp  # Phase 3

# Дедупликация STOP-алертов: кулдаун 5 минут на символ
# Предотвращает спам от hedge SL orderId race (orderId меняется каждый цикл)
_SL_DEDUP = {}  # symbol -> timestamp последнего STOP-алерта
SL_ALERT_COOLDOWN = 300  # 5 минут


def _check_risk_limits(positions: dict, risk_cfg) -> tuple[bool, list]:
    """Проверить risk-лимиты: max_total_margin, max_daily_loss.
    Возвращает (blocked, alerts) — blocked=True означает блокировку ВСЕХ новых входов.
    """
    blocked = False
    alerts = []
    if not positions:
        return blocked, alerts

    # Суммарная маржа
    total_margin = sum(float(p.get('positionIM', p.get('margin', 0))) for p in positions.values())
    max_margin = risk_cfg.get('max_total_margin', 500)
    if total_margin > max_margin:
        alerts.append(f'🚨 Превышена суммарная маржа: ${total_margin:.0f} > ${max_margin}')
        blocked = True

    # Дневной убыток (через metrics.json)
    metrics_file = os.path.join(DATA_DIR, 'metrics.json')
    daily_loss = 0.0
    try:
        import json as _json
        if os.path.exists(metrics_file):
            with open(metrics_file) as f:
                m = _json.load(f)
            daily_loss = float(m.get('daily_pnl', 0))  # negative = loss
    except Exception as e:
        log_event(f'⚠️ _check_risk_limits error: {e}')
        pass
    max_loss = risk_cfg.get('max_daily_loss', 50)
    if daily_loss < -max_loss:
        alerts.append(f'🚨 Дневной убыток превышен: -${abs(daily_loss):.0f} > -${max_loss}')
        blocked = True

    # Лимит LONG позиций
    long_count = sum(1 for p in positions.values() if p.get('side') == 'Buy')
    max_long = risk_cfg.get('max_long_positions', 12)
    if long_count > max_long:
        alerts.append(f'⚠️ Превышен лимит LONG: {long_count} > {max_long} (entry blocked)')
        blocked = True

    return blocked, alerts


def _close_instant(symbol: str, position: dict) -> bool:
    """Мгновенно закрыть позицию рынком. Возвращает True если ордер отправлен успешно."""
    import subprocess, json
    side = position.get('side', 'Buy')
    close_side = 'Sell' if side == 'Buy' else 'Buy'
    qty = str(int(float(position.get('size', 0))))
    body = json.dumps({
        'category': 'linear', 'symbol': symbol, 'side': close_side,
        'orderType': 'Market', 'qty': qty, 'positionIdx': int(position.get('positionIdx', 0)),
        'timeInForce': 'IOC', 'reduceOnly': True,
    })
    try:
        r = subprocess.run([BYBIT_CLI, 'raw', 'POST', '/v5/order/create', body],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            log_event(f'🚨 CLOSE FAILED {symbol}: {r.stderr.strip()[:200]}')
            return False
        resp = json.loads(r.stdout)
        if resp.get('retCode') != 0:
            ret_msg = resp.get('retMsg', 'unknown')[:200]
            log_event(f'🚨 CLOSE REJECTED {symbol}: {ret_msg}')
            return False
        log_event(f'✅ CLOSE OK {symbol}: {close_side} {qty} @ market')
        # -- Фаза 5.3: запись исхода в A/B тест --
        try:
            from .ab_test import record_outcome_for_symbol
            entry = float(position.get('entry', 0))
            mark = float(position.get('mark', 0))
            size = float(position.get('size', 0))
            pos_pnl = (mark - entry) * size if side == 'Buy' else (entry - mark) * size
            outcome = 'TP' if pos_pnl > 0 else 'SL'
            record_outcome_for_symbol(symbol, outcome, mark, pos_pnl)
        except Exception:
            pass
        return True
    except Exception as e:
        import traceback
        log_event(f'🚨 CLOSE EXCEPTION {symbol}: {e}')
        log_event(f'   traceback: {traceback.format_exc()[-300:]}')
        return False


# ═══════════════════════════════════════════════════════════
# Извлечённые функции главного цикла (рефакторинг #4)
# ═══════════════════════════════════════════════════════════

def _run_heavy_cycle(cfg, new_positions, new_orders, cycle_count, now_ts):
    """Тяжёлые проверки каждые HEAVY_CYCLE циклов (5 мин)."""
    HEAVY_CYCLE = cfg.monitor.heavy_cycle
    if cycle_count % HEAVY_CYCLE != 0:
        return

    cycle_elapsed = time.time() - now_ts
    heavy_ok = cycle_elapsed < 90
    if not heavy_ok:
        log_event(f'⏭️ Цикл перегружен ({cycle_elapsed:.0f}с) — тяжёлые проверки пропущены')

    _a = lambda fn, *a: _timed_call(fn, *a)

    if heavy_ok:
        try:
            regime_result = check_regime(force=True)
            regime_label = regime_result.get("regime", "UNKNOWN")
            regime_conf = regime_result.get("confidence", 0)
            rlog = f'📊 Режим рынка: {regime_label} (conf: {regime_conf}%)'
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {rlog}")
            if cycle_count % (HEAVY_CYCLE * 3) == 0:
                details = regime_result.get("details", {})
                if details:
                    btc_chg = details.get("btc_change_pct", 0)
                    eth_chg = details.get("eth_change_pct", 0)
                    log_event(f'{rlog} | BTC {btc_chg:+.1f}% ETH {eth_chg:+.1f}%')
        except Exception as e:
            log_event(f'⚠️ check_regime failed: {e}')

    if heavy_ok and new_positions:
        msgs, err = _a(check_overbought, new_positions)
        if err: log_event(f'⏱️ check_overbought: таймаут/ошибка — {err}')
        else:
            for msg in msgs: add_alert('INFO', msg)

    if heavy_ok:
        for fn, alert_type in [(check_pumps, 'STOP'), (check_weekly_pumps, 'STOP'),
                                (check_rsi_divergence, 'STOP'),
                                (check_squeeze, 'INFO'), (check_funding_pump, 'STOP')]:
            msgs, err = _a(fn, new_positions if fn == check_pumps else None) if fn in (check_pumps, check_weekly_pumps) else _a(fn)
            if err: log_event(f'⏱️ {err}: таймаут')
            else:
                for msg in (msgs or []): add_alert(alert_type, msg)

    if not rpc_state.get("paused"):
        dca_blocked, _ = _check_risk_limits(new_positions or {}, cfg.risk)
        if not dca_blocked:
            for msg in check_dca():
                add_alert("ENTRY", msg)
        else:
            log_event('🛑 DCA заблокирован: risk-лимит')

        # Phase 3: Partial TP — каждые 4 цикла
        if cycle_count % 4 == 0:
            try:
                ptp_alerts = check_partial_tp()
                for a in ptp_alerts:
                    add_alert("TP", a)
            except Exception as e:
                log_event(f'⚠️ partial_tp error: {e}')

    if heavy_ok and not rpc_state.get("paused"):
        msgs, err = _a(check_auto_short, new_positions or {})
        if err: log_event(f'⏱️ check_auto_short: таймаут — {err}')
        if cfg.strategy.junk.get('enabled', False):
            junk_msgs, junk_err = _a(check_junk_dca, new_positions or {})
            if junk_err:
                log_event(f'⏱️ check_junk_dca: таймаут — {junk_err}')
            else:
                for msg in (junk_msgs or []): add_alert('ENTRY', msg)

    for msg in check_bb_squeeze():
        add_alert('INFO', msg)

    if new_orders and new_positions is not None:
        for msg in clean_stale_orders(new_positions, new_orders):
            add_alert('INFO', msg)

    if heavy_ok:
        msgs, err = _a(check_funding_flip)
        if err: log_event(f'⏱️ check_funding_flip: таймаут — {err}')
        else:
            for msg in (msgs or []): add_alert('INFO', msg)

    corr_result, corr_err = _a(check_correlation, new_positions)
    if corr_err:
        log_event(f'⏱️ check_correlation: таймаут — {corr_err}')
    elif corr_result:
        corr_dedup = load_json(CORR_DEDUP_FILE)
        now_ts2 = time.time()
        for msg in corr_result.get('messages', []):
            pair_match = re.search(r'(\w+↔\w+)', msg)
            pair_key = pair_match.group(1) if pair_match else msg
            pair_hash = hashlib.md5(pair_key.encode()).hexdigest()[:16]
            last = corr_dedup.get(pair_hash, 0)
            if now_ts2 - last > 43200:  # 12 часов
                add_alert('STOP', msg)
                corr_dedup[pair_hash] = now_ts2
        save_json(CORR_DEDUP_FILE, corr_dedup)

        # Ужесточение SL для коррелирующих позиций (MONITOR.md §4)
        if corr_result.get('flagged'):
            sl_alerts = tighten_correlation_sl(
                new_positions, corr_result['flagged'], corr_dedup
            )
            for msg in sl_alerts:
                add_alert('STOP', msg)
            if sl_alerts:
                save_json(CORR_DEDUP_FILE, corr_dedup)

    # ── Фаза 4.3.5: обновление paper-трекинга конфлюенса ──
    if new_positions:
        try:
            from .confluence_paper import check_outcomes
            check_outcomes(new_positions)
        except Exception:
            pass


def _run_x10_cycle(cfg, new_positions, cycle_count, correlation_stop):
    """X10 стратегии каждые HEAVY_CYCLE*2 циклов (10 мин)."""
    HEAVY_CYCLE = cfg.monitor.heavy_cycle
    if cycle_count % (HEAVY_CYCLE * 2) != 0 or new_positions is None:
        return

    x10_ok, x10_reason = x10_entry_allowed(cfg)
    x10_blocked = False
    if not x10_ok:
        if not _is_duplicate(f'x10_stop_{x10_reason[:20]}', 'STOP'):
            log_event(f'🛑 X10 блок: {x10_reason}')
        x10_blocked = True

    if not x10_blocked:
        global_blocked, _ = _check_risk_limits(new_positions, cfg.risk)
        if global_blocked:
            x10_blocked = True
            log_event('🛑 X10 блок: глобальный risk-лимит')

    try:
        bal = bybit('GET', '/v5/account/wallet-balance?accountType=UNIFIED&coin=USDT')
        usdt = bal.get('result', {}).get('list', [{}])[0].get('coin', [{}])[0]
        balance_usdt = float(usdt.get('walletBalance', 0))
    except Exception:
        balance_usdt = 100.0

    correlated_x10 = set()
    if new_positions and len(new_positions) >= 3:
        corr_snapshot = load_correlation_snapshot()
        if corr_snapshot:
            corr_threshold = cfg.alerts.get('correlation_threshold', 0.80)
            for pair_data in corr_snapshot.get('pairs', []):
                if len(pair_data) >= 3:
                    s1, s2, r = pair_data[0], pair_data[1], pair_data[2]
                    if abs(r) > corr_threshold:
                        correlated_x10.add(s1)
                        correlated_x10.add(s2)

    # 1. BB Scalping M5 x10
    if not correlation_stop and not x10_blocked:
        scalp_alerts, scalp_entries = check_scalp_signals(new_positions, balance_usdt)
        for msg in scalp_alerts:
            sym = msg.split()[1]
            open_corr = sum(1 for s in new_positions if s in correlated_x10 and sym in correlated_x10)
            if open_corr >= 2:
                log_event(f'⏭️ СКАЛЬП {sym}: корреляция ({open_corr} связанных позиций)')
                continue
            add_alert('ENTRY', msg)
            send_telegram_alert(msg)
        for entry in scalp_entries:
            passed, reason = validate_entry(entry, balance_usdt)
            if passed:
                if execute_scalp(entry):
                    track_x10_entry(entry['symbol'], 'scalp')
                record_auto_entry(placed=True)
            else:
                log_event(f'⏭️ СКАЛЬП {entry["symbol"]}: {reason}')

    # 2. Mean Reversion x10
    if not correlation_stop and not x10_blocked:
        mean_alerts, mean_entries = check_mean_revert(new_positions)
        for msg in mean_alerts:
            sym = msg.split()[1]
            open_corr = sum(1 for s in new_positions if s in correlated_x10 and sym in correlated_x10)
            if open_corr >= 2:
                log_event(f'⏭️ MEAN {sym}: корреляция ({open_corr} связанных позиций)')
                continue
            add_alert('ENTRY', msg)
            send_telegram_alert(msg)
        for entry in mean_entries:
            passed, reason = validate_entry(entry, balance_usdt)
            if passed:
                if execute_mean_revert(entry):
                    track_x10_entry(entry['symbol'], 'mean_revert')
                record_auto_entry(placed=True)
            else:
                log_event(f'⏭️ MEAN {entry["symbol"]}: {reason}')

    # 3. Funding Rate Momentum x10
    if not x10_blocked:
        fund_alerts, fund_entries = check_funding_signals(new_positions)
        for msg in fund_alerts:
            sym = msg.split()[1]
            open_corr = sum(1 for s in new_positions if s in correlated_x10 and sym in correlated_x10)
            if open_corr >= 2:
                log_event(f'⏭️ FUNDING {sym}: корреляция ({open_corr} связанных позиций)')
                continue
            add_alert('ENTRY', msg)
            send_telegram_alert(msg)
        for entry in fund_entries:
            passed, reason = validate_entry(entry, balance_usdt)
            if passed:
                if execute_funding_entry(entry):
                    track_x10_entry(entry['symbol'], 'funding_momentum')
                record_auto_entry(placed=True)
            else:
                log_event(f'⏭️ FUNDING {entry["symbol"]}: {reason}')

    # 4. ATR риск-мониторинг
    risk_alerts = check_position_risk(new_positions, balance_usdt)
    for msg in risk_alerts:
        add_alert('STOP', msg)
        send_telegram_alert(msg)


def _run_safety_checks(new_positions, cfg, cycle_count, now_ts):
    """Ликвидация, просадка, риск-лимиты, маржа, шорт-лимиты."""
    if not new_positions:
        return
    HEAVY_CYCLE = cfg.monitor.heavy_cycle

    for msg in check_liquidation(new_positions):
        add_alert('STOP', msg)

    # Каскадная защита
    for sym, p in new_positions.items():
        if is_manual_position(sym):
            continue
        liq = p.get('liqPrice')
        sl = p.get('stopLoss')
        mark = p.get('mark', 0)
        if liq and sl and mark:
            dist_to_liq = abs(mark - liq)
            dist_to_sl = abs(mark - sl)
            if dist_to_sl > 0 and dist_to_liq < dist_to_sl * 0.5:
                _close_instant(sym, p)
                add_alert('STOP', f'🆘 {sym}: cascade — mark ${mark:.4f} ближе к liq ${liq:.4f} чем к SL ${sl:.4f}')

    if cycle_count % HEAVY_CYCLE == 0:
        dd_msg = check_daily_drawdown(new_positions)
        if dd_msg:
            add_alert('STOP', dd_msg)
        risk_blocked, risk_msgs = _check_risk_limits(new_positions, cfg.risk)
        for msg in risk_msgs:
            add_alert('STOP', msg)
        margin_msgs = check_margin_utilization(new_positions)
        for msg in margin_msgs:
            add_alert('STOP', msg)
            send_telegram_alert(msg, level='STOP')

    # Instant TP
    instant_syms = set(cfg.strategy.short.get('instant_tp_symbols', []))
    if instant_syms:
        for sym, p in new_positions.items():
            if is_manual_position(sym):
                continue
            if sym in instant_syms and float(p.get('upnl', 0)) > 0:
                _close_instant(sym, p)
                add_alert('TP', f'⚡ {sym} мгновенный TP: +${float(p["upnl"]):.2f}')

    # Лимит шортов
    max_short_pct = cfg.strategy.short.get('max_short_pct', 20)
    total_pos = len(new_positions)
    shorts = sum(1 for p in new_positions.values() if p.get('side') == 'Sell')
    if total_pos > 0 and shorts / total_pos * 100 > max_short_pct:
        dedup_file = os.path.join(DATA_DIR, 'last_short_alert.json')
        last_alert = 0
        if os.path.exists(dedup_file):
            try:
                with open(dedup_file) as f:
                    last_alert = json.load(f).get('ts', 0)
            except Exception as e:
                log_event(f'⚠️ last_short_alert read error: {e}')
        if now_ts - last_alert > 86400:
            add_alert('STOP', f'🚨 Шортов {shorts}/{total_pos} ({shorts/total_pos*100:.0f}%) > {max_short_pct}% лимит')
            safe_json_write(dedup_file, {'ts': now_ts, 'shorts': shorts, 'total': total_pos})

    # SHORT max_hold_hours
    max_hold = cfg.strategy.short.get('max_hold_hours', 0)
    if max_hold > 0:
        for sym, p in new_positions.items():
            if p.get('side') != 'Sell':
                continue
            open_time = p.get('openTime', 0)
            if open_time > 0:
                held_hours = (now_ts - open_time / 1000) / 3600
                if held_hours > max_hold:
                    _close_instant(sym, p)
                    add_alert('STOP', f'⏰ {sym}: SHORT закрыт по таймауту ({held_hours:.0f}ч > {max_hold}ч)')


def main_loop():
    cfg = Config()
    CYCLE_SECONDS = cfg.monitor.cycle_seconds
    WATCHDOG_SECONDS = cfg.monitor.watchdog_seconds
    HEAVY_CYCLE = cfg.monitor.heavy_cycle
    RPC_PORT = cfg.rpc.port
    RPC_BIND = cfg.rpc.bind

    print(f"🔄 Bybit WS Monitor v2.6 запущен")
    print(f"   Лог: {EVENTS_LOG}")
    print(f"   Алерты: {ALERTS_LOG}")
    print(f"   Проверка каждые {CYCLE_SECONDS} секунд")
    print(f"   Нажми Ctrl+C для остановки")

    # Инициализация watchlist-ротации
    rotate_watchlist()

    old_positions = fetch_positions()
    old_orders = fetch_orders()

    if old_positions:
        save_json(POSITIONS_SNAPSHOT, old_positions)
    if old_orders:
        save_json(ORDERS_SNAPSHOT, old_orders)

    # Защита позиций после старта: проверить SL на всех позициях сразу
    if old_positions:
        try:
            sl_alerts = check_and_fix_sl()
            if sl_alerts:
                for a in sl_alerts:
                    add_alert('SL', a)
            # Б/у-SL: рост >10% → SL на безубыток
            be_alerts = check_breakeven_sl()
            if be_alerts:
                for a in be_alerts:
                    add_alert('SL', a)
        except Exception as e:
            log_event(f'⚠️ startup SL check error: {e}')
        meta = load_json(ORDERS_METADATA)
        now = time.time()
        for key, o in old_orders.items():
            if key not in meta and o['kind'] == 'LIMIT_ENTRY':
                meta[key] = {'first_seen': now}
        save_json(ORDERS_METADATA, meta)

    log_event(f'Монитор запущен: {len(old_positions)} позиций, {len(old_orders)} ордеров')

    # Запуск RPC-сервера
    try:
        rpc_server = start_rpc_server(RPC_PORT, bind=RPC_BIND)
        log_event(f'🌐 RPC-сервер: http://{RPC_BIND}:{RPC_PORT}')
    except Exception as e:
        log_event(f'⚠️ RPC-сервер не запустился: {e}')

    # Запуск WebSocket-клиента (Фаза 4.1 — live цены/BB)
    ws_enabled = cfg.websocket.get('enabled', True)
    if ws_enabled:
        try:
            from .ws_client import start as ws_start
            ws_start()
            log_event('🔌 WebSocket-клиент запущен (live цены, BB)')
        except Exception as e:
            log_event(f'⚠️ WS-клиент не запустился: {e}')

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
                try:
                    rpc_server.shutdown()
                except Exception as e:
                    log_event(f'⚠️ RPC shutdown error: {e}')
                sys.exit(0)

            time.sleep(CYCLE_SECONDS)
            now_wd = time.time()
            cycle_count += 1
            now_ts = time.time()

            # Заранее инициализируем для watchdog
            new_positions = {}
            new_orders = []

            # Health-check: timestamp последнего успешного цикла
            try:
                save_json(HEALTH_FILE, now_ts)
            except Exception as e:
                log_event(f'⚠️ health file write error: {e}')

            # RPC health update
            rpc_update_health(alive=True, cycle_count=cycle_count)

            new_positions = fetch_positions()
            new_orders = fetch_orders()

            if now_wd - WATCHDOG_LAST > WATCHDOG_SECONDS:
                log_event(f'🚨 Watchdog: главный цикл завис ({now_wd - WATCHDOG_LAST:.0f}с) — аварийный выход')
                # Сохранить снепшоты перед выходом
                try:
                    if new_positions:
                        save_json(POSITIONS_SNAPSHOT, new_positions)
                    if new_orders:
                        save_json(ORDERS_SNAPSHOT, new_orders)
                except Exception as e:
                    log_event(f'⚠️ Watchdog snapshot save error: {e}')
                sys.exit(1)
            WATCHDOG_LAST = now_wd

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
                    if change_type == 'CLOSED' and sym in sl_hit_syms and sym not in new_positions:
                        # Dedup: кулдаун 5 минут на STOP-алерт (hedge SL orderId race)
                        _now = time.time()
                        if _now - _SL_DEDUP.get(sym, 0) < SL_ALERT_COOLDOWN:
                            continue
                        _SL_DEDUP[sym] = _now
                        old_pos = old_positions.get(sym, {})
                        entry = old_pos.get('entry', 0)
                        size = old_pos.get('size', 0)
                        side = old_pos.get('side', 'Buy')
                        lev = old_pos.get('leverage', 1)
                        exit_price = old_pos.get('stopLoss') or old_pos.get('mark', 0)
                        if side == 'Sell':
                            rpnl = size * (entry - exit_price)
                        else:
                            rpnl = size * (exit_price - entry)
                        pnl_pct = (rpnl / (size * entry)) * 100 if size and entry else 0
                        emoji = '🔴' if rpnl < 0 else '🛑'
                        add_alert('STOP', f'{emoji} {sym} SL: ${entry:.6f}→${exit_price:.6f} | {rpnl:+.1f}$ ({pnl_pct:+.1f}%) | {size:.0f}×{lev:.0f}x {side}')
                        record_alert('SL')
                        # ── Фаза 4.3.5: запись закрытия в confluence paper ──
                        try:
                            from .confluence_paper import record_close
                            direction = 'LONG' if side == 'Buy' else 'SHORT'
                            record_close(sym, direction, exit_price, rpnl)
                        except Exception:
                            pass
                        # SL re-entry: запомнить для лесенки (только LONG)
                        if side == 'Buy':
                            sl_price = old_pos.get('mark', 0)
                            notify_sl_hit(sym, sl_price, entry)
                        # Cooldown для LONG: запретить повторный вход на N часов
                        record_sl_hit(sym)
                        continue
                    if change_type == 'CLOSED' and sym in tp_hit_syms and sym not in new_positions:
                        old_pos = old_positions.get(sym, {})
                        entry = old_pos.get('entry', 0)
                        size = old_pos.get('size', 0)
                        side = old_pos.get('side', 'Buy')
                        lev = old_pos.get('leverage', 1)
                        exit_price = old_pos.get('mark', 0)
                        if side == 'Sell':
                            rpnl = size * (entry - exit_price)
                        else:
                            rpnl = size * (exit_price - entry)
                        pnl_pct = (rpnl / (size * entry)) * 100 if size and entry else 0
                        add_alert('TP', f'🎯 {sym} TP: ${entry:.6f}→${exit_price:.6f} | +{rpnl:.1f}$ (+{pnl_pct:.1f}%) | {size:.0f}×{lev:.0f}x {side}')
                        record_alert('TP')
                        # ── Фаза 4.3.5: запись закрытия в confluence paper ──
                        try:
                            from .confluence_paper import record_close
                            direction = 'LONG' if side == 'Buy' else 'SHORT'
                            record_close(sym, direction, exit_price, rpnl)
                        except Exception:
                            pass
                        continue
                    if change_type == 'CLOSED':
                        old_pos = old_positions.get(sym, {})
                        pnl = old_pos.get('upnl', 0)
                        entry = old_pos.get('entry', 0)
                        sign = '+' if pnl >= 0 else '−'
                        add_alert('INFO', f'📋 {sym} закрыта {sign}${abs(pnl):.2f} (вход ${entry:.4f})')
                        # Записать в x10-трекер если позиция с плечом ≥10
                        leverage = float(old_pos.get('leverage', 0) or 0)
                        if leverage >= 10:
                            strat = get_x10_strategy(sym) or 'scalp'
                            record_x10_trade(strat, float(pnl))
                            clear_x10_position(sym)
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
                        # Dedup: позиция не в снапшоте (гонка) — кулдаун 5 минут
                        if sym not in new_positions:
                            _now = time.time()
                            if _now - _SL_DEDUP.get(sym, 0) < SL_ALERT_COOLDOWN:
                                continue
                            _SL_DEDUP[sym] = _now
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

            # Trailing SL + JUNK Trail TP
            if cycle_count % TRAIL_CHECK_INTERVAL == 0 and new_positions:
                trail_actions = trailing_sl(new_positions)
                if trail_actions:
                    apply_trailing_sl(trail_actions)
                    for sym, idx, side, size, price in trail_actions:
                        add_alert('INFO', f'🔺 Trailing SL {sym} подтянут до ${price:.4f}')

                # Trailing TP для JUNK-шортов (только если enabled)
                if new_orders and cfg.strategy.junk.get('enabled', False):
                    junk_trail_actions = trailing_junk_tp(new_positions, new_orders)
                    if junk_trail_actions:
                        for sym, pnl_pct, new_tp, reason in junk_trail_actions:
                            add_alert('TP', f'🔒 JUNK TP {sym}: {reason} при +{pnl_pct:.1f}% → ${new_tp:.6f}')

            # X10 агрессивный трейлинг (каждый HEAVY_CYCLE)
            if cycle_count % HEAVY_CYCLE == 0 and new_positions:
                x10_actions = trailing_sl_x10(new_positions)
                if x10_actions:
                    apply_trailing_sl(x10_actions)
                    for sym, idx, side, size, price in x10_actions:
                        add_alert('INFO', f'⚡ X10 Trail SL {sym} → ${price:.4f}')

            # Funding Rate ротация: алерт при невыгодном фандинге (каждый HEAVY_CYCLE)
            if cycle_count % HEAVY_CYCLE == 0 and new_positions:
                rotations = check_funding_rotation(new_positions)
                for rot in rotations:
                    add_alert(
                        'INFO',
                        f'💰 Ротация {rot["from"]}→{rot["to"]}: '
                        f'фандинг {rot["current_funding"]}%→{rot["new_funding"]}% '
                        f'(Δ{rot["delta"]}%), BB={rot["bb_pct"]}%'
                    )

            # Partial TP: динамический сплит (каждые 4 HEAVY_CYCLE = ~8 мин)
            if cycle_count % (HEAVY_CYCLE * 4) == 0 and new_positions:
                try:
                    ptp_alerts = check_partial_tp()
                    for msg in ptp_alerts:
                        add_alert('INFO', msg)
                except Exception as e:
                    add_alert('WARN', f'Partial TP error: {e}')

            # Агрессивный авто-SL: каждые 4 цикла (2 мин)
            if cycle_count % 4 == 0 and new_positions:
                sl_alerts = check_and_fix_sl()
                for msg in sl_alerts:
                    if msg.startswith('🛡'):
                        add_alert('INFO', msg)
                    elif msg.startswith('⚠️'):
                        add_alert('STOP', msg)
                # Б/у-SL: рост >10% → SL на безубыток
                be_alerts = check_breakeven_sl()
                for msg in be_alerts:
                    add_alert('INFO', msg)
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
                    # Dedup: не спамить один и тот же TP каждые N циклов
                    now_ts = time.time()
                    _tp_alerted = getattr(main_loop, '_tp_alerted', {})
                    for sym, idx, side, qty, price, pos_size in tp_actions:
                        key = f'{sym}:{price:.4f}'
                        last = _tp_alerted.get(key, 0)
                        if now_ts - last > 1800:  # 30 мин кулдаун на ту же цену
                            add_alert('INFO', f'🎯 Auto-TP {sym}: @ ${price:.4f}')
                            _tp_alerted[key] = now_ts
                    main_loop._tp_alerted = _tp_alerted

            # Безопасность: ликвидация, просадка, риск-лимиты, шорт-лимиты
            _run_safety_checks(new_positions, cfg, cycle_count, now_ts)

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
                    reason = 'CLOSE'
                    alert_ref = ''
                    # Определяем стратегию
                    strategy_tag = p.get('strategy', '')
                    leverage = float(p.get('leverage', 0) or 0)
                    # x10 стратегии
                    if leverage >= 10:
                        x10_strat = get_x10_strategy(sym)
                        if x10_strat:
                            strategy_tag = f'x10:{x10_strat}'
                        else:
                            strategy_tag = 'x10'
                    elif side == 'Sell':
                        # Проверяем pumps.json для JUNK
                        try:
                            import json as _json2
                            pump_file = os.path.join(DATA_DIR, 'pumps.json')
                            if os.path.exists(pump_file):
                                with open(pump_file) as f:
                                    pumps = _json2.load(f)
                                if sym in pumps and pumps[sym].get('short_entry_ts'):
                                    strategy_tag = 'JUNK_SHORT'
                        except Exception as e:
                            log_event(f'⚠️ pumps.json read error (strategy tag): {e}')
                        if not strategy_tag:
                            strategy_tag = 'SHORT'
                    elif side == 'Buy':
                        strategy_tag = 'GRID_LONG'
                    # Приоритетная причина закрытия
                    for ct2, sym2, msg2 in ord_changes:
                        if sym2 == sym:
                            if ct2 == 'SL_HIT':
                                reason = 'SL'
                                alert_ref = 'SL_HIT'
                            elif ct2 == 'TP_HIT' and reason != 'SL':
                                reason = 'TP'
                                alert_ref = 'TP_HIT'
                    # Проверка ручного закрытия через алерты
                    for a in ALERTS:
                        if sym in a and ('закрыт по таймауту' in a or 'TIMEOUT' in a):
                            reason = 'TIMEOUT'
                        if sym in a and ('STOP JUNK' in a):
                            reason = 'JUNK_STOP'
                        if sym in a and ('EMERGENCY' in a):
                            reason = 'EMERGENCY'
                    log_trade(sym, entry, mark, pnl, side, reason, alert_ref, strategy_tag)
                    # SQLite trade_history (аудит PnL)
                    try:
                        db.add_trade(sym, side, strategy_tag, entry, mark,
                                     p.get('size', 0), pnl, fees=0)
                    except Exception:
                        pass

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
                        safe_json_write(dca_file, dca_state)
                    except Exception as e:
                        log_event(f'⚠️ main dca_cleanup: {e}')
            if reduced_syms and new_positions and new_orders:
                recycle_actions = handle_tp_recycle(reduced_syms, new_positions, new_orders)
                if recycle_actions:
                    apply_recycle(recycle_actions)

            # Тяжёлые проверки каждые HEAVY_CYCLE циклов
            _run_heavy_cycle(cfg, new_positions, new_orders, cycle_count, now_ts)

            # Сводка TP/SL покрытия раз в 4 часа
            if cycle_count % COVERAGE_CHECK_INTERVAL == 0 and new_positions and new_orders:
                cov_msg = check_coverage_summary(new_positions, new_orders)
                if cov_msg:
                    send_telegram_alert(cov_msg)
                    add_alert('INFO', '🛡 Отправлена сводка TP/SL покрытия')

            # Ежедневный PnL-алерт: худший день за неделю (раз в час с дедупликацией)
            if cycle_count % 120 == 0:  # каждый час
                pnl_msg = check_daily_pnl_alert()
                if pnl_msg:
                    add_alert('STOP', pnl_msg)
                    send_telegram_alert(pnl_msg, level='STOP')

            # Cost tracking: логирование комиссий (раз в час, с внутренним троттлингом)
            cost_tracker_check()

            # Funding tracker: поиск экстремальных ставок (раз в час, с внутренним троттлингом)
            funding_alerts = funding_tracker_check()
            for msg in funding_alerts:
                add_alert('INFO', msg)

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
                    entry_blocked, _ = _check_risk_limits(new_positions, cfg.risk)
                    if entry_blocked:
                        log_event('🛑 Авто-вход заблокирован: risk-лимит (max_daily_loss / max_total_margin)')
                    else:
                        auto_entries = auto_entry_scan(new_positions)
                        for msg in auto_entries:
                            add_alert('ENTRY', msg)
                            send_telegram_alert(msg)
                            record_auto_entry(placed=True)

            # SL re-entry: лесенка после стоп-лосса (каждые 10 циклов = 5 мин)
            # correlation_stop НЕ передаём — ре-энтри восстанавливает позицию, а не наращивает exposure
            if cycle_count % HEAVY_CYCLE == 0:
                reentry_blocked, _ = _check_risk_limits(new_positions or {}, cfg.risk)
                if reentry_blocked:
                    log_event('🛑 SL re-entry заблокирован: risk-лимит')
                else:
                    check_sl_reentry(new_positions or {}, False)
                # Очистка _SL_DEDUP от записей старше 24ч (memory leak fix)
                _SL_DEDUP.clear() if len(_SL_DEDUP) > 1000 else [
                    _SL_DEDUP.pop(sym, None) for sym in list(_SL_DEDUP)
                    if now_ts - _SL_DEDUP.get(sym, 0) > 86400
                ]

            # X10 стратегии каждые HEAVY_CYCLE*2 циклов
            _run_x10_cycle(cfg, new_positions, cycle_count, correlation_stop)

            # Сводка 09:00 и 21:00
            label = should_send_summary()
            if label:
                send_summary(label)

            # Сохраняем снепшоты (всегда, даже если пустые — иначе спам алертов)
            save_json(POSITIONS_SNAPSHOT, new_positions or {})
            old_positions = new_positions or {}
            save_json(ORDERS_SNAPSHOT, new_orders or {})
            old_orders = new_orders or {}

            # Тайминг цикла — алерт при >20s
            cycle_duration = time.time() - now_ts
            rpc_update_health(alive=True, cycle_count=cycle_count, cycle_duration=cycle_duration)
            if cycle_duration > 20:
                log_event(f'⚠️ Цикл {cycle_duration:.1f}s — превышен порог 20s')

            # Статус каждые 5 мин
            if cycle_count % HEAVY_CYCLE == 0:
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
                    margin_stats = get_margin_stats(new_positions or {})
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] {pos_count} поз, {ord_count} орд, "
                          f"маржа ${margin_stats['total_margin']:.0f}/{margin_stats['max_margin']:.0f} "
                          f"({margin_stats['utilization_pct']:.0f}%), всё штатно")

            # Алерты для bybit-monitor
            if cycle_count % 2 == 0:
                alerts = get_alerts()
                if alerts:
                    with locked_open(os.path.join(DATA_DIR, 'new_alerts.txt'), 'w') as f:
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
            import traceback
            log_event(f'Ошибка в цикле: {e}')
            log_event(f'Traceback:\\n{traceback.format_exc()}')
            time.sleep(5)
            continue


def handle_sigterm(signum, frame):
    global SHUTDOWN_REQUESTED
    from . import SHUTDOWN_REQUESTED as s
    log_event('Получен SIGTERM — graceful shutdown')
    import bybit_ws
    bybit_ws.SHUTDOWN_REQUESTED = True
    globals()['SHUTDOWN_REQUESTED'] = True

    # Проверить все позиции имеют SL
    try:
        from .auto_sl import check_and_fix_sl, check_breakeven_sl
        sl_alerts = check_and_fix_sl()
        for msg in sl_alerts:
            log_event(f'  Shutdown SL check: {msg}')
    except Exception as e:
        log_event(f'  Shutdown SL check failed: {e}')

    # Сохранить состояние
    try:
        new_positions = fetch_positions()
        new_orders = fetch_orders()
        if new_positions:
            save_json(POSITIONS_SNAPSHOT, new_positions)
        if new_orders:
            save_json(ORDERS_SNAPSHOT, new_orders)
        log_event(f'Shutdown: сохранено {len(new_positions) if new_positions else 0} позиций, '
                  f'{len(new_orders) if new_orders else 0} ордеров')
    except Exception as e:
        log_event(f'Shutdown: ошибка сохранения состояния: {e}')

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
        # JUNK trailing TP
        if new_orders:
            junk_trail_actions = trailing_junk_tp(new_positions, new_orders)
            if junk_trail_actions:
                for sym, pnl_pct, new_tp, reason in junk_trail_actions:
                    print(f'🔒 {sym}: JUNK TP {reason} при +{pnl_pct:.1f}% → ${new_tp:.6f}')
                    alerts.append(f'JUNK Trail TP {sym}: {reason} @ ${new_tp:.6f}')
        tp_actions = auto_take_profit(new_positions, new_orders)
        if tp_actions:
            apply_auto_tp(tp_actions)
            for sym, idx, side, qty, price, pos_size in tp_actions:
                print(f'🎯 {sym}: TP @ ${price:.4f}')
                alerts.append(f'Auto-TP {sym} @ ${price:.4f}')

    if new_positions:
        save_json(POSITIONS_SNAPSHOT, new_positions)
        db.save_positions(new_positions)  # SQLite SSOT
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
            with locked_open(wl_file, 'w') as f:
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
                with locked_open(wl_file, 'w') as f:
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
