"""
Telegram-Callback-Handler.

Wird alle 60s vom systemd-Timer aufgerufen. Polled getUpdates, parsed
callback_queries, schreibt feedback in feedback_reasons-Tabelle.

callback_data Format:
  "fb:{prediction_id}:{action}"     - Erste Klick-Reaktion (sold/fp/ignore)
  "fbr:{prediction_id}:{reason}"    - Reason-Folgefrage bei fp
                                        (macro/sector/insider/news/other)

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
    if not cq:
        return
    data = cq.get("data", "")
    if data.startswith("fb:"):
        _process_fb(cq)
    elif data.startswith("fbr:"):
        _process_fbr(cq)
    elif data.startswith("dca:"):
        _process_dca(cq)
    else:
        _answer_callback(cq["id"], "unbekanntes callback")


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
    log_feedback(pred_id, feedback_type=fb_type)
    _edit_message_markup(chat_id, msg_id, None)
    _answer_callback(cq_id, f"✓ DCA-{action} notiert")


if __name__ == "__main__":
    result = run_once()
    print(json.dumps(result, indent=2))

