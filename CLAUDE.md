# CLAUDE.md — Invest-Pi

> Kontext-Datei für Claude Code. Wird automatisch gelesen wenn du in dieses Verzeichnis wechselst.

## Mission
Autonomes, 100% KI-gesteuertes Investment-System auf Raspberry Pi 5.
Ziel: Maximale jaehrliche Rendite bei stabilem System. Kein ETF-Ersatz (6-7%),
sondern aktive Alpha-Generierung. Alle Entscheidungen trifft die KI autonom.
Aktuell Paper-Trading via Alpaca, perspektivisch Echtgeld-Nebeneinkommen.

## Owner
Mert Oege · mert.oege@gmail.com · GitHub: mertoege/invest-pi

## Architektur
- **Kein Docker** — Native Python 3.11, User , 16 systemd Timers
- **Pfad:** 
- **Config:**  — einzige Wahrheitsquelle für Portfolio + Universe + Settings
- **Secrets:**  (Alpaca Keys, Telegram Token, FRED API Key)
- **Daten:**  (SQLite DBs, JSON-Caches)
- **Logs:** 

## Module (src/)
| Modul | Zweck |
|-------|-------|
|  | Decision Engine, Position Sizing |
|  | Kill-Switch, Stop-Loss, Take-Profit, Trailing, Cash-Floor, Sector-Cap, Correlation, Daily-Loss |
|  | Pattern Miner, Calibration, Attribution, Reflection, HMM Regime, Config Patcher, Weight Optimizer, Backtest Engine |
|  | Telegram Notifier, Dispatch, Risk Scorer, FRED Signals, Breadth, Sentiment, Earnings |
|  | Base Adapter, Mock Sim, Alpaca Paper |
|  | Shared Utilities, DB Access |
|  | Telegram Callback Handler |

## Scripts (scripts/)
Einstiegspunkte für die systemd Timers:
, , , ,
, , , ,
, , , 

## Systemd Timers (17 Stueck)
| Timer | Intervall |
|-------|-----------|
| invest-pi-auto-pull | alle 2 Min |
| invest-pi-status-push | alle 2 Min |
| invest-pi-score | alle 30s |
| invest-pi-data-refresh | alle 30 Min |
| invest-pi-signals | alle 30 Min |
| invest-pi-learning | alle 20 Min |
| invest-pi-weekly-rotation | woechentlich (Sa) |
| invest-pi-daily-report | taeglich 18:00 |
| invest-pi-meta-review | So 10:00 |
| invest-pi-strategy-open | Mo-Fr 15:35 (Market Open) |
| invest-pi-strategy | Mo-Fr 18:00 (Midday) |
| invest-pi-rebalance | Mo-Fr 21:30 (Market Close, sell-only) |
| invest-pi-monthly-dca | monatlich |
| invest-pi-dca-watchdog | monatlich |
| invest-pi-weekly-mini-review | woechentlich |
| invest-pi-outcome-tracker | 1x/Tag |
| invest-pi-db-maintenance | taeglich 03:30 |

## Deployment
- **auto_pull** Timer holt alle 2 Min von GitHub 
- Push zu GitHub = automatisches Deploy in <2 Min
- **Status-Push** committed  alle 2 Min nach GitHub

## Wichtige Gotchas
1. **NICHT PokéPi!** Dies ist ein separates Projekt. PokéPi liegt unter 
2. **Kein Docker hier** — alles läuft nativ als systemd Services unter User 
3. **config.yaml** ist die einzige Wahrheitsquelle für Portfolio-Daten
4. **auto_pull** überschreibt lokale Änderungen — immer erst pushen!
5. **Paper-Trading only** — kein Echtgeld, Alpaca Paper Account

## Tech Stack
- Python 3.11, SQLite, yfinance, fredapi
- Alpaca SDK (Paper), Telegram Bot API
- scikit-learn, hmmlearn (Regime Detection)
- Raspberry Pi 5, Tailscale VPN

## Sprache
Antworte auf Deutsch. Sei direkt und präzise. Mert vertraut autonomer Arbeit.

