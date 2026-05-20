# Handoff — 2026-05-20

## Was in dieser Session passiert ist

### 1. Komplett-Audit: P0 + P1 Bugs gefixt
Systematischer Audit aller Pipelines. 10 P0-Bugs identifiziert (4 False Positives), 14 P1-Issues bewertet.

**P0 gefixt:**
- risk_scorer.py: Typo valuation_pct → valuation_percentile
- backtest_engine.py: ffill() → ffill(limit=3) gegen Data-Leakage
- run_strategy.py: _llm_screen_candidates + _llm_post_trade_analysis Stubs
- notifier.py: _ALERTS_ENABLED/_TRADES_ENABLED aus env-vars (vorher hardcoded False)
- predictions.py: subject_id=None Warning statt silent downgrade
- DCA-Watchdog Timer: 18:00 → 18:15 (Konflikt mit Strategy-Timer)

**P1 gefixt:**
- limits.py: market_price <= 0 Guard in stop_loss/take_profit/trailing_stop
- limits.py: CEST/CET Fallback korrekt fuer Maerz/Oktober (letzter Sonntag)
- run_strategy.py: take_profit avg_price einmalig laden statt pro Ticker
- config.yaml: cash_floor_pct (10%) + max_per_sector_pct (35%) explizit

### 2. Regime-Hysterese implementiert
- `_HYSTERESIS_PROB_MIN = 0.85`, `_HYSTERESIS_CONFIRM_N = 2`
- Regime-Wechsel erst nach 2 konsekutiven Signalen oder >85% Wahrscheinlichkeit
- Verhindert taegliches Hin-und-Her-Flippen

### 3. LLM-Dimensionen ersetzt (Zero-Cost)
- earnings_llm → Keyword-basierte Headline-Heuristik (_BEARISH_KW/_BULLISH_KW)
- llm_context → Signal-Kohaerenz-Check (_COHERENCE_GROUPS: technical/fundamental/sentiment/macro)
- Kein API-Cost, immer aktiv

### 4. Process-Locking (flock)
- flock_run.sh Wrapper fuer alle DB-schreibenden systemd-Services
- Lock-Gruppe "trading" (strategy, rebalance, sync, rotation, dca)
- Lock-Gruppe "scoring" (score_portfolio)
- --nonblock: bei Konflikt Exit 75 statt deadlock

## Aktueller Zustand

- **Portfolio:** ~$100k / ~23 Positionen / ~41% Cash
- **Regime:** high_vol_mixed (HMM, mit Hysterese)
- **Alerts:** ENABLED via env-vars (vorher hardcoded False)
- **Locking:** Alle kritischen Timer mit flock geschuetzt
- **Alle P0/P1 Bugs gefixt**, System stabil

## Offene Punkte

1. **Hitrate neu messen** — erste 7 Tage mit echten Alert-Levels abwarten
2. **Config-Patcher Backtest-Gate** — Patches sollen erst nach positivem Backtest applied werden
3. **Telegram-Feedback auswerten** — Phase 3b (Inline-Buttons → feedback_reasons-Tabelle)
4. **LLM-Screening** — Stub ersetzen wenn API-Budget vorhanden

## Wichtige Dateien (geaendert)

| Datei | Was |
|-------|-----|
| src/risk/limits.py | market_price Guard, CEST/CET Fix |
| src/alerts/risk_scorer.py | valuation_pct Typo, LLM→Heuristik |
| src/alerts/notifier.py | ALERTS/TRADES_ENABLED aus env-vars |
| src/common/predictions.py | subject_id=None Warning |
| src/learning/backtest_engine.py | ffill(limit=3) |
| src/learning/regime.py | Hysterese |
| scripts/run_strategy.py | Stubs, take_profit fix |
| scripts/flock_run.sh | NEU: Process-Locking Wrapper |
| config.yaml | cash_floor_pct, max_per_sector_pct |
| reviews/audit_2026-05-20.md | Audit-Ergebnis komplett |
