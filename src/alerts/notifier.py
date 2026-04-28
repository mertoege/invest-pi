"""
Telegram-Notifier.

Schickt Alerts + Info-Nachrichten an Merts persoenlichen Telegram-Chat
via Bot-API. HTML parse_mode (LESSONS Bug-Story 5: NIE Markdown).

Bei Stufe-2/3-Alerts werden Inline-Buttons gerendert:
  ✅ habe verkauft
  ❌ false positive
  🤷 ignoriere

Klick auf einen Button schickt eine callback_query an den Bot — wird in
Phase 3b von src/jobs/telegram_callbacks.py per Cron-Poll abgegriffen
und in feedback_reasons-Tabelle geschrieben.

Konfiguration via env:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
Fallback: api_keys-Section aus config.yaml.

Wenn nicht konfiguriert, no-op + log warning. Keine Crashes — Alert-Versand
darf den Score-Run nicht killen.
"""

from __future__ import annotations

from html import escape as _esc

import json
import logging
import os
from pathlib import Path
from typing import Optional

from ..common.retry import api_retry
from ..common.storage import ALERTS_DB, connect


_API_BASE = "https://api.telegram.org"
log = logging.getLogger("invest_pi.notifier")


# ────────────────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────────────────
def _from_yaml(key: str) -> Optional[str]:
    try:
        import yaml
        cfg = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "config.yaml").read_text())
        v = (cfg or {}).get("api_keys", {}).get(key)
        return v if v else None
    except Exception:
        return None


def _bot_token() -> Optional[str]:
    return os.environ.get("TELEGRAM_BOT_TOKEN") or _from_yaml("telegram_bot_token")


def _chat_id() -> Optional[str]:
    return os.environ.get("TELEGRAM_CHAT_ID") or _from_yaml("telegram_chat_id")


def is_configured() -> bool:
    return bool(_bot_token() and _chat_id())


# ────────────────────────────────────────────────────────────
# LOW-LEVEL SEND
# ────────────────────────────────────────────────────────────
@api_retry(attempts=3, min_wait=2, max_wait=10)
def _send_message(text: str, reply_markup: Optional[dict] = None) -> dict:
    """Direkter Telegram-API-Call. Raises bei Fehler."""
    import requests
    token = _bot_token()
    chat = _chat_id()
    if not (token and chat):
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID nicht gesetzt")
    payload = {
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",      # LESSONS Bug 5: NIE Markdown
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(
        f"{_API_BASE}/bot{token}/sendMessage",
        json=payload, timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _log_notification(
    ticker: str, level: int, payload: str,
    delivered: bool, prediction_id: Optional[int] = None,
) -> None:
    try:
        with connect(ALERTS_DB) as conn:
            conn.execute(
                """
                INSERT INTO notifications
                    (ticker, timestamp, level, channel, delivered, payload, prediction_id)
                VALUES (?, datetime('now'), ?, 'telegram', ?, ?, ?)
                """,
                (ticker, level, 1 if delivered else 0, payload, prediction_id),
            )
    except Exception as e:
        log.warning(f"konnte notifications-row nicht schreiben: {e}")


# ────────────────────────────────────────────────────────────
# HIGH-LEVEL APIs
# ────────────────────────────────────────────────────────────
_LEVEL_LABEL = {0: "Green", 1: "Watch", 2: "Caution", 3: "Red"}
_LEVEL_EMOJI = {0: "🟢", 1: "🟡", 2: "🟠", 3: "🔴"}


def send_alert(
    *,
    ticker:             str,
    alert_level:        int,
    composite:          float,
    triggered_dims:     list,
    prediction_id:      Optional[int] = None,
    dimensions_summary: Optional[str] = None,
) -> bool:
    """
    Risk-Alert an Telegram. Bei alert_level >= 2 mit Inline-Buttons.
    Returns True wenn der Telegram-API-Call erfolgreich war.
    """
    if not is_configured():
        log.info(f"telegram nicht konfiguriert, skip alert {ticker}")
        return False

    label = _LEVEL_LABEL.get(alert_level, "?")
    emoji = _LEVEL_EMOJI.get(alert_level, "⚪")

    triggered_str = ", ".join(_esc(d) for d in triggered_dims) if triggered_dims else "—"
    parts = [
        f"{emoji} <b>{label} Alert · {_esc(ticker)}</b>",
        f"Composite: <b>{composite:.1f}</b> / 100",
        f"Triggered: {triggered_str}",
    ]
    if dimensions_summary:
        parts.append("")
        # dimensions_summary darf <b>/<i>/<code> Tags enthalten — kein full-escape,
        # aber raw < Zeichen nicht gewollt. Caller ist verantwortlich, hier passthrough.
        parts.append(dimensions_summary)
    text = "\n".join(parts)

    reply_markup = None
    if alert_level >= 2 and prediction_id is not None:
        reply_markup = {
            "inline_keyboard": [[
                {"text": "✅ habe verkauft",  "callback_data": f"fb:{prediction_id}:sold"},
                {"text": "❌ false positive", "callback_data": f"fb:{prediction_id}:fp"},
                {"text": "🤷 ignoriere",       "callback_data": f"fb:{prediction_id}:ignore"},
            ]]
        }

    try:
        result = _send_message(text, reply_markup)
        delivered = bool(result.get("ok", False))
        _log_notification(ticker, alert_level, text, delivered, prediction_id)
        return delivered
    except Exception as e:
        log.error(f"telegram send_alert {ticker} failed: {e}")
        _log_notification(ticker, alert_level, str(e), False, prediction_id)
        return False


def send_trade(
    *,
    ticker:    str,
    side:      str,
    qty:       float,
    eur:       float,
    price_usd: float,
    reason:    str = "",
    paper:     bool = True,
) -> bool:
    """Info-Nachricht über einen ausgeführten Trade. Keine Buttons."""
    if not is_configured():
        return False
    badge = "📝 Paper" if paper else "💰 LIVE"
    arrow = "🟢" if side == "buy" else "🔴"
    text_parts = [
        f"{arrow} <b>{_esc(side.upper())} {_esc(ticker)}</b>  {badge}",
        f"Qty: <b>{qty}</b>  ({eur:.2f} EUR @ {price_usd:.2f} USD)",
    ]
    if reason:
        text_parts.append(f"<i>{_esc(reason)}</i>")
    text = "\n".join(text_parts)
    try:
        result = _send_message(text, None)
        delivered = bool(result.get("ok", False))
        _log_notification(ticker, 0, text, delivered)
        return delivered
    except Exception as e:
        log.error(f"telegram send_trade {ticker} failed: {e}")
        return False


def send_info(message: str, *, label: str = "info") -> bool:
    """Allzweck-Info-Nachricht. Wird auch fuer Drift-Warnungen + DCA-Empfehlungen genutzt."""
    if not is_configured():
        return False
    try:
        result = _send_message(message, None)
        delivered = bool(result.get("ok", False))
        _log_notification(label, 0, message, delivered)
        return delivered
    except Exception as e:
        log.error(f"telegram send_info failed: {e}")
        return False
