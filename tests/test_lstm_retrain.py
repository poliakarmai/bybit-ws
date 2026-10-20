"""Тесты чистых хелперов переобучения LSTM-режима.

Проверяется ТОЛЬКО логика precompute_features / class_weights /
label_distribution из bybit_ws.lstm_regime. Ни сети, ни обучения,
ни загрузки модели здесь нет — саму тренировку запускает оркестратор:
    python3 -m bybit_ws.lstm_regime --train-candidate

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_lstm_retrain.py -q
    cd /home/openclaw/bybit-ws && python3 tests/test_lstm_retrain.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bybit_ws import lstm_regime as L  # noqa: E402

# _calc_features требует n >= 20 истории на бар, поэтому индексы 0..18 — None.
WARMUP = 20


def _synthetic_bars(n=25):
    """Плоские, но ненулевые бары: признаки вычислимы, делений на ноль нет."""
    closes = [100.0 + (i % 5) for i in range(n)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    volumes = [1000.0 + i for i in range(n)]
    return closes, highs, lows, volumes


def test_precompute_features_warmup_and_idempotent():
    closes, highs, lows, volumes = _synthetic_bars(25)
    feats = L.precompute_features(closes, highs, lows, volumes)

    # По одной записи на бар — длина совпадает с длиной истории.
    assert len(feats) == 25
    # Прогрев: первые WARMUP-1 баров без истории.
    assert feats[:WARMUP - 1] == [None] * (WARMUP - 1)
    assert all(f is None for f in feats[:19])
    # Первый бар с полной историей даёт вектор.
    assert feats[19] is not None
    assert len(feats[19]) == 8

    # Повторный вызов на том же входе даёт тот же результат (функция чистая).
    assert L.precompute_features(closes, highs, lows, volumes) == feats


def test_class_weights_inverse_frequency():
    # n=4 метки, k=2 представленных класса, counts = 3 и 1.
    # Формула n/(k*count): w0 = 4/(2*3) = 0.6667, w1 = 4/(2*1) = 2.0
    w = L.class_weights([0, 0, 0, 1], n_classes=2)

    assert len(w) == 2
    assert w[0] < w[1]
    assert abs(w[0] - 4.0 / 6.0) < 1e-12
    assert abs(w[1] - 4.0 / 2.0) < 1e-12
    # Инвариант формулы: w_i * count_i одинаков для всех классов (= n/k).
    assert abs(w[0] * 3 - w[1] * 1) < 1e-9


def test_class_weights_missing_class_is_zero():
    # Класс 2 отсутствует в метках → вес 0.0, чтобы не тянуть loss в пустоту.
    w = L.class_weights([0, 0, 1], n_classes=3)

    assert len(w) == 3
    assert w[2] == 0.0
    # n=3, k=2 (только классы 0 и 1 представлены): w0 = 3/(2*2) = 0.75,
    # w1 = 3/(2*1) = 1.5
    assert abs(w[0] - 0.75) < 1e-12
    assert abs(w[1] - 1.5) < 1e-12


def test_class_weights_single_class_is_degenerate_safe():
    # Все метки одного класса → делить на «k равное нулю» нельзя.
    # Контракт: 1.0 на каждый класс (равномерный loss).
    assert L.class_weights([1, 1, 1], 5) == [1.0] * 5
    # Пустой вход — тоже вырожденный случай, не должно быть ZeroDivisionError.
    assert L.class_weights([], 5) == [1.0] * 5


def test_label_distribution_counts_and_zero_classes():
    dist = L.label_distribution([0, 0, 0, 2])

    # Все классы присутствуют в словаре, даже не встречавшиеся в метках.
    assert set(dist) == set(L.CLASS_NAMES)
    assert len(dist) == len(L.CLASS_NAMES)
    assert dist[L.CLASS_NAMES[0]] == 3
    assert dist[L.CLASS_NAMES[2]] == 1
    # Ноль для классов без единой метки — иначе перекос выборки не виден.
    assert dist[L.CLASS_NAMES[1]] == 0
    assert dist[L.CLASS_NAMES[3]] == 0
    assert dist[L.CLASS_NAMES[4]] == 0
    assert sum(dist.values()) == 4


def test_label_distribution_majority_baseline_math():
    # Базовая линия «всегда самый частый класс» = max(count) / total.
    labels = [0, 0, 0, 0, 1, 2, 3, 4]
    dist = L.label_distribution(labels)
    total = sum(dist.values())
    assert total == 8
    assert max(dist.values()) == 4
    assert abs(max(dist.values()) / total - 0.5) < 1e-12


def test_train_symbols_and_days_constants():
    # Датасет расширен: 8 мажоров × 900 дней вместо 2 × 365.
    assert L.TRAIN_DAYS == 900
    assert len(L.TRAIN_SYMBOLS) == 8
    assert 'BTCUSDT' in L.TRAIN_SYMBOLS and 'ETHUSDT' in L.TRAIN_SYMBOLS
    assert len(set(L.TRAIN_SYMBOLS)) == len(L.TRAIN_SYMBOLS)


def test_candidate_paths_never_touch_production():
    # Кандидат пишет в отдельные файлы и не совпадает ни с одним прод-артефактом.
    prod = {L.MODEL_PATH, L.SCALER_PATH, L.FEATURES_PATH}
    cand = {L.CANDIDATE_MODEL_PATH, L.CANDIDATE_SCALER_PATH, L.CANDIDATE_FEATURES_PATH}

    assert len(cand) == 3
    assert prod.isdisjoint(cand)
    assert L.CANDIDATE_MODEL_PATH.name == 'lstm_regime_candidate.pt'
    assert L.CANDIDATE_FEATURES_PATH.name == 'lstm_regime_candidate_features.json'
    assert L.CANDIDATE_SCALER_PATH.name == 'lstm_regime_candidate_scaler.pkl'


if __name__ == "__main__":
    tests = [
        test_precompute_features_warmup_and_idempotent,
        test_class_weights_inverse_frequency,
        test_class_weights_missing_class_is_zero,
        test_class_weights_single_class_is_degenerate_safe,
        test_label_distribution_counts_and_zero_classes,
        test_label_distribution_majority_baseline_math,
        test_train_symbols_and_days_constants,
        test_candidate_paths_never_touch_production,
    ]
    for t in tests:
        t()
    print(f"OK: {len(tests)}/{len(tests)}")
