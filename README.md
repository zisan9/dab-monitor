# DAB-Lejerbo vacant home monitor

Watches [dab-lejerbo.dk](https://dab-lejerbo.dk/) and sends a Telegram message when a
home appears in your area with rent up to 7,500 kr.

It watches three public pages, logged out:

| Source | How homes are given out | Checked | Area |
|---|---|---|---|
| Tidsbegrænsede boliger (time-limited) | First come, first served | Every minute in office hours, otherwise every 10 minutes | Greater Copenhagen |
| Ledige boliger | Not stated by the site | Every 10 minutes | Greater Copenhagen |
| Her bygger vi nyt | Announcements only | Every 10 minutes | Everywhere |

It cannot see personal waiting-list offers; those still arrive by email and in Mit DAB-Lejerbo.
Those go by seniority, not speed: say yes to every offer you would accept before its deadline.

## What you receive
- **A message per new home.** Time-limited homes include the application form link,
  because the first applicant gets the home.
- **One list instead of many messages** when more than five homes are new at once
  (for example after widening the area).
- **A daily summary at about 08:00 Danish time**: proof that the monitor is alive, the
  homes open right now, and homes just over budget (up to `near_miss_rent`).
- **A "Monitor problem" message** if a source fails three full checks in a row, and
  "working again" when it recovers.

Between those messages, silence means nothing new matched.

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
python monitor.py --status         # full check, then sends the summary now
```

### 3. GitHub
1. Create a **public** repository and push this folder to it. The office-hours watcher
   runs almost continuously, which is free on public repositories only. For a private
   repo, remove the loop in `.github/workflows/monitor.yml` and use `*/30 * * * *`.
2. In the repo: Settings → Secrets and variables → Actions → New repository secret.
   Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
3. Actions tab → **monitor** → Run workflow. You should get a "monitor is live" message.

During Danish office hours one run stays up for 50 minutes and the next one takes over.
Outside office hours each run does one check and stops. GitHub may start a scheduled
run 5-15 minutes late at busy times.

## Changing what you get alerted about

`config.json`:
- `max_rent` — homes above this are ignored (rent before utilities).
- `preferred_rent` — homes above this are still sent, marked "above 7.000 kr".
- `near_miss_rent` — homes between `max_rent` and this appear in the daily summary only.
- `time_limited_min_end` — time-limited homes only: ignore homes whose rental period ends
  before this date (`"2027-04-01"` = must run to April 2027 or later). Set it to `""`
  to turn the filter off. Homes whose end date cannot be read are still sent.
- `include_extended` — `false` drops the zones adjacent to zones 1-2.
- `include_wide` — `false` goes back to zones 1-2 (plus adjacent) only.
- `wide_sources` — which sources use the wide area. Remove `"ledige"` to keep vacant
  homes to the zones while time-limited homes stay wide.
- `extra_postcodes` — extra postcodes to always include, e.g. `[3390]`.
- `sources` — switch a source off with `false`.
- `fast_sources`, `office_hours` — what is checked every minute, and when.
- `daily_summary`, `daily_summary_hour` — the daily message.
- `batch_over` — more new homes than this in one check are sent as one list.

`zones.json` is the postcode table, in three tiers: `core` (zones 1-2), `extended`
(adjacent zones) and `wide` (the rest of Greater Copenhagen: all 2xxx postcodes, the
S-train and coast-line towns in North Zealand, Roskilde and Køge). It was built by hand,
so review it. Postcodes marked `"border": true` cross a zone boundary; alerts for them
say so and you check the exact address.

## How it behaves
- The first run alerts any home that already matches, records the existing new-build
  pages silently, and sends one "monitor is live" summary.
- A home is alerted once. If it leaves the list and comes back, it is alerted again.
- The every-minute check only looks for news. Failure counting and summaries belong
  to the full check.
- State is kept in `state/seen.json`, committed by the workflow.

## Which estates to sign up for
```powershell
python report.py --out estates.md          # zones 1-2 and adjacent
python report.py --wide --out estates.md   # all of Greater Copenhagen
```
Lists the waiting-list estates in your area that have homes within your rent limit, with
DAB-Lejerbo's estimated waiting time and any special letting rule (for example homes
allocated by the municipality). The site does not publish which estates use flexible
letting criteria; check the estate's page for that.

## Tests
```powershell
python -m unittest discover -s tests
```

## How the site works (for maintenance)
- Vacant homes: `POST /api/memberportal/availabletenancies/getAllAvailableTenancies`
  with the header `Module-Enum: 3500`. Without that header the site returns its 404 page.
- Time-limited homes: `GET /api/PageContent/GetPageContent?pageUrl=/boligsoegende/tidsbegraensede-boliger/`
  returns hand-written HTML, parsed with regular expressions in `parse_time_limited`.
  A paragraph can hold several homes; each ends with its "Udlejningsperiode" line.
- New builds: URLs under `/boligsoegende/her-bygger-vi-nyt/` in `/api/Sitemap/GetXmlSitemap`.
- Estate search (report only): `/api/memberportal/tenancysearch/` with `Module-Enum: 3400`;
  `getRule/<id>` returns the text of a special letting rule.
