#!/usr/bin/env python3
"""
portfolio_manager.py — Monatlicher KI-Portfolio-Manager fuer Merts echtes Revolut-Depot.

WAS DAS IST UND WAS ES NICHT IST:
Kein Kurs-Orakel. Der Job ist nicht "welche Aktie steigt naechsten Monat" — das ist
Wahrsagerei und in den eigenen Backtests 2026-06 sauber widerlegt worden. Der Job ist
Depot-Verwaltung: Was halte ich, warum halte ich es, gilt der Grund noch, wohin geht
das neue Geld, und wann muss etwas raus. Das ist Urteil ueber Zusammenhaenge — genau
das, was ein Sprachmodell kann und eine Momentum-Regel prinzipiell nicht.

DIE ENTSCHEIDENDE RANDBEDINGUNG — REVOLUT: EIN GRATIS-HANDEL PRO MONAT.
Bei 50 EUR Monatsrate kostet jeder weitere Handel bis zu 2 Prozent des Einsatzes.
Aktives Umschichten ist damit arithmetisch tot. Der Manager trifft deshalb GENAU EINE
Entscheidung pro Monat: kaufen, verkaufen oder warten. Ein Verkauf kostet den
Monatshandel und muss sich gegen "einen Monat lang investieren" rechnen.

AUFBAU (die Trennung ist Absicht):
  src/common/pm_context.py  — das Lagebild (Depot, Kennzahlen, Bewertung, Nachrichten,
                              Quartalstermine, Konjunktur, Marktbreite, Korrelation)
  src/common/thesis_store.py— Anlage-These + Widerlegungs-Kriterium je Position
  src/common/pm_rules.py    — harte Leitplanken. Die KI schlaegt vor, der CODE erlaubt.
  src/common/dca_benchmark.py— laufende Messung gegen "immer nur den Welt-ETF"

Aufruf:
    python scripts/portfolio_manager.py --dry-run   # rechnen + zeigen, nichts buchen
    python scripts/portfolio_manager.py             # Echtlauf (Timer, 1. des Monats)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.alerts import notifier
from src.common import config as cfg_mod
from src.common import depot_stillgelegt, pm_context, pm_rules, thesis_store
from src.common.json_utils import safe_parse
from src.common.llm import call_sonnet, is_configured as llm_configured

log = logging.getLogger("invest_pi.portfolio_manager")

SYSTEM = """Du bist der Portfolio-Manager fuer ein kleines, echtes Depot (Broker: Revolut, EUR).
Es ist ECHTES Geld eines Privatanlegers, kein Spielgeld und kein Handelskonto.

DEIN JOB IST NICHT KURSVORHERSAGE. Du sagst nicht, was naechsten Monat steigt — das kann
niemand. Du verwaltest: Du weisst, warum jede Position im Depot ist, du prueft jeden Monat,
ob dieser Grund noch gilt, und du entscheidest, wohin das neue Geld geht.

DIE HARTE RANDBEDINGUNG:
Revolut gibt EINEN Gratis-Handel pro Monat. Jeder weitere kostet bis zu 2 Prozent des
Einsatzes. Du hast also GENAU EINE Transaktion pro Monat. Verkaufen und Kaufen im selben
Monat geht nicht. Ein Verkauf verbraucht den Monatshandel — das neue Geld bleibt dann
liegen. Ein Verkauf muss sich also dagegen rechnen, einen Monat lang zu investieren.
Das heisst konkret: Verkaufe nur, wenn die Anlage-THESE gebrochen ist, nie wegen eines
schlechten Kursmonats.

DEINE MOEGLICHKEITEN (genau eine pro Monat):
  buy   — das Monatsbudget in EINEN Titel (neu oder Nachkauf)
  sell  — eine Position vollstaendig aufloesen (These gebrochen)
  wait  — nicht handeln, Geld sammelt sich fuer den Folgemonat

'wait' ist eine legitime, manchmal die beste Wahl: doppeltes Budget im Folgemonat kann
mehr wert sein als ein halbherziger Kauf heute. Aber nutze es nicht aus Bequemlichkeit —
Geld, das jahrelang liegt, verliert real an Wert.

ANLAGE-HORIZONT: Jahre, nicht Monate. Du kaufst nichts, was du nicht drei Jahre halten
wuerdest. Momentum allein ist KEIN Kaufgrund — was heiss laeuft, kuehlt ab, und du kannst
nicht schnell genug raus. Ein Kaufgrund ist ein struktureller: Marktstellung, Preismacht,
Bewertung im Verhaeltnis zur Ertragskraft, Streuungsnutzen fuers Depot.

BEI JEDEM KAUF verlangst du von dir selbst zwei Saetze:
  thesis       — warum dieser Titel auf Jahre. Kein Kursziel, ein Geschaeftsgrund.
  invalidation — was konkret eintreten muesste, damit du dich geirrt hast. Pruefbar
                 formuliert. NICHT "wenn der Kurs faellt" — das ist keine These, das ist
                 ein Stop-Loss. Sondern z.B. "wenn der Marktanteil zwei Quartale in Folge
                 sinkt" oder "wenn die Marge unter X faellt".

LEITPLANKEN (werden vom Code hart geprueft, ein Verstoss wird abgelehnt):
- max 25 Prozent je EINZELAKTIE, gemessen am Depot nach dem Kauf
- breite Welt-/Index-ETFs sind davon AUSGENOMMEN (sie sind in sich gestreut)
- mindestens 4 Positionen anstreben, 3 sind die harte Untergrenze
- nur handelbare Titel (grosse US-Werte, plus die drei UCITS-ETFs)

EHRLICHKEIT:
Du bekommst deine eigene bisherige Bilanz gegen "immer nur den Welt-ETF kaufen" gezeigt.
Wenn du bisher schlechter warst, sag das und handle danach. Eine Einzelaktie zu waehlen,
nur damit eine Entscheidung stattgefunden hat, ist der teuerste Fehler in diesem System.
Der breite ETF ist eine vollwertige Antwort, keine Kapitulation.

ANTWORTE NUR ALS JSON, kein Text drumherum:
{
  "market_view": "<1-2 Saetze Lage, allgemeinverstaendlich>",
  "holdings_review": [
    {"ticker": "<t>", "verdict": "gilt" | "wackelt" | "gebrochen",
     "note": "<1 Satz: gilt die These noch, und warum>",
     "thesis": "<NUR wenn die Position bisher keine These hat: trag sie nachtraeglich nach>",
     "invalidation": "<dito, pruefbares Widerlegungs-Kriterium>"}
  ],
  "decision": {
    "action": "buy" | "sell" | "wait",
    "ticker": "<t oder null bei wait>",
    "eur": <Betrag bei buy, sonst null>,
    "reason": "<2-3 Saetze, allgemeinverstaendlich, mit konkreten Zahlen>",
    "thesis": "<nur bei buy>",
    "invalidation": "<nur bei buy>"
  },
  "confidence": "high" | "medium" | "low",
  "risk_notes": "<kurz: was koennte an dieser Entscheidung schiefgehen>"
}

holdings_review MUSS jede Position im Depot enthalten.

ERSTLAUF: Positionen mit "thesis": null wurden vor deiner Zeit gekauft und haben keine
dokumentierte Begruendung. Trag sie nach — formuliere fuer jede solche Position, warum
man sie HEUTE halten wuerde, und woran man merken wuerde, dass das nicht mehr stimmt.
Wenn du fuer eine Position keinen guten Haltegrund findest, sag das offen im "note"-Feld
und setze verdict auf "wackelt" oder "gebrochen" — das ist eine ehrliche Antwort und
genau die Information, die spaeter eine Verkaufsentscheidung traegt."""


def _build_prompt(ctx: dict) -> str:
    return (
        "## Dein Depot (mit hinterlegter Anlage-These, falls vorhanden)\n"
        f"{json.dumps(ctx['holdings'], indent=2, ensure_ascii=False)}\n\n"
        "## Branchen-Gewichte im Depot (Prozent)\n"
        f"{json.dumps(ctx['sector_weights_pct'], indent=2, ensure_ascii=False)}\n\n"
        "## Gleichlauf-Risiko der Positionen (Korrelation)\n"
        f"{json.dumps(ctx['cluster_risk'], indent=2, ensure_ascii=False)}\n\n"
        "## Kaufkandidaten — breiter Scan ueber rund 90 grosse Werte, vorgefiltert auf\n"
        "   Titel ueber dem 200-Tage-Schnitt. Momentum-Werte als Dezimalzahl (0.25 = +25%).\n"
        "   Momentum ist hier NUR der Vorfilter, nicht der Kaufgrund.\n"
        f"{json.dumps(ctx['candidates'], indent=2, ensure_ascii=False)}\n\n"
        "## Waehlbare ETFs (auf Revolut in EUR handelbar) — Ticker EXAKT so verwenden\n"
        f"{json.dumps(ctx['etf_options'], indent=2, ensure_ascii=False)}\n\n"
        "## Grosswetterlage\n"
        f"{json.dumps(ctx['market'], indent=2, ensure_ascii=False)}\n\n"
        "## Budget diesen Monat\n"
        f"{ctx['budget_eur']:.2f} EUR\n\n"
        "## Deine bisherige Bilanz\n"
        f"{ctx['benchmark_record'] or 'Noch keine Messung vorhanden.'}\n\n"
        "## Leitplanken (Code prueft hart nach)\n"
        f"{json.dumps(ctx['constraints'], indent=2, ensure_ascii=False)}\n\n"
        "Triff jetzt deine EINE Entscheidung fuer diesen Monat. Antworte als JSON-Block."
    )


def _apply_reviews(reviews: list, existing: set[str]) -> None:
    """Haelt die Monats-Urteile fest. Positionen ohne hinterlegte These bekommen sie
    beim ersten Lauf nachgetragen — sonst kann spaeter nie geprueft werden, ob der
    Haltegrund noch gilt."""
    for r in reviews or []:
        t, v = (r.get("ticker") or "").upper(), (r.get("verdict") or "").lower()
        if not t:
            continue
        if t not in existing and r.get("thesis") and r.get("invalidation"):
            thesis_store.open_thesis(t, r["thesis"], r["invalidation"])
        if v:
            thesis_store.review(t, v, r.get("note", ""))


def _execute(decision: dict, cfg, budget_eur: float, pred_id) -> str:
    """Traegt die Entscheidung ins Depot-Ledger ein. Gibt eine Klartext-Meldung zurueck."""
    from scripts.buy import _guess_currency, persist_config_change, record_position, remove_position
    from src.common.dca_benchmark import record_pick

    action = decision["action"]
    ticker = (decision.get("ticker") or "").upper()

    if action == "wait":
        pm_rules.record_action("wait", None, None, decision.get("reason", ""))
        return "Nicht gehandelt — das Budget wandert in den naechsten Monat."

    if action == "sell":
        msg = remove_position(ticker)
        thesis_store.close_thesis(ticker, decision.get("reason", ""))
        pm_rules.record_action("sell", ticker, None, decision.get("reason", ""))
        persist_config_change(f"PM: Verkauf {ticker}")
        return msg

    # buy
    eur = float(decision.get("eur") or budget_eur)
    price = shares = None
    try:
        from src.common.data_loader import get_prices
        px = get_prices(ticker, period="5d")
        if px is not None and len(px) > 0:
            price = float(px["close"].iloc[-1])
            native = eur
            if _guess_currency(ticker) == "USD":
                from src.common.fx import eur_per_usd
                fx = eur_per_usd()
                native = eur / fx if fx else eur
            shares = round(native / price, 6) if price else None
    except Exception as e:
        log.warning(f"Preis fuer {ticker} nicht ermittelbar: {e}")

    msg = record_position(ticker, eur, shares=shares, price=price,
                          entry=cfg.entry_by_ticker(ticker))
    if decision.get("thesis") and decision.get("invalidation"):
        thesis_store.open_thesis(ticker, decision["thesis"], decision["invalidation"])
    pm_rules.record_action("buy", ticker, eur, decision.get("reason", ""))
    record_pick(verdict="buy_single" if not pm_rules.is_broad_etf(ticker) else "buy_etf",
                ticker=ticker, budget_eur=eur, entry_price=price,
                prediction_id=pred_id, note=(decision.get("reason") or "")[:200])
    persist_config_change(f"PM: Kauf {ticker} {eur:.0f}EUR @ {price}")
    return msg


def _telegram(plan: dict, decision: dict, blocks: list, warns: list,
              exec_msg: str, ctx: dict) -> str:
    from src.common.market_scan import REVOLUT_ETFS

    def disp(t: str) -> str:
        return t[:-3] if t and t.endswith(".DE") else t

    action = decision.get("action")
    ticker = (decision.get("ticker") or "").upper()

    if action == "buy":
        head = (f"🟢 <b>Kaufen: {escape(disp(ticker))} für "
                f"{float(decision.get('eur') or ctx['budget_eur']):.0f} EUR</b>")
        todo = "Das ist dein Gratis-Handel für diesen Monat."
    elif action == "sell":
        head = f"🔴 <b>Verkaufen: {escape(disp(ticker))} — komplett</b>"
        todo = ("Das verbraucht deinen Gratis-Handel — diesen Monat wird nicht gekauft, "
                "das Geld sammelt sich für nächsten Monat.")
    else:
        head = "⏸ <b>Diesen Monat nicht handeln</b>"
        todo = "Dein Gratis-Handel bleibt ungenutzt, das Budget wandert in den nächsten Monat."

    parts = [head, "", escape(decision.get("reason", "")), "", f"<i>{escape(todo)}</i>"]

    if action == "buy" and decision.get("thesis"):
        parts += ["", f"<b>Warum langfristig:</b> {escape(decision['thesis'])}",
                  f"<b>Woran ich merke, dass ich falsch lag:</b> {escape(decision['invalidation'])}"]

    wobbly = [r for r in (plan.get("holdings_review") or [])
              if (r.get("verdict") or "").lower() in ("wackelt", "gebrochen")]
    if wobbly:
        parts += ["", "<b>Beobachtung im Depot:</b>"]
        parts += [f"· {escape(disp((r.get('ticker') or '')))}: {escape(r.get('note',''))}"
                  for r in wobbly]

    if plan.get("market_view"):
        parts += ["", f"<i>Lage: {escape(plan['market_view'])}</i>"]
    if warns:
        parts += ["", "<i>" + escape(" · ".join(warns)) + "</i>"]
    if blocks:
        parts += ["", "⚠️ <i>Ursprünglicher Vorschlag abgelehnt (Regelverstoß): "
                  + escape(" · ".join(blocks)) + "</i>"]
    if exec_msg:
        parts += ["", f"<i>Ins Depot eingetragen: {escape(exec_msg)}</i>"]
    if ctx.get("benchmark_record"):
        parts += ["", f"📊 <i>{escape(ctx['benchmark_record'])}</i>"]

    return "\n".join(parts)


def main(dry_run: bool = False) -> int:
    if depot_stillgelegt.ist_stillgelegt():
        print(depot_stillgelegt.HINWEIS)
        return 0
    if not llm_configured():
        log.warning("ANTHROPIC_API_KEY fehlt — portfolio_manager uebersprungen")
        return 0
    if not notifier.is_configured() and not dry_run:
        log.warning("Telegram nicht konfiguriert — Anweisung kann nicht zugestellt werden")
        return 1

    cfg = cfg_mod.load()
    budget = float(cfg.settings.monatliches_budget_eur)
    ctx = pm_context.build(cfg, budget_eur=budget)

    result = call_sonnet(
        system=SYSTEM, prompt=_build_prompt(ctx),
        job_source="portfolio_manager", subject_type="portfolio", subject_id=None,
        input_summary=(f"PM-Lauf: {len(ctx['holdings'])} Positionen, "
                       f"{len(ctx['candidates'])} Kandidaten, Budget {budget:.0f} EUR"),
        max_tokens=2000, temperature=0.2, estimated_cost_eur=0.25,
    )
    if not result.ok:
        log.error(f"LLM-Aufruf fehlgeschlagen: {result.error}")
        if not dry_run:
            notifier.send_info(
                f"❌ <b>Portfolio-Manager</b> konnte nicht laufen: {escape(result.error or '?')}",
                label="pm_error")
        return 1

    plan = result.parsed_json or safe_parse(result.text, default={})
    decision = plan.get("decision") or {"action": "wait", "reason": "Keine auswertbare Antwort."}

    blocks, warns = pm_rules.validate(decision, cfg, budget)
    if blocks:
        log.warning(f"Entscheidung abgelehnt: {blocks}")
        decision = {"action": "wait",
                    "reason": ("Der Vorschlag verstiess gegen die Depot-Regeln und wurde "
                               "abgelehnt. Diesen Monat wird nicht gehandelt.")}

    if dry_run:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        print(f"\nLeitplanken — blockiert: {blocks or 'nichts'} | Hinweise: {warns or 'keine'}")
        print(f"[dry-run] Aktion waere: {decision['action']} "
              f"{decision.get('ticker') or ''} — NICHTS gebucht, NICHTS gesendet. "
              f"Kosten: {result.cost_eur:.4f} EUR")
        return 0

    with_thesis = {h["ticker"].upper() for h in ctx["holdings"] if h.get("thesis")}
    _apply_reviews(plan.get("holdings_review"), with_thesis)

    exec_msg = ""
    try:
        exec_msg = _execute(decision, cfg, budget, result.prediction_id)
    except Exception as e:
        log.error(f"Ausfuehrung fehlgeschlagen: {e}")
        exec_msg = f"FEHLER beim Eintragen: {e}"

    ok = notifier.send_action_required(
        _telegram(plan, decision, blocks, warns, exec_msg, ctx), label="portfolio_manager")
    print(f"PM pred_id={result.prediction_id} action={decision['action']} "
          f"ticker={decision.get('ticker')} blocks={len(blocks)} cost_eur={result.cost_eur:.4f}")
    return 0 if ok else 1


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Monatlicher KI-Portfolio-Manager")
    ap.add_argument("--dry-run", action="store_true",
                    help="rechnen und anzeigen, aber NICHT buchen und NICHT senden")
    sys.exit(main(dry_run=ap.parse_args().dry_run))
