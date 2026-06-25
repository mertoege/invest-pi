"""
Telegram-Callback-Handler.

Wird alle 60s vom systemd-Timer aufgerufen. Polled getUpdates, parsed
callback_queries, schreibt feedback in feedback_reasons-Tabelle.

callback_data Format:
  "fb:{prediction_id}:{action}"     - Erste Klick-Reaktion (sold/fp/ignore)
  "fbr:{prediction_id}:{reason}"    - Reason-Folgefrage bei fp
                                        (macro/sector/insider/news/other)
  "dca:{prediction_id}:{action}"    - DCA-Bestaetigung (bought/etf/skip)
  Text-Antwort auf den 'gekauft'-Prompt: "<Kurs> <Betrag>" -> echte Kaufwerte

Update-Offset wird in data/.telegram_offset persistiert damit kein update
doppelt processed wird.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# Path-Setup
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.alerts.notifier import _bot_token, _chat_id, is_configured
from src.common.predictions import log_feedback
from src.common.retry import api_retry
from src.common.storage import DATA_DIR

log = logging.getLogger("invest_pi.telegram_callbacks")

OFFSET_FILE = DATA_DIR / ".telegram_offset"
API_BASE = "https://api.telegram.org"


def _load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    try:
        OFFSET_FILE.write_text(str(offset))
    except Exception as e:
        log.warning(f"konnte offset nicht speichern: {e}")


@api_retry(attempts=3, min_wait=2, max_wait=10)
def _get_updates(offset: int, timeout: int = 5) -> list[dict]:
    import requests
    token = _bot_token()
    if not token:
        return []
    resp = requests.get(
        f"{API_BASE}/bot{token}/getUpdates",
        params={"offset": offset, "timeout": timeout},
        timeout=timeout + 5,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        log.warning(f"getUpdates returned not-ok: {data}")
        return []
    return data.get("result", [])


@api_retry(attempts=2, min_wait=1, max_wait=4)
def _answer_callback(callback_query_id: str, text: str = "") -> bool:
    """Macht den Spinner auf dem Inline-Button weg."""
    import requests
    token = _bot_token()
    if not token:
        return False
    resp = requests.post(
        f"{API_BASE}/bot{token}/answerCallbackQuery",
        json={"callback_query_id": callback_query_id, "text": text[:200]},
        timeout=10,
    )
    return resp.ok and resp.json().get("ok", False)


@api_retry(attempts=2, min_wait=1, max_wait=4)
def _edit_message_markup(chat_id, message_id, reply_markup: Optional[dict]) -> bool:
    """Aktualisiert die Buttons einer existierenden Nachricht."""
    import requests
    token = _bot_token()
    if not token:
        return False
    payload = {"chat_id": chat_id, "message_id": message_id}
    if reply_markup is None:
        payload["reply_markup"] = json.dumps({"inline_keyboard": []})
    else:
        payload["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(
        f"{API_BASE}/bot{token}/editMessageReplyMarkup",
        json=payload,
        timeout=10,
    )
    return resp.ok and resp.json().get("ok", False)


@api_retry(attempts=2, min_wait=1, max_wait=4)
def _send_message(text: str, reply_markup: Optional[dict] = None,
                  reply_to_message_id: Optional[int] = None) -> Optional[dict]:
    """Sendet eine HTML-Nachricht. Gibt das result-dict zurueck oder None."""
    import requests
    token = _bot_token(); chat = _chat_id()
    if not token or not chat:
        return None
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    resp = requests.post(f"{API_BASE}/bot{token}/sendMessage", json=payload, timeout=10)
    if resp.ok and resp.json().get("ok"):
        return resp.json().get("result")
    return None


def _ticker_for_pred(pred_id: int) -> Optional[str]:
    try:
        from src.common.storage import LEARNING_DB, connect
        with connect(LEARNING_DB) as conn:
            row = conn.execute(
                """SELECT subject_id, json_extract(output_json, '$.ticker') AS ticker_out
                   FROM predictions WHERE id = ?""", (pred_id,)).fetchone()
        if row:
            return row["subject_id"] or row["ticker_out"]
    except Exception as e:
        log.warning(f"ticker lookup failed: {e}")
    return None


# Marker, an dem wir eine Kauf-Antwort wiedererkennen (steht in der Prompt-Nachricht):
_BUY_INPUT_MARKER = "dca-input #"


def _send_buy_input_prompt(ticker: Optional[str], pred_id: int) -> None:
    """Fragt nach dem 'gekauft'-Klick den echten Kaufpreis + Betrag ab (force_reply)."""
    tk = ticker or "?"
    text = (
        f"\U0001f6d2 <b>{tk}</b> als gekauft markiert.\n"
        f"Wie viel genau? Antworte mit <b>Kurs</b> und <b>Betrag (\u20ac)</b> \u2014 "
        f"z.B. <code>169.68 50</code>\n"
        f"<i>{_BUY_INPUT_MARKER}{pred_id} \u00b7 {tk}</i>"
    )
    markup = {"force_reply": True, "input_field_placeholder": "Kurs Betrag, z.B. 169.68 50"}
    _send_message(text, reply_markup=markup)


def _process_buy_input(message: dict) -> None:
    """Antwort auf den 'gekauft'-Prompt: '<Kurs> <Betrag>' -> echte Werte speichern."""
    import re
    text = message.get("text", "") or ""
    prompt = (message.get("reply_to_message", {}) or {}).get("text", "") or ""
    m = re.search(r"dca-input #(\d+)", prompt)
    if not m:
        return
    pred_id = int(m.group(1))
    nums = re.findall(r"\d+(?:[.,]\d+)?", text)
    if len(nums) < 2:
        _send_message(
            "Ich brauche <b>zwei</b> Zahlen: Kurs und Betrag (\u20ac). z.B. <code>169.68 50</code>",
            reply_to_message_id=message.get("message_id"))
        return
    price = float(nums[0].replace(",", "."))
    amount = float(nums[1].replace(",", "."))
    reason = f"buy_price={price} amount_eur={amount}"
    updated = False
    try:
        from src.common.storage import LEARNING_DB, connect
        with connect(LEARNING_DB) as conn:
            row = conn.execute(
                "SELECT id FROM feedback_reasons WHERE prediction_id=? "
                "AND feedback_type='dca_bought' ORDER BY created_at DESC LIMIT 1",
                (pred_id,)).fetchone()
            if row:
                conn.execute("UPDATE feedback_reasons SET reason_text=? WHERE id=?",
                             (reason, row["id"]))
                conn.commit()
                updated = True
    except Exception as e:
        log.error(f"buy-input speichern fehlgeschlagen: {e}")
        _send_message("\u26a0\ufe0f Konnte die Werte nicht speichern.",
                      reply_to_message_id=message.get("message_id"))
        return
    if not updated:
        log_feedback(pred_id, feedback_type="dca_bought", reason_text=reason)
    tk = _ticker_for_pred(pred_id) or "?"
    _send_message(f"\u2713 Gespeichert: <b>{tk}</b> \u2014 \u20ac{amount:.2f} investiert @ {price} je Aktie.",
                  reply_to_message_id=message.get("message_id"))


# ────────────────────────────────────────────────────────────
# CALLBACK PROCESSING
# ────────────────────────────────────────────────────────────
_REASON_LABEL = {
    "macro":   "🌐 Macro-Lärm",
    "sector":  "💸 Sektor",
    "insider": "🕵 Insider",
    "news":    "📰 News",
    "other":   "✏️ Andere",
}


def _process_fb(callback_query: dict) -> None:
    """fb:{pred_id}:{action} — erste Klick-Reaktion."""
    data    = callback_query["data"]
    cq_id   = callback_query["id"]
    msg     = callback_query.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    msg_id  = msg.get("message_id")

    parts = data.split(":")
    if len(parts) < 3:
        _answer_callback(cq_id, "ungueltiges callback")
        return

    _, pred_id_str, action = parts[0], parts[1], parts[2]
    try:
        pred_id = int(pred_id_str)
    except ValueError:
        _answer_callback(cq_id, "ungueltiges pred_id")
        return

    if action in ("sold", "ignore"):
        # Direkt loggen + Buttons entfernen
        log_feedback(pred_id, feedback_type=action)
        _edit_message_markup(chat_id, msg_id, None)
        _answer_callback(cq_id, f"✓ {action} gespeichert")
        log.info(f"feedback recorded: pred={pred_id} action={action}")

    elif action == "fp":
        # Reason-Folgefrage
        log_feedback(pred_id, feedback_type="false_positive")
        new_markup = {"inline_keyboard": [
            [
                {"text": "🌐 Macro",   "callback_data": f"fbr:{pred_id}:macro"},
                {"text": "💸 Sektor",  "callback_data": f"fbr:{pred_id}:sector"},
            ],
            [
                {"text": "🕵 Insider", "callback_data": f"fbr:{pred_id}:insider"},
                {"text": "📰 News",    "callback_data": f"fbr:{pred_id}:news"},
            ],
            [{"text": "✏️ Andere",     "callback_data": f"fbr:{pred_id}:other"}],
        ]}
        _edit_message_markup(chat_id, msg_id, new_markup)
        _answer_callback(cq_id, "Warum war's ein FP?")
        log.info(f"false_positive recorded, reason-question shown: pred={pred_id}")
    else:
        _answer_callback(cq_id, f"unbekannte action: {action}")


def _process_fbr(callback_query: dict) -> None:
    """fbr:{pred_id}:{reason} — Reason-Folgefrage."""
    data    = callback_query["data"]
    cq_id   = callback_query["id"]
    msg     = callback_query.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    msg_id  = msg.get("message_id")

    parts = data.split(":")
    if len(parts) < 3:
        _answer_callback(cq_id, "ungueltig")
        return

    _, pred_id_str, reason = parts[0], parts[1], parts[2]
    try:
        pred_id = int(pred_id_str)
    except ValueError:
        _answer_callback(cq_id, "ungueltiges pred_id")
        return

    log_feedback(pred_id, feedback_type="false_positive", reason_code=reason)
    _edit_message_markup(chat_id, msg_id, None)
    label = _REASON_LABEL.get(reason, reason)
    _answer_callback(cq_id, f"✓ {label} notiert")
    log.info(f"reason recorded: pred={pred_id} reason={reason}")


def process_update(update: dict) -> None:
    cq = update.get("callback_query")
    if cq:
        data = cq.get("data", "")
        if data.startswith("fb:"):
            _process_fb(cq)
        elif data.startswith("fbr:"):
            _process_fbr(cq)
        elif data.startswith("dca:"):
            _process_dca(cq)
        else:
            _answer_callback(cq["id"], "unbekanntes callback")
        return
    msg = update.get("message")
    if msg and msg.get("text"):
        prompt = (msg.get("reply_to_message", {}) or {}).get("text", "") or ""
        if _BUY_INPUT_MARKER in prompt:
            _process_buy_input(msg)


# ────────────────────────────────────────────────────────────
# RUNNER
# ────────────────────────────────────────────────────────────
def run_once() -> dict:
    if not is_configured():
        return {"skipped": True, "reason": "telegram not configured"}

    offset = _load_offset()
    updates = _get_updates(offset)

    processed = 0
    last_id = offset
    for u in updates:
        try:
            process_update(u)
            processed += 1
        except Exception as e:
            log.error(f"update failed: {e}")
        last_id = max(last_id, u.get("update_id", 0) + 1)

    if last_id > offset:
        _save_offset(last_id)

    return {"updates": len(updates), "processed": processed, "new_offset": last_id}


def _process_dca(callback_query: dict) -> None:
    """dca:{pred_id}:{action}  action in {bought, etf, skip}"""
    data    = callback_query["data"]
    cq_id   = callback_query["id"]
    msg     = callback_query.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    msg_id  = msg.get("message_id")

    parts = data.split(":")
    if len(parts) < 3:
        _answer_callback(cq_id, "ungueltig")
        return
    _, pred_id_str, action = parts[0], parts[1], parts[2]
    try:
        pred_id = int(pred_id_str)
    except ValueError:
        _answer_callback(cq_id, "ungueltiges pred_id")
        return

    feedback_map = {"bought": "dca_bought", "etf": "dca_etf", "skip": "dca_skip"}
    fb_type = feedback_map.get(action, "dca_unknown")

    reason_text = None
    if action == "bought":
        reason_text = _get_buy_price_for_prediction(pred_id)

    log_feedback(pred_id, feedback_type=fb_type, reason_text=reason_text)
    _edit_message_markup(chat_id, msg_id, None)
    _answer_callback(cq_id, f"✓ DCA-{action} notiert")
    if action == "bought":
        _send_buy_input_prompt(_ticker_for_pred(pred_id), pred_id)


def _get_buy_price_for_prediction(pred_id: int) -> str | None:
    """Holt den aktuellen Kurs des Tickers und gibt 'buy_price=X' zurueck."""
    try:
        from src.common.storage import LEARNING_DB, connect
        with connect(LEARNING_DB) as conn:
            row = conn.execute(
                """SELECT subject_id, json_extract(output_json, '$.ticker') AS ticker_out
                   FROM predictions WHERE id = ?""",
                (pred_id,),
            ).fetchone()
        if not row:
            return None
        ticker = row["subject_id"] or row["ticker_out"]
        if not ticker:
            return None

        import yfinance as yf
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1d")
        if hist.empty:
            return None
        price = round(float(hist["Close"].iloc[-1]), 2)
        return f"buy_price={price}"
    except Exception as e:
        log.warning(f"konnte buy_price nicht ermitteln: {e}")
        return None


if __name__ == "__main__":
    result = run_once()
    print(json.dumps(result, indent=2))

