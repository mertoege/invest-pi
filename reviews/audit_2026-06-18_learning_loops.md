# Audit — Self-Learning-Loops (2026-06-18)

Systematische Prüfung aller Lern-/Feedback-Schleifen: **Wird der berechnete Output auch wirklich konsumiert (closed loop) — oder läuft er ins Leere (dead loop)?** Auslöser war ein heute gefundener Dead-Loop im config_patcher (Patches wurden lokal geschrieben, von auto_pull revertet, aber als "applied" verbucht).

Methodik: 4 parallele Read-only-Audits über src/ + scripts/ + DB-Inhalte (data/*.db), Verifikation per file:line und Datenstand.

---

## Ergebnis-Übersicht (16 Bausteine)

| Baustein | Status | Wird konsumiert in |
|---|---|---|
| predictions (loggen) | ✅ WORKS | sizing, calibration, reviews, daily_report |
| outcomes (T+1/7/30d messen) | ✅ WORKS | hit_rate → sizing/calibration; nightly UPDATE bestätigt |
| calibration → monthly_dca-Prompt | ✅ WORKS | monthly_dca.py:117/278 (Sonnet-Prompt) |
| hit_rate → Positionsgröße | ✅ WORKS | sizing.py:249 → run_strategy.py:536 |
| meta_review (Opus, monatl.) | ✅ WORKS | erzeugt Patches → log_patches |
| weekly_mini_review (wöch.) | ✅ WORKS | erzeugt Patches (Großteil) |
| config_patcher apply | ✅ WORKS (heute gefixt) | run_strategy.py:1303 → alle Trade-Pässe |
| attribution | ✅ WORKS | weight_optimizer + daily_report + meta_review |
| reflection | ✅ WORKS | via attribution → weights (return_7d) |
| regime HMM (train→use) | ✅ WORKS | decision/sizing/risk_scorer; geladenes Modell, kein Re-Fit |
| pattern_miner (patterns.db) | ✅ WORKS | risk_scorer.py:1667 find_similar_patterns |
| weight_optimizer | ✅ WORKS | score_portfolio.py:42 apply_weights (stündlich) |
| regime_tracker (accuracy) | 🟡 PARTIAL | nur über den toten daily-score-Prompt-Pfad |
| **drift detection** | ✅ **HEUTE GEFIXT** (war DEAD) | meta_review/weekly/daily_report |
| **backtest_gate** | ✅ **HEUTE GEFIXT** (war DEAD) | log_patches (validiert Patches) |
| **code_evolver** | ✅ **HEUTE STILLGELEGT** (Risiko) | — (Auto-Apply deaktiviert) |

---

## Gefundene Defekte & Maßnahmen

### 1. config_patcher — Regime-Patches verpufften (FIXED, Commit 7a6f4983)
apply_trading_patches schrieb config.yaml nur lokal + markierte den Patch in der (gitignored) learning.db als `applied`. auto_pull revertete config.yaml binnen ~2 Min → Patch verloren, aber nie erneut versucht. Die Regime-Selbstoptimierung lief seit Wochen ins Leere (von Reviews wiederholt als "Patches nicht im active_profile" gemeldet; Git-Commit `bc70a8ec "(auto-pull hatte reverted)"` ist der menschliche Vorläufer-Fix).
**Fix:** `_persist_regime_to_yaml` committet+pusht config.yaml jetzt; `mark_applied` erst nach erfolgreichem Commit (sonst Retry).

### 2. drift detection — lieferte immer `{}` (FIXED, Commit c29bb071)
`detect_drift` (outcomes.py:461/463) rief `hit_rate` ohne `by_measured=True` → Filter nach created_at. Wegen 7d-Outcome-Horizont sind frische Predictions fast nie gemessen → `measured==0` → Funktion gab immer `None` → in Reviews als leeres `{}` sichtbar. Die Frühwarnung bei sinkender Trefferquote war blind.
**Fix:** `by_measured=True` in beiden hit_rate-Aufrufen (vom hit_rate-Docstring selbst empfohlen). Verifiziert: liefert jetzt 350 gemessene (7d), ~86% hit-rate, kein Fehlalarm.
**Rest-Punkt:** `detect_dimension_drift` (outcomes.py:494) hat Daten, aber KEINEN Aufrufer — toter Code (nicht kritisch).

### 3. backtest_gate — lief nie (FIXED, Commit 99c25715)
`can_backtest` deckte nur 5 `trading.*`-Pfade ab; die Reviews emittieren aber fast ausschließlich `regime.*`-Patches → Gate-Bedingung nie wahr → 0 Backtests, 0 Blocks. Strategie-Änderungen gingen ungetestet live.
**Fix:** `can_backtest` + `validate_patch_via_backtest` decken jetzt `regime.<label>.<numerischer Param>` per adaptivem Backtest ab (Default-Profile vs. geänderter Wert). Verifiziert: läuft real durch.
**Rest-Punkt (bewusst belassen):** Bei echten Backtest-Infra-Fehlern (z.B. yfinance offline) wird der Patch weiter "fail-open" durchgelassen (geloggt), damit das Lernen nicht einfriert. Sektor-/target_invest-/max_trades-Params bleiben ungetestet (nicht im Engine modelliert).

### 4. code_evolver — Selbst-Code-Umschreiber STILLGELEGT (Commit 99c25715)
Konnte eigenen Trading-Code per LLM ändern + automatisch git-committen. Nie gefeuert (0 auto-evolve-Commits), aber hohes Risiko (unbeaufsichtigte Code-Änderung am Live-System; zudem auto_pull-Revert-Falle).
**Maßnahme (Mert-Entscheidung):** meta_review ruft `evolve()` nicht mehr auf, sondern erfasst KI-Code-Vorschläge nur zur manuellen Prüfung. `evolve()` ist zusätzlich per Default deaktiviert (Opt-in `INVEST_PI_ENABLE_CODE_EVOLVER=1`).
**Offene Grundsatzfrage:** Soll das System eigene Code-Änderungen je automatisch anwenden, wenn die KI sie für sinnvoll hält? → bewusst dem Menschen vorbehalten.

### 5. daily-score Lern-Kontext — verpuffte Berechnung (PARTIAL, nicht gefixt)
`ticker_calibration_block` (Kalibrierung/Reflection/Regime-Kontext) wird pro Score-Run berechnet und an `score_ticker(learning_context=…)` übergeben, dort aber nur in einen Log-Hash gehängt (risk_scorer.py:1686). Der Score ist rein heuristisch (kein LLM-Call) → der Kontext beeinflusst den Tages-Score NICHT. Kein Schaden (Daten erreichen über sizing/weights/monthly_dca trotzdem die Entscheidung), aber verschwendete Rechenzeit. Klärung später: entweder entfernen oder echt in den Score einspeisen.

### 6. Telegram-Button-Feedback — praktisch tot (nicht kritisch)
Nutzer klicken die Inline-Buttons faktisch nie (0 agree/disagree-Zeilen); der Live-Prompt-Leser sucht nur nach 'agree'/'disagree'. Das automatische Feedback (`auto:*`, 5625 Zeilen) wird dagegen korrekt konsumiert. Mensch-Feedback-Loop ist totes Gewicht, aber harmlos.

---

## Bottom line
Drei tote/blinde Schleifen gefunden und behoben (config_patcher, drift, backtest_gate), den riskanten Selbst-Code-Umschreiber stillgelegt. Der Kern der Selbstoptimierung (Reviews → validierte Patches → Live-Config) ist jetzt erstmals durchgängig geschlossen UND vor Anwendung backtest-geprüft. Rest-Punkte (#5/#6 + detect_dimension_drift) sind harmlos und können später aufgeräumt werden.
