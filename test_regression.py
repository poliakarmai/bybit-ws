#!/usr/bin/env python3
"""Регрессионный щит — ловит scoping bugs, import errors, API-несовместимость.

Запускать перед КАЖДЫМ деплоем: python3 test_regression.py
Добавлено: 29.06.2026 — после бага _llm_failures в entry_judge.
"""

import sys, os, subprocess, tempfile, json
from pathlib import Path

PROJECT = Path(__file__).parent
PASS = FAIL = 0

def check(desc, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✅ {desc}")
    else:
        FAIL += 1; print(f"  ❌ {desc} {detail}")


# ═══════════════════════════════════════════════════
# LAYER 1: py_compile — синтаксис + scoping
# ═══════════════════════════════════════════════════
print("\n─── L1: py_compile (syntax + scoping) ───")
PY_FILES = sorted([f for f in PROJECT.glob('*.py') 
                   if f.name != 'test_regression.py' and not f.name.startswith('test_')])
for f in PY_FILES:
    try:
        subprocess.run([sys.executable, '-m', 'py_compile', str(f)],
                       capture_output=True, check=True)
        check(f.name, True)
    except subprocess.CalledProcessError as e:
        check(f.name, False, e.stderr.decode()[:200])


# ═══════════════════════════════════════════════════
# LAYER 2: import — круговые зависимости, ImportError
# ═══════════════════════════════════════════════════
print("\n─── L2: imports ───")
IMPORT_TESTS = {
    'entry_judge': 'from bybit_ws.entry_judge import judge_entry, should_enter',
    'funding_entry': 'from bybit_ws.funding_entry import check_funding_signals',
    'funding_rotation': 'from bybit_ws.funding_rotation import check_funding_rotation',
    'mean_revert': 'from bybit_ws.mean_revert import check_mean_revert',
    'bb_scalp': 'from bybit_ws.bb_scalp import check_scalp_signals',
    'auto_entry': None,  # приватные функции, тестируются в L3
    'auto_short': 'from bybit_ws.auto_short import check_auto_short',
    'auto_tp': 'from bybit_ws.auto_tp import auto_take_profit',
    'auto_sl': 'from bybit_ws.auto_sl import _get_tiers',
    'trailing_sl': 'from bybit_ws.trailing_sl import trailing_sl',
    'risk_manager': 'from bybit_ws.risk_manager import check_symbol_concentration',
    'correlation': 'from bybit_ws.correlation import max_corr_with_open',
    # orderbook_filter, volume_filter, time_exit, session_params — ещё не созданы как модули
    'position_sizing': 'from bybit_ws.position_sizing import margin_for_strategy',
    'api': 'from bybit_ws.api import bybit',
    'rpc': 'from bybit_ws.state_db import StateDB',
}

sys.path.insert(0, str(PROJECT.parent))
for name, imp in IMPORT_TESTS.items():
    if imp is None:
        continue
    try:
        exec(imp)
        check(name, True)
    except Exception as e:
        check(name, False, str(e)[:150])


# ═══════════════════════════════════════════════════
# LAYER 3: logic — баги уровня _llm_failures
# ═══════════════════════════════════════════════════
print("\n─── L3: logic smoke ───")

# 3a. entry_judge: проверяем что global не приводит к UnboundLocalError
try:
    from bybit_ws.entry_judge import judge_entry
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write('''#!/usr/bin/env python3
import sys, json
print(json.dumps({"verdict": "pass", "blocking_issues": [], "confidence": 1.0}))
''')
        judge_script = f.name
    
    # Временно подменяем JUDGE_SCRIPT
    import bybit_ws.entry_judge as ej
    old_script = ej.JUDGE_SCRIPT
    old_enabled = ej.JUDGE_ENABLED
    ej.JUDGE_SCRIPT = judge_script
    ej.JUDGE_ENABLED = True
    ej._llm_failures = 0
    ej._llm_disabled_until = 0
    
    result = judge_entry('BTCUSDT', 'Buy', 40, 15.0, 96000.0, sl_price=94000.0)
    check('entry_judge returns dict', isinstance(result, dict))
    check('entry_judge verdict pass', result.get('verdict') == 'pass',
          f"got: {result.get('verdict')}")
    
    ej.JUDGE_SCRIPT = old_script
    ej.JUDGE_ENABLED = old_enabled
    os.unlink(judge_script)
except Exception as e:
    check('entry_judge logic', False, str(e))

# 3b. funding_entry: проверяем что check_funding_signals не падает с TypeError
try:
    from bybit_ws.funding_entry import check_funding_signals
    from unittest.mock import patch, MagicMock
    with patch('bybit_ws.funding_entry.bybit') as mock_bybit:
        # Мокаем kline (BB) + ticker (funding)
        mock_bybit.side_effect = [
            {'result': {'list': [
                ['0', '0', '0', '0', '95.0', '0', '0'],  # close=95
                ['0', '0', '0', '0', '96.0', '0', '0'],
                ['0', '0', '0', '0', '97.0', '0', '0'],
                ['0', '0', '0', '0', '98.0', '0', '0'],
                ['0', '0', '0', '0', '99.0', '0', '0'],
            ]}},
            {'result': {'list': [{'fundingRate': '0.002'}], 'retCode': 0}},
        ]
        alerts, entries = check_funding_signals({})
        check('funding_entry no crash', True)
        check('funding_entry returns lists', isinstance(alerts, list) and isinstance(entries, list))
except Exception as e:
    check('funding_entry logic', False, str(e)[:150])

# 3c. funding_rotation: проверяем что не падает с 'list' has no 'get'
try:
    from bybit_ws.funding_rotation import check_funding_rotation
    from unittest.mock import patch
    with patch('bybit_ws.funding_rotation.bybit') as mock_bybit:
        mock_bybit.return_value = {
            'retCode': 0,
            'result': {'list': [
                {'symbol': 'BTCUSDT', 'fundingRate': '0.0001'},
                {'symbol': 'ETHUSDT', 'fundingRate': '-0.0001'},
            ]}
        }
        result = check_funding_rotation({})
        check('funding_rotation no crash', True)
        check('funding_rotation returns list', isinstance(result, list),
              f'got type={type(result).__name__}')
except Exception as e:
    check('funding_rotation logic', False, str(e)[:150])

# 3d. mean_revert: проверяем что не падает с 'list' has no 'strip'
try:
    from bybit_ws.mean_revert import check_mean_revert
    from unittest.mock import patch
    with patch('bybit_ws.mean_revert.bybit') as mock_bybit:
        mock_bybit.return_value = {
            'result': {'list': [
                ['0', '0', '0', '0', '95.0', '0', '0'],
                ['0', '0', '0', '0', '96.0', '0', '0'],
                ['0', '0', '0', '0', '97.0', '0', '0'],
                ['0', '0', '0', '0', '98.0', '0', '0'],
                ['0', '0', '0', '0', '99.0', '0', '0'],
            ]}
        }
        alerts, entries = check_mean_revert({})
        check('mean_revert no crash', True)
        check('mean_revert returns lists', isinstance(alerts, list) and isinstance(entries, list))
except Exception as e:
    check('mean_revert logic', False, str(e)[:150])

# 3e. bb_scalp
try:
    from bybit_ws.bb_scalp import check_scalp_signals
    from unittest.mock import patch
    with patch('bybit_ws.bb_scalp._get_kline') as mock_kline:
        # Генерируем 30 свечей: цена падает к lower BB, RSI должен быть < 35
        closes = [100.0] * 30  # плоский рынок — RSI ~50
        mock_kline.return_value = closes
        alerts, entries = check_scalp_signals({}, 10000.0)
        check('bb_scalp no crash', True)
        check('bb_scalp returns lists', isinstance(alerts, list) and isinstance(entries, list))
except Exception as e:
    check('bb_scalp logic', False, str(e)[:150])


# ═══════════════════════════════════════════════════
# LAYER 4: deploy.sh — dry-run
# ═══════════════════════════════════════════════════
print("\n─── L4: deploy.sh dry-run ───")
deploy = PROJECT / 'deploy.sh'
if deploy.exists():
    try:
        result = subprocess.run(['bash', '-n', str(deploy)], capture_output=True, check=True)
        check('deploy.sh syntax', True)
    except subprocess.CalledProcessError as e:
        check('deploy.sh syntax', False, e.stderr.decode()[:200])
else:
    check('deploy.sh', False, 'NOT FOUND')


# ═══════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"PASS: {PASS}  FAIL: {FAIL}")
if FAIL > 0:
    print("❌ REGRESSION FOUND — НЕ ДЕПЛОИТЬ!")
    sys.exit(1)
else:
    print("✅ ALL CLEAR — можно деплоить")
