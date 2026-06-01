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


def _gather_context() -> dict:
    """Sammelt input-Kontext fuer Sonnet."""
    cfg = cfg_mod.load()
    rates = hit_rate_stratified("daily_score", days=30)

    # Top 5 Tickers nach niedrigstem composite (= attractiv) der letzten 24h
    sql = """
        SELECT subject_id, output_json, confidence, created_at
          FROM predictions
         WHERE job_source = 'daily_score'
           AND created_at >= datetime('now', '-24 hour')
         ORDER BY created_at DESC
    """
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(sql).fetchall()

    seen = {}
    for r in rows:
        if r["subject_id"] in seen:
            continue
        out = safe_parse(r["output_json"] or "{}", default={})
        seen[r["subject_id"]] = {
            "ticker":      r["subject_id"],
            "composite":   float(out.get("composite", 100)),
            "alert_level": int(out.get("alert_level", 0)),
            "triggered":   out.get("triggered_dims", []),
            "confidence":  r["confidence"],
        }
    candidates = sorted(seen.values(), key=lambda x: x["composite"])[:10]

    return {
        "month_budget_eur":  cfg.settings.monatliches_budget_eur,
        "etf_fallback":      cfg.settings.dca_fallback_etf,
        "hit_rate":          rates,
        "candidates":        candidates,
        "current_portfolio": [
            {"ticker": t, "invested_eur": p.invested_eur}
            for t, p in cfg.portfolio.items()
        ],
        "tradeable_universe": [e.ticker for e in cfg.universe if e.ring in (1, 2, 3)],
    }


def _build_prompt(ctx: dict) -> tuple[str, str]:
    system = (
        "Du bist ein vorsichtiger Investment-Berater fuer einen diversifizierten DCA-Plan (Sektor-ETFs, Blue Chips, Tech).\n"
        "Mert investiert monatlich 50 EUR. Heute soll ein einziger Titel empfohlen werden — oder ein ETF-Fallback wenn nichts ueberzeugt.\n"
        "Antworte NUR im JSON-Format wie unten beschrieben — kein Prosa drumherum.\n"
        "\n"
        "Output-Schema (strikt einhalten):\n"
        "{\n"
        '  "verdict":     "buy_single" | "buy_etf" | "skip",\n'
        '  "ticker":      "<ticker>",\n'
        '  "reason":      "<2-3 sentences why>",\n'
        '  "confidence":  "high" | "medium" | "low",\n'
        '  "alternative_etf": "<ticker>",\n'
        '  "risk_notes":  "<short>"\n'
        "}\n"
        "\n"
        "Conservative Heuristik:\n"
        "- Nur Tickers mit composite < 30 UND alert_level == 0 sind Buy-Kandidaten.\n"
        "- Bei <2 Buy-Kandidaten: ETF-Fallback empfehlen.\n"
        "- Vermeide Konzentration: wenn Mert in einem Ticker schon >40% hat, anderen vorschlagen.\n"
        "- Wenn die letzte 30d Hit-Rate unter 50% war: confidence senken.\n"
    )
    cal = calibration_block("daily_score") + calibration_block("trade_decision") + calibration_block("monthly_dca")
    prompt = (
        f"{cal}\n\n" if cal else ""
    ) + (
        f"## Top-10 Buy-Kandidaten nach niedrigstem Risk-Composite (letzte 24h):\n"
        f"{json.dumps(ctx['candidates'], indent=2)}\n\n"
        f"## Aktuelles Portfolio:\n"
        f"{json.dumps(ctx['current_portfolio'], indent=2)}\n\n"
        f"## Budget diesen Monat:\n"
        f"{ctx['month_budget_eur']:.0f} EUR\n\n"
        f"## Verfuegbare ETFs (waehle einen davon als alternative_etf):\n"
        f"  SPY   - SPDR S&P 500 ETF (Gesamtmarkt, breit gestreut)\n"
        f"  QQQ   - Invesco QQQ Trust (Nasdaq 100, Tech-lastig)\n"
        f"  SMH   - VanEck Semiconductor ETF (Halbleiter)\n"
        f"  XLK   - Technology Select Sector SPDR\n"
        f"  XLV   - Health Care Select Sector SPDR\n"
        f"  XLF   - Financial Select Sector SPDR\n"
        f"\nDefault wenn unsicher: {ctx['etf_fallback']}\n\n"
        f"WICHTIG: alternative_etf MUSS einer der obigen Tickers sein, NICHT leer.\n"
        f"Auch bei verdict=buy_single — der ETF dient als sichtbare Alternative im UI.\n\n"
        "Schreibe deine Empfehlung als JSON-Block."
    )
    return system, prompt


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
    alt    = data.get("alternative_etf") or "SMH"   # or-Operator catched empty strings

    label = "ETF-Fallback" if is_etf else "Empfohlener Buy"
    emoji = "⚪" if is_etf else "🎯"
    parts = [
        f"{emoji} <b>{label} · {escape(ticker)}</b>",
        f"Budget: <b>{budget_eur:.0f} EUR</b>",
        f"Konfidenz: <i>{escape(conf)}</i>",
        "",
        f"{escape(reason)}",
    ]
    if risk:
        parts.append(f"\n<i>Risiko: {escape(risk)}</i>")
    if not is_etf and alt:
        parts.append(f"\n<i>Alternativer ETF-Korb falls unsicher: <b>{escape(alt)}</b></i>")
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
    """Committet+pusht die config.yaml-Aenderung, damit sie nicht vom auto-pull/
    status-push (git reset --hard origin/main) verworfen wird. Best-effort mit
    rebase, falls remote zwischenzeitlich vorrueckte."""
    import subprocess
    repo = str(Path(__file__).resolve().parents[1])
    def _git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=30)
    try:
        _git("add", "config.yaml")
        if _git("commit", "-m", f"portfolio: {label}").returncode != 0:
            return  # nichts zu committen
        if _git("pull", "--rebase", "--no-edit").returncode != 0:
            _git("rebase", "--abort")
        if _git("push").returncode != 0:
            _git("push", "--force-with-lease")
    except Exception as e:
        log.error(f"config.yaml commit/push fehlgeschlagen: {e}")


def _auto_record_dca(verdict: str, data: dict, budget_eur: float, pred_id) -> str:
    """Bucht die DCA-Empfehlung automatisch ins config.yaml-Portfolio-Ledger ein
    (Voll-Autonomie, kein Telegram-Button) und loggt das Feedback fuer den
    Lern-Loop. Returns Status-Text fuer die Telegram-Info."""
    from scripts.buy import record_position, _guess_currency
    from src.common.predictions import log_feedback
    cfg = cfg_mod.load()
    fallback_etf = (data.get("alternative_etf") or cfg.settings.dca_fallback_etf or "SMH").upper()
    ticker = (data.get("ticker") if verdict == "buy_single" else fallback_etf) or fallback_etf
    ticker = ticker.upper()

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


def main() -> int:
    if not llm_configured():
        log.warning("ANTHROPIC_API_KEY nicht gesetzt — monthly_dca skipped")
        # Dennoch ein Telegram-Hint senden falls Notifier konfiguriert
        if notifier.is_configured():
            notifier.send_info(
                "ℹ️ <b>monthly_dca</b> uebersprungen — ANTHROPIC_API_KEY in .env fehlt.",
                label="dca_skip",
            )
        return 0

    if not notifier.is_configured():
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

    # AUTOMATISCH ins Portfolio-Ledger eintragen (Voll-Autonomie, kein Button).
    record_msg = ""
    if verdict in ("buy_single", "buy_etf"):
        try:
            record_msg = _auto_record_dca(verdict, data, ctx["month_budget_eur"], result.prediction_id)
        except Exception as e:
            log.error(f"Auto-DCA-Eintrag fehlgeschlagen: {e}")
            record_msg = f"FEHLER beim Eintragen: {e}"

    text, _markup = _build_telegram_text(
        verdict, data,
        prediction_id=result.prediction_id,
        budget_eur=ctx["month_budget_eur"],
    )
    if record_msg:
        text += f"\n\n✅ <b>Automatisch ins Portfolio eingetragen:</b>\n{escape(record_msg)}"

    # Voll-Autonomie: informativ, KEINE interaktiven Buttons.
    ok = _send_html_with_markup(text, {})
    print(f"DCA pred_id={result.prediction_id} verdict={verdict} recorded={bool(record_msg)} cost_eur={result.cost_eur:.4f}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
