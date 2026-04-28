# Invest-Pi · Project Status

**Stand:** 2026-04-28 nach Foundation + Paper-Trading-Layer
**Mission:** Pi analysiert + tradet autonom auf Demo-Account, lernt ueber Monate, optimiert sich selbst. Plus 1x/Monat 50€-DCA-Vorschlag an Mert via Telegram.
**API-Budget:** max 50€/Monat (Anthropic + Finnhub + NewsAPI)

---

## Architektur-Schichten

```
┌─────────────────────────────────────────────────────────┐
│  scripts/run_strategy.py        scripts/track_outcomes  │
│  scripts/sync_positions         scripts/score_portfolio │
└────────────────┬───────────┬───────────┬────────────────┘
                 │           │           │
        ┌────────┴───┐  ┌────┴─────┐  ┌──┴──────────┐
        │ src/trading│  │ src/risk │  │ src/alerts  │
        │ decision   │  │ limits   │  │ risk_scorer │
        │ sizing     │  │          │  │             │
        └────────┬───┘  └─────┬────┘  └──────┬──────┘
                 │            │              │
        ┌────────┴────────────┴──────────────┴────────┐
        │  src/broker         src/learning            │
        │  base               pattern_miner           │
        │  mock                                       │
        │  alpaca                                     │
        └─────────────────────┬───────────────────────┘
                              │
                  ┌───────────┴────────────┐
                  │  src/common            │
                  │  storage               │
                  │  config                │
                  │  data_loader           │
                  │  predictions  ◄── Self-Learning-Anker
                  │  outcomes                          
                  │  cost_caps
                  │  json_utils, retry
                  └────────────────────────┘
```

5 SQLite-DBs, klar getrennt: `market.db`, `patterns.db`, `alerts.db`, `learning.db`, `trading.db`.

---

## Was steht (verifiziert via Tests, 11/11 gruen)

### Phase 0 — Foundation (LESSONS-Plan)
- `storage.py`: 5 DBs, predictions+outcomes+feedback+cost_ledger+meta_reviews+trades+positions+equity_snapshots Schema.
- `json_utils.py`: strip_codefence, safe_parse, extract_json_block.
- `retry.py`: api_retry decorator + Profile fuer yfinance/finnhub/newsapi/anthropic.
- `predictions.py`: log_prediction, record_outcome, mark_batch_aggregate, hit_rate stratified, log_feedback.
- `cost_caps.py`: 3-Tier (hourly/daily/monthly), calendar-day, cost_awareness_block fuer Prompt-Injection.

### Phase 1 — Self-Learning-Loop
- `risk_scorer.score_ticker()` schreibt jede Berechnung als prediction-Row mit subject_id + prompt_hash + confidence-stratifiziert.
- `outcomes.py`: T+1d/7d/30d Messung + Drift-Detection (recent vs prior 7d hit-rate).
- `scripts/track_outcomes.py`: Cron-Runner.

### Phase 2 — Paper-Trading-Layer **(neu, ersetzt urspruengliches Phase-2-Telegram)**
- `src/broker/`: Abstract `BrokerAdapter` + `MockBroker` (in-memory Sim) + `AlpacaPaperBroker` (alpaca-py SDK, env-driven, Paper-only — Live wuerde separate Klasse + LIVE_TRADING-Toggle erfordern).
- `src/trading/`: `decision.py` (Conservative-Strategy-Engine: composite<25 + triggered_n=0 + ring 1/2 + conf>=medium → buy; alert>=2 → skip), `sizing.py` (high=100%/medium=60%/low=30% Konfidenz-Scaling).
- `src/risk/limits.py`: kill_switch (data/.KILL-File), market_hours (Mo-Fr 15:30-22:00 CET, naive), max_trades_per_day, max_daily_loss_pct, stop-loss-Detection.
- `scripts/run_strategy.py`: Main-Orchestrator (init → stop-loss-pass → buy-pass → equity-snapshot). `--dry-run` + `--mock` Flags.
- `scripts/sync_positions.py`: Broker → DB Sync (positions overwrite, equity-snapshot append). Hourly cron.
- Jede Trade-Decision wird als prediction-Row mit job_source='trade_decision' geloggt → outcome_tracker misst T+1d/7d/30d-PnL → meta_review (Phase 4) kalibriert das System.

---

## Was als naechstes dran ist

### Phase 2.5 (Mert-Aktion, blockierend fuer Live-Schaltung)
1. Alpaca Paper-Account anlegen → https://app.alpaca.markets/paper/dashboard/overview
2. ALPACA_API_KEY + ALPACA_API_SECRET als env-Variable auf dem Pi setzen (oder `.env`).
3. `pip install alpaca-py --break-system-packages` auf dem Pi.
4. Test: `python scripts/sync_positions.py` → sollte ~10000 USD startbalance zeigen.

### Phase 3 — Telegram (Notification + DCA-Empfehlung an Mert)
- `src/alerts/notifier.py` mit send_alert(prediction_id, level) + parse_mode=HTML.
- `src/jobs/telegram_callbacks.py` 1-min-cron polls getUpdates → log_feedback.
- 1x/Monat: monthly_dca-Job sendet 50€-Empfehlung an Mert (Mert kauft selbst). Buttons: "habe gekauft" / "habe ETF gekauft" / "skip".

### Phase 4 — Meta-Review (monatlich, Opus)
- `src/jobs/meta_review.py`: load Outcomes pro source/ticker/confidence/ring → Opus-Call → `reviews/<date>-<source>.md` + prediction-Row.
- `load_meta_reviews()` injiziert letzte Reviews + 30d-hit-rate + feedback_summary in jeden Sonnet/Heuristic-Score-Lauf.
- Drift-Warnung als Telegram-Push.

### Phase 5 — Pi-Operations
- `status_push.sh` (snapshot.json all 2 Min ins Repo) + `auto_pull.sh` (push-to-deploy mit 90s-healthcheck + auto-rollback).
- Daily DB-Backup gzipped + restic Cloud (B2).
- Hardware-Alerts (CPU/disk/memory).

### Phase 6 — Optimierung (nach 3+ Monaten Daten)
- Strategy-Variants A/B-tested via prompt_hash: conservative-v1 vs moderate-v2.
- Pattern-Library-Integration in Decision: Top-3-Analoga als zusaetzliches Signal.
- Macro-Layer als Multiplikator auf alle Buy-Schwellen (bei VIX>30 Schwelle anziehen).

---

## Architektur-Entscheidungen die getroffen wurden

| Entscheidung                              | Warum |
|-------------------------------------------|-------|
| 5 separate SQLite-DBs                     | Granulare Backups, klare Domains, kleine WAL-Files |
| predictions als Single-Source-of-Truth    | domain-agnostisch, alle Score-Quellen nutzen dieselbe Outcome-Pipeline |
| prompt_hash sha256[:16] von Tag 1         | spaeteres A/B-Testing der Prompt-Versionen |
| 3 Outcome-Fenster (T+1d/7d/30d)           | T+7d primary correctness, andere fuer Reichere Meta-Reviews |
| Calendar-day Cost-Aggregation             | LESSONS Bug-Story 2 |
| WAL graceful fallback                     | Pi nutzt WAL, Sandbox/Mount fallen auf default |
| INVEST_PI_DATA_DIR env override           | Tests koennen DBs in /tmp anlegen |
| Conservative als Default-Strategy         | low-risk Lerndaten in den ersten Monaten, dial-up spaeter ueber config.yaml |
| Echtgeld nur via separaten Live-Broker    | LIVE_TRADING-Toggle existiert NOCH NICHT — bewusst, kein Versehen moeglich |
| Stop-Loss bei -15%                        | Industriestandard, anpassbar via config |
| Ring 1+2 als tradeable                    | Pure-Plays + Oekosystem; Hyperscaler/EU/Software bleiben DCA-Only |
| Kill-Switch via .KILL-File                | low-tech und reliable; jeder Cron-Job prueft das |

---

## Bekannte Macken der Dev-Umgebung (siehe memory: dev_environment)

- WAL-Mode crasht auf dem Mount → graceful fallback ist drin.
- Write-Tool truncated Files >~250 Zeilen → Workflow ist: heredoc nach /tmp + cp.
- `__pycache__` lasst sich nicht loeschen → `PYTHONDONTWRITEBYTECODE=1` plus mtime-touch.

---

## Wie verifizieren

```bash
cd /path/to/Aktien
PYTHONDONTWRITEBYTECODE=1 python3 -B tests/test_smoke.py     # 6/6 passed
PYTHONDONTWRITEBYTECODE=1 python3 -B tests/test_trading.py   # 5/5 passed

# Live-Test mit MockBroker (kein Network):
python3 scripts/sync_positions.py --mock
python3 scripts/run_strategy.py --mock --dry-run
```

---

## Was Mert tun muss damit Live-Demo geht

1. **Alpaca Paper-Account erstellen** (https://app.alpaca.markets/paper/dashboard/overview, kostenlos, 2 Min).
2. **API Key + Secret kopieren** in `.env` auf dem Pi:
   ```
   ALPACA_API_KEY=PK...
   ALPACA_API_SECRET=...
   ```
3. **`pip install alpaca-py --break-system-packages`** auf dem Pi.
4. **Test-Run**: `python scripts/sync_positions.py` (sollte ~10k USD Paper-Balance zeigen).
5. **Cron-Setup** (systemd-Timer):
   - `score_portfolio.py` stuendlich
   - `run_strategy.py` taeglich 16:00 CET (eine Stunde nach Marktoeffnung)
   - `sync_positions.py` stuendlich
   - `track_outcomes.py` taeglich 02:00
6. **Monitoring**: `tail -f data/trading.db` ist sinnlos — stattdessen `sqlite3 data/trading.db "SELECT * FROM equity_snapshots ORDER BY timestamp DESC LIMIT 24"`.

---

## Schutzmechanismen (zusammengefasst)

- **Kein Echtgeld-Trading.** AlpacaPaperBroker fest auf paper-api.alpaca.markets gepinnt. Live wuerde eine separate Klasse + Toggle erfordern.
- **Kill-Switch.** `touch data/.KILL` blockiert ALLE neuen Trades sofort.
- **Cost-Cap Hard-Stop bei 50€/Monat.** Daily-Cap bei 2€, hourly 0.20€. Bei 70% des Tagesbudgets: cost_awareness_block in Prompts (wird Phase 3 angedockt).
- **max_trades_per_day=3.** Verhindert Klick-Hektik.
- **stop_loss_pct=15%.** Auto-Sell bei -15% pro Position.
- **max_daily_loss_pct=5%.** Bei -5% Equity-Drawdown vom heutigen Hoch: kein neuer Buy mehr.
- **Marktoeffnung-Check.** Keine Orders ausserhalb 15:30-22:00 CET Mo-Fr.
- **alert_level >= 2 → IMMER skip.** Selbst wenn composite niedrig — wenn das Risk-System Caution sagt, kein Buy.
