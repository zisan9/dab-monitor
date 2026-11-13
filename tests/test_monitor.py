import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402

CFG = {"max_rent": 7500, "preferred_rent": 7000, "failure_threshold": 3,
       "sources": {"ledige": True, "time_limited": True, "new_builds": True}}
TABLE = [
    {"from": 1000, "to": 1799, "tier": "core", "zones": "1", "area": "K", "border": False},
    {"from": 2300, "to": 2300, "tier": "core", "zones": "1 / 3", "area": "S", "border": True},
    {"from": 2610, "to": 2610, "tier": "extended", "zones": "32", "area": "Rødovre", "border": False},
]


def zones(extended=True, extra=()):
    return monitor.Zones(TABLE, list(extra), extended)


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

    def test_keys_are_stable_and_unique(self):
        again = monitor.parse_time_limited(TIME_LIMITED_HTML.replace("<br>", "<br />"))
        self.assertEqual([h["key"] for h in self.homes], [h["key"] for h in again])
        self.assertEqual(len({h["key"] for h in self.homes}), 6)


class RunTests(unittest.TestCase):
    def new_state(self):
        return {"initialized": False, "seen": {}, "failures": {}, "broken_alerted": {}, "heartbeat": ""}

    def go(self, state, fetchers, fail_send=False):
        sent = []

        def send(text):
            if fail_send:
                raise RuntimeError("nope")
            sent.append(text)

        ok = monitor.run(CFG, zones(), state, dry_run=False, send=send, fetchers=fetchers, log=lambda *_: None)
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
