# 🚀 Invest-Pi · Handover-Dokumentation

**Last update:** 2026-04-30 nach B3-V2-Backtester-Session  
**Project owner:** Mert Oege (mert.oege@gmail.com)  
**Repository:** https://github.com/mertoege/invest-pi  
**Pi-Tailscale-IP:** `100.92.115.43`  
**Cowork-Mount:** `C:\Users\merto\PiManager\Investpi\Aktien` (= `/sessions/<id>/mnt/Aktien/`)

---

## ⚡ Quick-Start für neue Cowork-Session

1. **Memory ist auto-loaded.** Lies `MEMORY.md` (auto-attached) für vollen Kontext.
2. **Code ist auf GitHub.** Niemals direkt im Mount git-operieren — immer in `/tmp` clonen:
   ```bash
   TOKEN=$(grep -oE 'oauth2:[^@]+' /sessions/determined-affectionate-clarke/mnt/Aktien/.git/config | head -1 | cut -d: -f2)
   cd /tmp && git clone --quiet "https://oauth2:${TOKEN}@github.com/mertoege/invest-pi.git" invest_pi_work
   cd /tmp/invest_pi_work
   git config core.autocrlf false
   git config user.email "mert.oege@gmail.com"
   git config user.name "Mert Oege"
   ```
3. **Pi-Status lesen:**
   ```bash
   cd /tmp/invest_pi_work && git fetch --quiet
   git show origin/main:_status/snapshot.json | python3 -m json.tool
   ```
4. **Live-Code-Push:** Edit in /tmp clone, `git commit && git push`. Pi pulled in <2 Min.

---

## 🎯 Mission

Autonomes Self-Learning-Investment-System auf Raspberry Pi 5. AI/Tech-Sektor-fokussiert.

**Kernziel** (von Mert direkt formuliert):  
> "Stabile Rendite so hoch wie möglich, mit dem Self-Learning-System sich kontinuierlich optimieren."

→ Übersetzung: **Sharpe-Ratio-Maximierung** mit kontrollierten Drawdowns.

**Echtgeld-Empfehlung (von uns ehrlich):**  
NEIN für aktuellen 50€/Monat-DCA-Use-Case. Steuerbürokratie + FX-Friction frisst zu viel Alpha. Paper bleibt Lernumgebung. Re-Evaluation nach 6-12 Monaten echter Live-Performance.

---

## 🏗 Architektur (4 Schichten)

```
┌─────────────────────────────────────────────────────────────────┐
│  scripts/                                                        │
│    score_portfolio  run_strategy  sync_positions  track_outcomes │
│    monthly_dca   meta_review   daily_report   build_patterns     │
│    backup_databases   hardware_check   train_regime              │
│    backtest                                                      │
└──────────┬─────────────────────────────────────┬─────────────────┘
           │                                     │
   ┌───────┴────────┐                    ┌───────┴─────────┐
   │ src/trading    │                    │ src/jobs        │
   │  decision      │                    │  telegram_cb    │
   │  sizing        │                    └───────┬─────────┘
   └───────┬────────┘                            │
           │                                     │
   ┌───────┴────────┐  ┌──────────────┐  ┌──────┴──────────┐
   │ src/risk       │  │ src/alerts   │  │ src/learning    │
   │  limits        │  │  notifier    │  │  pattern_miner  │
   │ (kill, SL, TP, │  │  dispatch    │  │  calibration    │
   │  trailing,     │  └──────┬───────┘  │  attribution    │
   │  cash-floor,   │         │          │  regime (HMM)   │
   │  sector-cap,   │         │          │  backtest_engine│
   │  daily-loss)   │         │          └──────┬──────────┘
   └───────┬────────┘         │                 │
           │                  │                 │
   ┌───────┴──────────────────┴─────────────────┴─────────┐
   │  src/broker                                           │
   │    base (Adapter)  mock (Sim)  alpaca (Paper)         │
   └────────────────────────┬──────────────────────────────┘
                            │
   ┌────────────────────────┴──────────────────────────────┐
   │  src/common                                            │
   │    storage (5 DBs)   config   data_loader   fx        │
   │    predictions  outcomes  cost_caps  llm  performance │
   │    json_utils  retry  logging_setup                   │
   └────────────────────────────────────────────────────────┘
```

### 5 SQLite-Datenbanken (in `data/`)

| DB | Inhalt |
|---|---|
| `market.db` | yfinance-Cache (OHLCV, Fundamentals, FX-Rate) |
| `patterns.db` | Pre-Drawdown-Muster aus 10y-Historie für Pattern-Matching |
| `alerts.db` | Risk-Score-Historie + Telegram-Notifications |
| `learning.db` | predictions + outcomes + feedback_reasons + cost_ledger + meta_reviews |
| `trading.db` | trades + positions (peak_price, strategy_label) + equity_snapshots (USD+EUR+FX) |

### 14 systemd-Timer (alle aktiv auf Pi)

| Timer | Schedule | Zweck |
|---|---|---|
| invest-pi-score | stündlich `:30` | 9-Dim-Risk-Scoring für 43 Tickers |
| invest-pi-strategy | Mo-Fr 16:00 CEST | Trade-Decisions, Buy/Sell |
| invest-pi-sync | stündlich `:35` | Broker→DB-Sync, Equity-Snapshot |
| invest-pi-outcomes | täglich 02:30 | T+1d/7d/30d-Outcome-Messung |
| invest-pi-auto-pull | alle 2 Min | Push-to-Deploy + auto-rollback |
| invest-pi-status-push | alle 2 Min | snapshot.json → GitHub |
| invest-pi-hardware | alle 30 Min | CPU/Disk/Mem-Alerts |
| invest-pi-telegram-callbacks | alle 60s | Inline-Button-Klicks → DB |
| invest-pi-patterns | monatlich 1. 03:00 | Pattern-Library Refresh |
| invest-pi-monthly-dca | monatlich 1. 14:00 | Sonnet-DCA-Empfehlung an Mert |
| invest-pi-meta-review | monatlich 2. 04:00 | Opus-Reflexion → Action-Plan |
| invest-pi-backup | täglich 03:30 | DB-Snapshots gzipped, 14d-rotation |
| invest-pi-daily-report | täglich 21:30 | Telegram-PnL-Push (täglich/wöchentlich) |
| invest-pi-train-regime | wöchentlich Sa 05:00 | HMM-Regime-Modell-Retrain |

---

## 📊 Aktueller Live-Stand

### Mode: `adaptive` (Regime-basierte Strategy-Auswahl)

Das System wählt pro Tag automatisch eine von drei Profilen basierend auf HMM-Output:

| Regime | score_buy_max | max_position_eur | max_open_pos | stop_loss | take_profit | trail_stop |
|---|---|---|---|---|---|---|
| `low_vol_bull` | 70 | 5000 | 25 | 12% | 65% | 12% |
| `high_vol_mixed` | 45 | 2500 | 20 | 10% | 40% | 10% |
| `bear` | 20 | 800 | 8 | 8% | 20% | 6% |
| `unknown` (fallback) | 45 | 2500 | 20 | 10% | 40% | 10% |

**Aktuelles Regime (Stand letzte Session):** `low_vol_bull` mit 54% Confidence.

### Globale Constraints (immer aktiv, alle Modi)

- **Cash-Floor:** 20% (nie voll deployed)
- **Sector-Cap:** 40% pro Sektor (Halbleiter, Software, Hyperscaler etc.)
- **TARGET_VOL_ANNUAL:** 18% (Vol-Targeting per Position)
- **alert_level >= 2** → IMMER skip
- **Daily-Loss-Bremse:** -5% Equity vom Tageshoch (USD-basiert, FX-resistent)
- **Cost-Hard-Stop:** 50€/Monat Anthropic-API-Cap
- **Marktöffnung:** Mo-Fr 15:30-22:00 CEST
- **Kill-Switch:** `touch data/.KILL` blockiert alle Trades sofort

### Tests-Coverage: 46/46 grün

| Modul | Tests |
|---|---|
| `tests/test_smoke.py` | 6/6 (init_all, predictions-lifecycle, batch_aggregate, cost_caps, json_utils) |
| `tests/test_trading.py` | 5/5 (mock_broker, decision_branches, sizing, kill_switch, stop_loss) |
| `tests/test_outcomes.py` | 4/4 (correctness, drift_detection, **TZ-bug-regression**) |
| `tests/test_calibration.py` | 3/3 (empty, with_predictions, with_meta_review) |
| `tests/test_decision_modes.py` | 4/4 (moderate_buys, conservative_skips, low_conf, take_profit) |
| `tests/test_notifier.py` | 5/5 (configured, html_escape, alert_with_buttons, send_trade, 400_handling) |
| `tests/test_dispatch.py` | 3/3 (no_alerts, pushes_high_level, dedup) |
| `tests/test_backtest_v2.py` | 16/16 (9-dim-scoring, vol-targeting, composite, integration, V1-compat) |

---

## 📈 Backtest-Resultate (3 Jahre 2022-2025)

| Strategy | Return | CAGR | Sharpe | Max-DD | Vola | Trades | Win-Rate |
|---|---|---|---|---|---|---|---|
| Conservative | +2.20% | +0.55% | 0.92 | -1.13% | 0.60% | 177 | 59.3% |
| Moderate | +55.02% | +11.66% | 0.91 | -22.89% | 13.01% | 312 | 41.7% |
| Aggressive | +97.84% | +18.72% | 0.92 | -36.20% | 21.00% | 284 | 46.0% |
| **ADAPTIVE** | **+43.70%** | **+9.55%** | **1.39** | **-6.44%** | **6.72%** | 321 | 54.5% |
| SMH Buy-Hold (Baseline) | +136% | ~+34% | n/a | ~-50% | hoch | 1 | n/a |

**Schlüssel-Befund:** ADAPTIVE hat die mit Abstand beste Sharpe (1.39 vs 0.92 für alle anderen) bei dramatisch niedrigerem Max-Drawdown. Liefert ~32% der SMH-Returns mit 13% des Drawdowns.

**Live-Erwartung:** sollte besser sein als Backtest, weil:
1. Echtes HMM (live trainiert) > rule-based Backtest-Regime
2. Voller 9-Dim-risk_scorer (Backtest hat nur 4-Dim signal_score)
3. Pattern-Library-Lookups (Backtest ignoriert das)
4. Wöchentliches Re-Training des Regime-Modells

Realistische Live-Sharpe: 1.2-1.5, Returns 3y: +50-80%.

---

## 🔑 Wichtige Setup-Details

### Pi-Identifikation

- **Hostname:** `pokepi` (geteilt mit PokéPi-Projekt)
- **Tailscale-IP:** `100.92.115.43`
- **SSH-Login:** `ssh pi@100.92.115.43`
- **App-User:** `investpi` (separater System-User mit eigenem Home `/home/investpi/`)
- **Working-Dir auf Pi:** `/home/investpi/invest-pi/`

### Permissions / Sudo

- **investpi-User** hat NOPASSWD-sudo auf:
  - `/home/investpi/invest-pi/scripts/systemd_sync.sh` (für auto-pull)
  - `/usr/bin/systemctl daemon-reload`
- **Sudoers-Snippet:** `/etc/sudoers.d/investpi-deploy`
- **pi-User** hat KEINEN Read-Access auf `/home/investpi/` (750-perms = bewusst)

### Sensitive Files (auf dem Pi, nie in Memory speichern!)

- `/home/investpi/invest-pi/.env` mit:
  - `ALPACA_API_KEY` + `ALPACA_API_SECRET` (Paper-Account Account-ID `PA3YM7D65FYR`)
  - `ANTHROPIC_API_KEY` (für monthly_dca + meta_review)
  - `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
  - Optional: `RESTIC_REPOSITORY` + `RESTIC_PASSWORD` für Cloud-Backup
- `/home/investpi/invest-pi/.git/config` mit eingebettetem GitHub-PAT

---

## 🌉 Pi-Communication-Workflow (KRITISCH)

**Claude in Cowork-Sandbox hat KEINEN direct Pi-Zugang.** Tailscale ist nicht in der Sandbox.

### Pattern: GitHub als Vermittler

```
Claude (Cowork)                GitHub                     Pi
     │                            │                         │
     │  /tmp/invest_pi_work       │                         │
     │  edit + commit + push  ───►│                         │
     │                            │                         │
     │                            │   auto_pull (alle 2min) │
     │                            │◄────────────────────────│
     │                            │                         │
     │                            │     Code aktiv          │
     │                            │                         │
     │                            │   status_push (alle 2min)│
     │                            │◄────────────────────────│
     │                            │                         │
     │  git fetch + show          │                         │
     │  origin/main:_status/...   │                         │
     │  ◄─────────────────────────│                         │
```

**Time-Lag pro Code-Change:** Push → Pi-pull (≤2min) → Smoke-Test (~30s) → Pi-Service-Restart-Cycle (≤2min) = **~3-4 Min nach Push ist Code live**.

### Workflow-Boilerplate

```bash
# Session-Start in Cowork
TOKEN=$(grep -oE 'oauth2:[^@]+' /sessions/determined-affectionate-clarke/mnt/Aktien/.git/config | head -1 | cut -d: -f2)
cd /tmp && rm -rf invest_pi_work
git clone --quiet "https://oauth2:${TOKEN}@github.com/mertoege/invest-pi.git" invest_pi_work
cd /tmp/invest_pi_work
git config core.autocrlf false
git config user.email "mert.oege@gmail.com"
git config user.name "Mert Oege"

# Edit, commit, push
# ... edits ...
git add -A && git commit -m "..." && git pull --rebase --no-edit --quiet && git push --quiet

# Status vom Pi lesen
git fetch --quiet && git show origin/main:_status/snapshot.json | python3 -m json.tool
```

### Quirks (alles aus Schmerz gelernt — NIEMALS direkt im Mount git-operieren)

1. **Mount-IO unzuverlässig für Git** — `git checkout`, `rm` und `git rebase` scheitern oft mit "Operation not permitted". Nur in `/tmp` arbeiten.
2. **CRLF-Renormalization** — `git config core.autocrlf false` IMMER setzen.
3. **Write-Tool-Truncation** — Files > ~250 Zeilen werden im Mount truncated. Workaround: Bash heredoc nach `/tmp`, dann `cp` zum Mount.
4. **`__pycache__` lässt sich auf Mount nicht löschen** — `PYTHONDONTWRITEBYTECODE=1` als Lösung.
5. **PowerShell-`curl` ist `Invoke-WebRequest`** — bei Befehlen für Mert immer `curl.exe` schreiben.
6. **Status-push race conditions** — `git pull --rebase --no-edit` IMMER vor Push.
7. **WAL-Mode crasht auf Mount** — `storage.connect()` hat `try/except OperationalError` als graceful fallback.

---

## 📁 Wichtige Files

### Source-Code (`src/`)

| Pfad | Zweck |
|---|---|
| `src/common/storage.py` | 5-DB-Schemas + connect-Helper + Migrationen |
| `src/common/predictions.py` | log_prediction, record_outcome, hit_rate, log_feedback |
| `src/common/outcomes.py` | T+1d/7d/30d-Messung, drift-detection |
| `src/common/cost_caps.py` | 3-Tier-Hard-Stop (hourly/daily/monthly) |
| `src/common/llm.py` | Anthropic-Wrapper (call_sonnet/opus/haiku) mit cost-tracking |
| `src/common/fx.py` | EUR/USD live via yfinance, 24h-cached |
| `src/common/performance.py` | Sharpe/Sortino/Max-DD/Calmar |
| `src/common/json_utils.py` | strip_codefence, safe_parse, extract_json_block |
| `src/common/retry.py` | Tenacity-Profile für yfinance/finnhub/anthropic |
| `src/common/logging_setup.py` | RotatingFileHandler-Logger |
| `src/common/config.py` | config.yaml-Loader (legacy, für Universe etc.) |
| `src/common/data_loader.py` | yfinance + market.db-Cache |
| `src/trading/__init__.py` | TradingConfig + load_trading_config + get_active_profile |
| `src/trading/decision.py` | Conservative/Moderate-Strategy-Engine + Multi-Horizon + Adaptive |
| `src/trading/sizing.py` | Position-Sizing mit Vol-Targeting |
| `src/risk/limits.py` | kill-switch, market-hours, stop_loss, take_profit, trailing, cash-floor, sector-concentration |
| `src/broker/base.py` | Abstract BrokerAdapter |
| `src/broker/mock.py` | In-Memory-Sim |
| `src/broker/alpaca.py` | Alpaca-Paper-API-Wrapper |
| `src/learning/pattern_miner.py` | Drawdown-Detection + Feature-Vektoren + Similarity-Search |
| `src/learning/regime.py` | HMM-Regime-Detection (3-state Gaussian HMM auf SPY+VIX) |
| `src/learning/calibration.py` | calibration_block für LLM-Prompt-Injection |
| `src/learning/attribution.py` | Performance pro Risk-Dim |
| `src/learning/backtest_engine.py` | Walk-Forward-Backtester mit adaptive-mode |
| `src/alerts/risk_scorer.py` | 9-Dim-Risk-Scoring + Pattern-Library-Lookup |
| `src/alerts/notifier.py` | Telegram-API-Wrapper (HTML, Inline-Buttons) |
| `src/alerts/dispatch.py` | Auto-Push für neue Stufe-2/3-Alerts |
| `src/jobs/telegram_callbacks.py` | 1-min-poll für Inline-Button-Klicks |

### Scripts (`scripts/`)

| Pfad | Zweck | systemd-Timer |
|---|---|---|
| `scripts/score_portfolio.py` | Stündliches Scoring | invest-pi-score |
| `scripts/run_strategy.py` | Mo-Fr Trade-Decisions | invest-pi-strategy |
| `scripts/sync_positions.py` | Stündlicher Broker→DB-Sync | invest-pi-sync |
| `scripts/track_outcomes.py` | Tägliche Outcome-Messung | invest-pi-outcomes |
| `scripts/monthly_dca.py` | Monatlicher Sonnet-DCA-Empfehlung | invest-pi-monthly-dca |
| `scripts/meta_review.py` | Monatlicher Opus-Self-Review | invest-pi-meta-review |
| `scripts/daily_report.py` | Täglicher PnL-Telegram-Push | invest-pi-daily-report |
| `scripts/build_patterns.py` | Pattern-Library Bootstrap | invest-pi-patterns |
| `scripts/train_regime.py` | Wöchentlicher HMM-Retrain | invest-pi-train-regime |
| `scripts/backup_databases.sh` | Tägliche DB-Backups | invest-pi-backup |
| `scripts/hardware_check.py` | CPU/Disk/Mem-Alerts | invest-pi-hardware |
| `scripts/auto_pull.sh` | Push-to-Deploy + Auto-Rollback | invest-pi-auto-pull |
| `scripts/status_push.sh` | snapshot.json → GitHub | invest-pi-status-push |
| `scripts/systemd_sync.sh` | Wrapper für systemd-File-Sync (NOPASSWD-sudo) | (kein Timer, von auto_pull aufgerufen) |
| `scripts/setup_pi.sh` | Initial-Pi-Installation (einmalig) | (manuell) |
| `scripts/backtest.py` | Walk-Forward-Backtest CLI | (manuell) |
| `scripts/score_skip_report.py` | Diagnose welche Tickers nicht gescort | (manuell) |
| `scripts/test_telegram.py` | Telegram-API-Smoke-Test | (manuell) |

### Tests (`tests/`)

7 Test-Module, 30 Tests total. Lauf-Befehl:
```bash
INVEST_PI_DATA_DIR=/tmp/test PYTHONDONTWRITEBYTECODE=1 python3 -B tests/test_<modul>.py
```

### Konfiguration

- `config.yaml` — Single source of truth für portfolio + universe + settings + strategies + regime_profiles + sector_map + api_keys-Templates
- `.env.example` — Template für Pi-Setup
- `requirements.txt` — Pinned dependencies (yfinance, alpaca-py, anthropic, hmmlearn, pandas, etc.)

---

## 💾 Memory-Files (in `~/.claude-cowork/.../memory/`)

Auto-loaded in jeder Session via MEMORY.md:

| Datei | Inhalt |
|---|---|
| `LESSONS_FOR_INVEST_PI.md` | Architektur-Bibel mit 7 TL;DR-Übertragungen aus PokéPi, 9 Bug-Stories, Pi-Comms-Workflow |
| `pokepi_project.md` | Schwester-Codebase, etablierte Patterns |
| `dev_environment.md` | Mount-Quirks (WAL, Write-Truncation, etc.) |
| `phase0_status.md` | Phase-0+1-Snapshot |
| `trading_layer.md` | Phase-2-Setup (Alpaca, Conservative-Defaults, Echtgeld-Sperre) |
| `pi_deployment.md` | Single-Pi-Entscheidung (shared mit PokéPi) |
| `pi_comms_workflow.md` | GitHub-als-Vermittler-Pattern |
| `pi_host.md` | Tailscale-IP + SSH-Setup |
| `deep_research_findings.md` | State-of-the-Art-Findings (Marktlage 2026, RL-Pitfalls, LLM-Trading, Backtesting-Bias, HMM-Regime) |
| `optimization_roadmap.md` | Phase A-D mit konkreten Tasks, Aufwand, Wartezeit-Abhängigkeit |

---

## 🛠 Roadmap — Was ist fertig vs offen

### ✅ Komplett implementiert

| Phase | Was |
|---|---|
| **Phase 0** | Storage (5 DBs), predictions, cost_caps, json_utils, retry, logging_setup |
| **Phase 1** | Self-Learning-Loop (predictions, outcomes, drift-detection) |
| **Phase 2** | Paper-Trading (broker, decision, sizing, risk-limits, run_strategy) |
| **Phase 3a** | Telegram-Notifier mit HTML + Inline-Buttons |
| **Phase 3b** | Telegram-Callback-Handler (60s-poll, fb:/fbr:/dca: patterns) |
| **Phase 4** | Meta-Review-Skelett (Opus + calibration_block) |
| **Phase 5** | Pi-Operations (auto_pull, status_push, hardware_check, backups, error_alerts) |
| **Anthropic-Layer** | LLM-Wrapper mit Cost-Tracking |
| **monthly_dca** | Sonnet-Job mit Inline-Buttons |
| **A1** Vol-Targeting | NVDA-Position automatisch kleiner als MSFT |
| **A2** Performance-Attribution | hit_rate pro Risk-Dim |
| **A3** Performance-Metrics | Sharpe/Sortino/Max-DD im daily_report |
| **B1** HMM-Regime-Detection | 3-state Gaussian HMM auf SPY+VIX, wöchentliches Retrain |
| **B2** Multi-Horizon-Strategy | long_term + mid_term parallel mit eigenen Schwellen |
| **B3 V1** Backtesting-Engine | Walk-Forward, no-look-ahead, 4-strategy-compare |
| **B3 V2** Enhanced Backtester | 9-Dim Risk-Scoring, Vol-Targeting, Cash-Floor, Sector-Cap, Daily-Loss-Brake, Multi-Horizon |
| **Adaptive-Mode** | Regime-basierte Strategy-Auswahl pro Tag |
| **USD-Tracking** | EUR + USD + FX in equity_snapshots |
| **Stabilitäts-Setup** | 20 Positionen, Cash-Floor, Sector-Cap, Vol-Targeting |
| **Daily-Report** | Täglicher Telegram-Push mit PnL + Performance-Metriken |
| **Tests** | 30/30 grün, 7 Module |
| **README + HANDOVER** | Dokumentation für Setup + Übergaben |

### ⏳ Noch offen — Phase A4/B/C/D (siehe `optimization_roadmap.md`)

| ID | Was | Wartezeit | Aufwand |
|---|---|---|---|
| **A4** | Confidence-Auto-Calibration | wartet auf 30+ Outcomes (Tag 14+) | 4h |
| ~~**B3 V2**~~ | ~~Backtester mit voller 9-Dim risk_scorer~~ | **DONE** | Session 8 |
| **B4** | Survivorship-Bias-Mitigation (delisted Tickers) | nicht-blockiert | 6h (alternative Datenquellen) |
| **C1** | Multi-Agent-LLM-Decision (TradingAgents-Pattern) | nicht-blockiert | 1 Woche, ~+25€/Mo cost |
| **C2** | Hallucination-Detection für LLM-Outputs | nach C1 | 2 Tage |
| **C3** | Prompt-Chaining (fine-grained task decomposition) | nicht-blockiert | 3 Tage |
| **D1** | RL-Layer für Position-Sizing | nach 6+ Mo Daten | 2-3 Wochen |
| **D2** | FastAPI Dashboard | nicht-blockiert | 1 Woche |
| **D3** | IBKR-Adapter für Echtgeld | nach Echtgeld-Entscheidung | 2 Wochen |
| **D4** | Kelly-basierte Position-Sizing | nach A4 | 2 Tage |

### 🎯 Was als nächstes empfohlen

1. **4-8 Wochen Live-Beobachtung** des aktuellen ADAPTIVE-Setups
2. Dann: A4 + B3 V2 (datenbasierte Optimierungen)
3. Dann: B4 (Survivorship-Bias) für robustere Pattern-Library
4. Phase C kommt erst sinnvoll mit Live-Daten zur Validierung

---

## 🔧 Diagnose-Befehle (für Mert)

### Pi-Status komplett

```bash
# Snapshot über GitHub (von überall aus möglich)
git fetch && git show origin/main:_status/snapshot.json | jq .

# Oder direkt vom Pi
ssh pi@100.92.115.43
sudo -u investpi cat /home/investpi/invest-pi/_status/snapshot.json | jq .
```

### Failed Services / Telegram-Spam debuggen

```bash
ssh pi@100.92.115.43
systemctl --failed --no-pager
for s in $(systemctl list-units --state=failed --no-legend --no-pager | awk '{print $2}' | grep invest-pi); do
  echo "=== $s ==="
  sudo journalctl -u "$s" --since="1 hour ago" --no-pager | tail -20
done
```

### Trade-/Performance-Inspektion

```bash
# Heutige Trades
sudo -u investpi sqlite3 /home/investpi/invest-pi/data/trading.db \
  "SELECT * FROM trades WHERE date(created_at,'localtime')=date('now','localtime') ORDER BY id DESC"

# Hit-Rate letzte 30d
sudo -u investpi sqlite3 /home/investpi/invest-pi/data/learning.db \
  "SELECT confidence, COUNT(*) AS n,
          SUM(CASE WHEN outcome_correct=1 THEN 1 ELSE 0 END) AS correct
     FROM predictions
    WHERE job_source='daily_score'
      AND created_at >= datetime('now','-30 day')
    GROUP BY confidence"

# Cost-Verbrauch diesen Monat
sudo -u investpi sqlite3 /home/investpi/invest-pi/data/learning.db \
  "SELECT api, ROUND(SUM(cost_eur),2) AS eur
     FROM cost_ledger
    WHERE strftime('%Y-%m', timestamp)=strftime('%Y-%m','now')
    GROUP BY api"

# Open Positions mit Strategy-Label
sudo -u investpi sqlite3 /home/investpi/invest-pi/data/trading.db \
  "SELECT ticker, qty, avg_price_eur, peak_price, strategy_label, opened_at FROM positions"
```

### Backtest manuell

```bash
sudo -u investpi bash -c 'cd /home/investpi/invest-pi && \
  python3 scripts/backtest.py --compare-strategies --start 2022-01-01 --end 2025-12-31'
```

### Notfall: Trading stoppen

```bash
# Kill-Switch — Service-Loop läuft weiter, aber tradet nichts
sudo -u investpi touch /home/investpi/invest-pi/data/.KILL

# Aufheben
sudo -u investpi rm /home/investpi/invest-pi/data/.KILL
```

---

## 🚨 Bekannte Failure-Modi + Fixes

| Problem | Symptom | Fix |
|---|---|---|
| outcomes-cron crasht (TZ-Bug) | Telegram-Failure-Push 02:30 mit "TypeError: can't compare offset-naive..." | bereits gefixt mit T108c7c0 (`replace(tzinfo=None)`) |
| Telegram-Service-Failure-Spam | Mehrere "service failure" pro Tag im Telegram | journalctl pro Service prüfen, meist single recurring crash mit 1h cooldown |
| auto-pull rebase-conflict | "non-fast-forward" beim Push | auto_pull.sh hat Recovery-Path: `git reset --hard origin/main` |
| status-push race-condition | gelegentlich failed | self-healing, `git pull --rebase --no-edit` davor |
| systemd-Files nicht gesynced | "Unit ... does not exist" | manuell: `sudo bash -c 'cp /home/investpi/.../scripts/systemd/* /etc/systemd/system/' && sudo systemctl daemon-reload` |

---

## 💰 Cost-Bilanz aktuell

| Komponente | EUR/Monat |
|---|---|
| Alpaca Paper, yfinance, Telegram, GitHub | 0 |
| Sonnet (monthly_dca, ~5k tokens, 1×/Monat) | ~0,03 |
| Opus (meta_review, ~20k tokens, 1×/Monat × 2 sources) | ~5-8 |
| **Gesamt aktuell** | **~5-8 €** |
| **Hard-Cap** | **50 €** |

→ ~85% Puffer für künftige Phase-C-Erweiterungen (Multi-Agent-LLM).

---

## 🔐 Security-Status

- ✅ Alpaca-Keys + Anthropic-Keys + Telegram-Token in `.env` (chmod 600)
- ✅ GitHub-PAT eingebettet in `.git/config` (Pi + Mert's Windows-Clone)
- ✅ NOPASSWD-sudoers nur scoped auf systemd_sync.sh + daemon-reload
- ✅ Echtgeld-Sperre durch hartcodiertes `paper-api.alpaca.markets`
- ✅ Kill-Switch via .KILL-File
- ⚠ Pi-SSH per Passwort (sollte später auf SSH-Key umgestellt werden)
- ⚠ GitHub-PAT 90-Tage-Expiry empfohlen

---

## 📞 Was Mert in einer neuen Session tun sollte

1. **In Cowork**: einfach neue Session öffnen — MEMORY.md ist auto-loaded
2. **Diese HANDOVER.md** ist auch im Repo — kann gelesen werden via:
   ```bash
   cat /sessions/determined-affectionate-clarke/mnt/Aktien/HANDOVER.md
   ```
3. **Nach Login**: Pi-Status checken (snapshot.json) bevor weitere Aktionen
4. **Wenn Code-Änderung nötig**: workflow-Boilerplate aus diesem Dokument

---

## 🎓 Realistische Erwartungen (ehrlich kommuniziert)

Aus 167-Studien-Meta-Analyse: **>90% akademischer Trading-Strategien failen mit Echtkapital**, selbst nach guten Backtests. "Implementation quality + domain knowledge > algorithmic complexity."

**Realistische Live-Performance ADAPTIVE über 12 Monate:**
- ~10-15% Wahrscheinlichkeit: Sharpe > 1.2, +15-25% Bull-Markt-Rendite
- ~50% Wahrscheinlichkeit: Sharpe 0.8-1.2, +8-15% Bull / -5-8% Bear
- ~25% Wahrscheinlichkeit: Sharpe 0.4-0.8, underperformt SMH-Hold im Bull
- ~10% Wahrscheinlichkeit: Sharpe < 0.4, Strategie-Versagen

**Über 3 Jahre stabilisieren** sich diese Werte. Ein einzelnes Jahr ist statistisch zu kurz für robuste Aussage.

---

## 📜 Session-History (was passiert ist, in chronologischer Reihenfolge)

1. **Session 1 (28.04.2026)** Phase 0+1 Foundation: Self-Learning-Loop, predictions, cost_caps, outcomes
2. **Session 2 (28.04.2026)** Phase 2: Paper-Trading-Layer, Alpaca-Setup, Conservative-Default
3. **Session 3 (28.04.2026)** Phase 3a Telegram-Notifier (HTML + Inline-Buttons)
4. **Session 4 (28.04.2026)** Pi-Setup + GitHub-Push-to-Deploy + Telegram-Test
5. **Session 5 (29.04.2026)** Phase 3b + 5 + Optimierungen: Callback-Handler, monthly_dca, Backups, Error-Alerts, Tests
6. **Session 6 (29.04.2026)** Deep Research → Phase A1+A2+A3 + B1 (HMM) + Stabilitäts-Setup + Backtesting V1
7. **Session 7 (30.04.2026)** TZ-Bug-Fix + Multi-Horizon (B2) + Adaptive-Mode + 3-Jahres-Backtest-Validation

8. **Session 8 (30.04.2026)** B3 V2: Enhanced Backtester mit 9-Dim-Scoring, Vol-Targeting, Constraints, 16 neue Tests

Plus 68 Code-Commits (ohne status-pushes), ~6500 LOC Application-Code, 14 systemd-Timer.

---

**Ende der Handover-Doku.** Bei Fragen / Updates: einfach diese Datei editieren + ins Repo committen.

Generiert: 2026-04-30 nach Session 7.
