import copy
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wire  # noqa: E402

CFG = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")))
UTC = dt.timezone.utc


def sec_json(filings):
    keys = ["accessionNumber", "filingDate", "form", "items", "primaryDocument"]
    rec = {k: [f[i] for f in filings] for i, k in enumerate(keys)}
    return json.dumps({"filings": {"recent": rec}}).encode()


def rss(items):
    body = "".join(
        f"<item><title>{t} - {s}</title><link>{l}</link><guid>{g}</guid>"
        f"<pubDate>{p}</pubDate><source url='https://x'>{s}</source></item>"
        for t, s, l, g, p in items)
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>x</title>{body}</channel></rss>".encode()


def fed_rss(items):
    body = "".join(f"<item><title>{t}</title><link>{l}</link><guid>{l}</guid><pubDate>{p}</pubDate></item>" for t, l, p in items)
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>FRB</title>{body}</channel></rss>".encode()


class World:
    """Fake internet: URL substring -> bytes or Exception."""

    def __init__(self):
        self.sec = {c["cik"]: sec_json([]) for c in CFG["companies"]}
        self.news = {}
        self.fed = fed_rss([])
        self.fail = set()
        self.uas = []

    def fetch(self, url, ua):
        self.uas.append((url, ua))
        for f in self.fail:
            if f in url:
                raise OSError("down")
        if "data.sec.gov" in url:
            cik = url.split("CIK")[1].split(".json")[0]
            return self.sec[cik]
        if "federalreserve.gov" in url:
            return self.fed
        if "news.google.com" in url:
            for key, val in self.news.items():
                if key in url:
                    return val
            return rss([])
        raise AssertionError(url)


def pub(ts):
    return ts.strftime("%a, %d %b %Y %H:%M:%S GMT")


class WireTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.pushes = []
        self.t0 = dt.datetime(2026, 11, 23, 18, 0, tzinfo=UTC)  # 10 a.m. PT Monday
        nv = "0001045810"
        self.world.sec[nv] = sec_json([
            ("0001045810-26-000082", "2026-11-12", "8-K", "5.02,9.01", "nvda-8k.htm"),
            ("0001197647-26-000013", "2026-11-06", "4", "", "form4.xml"),
        ])
        self.world.news["Nvidia"] = rss([
            ("Nvidia shares fall on SEC probe report", "Reuters", "https://n/1", "g1", pub(self.t0 - dt.timedelta(hours=30))),
        ])
        self.world.fed = fed_rss([("Federal Reserve issues FOMC statement", "https://fed/1", pub(self.t0 - dt.timedelta(days=20)))])

    def push(self, title, msg, prio, tags, click):
        self.pushes.append({"title": title, "msg": msg, "prio": prio, "tags": tags, "click": click})

    def go(self, state, ts, contact="Test User test@example.com"):
        return wire.run(CFG, state, self.world.fetch, self.push, ts=ts, sec_contact=contact, sleep=lambda s: None)

    def test_first_run_seeds_without_spam(self):
        state, lines, pushes = self.go({}, self.t0)
        self.assertEqual(pushes, [])
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("live", self.pushes[0]["title"])
        self.assertTrue(all("first check" in l["note"] for l in lines))
        self.assertIn("SEC NVDA", state["seeded"])
        sec_uas = [ua for url, ua in self.world.uas if "data.sec.gov" in url]
        self.assertTrue(sec_uas and all(ua == "Test User test@example.com" for ua in sec_uas))

    def test_second_run_pushes_new_items_with_right_priority(self):
        state, _, _ = self.go({}, self.t0)
        self.pushes.clear()
        t1 = self.t0 + dt.timedelta(minutes=5)
        nv = "0001045810"
        self.world.sec[nv] = sec_json([
            ("0001045810-26-000099", "2026-11-23", "8-K", "4.02,9.01", "nvda-8k2.htm"),
            ("0001197647-26-000020", "2026-11-23", "4", "", "form4.xml"),
            ("0001045810-26-000082", "2026-11-12", "8-K", "5.02,9.01", "nvda-8k.htm"),
        ])
        self.world.news["Nvidia"] = rss([
            ("Nvidia says past results can no longer be relied on, shares plunge", "Reuters", "https://n/2", "g2", pub(t1)),
            ("Nvidia says past results can no longer be relied upon; shares plunge", "Bloomberg", "https://n/3", "g3", pub(t1)),
            ("3 reasons to buy Nvidia stock forever", "Motley Fool", "https://n/4", "g4", pub(t1)),
            ("Nvidia holds developer conference", "Reuters", "https://n/5", "g5", pub(t1)),
            ("Nvidia shares fall on SEC probe report", "Reuters", "https://n/1", "g1", pub(self.t0 - dt.timedelta(hours=30))),
        ])
        self.world.fed = fed_rss([
            ("Federal Reserve issues FOMC statement", "https://fed/2", pub(t1)),
            ("Federal Reserve issues FOMC statement", "https://fed/1", pub(self.t0 - dt.timedelta(days=20))),
        ])
        state, lines, pushes = self.go(state, t1)
        titles = [(p["title"], p["prio"]) for p in self.pushes]
        self.assertIn(("NVDA · SEC EDGAR", 5), titles)
        self.assertIn(("MACRO · Federal Reserve", 5), titles)
        news = [p for p in self.pushes if p["title"] == "NVDA · Reuters"]
        self.assertEqual(len(news), 1)
        self.assertIn("relied on", news[0]["msg"])
        notes = {l["title"]: l.get("note", "") for l in lines}
        self.assertEqual(notes["3 reasons to buy Nvidia stock forever"], "logged only")
        self.assertEqual(notes["Nvidia holds developer conference"], "logged only")
        self.assertIn("same story", notes["Nvidia says past results can no longer be relied upon; shares plunge"])
        form4 = [l for l in lines if l.get("form") == "4"]
        self.assertEqual(form4[0]["note"], "logged only")
        sec_push = [p for p in self.pushes if p["title"] == "NVDA · SEC EDGAR"][0]
        self.assertIn("past financials no longer reliable", sec_push["msg"])
        self.assertTrue(sec_push["click"].endswith("/1045810/000104581026000099/nvda-8k2.htm"))
        self.assertIn("rotating_light", sec_push["tags"])
        # nothing repeats on the next run
        self.pushes.clear()
        state, lines, pushes = self.go(state, t1 + dt.timedelta(minutes=5))
        self.assertEqual(self.pushes, [])
        self.assertEqual(lines, [])

    def test_quiet_hours_hold_normal_alerts(self):
        state, _, _ = self.go({}, self.t0)
        self.pushes.clear()
        night = dt.datetime(2026, 11, 24, 7, 30, tzinfo=UTC)  # 11:30 p.m. PT
        self.world.news["Agnico"] = rss([("Agnico Eagle raises dividend", "Reuters", "https://a/1", "a1", pub(night))])
        self.world.sec["0001915657"] = sec_json([("0001628280-26-080000", "2026-11-24", "8-K", "1.03", "dino.htm")])
        state, lines, _ = self.go(state, night)
        self.assertEqual([p["title"] for p in self.pushes], ["DINO · SEC EDGAR"])
        self.assertEqual({l["title"]: l.get("note", "") for l in lines}["Agnico Eagle raises dividend"], "quiet hours: logged only")

    def test_daily_news_cap_and_one_per_run(self):
        state, _, _ = self.go({}, self.t0)
        self.pushes.clear()
        heads = ["TSMC revenue jumps 40%", "TSMC shares slump on export ban", "TSMC names new CFO",
                 "TSMC profit beats forecasts", "TSMC plans buyback", "TSMC halts plant after outage"]
        for i, h in enumerate(heads):
            t = self.t0 + dt.timedelta(minutes=5 * (i + 1))
            self.world.news["TSMC"] = rss([(h, "Reuters", f"https://t/{i}", f"t{i}", pub(t)),
                                           (h + " again", "CNBC", f"https://t/x{i}", f"tx{i}", pub(t))])
            state, _, _ = self.go(state, t)
        tsm = [p for p in self.pushes if p["title"].startswith("TSM ")]
        self.assertEqual(len(tsm), 4)

    def test_failing_source_warns_once_after_an_hour(self):
        state, _, _ = self.go({}, self.t0)
        self.pushes.clear()
        self.world.fail.add("federalreserve.gov")
        for i in range(14):
            state, _, _ = self.go(state, self.t0 + dt.timedelta(minutes=5 * (i + 1)))
        warns = [p for p in self.pushes if "attention" in p["title"]]
        self.assertEqual(len(warns), 1)
        self.assertIn("Fed", warns[0]["msg"])
        self.world.fail.clear()
        state, _, _ = self.go(state, self.t0 + dt.timedelta(hours=2))
        self.assertEqual(state["errors"]["Fed"], 0)

    def test_new_company_added_later_is_seeded_quietly(self):
        state, _, _ = self.go({}, self.t0)
        self.pushes.clear()
        cfg2 = copy.deepcopy(CFG)
        cfg2["companies"].append({"ticker": "NEM", "name": "Newmont", "cik": "0001164727", "news_query": "\"Newmont\""})
        self.world.sec["0001164727"] = sec_json([("0001164727-26-000050", "2026-11-23", "8-K", "2.02,9.01", "nem.htm")])
        self.world.news["Newmont"] = rss([("Newmont profit beats estimates", "Reuters", "https://m/1", "m1", pub(self.t0))])
        state, lines, _ = wire.run(cfg2, state, self.world.fetch, self.push, ts=self.t0 + dt.timedelta(minutes=5),
                                   sec_contact="T t@e.com", sleep=lambda s: None)
        self.assertEqual(self.pushes, [])
        self.assertTrue(all("first check" in l["note"] for l in lines))

    def test_missing_sec_contact_warns_once(self):
        state, _, _ = self.go({}, self.t0, contact=None)
        state, _, _ = self.go(state, self.t0 + dt.timedelta(minutes=5), contact=None)
        warns = [p for p in self.pushes if "SEC_CONTACT" in p["msg"]]
        self.assertEqual(len(warns), 1)
        self.assertFalse(any("data.sec.gov" in u for u, _ in self.world.uas))

    def test_metals_topic_is_strict(self):
        state, _, _ = self.go({}, self.t0)
        self.pushes.clear()
        t = self.t0 + dt.timedelta(minutes=5)
        self.world.news["gold%20price"] = rss([
            ("Gold prices edge higher in quiet trade", "Reuters", "https://g/1", "gg1", pub(t)),
            ("Silver prices plunge to lowest since 2025 as dollar surges", "Reuters", "https://g/2", "gg2", pub(t)),
        ])
        state, lines, _ = self.go(state, t)
        self.assertEqual([p["msg"] for p in self.pushes], ["Silver prices plunge to lowest since 2025 as dollar surges"])

    def test_news_url_and_log_shape(self):
        u = wire.news_url('"Agnico Eagle"')
        self.assertTrue(u.startswith("https://news.google.com/rss/search?q=%22Agnico%20Eagle%22%20when%3A2d"))
        state, lines, _ = self.go({}, self.t0)
        json.dumps(lines)  # serializable
        self.assertTrue(all("seenAt" in l and "id" in l for l in lines))
        json.dumps(state)


if __name__ == "__main__":
    unittest.main(verbosity=2)
