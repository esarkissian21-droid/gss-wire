import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import digest  # noqa: E402

RULES = json.load(open(digest.RULES_PATH))
START = dt.date(2026, 11, 27)


def weeks(n_before, n_after):
    return [START + dt.timedelta(weeks=k) for k in range(-n_before, n_after)]


def make_prices(ws, ratio_fn, stock_fn=None):
    gc = {w: 4000.0 for w in ws}
    si = {w: 4000.0 / ratio_fn(i) for i, w in enumerate(ws)}
    p = {"GC=F": gc, "SI=F": si, "SPY": {w: 100.0 for w in ws}}
    for t in RULES["universes"]["fair"] + RULES["universes"]["watchlist_hindsight"]:
        p[t] = {w: (stock_fn(t, i) if stock_fn else 50.0) for i, w in enumerate(ws)}
    return p


class ForwardTests(unittest.TestCase):
    def test_nothing_logged_before_start_week(self):
        ws = weeks(60, 1)  # ends exactly at start week, ratio crosses 81 the week before start
        p = make_prices(ws, lambda i: 85.0 if i >= 59 else 78.0)
        ledger, fs, new, _ = digest.forward_step(RULES, p, {}, [], "h")
        self.assertEqual([r for r in new if r["kind"] == "ratio"], [])
        self.assertEqual(fs["lastWeek"], START.isoformat())

    def test_cross_logged_then_scored_at_13_weeks_only_when_reached(self):
        ws = weeks(60, 20)
        # ratio 78 until start+1, then 84 (cross 81 up), then drifts to 80 by week +14
        def r(i):
            k = i - 60
            if k < 1:
                return 78.0
            return 84.0 if k < 14 else 80.0
        p = make_prices(ws, r)
        ledger, fs, new, scored = digest.forward_step(RULES, p, {}, [], "h")
        x = [r for r in ledger if r["kind"] == "ratio"]
        self.assertEqual(len(x), 1)
        self.assertEqual((x[0]["dir"], x[0]["line"], x[0]["episode"]), ("up", 81, True))
        self.assertIn("13", x[0]["out"])
        self.assertAlmostEqual(x[0]["out"]["13"]["goldOz"], 84 / 80 - 1, places=3)
        self.assertTrue(x[0]["out"]["13"]["right"])
        self.assertNotIn("26", x[0]["out"])
        # idempotent: same data again adds nothing
        ledger2, fs2, new2, scored2 = digest.forward_step(RULES, p, fs, ledger, "h")
        self.assertEqual((len(new2), scored2), (0, 0))

    def test_whipsaw_counts_one_episode(self):
        ws = weeks(60, 10)
        seq = {1: 82, 2: 80, 3: 82, 4: 80, 5: 82}
        p = make_prices(ws, lambda i: float(seq.get(i - 60, 78 if i - 60 < 1 else 80)))
        ledger, *_ = digest.forward_step(RULES, p, {}, [], "h")
        x = [r for r in ledger if r["kind"] == "ratio" and r["line"] == 81]
        self.assertEqual(len(x), 3)
        self.assertEqual(sum(r["episode"] for r in x), 1)

    def test_big_week_and_chase_scoring(self):
        ws = weeks(60, 16)
        def s(t, i):
            k = i - 60
            if t != "AAPL":
                return 50.0
            return 50.0 if k < 2 else (60.0 if k < 15 else 55.0)  # +20% at week +2, then fades
        p = make_prices(ws, lambda i: 78.0, s)
        ledger, *_ = digest.forward_step(RULES, p, {}, [], "h")
        b = [r for r in ledger if r["kind"] == "bigweek"]
        self.assertEqual([(r["t"], r["group"]) for r in b], [("AAPL", "fair")])
        self.assertAlmostEqual(b[0]["out"]["13"]["excess"], 55 / 60 - 1, places=3)
        self.assertTrue(b[0]["out"]["13"]["right"])

    def test_momentum_logged_on_start_week(self):
        ws = weeks(60, 14)
        p = make_prices(ws, lambda i: 78.0, lambda t, i: 50.0 + (i if t in ("AAPL", "MSFT") else -i * 0.1))
        ledger, *_ = digest.forward_step(RULES, p, {}, [], "h")
        m = [r for r in ledger if r["kind"] == "momentum"]
        self.assertEqual(m[0]["week"], START.isoformat())
        self.assertTrue(m[0]["checks"]["AAPL"]["pass"])
        self.assertFalse(m[0]["checks"]["XOM"]["pass"])
        self.assertIn("13", m[0]["out"])
        self.assertTrue(m[0]["out"]["13"]["right"])

    def test_digest_text(self):
        ws = weeks(3, 0)
        p = make_prices(ws, lambda i: [80.0, 79.57, 85.85][i])
        cfg = json.load(open(os.path.join(os.path.dirname(digest.RULES_PATH), "..", "config.json")))
        msg = digest.build_digest(cfg, p, dt.date(2026, 11, 22), [], [], [])
        self.assertIn("Ratio 85.8", msg)
        self.assertIn("81 to 86", msg)
        self.assertIn("in gold terms −8.5%", msg)
        self.assertIn("no signals yet", msg)


    def test_frozen_forecasts_logged_and_brier_scored(self):
        pred = json.load(open(digest.PRED_PATH))
        ws = weeks(60, 14)
        p = make_prices(ws, lambda i: 92.0 if i - 60 < 13 else 85.0)  # in 90+ zone, ratio lower 13 weeks later
        ledger, *_ = digest.forward_step(RULES, p, {}, [], "h", pred)
        pr = [r for r in ledger if r["kind"] == "pred"]
        self.assertEqual(pr[0]["week"], START.isoformat())
        self.assertEqual(pr[0]["zone"], "90+")
        o = pr[0]["out"]["13"]
        self.assertTrue(o["lower"])
        self.assertAlmostEqual(o["brier"], (pr[0]["p"]["13"] - 1) ** 2, places=3)
        fs = digest.forward_summary(ledger)
        self.assertLess(fs["brier"]["13"]["model"], 0.25)

    def test_digest_shows_model_odds(self):
        digest.PRED = json.load(open(digest.PRED_PATH))
        ws = weeks(3, 0)
        p = make_prices(ws, lambda i: [80.0, 79.57, 85.85][i])
        cfg = json.load(open(os.path.join(os.path.dirname(digest.RULES_PATH), "..", "config.json")))
        msg = digest.build_digest(cfg, p, dt.date(2026, 11, 22), [], [], [])
        self.assertIn("Model odds ratio is lower in a year: 53%", msg)
        self.assertIn("thin evidence", msg)
        digest.PRED = None


if __name__ == "__main__":
    unittest.main()
