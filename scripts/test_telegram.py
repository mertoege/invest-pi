#!/usr/bin/env python3
"""
test_telegram.py — Test-Skript fuer den Telegram-Notifier.

Aufruf (auf dem Pi):
    sudo -u investpi bash -c 'cd /home/investpi/invest-pi && python3 scripts/test_telegram.py'

Schickt drei Test-Nachrichten:
  1. send_info     — schlichte Hallo-Welt-Nachricht
  2. send_alert    — Stufe-2 Alert MIT Inline-Buttons (Klick noch ohne Effekt; das kommt Phase 3b)
  3. send_trade    — fake Trade-Confirmation (Paper)

Wenn dein Telegram alle drei kriegt, ist der Notifier sauber verkabelt.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# .env laden (simple parser, kein externes dotenv noetig)
env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.alerts.notifier import is_configured, send_alert, send_info, send_trade


def main() -> int:
    if not is_configured():
        print("ERROR: TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID nicht gesetzt.")
        print("       Pruefe .env und env-Vars.")
        return 1

    print("[1/3] send_info...")
    ok = send_info(
        "🤖 <b>Invest-Pi</b> ist live!\n\n"
        "First test message — wenn du das siehst, ist die Telegram-Verbindung sauber."
    )
    print(f"      → {'OK' if ok else 'FAIL'}")

    print("[2/3] send_alert (Stufe 2 mit Inline-Buttons)...")
    ok = send_alert(
        ticker="NVDA",
        alert_level=2,
        composite=58.3,
        triggered_dims=["technical_breakdown", "macro_regime"],
        prediction_id=99999,    # fake — Buttons reagieren noch nicht (Phase 3b)
        dimensions_summary=(
            "<i>Test-Alert: Composite ueber Caution-Schwelle.\n"
            "MACD bearish crossover + VIX-Spike +18% in 5T.</i>"
        ),
    )
    print(f"      → {'OK' if ok else 'FAIL'}")

    print("[3/3] send_trade (Paper-Trade-Confirmation)...")
    ok = send_trade(
        ticker="ASML",
        side="buy",
        qty=0.4,
        eur=80.50,
        price_usd=218.20,
        reason="Conservative-Strategy: composite 14.2 < 25, no triggered dims",
        paper=True,
    )
    print(f"      → {'OK' if ok else 'FAIL'}")

    print("\nAlle drei Nachrichten sollten in deinem Telegram-Chat sein.")
    print("Die Buttons in Test 2 reagieren noch nicht — das kommt Phase 3b (callbacks).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
