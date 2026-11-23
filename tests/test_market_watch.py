import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import market_watch as mw  # noqa: E402

NY = mw.NY


def mw_dump(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f)
OPEN_TS = dt.datetime(2026, 11, 24, 16, 30, tzinfo=dt.timezone.utc)  # Tue 11:30 a.m. New York


def chart(closes, vols, last_day, price=None):
    """Daily bars ending on last_day (a date), one per weekday going back."""
    days, d = [], last_day
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= dt.timedelta(days=1)
    days.reverse()
    ts = [int(dt.datetime(x.year, x.month, x.day, 15, 0, tzinfo=NY).timestamp()) for x in days]
    return json.dumps({"chart": {"result": [{"meta": {"regularMarketPrice": price or closes[-1]}, "timestamp": ts,
                                             "indicators": {"quote": [{"close": closes, "volume": vols}]}}]}}).encode()


ODDS = {"universe": 98, "span": ["2005-01-03", "2026-11-20"], "buckets": {
    "drop6": {"above200": {"63": {"n": 812, "pBeat": 53.0, "baseBeat": 49.0, "t": 1.1, "reliable": False}}},
    "jump3": {"all": {"63": {"n": 3000, "pBeat": 51.0, "baseBeat": 49.0, "t": 2.5, "reliable": True}}},
}}
GRADE = {"asOf": "2026-11-22", "version": "v1", "list": {"SSD": {"name": "Simpson Manufacturing"}, "TRV": {"name": "Travelers"}},
         "added": [], "removed": {}, "previousAsOf": None}


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = (mw.ODDS_PATH, mw.GRADE_PATH)
        mw.ODDS_PATH = os.path.join(self.dir, "odds.json")
        mw.GRADE_PATH = os.path.join(self.dir, "grade.json")
        mw_dump(ODDS, mw.ODDS_PATH)
        mw_dump(GRADE, mw.GRADE_PATH)
        self.cfg = {"companies": [{"ticker": "NVDA"}], "shortlist": {"SSD": {"tier": "A", "sell": "margin below 18%"}}}
        today = OPEN_TS.astimezone(NY).date()
        up = [100 + i * 0.2 for i in range(259)]  # steady uptrend, last close ~151.6
        self.charts = {
            "SPY": chart(up + [up[-1] * 0.997], [1e6] * 260, today),
            "SSD": chart(up + [up[-1] * 0.935], [1e6] * 259 + [2.0e6], today),  # -6.5%, heavy volume by 11:30
            "TRV": chart(up + [up[-1] * 1.004], [1e6] * 260, today),
            "NVDA": chart(up + [up[-1] * 1.01], [1e6] * 260, today),
        }

    def tearDown(self):
        mw.ODDS_PATH, mw.GRADE_PATH = self.old

    def fetch(self, url, ua):
        sym = url.split("/chart/")[1].split("?")[0]
        return self.charts[sym]

    def test_closed_market_only_announces_grade_list(self):
        state = {}
        sat = dt.datetime(2026, 11, 28, 17, 0, tzinfo=dt.timezone.utc)
        pushes, logs = mw.run_watch(self.cfg, state, self.fetch, sat, [])
        self.assertEqual([p[0] for p in pushes], ["Grade A list is live"])
        self.assertEqual(logs, [])
        pushes, _ = mw.run_watch(self.cfg, state, self.fetch, sat, [])
        self.assertEqual(pushes, [])  # announced once

    def test_big_drop_alert_explains_and_logs_prediction(self):
        state = {"watch": {"gradeSeen": "2026-11-22"}}
        recent = [{"asset": "SSD", "kind": "filing", "title": "8-K: guidance update", "date": "2026-11-24"}]
        pushes, logs = mw.run_watch(self.cfg, state, self.fetch, OPEN_TS, recent)
        self.assertEqual(len(pushes), 1)
        title, msg, prio, tags, click = pushes[0]
        self.assertIn("SSD −6.5%", title)
        self.assertIn("Volume so far", msg)
        self.assertIn("about the company", msg)
        self.assertIn("Filing: 8-K: guidance update", msg)
        self.assertIn("53% of the time vs 49%", msg)
        self.assertIn("Not a reliable edge", msg)
        self.assertIn("Shortlist Tier A", msg)
        self.assertIn("Grade A: still passes", msg)
        self.assertEqual(prio, 4)
        preds = state["watch"]["preds"]
        self.assertEqual(len(preds), 1)
        self.assertEqual((preds[0]["t"], preds[0]["bucket"], preds[0]["p"]), ("SSD", "drop6", 0.53))
        # same level again: no repeat
        pushes, _ = mw.run_watch(self.cfg, state, self.fetch, OPEN_TS + dt.timedelta(minutes=20), recent)
        self.assertEqual(pushes, [])

    def test_no_news_says_so_and_uses_lookup(self):
        state = {"watch": {"gradeSeen": "2026-11-22"}}
        asked = []

        def extra(sym, name):
            asked.append((sym, name))
            return []
        pushes, _ = mw.run_watch(self.cfg, state, self.fetch, OPEN_TS, [], extra)
        self.assertEqual(asked, [("SSD", "Simpson Manufacturing")])
        self.assertIn("Nothing in filings", pushes[0][1])

    def test_prediction_scored_when_due(self):
        today = OPEN_TS.astimezone(NY).date()
        state = {"watch": {"gradeSeen": "2026-11-22", "preds": [
            {"id": "x", "t": "TRV", "d": "2026-06-01", "px": 100.0, "spy": 100.0, "bucket": "drop3", "p": 0.6,
             "due": "2026-09-01"}]}}
        mw.run_watch(self.cfg, state, self.fetch, OPEN_TS, [])
        p = state["watch"]["preds"][0]
        self.assertIn("out", p)
        self.assertEqual(state["watch"]["score"]["n"], 1)
        self.assertLessEqual(p["out"]["end"], today.isoformat())

    def test_grade_changes_announced(self):
        g = dict(GRADE, asOf="2026-11-29", added=["CINF"], removed={"TRV": ["price trend not up"]}, previousAsOf="2026-11-22")
        mw_dump(g, mw.GRADE_PATH)
        state = {"watch": {"gradeSeen": "2026-11-22"}}
        sat = dt.datetime(2026, 11, 29, 20, 0, tzinfo=dt.timezone.utc)
        pushes, _ = mw.run_watch(self.cfg, state, self.fetch, sat, [])
        self.assertEqual(len(pushes), 1)
        self.assertIn("New: CINF", pushes[0][1])
        self.assertIn("Out: TRV (price trend not up)", pushes[0][1])

    def test_holiday_no_alerts(self):
        # charts whose last bar is yesterday -> SPY didn't trade today
        y = OPEN_TS.astimezone(NY).date() - dt.timedelta(days=1)
        up = [100 + i * 0.2 for i in range(260)]
        self.charts = {k: chart(up, [1e6] * 260, y) for k in ("SPY", "SSD", "TRV", "NVDA")}
        state = {"watch": {"gradeSeen": "2026-11-22"}}
        pushes, logs = mw.run_watch(self.cfg, state, self.fetch, OPEN_TS, [])
        self.assertEqual((pushes, logs), ([], []))


class GradeATests(unittest.TestCase):
    def test_rules(self):
        import screens
        good = {"screens": {"Quality": "pass"}, "pe": 12, "de": 0.2, "momentum": True, "chg52": 20,
                "flags": [], "oneOff": False, "revenue": 1e9}
        self.assertEqual(screens.grade_a(good), (True, []))
        self.assertFalse(screens.grade_a(dict(good, pe=25.4))[0])
        self.assertIn("up 96% in a year (chasing)", screens.grade_a(dict(good, chg52=96))[1])
        self.assertIn("debt/equity unknown", screens.grade_a(dict(good, de=None))[1])
        rep = screens.grade_a_report({"A": good, "B": dict(good, momentum=False)},
                                     {"asOf": "2026-11-22", "list": {"B": {}, "C": {}}}, dt.date(2026, 11, 29))
        self.assertEqual(list(rep["list"]), ["A"])
        self.assertEqual(rep["added"], ["A"])
        self.assertEqual(rep["removed"]["B"], ["price trend not up"])
        self.assertEqual(rep["removed"]["C"], ["no longer in the data"])


if __name__ == "__main__":
    unittest.main()
