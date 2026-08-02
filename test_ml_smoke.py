#!/usr/bin/env python3
"""Smoke-тесты ML-конвейера: ensemble + rl_agent."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILS = 0

# ── 1. Ensemble smoke ──
print('🧠 Ensemble smoke tests')
try:
    from ensemble import ensemble_should_enter

    signal = {'score': 40, 'bb_pos': 15, 'bb_width': 12, 'price': 100.0,
              'lower_bb': 95.0, 'upper_bb': 110.0, 'middle_bb': 102.0,
              'entry': 96.0, 'timeframe': 'D', 'mode': 'long', 'funding': 0.0001}
    market = {'regime': 'TRENDING_UP', 'regime_conf': 69, 'mtf_confluence': 3,
              'days_since_entry': 0, 'daily_return': 0.0}

    enter, conf, details = ensemble_should_enter(signal, market)
    assert isinstance(enter, bool), 'enter must be bool'
    assert isinstance(conf, float), 'conf must be float'
    assert isinstance(details, dict), 'details must be dict'
    assert 'weighted_score' in details, 'missing weighted_score'
    assert 'threshold' in details, 'missing threshold'
    assert 'votes' in details, 'missing votes'
    assert 0 <= conf <= 1, f'conf {conf} out of range'
    print(f'  ✅ ensemble_should_enter: enter={enter} conf={conf:.2f} score={details["weighted_score"]:.2f}')
except (ImportError, NameError):
    print('  ⏭️ ensemble smoke skipped (no torch/stable_baselines3)')
except Exception as e:
    print(f'  ❌ ensemble smoke: {e}')
    FAILS += 1

# ── 2. RL agent smoke ──
print('🤖 RL agent smoke tests')
try:
    from rl_agent import should_enter, predict, _dict_to_features
    import numpy as np

    state = {'bb_pct': 15.0, 'bb_width': 10.0, 'rsi': 30.0, 'atr_pct': 2.5,
             'vol_ratio': 1.2, 'funding': 0.0001, 'mtf_confluence': 3,
             'days_since_entry': 0, 'regime_1hot': [1, 0, 0, 0, 0], 'score_norm': 0.6}

    enter, reason = should_enter(state, 'LONG')
    assert isinstance(enter, bool), 'enter must be bool'
    assert isinstance(reason, str), 'reason must be str'
    print(f'  ✅ should_enter: enter={enter} reason={reason[:60]}')

    feat = _dict_to_features(state)
    assert isinstance(feat, np.ndarray), 'features must be ndarray'
    assert feat.shape == (13,), f'Expected (13,), got {feat.shape}'
    print(f'  ✅ _dict_to_features: shape={feat.shape}')

    if hasattr(predict, '__call__'):
        action, name, conf = predict(feat)
        assert action in (0, 1, 2), f'Invalid action: {action}'
        print(f'  ✅ predict: action={action} ({name}) conf={conf:.2f}')
except Exception as e:
    print(f'  ❌ rl_agent smoke: {e}')
    FAILS += 1

# ── 3. HMAC smoke ──
print('🔐 HMAC smoke tests')
try:
    from ml_scorer import _sign_file, _verify_file
    from pathlib import Path
    import tempfile

    with tempfile.NamedTemporaryFile(suffix='.test', delete=False) as f:
        f.write(b'test model data')
        tmp = Path(f.name)
    hmac_path = str(tmp) + '.hmac'
    _sign_file(tmp)
    assert os.path.exists(hmac_path), 'HMAC not created'
    assert _verify_file(tmp), 'HMAC verification failed'
    os.unlink(tmp)
    os.unlink(hmac_path)
    print('  ✅ HMAC sign+verify OK')
except Exception as e:
    print(f'  ❌ HMAC smoke: {e}')
    FAILS += 1

status = '✅ ALL' if FAILS == 0 else f'❌ {FAILS} FAILED'
print(f'\n{status} ML smoke tests')
sys.exit(0 if FAILS == 0 else 1)
