"""One-off report: which DAB-Lejerbo estates lie in your zones and have homes within budget.

These are waiting-list estates (not vacancies). Use the list to check that your
membership covers the right housing organisations.

    python report.py            print the table
    python report.py --out estates.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import monitor

SEARCH = f"{monitor.BASE}/api/memberportal/tenancysearch/"
HEADERS = {"Module-Enum": "3400"}  # value used by the "Find en bolig" web component


def build_rows(cfg: dict, zones: monitor.Zones) -> list[dict]:
    criteria = monitor.api_json(SEARCH + "getDefaultCriteriaViewModel", headers=HEADERS)
    wanted = zones.postcodes()
    picked = 0
    for area in criteria["areas"]:
        area["selected"] = area["id"] in wanted
        picked += area["selected"]
    if not picked:
        return []
    criteria["rent"]["selectedTo"] = cfg["max_rent"]
    criteria["pageSize"], criteria["page"] = 500, 1
    result = monitor.api_json(SEARCH + "getDepartmentsOnSearch", headers=HEADERS, data=criteria)
    rows = []
    for dep in result["departments"]:
        codes = sorted({a["zipCode"] for a in dep.get("addresses") or [] if a.get("zipCode")})
        hits = [(c, zones.lookup(c)) for c in codes]
        hits = [(c, z) for c, z in hits if z]
        if not hits:
            continue
        tier = "core" if any(z["tier"] == "core" for _, z in hits) else "extended"
        rows.append({
            "tier": tier,
            "postcodes": ", ".join(c for c, _ in hits),
            "zones": ", ".join(sorted({z["zones"] for _, z in hits})),
            "estate": dep["name"],
            "company": dep["company"]["name"],
            "homes": dep.get("tenancyCount") or 0,
            "min_rent": dep.get("minRent"),
            "rooms": f"{dep.get('minRooms')}-{dep.get('maxRooms')}",
            "link": f"{monitor.BASE}/boligsoegende/afdelingspraesentation/"
                    f"?companyId={dep['company']['id']}&departmentId={dep['id']}",
        })
    rows.sort(key=lambda r: (r["tier"] != "core", r["min_rent"] or 0))
    return rows


def render(rows: list[dict], cfg: dict) -> str:
    out = [f"# Estates in your zones with homes up to {monitor.kr(cfg['max_rent'])}", "",
           "Waiting-list estates, not vacancies. 'Homes' counts the homes within the rent limit; "
           "'From' is the cheapest one.", "",
           "| Area | Postcode | Zones | Estate | Housing organisation | Homes | From | Rooms | Link |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        area = "Zone 1-2" if r["tier"] == "core" else "Adjacent"
        rent = monitor.kr(r["min_rent"]) if r["min_rent"] else "?"
        out.append(f"| {area} | {r['postcodes']} | {r['zones']} | {r['estate']} | {r['company']} | "
                   f"{r['homes']} | {rent} | {r['rooms']} | {r['link']} |")
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
    ap.add_argument("--out")
    args = ap.parse_args()
    cfg, zones = monitor.load_config(Path(args.config))
    text = render(build_rows(cfg, zones), cfg)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
