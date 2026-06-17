"""
Phase 3: ML Scoring Module — машинное обучение для оценки сигналов.

Предсказывает вероятность успеха (TP) для LONG-сигналов на основе
исторических данных из бэктестов.

Использование:
    python3 ml_scorer.py --train              # обучить модель
    python3 ml_scorer.py --predict SYM BB_PCT  # предсказать для одного сигнала
"""

import json
import math
import os
import sys
import time
import joblib
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path.home()))
sys.path.insert(0, str(Path(__file__).parent))
from bybit_ws.api import bybit

ML_DIR = Path.home() / ".local" / "share" / "bybit-ws" / "ml"
ML_DIR.mkdir(parents=True, exist_ok=True)
MODEL_PATH = ML_DIR / "scorer_v1.joblib"
SCALER_PATH = ML_DIR / "scaler_v1.joblib"
FEATURES_PATH = ML_DIR / "features.json"

# ─── Feature Engineering ────────────────────────────────────

def compute_features(symbol: str, interval: str = "D") -> dict | None:
    """
    Собирает признаки для ML-модели из текущих рыночных данных.
    """
    try:
        # Дневные свечи для BB
        resp = bybit(
            "GET",
            f"/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit=100",
        )
        if isinstance(resp, dict) and resp.get("retCode") == 0:
            klines = resp["result"]["list"]
            klines.reverse()  # старые → новые
            
            closes = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            volumes = [float(k[5]) for k in klines]

            if len(closes) < 30:
                return None

            price = closes[-1]

            # BB%
            sma_20 = np.mean(closes[-20:])
            std_20 = np.std(closes[-20:])
            upper = sma_20 + 2 * std_20
            lower = sma_20 - 2 * std_20
            bb_pct = (price - lower) / (upper - lower) * 100 if upper != lower else 50.0

            # BB Width
            bb_width = (upper - lower) / sma_20 * 100 if sma_20 > 0 else 0

            # RSI 14
            gains, losses = [], []
            for i in range(-14, 0):
                delta = closes[i] - closes[i-1]
                gains.append(max(delta, 0))
                losses.append(max(-delta, 0))
            avg_gain = np.mean(gains)
            avg_loss = np.mean(losses)
            rsi = 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss > 0 else 100

            # Волатильность (стандартное отклонение доходностей, annualized)
            returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
            volatility = np.std(returns[-20:]) * math.sqrt(365) * 100 if returns else 0

            # Тренд: SMA20 / SMA50
            sma_50 = np.mean(closes[-50:]) if len(closes) >= 50 else sma_20
            trend_strength = (sma_20 / sma_50 - 1) * 100

            # Объём (нормированный к среднему)
            avg_vol = np.mean(volumes[-20:]) if len(volumes) >= 20 else volumes[-1]
            vol_norm = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

            # High-Low range (%)
            hl_range = (highs[-1] - lows[-1]) / price * 100

            # Поддержка/сопротивление: расстояние до SMA в сигмах
            dist_to_sma = (price - sma_20) / std_20 if std_20 > 0 else 0

            # Фандинг
            funding = 0.0
            try:
                resp_f = bybit("GET", f"/v5/market/funding/history?category=linear&symbol={symbol}&limit=1")
                if isinstance(resp_f, dict) and resp_f.get("retCode") == 0:
                    items = resp_f["result"]["list"]
                    if items:
                        funding = float(items[0].get("fundingRate", 0)) * 100
            except Exception:
                pass

            return {
                "symbol": symbol,
                "price": price,
                "bb_pct": round(bb_pct, 2),
                "bb_width": round(bb_width, 2),
                "rsi": round(rsi, 2),
                "volatility": round(volatility, 2),
                "trend_strength": round(trend_strength, 2),
                "vol_norm": round(vol_norm, 2),
                "hl_range": round(hl_range, 2),
                "dist_to_sma": round(dist_to_sma, 2),
                "funding": round(funding, 4),
                "timestamp": int(time.time()),
            }
    except Exception as e:
        print(f"  ⚠️ Ошибка признаков {symbol}: {e}")
    return None

# ─── Feature Vector ─────────────────────────────────────────

FEATURE_NAMES = [
    "bb_pct",       # BB% (0-100) — чем ниже, тем перепроданнее
    "bb_width",     # ширина полос (%)
    "rsi",          # RSI 14
    "volatility",   # годовая волатильность (%)
    "trend_strength",  # SMA20/SMA50 - 1 (%)
    "vol_norm",     # объём относительно среднего
    "hl_range",     # дневной диапазон (%)
    "dist_to_sma",  # расстояние до SMA в сигмах
    "funding",      # ставка фандинга (%)
]

def features_to_vector(features: dict) -> np.ndarray:
    return np.array([features.get(name, 0.0) for name in FEATURE_NAMES])


# ─── Training ────────────────────────────────────────────────

def train_model(data_path: str | None = None):
    """
    Обучает ML-модель на исторических данных.
    
    Если data_path не указан — генерирует синтетические данные на основе
    эвристик стратегии (холодный старт).
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import TimeSeriesSplit, cross_val_score

    X, y = [], []

    if data_path and Path(data_path).exists():
        # Реальные данные из бэктестов
        with open(data_path) as f:
            backtest_data = json.load(f)
        
        for result in backtest_data.get("results", []):
            for trade in result.get("trades", []):
                if trade["type"] == "entry":
                    # Ищем следующий exit для этого входа
                    features = compute_features(result["symbol"])
                    if features:
                        X.append(features_to_vector(features))
                        # Целевая: был ли профит при выходе? (определяем по ближайшему tp/sl)
                        y.append(1)  # временно — нужна логика сопоставления entry→exit
    else:
        # Холодный старт: синтетические данные на основе эвристик
        print("⚡ Холодный старт: генерирую синтетические данные...")
        np.random.seed(42)
        n_samples = 500

        for _ in range(n_samples):
            bb_pct = np.random.uniform(0, 30)  # перепроданная зона
            rsi = np.random.uniform(20, 45)
            volatility = np.random.uniform(30, 120)
            trend = np.random.uniform(-15, 10)
            vol_norm = np.random.uniform(0.5, 3.0)
            hl_range = np.random.uniform(1, 8)
            dist = np.random.uniform(-2.5, -0.5)
            funding = np.random.uniform(-0.1, 0.05)
            bb_width = np.random.uniform(5, 40)

            features = np.array([bb_pct, bb_width, rsi, volatility, trend,
                                 vol_norm, hl_range, dist, funding])

            # Эвристика: чем ниже BB% и RSI — тем выше вероятность успеха
            success_prob = (
                0.3 * (1 - bb_pct / 30) +
                0.2 * (1 - rsi / 50) +
                0.15 * max(0, (trend + 15) / 25) +
                0.15 * (1 - abs(funding + 0.01) * 100) +
                0.1 * max(0, 1 - volatility / 200) +
                0.1 * max(0, -dist / 2.5)
            )
            success = 1 if np.random.random() < success_prob else 0

            X.append(features)
            y.append(success)

    X = np.array(X)
    y = np.array(y)

    if len(X) < 10:
        print("❌ Недостаточно данных для обучения")
        return None, None

    # StandardScaler
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Logistic Regression
    model = LogisticRegression(
        penalty="l2",
        C=1.0,
        solver="liblinear",
        class_weight="balanced",
        random_state=42,
    )

    # TimeSeriesSplit кросс-валидация
    tscv = TimeSeriesSplit(n_splits=3)
    cv_scores = cross_val_score(model, X_scaled, y, cv=tscv, scoring="accuracy")
    print(f"  Cross-val accuracy: {cv_scores.mean():.3f} ± {cv_scores.std():.3f}")

    model.fit(X_scaled, y)

    # Сохранение
    joblib.dump(model, MODEL_PATH)
    joblib.dump(scaler, SCALER_PATH)

    # Сохраняем имена признаков
    with open(FEATURES_PATH, "w") as f:
        json.dump({"feature_names": FEATURE_NAMES, "trained_at": datetime.now().isoformat(),
                    "samples": len(X), "cv_accuracy": float(cv_scores.mean())}, f, indent=2)

    # Важность признаков
    print(f"\n📊 Важность признаков:")
    coefs = model.coef_[0]
    sorted_idx = np.argsort(np.abs(coefs))[::-1]
    for i in sorted_idx:
        print(f"  {FEATURE_NAMES[i]:20s}: {coefs[i]:+.4f}")

    return model, scaler


# ─── Prediction ──────────────────────────────────────────────

def predict(symbol: str, bb_pct: float | None = None) -> float | None:
    """
    Предсказывает вероятность успеха (TP) для сигнала.
    
    Возвращает ML-оценку от 0 до 1.
    Если модель не обучена — возвращает None.
    """
    if not MODEL_PATH.exists() or not SCALER_PATH.exists():
        return None

    model = joblib.load(MODEL_PATH)
    scaler = joblib.load(SCALER_PATH)

    # Получаем признаки
    features = compute_features(symbol)
    if features is None:
        return None

    # Если передан bb_pct — используем его вместо вычисленного
    if bb_pct is not None:
        features["bb_pct"] = bb_pct

    X = features_to_vector(features).reshape(1, -1)
    X_scaled = scaler.transform(X)
    proba = model.predict_proba(X_scaled)[0][1]

    return float(proba)


def ml_score_coin(symbol: str) -> dict | None:
    """
    Полный ML-скоринг монеты.
    Возвращает словарь с базовыми метриками + ml_score.
    """
    features = compute_features(symbol)
    if features is None:
        return None

    ml_prob = predict(symbol, features["bb_pct"]) if MODEL_PATH.exists() else None

    return {
        "symbol": symbol,
        **features,
        "ml_score": ml_prob,
        "ml_verdict": (
            "🟢 STRONG" if ml_prob and ml_prob > 0.7
            else "🟡 MODERATE" if ml_prob and ml_prob > 0.5
            else "🔴 WEAK" if ml_prob is not None
            else "⚪ NO_MODEL"
        ),
    }


# ─── CLI ─────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="ML Scoring for Bollinger Grid")
    parser.add_argument("--train", action="store_true", help="Обучить модель")
    parser.add_argument("--predict", nargs=2, metavar=("SYM", "BB_PCT"), help="Предсказать для символа")
    parser.add_argument("--data", default=None, help="JSON с данными бэктеста для обучения")
    args = parser.parse_args()

    if args.train:
        print("🤖 Обучение ML-модели...")
        train_model(args.data)
        print(f"\n✅ Модель сохранена: {MODEL_PATH}")
    elif args.predict:
        sym, bb = args.predict[0], float(args.predict[1])
        prob = predict(sym, bb)
        if prob is not None:
            print(f"🎯 {sym} (BB={bb}%): ML_score={prob:.3f} → {'🟢' if prob > 0.5 else '🔴'}")
        else:
            print("❌ Модель не обучена. Запустите --train")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
