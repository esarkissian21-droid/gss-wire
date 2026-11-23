#!/usr/bin/env python3
"""Do gold-miner ETFs recover within 6-12 months after a 40-50% crash? (claim shared on social media, Nov 2026)

Data: Yahoo weekly adjusted closes for GDX (2006-), GDXJ (2009-), GLD (2004-).
Tests:
  1. Calendar-year returns (checks '2025: gold +67%, GDX +156%, GDXJ +174%').
  2. Current drawdown from the trailing peak.
  3. Every week a fund first closes 40%+ (GDX) / 45%+ (GDXJ) below its running peak, one episode per
     26 weeks: return over the next 26 and 52 weeks, vs GLD over the same window, vs all weeks.
  4. Leverage: beta of weekly miner returns to GLD (how many % miners move per 1% gold).
Writes research/miners_backtest.json.
"""
import datetime as dt
import json
import os
import statistics as st
import sys
import time
import urllib.request

UA = "Mozilla/5.0 (gss-wire research; personal use)"


def weekly(sym):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1=1072915200&period2={int(time.time())}"
           f"&interval=1wk&events=div%2Csplit")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        j = json.loads(r.read())["chart"]["result"][0]
    ts = j["timestamp"]
    adj = (j["indicators"].get("adjclose") or [{}])[0].get("adjclose") or j["indicators"]["quote"][0]["close"]
    out = []
    for t, a in zip(ts, adj):
        if a is not None:
            out.append((dt.datetime.fromtimestamp(t, dt.timezone.utc).date(), a))
    out.sort()
    # drop a duplicate partial-week row on the same week
    clean = []
    for d, a in out:
        if clean and (d - clean[-1][0]).days < 4:
            clean[-1] = (d, a)
        else:
            clean.append((d, a))
    return clean


def year_returns(s):
    by = {}
    for d, a in s:
        by.setdefault(d.year, []).append(a)
    years = sorted(by)
    return {y: round(by[y][-1] / by[years[i - 1]][-1] - 1, 4) for i, y in enumerate(years) if i > 0}


def at_or_after(s, d):
    for x, a in s:
        if x >= d:
            return x, a
    return None, None


def crash_test(s, gld, threshold):
    peak, last_ep = 0.0, None
    events, all_w = [], {26: [], 52: []}
    gmap = dict(gld)
    for i, (d, a) in enumerate(s):
        peak = max(peak, a)
        dd = a / peak - 1
        for h in (26, 52):
            if i + h < len(s):
                all_w[h].append(s[i + h][1] / a - 1)
        if dd <= -threshold and (last_ep is None or (d - last_ep).days > 26 * 7):
            last_ep = d
            ev = {"date": d.isoformat(), "drawdown": round(dd, 3)}
            for h in (26, 52):
                if i + h < len(s):
                    d1, a1 = s[i + h]
                    _, g0 = at_or_after(gld, d)
                    _, g1 = at_or_after(gld, d1)
                    ev[f"r{h}"] = round(a1 / a - 1, 4)
                    if g0 and g1:
                        ev[f"gld{h}"] = round(g1 / g0 - 1, 4)
            events.append(ev)
        elif dd > -threshold * 0.5:
            pass
    def summ(key):
        xs = [e[key] for e in events if key in e]
        return None if not xs else {"n": len(xs), "mean": round(st.mean(xs), 4), "median": round(st.median(xs), 4),
                                    "up": sum(1 for x in xs if x > 0)}
    return {"events": events, "r26": summ("r26"), "r52": summ("r52"), "gld26": summ("gld26"), "gld52": summ("gld52"),
            "allWeeks": {h: {"n": len(v), "mean": round(st.mean(v), 4), "median": round(st.median(v), 4),
                             "up": round(sum(1 for x in v if x > 0) / len(v), 3)} for h, v in all_w.items()}}


def beta(s, gld):
    gm = dict(gld)
    pts = [(d, a) for d, a in s if d in gm]
    xs, ys = [], []
    for (d0, a0), (d1, a1) in zip(pts, pts[1:]):
        ys.append(a1 / a0 - 1)
        xs.append(gm[d1] / gm[d0] - 1)
    mx, my = st.mean(xs), st.mean(ys)
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs), 2)


def main():
    data = {s: weekly(s) for s in ("GLD", "GDX", "GDXJ")}
    out = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "first": {s: v[0][0].isoformat() for s, v in data.items()}, "last": {s: v[-1][0].isoformat() for s, v in data.items()}}
    out["years"] = {s: year_returns(v) for s, v in data.items()}
    out["current"] = {}
    for s, v in data.items():
        recent = [x for x in v if x[0] >= dt.date(2025, 6, 1)]
        pk = max(recent, key=lambda x: x[1])
        out["current"][s] = {"peakDate": pk[0].isoformat(), "fromPeak": round(v[-1][1] / pk[1] - 1, 4), "lastDate": v[-1][0].isoformat()}
    out["crash"] = {"GDX_40": crash_test(data["GDX"], data["GLD"], 0.40), "GDXJ_45": crash_test(data["GDXJ"], data["GLD"], 0.45)}
    out["beta"] = {s: beta(data[s], data["GLD"]) for s in ("GDX", "GDXJ")}
    json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "miners_backtest.json"), "w"), indent=1, default=str)
    print(json.dumps({k: out[k] for k in ("first", "last", "current", "beta")}, indent=1, default=str))
    print({s: {y: r for y, r in v.items() if y >= 2019} for s, v in out["years"].items()})
    for k, c in out["crash"].items():
        print(k, {kk: c[kk] for kk in ("r26", "r52", "gld26", "gld52", "allWeeks")})
        for e in c["events"]:
            print("  ", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
