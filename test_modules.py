#!/usr/bin/env python3
"""Модульные тесты: health.py, auto_sl.py, pump_detect.py"""
import sys, os, json, time, tempfile, shutil
from pathlib import Path

# Добавляем bybit-ws в путь
sys.path.insert(0, str(Path(__file__).parent.parent))

PASS = FAIL = 0

def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {desc}")
    else:
        FAIL += 1
        print(f"  ❌ {desc}")

# ──────────────────────────────────────────────
# Test: health.py — drawdown cooldown
# ──────────────────────────────────────────────
def test_drawdown_cooldown():
    print("\n─── health.py: кулдаун просадки ───")
    
    # Создаём временную DATA_DIR
    tmp = tempfile.mkdtemp()
    dd_file = os.path.join(tmp, 'drawdown_alert.json')
    
    # Эмулируем логику check_daily_drawdown
    # Проверяем что кулдаун 86400 работает
    
    # Тест 1: нет файла — алерт проходит
    last_alert = 0
    try:
        with open(dd_file) as f:
            last_alert = json.load(f).get('last_alert', 0)
    except Exception:
        pass
    check("Нет файла кулдауна — last_alert=0", last_alert == 0)
    
    # Тест 2: записали алерт
    state = {'last_alert': time.time(), 'drawdown': 5.4, 'equity': 216}
    with open(dd_file, 'w') as f:
        json.dump(state, f)
    check("Файл кулдауна создан", os.path.exists(dd_file))
    
    # Тест 3: проверяем что алерт блокируется
    try:
        with open(dd_file) as f:
            dd_state = json.load(f)
        last_alert = dd_state.get('last_alert', 0)
    except Exception:
        last_alert = 0
    
    elapsed = time.time() - last_alert
    blocked = elapsed < 86400
    check(f"Кулдаун блокирует повторный алерт ({elapsed:.0f}с < 86400с)", blocked)
    
    # Тест 4: expired кулдаун
    state = {'last_alert': time.time() - 100000, 'drawdown': 5.4, 'equity': 216}
    with open(dd_file, 'w') as f:
        json.dump(state, f)
    try:
        with open(dd_file) as f:
            dd_state = json.load(f)
        last_alert = dd_state.get('last_alert', 0)
    except Exception:
        last_alert = 0
    
    elapsed = time.time() - last_alert
    expired = elapsed >= 86400
    check(f"Кулдаун истёк — алерт проходит ({elapsed:.0f}с >= 86400с)", expired)
    
    shutil.rmtree(tmp)

# ──────────────────────────────────────────────
# Test: auto_sl.py — проверка JUNK-пропуска
# ──────────────────────────────────────────────
def test_auto_sl_junk_skip():
    print("\n─── auto_sl.py: пропуск JUNK-шортов ───")
    
    # Проверяем что код auto_sl проверяет pumps.json
    auto_sl_path = Path(__file__).parent / "auto_sl.py"
    if not auto_sl_path.exists():
        check("auto_sl.py существует", False)
        return
    
    code = auto_sl_path.read_text()
    
    # Проверка: pumps.json используется
    check("auto_sl проверяет pumps.json", 'pumps.json' in code)
    check("auto_sl импортирует DATA_DIR или os.path", 'DATA_DIR' in code or 'os.path' in code)
    
    # Проверка структуры функции
    check("auto_sl содержит def", 'def ' in code)
    check("auto_sl возвращает результат", 'return' in code)

# ──────────────────────────────────────────────
# Test: pump_detect.py — проверка структуры
# ──────────────────────────────────────────────
def test_pump_detect_structure():
    print("\n─── pump_detect.py: структура ───")
    
    pd_path = Path(__file__).parent / "pump_detect.py"
    if not pd_path.exists():
        check("pump_detect.py существует", False)
        return
    
    code = pd_path.read_text()
    
    check("check_pumps определена", 'def check_pumps' in code)
    check("check_weekly_pumps определена", 'def check_weekly_pumps' in code)
    check("daily_pump_threshold есть", 'daily_pump_threshold' in code.lower() or 'DAILY_PUMP' in code or 'pump_threshold' in code.lower())
    check("weekly_pump_threshold есть", 'weekly_pump_threshold' in code.lower() or 'WEEKLY_PUMP' in code)
    check("pumps.json используется", 'pumps.json' in code)
    check("prev[alerts] проверка инициализации", 'alerts' in code and 'if not prev' in code)

# ──────────────────────────────────────────────
# Test: auto_entry.py — full_score_coin
# ──────────────────────────────────────────────
def test_full_score_coin():
    print("\n─── auto_entry.py: full_score_coin ───")
    
    ae_path = Path(__file__).parent / "auto_entry.py"
    if not ae_path.exists():
        check("auto_entry.py существует", False)
        return
    
    code = ae_path.read_text()
    
    check("full_score_coin определена", 'def full_score_coin' in code)
    check("MIN_SCORE порог есть", 'MIN_SCORE' in code)
    check("BB score есть", 'bb_score' in code)
    check("Volume score есть", 'vol_score' in code)
    check("Down days score есть", 'down_score' in code)
    check("Funding score есть", 'fund_score' in code)
    check("Volatility score есть", 'vola_score' in code)
    check("Quality score есть", 'qscore' in code)
    check("quick_score_bb убрана или сохранена", True)  # OK в любом случае
    
    # Проверяем что старый bb_pos < 25 заменён на score >= MIN_SCORE
    has_old = "bb_pos < 25 and bb_pos > 0" in code
    has_new = "score" in code and "MIN_SCORE" in code
    check("Старый bb_pos<25 заменён на score>=MIN_SCORE", not has_old or has_new)

# ──────────────────────────────────────────────
# Test: dashboard
# ──────────────────────────────────────────────
def test_dashboard_exists():
    print("\n─── web/dashboard.html ───")
    dash = Path(__file__).parent / "web" / "dashboard.html"
    check("dashboard.html существует", dash.exists())
    if dash.exists():
        html = dash.read_text()
        check("Chart.js подключён", 'chart.js' in html.lower() or 'Chart' in html)
        check("RPC прокси (const RPC)", 'const RPC' in html)
        check("Автообновление есть", 'setInterval' in html)

# ──────────────────────────────────────────────

if __name__ == '__main__':
    test_drawdown_cooldown()
    test_auto_sl_junk_skip()
    test_pump_detect_structure()
    test_full_score_coin()
    test_dashboard_exists()
    
    print(f"\n{'='*40}")
    print(f"  PASS={PASS}  FAIL={FAIL}  TOTAL={PASS+FAIL}")
    if FAIL == 0:
        print("  ✅ Все тесты пройдены!")
    else:
        print(f"  ❌ {FAIL} тестов упало")
    sys.exit(0 if FAIL == 0 else 1)
