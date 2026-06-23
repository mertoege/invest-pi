#!/usr/bin/env python3
"""
monthly_digest.py — Monatlicher Verbesserungs-Digest an Mert via Telegram.

Kein Vollbericht — nur die TOPICS als Ueberblick ("was lohnt sich diesen Monat
anzuschauen"). Details besprechen Mert + Claude dann gemeinsam.

Zwei Teile:
  1. Intern (aus den Daten): offene strategic_recommendations nach Kategorie +
     die Top-Themen des letzten Meta-Reviews (prio_1).
  2. Aussenwelt: kurzer Prompt, externe Tools/Setups mit Claude durchzugehen
     (die echte Recherche macht Claude live mit Websuche — nicht hier auto-
     generiert, weil das ohne Websuche schnell veraltet/unzuverlaessig waere).

Timer: monatlich (nach dem Meta-Review).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from html import escape
from src.alerts import notifier
from src.common.json_utils import safe_parse
from src.common.storage import LEARNING_DB, connect


def _truncate(s, n=70):
    s = " ".join(str(s).split())
    return s[:n] + ("…" if len(s) > n else "")


def build_digest() -> str:
    parts = ["🗓️ <b>Monats-Digest — Verbesserungs-Themen</b>", ""]

    with connect(LEARNING_DB) as conn:
        # --- Intern 1: offene strategic_recommendations MIT Begründung ---
        recs = conn.execute(
            "SELECT id, category, effort, expected_impact, title, description "
            "FROM strategic_recommendations WHERE status='open'"
        ).fetchall()
        # --- Intern 2: Top-Themen des letzten Meta/Weekly-Reviews ---
        mr = conn.execute(
            "SELECT created_at, action_plan_js FROM meta_reviews "
            "WHERE action_plan_js IS NOT NULL ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    if recs:
        # effort/impact sind gemischt deutsch/englisch (low/klein, hoch/high) -> normalisieren
        def _impact_rank(v):
            v = (v or "").lower()
            return 0 if v in ("hoch", "high") else (1 if v in ("mittel", "medium") else 2)

        def _effort_rank(v):
            v = (v or "").lower()
            return 0 if v in ("klein", "low", "gering") else (1 if v in ("mittel", "medium") else 2)

        # wichtigste zuerst: hohe Wirkung, dann geringer Aufwand
        recs = sorted(recs, key=lambda r: (_impact_rank(r["expected_impact"]), _effort_rank(r["effort"])))
        high = [r for r in recs if _impact_rank(r["expected_impact"]) == 0]
        parts.append(f"🔧 <b>Intern: {len(recs)} offene Verbesserungs-ToDos</b>"
                     + (f" — {len(high)}× hohe Wirkung" if high else ""))
        parts.append("")
        # Top-5 MIT Begründung — sonst sieht Mert nur Schlagworte und nie das Warum
        for r in recs[:5]:
            imp = (r["expected_impact"] or "?").lower()
            eff = (r["effort"] or "?").lower()
            star = "⭐ " if _impact_rank(r["expected_impact"]) == 0 else "• "
            parts.append(f"{star}<b>{escape(_truncate(r['title'], 60))}</b> "
                         f"<i>[{escape(eff)}, Wirkung {escape(imp)}]</i>")
            if r["description"]:
                parts.append(f"   {escape(_truncate(r['description'], 180))}")
        if len(recs) > 5:
            parts.append(f"   …+{len(recs) - 5} weitere — Details mit Claude.")
        parts.append("")

    if mr:
        ap = safe_parse(mr["action_plan_js"] or "{}", default={})
        p1 = ap.get("prio_1") or []
        if p1:
            parts.append(f"📋 <b>Letzter Meta-Review ({mr['created_at'][:10]}) — Top:</b>")
            for item in p1[:3]:
                parts.append(f"  • {escape(_truncate(item, 75))}")
            parts.append("")

    # --- Aussenwelt: ehrlicher Prompt statt unzuverlaessiger Auto-Recherche ---
    parts.append("🌍 <b>Außenwelt:</b> Diesen Monat mit Claude durchgehen — "
                 "neue Tools / Infoquellen / Setups für deine Projekte? Frag mich.")
    parts.append("")
    parts.append("<i>Nur grobe Themen — Details schauen wir gemeinsam an.</i>")
    return "\n".join(parts)


def main() -> int:
    msg = build_digest()
    if not notifier.is_configured():
        print("Telegram nicht konfiguriert — Digest:\n" + msg)
        return 1
    try:
        notifier.send_info(msg, label="monthly_digest")
        print("Monats-Digest gesendet.")
        return 0
    except Exception as e:
        print(f"Senden fehlgeschlagen: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
