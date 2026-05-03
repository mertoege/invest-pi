# CLAUDE.md — Invest-Pi

> Kontext-Datei für Claude Code. Wird automatisch gelesen wenn du in dieses Verzeichnis wechselst.

## Mission
Autonomes Self-Learning-Investment-System auf Raspberry Pi 5.
Ziel: Sharpe-Ratio-Maximierung mit kontrollierten Drawdowns.
Aktuell Paper-Trading via Alpaca, 50€/Monat DCA-Simulation.

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

## Systemd Timers (16 Stück)
| Timer | Intervall |
|-------|-----------|
|  | alle 2 Min |
|  | alle 2 Min |
|  | alle 30s |
|  | alle 30 Min |
|  | alle 30 Min |
|  | alle 20 Min |
|  | wöchentlich (Sa) |
|  | täglich 18:00 |
|  | So 10:00 |
|  | Mo-Fr |
|  | monatlich |
|  | monatlich |
|  | monatlich |
|  | wöchentlich |
|  | 1x/Tag |
|  | täglich 03:30 |

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

