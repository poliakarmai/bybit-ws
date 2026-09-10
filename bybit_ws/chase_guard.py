"""Анти-chasing guard: блок входа, если монета уже разогналась.

Детерминированная чистая функция — все данные передаются аргументом.
Не бросает исключений ни при каком входе.
"""

from __future__ import annotations

from typing import Sequence


def is_chasing(closes: Sequence[float] | None, pct: float = 3.0, lookback: int = 5) -> bool:
    """True, если рост close-цены за последние lookback свечей превышает pct%.

    closes — список/последовательность close-цен (float), последний элемент =
    последняя закрытая свеча, порядок хронологический (старые → новые).

    Рост считается так: base = closes[-lookback-1]; если base > 0, то
    (closes[-1] - base) / base * 100 > pct  →  True, иначе False.

    Толерантность (никогда не бросать, вернуть False):
      - closes is None / пустой / короче lookback+1 элементов → False
      - base == 0 или не-числовой → False
      - pct <= 0 или lookback <= 0 → False (механика выключена)
    """
    try:
        # Параметры выключены — механика не работает
        if pct <= 0 or lookback <= 0:
            return False

        # Недостаточно данных
        if closes is None:
            return False
        # Приводим к list чтобы безопасно работать с индексами
        closes_list = list(closes)
        if len(closes_list) < lookback + 1:
            return False

        # Базовая цена — lookback свечей назад от последней
        base = closes_list[-lookback - 1]
        last = closes_list[-1]

        # Проверка base на валидность
        if not isinstance(base, (int, float)):
            return False
        if base <= 0:
            return False

        # Проверка last на валидность
        if not isinstance(last, (int, float)):
            return False

        # Расчёт роста в процентах
        growth_pct = (last - base) / base * 100.0
        return growth_pct > pct

    except Exception:
        # Любая непредвиденная ситуация — безопасный выход
        return False
