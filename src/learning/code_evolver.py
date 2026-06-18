"""
Code-Evolver — Self-Learning Code-Aenderungen mit Safety-Net.

Erweitert den Meta-Review-Flow: Opus kann jetzt nicht nur Config-Patches
sondern auch Code-Aenderungen vorschlagen. Diese werden:
  1. Validiert (nur erlaubte Dateien, old_code muss exakt matchen)
  2. Angewandt
  3. Getestet (pytest + Import-Check)
  4. Bei Fail: automatischer git revert HEAD

Inspiriert von PokePi's system_optimizer.py — gleiches Pattern,
adaptiert fuer Invest-Pi (kein Docker, pytest statt Health-Check).

Sicherheits-Guardrails:
  - Nur Dateien in ALLOWED_FILES duerfen geaendert werden
  - Max 3 Code-Aenderungen pro Review
  - old_code muss exakt 1x in der Datei vorkommen (eindeutig)
  - Import-Check pro Datei sofort nach Aenderung
  - pytest muss nach allen Aenderungen bestehen
  - Bei Fail: git revert + Telegram-Warnung
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("invest_pi.code_evolver")

REPO_ROOT = Path(__file__).resolve().parents[2]

# Nur diese Dateien duerfen automatisch geaendert werden
ALLOWED_FILES = {
    "src/alerts/risk_scorer.py",
    "src/alerts/sentiment.py",
    "src/alerts/earnings.py",
    "src/trading/decision.py",
    "src/trading/sizing.py",
    "src/risk/limits.py",
    "src/common/outcomes.py",
    "src/learning/reflection.py",
    "src/learning/weight_optimizer.py",
    "src/learning/regime.py",
    "scripts/score_portfolio.py",
    "scripts/run_strategy.py",
}

MAX_CODE_CHANGES_PER_RUN = 3


@dataclass
class CodeChangeResult:
    file: str
    description: str
    success: bool
    message: str


def apply_code_change(change: dict) -> CodeChangeResult:
    """
    Wendet eine einzelne Code-Aenderung an.

    Expected format:
        {
            "file": "src/alerts/risk_scorer.py",
            "description": "Volume-Divergence-Schwelle anpassen",
            "old": "if vol_trend < -0.005:",
            "new": "if vol_trend < -0.008:"
        }

    Returns CodeChangeResult mit success=True/False.
    """
    rel_path = change.get("file", "")
    description = change.get("description", "")
    old_code = change.get("old", "")
    new_code = change.get("new", "")

    if not rel_path or not old_code or not new_code:
        return CodeChangeResult(rel_path, description, False, "missing file/old/new")

    if rel_path not in ALLOWED_FILES:
        return CodeChangeResult(rel_path, description, False,
                                f"Datei nicht erlaubt: {rel_path}")

    full_path = REPO_ROOT / rel_path
    if not full_path.exists():
        return CodeChangeResult(rel_path, description, False,
                                f"Datei nicht gefunden: {rel_path}")

    content = full_path.read_text(encoding="utf-8")

    if old_code not in content:
        return CodeChangeResult(rel_path, description, False,
                                "OLD-Block nicht gefunden (Code hat sich geaendert?)")

    count = content.count(old_code)
    if count > 1:
        return CodeChangeResult(rel_path, description, False,
                                f"OLD-Block {count}x gefunden (nicht eindeutig)")

    if old_code == new_code:
        return CodeChangeResult(rel_path, description, False, "old == new, keine Aenderung")

    # Aenderung anwenden
    new_content = content.replace(old_code, new_code, 1)
    full_path.write_text(new_content, encoding="utf-8")

    # Import-Check
    module_path = rel_path.replace("/", ".").replace(".py", "")
    import_check = subprocess.run(
        ["python3", "-c", f"import {module_path}"],
        capture_output=True, text=True, timeout=30,
        cwd=str(REPO_ROOT),
    )

    if import_check.returncode != 0:
        # Sofort zurueckrollen
        full_path.write_text(content, encoding="utf-8")
        err = import_check.stderr[:200]
        return CodeChangeResult(rel_path, description, False,
                                f"Import-Check fehlgeschlagen: {err}")

    return CodeChangeResult(rel_path, description, True,
                            f"Erfolgreich: {description[:100]}")


def run_tests() -> tuple[bool, str]:
    """
    Fuehrt pytest aus. Returns (passed: bool, output: str).
    """
    result = subprocess.run(
        ["python3", "-m", "pytest", "tests/", "-q", "--tb=short"],
        capture_output=True, text=True, timeout=120,
        cwd=str(REPO_ROOT),
    )
    passed = result.returncode == 0
    output = result.stdout[-500:] if result.stdout else result.stderr[-500:]
    return passed, output


def git_commit(message: str, files: list[str] | None = None) -> bool:
    """Committed GEZIELT die angegebenen Dateien (Fallback: alle, wenn None).
    Gezieltes Adden verhindert, dass parallele/fremde Aenderungen (z.B. Status-
    Push, andere Jobs) versehentlich in einen Auto-Evolve-Commit gesweept werden."""
    try:
        if files:
            subprocess.run(["git", "add", "--", *files],
                           cwd=str(REPO_ROOT), capture_output=True, timeout=30)
            commit_cmd = ["git", "commit", "-m", message, "--", *files]
        else:
            subprocess.run(["git", "add", "-A"],
                           cwd=str(REPO_ROOT), capture_output=True, timeout=30)
            commit_cmd = ["git", "commit", "-m", message]
        result = subprocess.run(
            commit_cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except Exception as e:
        log.warning(f"git commit failed: {e}")
        return False


def git_revert_last() -> bool:
    """Revert des letzten Commits (Rollback)."""
    try:
        result = subprocess.run(
            ["git", "revert", "--no-edit", "HEAD"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def evolve(code_changes: list[dict]) -> dict:
    """
    Hauptfunktion: Code-Aenderungen anwenden, testen, committen oder rollbacken.

    Args:
        code_changes: Liste von {file, description, old, new} dicts.

    Returns:
        {
            "changes_applied": int,
            "changes_failed": int,
            "tests_passed": bool,
            "rolled_back": bool,
            "results": [CodeChangeResult, ...],
            "test_output": str,
        }
    """
    if not code_changes:
        return {"changes_applied": 0, "changes_failed": 0,
                "tests_passed": True, "rolled_back": False, "results": []}

    # SICHERHEIT (Mert, 2026-06-18): Auto-Anwenden von Code-Aenderungen ist
    # standardmaessig DEAKTIVIERT — das System soll seinen Trading-Code nicht
    # unbeaufsichtigt umschreiben+committen. Nur mit explizitem Opt-in aktiv.
    # meta_review ruft evolve() ohnehin nicht mehr auf; dies ist die zweite
    # Verteidigungslinie, falls evolve() je woanders verdrahtet wird.
    import os
    if os.environ.get("INVEST_PI_ENABLE_CODE_EVOLVER") != "1":
        log.warning(f"code_evolver deaktiviert — {len(code_changes)} Vorschlaege "
                    f"NICHT angewandt (Opt-in via INVEST_PI_ENABLE_CODE_EVOLVER=1)")
        return {"changes_applied": 0, "changes_failed": len(code_changes),
                "tests_passed": True, "rolled_back": False, "results": [],
                "disabled": True}

    # Max 3 Aenderungen
    code_changes = code_changes[:MAX_CODE_CHANGES_PER_RUN]

    results = []
    any_applied = False

    for change in code_changes:
        result = apply_code_change(change)
        results.append(result)
        if result.success:
            any_applied = True
        log.info(f"code-change {change.get('file', '?')}: "
                 f"{'OK' if result.success else 'FAIL'} — {result.message}")

    if not any_applied:
        return {
            "changes_applied": 0,
            "changes_failed": len(results),
            "tests_passed": True,
            "rolled_back": False,
            "results": [{"file": r.file, "success": r.success, "msg": r.message} for r in results],
        }

    # Tests laufen lassen
    tests_passed, test_output = run_tests()

    changed_files = [r.file for r in results if r.success and getattr(r, "file", None)]

    rolled_back = False
    if tests_passed:
        # Commit — NUR die geaenderten Dateien
        n_applied = sum(1 for r in results if r.success)
        descriptions = [r.description for r in results if r.success]
        msg = (f"auto-evolve: {n_applied} Code-Aenderung(en)\n\n"
               + "\n".join(f"- {d}" for d in descriptions))
        git_commit(msg, files=changed_files)
        log.info(f"code-evolution committed: {n_applied} changes")
    else:
        # Rollback: NUR die geaenderten Dateien zuruecksetzen (nicht '.', das
        # wuerde parallele uncommitted Arbeit anderer Jobs mit zerstoeren).
        log.warning("tests failed after code changes — rolling back")
        if changed_files:
            subprocess.run(
                ["git", "checkout", "--", *changed_files],
                cwd=str(REPO_ROOT), capture_output=True, timeout=30,
            )
        rolled_back = True

    return {
        "changes_applied": sum(1 for r in results if r.success),
        "changes_failed": sum(1 for r in results if not r.success),
        "tests_passed": tests_passed,
        "rolled_back": rolled_back,
        "results": [{"file": r.file, "success": r.success, "msg": r.message} for r in results],
        "test_output": test_output if not tests_passed else "",
    }
