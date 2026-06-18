"""
rl_agent.py — RL-агент для оптимизации входов (Фаза 5.5).

DQN (Deep Q-Network) на Stable-Baselines3.
Обучается на исторических данных выбирать: ENTER / WAIT / SKIP.

Использование:
  python rl_agent.py --train          # обучить агента
  python rl_agent.py --predict STATE  # предсказать действие (13 чисел через запятую)
  python rl_agent.py --info           # информация о модели
"""

import json
import os
import pickle
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import torch
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

DATA_DIR = Path.home() / '.local' / 'share' / 'bybit-ws'
MODEL_DIR = DATA_DIR / 'models'
MODEL_PATH = MODEL_DIR / 'rl_agent.zip'
META_PATH = MODEL_DIR / 'rl_agent_meta.json'

N_ACTIONS = 3
ACTION_NAMES = ['SKIP', 'ENTER_LONG', 'WAIT']


def train(symbols=('BTCUSDT', 'ETHUSDT'), days=365, timesteps=100_000):
    """Обучить DQN-агента на исторических данных."""
    if not HAS_TORCH:
        print("❌ PyTorch не установлен")
        return None

    from stable_baselines3 import DQN
    from stable_baselines3.common.callbacks import EvalCallback
    from stable_baselines3.common.monitor import Monitor

    try:
        from .rl_env import TradingEnv, fetch_kline_data
    except ImportError:
        from rl_env import TradingEnv, fetch_kline_data

    print("📡 Загрузка исторических данных...")
    all_data = fetch_kline_data(symbols, days)
    if not all_data:
        print("❌ Не удалось загрузить данные")
        return None

    summary = ', '.join(f'{d["symbol"]}: {len(d["closes"])}д' for d in all_data)
    print(f"   Загружено: {summary}")

    # Создаём среды
    def make_env(data, idx):
        def _init():
            env = TradingEnv(data, tp_pct=5.0, sl_pct=3.0, max_hold_days=14)
            return Monitor(env)
        return _init

    envs = [make_env(data, i) for i, data in enumerate(all_data)]
    train_env = envs[0]()
    eval_env = envs[0]() if len(envs) == 1 else envs[-1]()

    # Коллбэк для оценки
    eval_callback = EvalCallback(
        eval_env,
        best_model_save_path=str(MODEL_DIR),
        log_path=str(DATA_DIR),
        eval_freq=5000,
        n_eval_episodes=5,
        deterministic=True,
    )

    # DQN с tuned гиперпараметрами
    model = DQN(
        'MlpPolicy',
        train_env,
        learning_rate=0.0005,
        buffer_size=50000,
        learning_starts=5000,
        batch_size=64,
        tau=0.005,
        gamma=0.99,
        train_freq=4,
        gradient_steps=1,
        target_update_interval=1000,
        exploration_fraction=0.2,
        exploration_final_eps=0.05,
        max_grad_norm=1.0,
        policy_kwargs={'net_arch': [128, 64]},
        verbose=0,
        tensorboard_log=None,
    )

    print(f"\n🏋️ Обучение DQN ({timesteps} шагов)...")

    # Обучаем на данных каждого символа
    total_timesteps = 0
    for epoch in range(3):  # 3 прохода по всем символам
        for i, data in enumerate(all_data):
            if total_timesteps >= timesteps:
                break
            env = TradingEnv(data, tp_pct=5.0, sl_pct=3.0, max_hold_days=14)
            model.set_env(env)
            steps_this = min(timesteps // (len(all_data) * 3), timesteps - total_timesteps)
            model.learn(
                total_timesteps=steps_this,
                callback=eval_callback,
                reset_num_timesteps=False,
                progress_bar=False,
            )
            total_timesteps += steps_this
            env.close()
            print(f"   {data['symbol']}: {total_timesteps}/{timesteps} шагов")

    # Сохраняем
    os.makedirs(MODEL_DIR, exist_ok=True)
    model.save(str(MODEL_PATH))

    # Метаданные
    with open(META_PATH, 'w') as f:
        json.dump({
            'model_type': 'DQN',
            'n_actions': N_ACTIONS,
            'action_names': ACTION_NAMES,
            'timesteps': total_timesteps,
            'symbols': list(symbols),
            'days': days,
            'trained_at': datetime.now().isoformat(),
            'n_features': 13,
        }, f, indent=2)

    print(f"\n✅ Модель сохранена: {MODEL_PATH}")
    return model


def predict(state: np.ndarray) -> tuple[int, str, float]:
    """
    Предсказать оптимальное действие.
    Возвращает (action_id, action_name, confidence).
    """
    if not MODEL_PATH.exists():
        return 0, 'SKIP', 0.0

    try:
        from stable_baselines3 import DQN

        model = DQN.load(str(MODEL_PATH))
        state = np.array(state, dtype=np.float32).reshape(1, -1)

        # Убедимся что 13 признаков
        if state.shape[1] != 13:
            state = np.pad(state, ((0, 0), (0, max(0, 13 - state.shape[1]))), mode='constant')[:, :13]

        action, _states = model.predict(state, deterministic=True)
        action = int(action[0])

        # Q-values для confidence
        q_values = model.q_net.forward(torch.tensor(state, dtype=torch.float32))
        best_q = float(q_values[0, action].item())
        other_q = max(float(q_values[0, i].item()) for i in range(N_ACTIONS) if i != action)
        confidence = 1.0 / (1.0 + np.exp(-(best_q - other_q)))  # softmax-like confidence

        return action, ACTION_NAMES[action], round(confidence, 3)

    except Exception as e:
        print(f'[RL] predict error: {e}', file=sys.stderr)
        return 0, 'SKIP', 0.0


def should_enter(state_dict: dict, direction: str = 'LONG') -> tuple[bool, str]:
    """
    Высокоуровневый API: стоит ли входить в сигнал?
    Принимает словарь с признаками сигнала и направление (LONG/SHORT).
    Возвращает (enter: bool, reason: str).
    """
    if not MODEL_PATH.exists():
        return True, 'RL модель не обучена — полагаемся на эвристику'

    try:
        feat = _dict_to_features(state_dict)
        action, name, conf = predict(feat)

        target_action = 1  # ENTER_LONG (единственное торговое действие)

        if action == target_action:
            return True, f'RL: {name} (conf={conf:.2f})'
        elif action == 2:  # WAIT
            return False, f'RL: WAIT (conf={conf:.2f}) — подождать'
        else:  # SKIP или противоположное направление
            return False, f'RL: {name} (conf={conf:.2f}) — пропустить'
    except Exception as e:
        return True, f'RL error: {e} — входим по эвристике'


def _dict_to_features(d: dict) -> np.ndarray:
    """Конвертировать словарь сигнала в массив признаков (13)."""
    bb_pct = float(d.get('bb_pct', 50))
    bb_width = float(d.get('bb_width', 10))
    rsi = float(d.get('rsi', 50))
    atr_pct = float(d.get('atr_pct', 2))
    vol_ratio = float(d.get('vol_ratio', 1.0))
    funding = float(d.get('funding', 0)) / 0.1
    mtf = float(d.get('mtf_confluence', 1.5)) / 3.0
    days_since = min(1.0, float(d.get('days_since_entry', 0)) / 30)

    # Режим
    regime = d.get('regime', 'NEUTRAL').upper()
    regime_map = {
        'TRENDING_UP': [1, 0, 0], 'TRENDING_DOWN': [0, 1, 0],
        'HIGH_VOL': [0, 0, 1], 'CHOPPY': [0, 0, 1],
    }
    reg = regime_map.get(regime, [0, 0, 0])

    score = min(1.0, float(d.get('score', 25)) / 50.0)
    daily_ret = float(d.get('daily_return', 0)) / 10.0

    return np.array([
        bb_pct / 100, bb_width / 20, rsi / 100, atr_pct / 10,
        vol_ratio / 5, funding, mtf, days_since,
        *reg, score, daily_ret,
    ], dtype=np.float32)


# ── CLI ─────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='RL Agent for Entry Optimization')
    parser.add_argument('--train', action='store_true', help='Обучить агента')
    parser.add_argument('--predict', type=str, help='Предсказать действие (13 чисел через запятую)')
    parser.add_argument('--info', action='store_true', help='Инфо о модели')
    args = parser.parse_args()

    if args.train:
        train()
    elif args.predict:
        state = np.array([float(x) for x in args.predict.split(',')], dtype=np.float32)
        action, name, conf = predict(state)
        print(f'Action: {name} (id={action}, conf={conf:.3f})')
    elif args.info:
        if META_PATH.exists():
            with open(META_PATH) as f:
                info = json.load(f)
            print(json.dumps(info, indent=2))
            if MODEL_PATH.exists():
                size_kb = MODEL_PATH.stat().st_size / 1024
                print(f'\nМодель: {MODEL_PATH} ({size_kb:.0f} KB)')
        else:
            print('Модель не обучена. Запустите --train')
    else:
        parser.print_help()
