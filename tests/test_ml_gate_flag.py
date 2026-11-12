"""ML_ENABLED=0 должен отключать ML Gate (не только ensemble).

Раньше ML Gate гейтился через ab_test (assign_variant fallback 'A' при
BYBIT_AB_ENABLED=0), а не через ML_ENABLED — ml_gate_pass вызывался в обход
флага и шумел 'joblib не установлен' каждый первый heavy-цикл процесса.

    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_ml_gate_flag.py -q
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_ml_gate_gated_by_ml_enabled():
    src = (ROOT / "auto_entry.py").read_text()
    assert "if ab_group == 'A' and ML_ENABLED:" in src
