# Invest-Pi · Project Status

**Stand:** 2026-04-29 nach autonomer Optimierungs-Session
**Mission:** Pi analysiert + tradet autonom auf Alpaca-Demo, lernt ueber Monate, optimiert sich selbst. Plus 1×/Monat 50€-DCA-Vorschlag an Mert via Telegram.

---

## Was komplett live ist

| Phase | Status | Module |
|---|---|---|
| **Phase 0** Foundation | ✅ | storage, json_utils, retry, predictions, cost_caps |
| **Phase 1** Self-Learning-Loop | ✅ | risk_scorer (mit prediction-logging), outcomes, track_outcomes |
| **Phase 2** Paper-Trading | ✅ | broker (mock+alpaca), trading (decision+sizing), risk/limits, run_strategy |
| **Phase 3a** Telegram-Notifier | ✅ | notifier.py (HTML + Inline-Buttons), dispatch.py (auto-push bei Stufe-2) |
| **Phase 3b** Callback-Handler | ✅ | telegram_callbacks.py (1-min poll, fb:/fbr:/dca: patterns) |
| **Phase 5** Pi-Operations | ✅ | auto_pull, status_push, hardware_check, pattern-Library Auto-Bootstrap |
| **Anthropic-Wrapper** | ✅ | llm.py mit call_sonnet/opus/haiku, Cost-Tracking, Markdown-Strip |
| **monthly_dca** | ✅ | scripts/monthly_dca.py mit Sonnet-basierter Empfehlung |
| **FX dynamisch** | ✅ | fx.py via yfinance EURUSD=X mit 24h-Cache |
| **Skip-Diagnose** | ✅ | score_portfolio loggt skip-reason als prediction-Row |

## Was noch offen ist

| Phase | Was | Wann sinnvoll |
|---|---|---|
| **Phase 4** Meta-Review | Opus-Job analysiert Outcomes monatlich, kalibriert prompts | Nach ~30 outcome-getrackten Predictions, also frühestens in ~1 Woche |
| **Restic-Backup** | Lokal gzipped + B2 cloud, taeglich 03:30 + 04:00 | Wenn DBs > 100 MB werden, niedrige Prio jetzt |
| **FastAPI Dashboard** | Web-UI fuer Live-Status auf localhost:8000 | optional, niedrige Prio |

---

## Architektur-Schichten (aktualisiert)

```
┌───────────────────────────────────────────────────────────────────┐
│  scripts/                                                          │
│    score_portfolio  run_strategy  sync_positions  track_outcomes   │
│    monthly_dca  hardware_check  build_patterns  test_telegram      │
└──────────┬───────────────────────────────────────┬─────────────────┘
           │                                       │
   ┌───────┴────────┐                      ┌───────┴─────────┐
   │ src/trading    │                      │ src/jobs        │
   │  decision      │                      │  telegram_cb    │
   │  sizing        │                      │                 │
   └───────┬────────┘                      └───────┬─────────┘
           │                                       │
   ┌───────┴────────┐  ┌──────────────┐  ┌────────┴────────┐
   │ src/risk       │  │ src/alerts   │  │ src/learning    │
   │  limits        │  │  notifier    │  │  pattern_miner  │
   │                │  │  dispatch    │  │                 │
   └───────┬────────┘  └──────┬───────┘  └────────┬────────┘
           │                  │                   │
   ┌───────┴──────────────────┴───────────────────┴─────────┐
   │  src/broker                                             │
   │    base (Adapter)  mock (Sim)  alpaca (Paper)           │
   └────────────────────────┬────────────────────────────────┘
                            │
   ┌────────────────────────┴────────────────────────────────┐
   │  src/common                                              │
   │    storage (5 DBs)   config   data_loader   fx           │
   │    predictions  outcomes  cost_caps  llm                 │
   │    json_utils  retry                                     │
   └──────────────────────────────────────────────────────────┘
```

---

## systemd-Timer Inventory

| Timer | Schedule | Was |
|---|---|---|
| invest-pi-score | stündlich :30 | Risk-Scoring fuer alle 43 Tickers |
| invest-pi-strategy | Mo-Fr 16:00 | run_strategy, evtl. Paper-Trades |
| invest-pi-sync | stündlich :35 | Broker→DB Sync + Equity-Snapshot |
| invest-pi-outcomes | täglich 02:30 | Outcome-Tracker + Drift-Detection |
| invest-pi-auto-pull | alle 2 Min | git pull + smoke + auto-rollback |
| invest-pi-status-push | alle 2 Min | _status/snapshot.json schreiben + push |
| invest-pi-hardware | alle 30 Min | CPU/Disk/Mem-Check + Telegram-Push |
| invest-pi-telegram-callbacks | alle 60s | getUpdates poll + feedback-logging |
| invest-pi-patterns | monatlich am 1. 03:00 | Pattern-Library refresh |
| invest-pi-monthly-dca | monatlich am 1. 14:00 | Sonnet-DCA-Empfehlung an Mert |

10 Timer total.

---

## Was Mert tun muss damit die NEUEN Timer aktiv sind

Auf dem Pi:

```bash
sudo systemctl daemon-reload
for s in hardware patterns telegram-callbacks monthly-dca; do
  sudo systemctl enable --now invest-pi-$s.timer
done
```

Plus optional fuer Anthropic-Calls:
```bash
sudo -u investpi nano /home/investpi/invest-pi/.env
# ANTHROPIC_API_KEY=sk-ant-...

# Anthropic SDK installieren falls noch nicht da
sudo -u investpi pip install --break-system-packages --user anthropic
```

---

## Schutz-Mechanismen (zusammengefasst)

- **Kein Echtgeld-Trading.** AlpacaPaperBroker fest auf paper-api.alpaca.markets
- **Kill-Switch.** `touch data/.KILL` blockiert alle Trades
- **Cost-Cap Hard-Stop bei 50€/Monat.** + Cost-Awareness ab 70%
- **max_trades_per_day=3** + **stop_loss_pct=15%** + **max_daily_loss_pct=5%**
- **Marktoeffnung-Check.** Keine Orders ausserhalb 15:30-22:00 CET Mo-Fr
- **alert_level >= 2 → IMMER skip.**
- **Hardware-Cooldown.** Pi-Health-Alerts max 1×/h pro Metrik
- **Telegram-Spam-Schutz.** notifications-Tabelle de-dupliziert per prediction_id
- **Auto-Rollback.** Push der den Smoke-Test bricht wird vom Pi automatisch zurueckgerollt
- **Update-Offset.** Telegram-Updates werden nicht doppelt processed (.telegram_offset file)
