#!/usr/bin/env python3
"""
buy.py — Neue Position einbuchen oder bestehende aufstocken.

Nach jedem Kauf im Broker rufst du dieses Script auf. Es:
  1. Prüft ob der Kauf die Konzentrations-Limits verletzt
  2. Aktualisiert config.yaml
  3. Zeigt neue Portfolio-Zusammensetzung

Usage:
    python scripts/buy.py NVDA 50              # 50 EUR in NVDA
    python scripts/buy.py MSFT 50 --shares 0.5 --price 420.00
    python scripts/buy.py --check NVDA 50      # nur prüfen, nicht einbuchen
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from src.common import config as cfg_mod

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def record_position(ticker: str, eur_amount: float, shares: float | None = None,
                    price: float | None = None, entry=None) -> str:
    """Bucht einen Kauf ins config.yaml-Portfolio-Ledger ein (KEINE Broker-Order).
    Aktualisiert invested_eur, shares und avg_buy_price; legt neue Position an oder
    stockt auf. Returns kurzen Status-Text. Wiederverwendbar (CLI + Auto-DCA)."""
    raw = yaml.safe_load(CONFIG_PATH.read_text())
    if "portfolio" not in raw:
        raw["portfolio"] = {}

    if ticker in raw["portfolio"]:
        existing = raw["portfolio"][ticker]
        old_invested = float(existing.get("invested_eur", 0))
        new_invested = old_invested + eur_amount
        if price and existing.get("avg_buy_price") and existing.get("shares"):
            old_shares = float(existing["shares"])
            new_shares = old_shares + (shares or eur_amount / price)
            existing["shares"] = round(new_shares, 6)
            existing["avg_buy_price"] = round(new_invested / new_shares, 4)
        elif price and shares:
            existing["shares"] = round((existing.get("shares") or 0) + shares, 6)
            existing["avg_buy_price"] = price
        existing["invested_eur"] = round(new_invested, 2)
        msg = f"{ticker}: {old_invested:.0f} -> {new_invested:.0f} EUR aufgestockt"
    else:
        ring = entry.ring if entry else 0
        raw["portfolio"][ticker] = {
            "invested_eur":    round(eur_amount, 2),
            "shares":          round(shares, 6) if shares else None,
            "avg_buy_price":   round(price, 4) if price else None,
            "date_first":      _this_month(),
            "currency":        _guess_currency(ticker),
            "ring":            ring,
            "note":            entry.note if entry else "",
        }
        msg = f"{ticker}: neue Position {eur_amount:.0f} EUR"

    CONFIG_PATH.write_text(yaml.dump(raw, allow_unicode=True, default_flow_style=False))
    cfg_mod.reload()
    return msg


def remove_position(ticker: str) -> str:
    """Entfernt eine Position komplett aus dem config.yaml-Portfolio-Ledger
    (z.B. nach Verkauf). Ticker-Match ist case-insensitiv. Returns Status-Text;
    cfg wird neu geladen. Wiederverwendbar (CLI + Verkaufs-Callback)."""
    raw = yaml.safe_load(CONFIG_PATH.read_text())
    pf = raw.get("portfolio", {}) or {}
    key = next((k for k in pf if k.upper() == ticker.upper()), None)
    if key is None:
        return f"{ticker}: war nicht im Portfolio (nichts zu entfernen)"
    invested = float(pf[key].get("invested_eur", 0) or 0)
    del pf[key]
    raw["portfolio"] = pf
    CONFIG_PATH.write_text(yaml.dump(raw, allow_unicode=True, default_flow_style=False))
    cfg_mod.reload()
    return f"{key}: aus Portfolio entfernt ({invested:.0f} EUR gebucht)"


def persist_config_change(label: str) -> bool:
    """Committet+pusht die config.yaml-Aenderung, damit sie nicht vom auto-pull/
    status-push (git reset --hard origin/main) verworfen wird. Wiederverwendbar.
    Returns True, wenn die Aenderung nachweislich auf origin/main liegt.

    WARUM SO UMSTAENDLICH (Datenverlust am 01.08.2026, teuer gelernt):
    Die alte Fassung war "best effort" und hat jeden Rueckgabewert verschluckt —
    schlug der Push fehl, blieb der Commit rein lokal und war stumm verloren.
    Genau das passierte: 16:38:45 committete der Portfolio-Manager den UNH-Kauf,
    58 Sekunden spaeter lief status_push.sh in einen Rebase-Konflikt (auto_pull
    arbeitete zeitgleich im selben Repo) und machte `git reset --hard origin/main`.
    Der Reset rettet nur den eigenen Snapshot, alles andere Unpushte faellt weg —
    der Commit war weg, ohne eine einzige Fehlerzeile. Merts Depot zeigte danach
    einen VWCE-Kauf, den es nie gab, und den echten UNH-Kauf gar nicht.
    Konsequenz: Wir pruefen jetzt NACH dem Push, ob der Commit wirklich auf
    origin/main liegt, versuchen es mehrfach, und schlagen sonst laut Alarm.
    Die zweite Haelfte der Absicherung sitzt in status_push.sh/auto_pull.sh:
    deren Hard-Reset legt fremde Commits vorher beiseite und spielt sie zurueck.
    """
    import logging
    import subprocess
    import time
    log = logging.getLogger("invest_pi.buy")
    repo = str(Path(__file__).resolve().parents[1])

    def _git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=60)

    try:
        _git("add", "config.yaml")
        if _git("commit", "-m", f"portfolio: {label}").returncode != 0:
            return True  # nichts zu committen — Stand ist bereits sauber
        sha = _git("rev-parse", "HEAD").stdout.strip()

        # Mehrere Anlaeufe: die 2-Min-Timer (auto_pull, status_push) schreiben im
        # selben Repo, ein einzelner Versuch trifft die Luecke oft nicht.
        for versuch in range(1, 4):
            if _git("pull", "--rebase", "--no-edit").returncode != 0:
                _git("rebase", "--abort")
            sha = _git("rev-parse", "HEAD").stdout.strip()
            _git("push")
            # Beweis statt Rueckgabewert: liegt der Commit auf origin/main?
            _git("fetch", "origin", "main")
            if sha and _git("merge-base", "--is-ancestor", sha, "origin/main").returncode == 0:
                return True
            log.warning(f"config.yaml-Push noch nicht auf origin/main (Versuch {versuch}/3)")
            time.sleep(5)

        # Nicht durchgekommen. Der Commit steht lokal und ist damit in Gefahr,
        # vom naechsten Hard-Reset gefressen zu werden -> sichtbar wegsichern.
        _git("tag", "-f", f"rettung/config-{sha[:8]}", sha)
        log.error(
            f"config.yaml-Aenderung '{label}' ({sha[:8]}) NICHT auf origin/main gelandet. "
            f"Lokal gesichert als Tag rettung/config-{sha[:8]} — sonst waere sie beim "
            "naechsten reset --hard verloren."
        )
        try:
            from src.alerts import notifier
            notifier.send_action_required(
                f"⚠️ Depot-Buchung nicht gesichert\n\n{label}\n\n"
                "Die Änderung an der config.yaml konnte nicht ins Git gepusht werden und "
                "wäre beim nächsten Auto-Sync verloren. Sie liegt lokal als Rettungs-Tag "
                f"rettung/config-{sha[:8]}. Bitte nachsehen."
            )
        except Exception:
            pass  # Alarm ist Beiwerk; der Tag ist die eigentliche Rettung
        return False
    except Exception as e:
        log.error(f"config.yaml commit/push fehlgeschlagen: {e}")
        return False


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--check" in args
    args = [a for a in args if not a.startswith("--")]

    if len(args) < 2:
        print(__doc__)
        return

    ticker     = args[0].upper()
    eur_amount = float(args[1])
    shares     = float(args[2]) if len(args) > 2 else None
    price      = float(args[3]) if len(args) > 3 else None

    cfg = cfg_mod.load()

    # Universum-Check: ist der Ticker überhaupt bekannt?
    entry = cfg.entry_by_ticker(ticker)
    if not entry:
        print(f"\n  Warnung: {ticker} ist nicht in config.yaml/universe definiert.")
        print(f"  Kannst du trotzdem hinzufügen — trag ihn in config.yaml ein.")

    # Konzentrations-Check
    check = cfg.concentration_check(ticker, eur_amount)
    print(f"\n  Konzentrations-Check für {ticker} + {eur_amount:.0f} EUR:")
    if check["blocks"]:
        print(f"  BLOCK:")
        for b in check["blocks"]:
            print(f"    ! {b}")
    if check["warnings"]:
        for w in check["warnings"]:
            print(f"    ~ {w}")
    if not check["blocks"] and not check["warnings"]:
        print(f"  OK — {check['ticker_pct_after']:.0%} des Portfolios")

    if dry_run:
        print("\n  Dry-run, keine Änderung an config.yaml.")
        return

    if check["blocks"]:
        print("\n  Kauf abgebrochen wegen Limit-Verletzung.")
        print("  Mit --check kannst du alternative Beträge testen.")
        return

    # Config aktualisieren (gemeinsame Logik, auch vom Auto-DCA genutzt)
    msg = record_position(ticker, eur_amount, shares=shares, price=price, entry=entry)
    print(f"\n  {msg}")
    print(f"  config.yaml aktualisiert.")


def _this_month() -> str:
    import datetime
    return datetime.date.today().strftime("%Y-%m")


def _guess_currency(ticker: str) -> str:
    if ticker.endswith(".DE") or ticker.endswith(".PA") or ticker.endswith(".AS"):
        return "EUR"
    return "USD"


if __name__ == "__main__":
    main()
