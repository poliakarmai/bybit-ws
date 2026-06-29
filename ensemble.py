"""
ensemble.py — Ансамбль ML-моделей (Фаза 5.6).

Взвешенное голосование: RF (5.1) + LSTM (5.4) + RL (5.5).
Каждая модель даёт оценку 0-1, ансамбль вычисляет взвешенное среднее.

Использование:
    from .ensemble import ensemble_should_enter
    enter, conf, details = ensemble_should_enter(signal_data, market_state)
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

DATA_DIR = Path.home() / '.local' / 'share' / 'bybit-ws'
WEIGHTS_PATH = DATA_DIR / 'ensemble_weights.json'

# Веса по умолчанию (равные)
DEFAULT_WEIGHTS = {
    'rf': 0.34,    # ML Gate (RandomForest)
    'lstm': 0.33,  # LSTM-режим
    'rl': 0.33,    # RL-агент (DQN)
}

# Порог для входа
THRESHOLD = 0.45  # если взвешенный score >= порога → ENTER


def _get_weights():
    """Загрузить веса ансамбля (с авто-коррекцией при недоступности моделей)."""
    weights = dict(DEFAULT_WEIGHTS)

    if WEIGHTS_PATH.exists():
        try:
            with open(WEIGHTS_PATH) as f:
                saved = json.load(f)
            weights.update(saved)
        except Exception:
            pass

    # Проверка доступности моделей
    try:
        from .ml_scorer import MODEL_PATH as RF_PATH
    except ImportError:
        from ml_scorer import MODEL_PATH as RF_PATH
    try:
        from .lstm_regime import MODEL_PATH as LSTM_PATH
    except ImportError:
        from lstm_regime import MODEL_PATH as LSTM_PATH
    try:
        from .rl_agent import MODEL_PATH as RL_PATH
    except ImportError:
        from rl_agent import MODEL_PATH as RL_PATH

    available = {
        'rf': RF_PATH.exists(),
        'lstm': LSTM_PATH.exists(),
        'rl': RL_PATH.exists(),
    }

    # Перераспределяем веса недоступных моделей
    active = [k for k, v in available.items() if v]
    if active and len(active) < 3:
        total = sum(weights[k] for k in active)
        if total > 0:
            for k in active:
                weights[k] = weights[k] / total
        for k in ('rf', 'lstm', 'rl'):
            if k not in active:
                weights[k] = 0.0

    return weights, available


def _rf_score(signal_data: dict) -> tuple[float, str]:
    """ML Gate: pass=1.0, fail=0.0."""
    try:
        from .ml_scorer import ml_gate_pass
        passed, ml_prob = ml_gate_pass(signal_data)
        score = 1.0 if passed else 0.0
        detail = f'RF: {"PASS" if passed else "FAIL"} (prob={ml_prob:.2f})' if ml_prob else 'RF: PASS (no model)'
        return score, detail
    except Exception as e:
        return 0.5, f'RF: error ({e}) — нейтрально'


def _lstm_score(market_state: dict) -> tuple[float, str]:
    """LSTM-режим → агрессия, нормализованная в 0-1."""
    try:
        from .lstm_regime import get_cached_prediction
        data = get_cached_prediction()
        if not data:
            # Fallback to heuristic — импорт на уровне модуля
            try:
                from .regime import get_cached_regime
                data = get_cached_regime()
            except ImportError:
                return 0.5, 'LSTM: no data, regime unavailable'

        regime = data.get('regime', 'NEUTRAL')
        confidence = data.get('confidence', 50) / 100

        # Маппинг режима → скор
        regime_scores = {
            'TRENDING_UP': 0.9,
            'LOW_VOL': 0.7,
            'RANGING': 0.6,
            'NEUTRAL': 0.5,
            'TRENDING_DOWN': 0.3,
            'CHOPPY': 0.2,
            'HIGH_VOL': 0.15,
        }
        base = regime_scores.get(regime, 0.5)
        score = base * (0.5 + 0.5 * confidence)  # смешиваем с confidence
        detail = f'LSTM: {regime} (conf={confidence:.0%}, score={score:.2f})'
        return score, detail
    except Exception as e:
        return 0.5, f'LSTM: error ({e})'


def _rl_score(state_dict: dict) -> tuple[float, str]:
    """RL-агент: ENTER=1.0, WAIT=0.0, SKIP=0.0."""
    try:
        from .rl_agent import should_enter
        enter, reason = should_enter(state_dict, 'LONG')

        if 'ENTER_LONG' in reason:
            score = 1.0
        else:
            # WAIT и SKIP — не входить
            score = 0.0

        # Извлекаем confidence из reason
        conf_str = reason.split('conf=')[-1].rstrip(')') if 'conf=' in reason else '0.5'
        try:
            conf = float(conf_str)
        except ValueError:
            conf = 0.5
        score = score * (0.5 + 0.5 * conf)

        detail = f'RL: {reason}'
        return score, detail
    except Exception as e:
        return 0.0, f'RL: error ({e})'


def ensemble_should_enter(signal_data: dict, market_state: Optional[dict] = None) -> tuple[bool, float, dict]:
    """
    Ансамбль: взвешенное голосование RF + LSTM + RL.

    Args:
        signal_data: признаки сигнала (для RF и RL)
        market_state: состояние рынка (для LSTM), опционально

    Returns:
        should_enter: bool — входить или нет
        confidence: float — уверенность ансамбля (0-1)
        details: dict — вклады каждой модели
    """
    weights, available = _get_weights()
    market_state = market_state or {}

    scores = {}
    reasons = {}

    # RF
    rf_s, rf_r = _rf_score(signal_data)
    scores['rf'] = rf_s
    reasons['rf'] = rf_r

    # LSTM
    lstm_s, lstm_r = _lstm_score(market_state)
    scores['lstm'] = lstm_s
    reasons['lstm'] = lstm_r

    # RL
    rl_state = {
        'bb_pct': signal_data.get('bb_pos', 50),
        'bb_width': signal_data.get('bb_width', 10),
        'rsi': 50,
        'atr_pct': 2.0,
        'vol_ratio': 1.0,
        'funding': signal_data.get('funding', 0.0),
        'mtf_confluence': market_state.get('mtf_confluence', 2),
        'days_since_entry': market_state.get('days_since_entry', 0),
        'regime': market_state.get('regime', 'NEUTRAL'),
        'score': signal_data.get('score', 25),
        'daily_return': market_state.get('daily_return', 0.0),
    }
    rl_s, rl_r = _rl_score(rl_state)
    scores['rl'] = rl_s
    reasons['rl'] = rl_r

    # Взвешенное среднее
    weighted = sum(scores[k] * weights[k] for k in scores)
    active_models = [k for k in scores if available[k]]
    avg_conf = sum(scores[k] for k in active_models) / max(1, len(active_models))

    should_enter = weighted >= THRESHOLD

    details = {
        'weighted_score': round(weighted, 3),
        'threshold': THRESHOLD,
        'weights': {k: round(v, 2) for k, v in weights.items()},
        'scores': {k: round(v, 3) for k, v in scores.items()},
        'reasons': reasons,
        'available': available,
        'votes': len(active_models),
    }

    return should_enter, round(avg_conf, 3), details


def update_weights(rf_weight=None, lstm_weight=None, rl_weight=None):
    """Обновить веса ансамбля (сохраняются в JSON)."""
    weights = dict(DEFAULT_WEIGHTS)
    if WEIGHTS_PATH.exists():
        try:
            with open(WEIGHTS_PATH) as f:
                weights.update(json.load(f))
        except Exception:
            pass

    if rf_weight is not None:
        weights['rf'] = rf_weight
    if lstm_weight is not None:
        weights['lstm'] = lstm_weight
    if rl_weight is not None:
        weights['rl'] = rl_weight

    # Нормализация
    total = sum(weights.values())
    if total > 0:
        weights = {k: v / total for k, v in weights.items()}

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(WEIGHTS_PATH, 'w') as f:
        json.dump(weights, f, indent=2)

    return weights


# ── CLI ─────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == 'test':
        signal = {
            'score': 35, 'bb_pos': 15, 'bb_width': 8,
            'price': 50000, 'lower_bb': 49000, 'upper_bb': 52000,
            'middle_bb': 50500, 'entry': 48000,
            'timeframe': 'D', 'mode': 'long',
            'funding': -0.003,
        }
        market = {'regime': 'NEUTRAL', 'mtf_confluence': 2, 'days_since_entry': 5}

        enter, conf, details = ensemble_should_enter(signal, market)
        print(f'Enter: {enter} (conf={conf:.2f})')
        print(f'Score: {details["weighted_score"]} / {details["threshold"]}')
        for k, v in details['reasons'].items():
            print(f'  {v}')
        print(f'Weights: {details["weights"]}')
    elif len(sys.argv) > 1 and sys.argv[1] == 'weights':
        w, a = _get_weights()
        print(json.dumps({'weights': {k: round(v, 2) for k, v in w.items()}, 'available': a}, indent=2))
    else:
        print("Usage: ensemble.py [test|weights]")
