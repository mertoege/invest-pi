"""
LLM-Wrapper — Claude Code CLI mit Cost-Tracking + Predictions-Logging.

Nutzt `claude -p` (Claude Code CLI) statt direktem Anthropic SDK.
Pre-Check: cost_caps.can_call() bevor jeder Call.
Post-Call: log_cost in cost_ledger + log_prediction mit Token-Counts.

Usage:
    from src.common.llm import call_sonnet, call_opus

    result = call_sonnet(
        system="...",
        prompt="...",
        job_source="daily_score",
        subject_id="NVDA",
    )
    if result.ok:
        data = result.parsed_json or {}

Konfiguration:
  ANTHROPIC_API_KEY (env) — wird von Claude Code CLI genutzt
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .cost_caps import can_call, log_cost
from .fx import eur_per_usd
from .json_utils import safe_parse, strip_codefence
from .predictions import log_prediction

log = logging.getLogger("invest_pi.llm")

REPO_ROOT = Path(__file__).resolve().parents[2]


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


MODEL_MAP = {
    "claude-sonnet-4-6": "sonnet",
    "claude-opus-4-6":   "opus",
    "claude-haiku-4-5":  "haiku",
}


def _raw_call(model: str, system: str, prompt: str,
              max_tokens: int, temperature: float) -> dict:
    """Ruft Claude Code CLI auf. Returns parsed JSON response."""
    cli_model = MODEL_MAP.get(model, "sonnet")

    full_prompt = f"{system}\n\n---\n\n{prompt}"

    env = os.environ.copy()
    api_key = env.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        env_path = REPO_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    env["ANTHROPIC_API_KEY"] = api_key
                    break
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY nicht gesetzt")

    cmd = [
        "claude", "-p", full_prompt,
        "--model", cli_model,
        "--output-format", "json",
        "--max-turns", "1",
    ]

    result = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=300, env=env, cwd=str(REPO_ROOT),
    )

    if result.returncode != 0:
        stderr = result.stderr[:500] if result.stderr else ""
        raise RuntimeError(f"claude CLI failed (rc={result.returncode}): {stderr}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude CLI output not JSON: {e}\n{result.stdout[:200]}")

    if data.get("is_error"):
        raise RuntimeError(f"claude CLI error: {data.get('result', '?')[:300]}")

    usage = data.get("usage", {})
    input_tokens = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    return {
        "text": data.get("result", ""),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": data.get("total_cost_usd", 0),
        "session_id": data.get("session_id", ""),
    }


def is_configured() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        return "ANTHROPIC_API_KEY=" in env_path.read_text()
    return False


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
    """Generischer Claude-CLI-Call mit Pre-Check + Logging."""
    if not is_configured():
        return LLMResult(ok=False, model=model, error="ANTHROPIC_API_KEY nicht gesetzt")

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

    text = strip_codefence(raw["text"])
    in_tokens = int(raw["input_tokens"])
    out_tokens = int(raw["output_tokens"])
    cost_usd = float(raw.get("cost_usd", 0))
    cost = cost_usd * eur_per_usd() if cost_usd else 0.0

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
        confidence=None,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        cost_estimate_eur=cost,
    )

    log_cost(
        api="claude-cli",
        cost_eur=cost,
        job_source=job_source,
        prediction_id=pred_id,
        notes=f"{model} {in_tokens}+{out_tokens}t via CLI",
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
    """Wrapper fuer Sonnet — fuer haeufigere, billigere Calls."""
    kwargs.setdefault("model", "claude-sonnet-4-6")
    return _call(**kwargs)


def call_opus(**kwargs) -> LLMResult:
    """Wrapper fuer Opus — fuer monatliche Tiefenanalysen."""
    kwargs.setdefault("model", "claude-opus-4-6")
    kwargs.setdefault("max_tokens", 4096)
    return _call(**kwargs)


def call_haiku(**kwargs) -> LLMResult:
    """Wrapper fuer Haiku — billig, fuer simple Klassifikation."""
    kwargs.setdefault("model", "claude-haiku-4-5")
    return _call(**kwargs)
