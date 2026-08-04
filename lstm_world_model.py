"""
lstm_world_model.py — Multi-task LSTM с World Modeling для bybit-ws (v1.0).

Идея из ECHO (Anthropic, 2026): добавляем auxiliary loss на предсказание
наблюдений среды (OHLCV на t+1) поверх основной задачи классификации режима.

Архитектура:
  Input(30, 8) → LSTM(64) → LSTM(32) ─┬── Dense(32) → Dense(5) softmax  (режим)
                                       └── Dense(32) → Dense(5) linear   (OHLCV t+1)

Loss: L_regime + λ · L_world
  λ = 0.05 (из рекомендаций ECHO: [0.01, 0.05])

Каждая свеча становится training sample — плотный сигнал вместо разреженного.
"""
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False
    torch = None
    nn = None
    optim = None

# Используем константы из lstm_regime.py
SEQUENCE_LENGTH = 30
LOOKAHEAD = 7
N_CLASSES = 5
N_FEATURES = 13  # 8 рыночных + 5 макро
CLASS_NAMES = ['TRENDING_UP', 'TRENDING_DOWN', 'RANGING', 'HIGH_VOL', 'LOW_VOL']
CLASS_IDX = {n: i for i, n in enumerate(CLASS_NAMES)}

DATA_DIR = Path.home() / '.local' / 'share' / 'bybit-ws'
MODEL_DIR = DATA_DIR / 'models'
WORLD_MODEL_PATH = MODEL_DIR / 'lstm_world_model.pt'
WORLD_SCALER_PATH = MODEL_DIR / 'lstm_world_scaler.pkl'

# ── World Model Architecture ────────────────────────────────────────────

if HAS_TORCH:
    class LSTMWorldModel(nn.Module):
        """Multi-task LSTM: regime classification + OHLCV prediction."""

        def __init__(self, input_size=N_FEATURES, hidden_size=64,
                     num_layers=2, num_classes=N_CLASSES, dropout=0.3):
            super().__init__()
            # Shared encoder
            self.lstm = nn.LSTM(input_size, hidden_size, num_layers,
                                batch_first=True,
                                dropout=dropout if num_layers > 1 else 0)

            # Classification head (regime)
            self.fc1 = nn.Linear(hidden_size, 32)
            self.relu = nn.ReLU()
            self.dropout = nn.Dropout(dropout)
            self.classifier = nn.Linear(32, num_classes)

            # World head (OHLCV regression)
            self.world_fc1 = nn.Linear(hidden_size, 32)
            self.world_relu = nn.ReLU()
            self.world_dropout = nn.Dropout(dropout)
            self.world_head = nn.Linear(32, 5)  # open, high, low, close, volume

        def forward(self, x):
            """
            x: (batch, seq_len, input_size)
            Returns:
              regime_logits: (batch, num_classes)
              world_pred: (batch, 5) — predicted OHLCV at t+1
            """
            lstm_out, (hn, cn) = self.lstm(x)
            last_out = lstm_out[:, -1, :]

            # Regime head
            regime_h = self.relu(self.fc1(last_out))
            regime_h = self.dropout(regime_h)
            regime_logits = self.classifier(regime_h)

            # World head
            world_h = self.world_relu(self.world_fc1(last_out))
            world_h = self.world_dropout(world_h)
            world_pred = self.world_head(world_h)

            return regime_logits, world_pred


# ── Feature Engineering ─────────────────────────────────────────────────

def _calc_features(closes, highs, lows, volumes):
    """Рассчитать 8 рыночных признаков на основе истории (как в lstm_regime.py)."""
    if len(closes) < 5:
        return None
    returns = np.diff(closes) / closes[:-1]
    avg_range = np.mean([(h - l) / l for h, l in zip(highs[-5:], lows[-5:])]) * 100
    rsi_val = _calc_rsi(closes, 14)
    bb_data = _calc_bb(closes[-20:])
    return [
        np.mean(returns[-5:]) * 100 if len(returns) >= 5 else 0,   # mean_return_5
        np.std(returns[-10:]) * 100 if len(returns) >= 10 else 0,  # volatility_10
        rsi_val if rsi_val else 50,                                  # RSI
        bb_data['width'] if bb_data else 0,                          # BB width
        (closes[-1] - np.mean(closes[-20:])) / (np.std(closes[-20:]) + 1e-8) if len(closes) >= 20 else 0,  # z-score
        max(highs[-5:]) / np.mean(closes[-20:]) - 1 if len(closes) >= 20 else 0,  # distance from high
        np.mean(closes[-20:]) / closes[-1] - 1 if len(closes) >= 20 else 0,  # distance from mean
        np.log(np.mean(volumes[-5:]) + 1) if len(volumes) >= 5 else 0,  # log volume
    ]


def _calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes[-period-1:])
    gain = np.mean(deltas[deltas > 0]) if any(deltas > 0) else 0
    loss = np.mean(-deltas[deltas < 0]) if any(deltas < 0) else 1e-8
    if loss == 0:
        return 100.0
    rs = gain / loss
    return 100.0 - 100.0 / (1.0 + rs)


def _calc_bb(closes, period=20, std_dev=2):
    if len(closes) < period:
        return None
    middle = np.mean(closes)
    std = np.std(closes)
    return {
        'upper': middle + std_dev * std,
        'middle': middle,
        'lower': middle - std_dev * std,
        'width': (2 * std_dev * std) / middle * 100 if middle > 0 else 0,
    }


def _get_macro_features():
    """Макро-признаки (заглушка — в реальности из внешнего API)."""
    return [0.0] * 5  # VIX proxy, DXY proxy, BTC dominance, funding rate, market cap


def _label_regime(closes, highs, lows, idx):
    """Метка режима (forward-looking, lookahead LOOKAHEAD дней)."""
    if idx + LOOKAHEAD >= len(closes):
        return None
    future_return = (closes[idx + LOOKAHEAD] - closes[idx]) / closes[idx]
    avg_range = np.mean([(h - l) / l for h, l in
                         zip(highs[idx:idx + LOOKAHEAD], lows[idx:idx + LOOKAHEAD])]) * 100
    if future_return > 0.05:
        return 'TRENDING_UP'
    elif future_return < -0.05:
        return 'TRENDING_DOWN'
    elif avg_range > 4.0:
        return 'HIGH_VOL'
    elif avg_range < 1.5:
        return 'LOW_VOL'
    else:
        return 'RANGING'


# ── Data Pipeline ───────────────────────────────────────────────────────

def build_world_dataset(all_data, seq_len=SEQUENCE_LENGTH):
    """
    Построить датасет для multi-task обучения:
    X: (N, seq_len, features)
    y_regime: (N,) — метка класса режима
    y_world: (N, 5) — OHLCV на следующий день (нормированные)
    """
    X_list, y_regime_list, y_world_list = [], [], []
    macro = _get_macro_features()

    for sym_data in all_data:
        closes = np.array(sym_data['closes'])
        highs = np.array(sym_data['highs'])
        lows = np.array(sym_data['lows'])
        volumes = np.array(sym_data['volumes'])

        if len(closes) < seq_len + LOOKAHEAD + 1:
            continue

        for i in range(seq_len, len(closes) - LOOKAHEAD):
            features = []
            for j in range(i - seq_len, i):
                feat = _calc_features(
                    closes[:j + 1], highs[:j + 1], lows[:j + 1], volumes[:j + 1]
                )
                if feat is None:
                    break
                features.append(feat + macro)
            if len(features) != seq_len:
                continue

            # Regime label
            label = _label_regime(closes, highs, lows, i)
            if label is None or label not in CLASS_IDX:
                continue

            # World target: относительные изменения (%, а не абсолютные цены)
            next_idx = i + 1
            current_close = closes[i]
            if current_close <= 0:
                continue
            world_target = [
                (closes[next_idx] - current_close) / current_close,      # [0] close return %, negative=down
                (highs[next_idx] - current_close) / current_close,       # [1] high deviation %, always >=0
                (current_close - lows[next_idx]) / current_close,        # [2] low deviation %, always >=0 (close−low≥0)
                (highs[next_idx] - lows[next_idx]) / current_close,      # [3] daily range %, always >=0
                math.log(max(volumes[next_idx] / max(volumes[i-5:i+1].mean() if i >= 5 else volumes[next_idx], 1e-8), 0.01)),  # [4] volume ratio
            ]

            X_list.append(features)
            y_regime_list.append(CLASS_IDX[label])
            y_world_list.append(world_target)

    if not X_list:
        return None, None, None

    X = np.array(X_list, dtype=np.float32)
    y_regime = np.array(y_regime_list, dtype=np.int64)
    y_world = np.array(y_world_list, dtype=np.float32)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    y_world = np.nan_to_num(y_world, nan=0.0, posinf=0.0, neginf=0.0)
    return X, y_regime, y_world


def fetch_training_data(symbols=('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'LTCUSDT', 'ADAUSDT'), days=730):
    """Загрузить исторические D-свечи (как в lstm_regime.py)."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    try:
        from bybit_ws.api import bybit
    except ImportError:
        from api import bybit

    all_data = []
    for sym in symbols:
        closes, highs, lows, volumes = [], [], [], []
        fetched = 0
        end_ms = None
        while fetched < days:
            limit = min(200, days - fetched)
            url = (f'/v5/market/kline?category=linear&symbol={sym}'
                   f'&interval=D&limit={limit}')
            if end_ms:
                url += f'&end={end_ms}'
            resp = bybit('GET', url)
            if not resp or resp.get('retCode') != 0:
                break
            candles = resp['result'].get('list', [])
            if not candles:
                break
            for c in reversed(candles):
                closes.append(float(c[4]))
                highs.append(float(c[2]))
                lows.append(float(c[3]))
                volumes.append(float(c[5]))
            fetched += len(candles)
            if len(candles) < limit:
                break
            end_ms = candles[0][0]
            time.sleep(0.15)
        if closes:
            all_data.append({
                'symbol': sym, 'closes': closes,
                'highs': highs, 'lows': lows, 'volumes': volumes,
            })
    return all_data


# ── Training ────────────────────────────────────────────────────────────

# Cache path for fetched data to avoid re-downloading
WORLD_DATA_CACHE = DATA_DIR / 'lstm_world_data_cache.pkl'

def _load_data_with_cache(symbols=('BTCUSDT', 'ETHUSDT'), days=365, force_refetch=False):
    """Load training data with pickle cache. Falls back to BTC+ETH 365d if no cache."""
    import pickle

    if not force_refetch and WORLD_DATA_CACHE.exists():
        try:
            with open(WORLD_DATA_CACHE, 'rb') as f:
                data = pickle.load(f)
            print(f"📦 Загружен кеш данных: {len(data)} символов, "
                  f"{sum(len(d['closes']) for d in data)} свечей из {WORLD_DATA_CACHE}")
            return data
        except Exception as e:
            print(f"⚠️ Ошибка чтения кеша: {e}, загружаем заново")

    print(f"📡 Загрузка исторических данных ({len(symbols)} символов, {days} дней)...")
    data = fetch_training_data(list(symbols), days=days)
    if not data:
        print("❌ Не удалось загрузить данные")
        return None

    # Save to cache
    WORLD_DATA_CACHE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(WORLD_DATA_CACHE, 'wb') as f:
            pickle.dump(data, f)
        print(f"💾 Кеш сохранён: {WORLD_DATA_CACHE}")
    except Exception as e:
        print(f"⚠️ Не удалось сохранить кеш: {e}")

    return data


def train_world_model(lambda_world=0.03, epochs=200, force=False):
    """Обучить multi-task LSTM с world modeling."""
    if not HAS_TORCH:
        print("❌ PyTorch не установлен. pip install torch")
        return None

    data = _load_data_with_cache()
    if not data:
        return None

    for d in data:
        print(f"   {d['symbol']}: {len(d['closes'])} дней")
    print("🔨 Построение multi-task датасета...")
    X, y_regime, y_world = build_world_dataset(data)
    if X is None:
        print("❌ Не удалось построить датасет")
        return None

    print(f"   Сэмплов: {len(X)}, форма X: {X.shape}, y_world: {y_world.shape}")

    # Баланс классов
    unique, counts = np.unique(y_regime, return_counts=True)
    for cls_idx, cnt in zip(unique, counts):
        print(f"   {CLASS_NAMES[cls_idx]}: {cnt} ({cnt/len(y_regime)*100:.1f}%)")

    # Масштабирование (только для признаков X, target'ы уже нормированы)
    from lstm_regime import FeatureScaler
    scaler = FeatureScaler()
    X_scaled = scaler.fit_transform(X)

    # Train/val split
    split = int(len(X) * 0.8)
    X_train = torch.tensor(X_scaled[:split], dtype=torch.float32)
    y_regime_train = torch.tensor(y_regime[:split], dtype=torch.long)
    y_world_train = torch.tensor(y_world[:split], dtype=torch.float32)
    X_val = torch.tensor(X_scaled[split:], dtype=torch.float32)
    y_regime_val = torch.tensor(y_regime[split:], dtype=torch.long)
    y_world_val = torch.tensor(y_world[split:], dtype=torch.float32)

    # Class weights
    weight_array = np.ones(N_CLASSES, dtype=np.float32)
    for cls_idx, cnt in zip(unique, counts):
        weight_array[cls_idx] = len(y_regime) / (len(unique) * max(cnt, 1))
    class_weights = torch.tensor(weight_array, dtype=torch.float32)

    # Model
    model = LSTMWorldModel()
    regime_criterion = nn.CrossEntropyLoss(weight=class_weights)
    world_criterion = nn.MSELoss()
    optimizer = optim.Adam(model.parameters(), lr=0.001, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, patience=5, factor=0.5
    )

    batch_size = 64
    best_val_acc = 0
    best_world_mse = float('inf')
    best_epoch = 0

    print(f"\n🎯 Training с λ_world={lambda_world}, {epochs} эпох, batch={batch_size}, 5 symbols×2yr...")
    for epoch in range(epochs):
        model.train()
        total_loss = 0
        total_regime_loss = 0
        total_world_loss = 0

        for i in range(0, len(X_train), batch_size):
            batch_x = X_train[i:i + batch_size]
            batch_r = y_regime_train[i:i + batch_size]
            batch_w = y_world_train[i:i + batch_size]

            regime_logits, world_pred = model(batch_x)
            regime_loss = regime_criterion(regime_logits, batch_r)
            world_loss = world_criterion(world_pred, batch_w)
            loss = regime_loss + lambda_world * world_loss

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            total_loss += loss.item()
            total_regime_loss += regime_loss.item()
            total_world_loss += world_loss.item()

        # Validation
        model.eval()
        with torch.no_grad():
            val_logits, val_world = model(X_val)
            val_regime_loss = regime_criterion(val_logits, y_regime_val)
            val_world_loss = world_criterion(val_world, y_world_val)
            val_acc = (val_logits.argmax(dim=1) == y_regime_val).float().mean()

        scheduler.step(val_regime_loss)

        if val_acc > best_val_acc:
            best_val_acc = val_acc.item()
            best_world_mse = val_world_loss.item()
            best_epoch = epoch + 1
            # Сохранить лучшую модель сразу
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            torch.save({
                'model_state': model.state_dict(),
                'config': {'lambda_world': lambda_world},
                'val_acc': best_val_acc,
                'world_mse': best_world_mse,
                'epoch': best_epoch,
                'timestamp': datetime.now().isoformat(),
            }, WORLD_MODEL_PATH)

        if (epoch + 1) % 10 == 0:
            print(f"  Epoch {epoch+1}/{epochs} | "
                  f"L={total_loss/(len(X_train)//batch_size):.4f} | "
                  f"Regime_L={total_regime_loss:.4f} World_L={total_world_loss:.4f} | "
                  f"Val_acc={val_acc.item():.3f}")

    print(f"\n✅ Обучение завершено. Best val acc={best_val_acc:.3f}, "
          f"world MSE={best_world_mse:.4f}")

    # Сохранить модель
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    torch.save({
        'model_state': model.state_dict(),
        'config': {'lambda_world': lambda_world},
        'val_acc': best_val_acc,
        'world_mse': best_world_mse,
        'timestamp': datetime.now().isoformat(),
    }, WORLD_MODEL_PATH)
    print(f"💾 Модель сохранена: {WORLD_MODEL_PATH}")

    # Сохранить скейлер
    import pickle
    with open(WORLD_SCALER_PATH, 'wb') as f:
        pickle.dump({'min_': scaler.min_, 'max_': scaler.max_}, f)

    return model, best_val_acc, best_world_mse


def predict_world(symbol='BTCUSDT', days=30):
    """
    Предсказать режим рынка И OHLCV на следующий день.
    Возвращает: {regime, confidence, world_prediction, ...}
    """
    if not HAS_TORCH or not WORLD_MODEL_PATH.exists():
        return None

    model = LSTMWorldModel()
    model.eval()

    try:
        ckpt = torch.load(WORLD_MODEL_PATH, map_location='cpu')
        model.load_state_dict(ckpt['model_state'])
    except Exception as e:
        print(f"❌ Ошибка загрузки модели: {e}")
        return None

    try:
        import pickle
        with open(WORLD_SCALER_PATH, 'rb') as f:
            scaler_params = pickle.load(f)
    except Exception:
        return None

    # Загрузить последние данные
    data = fetch_training_data([symbol], days=max(days, SEQUENCE_LENGTH))
    if not data:
        return None

    d = data[0]
    closes, highs, lows, volumes = d['closes'], d['highs'], d['lows'], d['volumes']

    # Построить features для последнего окна
    features = []
    macro = _get_macro_features()
    for j in range(len(closes) - SEQUENCE_LENGTH, len(closes)):
        feat = _calc_features(closes[:j + 1], highs[:j + 1], lows[:j + 1], volumes[:j + 1])
        if feat is None:
            return None
        features.append(feat + macro)

    X = np.array([features], dtype=np.float32)
    X = np.nan_to_num(X, nan=0.0)

    # Scale
    min_ = scaler_params['min_']
    max_ = scaler_params['max_']
    X_scaled = (X - min_) / (max_ - min_ + 1e-8)

    with torch.no_grad():
        logits, world_pred = model(torch.tensor(X_scaled, dtype=torch.float32))
        probs = torch.softmax(logits, dim=1)[0]
        regime_idx = probs.argmax().item()
        confidence = probs[regime_idx].item()

    current_close = closes[-1]
    world = world_pred[0].numpy()

    return {
        'regime': CLASS_NAMES[regime_idx],
        'confidence': round(confidence * 100, 1),
        'world_prediction': {
            # Reconstruction: target[0]=Δ_close%, target[1]=Δ_high%, target[2]=Δ_low% (close−low)/close
            # close_t1 = close × (1 + Δ_close)   ; high_t1 = close × (1 + Δ_high)
            # low_t1  = close × (1 − Δ_low)      (Δ_low is positive: (close−low)/close, so subtract)
            'close_t1': round(current_close * (1 + world[0]), 4),
            'high_t1': round(current_close * (1 + world[1]), 4),
            'low_t1': round(current_close * (1 - world[2]), 4),
            'daily_range_pct': round(world[3] * 100, 2),   # target[3] = (high−low)/close
            'volume_log_ratio': round(world[4], 2),
        },
        'current_close': current_close,
        'model_val_acc': round(ckpt.get('val_acc', 0) * 100, 1),
    }


# ── CLI ─────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='LSTM World Model for bybit-ws')
    ap.add_argument('--train', action='store_true', help='Обучить модель')
    ap.add_argument('--predict', action='store_true', help='Предсказать режим + OHLCV')
    ap.add_argument('--lambda', type=float, default=0.03, help='λ для world loss')
    ap.add_argument('--symbol', default='BTCUSDT', help='Символ для предсказания')
    args = ap.parse_args()

    if args.train:
        train_world_model(lambda_world=getattr(args, 'lambda', 0.05))
    elif args.predict:
        result = predict_world(args.symbol)
        if result:
            print(json.dumps(result, indent=2, default=str))
        else:
            print("❌ Не удалось выполнить предсказание")
    else:
        ap.print_help()
