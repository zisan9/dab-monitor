"""DAB-Lejerbo vacant home monitor.

Checks three public sources on dab-lejerbo.dk, keeps the homes that fall in the
configured fare zones and rent limit, and sends a Telegram message for each one
it has not seen before. Standard library only.

    python monitor.py               normal run
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
PAGE_CONTENT_URL = f"{BASE}/api/PageContent/GetPageContent"
SITEMAP_URL = f"{BASE}/api/Sitemap/GetXmlSitemap"
NEW_BUILD_PREFIX = f"{BASE}/boligsoegende/her-bygger-vi-nyt/"
LLMS_PAGE_URL = f"{BASE}/api/llms/page-content"

SOURCE_LABELS = {
    "ledige": "Ledige boliger",
    "time_limited": "Time-limited home",
    "new_builds": "New-build project",
}


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
    def __init__(self, table: list[dict], extra: list[int], include_extended: bool):
        self.table = table
        self.extra = {int(p) for p in extra}
        self.include_extended = include_extended

    def lookup(self, postcode) -> dict | None:
        """Return the zone entry if the postcode is inside the watched area."""
        try:
            pc = int(str(postcode).strip())
        except (TypeError, ValueError):
            return None
        if pc in self.extra:
            return {"tier": "core", "zones": "custom", "area": "", "border": False}
        for row in self.table:
            if row["from"] <= pc <= row["to"]:
                if row["tier"] == "extended" and not self.include_extended:
                    return None
                return row
        return None

    def postcodes(self) -> set[int]:
        out = set(self.extra)
        for row in self.table:
            if row["tier"] == "extended" and not self.include_extended:
                continue
            out.update(range(row["from"], row["to"] + 1))
        return out


def load_config(path: Path) -> tuple[dict, Zones]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    zones_path = path.parent / cfg.get("zones_file", "zones.json")
    table = json.loads(zones_path.read_text(encoding="utf-8"))["postcodes"]
    zones = Zones(table, cfg.get("extra_postcodes", []), cfg.get("include_extended", True))
    return cfg, zones


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
_RENT = re.compile(r"husleje[^\d\n]*([\d.]+(?:,\d+)?)", re.I)
_DEPOSIT = re.compile(r"indskud[^\d\n]*([\d.]+(?:,\d+)?)", re.I)


def danish_number(text: str) -> float | None:
    try:
        return float(text.strip(".").replace(".", "").replace(",", "."))
    except ValueError:
        return None


def html_to_lines(fragment: str) -> list[str]:
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ")
    return [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n") if ln.strip()]


def parse_time_limited(content: str) -> list[dict]:
    """The page is hand-written: an <h3> per estate, then one <p> per home."""
    homes, estate, seen = [], "", set()
    for m in re.finditer(r"<(h[2-4]|p)\b[^>]*>(.*?)</\1>", content, flags=re.S | re.I):
        tag, lines = m.group(1).lower(), html_to_lines(m.group(2))
        if not lines:
            continue
        if tag != "p":
            estate = " ".join(lines)
            continue
        postcode = city = None
        for ln in lines:
            pm = _POSTCODE_LINE.search(ln)
            if pm and 1000 <= int(pm.group(1)) <= 9999:
                postcode, city = pm.group(1), pm.group(2).strip()
                break
        if not postcode:
            continue
        text = "\n".join(lines)
        rent_m, dep_m = _RENT.search(text), _DEPOSIT.search(text)
        norm = re.sub(r"\W+", "", (estate + text).lower())
        key = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
        if key in seen:
            continue
        seen.add(key)
        title = lines[0] if not _POSTCODE_LINE.search(lines[0]) else estate
        homes.append({
            "source": "time_limited",
            "key": key,
            "title": title,
            "postcode": postcode,
            "city": city,
            "rent": danish_number(rent_m.group(1)) if rent_m else None,
            "deposit": danish_number(dep_m.group(1)) if dep_m else None,
            "rooms": None, "sqm": None, "kind": "", "available": "",
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


# ---------------------------------------------------------------- filter / format

def match(home: dict, cfg: dict, zones: Zones) -> dict | None:
    """Return the zone entry when the home is in the area and within the rent cap."""
    zone = zones.lookup(home.get("postcode"))
    if zone is None:
        return None
    rent = home.get("rent")
    if rent is not None and rent > cfg["max_rent"]:
        return None
    return zone


def kr(value) -> str:
    return f"{value:,.0f}".replace(",", ".") + " kr"


def format_home(home: dict, zone: dict, cfg: dict) -> str:
    e = html.escape
    lines = [f"🏠 <b>{e(SOURCE_LABELS[home['source']])}</b>",
             f"<b>{e(home['title'])}</b>, {e(str(home['postcode']))} {e(home['city'])}"]
    tier = "zone 1-2" if zone["tier"] == "core" else "adjacent zone"
    zline = f"Area: {tier} (zones {zone['zones']})"
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
    if home["source"] == "ledige":
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
    else:
        if home.get("estate"):
            lines.append(e(f"Estate: {home['estate']}"))
        lines.append("<i>" + e(home["details"]) + "</i>")
    lines.append(home["link"])
    return "\n".join(lines)


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
    state = {"initialized": False, "seen": {}, "failures": {}, "broken_alerted": {}, "heartbeat": ""}
    if path.exists():
        state.update(json.loads(path.read_text(encoding="utf-8")))
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- main run

def run(cfg: dict, zones: Zones, state: dict, *, dry_run: bool, send=telegram_send,
        fetchers: dict | None = None, log=print) -> bool:
    """One check. Returns True when every enabled source worked and all alerts were sent."""
    fetchers = fetchers or {"ledige": fetch_ledige, "time_limited": fetch_time_limited,
                            "new_builds": fetch_new_builds}
    today = dt.date.today().isoformat()
    first_run = not state["initialized"]
    ok = True
    counts = {}

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
            counts[source] = f"{len(items)} projects tracked"
        else:
            matched = 0
            for home in items:
                zone = match(home, cfg, zones)
                if zone is None:
                    continue
                matched += 1
                if home["key"] in seen:
                    current[home["key"]] = seen[home["key"]]
                elif deliver(format_home(home, zone, cfg)):
                    current[home["key"]] = today
                else:
                    ok = False
            counts[source] = f"{len(items)} homes listed, {matched} in your area and budget"
        # Keys that left the feed are dropped, so a home that comes back is alerted again.
        state["seen"][source] = current
        log(f"{source}: {counts[source]}")

    if first_run and ok:
        summary = "\n".join(f"• {SOURCE_LABELS[s]}: {c}" for s, c in counts.items())
        ext = "plus adjacent zones" if zones.include_extended else "only"
        if deliver(f"✅ <b>DAB-Lejerbo monitor is live</b>\nWatching zones 1-2 {ext}, "
                   f"rent up to {kr(cfg['max_rent'])}.\n{summary}"):
            state["initialized"] = True
        else:
            ok = False

    year, week, _ = dt.date.today().isocalendar()
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
    ap.add_argument("--test-message", action="store_true", help="send a test message and exit")
    args = ap.parse_args(argv)

    if args.test_message:
        telegram_send("✅ Test message from the DAB-Lejerbo monitor. Telegram is set up correctly.")
        print("test message sent")
        return 0

    cfg, zones = load_config(Path(args.config))
    state_path = Path(args.state)
    state = load_state(state_path)
    ok = run(cfg, zones, state, dry_run=args.dry_run)
    if not args.dry_run:
        save_state(state_path, state)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
