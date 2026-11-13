# DAB-Lejerbo vacant home monitor

Checks [dab-lejerbo.dk](https://dab-lejerbo.dk/) every 10 minutes and sends a Telegram
message when a home appears in Copenhagen fare zones 1-2 (plus the adjacent zones) with
rent up to 7,500 kr.

It watches three public pages, logged out:

| Source | What triggers an alert |
|---|---|
| Ledige boliger | A new vacant home in your area and budget |
| Tidsbegrænsede boliger | A new time-limited home in your area and budget |
| Her bygger vi nyt | Any newly announced building project |

It cannot see personal waiting-list offers; those still arrive by email and in Mit DAB-Lejerbo.

## Setup

### 1. Telegram bot
1. In Telegram, open a chat with **@BotFather**, send `/newbot` and follow the prompts.
   Copy the token it gives you (looks like `123456789:AA...`).
2. Open a chat with your new bot and send it any message.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the
   number at `"chat":{"id": ...}`. That is your chat id.

### 2. Test on your PC (PowerShell)
```powershell
$env:TELEGRAM_BOT_TOKEN = "<token>"
$env:TELEGRAM_CHAT_ID = "<chat id>"
python monitor.py --test-message   # you should get a message
python monitor.py --dry-run        # shows what it would send, changes nothing
```

### 3. GitHub
1. Create a **public** repository and push this folder to it. (A private repo's free
   minutes are not enough for 10-minute checks; if you want it private, change the
   schedule in `.github/workflows/monitor.yml` to `*/30 * * * *`.)
2. In the repo: Settings → Secrets and variables → Actions → New repository secret.
   Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
3. Actions tab → **monitor** → Run workflow. You should get a "monitor is live" message.

After that it runs on its own. GitHub may start a scheduled run 5-15 minutes late at busy times.

## Changing what you get alerted about

`config.json`:
- `max_rent` — homes above this are ignored (rent before utilities).
- `preferred_rent` — homes above this are still sent, marked "above 7.000 kr".
- `include_extended` — `false` limits alerts to zones 1-2 only.
- `extra_postcodes` — extra postcodes to always include, e.g. `[2770]`.
- `sources` — switch a source off with `false`.

`zones.json` is the postcode-to-zone table. It was built by hand from the zone map, so
review it. Postcodes marked `"border": true` cross a zone boundary; alerts for them say
so and you check the exact address.

## How it behaves
- The first run alerts any home that already matches, records the existing new-build
  pages silently, and sends one "monitor is live" summary.
- A home is alerted once. If it leaves the list and comes back, it is alerted again.
- If a source fails three checks in a row you get one "Monitor problem" message, and a
  "working again" message when it recovers. So silence means nothing new.
- State is kept in `state/seen.json`, committed by the workflow.

## Which estates to sign up for
```powershell
python report.py --out estates.md
```
Lists the waiting-list estates in your zones that have homes within your rent limit,
and the housing organisations they belong to.

## Tests
```powershell
python -m unittest discover -s tests
```

## How the site works (for maintenance)
- Vacant homes: `POST /api/memberportal/availabletenancies/getAllAvailableTenancies`
  with the header `Module-Enum: 3500`. Without that header the site returns its 404 page.
- Time-limited homes: `GET /api/PageContent/GetPageContent?pageUrl=/boligsoegende/tidsbegraensede-boliger/`
  returns hand-written HTML, parsed with regular expressions in `parse_time_limited`.
- New builds: URLs under `/boligsoegende/her-bygger-vi-nyt/` in `/api/Sitemap/GetXmlSitemap`.
- Estate search (report only): `/api/memberportal/tenancysearch/` with `Module-Enum: 3400`.
