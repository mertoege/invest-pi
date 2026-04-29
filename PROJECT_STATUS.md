# Invest-Pi · Project Status

**Stand:** 2026-04-29 nach finaler Optimierungs-Session
**Mission:** Autonomes Self-Learning-Investment-System auf Pi 5 mit Mid-Term-Trading auf Alpaca-Demo, monatlicher DCA-Empfehlung an Mert, kontinuierlicher Selbstoptimierung via Opus-Meta-Review.

---

## Was komplett live ist

| Phase | Status | Module |
|---|---|---|
| **Phase 0** Foundation | ✅ | storage (5 DBs), json_utils, retry, predictions, cost_caps, fx |
| **Phase 1** Self-Learning Loop | ✅ | risk_scorer (mit prediction-logging), outcomes, track_outcomes |
| **Phase 2** Paper-Trading | ✅ | broker (mock+alpaca), trading (decision+sizing), risk/limits, run_strategy |
| **Phase 3a** Telegram-Notifier | ✅ | notifier.py + dispatch.py (auto-push bei Stufe-2) |
| **Phase 3b** Callback-Handler | ✅ | telegram_callbacks.py (1-min poll, fb:/fbr:/dca: patterns) |
| **Phase 4** Meta-Review (Skelett) | ✅ Code ready, aktiviert nach 30+ Outcomes | meta_review.py + calibration.py |
| **Phase 5** Pi-Operations | ✅ | auto_pull, status_push, hardware_check, backup, error_alerts |
| **Anthropic-Wrapper** | ✅ | llm.py mit call_sonnet/opus/haiku, Cost-Tracking, prediction-Log |
| **monthly_dca** | ✅ + Calibration-injected | scripts/monthly_dca.py |
| **Moderate-Strategy** | ✅ | composite<45 + alert<=1 + Take-Profit + Trailing-Stop |
| **USD-Tracking** | ✅ | EUR/USD beide in equity_snapshots, daily_loss FX-resistent |
| **Tests-Coverage** | ✅ 21 Tests / 5 Module | smoke + trading + outcomes + calibration + decision_modes |

## Was noch offen ist

| Aufgabe | Wann sinnvoll |
|---|---|
| **Phase 4 erste Live-Aktivierung** | nach ~7-14 Tagen wenn 30+ T+7d-outcomes da sind |
| **FastAPI Dashboard** (optional) | niedrige Prio |
| **IBKR-Adapter** | nur wenn Echtgeld-Wechsel aussteht |
| **A/B-Testing-Layer** | wenn moderate-v1 vs eine v2 verglichen werden soll |

---

## Trade-Logik (aktuell: Mode "moderate")

**Wann gekauft:** composite<45 UND alert_level<=1 UND triggered_dims<=2 UND Ring 1 oder 2 UND Position nicht offen UND <8 open positions UND Cash>=25EUR

**Wann verkauft:**
1. Take-Profit: Position +20% über avg_price
2. Trailing-Stop: ab +12% Buchgewinn → Sell wenn -8% vom Hoch
3. Stop-Loss: Position -10% unter avg_price
4. Max-Daily-Loss: Equity -5% vom Tageshoch (USD-basiert) → kein neuer Buy

**Frequenz:** ~15-30 Trades/Monat erwartet (vs ~5-10 bei conservative)

---

## systemd-Timer Inventory (12 Timer)

| Timer | Schedule | Was |
|---|---|---|
| invest-pi-score | stündlich :30 | Risk-Scoring 43 Tickers |
| invest-pi-strategy | Mo-Fr 16:00 | run_strategy mit 4 Sell-Pässen + Buy-Pass |
| invest-pi-sync | stündlich :35 | Broker→DB Sync + Equity (EUR+USD) + peak_price |
| invest-pi-outcomes | täglich 02:30 | Outcome-Tracker (T+1d/7d/30d) |
| invest-pi-auto-pull | alle 2 Min | Push-to-Deploy + systemd-sync + smoke + auto-rollback |
| invest-pi-status-push | alle 2 Min | _status/snapshot.json mit USD+EUR+FX |
| invest-pi-hardware | alle 30 Min | CPU/Disk/Mem-Alerts |
| invest-pi-telegram-callbacks | alle 60s | Inline-Button-Klicks → feedback_reasons |
| invest-pi-patterns | monatlich am 1. 03:00 | Pattern-Library Refresh |
| invest-pi-monthly-dca | monatlich am 1. 14:00 | Sonnet-DCA-Empfehlung |
| invest-pi-meta-review | monatlich am 2. 04:00 | Opus-Reflexion → Action-Plan |
| invest-pi-backup | täglich 03:30 | sqlite3 .backup → gzip → Rotation 14d (+ optional restic) |

Plus: `invest-pi-error-alert@.service` als Template-Unit für OnFailure-Hooks bei *allen* obigen Timern.

---

## Was Mert tun muss damit die NEUEN Timer aktiv sind

Auto_pull synct Files automatisch nach `/etc/systemd/system/`, aber **enable** muss manuell:

```bash
ssh pi@100.92.115.43

sudo systemctl daemon-reload
for s in meta-review backup; do
  sudo systemctl enable --now invest-pi-$s.timer
done

# Verify (sollte 12 Timer zeigen)
systemctl list-timers 'invest-pi-*' --no-pager
```

Optional für Cloud-Backup (nur wenn gewünscht):
```bash
sudo apt-get install -y restic
sudo -u investpi nano /home/investpi/invest-pi/.env
# RESTIC_REPOSITORY=b2:bucket-name:invest-pi
# RESTIC_PASSWORD=<dein-encryption-passwort>
# B2_ACCOUNT_ID + B2_ACCOUNT_KEY auch
sudo -u investpi -H bash -c 'cd /home/investpi/invest-pi && restic init'
```

---

## Kosten-Bilanz

| Komponente | EUR/Monat |
|---|---|
| Alpaca Paper, yfinance, Telegram, GitHub | 0 |
| Sonnet (monthly_dca, ~5k tokens/call, 1×/Monat) | 0,03 |
| Opus (meta_review, ~20k tokens/call, 1×/Monat × 2 sources) | ~5-8 |
| **Gesamt** | **~5-8** |

Hard-Cap bei 50€/Monat. ~85% Puffer für künftige Erweiterungen (z.B. Sonnet-augmented Score-Scoring).

---

## Self-Learning-Loop (geschlossen)

```
score_portfolio (heuristic)              Telegram-Buttons
  ↓ predictions(subject_id=ticker)         ↓ feedback_reasons
  ↓                                        ↓
outcomes (T+1d/7d/30d)                  ┌──┘
  ↓ outcome_correct                     │
  ↓                                     ↓
meta_review (Opus, monatlich) ←─────────┘
  ↓ summary_md + action_plan
  ↓
calibration_block ─┐
                   ↓ injected in alle nachfolgenden Sonnet/Opus-Prompts
  ↓
naechster monthly_dca + naechster meta_review ist klueger
```

Schleife geschlossen seit T39. Aktiviert wird sie automatisch wenn die ersten 30+ Outcomes gemessen wurden — frühestens in ~7 Tagen.

---

## Schutz-Mechanismen (final)

- AlpacaPaperBroker fest paper-only
- Kill-Switch via `data/.KILL`
- Cost-Cap Hard-Stop 50€/Monat
- max_trades_per_day=5 (moderate)
- stop_loss/take_profit/trailing_stop alle aktiv
- max_daily_loss FX-resistent (USD-basis)
- Marktöffnungs-Check
- alert>=2 → IMMER skip
- Hardware-Cooldown
- OnFailure-Telegram-Push bei Service-Crashes
- Auto-Rollback bei Smoke-Test-Fail nach Pull
- Update-Offset-File gegen Doppel-Process von Telegram-Callbacks
- Daily DB-Backup gzipped, 14d retention
- Optional restic + B2 für Off-Site Backup
