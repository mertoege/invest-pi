# CLAUDE.md — Invest-Pi

> Kontext-Datei für Claude Code. Wird automatisch gelesen wenn du in dieses Verzeichnis wechselst.
> Letzter Abgleich mit Live-System: 2026-06-18.

## Mission — EIN Ziel: den Markt schlagen
Die Leitfrage des Projekts (Mert): *Kann man mit einem Raspberry Pi + KI + Geduld
den Aktienmarkt schlagen und damit passives Einkommen erzeugen?* Erfolg = gaengige
ETFs/Indizes (SPY/QQQ) ueber mehrere Jahre UND mehrere Marktphasen DEUTLICH schlagen
(nicht +1%), risiko-adjustiert. Aktuell Paper-Trading via Alpaca; Echtgeld erst,
wenn der Edge ueber Zyklen bewiesen ist.

## Arbeitsweise — volle Autonomie (von Mert ausdruecklich erteilt)
Mert ist interessierter Laie, **nicht** technisch tief. Er hat Claude ausdruecklich
die **kompletten technischen Entscheidungen** uebertragen: Architektur, welche Jobs,
Self-Learning ja/nein, Strategie-Design — **Claude entscheidet, weil Claude es besser
beurteilen kann.** Nicht mit technischen Detail-Fragen loechern; selbst entscheiden,
handeln, und das **Ergebnis in Klartext** berichten (was es praktisch bedeutet).
- **Frag Mert nur**, wenn es (a) um **echtes Geld / Echtgeld-Umstieg** geht, (b) etwas
  schwer Umkehrbares mit realem Risiko ansteht, oder (c) eine reine Wertentscheidung
  ist (z.B. wie viel Risiko ihm lieb ist). Technik-Entscheidungen: selbst treffen.
- **Bewiesene Erkenntnisse (Stand 2026-06, per Backtest belegt):** Das alte komplexe
  Score-/Self-Learning-System schlug den Markt NICHT (Risiko-Vorhersage schlechter
  als ein Dummy). Aktives Viel-Handeln schadet. Was WIRKT: regelbasiertes **Momentum**
  (Top-5, monatlich, breites Universum) — schlug 2018–2026 den Markt in 7/9 Jahren bei
  geringerem Drawdown. Lernen gehoert auf die **Strategie-/Meta-Ebene** (Varianten per
  Backtest validieren), NICHT Trade-fuer-Trade. Details: reviews/, champion_duell*.py.
- **Echtgeld-Regel:** kein echtes Geld, bevor der Ansatz ueber mehrere Jahre + >=1 Crash
  im Backtest UND live im Paper-Trading ueberzeugt hat.

## Owner
Mert Oege · mert.oege@gmail.com · GitHub: mertoege/invest-pi

## Architektur
- **Kein Docker** — Native Python 3.11+, User `investpi`, 22 systemd Timers + 1 Web-Service
- **Pfad:** `/home/investpi/invest-pi`
- **Config:** `config.yaml` — einzige Wahrheitsquelle für Portfolio + Universe + Settings
- **Secrets:** `.env` (Alpaca Keys, Telegram Token, FRED API Key) — von allen Units via `EnvironmentFile` geladen
- **Daten:** `data/` (SQLite DBs: trading, learning, alerts, market, patterns; JSON-Caches)
- **Logs:** `logs/` (auto_pull.log, status_push.log, backup.log, webapp.log)
- **Webapp:** `webapp/main.py` (FastAPI Dashboard, `invest-pi-webapp.service`, läuft dauerhaft)

## Module (src/)
| Modul | Zweck |
|-------|-------|
| `src/trading/` | Decision Engine (`decision.py`), Position Sizing (`sizing.py`) |
| `src/risk/` | `limits.py`: Kill-Switch, Stop-Loss, Take-Profit, Trailing, Cash-Floor, Sector-Cap, Correlation, Daily-Loss |
| `src/learning/` | Pattern Miner, Calibration, Attribution, Reflection, HMM Regime (+ `regime_tracker`), Config Patcher, `backtest_gate`, Weight Optimizer, Backtest Engine, `code_evolver`, Correlation |
| `src/alerts/` | Telegram Notifier, Dispatch, Risk Scorer, FRED Signals, Market Breadth, Sentiment, Earnings |
| `src/broker/` | Base Adapter, Mock Sim, Alpaca Paper (paper-only hardcoded) |
| `src/common/` | Shared Utilities, DB Access (`storage.py`), LLM-Wrapper (`llm.py`), Predictions, Outcomes, Cost-Caps, FX, Performance |
| `src/jobs/` | Telegram Callback Handler (`telegram_callbacks.py`) |

## Scripts (scripts/) — Einstiegspunkte der Timer
`run_strategy.py`, `score_portfolio.py`, `sync_orders.py` / `sync_positions.py`, `track_outcomes.py`,
`autonomous_operator.py`, `weekly_rotation.py`, `train_regime.py`, `weekly_mini_review.py`, `weekly_recap.py`,
`build_patterns.py`, `universe_screener.py`, `monthly_dca.py`, `monthly_digest.py`, `meta_review.py`,
`daily_report.py`, `dca_watchdog.py`, `hardware_check.py`, `backup_databases.sh`,
`auto_pull.sh`, `status_push.sh`, `flock_run.sh` (Process-Locking-Wrapper)

## Systemd Timers (22 Stück) — Stand 2026-06-18
| Timer | Schedule | Zweck |
|-------|----------|-------|
| invest-pi-auto-pull | alle 2 Min | Push-to-Deploy + systemd-sync + smoke + auto-rollback |
| invest-pi-status-push | alle 2 Min | `_status/snapshot.json` → GitHub |
| invest-pi-telegram-callbacks | ~60s | Inline-Button-Klicks → feedback_reasons |
| invest-pi-score | stündlich :30 | Risk-Scoring der Watchlist |
| invest-pi-sync | stündlich :35 | Broker→DB Sync + Equity (EUR+USD) + peak_price |
| invest-pi-strategy-hourly | Mo-Fr stündlich | **Momentum-Rebalance** (engine=momentum: `run_strategy.py`→`momentum_rebalance.run_due`; 1×/Monat Ziel + Konvergenz). Alte Score-Pipeline + `weekly_rotation` stillgelegt (no-op). |
| invest-pi-hardware | alle 30 Min | CPU/Disk/Mem-Alerts |
| invest-pi-operator | täglich 13:00 | Autonomer System-Check + Auto-Fix vor Marktöffnung |
| invest-pi-dca-watchdog | täglich 18:00 | DCA-Überwachung |
| invest-pi-daily-report | täglich 21:30 | Tagesreport (Telegram) |
| invest-pi-rebalance | Mo-Fr 21:30 | Market-Close-Rebalance (sell-only) |
| invest-pi-outcomes | täglich 02:30 | Outcome-Tracker (T+1d/7d/30d) |
| invest-pi-backup | täglich 03:30 | sqlite3 .backup → gzip → `data/backups/<date>/` |
| invest-pi-train-regime | Sa 05:00 | HMM-Regime-Modell neu trainieren (5y SPY+VIX) |
| invest-pi-rotation | Sa ~12:00 | Weekly Rotation + Top-Up (flock "trading") |
| invest-pi-weekly-mini-review | So 10:00 | Wöchentliche Mini-Reflexion (daily_score) |
| invest-pi-weekly-recap | So 19:00 | Wochen-Recap (Telegram) |
| invest-pi-patterns | monatl. 1. 03:00 | Pattern-Library Refresh |
| invest-pi-universe-screener | monatl. 1. 06:00 | Neue Ticker-Kandidaten für Watchlist |
| invest-pi-monthly-dca | monatl. 1. 14:00 | Sonnet-DCA-Empfehlung |
| invest-pi-meta-review | monatl. 2. 04:00 | Opus-Meta-Reflexion → Action-Plan |
| invest-pi-monthly-digest | monatl. 3. 09:00 | Verbesserungs-Digest (Telegram) |

Plus `invest-pi-error-alert@.service` (Template-Unit für OnFailure-Telegram-Push).
Aus dem Repo entfernt (2026-06-24, tote/deaktivierte Units): `strategy`, `-strategy-open`, `-strategy-close` (durch `strategy-hourly` ersetzt) + Score-System-Timer `train-regime`, `patterns`, `meta-review`. `invest-pi-score` bleibt deaktiviert im Repo (Referenz). `/etc`-Altlasten + `rotation`-Timer noch per sudo am Pi abzuraeumen.

## Deployment
- **auto_pull** Timer holt alle 2 Min von GitHub
- Push zu GitHub = automatisches Deploy in <2 Min (mit Smoke-Test + Auto-Rollback)
- **status_push** committed `_status/snapshot.json` alle 2 Min nach GitHub

## Wichtige Gotchas
1. **NICHT PokéPi!** Dies ist ein separates Projekt — nicht verwechseln.
2. **Kein Docker hier** — alles läuft nativ als systemd Services unter User `investpi`.
3. **config.yaml** ist die einzige Wahrheitsquelle für Portfolio-Daten.
4. **auto_pull überschreibt lokale Änderungen** — `stash → pull → stash pop`; bei Konflikt geht die Änderung verloren. Immer SOFORT committen + pushen, sonst ist die Änderung in <2 Min weg!
5. **Paper-Trading only** — kein Echtgeld, Alpaca Paper Account.
6. **Status-Monitoring** in `status_push.sh` muss mit Unit-Namen synchron bleiben (z.B. `strategy-hourly`, nicht `strategy`).
7. Manuelle Script-Läufe brauchen geladene `.env` (`set -a; source .env; set +a`), sonst fehlen Broker-Keys.

## Tech Stack
- Python 3.11+, SQLite, yfinance, fredapi
- Alpaca SDK (Paper), Telegram Bot API, FastAPI (Webapp)
- scikit-learn, hmmlearn (Regime Detection), tenacity (Retry)
- Raspberry Pi 5, Tailscale VPN

## Sprache
Antworte auf Deutsch. Sei direkt und präzise. Mert vertraut autonomer Arbeit.
