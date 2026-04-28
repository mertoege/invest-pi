"""
JSON-Helpers — defensive Parsing für Claude-Outputs.

Hintergrund (LESSONS_FOR_INVEST_PI.md TL;DR Punkt 4 + Bug 1 in Outcome-Tracker):
  Claude wickelt JSON-Outputs oft in Markdown-Codefences (```json ... ```).
  Ohne Strip schlägt json.loads fehl, der Outcome-Tracker findet nichts und
  Predictions hängen für immer als pending. Daher IMMER strip_codefence vor
  json.loads.

  Plus: defensive safe_parse mit Fallback auf {} statt Crash, damit ein
  einzelner kaputter Sonnet-Output nicht den ganzen Job killt.
"""

from __future__ import annotations

import json
import re
from typing import Any


_CODEFENCE_RE = re.compile(
    r"^```(?:json|JSON)?\s*\n?(.*?)\n?```\s*$",
    re.DOTALL,
)


def strip_codefence(text: str) -> str:
    """
    Entfernt führende/abschließende ```json ... ``` Markdown-Codefences.

    Robust gegen:
      - ``` ... ```             (kein Sprach-Tag)
      - ```json ... ```         (lower)
      - ```JSON ... ```         (upper)
      - Leerzeilen am Anfang/Ende
      - Kein Codefence vorhanden → Original-Text zurück
    """
    if not text:
        return text
    stripped = text.strip()
    m = _CODEFENCE_RE.match(stripped)
    return m.group(1).strip() if m else stripped


def safe_parse(text: str, default: Any = None) -> Any:
    """
    Versucht JSON zu parsen, mit Markdown-Strip vor dem Parse.
    Gibt `default` (oder {}) zurück bei Parse-Fehler — nie eine Exception.

    Usage:
        data = safe_parse(claude_output, default={})
        if not data:
            log.warning("Claude-Output nicht parsebar, skip")
            return
    """
    if default is None:
        default = {}
    cleaned = strip_codefence(text)
    if not cleaned:
        return default
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return default


def extract_json_block(text: str) -> str | None:
    """
    Findet den letzten zusammenhängenden JSON-Block in einem Text.
    Nützlich wenn Claude Prosa + JSON-Anhang produziert
    ('Hier ist meine Analyse... {"verdict": "buy", ...}').

    Sucht balanced { ... } oder [ ... ] vom Ende her.
    """
    if not text:
        return None
    stripped = strip_codefence(text)
    # Versuche ganzen Text zuerst
    try:
        json.loads(stripped)
        return stripped
    except (json.JSONDecodeError, TypeError):
        pass

    # Sonst: scan vom Ende nach balanced { } oder [ ]
    for opener, closer in [("{", "}"), ("[", "]")]:
        depth = 0
        end_idx = stripped.rfind(closer)
        if end_idx == -1:
            continue
        for i in range(end_idx, -1, -1):
            ch = stripped[i]
            if ch == closer:
                depth += 1
            elif ch == opener:
                depth -= 1
                if depth == 0:
                    candidate = stripped[i:end_idx + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except (json.JSONDecodeError, TypeError):
                        break
    return None


if __name__ == "__main__":
    # Schnell-Test
    cases = [
        '```json\n{"a": 1}\n```',
        '```\n{"b": 2}\n```',
        '{"c": 3}',
        '```json\n{"d": 4}',  # unbalanced — kein Match
        'Hier ist die Analyse: {"verdict": "buy", "confidence": "high"}',
    ]
    for c in cases:
        print(f"INPUT:    {c!r}")
        print(f"STRIPPED: {strip_codefence(c)!r}")
        print(f"PARSED:   {safe_parse(c)!r}")
        print(f"EXTRACT:  {extract_json_block(c)!r}")
        print("-" * 50)
