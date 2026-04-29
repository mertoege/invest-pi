# Invest-Pi

Autonomes Self-Learning-Investment-System auf Raspberry Pi 5. Risk-Scoring, Paper-Trading auf Alpaca-Demo, Telegram-Feedback-Loop, monatliche Sonnet-DCA-Empfehlungen, Opus-Meta-Reviews — schließt sich zum lernenden Kreislauf.

## Was es macht

- **Stündlich** scort der Pi alle ~43 AI/Tech-Tickers nach 9 Risiko-Dimensionen
- **Mo-Fr 16:00 CEST** entscheidet er auf dem Alpaca-Paper-Konto: kaufen / nicht kaufen / verkaufen (Stop-Loss / Take-Profit / Trailing-Stop)
- **Stündlich** synct er Broker-State zurück, schreibt Equity-Snapshots (EUR + USD + FX)
- **Täglich 02:30** misst er die Outcomes der vergangenen Predictions (T+1d / 7d / 30d)
- **1× / Monat** liefert Sonnet eine 50€-DCA-Empfehlung an dich via Telegram
- **1× / Monat** reflektiert Opus über alle Outcomes, schreibt einen Action-Plan, der in nachfolgende Prompts injected wird (Self-Learning-Loop)
- **Telegram-Buttons** für jeden Alert/Trade/DCA — Klicks landen in der Lerndatenbank
- **24/7 Pi-Health** via Hardware-Monitor + auto-pull (Push-to-Deploy mit Auto-Rollback)

## Architektur (5-DB / mehrschichtig)

```
┌─────────────────────────────────────────────────────────────────┐
│  scripts/                                                        │
│    score_portfolio  run_strategy  sync_positions  track_outcomes │
│    monthly_dca   meta_review   daily_report   build_patterns     │
│    backup_databases   hardware_check                             │
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
   │ (kill,SL,TP,   │  │  dispatch    │  │  calibration    │
   │  trailing,     │  └──────┬───────┘  └──────┬──────────┘
   │  daily-loss)   │         │                 │
   └───────┬────────┘         │                 │
           │                  │                 │
   ┌───────┴──────────────────┴─────────────────┴─────────┐
   │  src/broker                                           │
   │    base (Adapter)  mock (Sim)  alpaca (Paper)         │
   └────────────────────────┬──────────────────────────────┘
                            │
   ┌────────────────────────┴──────────────────────────────┐
   │  src/common                                            │
   │    storage (5 DBs)   config   data_loader   fx         │
   │    predictions  outcomes  cost_caps  llm               │
   │    json_utils  retry  logging_setup                    │
   └────────────────────────────────────────────────────────┘
```

5 SQLite-DBs:
- `market.db` — yfinance-Cache (OHLCV, Fundamentals, FX-Rate)
- `patterns.db` — Pre-Drawdown-Muster aus 10y-Historie
- `alerts.db` — Risk-Score-Historie + Telegram-Notifications
- `learning.db` — predictions + outcomes + feedback_reasons + cost_ledger + meta_reviews
- `trading.db` — trades + positions (mit peak_price) + equity_snapshots (USD+EUR+FX)

## Quickstart

### Pi-Setup (einmal)

```bash
ssh pi@<pi-ip>

curl -sL https://raw.githubusercontent.com/<user>/invest-pi/main/scripts/setup_pi.sh -o /tmp/setup_pi.sh
sudo GITHUB_TOKEN=ghp_xxx GITHUB_USER=<user> bash /tmp/setup_pi.sh
```

Setup macht:
1. apt-Pakete (python3-pip, sqlite3, rsync, git, curl, jq)
2. System-User `investpi` anlegen
3. Repo klonen mit eingebettetem Token nach `/home/investpi/invest-pi/`
4. Python-Dependencies via `pip install -r requirements.txt --user`
5. data/, logs/, _status/ Verzeichnisse anlegen
6. .env aus .env.example
7. systemd-Service- und Timer-Files installieren (aber nicht enablen)
8. Smoke-Test laufen lassen

### Keys eintragen

```bash
sudo -u investpi nano /home/investpi/invest-pi/.env
```

Erforderlich:
- `ALPACA_API_KEY` + `ALPACA_API_SECRET` — Paper-Account von app.alpaca.markets
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — eigener Bot via @BotFather
- `ANTHROPIC_API_KEY` — für monthly_dca + meta_review
- Optional: `RESTIC_REPOSITORY` + `RESTIC_PASSWORD` für Cloud-Backup

### Timer aktivieren

```bash
sudo systemctl daemon-reload
for s in score sync outcomes strategy auto-pull status-push hardware patterns \
         telegram-callbacks monthly-dca meta-review backup daily-report; do
  sudo systemctl enable --now invest-pi-$s.timer
done

systemctl list-timers 'invest-pi-*' --no-pager   # sollte 13 Timer zeigen
```

## Konfiguration

`config.yaml.settings.trading`:

```yaml
mode: "moderate"             # conservative | moderate | experimental
score_buy_max: 45            # composite-Schwelle (conservative: 25)
moderate_alert_max: 1        # alert_level<=1 erlaubt (conservative: 0)
max_open_positions: 8        # gleichzeitig (conservative: 5)
max_position_eur: 300.0      # pro Position (conservative: 200)
max_trades_per_day: 5        # Hard-Cap (conservative: 3)
stop_loss_pct: 0.10          # auto-sell -10% (conservative: 0.15)
take_profit_pct: 0.20        # auto-sell +20% (NEU)
trailing_stop_pct: 0.08      # -8% vom Hoch (NEU)
trailing_activation_pct: 0.12 # ab +12% Profit aktiv
max_daily_loss_pct: 0.05     # Equity -5% Tageshoch → kein neuer Buy
```

Switch zurück auf conservative: einzig `mode:` Wert ändern + alle anderen Felder bleiben aber laden ihre Defaults.

## systemd-Timer Schedule

| Timer | Schedule | Was |
|---|---|---|
| score | stündlich :30 | Risk-Scoring |
| strategy | Mo-Fr 16:00 | Trade-Decisions |
| sync | stündlich :35 | Broker→DB |
| outcomes | täglich 02:30 | T+1d/7d/30d-Messung |
| auto-pull | alle 2 Min | Push-to-Deploy |
| status-push | alle 2 Min | snapshot.json |
| hardware | alle 30 Min | CPU/Disk/Mem-Alerts |
| telegram-callbacks | alle 60s | Button-Klicks → DB |
| patterns | 1. des Monats 03:00 | Pattern-Library |
| monthly-dca | 1. des Monats 14:00 | Sonnet-Empfehlung |
| meta-review | 2. des Monats 04:00 | Opus-Reflexion |
| backup | täglich 03:30 | DB-Snapshots |
| daily-report | täglich 21:30 | Telegram-PnL |

## Self-Learning-Loop

```
Score (heuristic)              Telegram-Buttons (User-Feedback)
  ↓ predictions                  ↓ feedback_reasons
  ↓                              ↓
Outcomes (T+1d/7d/30d)         ┌─┘
  ↓ outcome_correct            │
  ↓                            ↓
Meta-Review (Opus, monatlich) ←┘
  ↓ summary_md + action_plan
  ↓
calibration_block ────────┐
                          ↓ injected in alle Sonnet/Opus-Prompts
naechster Score / DCA / Review wird kalibrierter
```

## Diagnose-Befehle

```bash
# Pi-Status (zeigt git.commit, equity, services, hardware)
git fetch && git show origin/main:_status/snapshot.json | jq .

# Heutige Trades
sudo -u investpi sqlite3 ~/invest-pi/data/trading.db \
  "SELECT * FROM trades WHERE date(created_at)=date('now') ORDER BY id DESC"

# Hit-Rate letzte 30d
sudo -u investpi sqlite3 ~/invest-pi/data/learning.db \
  "SELECT confidence, COUNT(*) AS n,
          SUM(CASE WHEN outcome_correct=1 THEN 1 ELSE 0 END) AS correct
     FROM predictions
    WHERE job_source='daily_score'
      AND created_at >= datetime('now','-30 day')
    GROUP BY confidence"

# User-Feedback letzten Monat
sudo -u investpi sqlite3 ~/invest-pi/data/learning.db \
  "SELECT feedback_type, reason_code, COUNT(*) FROM feedback_reasons GROUP BY 1,2"

# Cost-Verbrauch diesen Monat
sudo -u investpi sqlite3 ~/invest-pi/data/learning.db \
  "SELECT api, ROUND(SUM(cost_eur),2) AS eur
     FROM cost_ledger
    WHERE strftime('%Y-%m', timestamp) = strftime('%Y-%m','now')
    GROUP BY api"

# Service-Status + letzte Logs
systemctl status 'invest-pi-*'
journalctl -u invest-pi-strategy.service --since="24 hours ago"

# Manuell Trades stoppen (Kill-Switch)
sudo -u investpi touch /home/investpi/invest-pi/data/.KILL
sudo -u investpi rm /home/investpi/invest-pi/data/.KILL    # Aufheben
```

## Tests

```bash
cd /home/investpi/invest-pi
PYTHONDONTWRITEBYTECODE=1 INVEST_PI_DATA_DIR=/tmp/test python3 -B tests/test_smoke.py
PYTHONDONTWRITEBYTECODE=1 INVEST_PI_DATA_DIR=/tmp/test python3 -B tests/test_trading.py
PYTHONDONTWRITEBYTECODE=1 INVEST_PI_DATA_DIR=/tmp/test python3 -B tests/test_outcomes.py
PYTHONDONTWRITEBYTECODE=1 INVEST_PI_DATA_DIR=/tmp/test python3 -B tests/test_calibration.py
PYTHONDONTWRITEBYTECODE=1 INVEST_PI_DATA_DIR=/tmp/test python3 -B tests/test_decision_modes.py
```

Alle 21 Tests müssen grün sein nach jedem Code-Change. `auto_pull.sh` macht das automatisch nach jedem Pull, mit Auto-Rollback bei Fail.

## Schutz-Mechanismen

- **Kein Echtgeld:** AlpacaPaperBroker fest paper-only, keine Live-Klasse
- **Kill-Switch:** `touch data/.KILL` blockt alle Trades
- **Cost-Hard-Stop:** 50€/Monat Anthropic-Cap, kickt automatisch
- **Trade-Limits:** max 5/Tag, max 8 offene Positionen
- **Risiko-Limits:** Stop-Loss -10%, Take-Profit +20%, Trailing-Stop -8% vom Hoch ab +12%
- **Daily-Loss-Bremse:** -5% Equity/Tag (USD-basis, FX-resistent) → keine neuen Buys
- **Markt-Hours:** keine Orders außerhalb Mo-Fr 15:30-22:00 CET
- **alert>=2 → IMMER skip:** Caution-Level blockiert Buys hart
- **Auto-Rollback:** Push der Smoke-Test bricht wird vom Pi automatisch zurück
- **Software-Error-Telegram:** alle 12 Cron-Services pingen bei Crash
- **Hardware-Monitor:** CPU>75°C / Disk>90% / Mem>90% → Telegram (max 1×/h pro Metrik)
- **DB-Backup:** täglich gzipped, 14d retention, optional restic+B2

## Cost-Bilanz (laufend)

| Komponente | EUR/Monat |
|---|---|
| Alpaca Paper, yfinance, Telegram, GitHub | 0 |
| Sonnet (monthly_dca) | ~0,03 |
| Opus (meta_review) | ~5-8 |
| **Gesamt aktuell** | **~5-8** |

50€-Hard-Cap → ~85% Puffer.

## Roadmap

- **Phase 4 wird live** (~7-14 Tage nach Setup): erste Outcomes da, Opus-Meta-Review aktiv
- **A/B-Testing-Layer:** moderate-v1 vs v2 parallel
- **IBKR-Adapter:** für Echtgeld auf EU-Tickers später
- **FastAPI Dashboard:** Web-UI auf localhost:8000

## Lizenz

Privat-Projekt. Keine Lizenz, keine Verteilung.
