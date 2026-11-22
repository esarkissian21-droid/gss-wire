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


def fake_px(url):
    sym = url.split("/chart/")[1].split("?")[0]
    base = {"GOOD": 100.0, "BANK": 50.0, "TINY": 5.0}[sym]
    closes = [base * (0.8 + 0.2 * i / 299) for i in range(300)]  # rising: above 200-day average, up on the year
    return json.dumps({"chart": {"result": [{"indicators": {"quote": [{"close": closes}]}}]}}).encode()


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
        self.assertAlmostEqual(self.g["pe"], 100.0 / 7.4, places=1)
        self.assertAlmostEqual(self.g["pb"], 100.0 * 325e6 / 10e9, places=2)
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


if __name__ == "__main__":
    unittest.main()
