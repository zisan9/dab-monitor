"""One-off report: which DAB-Lejerbo estates lie in your area and have homes within budget.

These are waiting-list estates (not vacancies). Use the list to check that your
membership covers the right housing organisations, and to see where the wait is short.

    python report.py                  zones 1-2 and adjacent zones
    python report.py --wide           all of Greater Copenhagen
    python report.py --out estates.md
"""
from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path

import monitor

SEARCH = f"{monitor.BASE}/api/memberportal/tenancysearch/"
HEADERS = {"Module-Enum": "3400"}  # value used by the "Find en bolig" web component
WAIT_ORDER = ["Kort ventetid", "1-3 år", "3-5 år", "5-10 år", "Mere end 10 år"]


def search(criteria: dict, wanted: set[int], max_rent: float, wait_id=None) -> list[dict]:
    crit = copy.deepcopy(criteria)
    for area in crit["areas"]:
        area["selected"] = area["id"] in wanted
    for wt in crit.get("waitTimes") or []:
        wt["selected"] = wt["id"] == wait_id
    crit["rent"]["selectedTo"] = max_rent
    crit["pageSize"], crit["page"] = 500, 1
    return monitor.api_json(SEARCH + "getDepartmentsOnSearch", headers=HEADERS, data=crit)["departments"]


def rule_title(rule_id: str, cache: dict) -> str:
    """Special letting rule of an estate, e.g. 'Kommunal visitering' (allocated by the municipality)."""
    if rule_id not in cache:
        try:
            msg = monitor.api_json(SEARCH + "getRule/" + rule_id, headers=HEADERS).get("message") or {}
            text = msg.get("title") or re.sub(r"<[^>]+>", "", msg.get("message") or "")
            cache[rule_id] = text.strip() or "special rules"
        except Exception:
            cache[rule_id] = "special rules"
    return cache[rule_id]


def build_rows(cfg: dict, zones: monitor.Zones, wide: bool = False) -> list[dict]:
    criteria = monitor.api_json(SEARCH + "getDefaultCriteriaViewModel", headers=HEADERS)
    wanted = zones.postcodes(wide=wide) & {a["id"] for a in criteria["areas"]}
    if not wanted:
        return []
    departments = search(criteria, wanted, cfg["max_rent"])

    # The search can filter on expected waiting time; one search per bucket tells us each estate's wait.
    waits: dict[tuple, list[str]] = {}
    for wt in criteria.get("waitTimes") or []:
        try:
            for dep in search(criteria, wanted, cfg["max_rent"], wait_id=wt["id"]):
                waits.setdefault((dep["company"]["id"], dep["id"]), []).append(wt["text"])
        except Exception:
            pass

    rules: dict = {}
    rows = []
    for dep in departments:
        codes = sorted({a["zipCode"] for a in dep.get("addresses") or [] if a.get("zipCode")})
        hits = [(c, zones.lookup(c)) for c in codes]
        hits = [(c, z) for c, z in hits if z and (wide or z["tier"] != "wide")]
        if not hits:
            continue
        tiers = {z["tier"] for _, z in hits}
        tier = "core" if "core" in tiers else "extended" if "extended" in tiers else "wide"
        rule_ids = {g.get("assignmentRule") for g in dep.get("tenancyGroups") or []} - {None, ""}
        wait = waits.get((dep["company"]["id"], dep["id"]), [])
        wait.sort(key=lambda w: WAIT_ORDER.index(w) if w in WAIT_ORDER else 99)
        rows.append({
            "tier": tier,
            "postcodes": ", ".join(c for c, _ in hits),
            "area": ", ".join(sorted({z["zones"] or z["area"] for _, z in hits})),
            "estate": dep["name"],
            "company": dep["company"]["name"],
            "homes": dep.get("tenancyCount") or 0,
            "min_rent": dep.get("minRent"),
            "rooms": f"{dep.get('minRooms')}-{dep.get('maxRooms')}",
            "wait": " / ".join(wait) or "?",
            "wait_rank": min([WAIT_ORDER.index(w) for w in wait if w in WAIT_ORDER], default=99),
            "rules": ", ".join(sorted({rule_title(r, rules) for r in rule_ids})),
            "link": f"{monitor.BASE}/boligsoegende/afdelingspraesentation/"
                    f"?companyId={dep['company']['id']}&departmentId={dep['id']}",
        })
    order = {"core": 0, "extended": 1, "wide": 2}
    rows.sort(key=lambda r: (order[r["tier"]], r["wait_rank"], r["min_rent"] or 0))
    return rows


def render(rows: list[dict], cfg: dict) -> str:
    names = {"core": "Zone 1-2", "extended": "Adjacent", "wide": "Wider area"}
    out = [f"# Estates in your area with homes up to {monitor.kr(cfg['max_rent'])}", "",
           "Waiting-list estates, not vacancies. 'Homes' counts the homes within the rent limit; "
           "'From' is the cheapest one. 'Wait' is DAB-Lejerbo's own estimate for an outside applicant. "
           "'Special rules' means you cannot simply sign up: for example 'Kommunal visitering' homes "
           "are allocated by the municipality. Sorted by area, then shortest wait.", "",
           "| Area | Postcode | Zones | Estate | Housing organisation | Homes | From | Rooms | Wait | Special rules | Link |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        rent = monitor.kr(r["min_rent"]) if r["min_rent"] else "?"
        out.append(f"| {names[r['tier']]} | {r['postcodes']} | {r['area']} | {r['estate']} | {r['company']} | "
                   f"{r['homes']} | {rent} | {r['rooms']} | {r['wait']} | {r['rules']} | {r['link']} |")
    orgs = sorted({r["company"] for r in rows})
    out += ["", f"{len(rows)} estates across {len(orgs)} housing organisations:", ""]
    out += [f"- {o}" for o in orgs]
    return "\n".join(out) + "\n"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(monitor.ROOT / "config.json"))
    ap.add_argument("--wide", action="store_true", help="include all of Greater Copenhagen")
    ap.add_argument("--out")
    args = ap.parse_args()
    cfg, zones = monitor.load_config(Path(args.config))
    text = render(build_rows(cfg, zones, wide=args.wide), cfg)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
