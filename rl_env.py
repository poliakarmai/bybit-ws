"""
rl_env.py — RL-среда для обучения агента выбора момента входа (Фаза 5.5).

Gymnasium-совместимая среда на исторических kline-данных.
Агент решает: ENTER_LONG, ENTER_SHORT, WAIT, или SKIP.

State (13 признаков):
  BB%, BB_width, RSI, ATR%, Vol_ratio, Funding, MTF_confluence,
  Days_since_entry, Regime_1hot×5 (первые 3), Score_norm

Actions: SKIP=0, ENTER_LONG=1, ENTER_SHORT=2, WAIT=3

Reward: PnL сделки (TP+, SL−), 0 за SKIP, -0.01 за WAIT
"""

import math
import os
import random
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import gymnasium as gym
    from gymnasium import spaces
    HAS_GYM = True
except ImportError:
    HAS_GYM = False

DATA_DIR = Path.home() / '.local' / 'share' / 'bybit-ws'

# ── Константы ────────────────────────────────────────────────

N_FEATURES = 13
N_ACTIONS = 3

ACTION_NAMES = ['SKIP', 'ENTER_LONG', 'WAIT']

REGIME_MAP = {
    'TRENDING_UP': 0, 'TRENDING_DOWN': 1, 'RANGING': 2,
    'HIGH_VOL': 3, 'LOW_VOL': 4, 'CHOPPY': 5, 'NEUTRAL': 6, 'UNKNOWN': 6,
}


def _calc_features(closes, highs, lows, volumes, idx, funding_rate=0.0):
    """Вычислить признаки для индекса idx в исторических данных."""
    if idx < 20:
        return None

    close = closes[idx]
    high = highs[idx]
    low = lows[idx]
    vol = volumes[idx]

    if close == 0:
        return None

    # 1. BB% (0-100)
    window = closes[idx - 19:idx + 1]
    sma = sum(window) / 20
    std = (sum((x - sma) ** 2 for x in window) / 20) ** 0.5
    bb_pct = (close - (sma - 2 * std)) / (4 * std) * 100 if std > 0 else 50
    bb_pct = max(0, min(100, bb_pct))

    # 2. BB width %
    bb_width = (4 * std) / sma * 100 if sma > 0 else 10

    # 3. RSI(14)
    gains = 0.0
    losses = 0.0
    rsi_n = min(14, idx)
    for i in range(idx - rsi_n + 1, idx + 1):
        chg = closes[i] - closes[i - 1]
        if chg > 0:
            gains += chg
        else:
            losses -= chg
    avg_gain = gains / rsi_n if rsi_n > 0 else 0
    avg_loss = losses / rsi_n if rsi_n > 0 else 1e-9
    rsi = 100 - 100 / (1 + avg_gain / avg_loss) if avg_loss > 0 else 100
    rsi = max(0, min(100, rsi))

    # 4. ATR(14)/close %
    trs = []
    for i in range(max(0, idx - 13), idx + 1):
        h = highs[i]
        l = lows[i]
        prev_c = closes[i - 1] if i > 0 else close
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    atr = sum(trs) / len(trs) if trs else 0
    atr_pct = atr / close * 100 if close > 0 else 0

    # 5. Volume ratio
    if idx >= 21:
        avg_vol = sum(volumes[idx - 20:idx]) / 20
    else:
        avg_vol = sum(volumes[:idx]) / max(1, idx)
    vol_ratio = vol / avg_vol if avg_vol > 0 else 1.0
    vol_ratio = min(vol_ratio, 10.0)

    # 6-8. Regime one-hot (первые 3 класса)
    # Упрощённо: определяем по движению цены
    ret_5d = (closes[idx] / closes[idx - 5] - 1) * 100 if idx >= 5 else 0
    if ret_5d > 5:
        regime_vec = [1, 0, 0]  # trending_up
    elif ret_5d < -5:
        regime_vec = [0, 1, 0]  # trending_down
    elif atr_pct > 4:
        regime_vec = [0, 0, 1]  # high_vol
    else:
        regime_vec = [0, 0, 0]  # ranging/low_vol

    # 9. Funding rate (нормализовано)
    funding_norm = max(-0.1, min(0.1, funding_rate)) / 0.1  # -1..1

    # 10. MTF confluence proxy (0-1)
    mtf_proxy = 0.5  # нейтрально

    # 11. Score proxy (0-1, где 1 = идеальный BB%)
    score_proxy = max(0, (100 - bb_pct) / 100) if bb_pct > 10 else 1.0

    # 12. Days since entry proxy
    days_since = 0.0

    # 13. Daily return
    daily_ret = (closes[idx] / closes[idx - 1] - 1) * 100 if idx > 0 else 0

    features = [
        bb_pct / 100,       # 0-1
        bb_width / 20,      # 0-1 (20% max)
        rsi / 100,          # 0-1
        atr_pct / 10,       # 0-1 (10% max)
        vol_ratio / 5,      # 0-1 (5x max)
        funding_norm,       # -1..1
        mtf_proxy,          # 0-1
        days_since,         # 0-1
        *regime_vec,        # 3 × 0/1
        score_proxy,        # 0-1
        daily_ret / 10,     # норм. дневная доходность
    ]

    return np.array(features, dtype=np.float32)


class TradingEnv(gym.Env):
    """
    RL-среда: агент проходит по историческим данным и решает когда входить.
    """

    def __init__(self, kline_data, tp_pct=5.0, sl_pct=3.0, max_hold_days=14):
        super().__init__()
        if not HAS_GYM:
            raise ImportError("gymnasium not installed")

        self.closes = np.array(kline_data['closes'], dtype=np.float32)
        self.highs = np.array(kline_data['highs'], dtype=np.float32)
        self.lows = np.array(kline_data['lows'], dtype=np.float32)
        self.volumes = np.array(kline_data['volumes'], dtype=np.float32)
        self.symbol = kline_data.get('symbol', 'UNKNOWN')

        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.max_hold_days = max_hold_days

        self.n_days = len(self.closes)
        self.start_idx = 30  # минимум 30 дней для признаков

        # Action/observation spaces
        self.action_space = spaces.Discrete(N_ACTIONS)
        self.observation_space = spaces.Box(
            low=-1.0, high=1.0, shape=(N_FEATURES,), dtype=np.float32
        )

        self.reset()

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)

        self.idx = self.start_idx + random.randint(0, max(0, self.n_days - self.start_idx - 60))
        self.position = None  # {'side': 'LONG'/'SHORT', 'entry': price, 'day': idx}
        self.total_pnl = 0.0
        self.trades = []
        self.days_since_entry = 0
        self.done = False

        return self._get_obs(), {}

    def _get_obs(self):
        feat = _calc_features(
            self.closes, self.highs, self.lows, self.volumes,
            self.idx, funding_rate=0.0
        )
        if feat is None:
            feat = np.zeros(N_FEATURES, dtype=np.float32)
        # Days since entry
        feat[7] = min(1.0, self.days_since_entry / 30)
        return feat.astype(np.float32)

    def _simulate_trade(self, entry_price):
        """Симулировать LONG-сделку от входа до TP/SL/истечения."""
        entry_day = self.idx
        for day in range(entry_day + 1, min(entry_day + self.max_hold_days + 1, self.n_days)):
            close = float(self.closes[day])
            high = float(self.highs[day])
            low = float(self.lows[day])

            tp_price = entry_price * (1 + self.tp_pct / 100)
            sl_price = entry_price * (1 - self.sl_pct / 100)
            if high >= tp_price:
                return tp_price, 'TP', day - entry_day
            if low <= sl_price:
                return sl_price, 'SL', day - entry_day

        # Истекло время — закрываем по последней цене
        last_day = min(entry_day + self.max_hold_days, self.n_days - 1)
        last_close = float(self.closes[last_day])
        return last_close, 'TIMEOUT', last_day - entry_day

    def step(self, action):
        if self.done:
            raise RuntimeError("Episode is done. Call reset().")

        reward = 0.0
        info = {'action': ACTION_NAMES[action]}

        if self.position is not None:
            # В позиции — проверяем закрытие
            entry_price = self.position['entry']
            side = self.position['side']
            exit_price, outcome, days_held = self._simulate_trade(entry_price)

            if side == 'LONG':
                pnl = (exit_price - entry_price) / entry_price * 100
            else:
                pnl = (entry_price - exit_price) / entry_price * 100

            # Комиссия 0.11% на круг
            pnl -= 0.11
            reward = pnl

            self.total_pnl += pnl
            self.trades.append({
                'side': side, 'entry': entry_price, 'exit': exit_price,
                'outcome': outcome, 'pnl': round(pnl, 2), 'days': days_held,
            })
            self.position = None
            self.days_since_entry = 0
            info.update({'pnl': round(pnl, 2), 'outcome': outcome})

        elif action == 1:  # ENTER_LONG
            self.position = {'side': 'LONG', 'entry': float(self.closes[self.idx]), 'day': self.idx}
            info['entered'] = 'LONG'

        elif action == 2:  # WAIT
            reward = -0.01  # небольшой штраф за бездействие

        # else action == 0: SKIP — просто идём дальше

        # Следующий день
        self.idx += 1
        if self.position:
            self.days_since_entry += 1

        # Конец эпизода
        if self.idx >= self.n_days - self.max_hold_days:
            self.done = True
            # Закрыть открытую позицию
            if self.position:
                exit_price = float(self.closes[-1])
                side = self.position['side']
                if side == 'LONG':
                    pnl = (exit_price - self.position['entry']) / self.position['entry'] * 100 - 0.11
                else:
                    pnl = (self.position['entry'] - exit_price) / self.position['entry'] * 100 - 0.11
                reward += pnl
                self.total_pnl += pnl
                self.trades.append({
                    'side': side, 'entry': self.position['entry'], 'exit': exit_price,
                    'outcome': 'EOS', 'pnl': round(pnl, 2), 'days': self.days_since_entry,
                })
                self.position = None

        obs = self._get_obs()
        truncated = False

        return obs, reward, self.done, truncated, info


# ── Загрузка данных ──────────────────────────────────────────

def fetch_kline_data(symbols=('BTCUSDT', 'ETHUSDT', 'SOLUSDT'), days=365):
    """Загрузить D-свечи для обучения RL."""
    import sys
    import time as _time
    sys.path.insert(0, str(Path(__file__).parent.parent))
    try:
        from bybit_ws.api import bybit
    except ImportError:
        from api import bybit

    all_data = []
    for sym in symbols:
        closes, highs, lows, volumes = [], [], [], []
        fetched = 0
        end_ms = None
        while fetched < days:
            limit = min(200, days - fetched)
            url = f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit={limit}'
            if end_ms:
                url += f'&end={end_ms}'
            resp = bybit('GET', url)
            if not resp or resp.get('retCode') != 0:
                break
            candles = resp['result'].get('list', [])
            if not candles:
                break
            for c in reversed(candles):
                closes.append(float(c[4]))
                highs.append(float(c[2]))
                lows.append(float(c[3]))
                volumes.append(float(c[5]))
            fetched += len(candles)
            if len(candles) < limit:
                break
            end_ms = candles[0][0]
            _time.sleep(0.1)
        if closes:
            all_data.append({
                'symbol': sym, 'closes': closes, 'highs': highs,
                'lows': lows, 'volumes': volumes,
            })
    return all_data


# ── CLI ─────────────────────────────────────────────────────

if __name__ == '__main__':
    # Быстрый тест
    if not HAS_GYM:
        print("❌ gymnasium не установлен")
        exit(1)

    # Тест на синтетических данных
    np.random.seed(42)
    n = 365
    trend = np.cumsum(np.random.randn(n) * 2) + 100
    data = {
        'symbol': 'TEST',
        'closes': trend.tolist(),
        'highs': (trend * 1.02).tolist(),
        'lows': (trend * 0.98).tolist(),
        'volumes': np.random.randint(1000, 5000, n).tolist(),
    }

    env = TradingEnv(data)
    obs, _ = env.reset()
    print(f"✅ Env OK: obs shape={obs.shape}, action_space={env.action_space.n}")

    total_reward = 0
    steps = 0
    while True:
        action = env.action_space.sample()
        obs, reward, done, truncated, info = env.step(action)
        total_reward += reward
        steps += 1
        if done:
            break

    print(f"✅ Random agent: steps={steps}, reward={total_reward:.2f}, trades={len(env.trades)}")
