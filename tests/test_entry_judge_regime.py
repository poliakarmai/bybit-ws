"""Регресс: судья входа не должен блокировать LONG по залипшему regime.

LSTM regime залип на TRENDING_DOWN (ложный сигнал, не рыночное состояние) — см.
skill bybit-ws-maintenance § «Сам LSTM-режим МЁРТВ». Решение пользователя: LONG не
блокируется режимом (get_regime_strategy). Раньше prompt судьи содержал
'Regime: {regime}', и LLM резал все LONG как 'side vs regime contradiction',
отменяя код-гейт. Поле убрано — судья судит сигнал, а не режим.

    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_entry_judge_regime.py -q
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_prompt_has_no_regime_field():
    src = (ROOT / "entry_judge.py").read_text()
    assert "Regime:" not in src


def test_band_gap_hint_still_present():
    # dip-buy лимитка ниже BB lower — дизайн, судья не должен рубить за это вслепую
    src = (ROOT / "entry_judge.py").read_text()
    assert "do not reject for that alone" in src
