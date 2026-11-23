#!/usr/bin/env python3
"""Does hedge-fund ("managed money") positioning in gold and silver futures predict prices?
(claim shared on social media, Nov 2026: 'price lags, positioning leads'; crowded trades reverse.)

Data: CFTC Disaggregated Commitments of Traders, futures only (2006-), COMEX gold 088691 and silver 084691.
Positions are as of Tuesday and published Friday, so each reading is usable from the following week.
Signal: managed-money net position as % of open interest, ranked against the prior 156 weeks (3 years).
  crowded long  = top 10% of the 3-year range; crowded short = bottom 10%.
Outcome: gold / silver futures (Yahoo GC=F, SI=F) over the next 4, 13, 26 weeks vs all weeks; and for
  silver-vs-gold crowding, the gold/silver ratio. Also: does positioning follow price (trend-following)?
Writes research/cot_backtest.json.
"""
import csv
import datetime as dt
import io
import json
import os
import statistics as st
import sys
import time
import urllib.request
import zipfile

CODES = {"gold": "088691", "silver": "084691"}
UA = "Mozilla/5.0 (gss-wire research; personal use)"


def get(url, tries=4):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception:
            time.sleep(5 * (k + 1))
    raise RuntimeError("fetch failed: " + url)


def cot_rows():
    urls = ["https://www.cftc.gov/files/dea/history/fut_disagg_txt_hist_2006_2016.zip"]
    urls += [f"https://www.cftc.gov/files/dea/history/fut_disagg_txt_{y}.zip" for y in range(2017, dt.date.today().year + 1)]
    out = {}
    for u in urls:
        try:
            zf = zipfile.ZipFile(io.BytesIO(get(u)))
        except Exception as e:
            print("skip", u, e, flush=True)
            continue
        for name in zf.namelist():
            text = zf.read(name).decode("utf-8", "replace")
            rdr = csv.DictReader(io.StringIO(text))
            for r in rdr:
                r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
                code = r.get("CFTC_Contract_Market_Code", "")
                which = next((k for k, c in CODES.items() if code == c), None)
                if not which:
                    continue
                d = r.get("Report_Date_as_YYYY-MM-DD") or r.get("Report_Date_as_YYYY_MM_DD")
                try:
                    day = dt.date.fromisoformat(d[:10])
                    oi = float(r["Open_Interest_All"])
                    ml, ms = float(r["M_Money_Positions_Long_All"]), float(r["M_Money_Positions_Short_All"])
                except (KeyError, ValueError, TypeError):
                    continue
                out[(which, day)] = 100 * (ml - ms) / oi
        print("read", u, len(out), flush=True)
    series = {k: sorted((d, v) for (w, d), v in out.items() if w == k) for k in CODES}
    return series


def weekly_px(sym):
    j = None
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(sym)}?period1=1136073600"
               f"&period2={int(time.time())}&interval=1wk")
        try:
            j = json.loads(get(url, 2))["chart"]["result"][0]
            if j.get("timestamp"):
                break
        except Exception as e:
            print("price fetch", sym, host, type(e).__name__, flush=True)
    if not j or not j.get("timestamp"):
        return []
    out = []
    for t, c in zip(j["timestamp"], j["indicators"]["quote"][0]["close"]):
        if c is not None:
            d = dt.datetime.fromtimestamp(t, dt.timezone.utc).date()
            out.append((d + dt.timedelta(days=(4 - d.weekday()) % 7), c))
    clean = {}
    for d, c in sorted(out):
        clean[d] = c
    return sorted(clean.items())


def fwd(px, day, weeks):
    """Return from the first weekly close after `day` (publication Friday) to `weeks` later."""
    i = next((k for k, (d, _) in enumerate(px) if d > day), None)
    if i is None or i + weeks >= len(px):
        return None
    return px[i + weeks][1] / px[i][1] - 1


def pct_rank(hist, v):
    return sum(1 for h in hist if h <= v) / len(hist)


def summ(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "mean": round(st.mean(xs), 4), "median": round(st.median(xs), 4),
            "up": round(sum(1 for x in xs if x > 0) / len(xs), 3)}


def episodes(dates, gap_weeks=13):
    """Keep the first week of each run of signals (a new run needs a 13-week gap)."""
    keep, last = [], None
    for d in dates:
        if last is None or (d - last).days > gap_weeks * 7:
            keep.append(d)
        last = d
    return keep


def main():
    cot = cot_rows()
    px = {"gold": weekly_px("GC=F"), "silver": weekly_px("SI=F")}
    if not px["gold"] or not px["silver"]:  # fall back to the ETFs (track the metal less fees)
        px = {"gold": px["gold"] or weekly_px("GLD"), "silver": px["silver"] or weekly_px("SLV")}
    print("price weeks:", {k: len(v) for k, v in px.items()}, flush=True)
    pm = {k: dict(v) for k, v in px.items()}
    ratio = sorted((d, pm["gold"][d] / pm["silver"][d]) for d in pm["gold"] if d in pm["silver"])
    out = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "weeks": {k: len(v) for k, v in cot.items()}, "first": {k: v[0][0].isoformat() for k, v in cot.items()},
           "last": {k: v[-1][0].isoformat() for k, v in cot.items()}, "metals": {}}
    pub = lambda d: d + dt.timedelta(days=3)  # Tuesday data, Friday release
    ranks = {}
    for m, s in cot.items():
        rk = []
        for i in range(156, len(s)):
            hist = [v for _, v in s[i - 156:i]]
            rk.append((s[i][0], s[i][1], pct_rank(hist, s[i][1])))
        ranks[m] = rk
        res = {"current": {"date": s[-1][0].isoformat(), "netPctOI": round(s[-1][1], 1), "rank3y": round(rk[-1][2], 2)}}
        for label, cond in (("crowdedLong", lambda r: r >= 0.9), ("crowdedShort", lambda r: r <= 0.1), ("all", lambda r: True)):
            dates = [d for d, _, r in rk if cond(r)]
            if label != "all":
                dates = episodes(dates)
            res[label] = {h: summ([fwd(px[m], pub(d), h) for d in dates]) for h in (4, 13, 26)}
            res[label]["episodes"] = len(dates)
        # does positioning follow price? correlation of the 4-week change in net position with the prior vs next 4-week return
        prior, nxt, chg = [], [], []
        for i in range(4, len(s) - 1):
            d0, d1 = s[i - 4][0], s[i][0]
            p_prior = fwd(px[m], d0 - dt.timedelta(days=7), 4)
            p_next = fwd(px[m], pub(d1), 4)
            if p_prior is None or p_next is None:
                continue
            chg.append(s[i][1] - s[i - 4][1]); prior.append(p_prior); nxt.append(p_next)
        def corr(a, b):
            if len(a) < 3:
                return None
            ma, mb = st.mean(a), st.mean(b)
            return round(sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5, 3)
        res["followsPrice"] = {"corrWithPrior4wReturn": corr(chg, prior), "corrWithNext4wReturn": corr(chg, nxt), "n": len(chg)}
        out["metals"][m] = res
    # silver crowded relative to gold -> ratio
    rg = {d: r for d, _, r in ranks["gold"]}
    rel = [(d, r - rg[d]) for d, _, r in ranks["silver"] if d in rg]
    rr = {}
    for label, cond in (("silverMoreCrowdedLong", lambda x: x >= 0.6), ("silverMoreCrowdedShort", lambda x: x <= -0.6)):
        dates = episodes([d for d, x in rel if cond(x)])
        rr[label] = {h: summ([fwd(ratio, pub(d), h) for d in dates]) for h in (4, 13, 26)}
        rr[label]["episodes"] = len(dates)
    rr["all"] = {h: summ([fwd(ratio, pub(d), h) for d, _ in rel]) for h in (4, 13, 26)}
    out["ratio"] = rr
    json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cot_backtest.json"), "w"), indent=1)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
