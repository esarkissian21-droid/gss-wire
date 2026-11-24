import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import screens  # noqa: E402

TODAY = dt.date(2026, 11, 22)


class FakeSEC:
    """frames[(concept, period)] = {cik: val}"""

    def __init__(self):
        self.f = {}
        self.calls = []

    def put(self, concept, period, cik, val):
        self.f.setdefault((concept, period), {})[cik] = val

    def __call__(self, url):
        self.calls.append(url)
        if url == screens.TICKERS:
            return json.dumps({"0": {"cik_str": 1, "ticker": "GOOD"}, "1": {"cik_str": 2, "ticker": "BANK"},
                               "2": {"cik_str": 3, "ticker": "TINY"}}).encode()
        parts = url.split("/frames/")[1].split("/")
        concept, period = parts[1], parts[3].replace(".json", "")
        d = self.f.get((concept, period))
        if d is None:
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "nf", None, None)
        return json.dumps({"data": [{"cik": c, "val": v} for c, v in d.items()]}).encode()


T0 = 1764000000  # a timestamp ~Nov 2025


def fake_px(url):
    sym = url.split("/chart/")[1].split("?")[0]
    base = {"GOOD": 100.0, "BANK": 50.0, "TINY": 5.0, "SPY": 600.0}[sym]
    # rising with a small wiggle: above 200-day average, up on the year; GOOD moves 1.5x the market's wiggle
    wig = {"GOOD": 1.5, "BANK": 0.5, "TINY": 1.0, "SPY": 1.0}[sym]
    closes = [base * (0.8 + 0.2 * i / 299) * (1 + 0.01 * wig * (1 if i % 2 else -1)) for i in range(300)]
    ts = [T0 + 86400 * i for i in range(300)]
    return json.dumps({"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": closes}]}}]}}).encode()


def build():
    s = FakeSEC()
    # GOOD (cik 1): growing industrial. Quarterly EPS 2.0 now vs 1.5 a year ago; Q4s only via annual.
    for (y, q), eps, rev, ni in [((2025, 4), None, None, None), ((2026, 1), 1.8, 3e9, 600e6), ((2026, 2), 1.9, 3.1e9, 620e6),
                                 ((2026, 3), 2.0, 3.2e9, 650e6), ((2025, 1), 1.3, 2.4e9, 450e6), ((2025, 2), 1.4, 2.5e9, 470e6),
                                 ((2025, 3), 1.5, 2.5e9, 500e6), ((2024, 1), 1.0, 2e9, 330e6), ((2024, 2), 1.1, 2e9, 350e6),
                                 ((2024, 3), 1.2, 2.1e9, 380e6)]:
        if eps is not None:
            s.put("EarningsPerShareDiluted", f"CY{y}Q{q}", 1, eps)
            s.put("Revenues", f"CY{y}Q{q}", 1, rev)
            s.put("NetIncomeLoss", f"CY{y}Q{q}", 1, ni)
    s.put("EarningsPerShareDiluted", "CY2025", 1, 1.3 + 1.4 + 1.5 + 1.7)
    s.put("Revenues", "CY2025", 1, 2.4e9 + 2.5e9 + 2.5e9 + 2.9e9)
    s.put("NetIncomeLoss", "CY2025", 1, 450e6 + 470e6 + 500e6 + 560e6)
    s.put("EarningsPerShareDiluted", "CY2024", 1, 1.0 + 1.1 + 1.2 + 1.3)
    s.put("Revenues", "CY2024", 1, 2e9 + 2e9 + 2.1e9 + 2.2e9)
    s.put("NetIncomeLoss", "CY2024", 1, 330e6 + 350e6 + 380e6 + 400e6)
    s.put("EarningsPerShareDiluted", "CY2020", 1, 3.0)
    for per in ("CY2026Q3I", "CY2025Q3I"):
        s.put("StockholdersEquity", per, 1, 10e9)
        s.put("AssetsCurrent", per, 1, 6e9)
        s.put("LiabilitiesCurrent", per, 1, 2e9)
        s.put("LongTermDebt", per, 1, 2e9)
    s.put("EntityCommonStockSharesOutstanding", "CY2026Q3I", 1, 325e6)
    # BANK (cik 2): no current ratio, no debt tags -> D/E unknown, can't pass Value/Quality
    for (y, q) in [(2026, 1), (2026, 2), (2026, 3), (2025, 1), (2025, 2), (2025, 3), (2024, 1), (2024, 2), (2024, 3)]:
        s.put("RevenuesNetOfInterestExpense", f"CY{y}Q{q}", 2, 1e9)
        s.put("NetIncomeLoss", f"CY{y}Q{q}", 2, 300e6)
        s.put("EarningsPerShareDiluted", f"CY{y}Q{q}", 2, 1.0)
    for c, v in (("RevenuesNetOfInterestExpense", 4e9), ("NetIncomeLoss", 1.2e9), ("EarningsPerShareDiluted", 4.0)):
        s.put(c, "CY2025", 2, v)
        s.put(c, "CY2024", 2, v)
    s.put("StockholdersEquity", "CY2026Q3I", 2, 5e9)
    # TINY (cik 3): too small
    s.put("Revenues", "CY2026Q3", 3, 1e6)
    return s


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.sec = build()
        self.res = screens.run(self.sec, fake_px, TODAY, always=("BANK",), sleep=lambda s: None)
        self.g = self.res["rows"]["GOOD"]

    def test_period_choice(self):
        self.assertEqual(screens.quarter_periods(TODAY), (2026, 3))
        self.assertEqual(screens.quarter_periods(dt.date(2026, 11, 10)), (2026, 2))  # Q3 10-Qs not all in yet
        self.assertEqual(screens.quarter_periods(dt.date(2027, 1, 5)), (2026, 3))

    def test_ttm_uses_derived_q4(self):
        # TTM EPS = Q4'25 (derived 1.7) + 1.8 + 1.9 + 2.0 = 7.4 ; previous TTM = Q4'24 1.3 + 1.3+1.4+1.5 = 5.5
        self.assertAlmostEqual(self.g["epsTTM"], 7.4, places=2)
        self.assertAlmostEqual(self.g["epsYr"], 100 * (7.4 / 5.5 - 1), places=1)
        self.assertAlmostEqual(self.g["epsQ"], 100 * (2.0 / 1.5 - 1), places=1)
        self.assertAlmostEqual(self.g["salesQ"], 100 * (3.2 / 2.5 - 1), places=1)

    def test_ratios_and_price_metrics(self):
        self.assertAlmostEqual(self.g["de"], 0.2, places=2)
        self.assertAlmostEqual(self.g["cr"], 3.0, places=2)
        ni_ttm = 560e6 + 600e6 + 620e6 + 650e6
        self.assertAlmostEqual(self.g["roe"], 100 * ni_ttm / 10e9, places=1)
        self.assertAlmostEqual(self.g["pe"], self.g["price"] / 7.4, places=1)
        self.assertAlmostEqual(self.g["pb"], self.g["price"] * 325e6 / 10e9, places=2)
        self.assertTrue(self.g["momentum"])
        self.assertAlmostEqual(self.g["eps5y"], 100 * ((5.9 / 3.0) ** 0.2 - 1), places=1)

    def test_screens(self):
        self.assertEqual(self.g["screens"]["Value"], "fail")    # P/E 13.5 ok, P/B 3.25 fails
        self.assertIn("GOOD", self.res["passes"]["Quality"])
        self.assertIn("GOOD", self.res["passes"]["Growth"])

    def test_no_debt_label_is_unknown_not_zero(self):
        self.assertIsNone(screens.de_ratio(None, 5e9))
        self.assertEqual(screens.de_ratio(0.0, 5e9), 0.0)

    def test_late_filer_falls_back_a_quarter(self):
        s = build()
        for k in [k for k in s.f if k[1] == "CY2026Q3"]:
            s.f[k].pop(1, None)
        res = screens.run(s, fake_px, TODAY, sleep=lambda x: None)
        self.assertEqual(res["rows"]["GOOD"]["period"], "CY2026Q2")

    def test_bank_unknowns_and_small_skipped(self):
        b = self.res["rows"]["BANK"]
        self.assertIsNone(b["de"])
        self.assertIsNone(b["cr"])
        self.assertNotEqual(b["screens"]["Quality"], "pass")
        self.assertNotIn("TINY", self.res["rows"])


class StrategyTests(unittest.TestCase):
    SPEC = {"strategies": [{"id": "q", "name": "Quality test", "description": "", "createdOn": "2026-11-22",
                            "startDate": "2026-11-22", "prefilters": [["revenue", ">=", 5e9], ["roe", ">", 12]],
                            "filters": [["mktCap", ">=", 1e9]], "rank": [["pb", "asc", 1], ["beta", "asc", 1]],
                            "top": 5, "horizonsWeeks": [13]}]}

    def test_beta_math(self):
        import strategies_engine as se
        spy = {f"d{i:03d}": 100 * (1 + 0.01 * (1 if i % 2 else -1)) for i in range(200)}
        stock = {k: 100 * (1 + 0.02 * (1 if int(k[1:]) % 2 else -1)) for k in spy}
        self.assertAlmostEqual(se.raw_beta(stock, spy), 2.0, places=1)
        self.assertAlmostEqual(se.beta(stock, spy), 0.6 * 2.0 + 0.4, places=1)

    def test_strategy_picks_logged_and_scored_later(self):
        import strategies_engine as se
        res = screens.run(build(), fake_px, TODAY, sleep=lambda s: None, spec=self.SPEC, ledger=[])
        st_ = res["strategies"]["q"]
        self.assertTrue(st_["live"])
        self.assertEqual([p["t"] for p in st_["picks"]], ["GOOD"])  # BANK: revenue too small for prefilter (4e9)
        self.assertIsNotNone(st_["picks"][0]["beta"])
        led = res["_ledger"]
        self.assertEqual(len(led), 1)
        self.assertEqual(led[0]["picks"][0]["t"], "GOOD")
        # score 13 weeks later with synthetic histories: pick +10%, SPY +5%
        e = led[0]
        d0 = dt.date.fromisoformat(e["date"])
        d1 = d0 + dt.timedelta(weeks=13)
        e["picks"][0]["px"], e["spyAt"] = 100.0, 600.0
        hist = {"GOOD": {d1.isoformat(): 110.0}}
        spy = {d1.isoformat(): 630.0}
        led2, new, scored = se.log_and_score({"strategies": []}, {}, d1, hist, spy, led)
        self.assertEqual(scored, 1)
        self.assertAlmostEqual(led2[0]["out"]["13"]["meanExcess"], 110 / 100 / (630 / 600) - 1, places=4)

    def test_sector_cap(self):
        import strategies_engine as se
        rows = {f"I{i}": {"pb": 1 + i / 10, "sic": "6331", "industry": "Fire, marine & casualty insurance"} for i in range(5)}
        rows["X"] = {"pb": 5.0, "sic": "3570", "industry": "Computers"}
        strat = {"rank": [["pb", "asc", 1]], "top": 4, "maxPerSector": 3}
        self.assertEqual([p["t"] for p in se.pick(strat, rows)], ["I0", "I1", "I2", "X"])

    def test_before_start_date_no_logging(self):
        spec = json.loads(json.dumps(self.SPEC))
        spec["strategies"][0]["startDate"] = "2026-11-29"
        res = screens.run(build(), fake_px, TODAY, sleep=lambda s: None, spec=spec, ledger=[])
        self.assertEqual(res["_ledger"], [])
        self.assertFalse(res["strategies"]["q"]["live"])
        self.assertEqual(len(res["strategies"]["q"]["picks"]), 1)  # preview still shown


if __name__ == "__main__":
    unittest.main()


class WeeklyCadenceTests(unittest.TestCase):
    def test_strategy_logs_once_a_week_even_if_run_daily(self):
        import datetime as dt
        import strategies_engine as se
        spec = {"strategies": [{"id": "s1", "startDate": "2026-11-29", "rank": [], "top": 1,
                                "filters": [], "maxPerSector": 3, "horizonsWeeks": [13]}]}
        led = []
        for day in range(30, 37):  # Nov 30 .. Dec 6 daily
            d = dt.date(2026, 11, day) if day <= 30 else dt.date(2026, 12, day - 30)
            led, new, _ = se.log_and_score(spec, {}, d, {}, {}, led)
        self.assertEqual([e["date"] for e in led], ["2026-11-30"])
        led, new, _ = se.log_and_score(spec, {}, dt.date(2026, 12, 7), {}, {}, led)
        self.assertEqual([e["date"] for e in led], ["2026-11-30", "2026-12-07"])


class OptionSetupTests(unittest.TestCase):
    def test_setups_respect_rules(self):
        import datetime as dt
        import screens
        base = {"name": "X", "price": 100.0, "vol3m": 0.2, "momentum": True, "chg52": 20, "jump5d63": 0.05}
        rows = {"TRV": dict(base), "NVDA": dict(base, us=True), "SSD": dict(base, momentum=False),
                "CBOE": dict(base, jump5d63=0.18), "SEIC": dict(base), "XFOR": dict(base, us=False),
                "TSM": dict(base)}
        out = screens.option_setups(rows, ["NVDA", "SEIC", "XFOR", "TSM"], dt.date(2026, 11, 30))
        # AI (NVDA) now allowed; foreign by flag (XFOR) or known list (TSM) excluded
        self.assertEqual([x["t"] for x in out["list"]], ["TRV", "NVDA", "SEIC"])
        trv = out["list"][0]
        self.assertEqual(trv["move3m"], 10.0)      # 20% vol x sqrt(1/4)
        self.assertEqual(trv["callPct"], 4.0)      # 0.4 x 10%
        self.assertEqual(trv["contractCost"], 400)  # 4% of $100 x 100 shares
