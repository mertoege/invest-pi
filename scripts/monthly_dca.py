#!/usr/bin/env python3
"""
monthly_dca.py — Monatliche DCA-Empfehlung an Mert via Telegram.

Pipeline:
  1. Lade aktuelle Risk-Scores + Hit-Rate-History + offene Positionen
  2. Baue Sonnet-Prompt mit Kontext + JSON-Output-Forderung
  3. Anthropic-Call via llm.call_sonnet
  4. Parse JSON: {ticker, reason, confidence, alternative_etf}
  5. Telegram-Push an Mert mit Inline-Buttons:
       ✅ habe gekauft  /  ⚪ ETF gekauft  /  ⏸ skip

Cron: 1. des Monats 14:00 CEST (vor US-Marktoeffnung).
Feature-Flag: skip wenn ANTHROPIC_API_KEY leer (logs + exit 0).

Callback-Format:
  dca:{prediction_id}:{action}   action ∈ {bought, etf, skip}
"""

from __future__ import annotations

import json
import logging
import os
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# .env laden
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
from src.common.json_utils import safe_parse
from src.common.llm import call_sonnet, is_configured as llm_configured
from src.common.predictions import hit_rate_stratified, latest_risk_score_summary, log_prediction
from src.learning.calibration import calibration_block
from src.common.storage import LEARNING_DB, connect

log = logging.getLogger("invest_pi.monthly_dca")


def _risk_scores_by_ticker(days: int = 7) -> dict[str, dict]:
    """Vorhandene Risk-Scores als ZUSATZ-Info (nicht als Auswahlgrundlage).

    Achtung: diese Scores deckten zuletzt nur die bereits gehaltenen Positionen ab,
    weil sie faktisch nur noch vom DCA-Watchdog erzeugt werden. Genau deshalb sind
    sie hier eine Anreicherung fuer die wenigen Ticker, die es gibt — und NICHT mehr
    die Quelle der Kandidatenliste (siehe Audit-Kommentar in src/common/market_scan.py).
    """
    sql = """
        SELECT subject_id, output_json, created_at
          FROM predictions
         WHERE job_source = 'daily_score'
           AND created_at >= datetime('now', ?)
         ORDER BY created_at DESC
    """
    out: dict[str, dict] = {}
    try:
        with connect(LEARNING_DB) as conn:
            rows = conn.execute(sql, (f"-{int(days)} day",)).fetchall()
    except Exception as e:
        log.warning(f"Risk-Scores nicht ladbar: {e}")
        return out
    for r in rows:
        if r["subject_id"] in out:
            continue
        o = safe_parse(r["output_json"] or "{}", default={})
        out[r["subject_id"]] = {
            "composite":   o.get("composite"),
            "alert_level": o.get("alert_level"),
        }
    return out


def _portfolio_breakdown(cfg) -> tuple[list[dict], dict[str, float]]:
    """Depot-Positionen + Sektor-Gewichte in Prozent — damit das LLM Klumpenrisiko
    tatsaechlich pruefen kann statt es zu raten."""
    from src.common.market_scan import SECTORS
    positions = [
        {"ticker": t, "invested_eur": p.invested_eur,
         "sector": SECTORS.get(t.upper(), "?")}
        for t, p in cfg.portfolio.items()
    ]
    total = sum(p["invested_eur"] or 0 for p in positions) or 1.0
    sectors: dict[str, float] = {}
    for p in positions:
        sectors[p["sector"]] = sectors.get(p["sector"], 0.0) + (p["invested_eur"] or 0)
    return positions, {k: round(v / total * 100, 1) for k, v in sorted(
        sectors.items(), key=lambda kv: -kv[1])}


def _gather_context() -> dict:
    """Sammelt den Analyse-Kontext: breiter Markt-Scan statt der alten 4-Ticker-Liste."""
    from src.common.market_scan import etf_metrics, top_candidates

    cfg = cfg_mod.load()
    rates = hit_rate_stratified("daily_score", days=30)
    risk = _risk_scores_by_ticker()

    candidates = top_candidates(n=15)
    for c in candidates:
        if c["ticker"] in risk:
            c["risk_score"] = risk[c["ticker"]]

    positions, sector_weights = _portfolio_breakdown(cfg)

    return {
        "month_budget_eur":  cfg.settings.monatliches_budget_eur,
        "etf_fallback":      cfg.settings.dca_fallback_etf,
        "hit_rate":          rates,
        "candidates":        candidates,
        "etf_options":       etf_metrics(),
        "current_portfolio": positions,
        "sector_weights_pct": sector_weights,
    }


def _build_prompt(ctx: dict, exclude: set[str] | None = None) -> tuple[str, str]:
    exclude = {t.upper() for t in (exclude or set())}
    system = (
        "Du bist Analyst fuer einen monatlichen Sparplan mit ECHTEM Geld (Broker: Revolut, EUR).\n"
        "Du bekommst harte Kennzahlen zu ~15 vorgefilterten Aktien und den drei waehlbaren ETFs.\n"
        "Deine Aufgabe: EINEN Titel fuer diesen Monat auswaehlen — Einzelaktie ODER ETF.\n"
        "Antworte NUR im JSON-Format — keine Prosa drumherum.\n"
        "\n"
        "Output-Schema (strikt einhalten):\n"
        "{\n"
        '  "verdict":     "buy_single" | "buy_etf" | "skip",\n'
        '  "ticker":      "<ticker>",\n'
        '  "reason":      "<2-3 Saetze, allgemeinverstaendlich, mit konkreten Zahlen>",\n'
        '  "confidence":  "high" | "medium" | "low",\n'
        '  "alternative_etf": "<ticker>",\n'
        '  "risk_notes":  "<kurz>"\n'
        "}\n"
        "\n"
        "ENTSCHEIDENDE RANDBEDINGUNG — lies das zuerst:\n"
        "Dieser Sparplan hat KEINE Rotationsregel. Was gekauft wird, wird auf Jahre gehalten;\n"
        "verkauft wird nur bei einer echten Katastrophe. Ein Titel, der nur gerade heiss laeuft,\n"
        "ist deshalb NICHT geeignet — Momentum verfaellt, und ohne Umschichten bleibt der Schrott\n"
        "liegen. Du waehlst einen Titel, den man drei Jahre halten kann, nicht den Monatssieger.\n"
        "\n"
        "Regeln fuer verdict=buy_single (ALLE muessen erfuellt sein):\n"
        "1. Der Titel schlaegt den besten ETF im 6M-Momentum deutlich (mind. das 1,5-fache).\n"
        "2. Der Vorsprung ist nicht nur ein Strohfeuer: mom_12_1 (12-Monats-Trend OHNE den\n"
        "   letzten Monat) bestaetigt die Richtung und ist positiv.\n"
        "3. vol_annual unter 0.50 — mehr Schwankung ist fuer Buy-and-Hold ohne Stop nicht tragbar.\n"
        "4. dd_from_52w_high besser als -0.25 — ein Titel tief unter seinem Hoch ist kein Trend.\n"
        "5. Der Sektor macht im Depot noch keine 40% aus (sector_weights_pct beachten).\n"
        "6. Der Titel ist noch nicht im Depot ODER dort unter 25% Gewicht.\n"
        "Erfuellt KEIN Kandidat alle sechs Punkte: verdict=buy_etf.\n"
        "\n"
        "Regeln fuer verdict=buy_etf:\n"
        "- Waehle den ETF, der zum Depot passt: bei hoher Tech-Quote im Depot NICHT EQQQ.\n"
        "- Bei etwa gleichwertigen ETFs den breitesten nehmen (VWCE vor SPYL vor EQQQ).\n"
        "\n"
        "verdict=skip nur bei einem echten Grund (z.B. alle Daten unbrauchbar).\n"
        "Fallende Kurse sind KEIN Skip-Grund — guenstiger einkaufen ist der Sinn eines Sparplans.\n"
        "\n"
        "Ehrlichkeit vor Aktionismus: Wenn der breite ETF die vernuenftigere Wahl ist, sag das\n"
        "klar und begruende es mit Zahlen. Eine Einzelaktie zu empfehlen, nur damit eine\n"
        "Entscheidung stattgefunden hat, ist der teuerste Fehler in diesem System.\n"
    )
    cal = calibration_block("daily_score") + calibration_block("trade_decision") + calibration_block("monthly_dca")
    cands = [c for c in ctx["candidates"] if c["ticker"].upper() not in exclude]
    bench = _benchmark_hint()
    prompt = (
        f"{cal}\n\n" if cal else ""
    ) + (
        f"## Kaufkandidaten — breiter Scan ueber ~90 Large Caps, vorgefiltert auf Werte\n"
        f"   ueber dem 200-Tage-Schnitt, sortiert nach 6-Monats-Momentum.\n"
        f"   Alle Werte als Dezimalzahl (0.25 = +25%).\n"
        f"{json.dumps(cands, indent=2)}\n\n"
        f"## Waehlbare ETFs — NUR diese drei (auf Revolut in EUR handelbar), mit denselben Kennzahlen.\n"
        f"   Nutze EXAKT den Ticker-String inkl. Boersen-Endung '.DE':\n"
        f"{json.dumps(ctx['etf_options'], indent=2)}\n\n"
        f"## Aktuelles Depot:\n"
        f"{json.dumps(ctx['current_portfolio'], indent=2)}\n\n"
        f"## Sektor-Gewichte im Depot (Prozent):\n"
        f"{json.dumps(ctx['sector_weights_pct'], indent=2)}\n\n"
        f"## Budget diesen Monat:\n"
        f"{ctx['month_budget_eur']:.0f} EUR\n\n"
    ) + (f"## Bisherige Bilanz dieses Sparplans:\n{bench}\n\n" if bench else "") + (
        f"Default wenn unsicher: {ctx['etf_fallback']}\n\n"
        f"WICHTIG: Bei verdict=buy_etf MUSS 'ticker' einer dieser drei Strings sein.\n"
        f"'alternative_etf' MUSS immer einer dieser drei Strings sein (SPYL.DE | VWCE.DE | EQQQ.DE),\n"
        f"exakt so geschrieben, NICHT leer — auch bei verdict=buy_single (dient als sichtbare Alternative).\n\n"
        "Schreibe deine Empfehlung als JSON-Block."
    )
    return system, prompt


def _benchmark_hint() -> str:
    """Bisherige Trefferbilanz gegen den Welt-ETF — das LLM soll wissen, ob seine
    eigenen Einzeltitel-Wetten in der Vergangenheit tatsaechlich getragen haben."""
    try:
        from src.common.dca_benchmark import summary_line
        return summary_line()
    except Exception as e:
        log.warning(f"Benchmark-Bilanz nicht ladbar: {e}")
        return ""


def _disp(ticker: str) -> str:
    """Blendet die Boersen-Endung (.DE/.L/...) fuer die Telegram-Anzeige aus —
    Mert sieht 'SPYL' statt 'SPYL.DE'."""
    for suf in (".DE", ".L", ".AS", ".PA", ".MI"):
        if ticker.endswith(suf):
            return ticker[: -len(suf)]
    return ticker


def _build_telegram_text(verdict: str, data: dict, prediction_id: int, budget_eur: float) -> tuple[str, dict]:
    """Returns (HTML-text, reply_markup-dict)."""
    if verdict == "skip":
        text = (
            f"⏸ <b>DCA diesen Monat: SKIP</b>\n"
            f"<i>{escape(data.get('reason', ''))}</i>"
        )
        return text, {}

    is_etf = verdict == "buy_etf"
    ticker = data.get("ticker", "?")
    reason = data.get("reason", "")
    conf   = data.get("confidence", "?")
    risk   = data.get("risk_notes", "")
    alt    = data.get("alternative_etf") or "VWCE.DE"   # or-Operator catched empty strings

    art = "ETF · breit gestreut" if is_etf else "Einzeltitel"
    parts = [
        f"📈 <b>Monatlicher Invest-Vorschlag · {escape(_disp(ticker))}</b>",
        f"Art: <i>{art}</i>",
        f"Budget: <b>{budget_eur:.0f} EUR</b>",
        f"Konfidenz: <i>{escape(conf)}</i>",
        "",
        f"{escape(reason)}",
    ]
    if risk:
        parts.append(f"\n<i>Risiko: {escape(risk)}</i>")
    if not is_etf and alt:
        parts.append(f"\n<i>Alternativer ETF-Korb falls unsicher: <b>{escape(_disp(alt))}</b></i>")
    text = "\n".join(parts)

    reply_markup = {"inline_keyboard": [[
        {"text": f"✅ {ticker} gekauft",  "callback_data": f"dca:{prediction_id}:bought"},
        {"text": f"⚪ {alt} (ETF) gekauft", "callback_data": f"dca:{prediction_id}:etf"},
        {"text": "⏸ skip",                 "callback_data": f"dca:{prediction_id}:skip"},
    ]]}
    return text, reply_markup


def _send_html_with_markup(text: str, reply_markup: dict) -> bool:
    """Custom Helper — notifier.send_alert ist alert-spezifisch, nutzen wir hier nicht."""
    try:
        from src.alerts.notifier import _send_message
        result = _send_message(text, reply_markup if reply_markup else None)
        return bool(result.get("ok", False))
    except Exception as e:
        log.error(f"DCA-telegram send failed: {e}")
        return False


def _persist_config_change(label: str) -> None:
    """Delegiert an die eine gepflegte Fassung in buy.py.

    Hier stand bis 2026-08-10 eine wortgleiche Kopie — und damit auch der
    Datenverlust-Fehler, der am 01.08. den UNH-Kauf verschluckt hat (Details im
    Docstring von buy.persist_config_change). Eine Kopie zu fixen und die andere
    zu vergessen ist genau die Falle, die wir hier zumachen."""
    from scripts.buy import persist_config_change
    persist_config_change(label)


def _auto_record_dca(verdict: str, data: dict, budget_eur: float, pred_id) -> str:
    """Bucht die DCA-Empfehlung automatisch ins config.yaml-Portfolio-Ledger ein
    (Voll-Autonomie, kein Telegram-Button) und loggt das Feedback fuer den
    Lern-Loop. Returns Status-Text fuer die Telegram-Info."""
    from scripts.buy import record_position, _guess_currency
    from src.common.predictions import log_feedback
    cfg = cfg_mod.load()
    fallback_etf = (data.get("alternative_etf") or cfg.settings.dca_fallback_etf or "VWCE.DE").upper()
    # Bei buy_etf ist die empfohlene ETF 'ticker' (nicht alternative_etf) — sonst wich die
    # Buchung von der Empfehlung ab. fallback_etf dient nur als Notausweg beim Konzentrations-Block.
    ticker = (data.get("ticker") or fallback_etf).upper()

    # Konzentrations-Check: bei Block auf ETF-Fallback ausweichen
    if cfg.concentration_check(ticker, budget_eur).get("blocks"):
        if fallback_etf != ticker and not cfg.concentration_check(fallback_etf, budget_eur).get("blocks"):
            ticker = fallback_etf
        else:
            return f"NICHT eingetragen (Konzentrations-Limit): {ticker}"

    # Aktuellen Preis holen -> shares berechnen (best-effort, sonst nur invested_eur)
    shares = price = None
    try:
        from src.common.data_loader import get_prices
        px = get_prices(ticker, period="5d")
        if px is not None and len(px) > 0:
            price = float(px["close"].iloc[-1])
            if _guess_currency(ticker) == "USD":
                from src.common.fx import eur_per_usd
                fx = eur_per_usd()
                native = budget_eur / fx if fx else budget_eur
            else:
                native = budget_eur
            shares = round(native / price, 6) if price else None
    except Exception as e:
        log.warning(f"DCA-Preis fuer {ticker} nicht ermittelbar: {e}")

    msg = record_position(ticker, budget_eur, shares=shares, price=price,
                          entry=cfg.entry_by_ticker(ticker))
    # config.yaml committen+pushen, sonst verwirft auto-pull/status-push die Aenderung
    _persist_config_change(f"Auto-DCA {ticker} {budget_eur:.0f}EUR @ {price}")
    if pred_id is not None:
        try:
            log_feedback(pred_id, feedback_type="dca_bought",
                         reason_text=f"auto-recorded {ticker} {budget_eur:.0f}EUR @ {price}")
        except Exception:
            pass
    return msg


def main(dry_run: bool = False) -> int:
    if not llm_configured():
        log.warning("ANTHROPIC_API_KEY nicht gesetzt — monthly_dca skipped")
        # Dennoch ein Telegram-Hint senden falls Notifier konfiguriert
        if notifier.is_configured():
            notifier.send_info(
                "ℹ️ <b>monthly_dca</b> uebersprungen — ANTHROPIC_API_KEY in .env fehlt.",
                label="dca_skip",
            )
        return 0

    if not notifier.is_configured() and not dry_run:
        log.warning("Telegram nicht konfiguriert — DCA-Empfehlung kann nicht zugestellt werden")
        return 1

    ctx = _gather_context()

    system, prompt = _build_prompt(ctx)
    result = call_sonnet(
        system=system,
        prompt=prompt,
        job_source="monthly_dca",
        subject_type="portfolio",
        subject_id=None,
        input_summary=f"DCA-Recommendation, {len(ctx['candidates'])} Kandidaten, {len(ctx['current_portfolio'])} Positionen",
        max_tokens=800,
        temperature=0.2,
        estimated_cost_eur=0.04,
    )
    if not result.ok:
        log.error(f"sonnet call failed: {result.error}")
        notifier.send_info(f"❌ <b>monthly_dca</b> failed: {escape(result.error or '?')}", label="dca_error")
        return 1

    data = result.parsed_json or safe_parse(result.text, default={})
    verdict = data.get("verdict", "skip")

    if dry_run:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print(f"\n[dry-run] verdict={verdict} — NICHTS gebucht, NICHTS gesendet. "
              f"Kosten: {result.cost_eur:.4f} EUR")
        return 0

    # AUTOMATISCH ins Portfolio-Ledger eintragen (Voll-Autonomie, kein Button).
    record_msg = ""
    if verdict in ("buy_single", "buy_etf"):
        try:
            record_msg = _auto_record_dca(verdict, data, ctx["month_budget_eur"], result.prediction_id)
        except Exception as e:
            log.error(f"Auto-DCA-Eintrag fehlgeschlagen: {e}")
            record_msg = f"FEHLER beim Eintragen: {e}"

    # Pick + gleichzeitigen Benchmark-Kurs mitschreiben, damit spaeter messbar ist,
    # ob diese Entscheidung besser war als stumpf den Welt-ETF zu kaufen.
    if verdict in ("buy_single", "buy_etf"):
        try:
            from src.common.dca_benchmark import record_pick
            record_pick(verdict=verdict,
                        ticker=(data.get("ticker") or ctx["etf_fallback"]),
                        budget_eur=ctx["month_budget_eur"],
                        prediction_id=result.prediction_id,
                        note=(data.get("reason") or "")[:200])
        except Exception as e:
            log.error(f"Benchmark-Eintrag fehlgeschlagen: {e}")

    text, _markup = _build_telegram_text(
        verdict, data,
        prediction_id=result.prediction_id,
        budget_eur=ctx["month_budget_eur"],
    )
    if record_msg:
        text += f"\n\n✅ <b>Automatisch ins Portfolio eingetragen:</b>\n{escape(record_msg)}"

    # Laufende Beweisfuehrung: schlaegt der Sparplan den Welt-ETF oder nicht?
    bilanz = _benchmark_hint()
    if bilanz:
        text += f"\n\n\U0001f4ca <i>{escape(bilanz)}</i>"

    # Voll-Autonomie: informativ, KEINE interaktiven Buttons.
    ok = _send_html_with_markup(text, {})
    print(f"DCA pred_id={result.prediction_id} verdict={verdict} recorded={bool(record_msg)} cost_eur={result.cost_eur:.4f}")
    return 0 if ok else 1


def send_replacement_recommendation(sold_ticker: str) -> bool:
    """Erzeugt nach einem Verkauf einen Ersatz-Vorschlag (Sonnet, Einzeltitel oder
    ETF) und schickt ihn mit Bestaetigungs-Buttons an Mert. Schliesst den gerade
    verkauften Titel + aktuelle Holdings aus. KEIN Auto-Eintrag — Mert bestaetigt
    ueber die 'gekauft'-Buttons (dca:...), was ihn dann als DCA-Holding einbucht.
    Aufgerufen aus dem Verkauft-Callback (telegram_callbacks._process_dca_sell)."""
    if not llm_configured():
        notifier.send_action_required(
            f"\u2139\ufe0f Ersatz fuer <b>{escape(_disp(sold_ticker))}</b>: automatischer "
            f"Vorschlag nicht moeglich (ANTHROPIC_API_KEY fehlt). Bitte manuell waehlen.",
            label="dca_replace")
        return False

    ctx = _gather_context()
    exclude = {sold_ticker.upper()} | {p["ticker"].upper() for p in ctx["current_portfolio"]}
    system, prompt = _build_prompt(ctx, exclude=exclude)
    prompt = (
        f"## ERSATZ-EMPFEHLUNG: Mert hat {sold_ticker} gerade verkauft.\n"
        f"Empfiehl EINEN Ersatz. NICHT empfehlen (verkauft oder schon im Depot): "
        f"{sorted(exclude)}\n\n"
    ) + prompt

    result = call_sonnet(
        system=system,
        prompt=prompt,
        job_source="monthly_dca",
        subject_type="portfolio",
        subject_id=None,
        input_summary=f"DCA-Ersatz nach Verkauf {sold_ticker}",
        max_tokens=800,
        temperature=0.2,
        estimated_cost_eur=0.04,
    )
    if not result.ok:
        notifier.send_action_required(
            f"\u274c Ersatz-Vorschlag fuer {escape(_disp(sold_ticker))} fehlgeschlagen: "
            f"{escape(result.error or '?')}", label="dca_replace")
        return False

    data = result.parsed_json or safe_parse(result.text, default={})
    verdict = data.get("verdict", "skip")
    text, markup = _build_telegram_text(
        verdict, data,
        prediction_id=result.prediction_id,
        budget_eur=ctx["month_budget_eur"],
    )
    text = f"\U0001f504 <b>Ersatz fuer verkaufte {escape(_disp(sold_ticker))}</b>\n\n" + text
    return notifier.send_action_required(
        text, label="dca_replace",
        reply_markup=markup if markup else None)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Monatlicher Sparplan-Vorschlag")
    ap.add_argument("--dry-run", action="store_true",
                    help="Empfehlung berechnen und ausgeben, aber NICHT buchen und NICHT senden")
    sys.exit(main(dry_run=ap.parse_args().dry_run))
