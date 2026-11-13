"""DAB-Lejerbo vacant home monitor.

Checks three public sources on dab-lejerbo.dk, keeps the homes that fall in the
configured area and rent limit, and sends a Telegram message for each one it
has not seen before. Standard library only.

    python monitor.py               full check of every source
    python monitor.py --fast        quick check of the first-come sources (office hours only)
    python monitor.py --status      send the summary message now
    python monitor.py --dry-run     print matches, send nothing, save nothing
    python monitor.py --test-message  send a test message to Telegram
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "https://dab-lejerbo.dk"
USER_AGENT = "Mozilla/5.0 (compatible; vacant-home-monitor)"

# The member-portal API returns the site's 404 page unless this header is sent.
# 3500 is the value used by the "Ledige boliger" web component.
LEDIGE_MODULE = "3500"
LEDIGE_URL = f"{BASE}/api/memberportal/availabletenancies/getAllAvailableTenancies"
LEDIGE_PAGE = f"{BASE}/boligsoegende/ledige-boliger/"
TIME_LIMITED_PATH = "/boligsoegende/tidsbegraensede-boliger/"
TIME_LIMITED_FORM = f"{BASE}{TIME_LIMITED_PATH}blanket-til-tidsbegraenset-bolig/"
PAGE_CONTENT_URL = f"{BASE}/api/PageContent/GetPageContent"
SITEMAP_URL = f"{BASE}/api/Sitemap/GetXmlSitemap"
NEW_BUILD_PREFIX = f"{BASE}/boligsoegende/her-bygger-vi-nyt/"
LLMS_PAGE_URL = f"{BASE}/api/llms/page-content"

SOURCE_LABELS = {
    "ledige": "Ledige boliger",
    "time_limited": "Time-limited home",
    "new_builds": "New-build project",
}
SOURCE_PLURALS = {
    "ledige": "vacant homes",
    "time_limited": "time-limited homes",
    "new_builds": "new-build projects",
}
MESSAGE_LIMIT = 3500  # Telegram allows 4096 characters


# ---------------------------------------------------------------- HTTP

def http(url: str, *, data: dict | None = None, headers: dict | None = None,
         timeout: int = 60, tries: int = 3) -> bytes:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    hdrs.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt < tries - 1:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"request failed: {url}: {last}")


def api_json(url: str, **kw) -> dict:
    raw = http(url, **kw)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise RuntimeError(f"expected JSON from {url}, got {raw[:80]!r}")
    if not isinstance(payload, dict) or not payload.get("succeeded"):
        raise RuntimeError(f"API reported failure for {url}")
    return payload["data"]


# ---------------------------------------------------------------- zones

class Zones:
    """Postcode table with three tiers: core (zones 1-2), extended (adjacent zones)
    and wide (the rest of Greater Copenhagen, applied only to the sources listed
    in wide_sources)."""

    def __init__(self, table: list[dict], extra: list[int], include_extended: bool,
                 include_wide: bool = False, wide_sources=("time_limited",)):
        self.table = table
        self.extra = {int(p) for p in extra}
        self.include_extended = include_extended
        self.include_wide = include_wide
        self.wide_sources = set(wide_sources)

    def _enabled(self, row: dict, source: str | None) -> bool:
        if row["tier"] == "extended":
            return self.include_extended
        if row["tier"] == "wide":
            return self.include_wide and (source is None or source in self.wide_sources)
        return True

    def lookup(self, postcode, source: str | None = None) -> dict | None:
        """Return the zone entry if the postcode is inside the area watched for this source."""
        try:
            pc = int(str(postcode).strip())
        except (TypeError, ValueError):
            return None
        if pc in self.extra:
            return {"tier": "core", "zones": "custom", "area": "", "border": False}
        for row in self.table:  # first match wins, so narrow rows come before wide ranges
            if row["from"] <= pc <= row["to"]:
                return row if self._enabled(row, source) else None
        return None

    def postcodes(self, wide: bool = False) -> set[int]:
        out: set[int] = set()
        for row in reversed(self.table):  # reversed so that earlier rows win, as in lookup
            codes = range(row["from"], row["to"] + 1)
            if row["tier"] == "wide":
                keep = wide and self.include_wide
            else:
                keep = self._enabled(row, None)
            if keep:
                out.update(codes)
            else:
                out.difference_update(codes)
        return out | self.extra

    def describe(self) -> str:
        text = "zones 1-2" + (" plus adjacent zones" if self.include_extended else " only")
        wide = [SOURCE_PLURALS[s] for s in SOURCE_PLURALS if s in self.wide_sources]
        if self.include_wide and wide:
            text += "; all of Greater Copenhagen for " + " and ".join(wide)
        return text


def tier_label(zone: dict) -> str:
    if zone["tier"] == "core":
        return f"zone 1-2 (zones {zone['zones']})"
    if zone["tier"] == "extended":
        return f"adjacent zone (zones {zone['zones']})"
    return f"wider area, {zone['area']}"


def load_config(path: Path) -> tuple[dict, Zones]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    zones_path = path.parent / cfg.get("zones_file", "zones.json")
    table = json.loads(zones_path.read_text(encoding="utf-8"))["postcodes"]
    zones = Zones(table, cfg.get("extra_postcodes", []), cfg.get("include_extended", True),
                  cfg.get("include_wide", False), cfg.get("wide_sources", ["time_limited"]))
    return cfg, zones


# ---------------------------------------------------------------- time

def copenhagen_now(utc: dt.datetime | None = None) -> dt.datetime:
    """Danish local time, computed without a timezone database (EU summer-time rule)."""
    utc = utc or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    def last_sunday(month: int) -> dt.datetime:
        d = dt.datetime(utc.year, month, 31, 1)
        return d - dt.timedelta(days=(d.weekday() + 1) % 7)

    summer = last_sunday(3) <= utc < last_sunday(10)
    return utc + dt.timedelta(hours=2 if summer else 1)


def in_office_hours(cfg: dict, now: dt.datetime) -> bool:
    """Time-limited homes are only posted while the office is open."""
    oh = cfg.get("office_hours", {})
    if now.weekday() not in oh.get("weekdays", [0, 1, 2, 3, 4]):
        return False
    return oh.get("start", "07:30") <= now.strftime("%H:%M") < oh.get("end", "17:30")


# ---------------------------------------------------------------- sources

def fetch_ledige() -> list[dict]:
    homes, page, size = [], 1, 500
    while True:
        data = api_json(LEDIGE_URL, headers={"Module-Enum": LEDIGE_MODULE}, data={
            "selectedNumberOfRooms": 0, "selectedMinimumSize": 0, "selectedMaxRent": 0,
            "selectedMainRegion": "", "selectedRegion": "",
            "fallbackDescriptionToDepartmentDescription": False,
            "pageNumber": page, "pageSize": size, "sortBy": "moveInDate",
            "selectedApartmentTypeGroupIds": [], "selectedTenancyTypeGroupIds": [],
        })
        try:
            batch = data["data"]["availableTenancies"]
            total = int(data["totalRecords"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("Ledige boliger: response has an unexpected shape")
        if not isinstance(batch, list):
            raise RuntimeError("Ledige boliger: response has an unexpected shape")
        homes.extend(parse_ledige(t) for t in batch)
        if len(homes) >= total or not batch or page >= 20:
            return homes
        page += 1


def parse_ledige(t: dict) -> dict:
    addr = t.get("address") or {}
    key = f"{t['companyId']}_{t['departmentId']}_{t['tenancyId']}"
    kind = ", ".join(x for x in (t.get("tenancyType"), t.get("apartmentType")) if x)
    return {
        "source": "ledige",
        "key": key,
        "title": " ".join(x for x in (addr.get("street"), addr.get("location")) if x),
        "postcode": addr.get("zipCode"),
        "city": addr.get("city") or "",
        "rent": t.get("rentNet"),
        "deposit": t.get("deposit"),
        "rooms": t.get("rooms"),
        "sqm": t.get("sqm"),
        "kind": kind,
        "available": t.get("rentalNote") or "",
        "estate": t.get("departmentName") or "",
        "details": "",
        "link": f"{LEDIGE_PAGE}?ten={key}",
    }


def fetch_time_limited() -> list[dict]:
    url = PAGE_CONTENT_URL + "?" + urllib.parse.urlencode({"pageUrl": TIME_LIMITED_PATH, "query": ""})
    data = api_json(url)
    content = data.get("htmlContent") if isinstance(data, dict) else None
    if not content:
        raise RuntimeError("Time-limited homes: page content missing")
    homes = parse_time_limited(content)
    if not homes:
        raise RuntimeError("Time-limited homes: no listings could be read from the page")
    return homes


_POSTCODE_LINE = re.compile(r"(?:^|\s)(\d{4})\s+([A-ZÆØÅ][^\d\n]*)$")
_POSTCODE_TAIL = re.compile(r"[,\s]*(?:postnummer og by\s*)?\b\d{4}\s+[A-ZÆØÅ][^\d\n]*$", re.I)
_RENT = re.compile(r"husleje[^\d\n]*([\d.]+(?:,\d+)?)", re.I)
_DEPOSIT = re.compile(r"indskud[^\d\n]*([\d.]+(?:,\d+)?)", re.I)
_PERIOD = re.compile(r"udlejningsperiode:?\s*(.*)", re.I)


def danish_number(text: str) -> float | None:
    try:
        return float(text.strip(".").replace(".", "").replace(",", "."))
    except ValueError:
        return None


def html_to_lines(fragment: str) -> list[str]:
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ")
    return [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n") if ln.strip()]


def split_homes(lines: list[str]) -> list[list[str]]:
    """A paragraph sometimes holds several homes. Each home ends with its rental-period line."""
    chunks, cur, i = [], [], 0
    while i < len(lines):
        cur.append(lines[i])
        pm = _PERIOD.search(lines[i])
        if pm:
            if not re.search(r"\d", pm.group(1)) and i + 1 < len(lines):  # dates on the next line
                i += 1
                cur.append(lines[i])
            chunks.append(cur)
            cur = []
        i += 1
    if cur:
        chunks.append(cur)
    homes: list[list[str]] = []
    for chunk in chunks:
        if homes and not _RENT.search("\n".join(chunk)):
            homes[-1].extend(chunk)  # trailing note or link: belongs to the home before it
        else:
            homes.append(chunk)
    return homes


def parse_time_limited(content: str) -> list[dict]:
    """The page is hand-written: an <h3> per estate, then <p> blocks holding one or more homes."""
    homes, estate, seen = [], "", set()
    for m in re.finditer(r"<(h[2-4]|p)\b[^>]*>(.*?)</\1>", content, flags=re.S | re.I):
        tag, lines = m.group(1).lower(), html_to_lines(m.group(2))
        if not lines:
            continue
        if tag != "p":
            estate = " ".join(lines)
            continue
        postcode = city = None  # a home without its own postcode line takes the one above it
        for chunk in split_homes(lines):
            for ln in chunk:
                pm = _POSTCODE_LINE.search(ln)
                if pm and 1000 <= int(pm.group(1)) <= 9999:
                    postcode, city = pm.group(1), pm.group(2).strip(" .")
                    break
            if not postcode:
                continue
            text = "\n".join(chunk)
            rent_m, dep_m = _RENT.search(text), _DEPOSIT.search(text)
            norm = re.sub(r"\W+", "", (estate + text).lower())
            key = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
            if key in seen:
                continue
            seen.add(key)
            period = ""
            for i, ln in enumerate(chunk):
                pm = _PERIOD.search(ln)
                if pm:
                    period = pm.group(1)
                    if not re.search(r"\d", period) and i + 1 < len(chunk):
                        period = (period + " " + chunk[i + 1]).strip()
                    break
            homes.append({
                "source": "time_limited",
                "key": key,
                "title": _POSTCODE_TAIL.sub("", chunk[0]).strip(" ,") or estate,
                "postcode": postcode,
                "city": city,
                "rent": danish_number(rent_m.group(1)) if rent_m else None,
                "deposit": danish_number(dep_m.group(1)) if dep_m else None,
                "rooms": None, "sqm": None, "kind": "", "available": "",
                "period": period.strip(" ."),
                "flexible": bool(re.search(r"fleksib", text, re.I)),
                "estate": estate,
                "details": text,
                "link": BASE + TIME_LIMITED_PATH,
            })
    return homes


def fetch_new_builds() -> list[dict]:
    xml = http(SITEMAP_URL).decode("utf-8", "replace")
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
    if not urls:
        raise RuntimeError("New builds: sitemap is empty or unreadable")
    out = []
    for u in sorted(set(urls)):
        if u.startswith(NEW_BUILD_PREFIX) and u.rstrip("/") != NEW_BUILD_PREFIX.rstrip("/"):
            out.append({"source": "new_builds", "key": u, "link": u})
    return out


def new_build_title(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    try:
        text = http(LLMS_PAGE_URL + "?" + urllib.parse.urlencode({"path": path}), tries=1).decode("utf-8", "replace")
        m = re.search(r"^#\s+(.+)$", text, flags=re.M)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").capitalize()


FETCHERS = {"ledige": fetch_ledige, "time_limited": fetch_time_limited, "new_builds": fetch_new_builds}


# ---------------------------------------------------------------- filter / format

def match(home: dict, cfg: dict, zones: Zones) -> dict | None:
    """Return the zone entry when the home is in the area and within the rent cap."""
    zone = zones.lookup(home.get("postcode"), home.get("source"))
    if zone is None:
        return None
    rent = home.get("rent")
    if rent is not None and rent > cfg["max_rent"]:
        return None
    return zone


def near_miss(home: dict, cfg: dict, zones: Zones) -> bool:
    """In the area, but a little over the rent cap. Reported in the daily summary only."""
    rent, top = home.get("rent"), cfg.get("near_miss_rent", 0)
    if rent is None or not cfg["max_rent"] < rent <= top:
        return False
    return zones.lookup(home.get("postcode"), home.get("source")) is not None


def kr(value) -> str:
    return f"{value:,.0f}".replace(",", ".") + " kr"


def format_home(home: dict, zone: dict, cfg: dict) -> str:
    e = html.escape
    first_come = home["source"] == "time_limited"
    icon = "⚡" if first_come else "🏠"
    lines = [f"{icon} <b>{e(SOURCE_LABELS[home['source']])}</b>",
             f"<b>{e(home['title'])}</b>, {e(str(home['postcode']))} {e(home['city'])}"]
    zline = "Area: " + tier_label(zone)
    if zone.get("border"):
        zline += " — postcode crosses a zone border, check the address"
    lines.append(e(zline))
    rent = home.get("rent")
    if rent is None:
        lines.append("Rent: could not be read, check the listing")
    else:
        note = f" (above {kr(cfg['preferred_rent'])})" if rent > cfg.get("preferred_rent", rent) else ""
        lines.append(f"Rent: {kr(rent)}/month{note}")
    if home.get("deposit") is not None:
        lines.append(f"Deposit: {kr(home['deposit'])}")
    if not first_come:
        facts = []
        if home.get("rooms"):
            facts.append(f"{home['rooms']} rooms")
        if home.get("sqm"):
            facts.append(f"{home['sqm']:g} m²")
        if home.get("kind"):
            facts.append(home["kind"])
        if facts:
            lines.append(e(", ".join(facts)))
        if home.get("available"):
            lines.append(e(f"Status: {home['available']}"))
        if home.get("estate"):
            lines.append(e(f"Estate: {home['estate']}"))
        lines.append(home["link"])
        return "\n".join(lines)

    if home.get("period"):
        lines.append(e(f"Period: {home['period']}"))
    if home.get("estate"):
        lines.append(e(f"Estate: {home['estate']}"))
    if home.get("flexible"):
        lines.append("Let by flexible rules (job, study or similar criteria apply)")
    lines.append("<i>" + e(home["details"]) + "</i>")
    lines.append("<b>First come, first served.</b> Apply now:")
    lines.append(TIME_LIMITED_FORM)
    lines.append("Listing: " + home["link"])
    return "\n".join(lines)


def short_line(home: dict) -> str:
    rent = kr(home["rent"]) if home.get("rent") is not None else "rent ?"
    bits = [f"{home['title']}, {home['postcode']} {home['city']}", rent]
    if home.get("rooms"):
        bits.append(f"{home['rooms']} rooms")
    if home.get("period"):
        bits.append(home["period"])
    return "• " + html.escape(" — ".join(bits))


def chunk_lines(header: str, entries: list[tuple[str, str]], footer: str = "") -> list[tuple[str, list[str]]]:
    """Pack (key, line) entries into as few messages as fit. Returns (text, keys) per message."""
    out, keys, body = [], [], []
    for key, line in entries:
        if body and len(header) + len(footer) + sum(len(x) + 1 for x in body) + len(line) > MESSAGE_LIMIT:
            out.append(("\n".join([header, *body, footer]).strip(), keys))
            keys, body = [], []
        keys.append(key)
        body.append(line)
    if body:
        out.append(("\n".join([header, *body, footer]).strip(), keys))
    return out


def format_batch(source: str, new: list[tuple[dict, dict]]) -> list[tuple[str, list[str]]]:
    """Many new homes at once (first run, or a wider area) are sent as lists, not one by one."""
    new = sorted(new, key=lambda hz: (hz[0].get("rent") is None, hz[0].get("rent") or 0))
    header = f"🏠 <b>{len(new)} new {SOURCE_PLURALS[source]} in your area and budget</b>"
    footer = (f"\nFirst come, first served. Apply: {TIME_LIMITED_FORM}\nListings: {BASE}{TIME_LIMITED_PATH}"
              if source == "time_limited" else f"\n{LEDIGE_PAGE}")
    return chunk_lines(header, [(h["key"], short_line(h)) for h, _ in new], footer)


def format_summary(cfg: dict, zones: Zones, now: dt.datetime, report: dict) -> str:
    lines = [f"📊 <b>Daily summary, {now.day} {now:%B}</b>",
             "The monitor is running. You only get other messages when a new home matches.",
             f"Watching {html.escape(zones.describe())}, rent up to {kr(cfg['max_rent'])}.", ""]
    for source, info in report.items():
        label = SOURCE_PLURALS[source].capitalize()
        if info.get("error"):
            lines.append(f"• {label}: check FAILED")
        elif source == "new_builds":
            lines.append(f"• {label}: {info['listed']} tracked")
        else:
            lines.append(f"• {label}: {info['listed']} listed, {len(info['matches'])} match")
    for title, field, cap in (("Open right now", "matches", 10),
                              (f"Just over budget (up to {kr(cfg.get('near_miss_rent', 0))})", "near", 5)):
        homes = [h for info in report.values() for h in info.get(field, [])]
        if not homes:
            continue
        homes.sort(key=lambda h: (h.get("rent") is None, h.get("rent") or 0))
        lines += ["", f"<b>{title}</b>"] + [short_line(h) for h in homes[:cap]]
        if len(homes) > cap:
            lines.append(f"…and {len(homes) - cap} more")
    return "\n".join(lines)[:MESSAGE_LIMIT + 400]


# ---------------------------------------------------------------- Telegram

def telegram_send(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    try:
        raw = http(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, data={
            "chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
            "disable_web_page_preview": True})
    except RuntimeError:
        # Never let the token (part of the URL) reach the logs.
        raise RuntimeError("Telegram request failed") from None
    if not json.loads(raw).get("ok"):
        raise RuntimeError("Telegram rejected the message")


# ---------------------------------------------------------------- state

def load_state(path: Path) -> dict:
    state = {"initialized": False, "seen": {}, "failures": {}, "broken_alerted": {},
             "heartbeat": "", "daily": ""}
    if path.exists():
        state.update(json.loads(path.read_text(encoding="utf-8")))
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- main run

def run(cfg: dict, zones: Zones, state: dict, *, dry_run: bool, send=telegram_send,
        fetchers: dict | None = None, log=print, fast: bool = False, force_summary: bool = False,
        now: dt.datetime | None = None) -> bool:
    """One check. Returns True when every checked source worked and all alerts were sent.

    fast=True checks only cfg["fast_sources"], and only for news: it does not count
    failures and sends no summaries. The full check does those.
    """
    fetchers = fetchers or FETCHERS
    now = now or copenhagen_now()
    today = now.date().isoformat()
    first_run = not state["initialized"]
    if fast:
        if first_run:
            return True  # the first full check sets the baseline
        wanted = set(cfg.get("fast_sources", ["time_limited"]))
        fetchers = {s: f for s, f in fetchers.items() if s in wanted}
    ok = True
    report: dict = {}

    def deliver(text: str) -> bool:
        if dry_run:
            log("--- would send ---\n" + text + "\n")
            return True
        try:
            send(text)
            return True
        except Exception as exc:
            log(f"send failed: {exc}")
            return False

    for source, fetch in fetchers.items():
        if not cfg.get("sources", {}).get(source, True):
            continue
        try:
            items = fetch()
        except Exception as exc:
            ok = False
            report[source] = {"error": True}
            if fast:
                log(f"{source}: failed: {exc}")
                continue
            n = state["failures"].get(source, 0) + 1
            state["failures"][source] = n
            log(f"{source}: FAILED ({n} in a row): {exc}")
            if n >= cfg.get("failure_threshold", 3) and not state["broken_alerted"].get(source):
                if deliver(f"⚠️ <b>Monitor problem</b>\n{html.escape(SOURCE_LABELS[source])} has failed "
                           f"{n} checks in a row, so new homes there may be missed.\n"
                           f"Last error: {html.escape(str(exc))[:300]}"):
                    state["broken_alerted"][source] = True
            continue

        if state["failures"].get(source) and state["broken_alerted"].get(source):
            deliver(f"✅ {html.escape(SOURCE_LABELS[source])} is working again.")
        state["failures"][source] = 0
        state["broken_alerted"][source] = False

        seen = state["seen"].get(source, {})
        current = {}
        if source == "new_builds":
            for it in items:
                if it["key"] in seen or first_run:
                    current[it["key"]] = seen.get(it["key"], today)
                    continue
                title = new_build_title(it["link"])
                if deliver(f"🏗 <b>New-build project announced</b>\n{html.escape(title)}\n{it['link']}"):
                    current[it["key"]] = today
                else:
                    ok = False
            report[source] = {"listed": len(items)}
            log(f"{source}: {len(items)} projects tracked")
        else:
            matches, new = [], []
            for home in items:
                zone = match(home, cfg, zones)
                if zone is None:
                    continue
                matches.append(home)
                if home["key"] in seen:
                    current[home["key"]] = seen[home["key"]]
                else:
                    new.append((home, zone))
            if len(new) > cfg.get("batch_over", 5):
                messages = format_batch(source, new)
            else:
                messages = [(format_home(h, z, cfg), [h["key"]]) for h, z in new]
            for text, keys in messages:
                if deliver(text):
                    current.update((k, today) for k in keys)
                else:
                    ok = False
            report[source] = {"listed": len(items), "matches": matches,
                              "near": [h for h in items if near_miss(h, cfg, zones)]}
            log(f"{source}: {len(items)} homes listed, {len(matches)} in your area and budget, {len(new)} new")
        # Keys that left the feed are dropped, so a home that comes back is alerted again.
        state["seen"][source] = current

    if fast:
        return ok

    if first_run and ok:
        counts = "\n".join(
            f"• {SOURCE_LABELS[s]}: " + (f"{r['listed']} projects tracked" if s == "new_builds" else
                                         f"{r['listed']} homes listed, {len(r['matches'])} in your area and budget")
            for s, r in report.items())
        if deliver(f"✅ <b>DAB-Lejerbo monitor is live</b>\nWatching {html.escape(zones.describe())}, "
                   f"rent up to {kr(cfg['max_rent'])}.\n{counts}"):
            state["initialized"] = True
        else:
            ok = False

    due = (cfg.get("daily_summary", True) and state.get("daily") != today
           and now.hour >= cfg.get("daily_summary_hour", 8) and state["initialized"])
    if force_summary or due:
        if deliver(format_summary(cfg, zones, now, report)):
            state["daily"] = today
        else:
            ok = False

    year, week, _ = now.date().isocalendar()
    state["heartbeat"] = f"{year}-W{week:02d}"
    return ok


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="DAB-Lejerbo vacant home monitor")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--state", default=str(ROOT / "state" / "seen.json"))
    ap.add_argument("--dry-run", action="store_true", help="print matches; send and save nothing")
    ap.add_argument("--fast", action="store_true",
                    help="check only the first-come sources, and only during office hours")
    ap.add_argument("--status", action="store_true", help="full check, then send the summary now")
    ap.add_argument("--in-office-hours", action="store_true",
                    help="exit 0 if the Danish office is open now, 1 otherwise")
    ap.add_argument("--test-message", action="store_true", help="send a test message and exit")
    args = ap.parse_args(argv)

    if args.test_message:
        telegram_send("✅ Test message from the DAB-Lejerbo monitor. Telegram is set up correctly.")
        print("test message sent")
        return 0

    cfg, zones = load_config(Path(args.config))
    now = copenhagen_now()
    if args.in_office_hours:
        return 0 if in_office_hours(cfg, now) else 1
    if args.fast and not in_office_hours(cfg, now):
        print("outside office hours, nothing to do")
        return 0

    state_path = Path(args.state)
    state = load_state(state_path)
    ok = run(cfg, zones, state, dry_run=args.dry_run, fast=args.fast, force_summary=args.status, now=now)
    if not args.dry_run:
        save_state(state_path, state)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
