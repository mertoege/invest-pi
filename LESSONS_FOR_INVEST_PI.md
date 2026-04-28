# Lessons aus PokéPi für Invest-Pi

**Stand:** 27. April 2026, nach einer ~284-Commit-Marathon-Session am PokéPi-Projekt.
**Adressat:** Mert + Claude in der nächsten Invest-Pi-Session.
**Zweck:** Damit Invest-Pi von Anfang an die Architektur-Entscheidungen, Bug-Patterns
und Self-Learning-Mechanismen mitbringt, für die wir bei PokéPi wochenlang im Trial-and-Error
gebraucht haben.

---

## TL;DR — die 7 wichtigsten Übertragungen

1. **Self-Learning-Loop ist nicht „nice to have", sondern die Architektur-Grundlage.**
   Outcome-Tracking + Meta-Review müssen *vom ersten Tag an* mitgedacht werden, nicht
   später draufgesetzt. Konkret: jede Risk-Score-Prediction braucht eine `subject_id`
   (= Ticker), die später per `outcome_tracker` mit dem realen Kursverlauf abgleichbar ist.

2. **Predictions in einer einzigen Tabelle, mit `prompt_hash` + `subject_id` durchgängig.**
   Eine `predictions`-Tabelle mit Standardspalten (id, created_at, job_source, model,
   prompt_hash, input_summary, output_json, confidence, subject_type, subject_id,
   input_tokens, output_tokens, cost_estimate_eur, outcome_json, outcome_correct).
   Schema kommt unten — copy-paste-fähig.

3. **Cost-Caps brauchen 3 Tiers (hourly/daily/monthly).** Ohne hard-cap kann ein
   buggy Loop dir 100€ in 2 Stunden verbrennen. Bei PokéPi war das Szenario nahe.

4. **JSON-Output von Claude *immer* von Markdown-Codefences befreien**, bevor du
   ihn speicherst. Sonst scheitert `json_extract` und der Outcome-Tracker findet
   nichts. Dieser Bug hat in PokéPi einen ganzen Tag gekostet.

5. **Telegram-Inline-Buttons (✅/❌/🤷) mit Reason-Folgefragen** sind der größte
   Lern-Hebel überhaupt. Verkürzt die Outcome-Frist von 7-30 Tagen auf 0 Sekunden.
   Bei Invest-Pi: bei jedem Risk-Alert Stufe ≥2 die Buttons.

6. **„Stubs sichtbar lassen"** (Invest-Pi-Doc-Phrase) ist der richtige Modus —
   aber nur wenn die Stubs in der Statistik nicht als „pending" für immer hängen.
   PokéPi hatte 200 alte Aggregate-Predictions die für immer als „pending" zählten.
   Lösung: bei nie-messbaren Quellen sofort `outcome_json="batch_aggregate"`
   markieren, `outcome_correct=NULL`.

7. **Status-Bus via Git-Push.** Pi schreibt alle 2 Min einen `_status/snapshot.json`
   ins Repo, Claude liest das von außen via `git pull`. Macht remote-Monitoring
   trivial ohne SSH/Tailscale-Login. Übertragbar 1:1 — ist eines der besten
   Patterns aus PokéPi.

---

## Architektur-Übertragungen — PokéPi → Invest-Pi

| PokéPi-Konzept | Invest-Pi-Äquivalent | Notiz |
|---|---|---|
| `alert_listing` (per-listing-prediction) | `daily_score` (per-ticker-per-day-score) | jede Score-Berechnung = 1 row mit subject_id=ticker |
| `alert` (Batch-call) | `score_batch` (alle Tickers in einem Run) | nur für cost-tracking, sofort batch_aggregate |
| `daily_inventory` (Inventar-Check) | `weekly_review` (Wochenanalyse je Position) | longer-form |
| `friday_report` (Verkaufsliste) | `monthly_dca` (DCA-Empfehlung) | strukturierter JSON-Block am Ende! |
| `weekly_report` (Opus-Tiefenanalyse) | `quarterly_outlook` (Markt-Phase-Bericht) | Quartal statt Woche, da langsamer |
| `meta_review` (monatliche Selbst-Reflexion) | `meta_review` (genau gleich) | unverändert übernehmen |
| `subject_id = matched_card_id` | `subject_id = ticker` | trivial-eindeutig |
| `visual_condition` (Foto-Bewertung) | *kein direktes Pendant* | aber: `macro_regime` + `sector_regime` als Multiplikatoren |
| `liquidity` (Cardmarket-Spread) | `market_cap_tier` + `avg_daily_volume` | Penny-Stock-Detection |
| `market_direction` (avg7 vs avg30) | bereits drin: `technical_breakdown` Dimension | |
| `bundle_filter` (Sammelpaket-Drops) | *nicht nötig* | yfinance liefert single-tickers |
| TCG-API + Cardmarket-Preise | yfinance + Finnhub + NewsAPI | gleiche Struktur: external API mit lokalem Cache |
| `seen_listings`-Dedup | nicht nötig (yfinance ist deterministisch) | |
| Cost-Caps Anthropic | Cost-Caps Anthropic + Finnhub-Tier-Tracking | Finnhub Free hat Limit/min |
| Telegram ✅ Gekauft → Auto-Inventar | Telegram ✅ DCA-Vorschlag → buy.py auto-aufrufen | mit confirmation-button |

---

## Das vollständige Self-Learning-Loop-Diagram

So sieht der Loop in PokéPi heute aus, **adaptiert für Invest-Pi**:

```
┌──────────────────────────────────────────────────────────────────┐
│  MARKET DATA                                                     │
│   yfinance (prices) + Finnhub (insider/analyst) + NewsAPI (news) │
│   → market.db (cached, OHLCV, fundamentals)                      │
└──────┬───────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────┐
│  SCORING (stündlich/daily, Claude Sonnet pro Position)           │
│   risk_scorer.score_ticker() → 9-Dim-Score + Composite           │
│   ↓                                                               │
│   predictions (DB): job_source='daily_score', subject_id=ticker, │
│                     output_json={dimensions, composite, alert},  │
│                     prompt_hash=<sha256 system-prompt>           │
└──────┬───────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────┐
│  ALERT-DELIVERY (wenn alert_level >= 2)                          │
│   Telegram mit Inline-Buttons:                                   │
│     ✅ "Habe DCA gemacht" / ❌ "False Positive" / 🤷 "Egal"      │
│     ↓                                                             │
│   Bei ❌ Folgefrage:                                              │
│     🟡 Macro / 💸 Sector / 🕵 Insider / 📰 News / ✏️ andere      │
└──────┬───────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────┐
│  OUTCOME-TRACKING (täglich 02:00, ab T+7 messbar)                │
│   pro daily_score-prediction:                                    │
│     - actual_close_after_7d                                      │
│     - actual_close_after_30d                                     │
│     - max_drawdown_in_window                                     │
│     - alert_level=2 → korrekt wenn drawdown >5% in 7d            │
│     - alert_level=0 → korrekt wenn keine drawdown >5% in 7d     │
│   PLUS: User-Feedback (✅/❌/🤷) als sofortige ground-truth      │
└──────┬───────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────┐
│  META-REVIEW (monatlich, Claude Opus)                            │
│   Input pro Quelle (daily_score, weekly_review, monthly_dca):    │
│     - 30+ outcome'd predictions                                  │
│     - Konfidenz-Stratifizierung                                  │
│     - Sektor-/Ring-Stratifizierung                               │
│     - User-Feedback-Patterns                                     │
│     - Drift-Detection (7d vs prior 7d hit-rate)                  │
│   Output: reviews/<date>-<source>.md mit ACTION-PLAN Prio 1/2/3  │
└──────┬───────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────┐
│  PROMPT-FEEDBACK (geschlossener Loop)                            │
│   load_meta_reviews() injected die letzten Reviews + aktuelle    │
│   30d-Hit-Rate + User-Feedback-Patterns in den NÄCHSTEN          │
│   Sonnet-Prompt. Damit kalibriert sich das System ohne           │
│   Code-Änderung über Wochen.                                     │
└──────────────────────────────────────────────────────────────────┘
```

**Zentrale Entdeckung aus PokéPi:** *ohne* den Pfad Outcomes → Meta-Review →
zurück-in-den-Prompt ist das System statisch, egal wie smart der initiale Prompt
ist. Der Loop muss vom ersten Tag an gebaut werden, sonst wird er nie nachgerüstet.

---

## Predictions-Tabelle — copy-paste-fähig

```python
# Aus PokéPi gelernte Standard-Form:
CREATE TABLE IF NOT EXISTS predictions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    job_source            TEXT    NOT NULL,    -- 'daily_score' / 'monthly_dca' / 'meta_review'
    model                 TEXT    NOT NULL,    -- 'claude-sonnet-4-6' etc.
    prompt_hash           TEXT,                -- sha256 vom System-Prompt (für A/B-Test!)
    input_hash            TEXT,                -- sha256 vom Input-Payload (Dedup)
    input_summary         TEXT,                -- 'NVDA, 9 dims, 14d basis'
    output_json           TEXT,                -- Roh-JSON, Markdown-strip applied
    confidence            TEXT,                -- 'high' | 'medium' | 'low'
    subject_type          TEXT,                -- 'ticker' / 'portfolio' / 'sector'
    subject_id            TEXT,                -- 'NVDA' / 'tech-sector' / etc.
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cost_estimate_eur     REAL,
    outcome_json          TEXT,                -- gefüllt vom outcome_tracker
    outcome_measured_at   TEXT,
    outcome_correct       INTEGER              -- 1 / 0 / NULL (NULL = unmessbar)
);
CREATE INDEX idx_pred_job_date ON predictions(job_source, created_at);
CREATE INDEX idx_pred_subject  ON predictions(subject_type, subject_id);
```

**Was PokéPi falsch gemacht hat und was der Schmerz war:**

- **`subject_id` zuerst NICHT durchgängig gesetzt** — alle alert-Predictions waren
  Batch-Calls ohne subject_id, also für outcome-tracking unbrauchbar. Erst nach
  ~24h und 200 toten Predictions habe ich das entdeckt.
  → **Bei Invest-Pi: jede einzelne Score-Berechnung als eigene prediction-Row,
  mit subject_id=ticker.** Auch wenn sie aus einem Batch-Sonnet-Call kommt.

- **`output_json` ohne Markdown-Strip** — Claude wickelt JSON oft in
  ` ```json ... ``` ` Blocks. Ohne Strip funktioniert kein json_extract.
  ```python
  def strip_codefence(text: str) -> str:
      m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$",
                   text.strip(), re.DOTALL | re.IGNORECASE)
      return m.group(1).strip() if m else text
  ```

- **`prompt_hash` nicht gesetzt** = A/B-Tests unmöglich. Set ihn IMMER auf
  `sha256(system_prompt)[:16]`.

---

## Bug-Stories aus PokéPi — Defensive Patterns

### Bug 1: Outcome-Tracker für immer pending
**Symptom:** `learning_loop = {alert: total: 200, measured: 0, pending: 200}` —
für Wochen.
**Ursache:** Aggregat-Predictions (Batch-Calls über mehrere Listings) hatten
keinen sinnvollen Outcome, der Tracker konnte sie nie messen, sie hingen für
immer in „pending".
**Fix:** Sofortige `batch_aggregate`-Markierung mit `outcome_correct=NULL`:
```python
def _outcome_for_batch_aggregate(pred):
    return {"type": "batch_aggregate",
            "reason": "Cost-Tracking, Outcomes über child-rows"}, None
```
**Für Invest-Pi:** Wenn du je einen Sonnet-Call hast der mehrere Tickers
gleichzeitig bewertet, mach 1 Row pro Ticker als child mit subject_id, 1 Row
für den Batch-Call mit `cost_estimate_eur` (sofort batch_aggregate).

### Bug 2: Calendar-Day vs rolling 24h
**Symptom:** Tagesbudget bei 2,11€/2€ angeblich überschritten, obwohl heute
nichts gelaufen war.
**Ursache:** Counter `WHERE created_at >= datetime('now','-1 day')` ist rolling
24h, nicht calendar-day. Predictions vom gestern-Abend zählten heute morgen
noch mit.
**Fix:** `date(created_at) = date('now','localtime')`.
**Für Invest-Pi:** auch hier — Cost-Cap-Logik IMMER calendar-day, nicht rolling.

### Bug 3: Inverted Confidence
**Symptom:** Konfidenz-Kalibrierung im Meta-Review zeigte verkehrte Trends.
**Ursache:** Ich speicherte `confidence=verdict.get("seller_risk")` direkt.
seller_risk='low' bedeutet aber „Verkäufer ist sicher" = confidence='high',
nicht 'low'.
**Fix:** explizite Map-Invertierung: `{"low":"high","medium":"medium","high":"low"}`.
**Für Invest-Pi:** Bei jedem Mapping zwischen externem Signal und confidence
EXPLIZIT prüfen welches Vorzeichen — nie 1:1 übernehmen.

### Bug 4: Snapshot-Quelle ignorierte Tracking-Subjects
**Symptom:** outcome_tracker rief `get_latest_snapshot(ticker)` auf, bekam aber
nichts weil `price_snapshot.py` nur Inventar+Watchlist trackte, nicht die
gescannten Listing-Karten.
**Fix:** `_get_unique_cards()` zieht zusätzlich alle distinct subject_ids aus
predictions WHERE job_source='alert_listing' der letzten 30d.
**Für Invest-Pi:** Jeder Ticker für den jemals eine Prediction läuft, MUSS in
der Daily-Snapshot-Schleife landen. Sonst kann der Outcome-Tracker nichts
messen. Pragmatic: in `score_portfolio.py` *vor* dem Scoren einen
yfinance-Snapshot ziehen für *alle* Tickers in `predictions`-Tabelle der
letzten 30d, nicht nur für Portfolio+Watchlist.

### Bug 5: Telegram Markdown vs HTML
**Symptom:** HTTP 400 von Telegram-API bei Nachrichten mit Underscore.
**Ursache:** `parse_mode="Markdown"` interpretiert `_` als Italic-Marker und
crashed bei Unbalanced.
**Fix:** *Immer* `parse_mode="HTML"` benutzen, nie Markdown. HTML ist robuster.
**Für Invest-Pi:** Same.

### Bug 6: Container-Crash-Loop nach Import-Fehler
**Symptom:** Backend-Container restartet im 5-Sekunden-Loop, ich hatte 502 für
40 Minuten.
**Ursache:** Ein neuer Endpoint nutzte `PlainTextResponse` aber Import war
nicht da → Modul-Import scheiterte → uvicorn crashed → Docker restartet endless.
**Fix:** `bash -n` (für shell) bzw. `python -m py_compile` (für Python) nach
JEDER File-Änderung BEVOR push. Zusätzlich: in CI auf dem Pi einen `pre-pull
hook` der den neuen Container kurz lokal startet und Health-Check macht
(haben wir nicht eingebaut, wäre nächster Schritt).
**Für Invest-Pi:** auf dem Pi5 hat `auto_pull.sh` schon einen 90s-Health-Check
+ Auto-Rollback. Bei Invest-Pi sollte das gleiche Pattern her — gibt es ja
schon mit systemd, aber nicht als auto-rollback. Empfehlung: Skript das vor
`docker compose up` einen container-test macht.

### Bug 7: yfinance/External-API ohne Retries
PokéPi hatte zuerst `httpx.get(...)` ohne Retry → 1 Netzwerk-Hicks =
ganze Pipeline-Iteration verloren.
**Fix:** tenacity-Retries:
```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3),
       wait=wait_exponential(multiplier=1, min=2, max=10))
def get_prices(ticker, ...):
    ...
```
**Für Invest-Pi:** Genauso für yfinance, Finnhub und NewsAPI.

### Bug 8: Frontend-Cache vergiftet PWA
**Symptom:** Code-Änderung gepusht, Pi rebuilded, Mert sieht im Browser
weiter den alten Stand für Stunden.
**Ursache:** nginx hatte für `index.html` keinen `no-cache`-Header. Browser
holte das alte HTML aus dem Disk-Cache, das verwies auf alte
`/assets/<hash>.js`-Files.
**Fix:** `location = /index.html { add_header Cache-Control "no-cache"; ... }`.
Plus: PWA-Service-Worker mit `cache: 'pokepi-v<n>'` und Version bei jedem
Breaking-Change hochzählen.
**Für Invest-Pi:** Wenn FastAPI Dashboard kommt — gleiche nginx-Config, oder
Cache-Header direkt im FastAPI-Response.

### Bug 9: Sheet-Layout abgeschnitten
**Symptom:** Filter-Sheet/Detail-Sheet Buttons schwebten mittig im Screen,
darunter leerer Raum, BottomNav unten — verwirrend.
**Ursache:** Mit `display: flex` ohne explizite Container-Höhe wird
`flex: 1` nicht sinnvoll — das Element streckt nicht auf den Container.
Fix: Sheet als `position: fixed; bottom: 0` mit `max-height` plus
3-Slot-flex-column (Header / Body=`overflow:auto` / Footer-Slot).
**Für Invest-Pi:** Wenn FastAPI/React/Streamlit-Dashboard kommt — beim ersten
Bottom-Sheet/Modal-Pattern an dieses Layout denken.

---

## Cost-Management — die 3 Tiers

PokéPi hat:
- **hourly_anthropic_budget_eur: 0.50€** — Schutz gegen Listing-Boom innerhalb 1h
- **daily_anthropic_budget_eur: 2.00€** (Mo-Fr) / 5.00€ (Wochenende) — calendar-day
- **monthly_anthropic_budget_eur: 50.00€** — hard stop, Telegram-Warnung bei Erreichen

Plus **Cost-Awareness im Prompt** ab 70% des Tagesbudgets:
> „⚠️ KOSTEN-MODUS: Tagesbudget zu 75% verbraucht. Nur HOCH-Konfidenz
> KAUFEN, im Zweifel SKIP."

→ Sonnet wird nicht abgeschaltet, sondern strenger.

**Übertragung Invest-Pi:**

```yaml
# config.yaml ergänzen:
settings:
  api_costs:
    hourly_eur:    0.10        # 24/7-Scoring → niedriger
    daily_eur:     1.00
    monthly_eur:   25.00       # primär opus-monthly + sonnet-daily-cycles

  finnhub_rate_limit:
    free_tier_per_min:   60    # Finnhub Free
    free_tier_per_day:   1000  # + 5 calls/min limit für andere endpoints

  newsapi_rate_limit:
    free_tier_per_day:   1000  # /everything endpoint
```

**Wichtig:** Finnhub und NewsAPI haben Rate-Limits *pro Sekunde/Minute*, nicht
nur in EUR. Das ist eine andere Kategorie — du brauchst einen `last_call_ts`-
Tracker pro API. PokéPi hatte das nicht weil TCG-API-Free-Tier großzügig ist.

---

## Workflow-Patterns — copy-paste-würdig

### A) Telegram-Inline-Buttons mit Reason-Folgefrage

PokéPi-Pattern:

```python
# Beim KAUFEN-Alert:
reply_markup = {"inline_keyboard": [[
    {"text": "✅ Gekauft",      "callback_data": f"fb:{prediction_id}:bought"},
    {"text": "❌ War schlecht", "callback_data": f"fb:{prediction_id}:bad"},
    {"text": "🤷 Egal",          "callback_data": f"fb:{prediction_id}:meh"},
]]}

# Bei ❌-Klick: editMessageReplyMarkup tauscht die Buttons gegen Reason-Grid:
[[{"text": "🟡 Kondition", "callback_data": f"fbr:{pred_id}:cond"}, ...]]
```

Cron-Job (jede Minute) polled `getUpdates`, mappt callback_data auf Prediction-Row,
schreibt outcome direkt + speichert reason in `feedback_reasons`-Tabelle.

**Für Invest-Pi:**
- Stufe-2-Alert (Caution): Buttons „📉 stimmt, hab verkauft" / „🤷 ignoriere" / „❌ false positive"
- Stufe-3-Alert (Red): Buttons „🚪 Verkauf jetzt eingeleitet" / „⏸ halte trotzdem" / „❌ false positive"
- bei ❌: Reason-Folgefrage „🌐 Macro / 💸 Sector / 🕵 Insider war veraltet / 📰 News-Sentiment falsch / ✏️ andere"

Reasons fließen mit der Zeit ins Sonnet-Prompt zurück: *„Mert hat in den letzten
30d 6 Stufe-2-Alerts als false positive markiert, davon 4× wegen Macro-Lärm —
sei vorsichtiger mit Macro-Triggern bei niedrigem VIX."*

### B) Auto-Action bei ✅
PokéPi: ✅ Gekauft → Karte landet automatisch im Inventar mit Listing-Preis als
purchase_price.

**Für Invest-Pi:** ✅ DCA-gemacht → `buy.py {ticker} {amount}` wird automatisch
aufgerufen, Position-Update in config.yaml. Mit confirmation-button vorher
(„soll ich das einbuchen?"), weil Investments mit echtem Geld höhere Konsequenzen
haben als Karten-Käufe.

### C) Lösch-Grund-Sheet
Wenn der User einen Alert löscht → Modal mit 6 Reason-Buttons + Freitext.
Reason landet in `feedback_reasons`, fließt in den nächsten Sonnet-Prompt:

```
## User-Feedback-Muster (letzte 30d, abgelehnt)
Telegram-❌: 4 Alerts markiert.
- 3x bei Sektor=Semiconductor
App-Löschungen: 8 Alerts mit Begründung verworfen.
- 4x: Macro-Lärm
- 2x: News war veraltet
Beispiele:
  • NVDA Stufe-2 bei VIX-Spike — verschwand nach 24h
  • ASML Stufe-3 nach Quartalsbericht — overreaction

Bei ähnlichen Patterns vorsichtiger sein.
```

### D) Bulk-Actions
Mehrere Alerts mit gleichem Reason auf einmal weg-learnen. Sehr wertvoll wenn
das System eine Iteration lang ein konsistentes Falsch-Muster hat.

---

## Pi-Operations — copy-paste-Grundlage

### Status-Bus via Git

PokéPi-Pattern: Pi schreibt alle 2 Min via systemd-Timer ein
`_status/snapshot.json` ins Git-Repo, pusht. Claude (von beliebigem Ort) liest
es via `git pull origin main && cat _status/snapshot.json`.

**Vorteile:**
- Kein SSH/Tailscale-Login nötig
- Versionierte History (jeder snapshot ist ein commit)
- Funktioniert über jede Firewall
- Audit-Trail kostenlos

**Übertragung 1:1 für Invest-Pi.** Was rein soll:
- Container-Status
- Letzte Score-Run (welche Ticker, wann, welcher composite)
- DB-counts (predictions total/measured/correct/incorrect pro source)
- Cost (heute/monat) gegen budget
- Letzte Outcomes
- yfinance/Finnhub/NewsAPI Health (last successful call)
- Pi-Hardware (CPU temp, disk, mem)

### Auto-Pull mit Rollback-Schutz

PokéPi hat `scripts/auto_pull.sh` der:
1. Alle 2 Min `git fetch`
2. Bei Diff: `git pull` + `docker compose up -d --build` für betroffene Services
3. Wartet 90s auf Backend-Healthcheck
4. Bei Fail: `git reset HEAD~1 && rebuild` + Telegram-Warnung

→ Push-to-Deploy ohne SSH. **Übertragung 1:1.**

### Wie Claude (in einer Cowork/Sandbox-Session) mit dem Pi spricht

Das ist der wichtigste Workflow-Trick aus PokéPi und der Grund warum
Push-to-Deploy + Status-Bus überhaupt so wertvoll sind.

**Claude hat KEINEN direkten Netzwerk-Zugang zum Pi.** Aus der Cowork-Sandbox
gibt es weder SSH, noch Tailscale, noch HTTP zum Pi. Was es gibt:

1. **GitHub als Vermittler.** Pi und Claude treffen sich nur über das
   gemeinsame Git-Repo.
2. **Pi-Mount im Cowork-Workspace.** Mert hat den Pi-Repo-Ordner auf seinem
   Windows-PC liegen (`C:\Users\merto\PiManager\Pokepi\`), Cowork hängt das
   in die Sandbox als `/sessions/.../mnt/Pokepi/` ein. Claude kann Files
   lesen — aber Git-Operationen darin sind unzuverlässig (siehe Quirks unten).

**Der zuverlässige Workflow ist:**

```bash
# 1. Token-URL aus dem gemounteten .git/config greifen
REMOTE=$(grep -oE 'https://[^ ]+' /sessions/.../mnt/Pokepi/.git/config | head -1)

# 2. Frisch in /tmp clonen (echtes Linux-FS, keine Mount-Quirks)
cd /tmp && git clone --depth 50 --branch main --quiet "$REMOTE" pokepi_work
cd /tmp/pokepi_work

# 3. CRLF + skip-worktree-Voodoo abschalten — sonst Renormalize-Phantome
git config core.autocrlf false
echo "* binary" > .git/info/attributes
git update-index --skip-worktree backend/services/price_history.py 2>/dev/null

# 4. Identität setzen
git config user.email "mert.oege@gmail.com"
git config user.name "Mert Oege"

# 5. Edits machen, committen, pushen — alles im /tmp-Clone
# (NICHT im /sessions/.../mnt/-Mount, da scheitern Git-Ops an Permissions)

# 6. Status vom Pi lesen via fetch + show (kein checkout nötig)
git fetch --quiet
git show origin/main:_status/snapshot.json | python3 -m json.tool
```

**Time-Lag pro Iteration:**
- Claude pusht: t=0
- Pi auto_pull-Tick: t≤120s
- Pi rebuild + healthcheck: t+30-90s
- Pi status_push-Tick mit neuem Commit als Beweis: t≤180s

→ **Eine Code-Änderung ist ~3-4 Min nach Push live messbar.** Plan deinen
Workflow danach: Push, dann ~120s warten, dann `git show origin/main:_status/snapshot.json`
um zu sehen ob der Pi die neuen Container hochgezogen hat.

**Quirks die du im Hinterkopf haben musst (alles aus PokéPi-Schmerz gelernt):**

1. **Cowork-Mount-Filesystem hat unzuverlässige IO für Git.**
   `git checkout`, `rm` und `git rebase` scheitern oft mit
   `Operation not permitted` auf der Mount-Seite. Im /tmp-Clone passiert
   das nie. → Immer in /tmp arbeiten, nie direkt im Mount.

2. **CRLF-Renormalization auf Windows-Repos.** Wenn Mert auf Windows
   committet hat, und du auf Linux pullst, normalisiert Git die Endings —
   plötzlich sind Files als „modified" markiert ohne dass du was geändert
   hast. Lösung: `git config core.autocrlf false` + skip-worktree für
   Files die wiederholt phantom-modified erscheinen
   (price_history.py, scraper.py waren in PokéPi die Übeltäter).

3. **Edit-Tool sync auf Mount ist verzögert.** Wenn du `Edit` oder `Write`
   auf eine Datei im Mount machst, kann es 30+ Sekunden dauern bis bash
   die neue Version sieht. Außerdem kann die Datei truncated landen wenn
   die Edits groß sind. → Immer per bash heredoc schreiben oder direkt
   in /tmp arbeiten und ans Mount via `cp` rüberkopieren.

4. **PowerShell aliased `curl`** auf Invoke-WebRequest — das nimmt Headers
   nicht als Strings sondern als Dictionary. Mert ist auf Windows; wenn
   du ihm curl-Beispiele gibst, immer **`curl.exe`** schreiben oder
   **`Invoke-RestMethod -Headers @{...}`** statt Bash-Style-curl.

5. **Pi pusht ständig (alle 2 Min) Status-Snapshots.** Wenn du einen Push
   timing-mäßig knapp gegen ein Pi-Status-Push triggerst, kriegst du
   `(non-fast-forward)`. Lösung: `git pull --rebase --no-edit` vor jedem
   Push, oder `git push --force-with-lease` als Notfall. Pi-Status-Pushes
   verlieren das Rennen, kein Daten-Verlust.

6. **Ein Token im Repo-`.git/config` ist sichtbar im Sandbox.** Im PokéPi
   ist das pragmatisch in Kauf genommen weil Cowork-Sandboxen ephemer
   sind. Bei Invest-Pi wäre eine cleane Lösung:
   - GitHub Deploy Key (per-repo SSH-Key) statt Personal Access Token
   - Oder: PAT scoped nur auf das Invest-Pi-Repo, mit Auto-Expire 90d

**Für Invest-Pi konkret:**

Der gleiche Workflow funktioniert 1:1, du brauchst nur:
- GitHub-Repo mit dem Pi-Code
- Auf dem Pi: `~/invest-pi/` als Git-Clone, mit eingebettetem Token
  (oder Deploy-Key) für Auto-Push
- `scripts/status_push.sh` + systemd-Timer (alle 2 Min)
- `scripts/auto_pull.sh` + systemd-Timer (alle 2 Min)
- Den Repo-Ordner als Cowork-Mount auf Mert's PC haben

Dann sieht eine Claude-Session so aus:
```
Mert: "Schau dir die letzte Hit-Rate an"
Claude: cd /tmp/invest_pi && git fetch && git show origin/main:_status/snapshot.json
        → liest stats raus, antwortet.

Mert: "Erweitere den Risk-Scorer um Dimension 10"
Claude: cd /tmp/invest_pi && [edits] && git commit && git push
        → "Push durch, in ~3 Min ist Pi rebuilt"

[2-3 Min später]

Claude: git fetch && git show origin/main:_status/snapshot.json
        → "Container Up 30 Seconds healthy, neuer Code aktiv"
```

**Das macht das System remote-debugbar ohne dass Claude jemals den Pi
direkt erreichen muss.** Selbst wenn der Pi hinter NAT, Firewall, mobiler
Verbindung oder Tailscale-only ist.

### Backups
- **Lokal**: täglich 03:30 sqlite3 .backup → gzipped → `data/backups/`, Rotation 14d
- **Cloud (restic + Backblaze B2)**: täglich 04:00 → encrypted, retention 7d/4w/6m

Für Invest-Pi: identisch. market.db kann groß werden (10y OHLCV für 50 Tickers
sind ~50MB), patterns.db klein (~5MB), alerts.db wächst linear (~1MB/Monat).

### Disk-Cleanup
Wöchentlich Sonntag 02:30:
- Logs > 5MB rotieren
- Rotated logs > 14d löschen
- alert_funnel-Rows > 90d weg
- VACUUM auf SQLite

### Hardware-Alerts
Wenn `cpu_temp ≥ 75°C` ODER `disk ≥ 90%` ODER `memory ≥ 90%` → Telegram-Push.
Lockfile verhindert Spam (max 1x/Stunde).

---

## Konkrete Prompt-Patterns die in PokéPi gewachsen sind

### Pattern 1: Mid-Period-Hit-Rate im Prompt
Sonnet sieht in jedem Score-Call die letzte 30d-Hit-Rate seiner eigenen
Predictions:

```
## Aktuelle Lern-Statistik (letzte 30d, daily_score):
Total: 87 Outcomes, Hit-Rate gesamt: 64%
- Verdict ALERT_3: 4/8 korrekt (50%)
- Verdict ALERT_2: 12/22 korrekt (55%)
- Verdict GREEN:   42/57 korrekt (74%)
- Konfidenz high:   6/8 korrekt (75%) — gut kalibriert
- Konfidenz medium: 18/40 korrekt (45%) — moeglicherweise zu optimistisch
- Konfidenz low:    16/39 korrekt (41%)
```

**Effekt:** Sonnet kalibriert sich selbst zwischen Wochenberichten ohne
Code-Änderung. Kostet ~200 Tokens pro Prompt.

### Pattern 2: User-Feedback-Patterns im Prompt
Wie oben — Mert's ❌-Reasons der letzten 30d landen im System-Prompt.

### Pattern 3: Markt-Stimmung extrahiert aus Wochenbericht
PokéPi: aus `market_report.md` wird die `## Markt-Stimmung`-Sektion separat
extrahiert und kompakt in den alert-Prompt gehängt — Sonnet sieht die
Marktphase ohne den ganzen Bericht zu lesen.

**Für Invest-Pi:** der `quarterly_outlook.md` von Opus hat sicher Sektionen
wie `## Sektor-Stimmung` / `## Macro-Lage` — die einzeln in die täglichen
Score-Prompts injecten.

### Pattern 4: Cost-Awareness-Block
Wenn Tagesbudget zu >70% aufgebraucht: extra-Block im Prompt.

### Pattern 5: Konfidenz-Stratifikation in meta_review
Opus bekommt nicht nur Total-Hit-Rate, sondern pro Confidence-Stufe getrennt,
plus pro Sektor/Set/Card-Type. Damit kann Opus konkret sagen *„HOCH ist nur
zu 50% korrekt — Schwelle anziehen wenn drunter"*.

**Für Invest-Pi-Übersetzung:** plus Stratifizierung pro Ring (1/2/3), pro
Sektor (Semiconductor/Software/Hyperscaler/etc.), pro Macro-Regime
(low-vol/high-vol/crisis).

---

## Frontend-Lessons (falls Invest-Pi ein UI bekommt)

### Sheet-Layout
3-Slot flex column: Header (fix top) / Body (`flex:1; overflow:auto`) / Footer
(fix bottom mit border-top + safe-area-bottom-padding).

### PWA-Setup
- `manifest.webmanifest` mit `display: standalone` + Icons 192/512 als
  `purpose: "any maskable"`
- Service-Worker mit `cache-first` für statics, `network-first` für API,
  Offline-Fallback {`offline: true`} für API-fails
- index.html `Cache-Control: no-cache, no-store, must-revalidate` in nginx

### State-Management-Pattern
PokéPi hat `useApi(fetcher, fallback, intervalMs)` als Standard-Hook —
loading/error/data automatisch, mit Fallback auf mocks.js wenn Backend
nicht erreichbar. Plus `useStored(key, default)` für localStorage-State
(Admin-Token, lastSellChannel, etc.).

### Live-Reload-Pattern
Bei state-mutations (verkauft, gelöscht, ✅ Gekauft) — `refreshKey`-Counter in
`<App>` rauf, alle Children kriegen das als `key`-Prop, React remountet sie,
useApi lädt neu. Ohne komplexes State-Management.

---

## Implementation-Plan für Invest-Pi (Reihenfolge)

Was die Doku bei dir hat (`README.md` von Invest-Pi) ist eine sehr gute
Foundation. Hier was ich aus PokéPi-Erfahrung als nächste Steps empfehle —
**bevor** du irgendwelche advanced features baust:

### Phase 0: Foundation festziehen (1 Tag)
1. **`predictions`-Tabelle anlegen** (Schema oben). Schon jetzt, nicht später.
2. **Markdown-Strip-Helper** in `src/common/json_utils.py`.
3. **Tenacity-Retries** auf yfinance, Finnhub, NewsAPI.
4. **Status-Bus** (`scripts/status_push.sh` analog PokéPi) — Pi schreibt alle
   2 Min `_status/snapshot.json` ins Repo, pusht.
5. **Auto-Pull** (`scripts/auto_pull.sh` analog PokéPi) — Push-to-Deploy.

### Phase 1: Self-Learning-Loop verkabeln (2-3 Tage)
6. **`risk_scorer.score_ticker()` schreibt jede Score-Berechnung als prediction-Row**
   mit `subject_id=ticker`, `prompt_hash=sha256(system_prompt)[:16]`.
7. **Outcome-Tracker als cron job (`scripts/track_outcomes.py`)** läuft täglich
   02:00, vergleicht 7d-alte Predictions mit aktuellen Kursen.
8. **Drift-Detection** im outcome_tracker — wenn 7d-hit-rate vs prior 7d
   um >15pp fällt → Telegram-Warnung.

### Phase 2: Telegram-Feedback-Loop (1 Tag)
9. **`src/alerts/notifier.py`** mit Inline-Buttons + `telegram_callbacks.py`
   cron-job (1x/Min via systemd-Timer).
10. **`feedback_reasons`-Tabelle** + Reason-Folgefrage bei ❌.

### Phase 3: Cost-Caps (0.5 Tage)
11. **3-Tier-Caps (hourly/daily/monthly)** in config.yaml.
12. **Cost-Awareness-Block** im Prompt ab 70% Tagesbudget.
13. **Hard-Stop bei Monthly** mit Telegram-Notification.

### Phase 4: Meta-Loop (1 Tag)
14. **`scripts/meta_review.py`** monatlich — Opus reflektiert über alle
    `daily_score`-Predictions mit Outcomes pro Ticker, pro Sektor, pro
    Confidence. Output: `data/reviews/<date>-daily_score.md`.
15. **`load_meta_reviews()`** in den Score-Prompt — geschlossener Loop.

### Phase 5: User-Workflow (1 Tag)
16. **DCA-Empfehlung** (`monthly_dca`-Job) mit JSON-Block am Ende für robust
    outcome-tracking.
17. **buy.py mit Auto-Trigger** aus Telegram „✅ habe gekauft"-Click.

### Phase 6: Operations (0.5 Tage)
18. **DB-Backup täglich** (lokal gzipped + restic Cloud).
19. **Disk-Cleanup wöchentlich**.
20. **Hardware-Alerts** (CPU/disk/memory).
21. **Container-Limits** in docker-compose (oder systemd-Limits da
    Invest-Pi systemd-basiert ist statt Docker).

### Phase 7 (optional): Frontend
22. **FastAPI Dashboard** mit Sheet-Layout, PWA-Manifest, Service-Worker.

---

## Anti-Patterns — was Invest-Pi NICHT machen sollte

1. **„Ich bau erst den Risk-Scorer fertig, dann das Outcome-Tracking."**
   → Das Tracking muss von Anfang an mitlaufen. Sonst hast du nach 3 Wochen
   tausende un-tracked Predictions und kein Lern-Material.

2. **„Cost-Caps sind nicht nötig, ich behalte das schon im Auge."**
   → Vergiss man, vergisst man. PokéPi hat 2,11€ in 6 Stunden verbraucht ohne
   dass mir das aufgefallen wäre. Hard-Stop = Sicherheitsnetz.

3. **„Sonnet macht das schon richtig, Konfidenz brauche ich nicht zu kalibrieren."**
   → In PokéPi war Sonnet bei medium-Konfidenz nur zu 38% korrekt — also
   schlechter als Münzwurf. Ohne Konfidenz-Stratifikation hättest du das nie
   gemerkt.

4. **„Markdown-Codefences im Output sind kein Problem."**
   → Doch. Strip immer.

5. **„Ich speichere prompt_hash später, brauche ich jetzt nicht."**
   → Wenn du später A/B-testen willst, brauchst du historische prompt_hashes.
   Setze ihn von Tag 1 an.

6. **„Telegram-Markdown ist OK."**
   → Ein Underscore in einem Ticker-Namen (z.B. „BRK_B") killt dir den Push.
   Immer HTML.

7. **„Externe APIs ohne Retry — passt schon."**
   → 1× yfinance-Hicks = ganzer Score-Run versaut.

8. **„Predictions-Tabelle kann ich später normalisieren."**
   → Migrations sind teuer wenn da schon 50.000 Rows drin sind. Schema-First.

9. **„Frontend ohne PWA-Manifest, ist ja nur lokal."**
   → Sobald du das Dashboard auf dem Handy benutzen willst (was *immer*
   passiert), willst du Add-to-Home-Screen + Offline-Fallback.

10. **„Cron-jobs schreiben Logs in stdout, das passt schon."**
    → Wachsen unkontrolliert. Wöchentliche Log-Rotation einbauen.

---

## Spezifika für Invest-Pi (anders als PokéPi)

### 1. Outcome-Frist ist anders
PokéPi: 7 Tage (Pokémon-Karten-Markt bewegt sich langsam).
Invest-Pi: **3 Outcomes pro prediction** sinnvoll:
- T+1d (Day-after — News-Reaktion)
- T+7d (kurzfristige Markt-Reaktion)
- T+30d (mittelfristige Bestätigung)

→ jede prediction kann 3 outcome-Spalten haben (`outcome_1d`, `outcome_7d`,
`outcome_30d`), oder du machst 3 separate outcome-rows.

### 2. False-Positive-Rate ist akzeptierter
Invest-Pi-Doc sagt: 30-40% false positives bei Stufe-2-Alerts erwartet.
**Lerne diese Erwartung in den Prompt:** *„Stufe-2-Alerts haben historisch
60-70% Hit-Rate — das ist OK. Mache nicht den Fehler, durch über-vorsichtige
Schwellen die hit-rate auf 95% zu pushen, dann verpasst du echte Risiken."*

### 3. Pattern-Library ist pre-trained, nicht learned
Bei PokéPi gab's keine pre-existing Pattern-Library — alles wurde live
gelernt. Bei Invest-Pi ist `pattern_miner` schon ein vorgefertigtes
„Ähnlichkeits-DB". Das macht den Cold-Start schneller.

**Ergänzung:** in den Score-Prompt nicht nur die letzten 30d eigenen
Outcomes, sondern AUCH die Top-3 historischen Analoga aus der Pattern-Library
(„aktuelle Lage ähnelt 2018 Q3 NVDA-Korrektur, 2020 ASML pre-COVID").
Das gibt Sonnet einen Anker den PokéPi-Sonnet nie hatte.

### 4. Ring-Konzept ist gold
PokéPi hat keinen Ring/Tier-Begriff — das ist ein wertvolles Konzept aus
Invest-Pi das du in der Stratifizierung nutzen kannst. Konfidenz-Kalibrierung
*per Ring* zeigt z.B. ob Sonnet bei Spec-Stocks (Ring 3) systematisch
über-optimistisch ist.

### 5. Macro-Layer
Bei PokéPi hatten wir keinen Macro-Layer. Bei Invest-Pi ist `macro_regime`
eine eigene Dimension. **Lesson:** Macro-Regime-Wechsel sind seltene Events
(2008, 2020, 2022) — die haben sehr wenig outcome-data. Bewerte sie konservativ:
„Macro-Regime-Switch ist eine Behauptung mit niedriger Konfidenz, alle anderen
Dimensionen sollten das stützen."

### 6. yfinance vs TCG-API
yfinance ist deutlich rate-limited und unzuverlässig (Sub-Library um eine
inoffizielle Yahoo-API). PokéPi konnte sich auf die TCG-API verlassen.
**Empfehlung:** `force_refresh=False` als Default und großzügiger Cache.
Plus: Fallback auf alternative Quelle (z.B. Alpha Vantage) wenn yfinance fail.

---

## Was PokéPi (und ich) am liebsten anders gemacht hätte

Wenn ich PokéPi nochmal von vorne starten würde — das hier wäre meine
Foundation am ersten Tag:

```
backend/
├── common/
│   ├── predictions.py          # Schema + log_prediction + record_outcome
│   ├── outcomes.py             # outcome_tracker logic, Drift-Detection
│   ├── meta_review.py          # monatlicher Opus-Review
│   ├── feedback.py             # User-Feedback-Patterns für Prompts
│   ├── cost_caps.py            # 3-Tier-Caps + Cost-Awareness-Block
│   ├── retry.py                # tenacity-defaults
│   └── json_utils.py           # strip_codefence, safe_parse
├── status_bus/
│   ├── push.sh                 # snapshot.json → git
│   ├── auto_pull.sh            # git pull + auto-rollback
│   └── backup.sh               # restic + lokal
├── jobs/
│   ├── score.py                # scoring-cron
│   ├── meta.py                 # meta-review-cron
│   └── outcomes.py             # outcome-tracking-cron
└── notifier/
    ├── telegram.py             # send_alert + inline-buttons + silent-quiet-hours
    └── callbacks.py            # cron-poll für callback_queries
```

**Diese Struktur ist domain-agnostisch** — du kannst sie für PokéPi,
Invest-Pi, oder ein drittes Projekt nehmen. Die domain-Logik (alert_loop für
Pokémon, risk_scorer für Stocks) baut sich oben drauf auf.

---

## Was ich JETZT für Invest-Pi tun würde wenn ich Mert wäre

1. **Diese Datei bookmarken.**
2. **Phase 0 + Phase 1 zuerst** (Foundation + Self-Learning). 3-4 Tage.
3. **Den `risk_scorer` jetzt schon so schreiben dass jede Score-Berechnung
   als prediction-Row landet.** Sogar wenn der scorer noch deterministisch
   ist (kein Sonnet) — das Gerüst muss da sein.
4. **Telegram-Notifier + Buttons VOR dem Claude-Layer.** Klingt
   kontraintuitiv, aber: erst muss die User-Feedback-Schiene laufen, sonst
   hat Claude später keine Trainings-Daten.
5. **Erstmal ohne Claude-Sonnet auskommen** und nur den deterministischen
   9-Dim-Score nutzen — sammelt Daten. Sonnet später dazuschalten wenn du
   30+ Outcomes pro Ticker hast.
6. **Pattern-Miner nicht überschätzen** — die Doku selbst sagt es:
   „Ähnlichkeits-DB, keine Vorhersage". Ehrlich kommunizieren in jedem
   Output.

---

**Letzter Hinweis:** PokéPi war ein 14-Tage-Sprint mit ~300 Commits am Ende.
Invest-Pi sollte langsamer wachsen — gerade weil echtes Geld im Spiel ist.
Lieber 3 Wochen Foundation als 1 Tag Foundation und dann 6 Monate Bug-Hunt.

Wenn du in der nächsten Session mit Claude an Invest-Pi arbeitest und Claude
sagt „schreiben wir das schnell zusammen" — schick ihm/ihr diese Datei. Das
beschleunigt deutlich, weil das ganze gelernte Wissen aus PokéPi sofort da ist.

— geschrieben am 27.04.2026 nach der finalen PokéPi-Session
