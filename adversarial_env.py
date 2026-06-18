"""
Adversarial Environment Generator for RL training.
Генерирует синтетические сценарии (пампы, дампы, флеты, волатильность)
на основе исторических данных для улучшения обобщения RL-агента.

Идея из LLM-as-Environment-Engineer: когда агент ошибается на определённых
режимах — добавляем больше таких режимов в трейнинг.

Usage:
    from adversarial_env import augment_training_data
    real_data = fetch_kline_data(['BTCUSDT', 'ETHUSDT'], days=365)
    augmented = augment_training_data(real_data)
    # train on augmented instead of real_data
"""

import random
import math
from typing import Optional


def _generate_pump(closes: list, factor: float = 1.5) -> list:
    """Генерирует памп: резкий рост + коррекция в последних 20% данных."""
    result = closes.copy()
    n = len(result)
    pump_start = int(n * 0.8)
    pump_len = n - pump_start

    # Экспоненциальный рост
    for i in range(pump_len):
        progress = i / pump_len
        multiplier = 1.0 + (factor - 1.0) * (progress ** 2)  # ускорение к концу
        result[pump_start + i] = closes[pump_start] * multiplier

    # Добавляем шум
    for i in range(pump_start, n):
        noise = random.gauss(0, closes[pump_start] * 0.02)
        result[i] += noise

    return result


def _generate_dump(closes: list, factor: float = 0.6) -> list:
    """Генерирует дамп: резкое падение в последних 20% данных."""
    result = closes.copy()
    n = len(result)
    dump_start = int(n * 0.8)
    dump_len = n - dump_start

    for i in range(dump_len):
        progress = i / dump_len
        multiplier = 1.0 - (1.0 - factor) * (progress ** 2)
        result[dump_start + i] = closes[dump_start] * multiplier

    for i in range(dump_start, n):
        noise = random.gauss(0, closes[dump_start] * 0.02)
        result[i] += noise

    return result


def _generate_choppy(closes: list, amplitude: float = 0.08) -> list:
    """Генерирует чоппи-флет: частые колебания вокруг средней."""
    result = closes.copy()
    n = len(result)
    base = sum(closes[-50:]) / 50 if len(closes) >= 50 else closes[-1]

    for i in range(n):
        cycle = math.sin(i * 0.3) * amplitude * base
        noise = random.gauss(0, base * 0.01)
        result[i] = base + cycle + noise

    return result


def _generate_high_vol(closes: list, vol_factor: float = 3.0) -> list:
    """Генерирует высоковолатильный режим: увеличенная амплитуда колебаний."""
    result = closes.copy()
    n = len(result)

    # Вычисляем реальную волатильность
    returns = [closes[i] / closes[i-1] - 1 for i in range(1, n)]
    std = (sum(r**2 for r in returns) / len(returns)) ** 0.5 if returns else 0.02

    # Увеличиваем волатильность
    for i in range(1, n):
        amplified_return = random.gauss(0, std * vol_factor)
        result[i] = result[i-1] * (1 + amplified_return)

    return result


def _generate_trend_reversal(closes: list) -> list:
    """Генерирует разворот тренда: рост → резкое падение."""
    result = closes.copy()
    n = len(result)
    mid = int(n * 0.5)

    # Первая половина: тренд вверх
    trend_rate = 1.0005
    for i in range(1, mid):
        result[i] = result[i-1] * (1 + trend_rate + random.gauss(0, 0.01))

    # Вторая половина: резкий разворот вниз
    for i in range(mid, n):
        result[i] = result[i-1] * (1 - 0.003 + random.gauss(0, 0.015))

    return result


def augment_training_data(all_data: list, n_scenarios: int = 4) -> list:
    """
    Дополняет реальные данные синтетическими сценариями.

    Args:
        all_data: список словарей [{symbol, closes, highs, lows, opens, volumes}, ...]
        n_scenarios: сколько сценариев на каждый символ

    Returns:
        расширенный список (реальные + синтетические данные)
    """
    augmented = list(all_data)  # сохраняем реальные данные

    generators = [
        ('pump', lambda c: _generate_pump(c, random.uniform(1.3, 2.0))),
        ('dump', lambda c: _generate_dump(c, random.uniform(0.4, 0.7))),
        ('choppy', lambda c: _generate_choppy(c, random.uniform(0.03, 0.10))),
        ('high_vol', lambda c: _generate_high_vol(c, random.uniform(2.0, 4.0))),
        ('trend_reversal', _generate_trend_reversal),
    ]

    for data in all_data:
        closes = data.get('closes', [])
        if len(closes) < 50:
            continue

        for i in range(min(n_scenarios, len(generators))):
            gen_name, gen_fn = generators[i % len(generators)]
            try:
                synth_closes = gen_fn(closes)

                # Создаём синтетические OHLCV из closes
                synth_highs = [c * random.uniform(1.005, 1.03) for c in synth_closes]
                synth_lows = [c * random.uniform(0.97, 0.995) for c in synth_closes]
                synth_opens = [synth_closes[0]] + synth_closes[:-1]
                # Добавляем шум к opens
                synth_opens = [c * random.uniform(0.995, 1.005) for c in synth_opens]
                synth_volumes = data.get('volumes', [0] * len(closes))
                if not synth_volumes or len(synth_volumes) != len(closes):
                    synth_volumes = [random.uniform(100, 10000) for _ in synth_closes]

                synth_data = {
                    'symbol': f"{data['symbol']}_{gen_name}",
                    'closes': synth_closes,
                    'highs': synth_highs,
                    'lows': synth_lows,
                    'opens': synth_opens,
                    'volumes': synth_volumes,
                    'synthetic': True,
                    'scenario': gen_name,
                }
                augmented.append(synth_data)
            except Exception:
                pass  # skip this scenario

    return augmented


if __name__ == '__main__':
    # Smoke test
    import json
    test_data = [{
        'symbol': 'TESTUSDT',
        'closes': [100.0 * (1 + 0.001 * i + random.gauss(0, 0.01)) for i in range(200)],
        'highs': [100.0 * (1 + 0.001 * i + random.gauss(0, 0.02)) for i in range(200)],
        'lows': [100.0 * (1 + 0.001 * i + random.gauss(0, 0.005)) for i in range(200)],
        'opens': [100.0 * (1 + 0.001 * i) for i in range(200)],
        'volumes': [random.uniform(100, 10000) for _ in range(200)],
    }]

    augmented = augment_training_data(test_data, n_scenarios=5)
    print(f"Original: {len(test_data)} datasets")
    print(f"Augmented: {len(augmented)} datasets")
    for d in augmented:
        tag = f" [synth:{d.get('scenario','?')}]" if d.get('synthetic') else " [real]"
        print(f"  {d['symbol']}: {len(d['closes'])} candles{tag}")
