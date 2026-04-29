"""
LLM-Wrapper — Anthropic-Calls mit Cost-Tracking + Predictions-Logging.

Pre-Check: cost_caps.can_call() bevor jeder Call.
Post-Call: log_cost in cost_ledger + log_prediction mit Token-Counts.
Markdown-Strip auf Output.

Usage:
    from src.common.llm import call_sonnet, call_opus

    result = call_sonnet(
        system="...",
        prompt="...",
        job_source="daily_score",
        subject_id="NVDA",
    )
    if result.ok:
        data = result.parsed_json or {}    # Markdown-stripped, JSON-parsed

Konfiguration:
  ANTHROPIC_API_KEY (env)

Pricing (Stand April 2026, anpassbar):
  Sonnet 4.6:  $3.00 / $15.00 per 1M input/output tokens
  Opus 4.6:   $15.00 / $75.00 per 1M input/output tokens
  EUR-Conversion via fx.eur_per_usd
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from .cost_caps import can_call, log_cost
from .fx import eur_per_usd
from .json_utils import safe_parse, strip_codefence
from .predictions import log_prediction
from .retry import anthropic_retry

log = logging.getLogger("invest_pi.llm")


# ────────────────────────────────────────────────────────────
# PRICING (Stand April 2026, in USD per 1M tokens)
# ────────────────────────────────────────────────────────────
PRICING = {
    "claude-sonnet-4-6":  {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":    {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5":   {"input": 0.80,  "output": 4.00},
}


def _cost_eur(model: str, input_tokens: int, output_tokens: int) -> float:
    p = PRICING.get(model)
    if not p:
        return 0.0
    usd = (input_tokens / 1_000_000) * p["input"] + (output_tokens / 1_000_000) * p["output"]
    return usd * eur_per_usd()


# ────────────────────────────────────────────────────────────
# RESULT TYPE
# ────────────────────────────────────────────────────────────
@dataclass
class LLMResult:
    ok:                bool
    text:              str = ""
    parsed_json:       Any = None
    input_tokens:      int = 0
    output_tokens:     int = 0
    cost_eur:          float = 0.0
    model:             str = ""
    prediction_id:     Optional[int] = None
    error:             Optional[str] = None
    raw:               dict = field(default_factory=dict)


# ────────────────────────────────────────────────────────────
# CORE CALL
# ────────────────────────────────────────────────────────────
@anthropic_retry
def _raw_call(model: str, system: str, prompt: str,
              max_tokens: int, temperature: float) -> dict:
    """Tenacity-retried Anthropic-Call. Returns raw response dict."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY env-var nicht gesetzt")
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise RuntimeError(
            f"anthropic SDK nicht installiert: pip install anthropic --break-system-packages\n"
            f"Original: {e}"
        )

    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return {
        "text": resp.content[0].text if resp.content else "",
        "input_tokens":  resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "stop_reason":   resp.stop_reason,
        "id":            resp.id,
    }


# ────────────────────────────────────────────────────────────
# HIGH-LEVEL APIs
# ────────────────────────────────────────────────────────────
def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _call(
    *,
    model:         str,
    system:        str,
    prompt:        str,
    job_source:    str,
    subject_type:  str = "ticker",
    subject_id:    Optional[str] = None,
    input_summary: Optional[str] = None,
    max_tokens:    int = 1024,
    temperature:   float = 0.0,
    estimated_cost_eur: Optional[float] = None,
) -> LLMResult:
    """Generischer Anthropic-Call mit Pre-Check + Logging."""
    if not is_configured():
        return LLMResult(ok=False, model=model, error="ANTHROPIC_API_KEY nicht gesetzt")

    # Pre-Cost-Check
    rough = estimated_cost_eur if estimated_cost_eur is not None else 0.05
    allowed, reason = can_call(estimated_cost_eur=rough)
    if not allowed:
        log.warning(f"llm call blocked by cost-cap: {reason}")
        return LLMResult(ok=False, model=model, error=f"cost-cap: {reason}")

    try:
        raw = _raw_call(model, system, prompt, max_tokens, temperature)
    except Exception as e:
        log.error(f"llm call failed: {e}")
        return LLMResult(ok=False, model=model, error=str(e))

    text       = strip_codefence(raw["text"])
    in_tokens  = int(raw["input_tokens"])
    out_tokens = int(raw["output_tokens"])
    cost       = _cost_eur(model, in_tokens, out_tokens)

    # Prediction-Row (output ist das ge-strippte text)
    parsed = safe_parse(text, default=None)
    pred_id = log_prediction(
        job_source=job_source,
        model=model,
        subject_type=subject_type,
        subject_id=subject_id,
        prompt=system,
        input_payload={"prompt_preview": prompt[:300]},
        input_summary=input_summary,
        output=parsed if parsed else text,
        confidence=None,    # Caller setzt confidence ggf. manuell
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        cost_estimate_eur=cost,
    )

    # Cost-Ledger
    log_cost(
        api="anthropic",
        cost_eur=cost,
        job_source=job_source,
        prediction_id=pred_id,
        notes=f"{model} {in_tokens}+{out_tokens}t",
    )

    return LLMResult(
        ok=True,
        text=text,
        parsed_json=parsed,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        cost_eur=cost,
        model=model,
        prediction_id=pred_id,
        raw=raw,
    )


def call_sonnet(**kwargs) -> LLMResult:
    """Wrapper fuer Sonnet — fuer haeufigere, billigere Calls (z.B. daily_score-Augmentation)."""
    kwargs.setdefault("model", "claude-sonnet-4-6")
    return _call(**kwargs)


def call_opus(**kwargs) -> LLMResult:
    """Wrapper fuer Opus — fuer monatliche Tiefenanalysen (meta_review, quarterly_outlook)."""
    kwargs.setdefault("model", "claude-opus-4-6")
    kwargs.setdefault("max_tokens", 4096)
    return _call(**kwargs)


def call_haiku(**kwargs) -> LLMResult:
    """Wrapper fuer Haiku — sehr billig, fuer simple Klassifikations-Aufgaben."""
    kwargs.setdefault("model", "claude-haiku-4-5")
    return _call(**kwargs)
