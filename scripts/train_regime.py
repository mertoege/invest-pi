#!/usr/bin/env python3
"""
train_regime.py — Trainiert HMM-Regime-Modell neu auf 5y SPY+VIX-Daten.

Wird wöchentlich vom systemd-Timer aufgerufen (Sa 05:00, nach meta_review).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.learning.regime import train_model, current_regime


def main() -> int:
    print("Training HMM-Regime-Modell...")
    bundle = train_model(period="5y")
    if bundle is None:
        print("  ✘ Training fehlgeschlagen (hmmlearn fehlt oder zu wenig Daten)")
        return 1
    print(f"  ✔ Modell trainiert auf {bundle['n_train']} Beobachtungen")
    print(f"  state_labels: {bundle['state_labels']}")

    state = current_regime()
    print(f"\nAktuelles Regime: {state.label} (prob {state.probability:.0%}, method {state.method})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
