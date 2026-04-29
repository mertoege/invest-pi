"""
Market-Regime-Detection via Hidden-Markov-Model.

Methode:
  1. Hole 5y SPY-tagesreturns + VIX-Level + VIX-5d-Veraenderung
  2. Trainiere 3-state Gaussian HMM auf diesen Features
  3. Inferiere aktuelles Regime via Viterbi
  4. Persistiere Modell in data/regime_model.pkl

3 Regimes (nach training automatisch zugeordnet):
  - low_vol_bull:   niedrige VIX, positive Returns (= "normal up")
  - high_vol_mixed: erhoehte VIX, gemischte Returns (= "uncertainty")
  - bear:           hohe VIX, negative Returns (= "panic/crash")

Decision-Engine nutzt Regime als Multiplier auf score_buy_max:
  - low_vol_bull:   1.0  (volle moderate-Aggressivitaet)
  - high_vol_mixed: 0.7  (engerer Filter)
  - bear:           0.5  (sehr conservative, kaum Buys)

Graceful degradation: wenn hmmlearn nicht installiert ist oder nicht
genug Daten, fallback auf rule-based VIX-Regime (3-tier nach VIX-Level).

Re-Training: weekly via systemd-Timer.
"""

from __future__ import annotations

import datetime as dt
import logging
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from ..common.storage import DATA_DIR

log = logging.getLogger("invest_pi.regime")

MODEL_PATH = DATA_DIR / "regime_model.pkl"
N_STATES = 3
TRAINING_LOOKBACK = "5y"

# Regime-Multiplier auf score_buy_max
REGIME_BUY_MULTIPLIER = {
    "low_vol_bull":   1.0,
    "high_vol_mixed": 0.7,
    "bear":           0.5,
    "unknown":        0.7,  # konservativer Default
}


@dataclass
class RegimeState:
    label:       str        # 'low_vol_bull' | 'high_vol_mixed' | 'bear' | 'unknown'
    probability: float      # Confidence im aktuellen Regime
    state_idx:   int        # Internal HMM-state index
    method:      str        # 'hmm' | 'rule_based'
    as_of:       str        # ISO-timestamp


# ────────────────────────────────────────────────────────────
# FEATURE EXTRACTION
# ────────────────────────────────────────────────────────────
def _fetch_features(period: str = TRAINING_LOOKBACK) -> Optional[pd.DataFrame]:
    """Holt SPY-returns + VIX-level + VIX-5d-Change als Feature-Matrix."""
    try:
        from ..common.data_loader import get_prices
        spy = get_prices("SPY", period=period)
        vix = get_prices("^VIX", period=period)
    except Exception as e:
        log.warning(f"feature fetch failed: {e}")
        return None

    if spy.empty or vix.empty:
        return None

    df = pd.DataFrame()
    df["spy_close"] = spy["close"]
    df["vix_close"] = vix["close"]
    df = df.dropna()
    if len(df) < 100:
        return None

    df["spy_ret_1d"]  = df["spy_close"].pct_change()
    df["spy_ret_5d"]  = df["spy_close"].pct_change(5)
    df["vix_change_5d"] = df["vix_close"].pct_change(5)
    df = df.dropna()

    return df[["spy_ret_1d", "vix_close", "vix_change_5d"]]


def _classify_states(model, features: pd.DataFrame) -> dict[int, str]:
    """
    Mapped HMM-state-indices auf semantische Labels basierend auf state-means.

    Logik: state mit niedrigster vix-mean = low_vol_bull
           state mit hoechster vix-mean = bear (oder high_vol_mixed wenn ret > 0)
    """
    means = model.means_     # shape (n_states, n_features)
    # features-Reihenfolge: spy_ret_1d, vix_close, vix_change_5d
    state_info = []
    for i, m in enumerate(means):
        state_info.append({
            "idx":     i,
            "ret":     float(m[0]),
            "vix":     float(m[1]),
            "vix_chg": float(m[2]),
        })
    # Sort by VIX ascending → idx 0 = low_vol, idx 2 = high_vol
    sorted_by_vix = sorted(state_info, key=lambda x: x["vix"])
    label_map: dict[int, str] = {}
    label_map[sorted_by_vix[0]["idx"]] = "low_vol_bull"
    # Mid-VIX
    mid = sorted_by_vix[1]
    label_map[mid["idx"]] = "high_vol_mixed"
    # High-VIX: bear wenn negative returns, sonst high_vol
    high = sorted_by_vix[2]
    if high["ret"] < 0:
        label_map[high["idx"]] = "bear"
    else:
        label_map[high["idx"]] = "high_vol_mixed"
        # Re-label: wenn beide oberen high_vol_mixed sind, mid wird bear wenn dort returns negativ
        if mid["ret"] < high["ret"] and mid["ret"] < 0:
            label_map[mid["idx"]] = "bear"
    return label_map


# ────────────────────────────────────────────────────────────
# TRAINING + INFERENCE
# ────────────────────────────────────────────────────────────
def train_model(period: str = TRAINING_LOOKBACK) -> Optional[dict]:
    """Trainiert HMM neu, persistiert in MODEL_PATH. Returns {model, state_labels} oder None."""
    try:
        from hmmlearn import hmm
    except ImportError:
        log.warning("hmmlearn nicht installiert — kein HMM-Training moeglich")
        return None

    features = _fetch_features(period)
    if features is None or len(features) < 100:
        log.warning("zu wenig Feature-Daten fuer HMM-Training")
        return None

    X = features.values
    # GaussianHMM mit diag-Kovarianz fuer Stabilitaet
    model = hmm.GaussianHMM(n_components=N_STATES, covariance_type="diag",
                            n_iter=200, random_state=42)
    try:
        model.fit(X)
    except Exception as e:
        log.error(f"HMM-fit failed: {e}")
        return None

    state_labels = _classify_states(model, features)

    bundle = {
        "model":        model,
        "state_labels": state_labels,
        "trained_at":   dt.datetime.utcnow().isoformat(),
        "feature_cols": list(features.columns),
        "n_train":      len(features),
    }
    try:
        with open(MODEL_PATH, "wb") as f:
            pickle.dump(bundle, f)
    except Exception as e:
        log.warning(f"konnte Modell nicht speichern: {e}")
    return bundle


def load_model() -> Optional[dict]:
    if not MODEL_PATH.exists():
        return None
    try:
        with open(MODEL_PATH, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        log.warning(f"konnte Modell nicht laden: {e}")
        return None


def _rule_based_regime(features: pd.DataFrame) -> RegimeState:
    """Fallback ohne HMM: pure VIX-Schwellen."""
    last = features.iloc[-1]
    vix = float(last["vix_close"])
    ret_5d = float(features["spy_ret_1d"].tail(5).mean())
    if vix > 28 and ret_5d < -0.005:
        label = "bear"
    elif vix > 22:
        label = "high_vol_mixed"
    else:
        label = "low_vol_bull"
    return RegimeState(
        label=label, probability=0.6, state_idx=-1, method="rule_based",
        as_of=dt.datetime.utcnow().isoformat(timespec="seconds"),
    )


def current_regime() -> RegimeState:
    """
    Hauptfunktion. Returns aktuelles Regime, mit graceful fallback.
    """
    features = _fetch_features(period="6mo")
    if features is None or len(features) < 30:
        return RegimeState(
            label="unknown", probability=0.0, state_idx=-1, method="no_data",
            as_of=dt.datetime.utcnow().isoformat(timespec="seconds"),
        )

    bundle = load_model()
    if bundle is None:
        # Versuche, frisch zu trainieren
        bundle = train_model(period="5y")

    if bundle is None:
        # HMM nicht moeglich → rule-based
        return _rule_based_regime(features)

    try:
        model = bundle["model"]
        labels = bundle["state_labels"]
        X = features.values
        # Posterior-Probability fuer den letzten Punkt
        posteriors = model.predict_proba(X)
        last_post = posteriors[-1]
        last_state = int(np.argmax(last_post))
        prob = float(last_post[last_state])
        label = labels.get(last_state, "unknown")
        return RegimeState(
            label=label, probability=prob, state_idx=last_state, method="hmm",
            as_of=dt.datetime.utcnow().isoformat(timespec="seconds"),
        )
    except Exception as e:
        log.warning(f"HMM-inference failed: {e}")
        return _rule_based_regime(features)


def regime_buy_multiplier() -> float:
    """Multiplier fuer score_buy_max basierend auf aktuellem Regime."""
    r = current_regime()
    return REGIME_BUY_MULTIPLIER.get(r.label, 0.7)
