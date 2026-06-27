"""
Entry Judge — pre-entry валидация сигнала через cross-model judge (Nemotron).
Вызывается перед ордером: если verdict=revise → пропускаем вход.

Feature flag: BYBIT_ENTRY_JUDGE_ENABLED=1
Зависимость: ~/.hermes/scripts/cross-model-judge.py + OPENROUTER_API_KEY
"""

import json
import os
import subprocess
import time
from typing import Optional

JUDGE_SCRIPT = os.path.expanduser("~/.hermes/scripts/cross-model-judge.py")
JUDGE_TIMEOUT = 15  # секунд на вызов судьи
JUDGE_ENABLED = os.environ.get("BYBIT_ENTRY_JUDGE_ENABLED", "0") == "1"

# Символы которые НИКОГДА не судим (чёрный список)
JUDGE_BLACKLIST = {
    # Слишком волатильные — судья не успевает
}

# Минимальный score для вызова судьи (не тратим токены на слабые сигналы)
JUDGE_MIN_SCORE = 20


def judge_entry(
    symbol: str,
    side: str,
    score: int,
    bb_pos: float,
    entry_price: float,
    sl_price: Optional[float] = None,
    tp_price: Optional[float] = None,
    funding_rate: float = 0.0,
    bb_lower: float = 0.0,
    bb_upper: float = 0.0,
    mtf_confluence: int = 0,
    regime: str = "NEUTRAL",
) -> dict:
    """
    Прогнать сигнал через cross-model judge.

    Returns:
        {"verdict": "pass"|"revise", "blocking_issues": [...], "confidence": 0.0-1.0}
    """
    if not JUDGE_ENABLED:
        return {"verdict": "pass", "blocking_issues": [], "confidence": 1.0, "notes": "judge disabled"}

    if symbol in JUDGE_BLACKLIST:
        return {"verdict": "pass", "blocking_issues": [], "confidence": 1.0, "notes": "blacklisted"}

    if score < JUDGE_MIN_SCORE:
        return {"verdict": "pass", "blocking_issues": [], "confidence": 1.0, "notes": "low score skip"}

    if not os.path.exists(JUDGE_SCRIPT):
        return {
            "verdict": "revise",
            "blocking_issues": ["Judge script not found"],
            "confidence": 0.0,
            "notes": "no script",
        }

    # Формируем контекст для судьи
    sl_info = f"SL=${sl_price:.4f}" if sl_price else "SL=not set"
    tp_info = f"TP=${tp_price:.4f}" if tp_price else "TP=not set"
    mtf_info = f"MTF confluence: {mtf_confluence}/3" if mtf_confluence else ""

    signal_context = f"""Signal: {side} {symbol}
Entry: ${entry_price:.4f}
{sl_info}  {tp_info}
Score: {score}/50 (BB position: {bb_pos:.0f}%)
BB: lower=${bb_lower:.4f} upper=${bb_upper:.4f}
Funding: {funding_rate*100:.4f}%
Regime: {regime}
{mtf_info}

Analyze this trading signal. Is this a reasonable entry?
Check:
1. Is entry near BB lower band (for LONG) / upper band (for SHORT)?
2. Is SL wide enough (at least 2-3% from entry)?
3. Is the score adequate (≥25 is decent, ≥35 is strong)?
4. Any red flags (negative funding, extreme volatility)?
"""

    try:
        proc = subprocess.run(
            ["python3", JUDGE_SCRIPT, "--mode", "general", "--json", "-"],
            input=signal_context,
            capture_output=True,
            text=True,
            timeout=JUDGE_TIMEOUT,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            result = json.loads(proc.stdout)
            return result
        else:
            # Судья не ответил или ошибка — БЛОКИРУЕМ вход (fail-closed, 27.06)
            return {
                "verdict": "revise",
                "blocking_issues": ["Judge script error"],
                "confidence": 0.0,
                "notes": f"judge error: {proc.stderr[:100] if proc.stderr else 'no output'}",
            }
    except subprocess.TimeoutExpired:
        return {
            "verdict": "revise",
            "blocking_issues": [],
            "confidence": 0.0,
            "notes": "judge timeout",
        }
    except Exception as e:
        return {
            "verdict": "revise",
            "blocking_issues": [],
            "confidence": 0.0,
            "notes": f"judge error: {e}",
        }


def should_enter(
    symbol: str,
    side: str,
    score: int,
    bb_pos: float,
    entry_price: float,
    **kwargs,
) -> tuple[bool, str]:
    """
    Быстрый вызов: True если можно входить, False если судья заблокировал.

    Returns: (can_enter, reason)
    """
    if not JUDGE_ENABLED:
        return True, "judge disabled"

    verdict = judge_entry(symbol, side, score, bb_pos, entry_price, **kwargs)

    if verdict.get("verdict") == "pass":
        return True, f"judge pass (confidence: {verdict.get('confidence', 0):.0%})"

    issues = verdict.get("blocking_issues", ["unknown"])
    return False, f"judge blocked: {'; '.join(issues[:3])}"


# Кеш результатов (на время жизни процесса, TTL=60s)
_judge_cache: dict[str, tuple[float, dict]] = {}
_JUDGE_CACHE_TTL = 60


def judge_entry_cached(
    symbol: str,
    side: str,
    score: int,
    bb_pos: float,
    entry_price: float,
    **kwargs,
) -> dict:
    """Судья с кешированием результатов на 60 секунд."""
    cache_key = f"{symbol}:{side}"
    now = time.time()

    if cache_key in _judge_cache:
        ts, cached_result = _judge_cache[cache_key]
        if now - ts < _JUDGE_CACHE_TTL:
            return cached_result

    result = judge_entry(symbol, side, score, bb_pos, entry_price, **kwargs)
    _judge_cache[cache_key] = (now, result)
    return result
