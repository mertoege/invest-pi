# Code-Audit 2026-07-02 — Fable 5, Geld-Pfade

Vier parallele Fable-5-Subagenten haben die geldkritischen Pfade geprueft
(Risiko-Limits, Positionsgroesse+Entscheidung, Momentum-Rebalance, Broker+Sync).
Einige Funde wurden direkt auf dem Pi verifiziert. Auftrag: echte Korrektheits-/
Logikfehler, keine Stil-Funde.

WICHTIGE EINORDNUNG: Live handelt nur `momentum_rebalance.py` (via run_strategy
`run_due`). Die alte Score-Pipeline (`decision.py`, `sizing.py`, Buy-/Score-Paesse
in run_strategy, Grossteil `limits.py`) ist unter engine=momentum uebersprungen —
Funde dort sind real, aber NICHT live. Sortierung: LIVE > Echtgeld-relevant > Legacy.

## Behoben am 2026-07-02 (Commit dieses Audits)
- run_strategy.main(): Exit-Code wird durchgereicht (stiller Monats-Ausfall behoben).
- momentum _open_orders_block: fail-closed statt fail-open (Doppel-Order verhindert).
- alpaca.place_order: @api_retry entfernt (kein Doppel-Submit bei Timeout).
- alpaca._from_alpaca_order: 'canceled' -> 'cancelled' normalisiert (Storno-Sync).

## OFFEN — Entscheidung Mert (im Manifest als tasks:)
- Strategie-Divergenz: Live-Engine trimmt Gewinner nicht + Sanity-Filter/Kurs-Cache-Bug
  -> handelt andere Strategie als der validierte Backtest. Angleichen+neu backtesten
  ODER bewusst akzeptieren.
- Echtgeld-Gate: unter Momentum greifen nur Kill-Switch + -30%-Breaker; alle anderen
  Limits liegen im uebersprungenen Alt-Pfad. Vor Echtgeld festlegen, was gelten soll.

## OFFEN — Bugs (noch nicht gefixt, brauchen etwas mehr Sorgfalt)
### LIVE (Momentum-Pfad)
- momentum orders werden NICHT in `trades` protokolliert (kein Order-Audit-Trail);
  sync_orders laeuft fuer die aktive Strategie ins Leere. (broker/sync #6)
- Kurs-Cache mischt Adjustierungs-Basen: Split -> Ticker ~18 Monate stumm aus Ranking;
  Dividenden verzerren 6M-Momentum. Fix: 1x/Monat force_refresh. (momentum F2)
- _pick_top Cross-Check kann am Stichtag falsches Monatsziel fixieren (legit Gap/stale
  yf-Close); q.last in {0,None} wird ungeprueft genommen. (momentum F5)
- get_quote: bid=0 -> last=ask (Pre-Market-Ask verzerrt Sizing); Fallback nutzt bis zu
  tagealte Closes ohne echten Freshness-Check. (broker/sync #8)
- EUR-Einstandskurs wird bei jedem sync_positions mit Tages-FX ueberschrieben -> EUR-PnL
  im Tagesbericht driftet mit Wechselkurs. (broker/sync #5)
- Circuit-Breaker rechnet Drawdown in EUR (FX-verzerrt) + geht bei fehlenden Snapshots
  still auf 0 (deaktiviert). Fix: USD + peak=None alarmieren. (momentum F9)
- 21:30 "sell-only"-Rebalance ignoriert --skip-buys im Momentum-Zweig -> kauft. (F7)
- Teil-Fills bei Storno/Expiry: filled_qty/avg_fill_price verworfen. (broker/sync #3)
- Sofort-Fill speichert Quote-Preis statt echten avg_fill_price. (broker/sync #4)

### ECHTGELD-relevant (limits.py — aktuell umgangen, vor Live scharf zu schalten)
- Trailing-Stop-Totzone: Aktivierung prueft aktuellen statt Peak-Gewinn -> Peaks
  zwischen +act% und (1+act)/(1-ts)-1 loesen NIE aus. (limits F1)
- cash_floor_check prueft Ist-Cash, nicht Zustand nach dem Kauf -> Floor um eine
  Ordergroesse unterschreitbar. (limits F2)
- Partial-Take-Profit-Tiers: einmal pro Ticker fuer immer verbraucht, auch bei
  abgelehnter Order (Query ohne Status/Positions/Zeit-Filter). (limits F3)
- correlation_check: nur Durchschnitt blockt, Einzelkorrelation 1.0 passiert;
  Datenabruf-Fehler der gehaltenen Ticker -> fail-open. (limits F4/F5)
- daily_loss / drawdown: COALESCE(total_usd,total_eur) mischt Waehrungen ->
  ~8% Phantom-Bewegung, echter Verlust maskierbar; drawdown fail-open bei fehlendem
  total_usd. (limits F6/F7)
- market_price<=0 nimmt Position still aus ALLEN Schutzverkaeufen (Stop/TP/Trailing)
  -> Delisting/Symbol-Bug = unbegrenzt ungeschuetzt, kein Log. (limits F8)
- adaptive Stop-Loss weitet bis 1.8x ohne absoluten Deckel (lockerster Schutz im
  Crash). (limits F10)
- _position_strategy source hart "paper" -> beim Echtgeld-Umstieg falsche Labels/
  Schwellen fuer Live-Positionen. (limits F11)
- Marktzeiten in Berlin-Zeit statt US-DST -> Versatzwochen ~1h falsches Fenster,
  Halbtage fehlen. (limits F12)

### LEGACY (Score-Pipeline, aktuell no-op)
- decision.latest_risk_score: max_age_hours nie angewandt -> Entscheidungen auf
  beliebig alten Scores.
- sizing: max_position_eur-Cap nie bindend (bis ~4x moeglich); Cash-Check erlaubt
  >100% pro Order + Ordersumme ungegengerechnet; VaR-Limit rechnet mit Cash statt
  Portfolio; Kelly zaehlt Phantom-Wins.
- decision.triggered_ok=True hartkodiert (Trigger-Filter aus), Reason-Text luegt.
- run_strategy _buy_gate: check.ok statt check.allowed -> beide Buy-Paesse crashen
  still bei jedem Lauf (fail-closed, aber tot). (limits A1)
- pre_trade_check: Cost-Cap-Zweig toter Code. (limits F9)

Volle Details je Fund (Zeilennummern, Fix-Vorschlaege) siehe Chat-Transkript des
Audits vom 2026-07-02.
