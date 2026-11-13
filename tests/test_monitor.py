import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402

dt = monitor.dt

CFG = {"max_rent": 7500, "preferred_rent": 7000, "near_miss_rent": 8500, "failure_threshold": 3,
       "daily_summary": False, "fast_sources": ["time_limited"],
       "sources": {"ledige": True, "time_limited": True, "new_builds": True}}
TABLE = [
    {"from": 1000, "to": 1799, "tier": "core", "zones": "1", "area": "K", "border": False},
    {"from": 2300, "to": 2300, "tier": "core", "zones": "1 / 3", "area": "S", "border": True},
    {"from": 2610, "to": 2610, "tier": "extended", "zones": "32", "area": "Rødovre", "border": False},
    {"from": 2000, "to": 2999, "tier": "wide", "zones": "", "area": "Suburbs", "border": False},
]


def zones(extended=True, extra=(), wide=False):
    return monitor.Zones(TABLE, list(extra), extended, wide, ["time_limited"])


def tenancy(tid=1, zip_code="2300", rent=6500.0):
    return {"companyId": 12, "departmentId": 2, "tenancyId": tid, "rooms": 2, "sqm": 60.0,
            "rentNet": rent, "deposit": 20000.0,
            "address": {"street": "Greisvej", "location": "24", "zipCode": zip_code, "city": "København S"},
            "rentalNote": "Ledig", "tenancyType": "Etagebolig", "apartmentType": "Lejlighed",
            "departmentName": "Frydenspark"}


TIME_LIMITED_HTML = """
<p>Intro text without any postcode.</p>
<h3>Lærkevej, Gørlev</h3>
<p>Lærkevej 3<br>4270 Høng<br>Husleje: 4.038 kr.<br>Indskud: 18.576 kr.<br>Rækkehus, 3-rums, 84 kvm.<br>
<strong>Udlejningsperiode: 15. april - 31. marts 2027</strong></p>
<p>Drosselvej 15<br>4270 Høng<br>Husleje: 4.474,50kr.<br>Indskud: 20.736kr.</p>
<p><a href="https://example.invalid/x">Læs mere og se billeder af afdelingen</a></p>
<h3>Testgården, København</h3>
<p>Solsiden 3D<br>2300 København S<br>3 rums rækkehus, 67 kvm<br>Husleje 4.762 kr.<br>Indskud 13.000 kr.<br>
<strong>Udlejningsperiode: 1. august 2026 –31. marts 2027</strong></p>
<p>Paltholmpark 5<br>Postnummer og by 1620 København V<br>Indskud: 18.912 kroner<br>Husleje: 7.229 kroner<br>Køkken: 600 kroner</p>
<p>Dyrtvej 1<br>1620 København V<br>Husleje: 9.100 kr.</p>
<p>Ukendtvej 9<br>1620 København V<br>3 rum, pris oplyses senere</p>
"""

MULTI_HOME_HTML = (
    "<h3>Ullerupvænge, Hundested</h3>"
    "<p>Vængevej 1B, 3390 Hundested<br>Husleje inkl. forbrug 6.789,00 Kr.<br>Indskud 4.000,00 Kr.<br>"
    "Udlejningsperiode: 01-09-2026 - 31-03-2028<br>"
    "Vængevej 9E<br>Husleje: 6.360,00 kr.<br>Indskud: 4.000 kr.<br>Udlejningsperiode: nu - 31-03-2028<br>"
    "UDLEJES EFTER FLEKSIBLE REGLER<br><a href=\"x\">Læs mere</a></p>")


class ZoneTests(unittest.TestCase):
    def test_core_extended_and_outside(self):
        z = zones()
        self.assertEqual(z.lookup("1620")["tier"], "core")
        self.assertEqual(z.lookup(2610)["tier"], "extended")
        self.assertIsNone(z.lookup("7700"))
        self.assertIsNone(z.lookup(None))
        self.assertIsNone(z.lookup("abc"))

    def test_extended_switch_and_extra(self):
        self.assertIsNone(zones(extended=False).lookup("2610"))
        self.assertEqual(zones(extra=[7700]).lookup("7700")["tier"], "core")

    def test_wide_tier_applies_only_to_its_sources(self):
        self.assertIsNone(zones().lookup("2830", "time_limited"))
        z = zones(wide=True)
        self.assertEqual(z.lookup("2830", "time_limited")["tier"], "wide")
        self.assertIsNone(z.lookup("2830", "ledige"))
        self.assertEqual(z.lookup("2610", "ledige")["tier"], "extended")  # narrow row wins
        self.assertIsNone(zones(extended=False, wide=True).lookup("2610", "time_limited"))
        self.assertNotIn(2830, z.postcodes())
        self.assertIn(2830, z.postcodes(wide=True))
        self.assertIn(2610, z.postcodes())


class TimeTests(unittest.TestCase):
    def test_copenhagen_time(self):
        self.assertEqual(monitor.copenhagen_now(dt.datetime(2026, 11, 13, 9, 0)).hour, 10)  # winter
        self.assertEqual(monitor.copenhagen_now(dt.datetime(2026, 7, 1, 9, 0)).hour, 11)    # summer
        self.assertEqual(monitor.copenhagen_now(dt.datetime(2026, 3, 29, 0, 59)).hour, 1)
        self.assertEqual(monitor.copenhagen_now(dt.datetime(2026, 3, 29, 1, 0)).hour, 3)
        self.assertEqual(monitor.copenhagen_now(dt.datetime(2026, 10, 25, 1, 0)).hour, 2)

    def test_office_hours(self):
        self.assertTrue(monitor.in_office_hours({}, dt.datetime(2026, 11, 13, 10, 0)))    # Friday
        self.assertFalse(monitor.in_office_hours({}, dt.datetime(2026, 11, 13, 18, 0)))
        self.assertFalse(monitor.in_office_hours({}, dt.datetime(2026, 11, 14, 10, 0)))   # Saturday


class FilterTests(unittest.TestCase):
    def test_rent_cap(self):
        z = zones()
        self.assertIsNotNone(monitor.match(monitor.parse_ledige(tenancy(rent=7500.0)), CFG, z))
        self.assertIsNone(monitor.match(monitor.parse_ledige(tenancy(rent=7501.0)), CFG, z))
        self.assertIsNone(monitor.match(monitor.parse_ledige(tenancy(zip_code="7700")), CFG, z))

    def test_message(self):
        home = monitor.parse_ledige(tenancy(rent=7200.0))
        text = monitor.format_home(home, zones().lookup("2300"), CFG)
        self.assertIn("Greisvej 24", text)
        self.assertIn("7.200 kr/month (above 7.000 kr)", text)
        self.assertIn("check the address", text)
        self.assertIn("?ten=12_2_1", text)


class TimeLimitedTests(unittest.TestCase):
    def setUp(self):
        self.homes = monitor.parse_time_limited(TIME_LIMITED_HTML)

    def test_parses_varied_formats(self):
        self.assertEqual(len(self.homes), 6)
        by = {h["title"]: h for h in self.homes}
        self.assertEqual(by["Lærkevej 3"]["postcode"], "4270")
        self.assertEqual(by["Lærkevej 3"]["rent"], 4038.0)
        self.assertEqual(by["Lærkevej 3"]["deposit"], 18576.0)
        self.assertEqual(by["Lærkevej 3"]["estate"], "Lærkevej, Gørlev")
        self.assertEqual(by["Drosselvej 15"]["rent"], 4474.5)
        self.assertEqual(by["Solsiden 3D"]["rent"], 4762.0)
        self.assertEqual(by["Solsiden 3D"]["city"], "København S")
        self.assertEqual(by["Paltholmpark 5"]["postcode"], "1620")
        self.assertEqual(by["Paltholmpark 5"]["rent"], 7229.0)
        self.assertIsNone(by["Ukendtvej 9"]["rent"])

    def test_matching(self):
        z = zones()
        got = sorted(h["title"] for h in self.homes if monitor.match(h, CFG, z))
        # in area and affordable, plus the one whose rent could not be read
        self.assertEqual(got, ["Paltholmpark 5", "Solsiden 3D", "Ukendtvej 9"])

    def test_several_homes_in_one_paragraph(self):
        homes = monitor.parse_time_limited(MULTI_HOME_HTML)
        self.assertEqual([h["title"] for h in homes], ["Vængevej 1B", "Vængevej 9E"])
        self.assertEqual([h["rent"] for h in homes], [6789.0, 6360.0])
        self.assertEqual(homes[1]["postcode"], "3390")  # taken from the home above it
        self.assertEqual(homes[0]["period"], "01-09-2026 - 31-03-2028")
        self.assertEqual([h["flexible"] for h in homes], [False, True])

    def test_end_dates(self):
        cases = {"1. okt. 26 - 15. juni 27": (2027, 6, 15), "nu - 31.08.2027": (2027, 8, 31),
                 "LEDIG NU til 30-06-2027": (2027, 6, 30), "1.11-2026-15.01.2027": (2027, 1, 15),
                 "15. april - 31. marts 2027": (2027, 3, 31), "16.11.26 til 31.03.28": (2028, 3, 31),
                 "1. nov.26 - 15. juni 27": (2027, 6, 15), "01.12.2026 – 30.04-2027": (2027, 4, 30),
                 "Klar til indflytning - 3. maj 2027": (2027, 5, 3), "Frem til 31.01.2027.": (2027, 1, 31)}
        for text, ymd in cases.items():
            self.assertEqual(monitor.end_date(text), dt.date(*ymd), text)
        self.assertIsNone(monitor.end_date("efter aftale"))
        self.assertIsNone(monitor.end_date(""))

    def test_minimum_end_date_filter(self):
        cfg = dict(CFG, time_limited_min_end="2027-04-01")
        z = zones()
        got = sorted(h["title"] for h in self.homes if monitor.match(h, cfg, z))
        # Solsiden 3D ends 31 March 2027 and drops out; homes with no readable end date stay
        self.assertEqual(got, ["Paltholmpark 5", "Ukendtvej 9"])
        home = monitor.parse_ledige(tenancy())
        self.assertIsNotNone(monitor.match(home, cfg, z))  # other sources are not affected

    def test_estate_links_and_period_variant(self):
        by = {h["title"]: h for h in self.homes}
        self.assertEqual(by["Lærkevej 3"]["estate_link"], "https://example.invalid/x")
        self.assertEqual(by["Drosselvej 15"]["estate_link"], "https://example.invalid/x")
        self.assertEqual(by["Solsiden 3D"]["estate_link"], "")
        homes = monitor.parse_time_limited(
            "<h3>Klosterparken</h3><p>Vej 1<br>4400 Kalundborg<br>Husleje: 4.585 kroner<br>"
            "TIDSBEGRÆNSET Periode: 15-11-2026 – 06-12-2027</p><p><a href='/x/y/'>Læs mere</a></p>")
        self.assertEqual(homes[0]["ends"], dt.date(2027, 12, 6))
        self.assertEqual(homes[0]["estate_link"], "https://dab-lejerbo.dk/x/y/")
        text = monitor.format_home(homes[0], {"tier": "wide", "area": "X", "zones": ""}, CFG)
        self.assertIn("Estate page and photos: https://dab-lejerbo.dk/x/y/", text)
        self.assertIn("google.com/maps", text)
        self.assertIn('<a href="https://dab-lejerbo.dk/x/y/">', monitor.short_line(homes[0]))

    def test_alert_has_apply_link(self):
        home = next(h for h in self.homes if h["title"] == "Solsiden 3D")
        text = monitor.format_home(home, zones().lookup("2300"), CFG)
        self.assertIn("First come, first served", text)
        self.assertIn(monitor.TIME_LIMITED_FORM, text)
        self.assertIn("Period: 1. august 2026 –31. marts 2027", text)

    def test_keys_are_stable_and_unique(self):
        again = monitor.parse_time_limited(TIME_LIMITED_HTML.replace("<br>", "<br />"))
        self.assertEqual([h["key"] for h in self.homes], [h["key"] for h in again])
        self.assertEqual(len({h["key"] for h in self.homes}), 6)


class RunTests(unittest.TestCase):
    def new_state(self):
        return {"initialized": False, "seen": {}, "failures": {}, "broken_alerted": {}, "heartbeat": "",
                "daily": ""}

    def go(self, state, fetchers, fail_send=False, cfg=CFG, **kw):
        sent = []

        def send(text):
            if fail_send:
                raise RuntimeError("nope")
            sent.append(text)

        kw.setdefault("now", dt.datetime(2026, 11, 13, 10, 0))
        ok = monitor.run(cfg, zones(), state, dry_run=False, send=send, fetchers=fetchers,
                         log=lambda *_: None, **kw)
        return ok, sent

    def test_alert_once_then_again_after_relisting(self):
        state = self.new_state()
        feed = [monitor.parse_ledige(tenancy(1)), monitor.parse_ledige(tenancy(2, zip_code="7700"))]
        ok, sent = self.go(state, {"ledige": lambda: feed})
        self.assertTrue(ok)
        self.assertEqual(len(sent), 2)  # the home + the "monitor is live" message
        self.assertIn("Greisvej", sent[0])
        self.assertIn("monitor is live", sent[1])
        self.assertTrue(state["initialized"])

        ok, sent = self.go(state, {"ledige": lambda: feed})
        self.assertEqual(sent, [])

        self.go(state, {"ledige": lambda: []})
        ok, sent = self.go(state, {"ledige": lambda: feed})
        self.assertEqual(len(sent), 1)

    def test_failed_send_is_retried_next_run(self):
        state = self.new_state()
        state["initialized"] = True
        feed = [monitor.parse_ledige(tenancy(1))]
        ok, _ = self.go(state, {"ledige": lambda: feed}, fail_send=True)
        self.assertFalse(ok)
        self.assertEqual(state["seen"]["ledige"], {})
        ok, sent = self.go(state, {"ledige": lambda: feed})
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)

    def test_many_new_homes_arrive_as_one_list(self):
        state = self.new_state()
        state["initialized"] = True
        feed = [monitor.parse_ledige(tenancy(i, rent=5000.0 + i)) for i in range(1, 9)]
        ok, sent = self.go(state, {"ledige": lambda: feed})
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        self.assertIn("8 new vacant homes", sent[0])
        self.assertEqual(len(state["seen"]["ledige"]), 8)
        ok, sent = self.go(state, {"ledige": lambda: feed})
        self.assertEqual(sent, [])

    def test_fast_check_only_first_come_sources_and_no_failure_count(self):
        state = self.new_state()
        calls = []

        def boom():
            raise RuntimeError("down")

        home = next(h for h in monitor.parse_time_limited(TIME_LIMITED_HTML) if h["title"] == "Solsiden 3D")
        fetchers = {"ledige": lambda: calls.append("ledige") or [], "time_limited": lambda: [home]}
        ok, sent = self.go(state, fetchers, fast=True)   # no baseline yet: does nothing
        self.assertEqual((sent, calls, state["seen"]), ([], [], {}))
        state["initialized"] = True
        ok, sent = self.go(state, fetchers, fast=True)
        self.assertEqual(len(sent), 1)
        self.assertEqual(calls, [])
        for _ in range(5):
            ok, sent = self.go(state, {"time_limited": boom}, fast=True)
            self.assertFalse(ok)
            self.assertEqual(sent, [])
        self.assertFalse(state["failures"].get("time_limited"))
        self.assertEqual(len(state["seen"]["time_limited"]), 1)

    def test_daily_summary_once_a_day(self):
        cfg = dict(CFG, daily_summary=True, daily_summary_hour=8)
        state = self.new_state()
        state["initialized"] = True
        state["seen"]["ledige"] = {"12_2_1": "2026-01-01"}
        feed = [monitor.parse_ledige(tenancy(1)), monitor.parse_ledige(tenancy(2, rent=8000.0))]
        src = {"ledige": lambda: feed}
        ok, sent = self.go(state, src, cfg=cfg, now=dt.datetime(2026, 11, 13, 7, 0))
        self.assertEqual(sent, [])
        ok, sent = self.go(state, src, cfg=cfg, now=dt.datetime(2026, 11, 13, 8, 5))
        self.assertEqual(len(sent), 1)
        self.assertIn("Daily summary, 13 November", sent[0])
        self.assertIn("2 listed, 1 match", sent[0])
        self.assertIn("Just over budget", sent[0])
        ok, sent = self.go(state, src, cfg=cfg, now=dt.datetime(2026, 11, 13, 9, 0))
        self.assertEqual(sent, [])
        ok, sent = self.go(state, src, cfg=cfg, now=dt.datetime(2026, 11, 14, 9, 0))
        self.assertEqual(len(sent), 1)

    def test_broken_alert_once_and_recovery(self):
        state = self.new_state()
        state["initialized"] = True
        state["seen"]["ledige"] = {"12_2_1": "2026-01-01"}

        def boom():
            raise RuntimeError("404 page")

        all_sent = []
        for _ in range(5):
            ok, sent = self.go(state, {"ledige": boom})
            self.assertFalse(ok)
            all_sent += sent
        self.assertEqual(len(all_sent), 1)
        self.assertIn("Monitor problem", all_sent[0])
        self.assertEqual(state["seen"]["ledige"], {"12_2_1": "2026-01-01"})  # kept during outage

        ok, sent = self.go(state, {"ledige": lambda: [monitor.parse_ledige(tenancy(1))]})
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        self.assertIn("working again", sent[0])

    def test_new_builds_seeded_silently_then_alerted(self):
        orig = monitor.new_build_title
        monitor.new_build_title = lambda url: "Nyt projekt"
        try:
            state = self.new_state()
            a = {"source": "new_builds", "key": "u/a", "link": "u/a"}
            b = {"source": "new_builds", "key": "u/b", "link": "u/b"}
            ok, sent = self.go(state, {"new_builds": lambda: [a]})
            self.assertEqual(len(sent), 1)
            self.assertIn("monitor is live", sent[0])
            ok, sent = self.go(state, {"new_builds": lambda: [a, b]})
            self.assertEqual(len(sent), 1)
            self.assertIn("Nyt projekt", sent[0])
        finally:
            monitor.new_build_title = orig


if __name__ == "__main__":
    unittest.main()
