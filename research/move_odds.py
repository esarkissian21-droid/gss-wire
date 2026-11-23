#!/usr/bin/env python3
"""What usually happens after a big one-day move? Base rates for the market-hours watch.

For ~100 large US companies (daily prices 2005-), find days with:
  drop3  close-to-close move between -3% and -6%     jump3  between +3% and +6%
  drop6  -6% or worse                                jump6  +6% or better
  vol2dn volume 2x its 50-day average on a down day  vol2up same on an up day
Each bucket is split by where the stock was beforehand (above / below its 200-day average).
One event per stock per bucket per 21 trading days, so one long slide isn't counted ten times.

Outcomes 5, 21 and 63 trading days later: share of events where the stock was higher, and where it
beat SPY over the same days, compared with the same stocks on ordinary days (the baseline).
"Edge" = event share minus baseline share. Events on the same day move together, so the t-stat is
clustered by calendar month, and the sample is split 2005-2015 vs 2016-2026: a bucket is called
"reliable" only if |t| >= 2 overall AND both halves point the same way.

Biases (reported, not fixed): today's large companies, so failures that dropped out are missing
(survivorship lifts every bucket and the baseline alike; the edge, a difference, is less affected).
Non-blind: rules were chosen after seeing history. Writes research/move_odds.json.
"""
import datetime as dt
import json
import math
import os
import statistics as st
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (gss-wire research; personal use)"
START = int(dt.datetime(2004, 6, 1, tzinfo=dt.timezone.utc).timestamp())
HORIZONS = (5, 21, 63)
GAP = 21
UNIVERSE = """AAPL MSFT AMZN GOOGL META NVDA AVGO ORCL CSCO IBM INTC TXN QCOM ADBE CRM ACN AMD AMAT MU ADP
JPM BAC WFC C GS MS SCHW BLK AXP USB PNC TRV PGR CB ALL MET AIG MMC CME ICE
JNJ PFE MRK ABBV LLY UNH ABT TMO DHR BMY AMGN GILD MDT CVS CI ISRG SYK
PG KO PEP WMT COST HD LOW MCD SBUX NKE TGT TJX CL MO PM KMB GIS
XOM CVX COP SLB EOG OXY PSX MPC
CAT DE HON GE MMM UNP UPS FDX LMT RTX BA EMR ITW GD NSC CSX
VZ T TMUS CMCSA DIS NFLX
NEE DUK SO D AEP LIN APD SHW NEM ECL SPG PLD AMT""".split()


def fetch(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 + 3 * i)


def daily(sym):
    now = int(time.time())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={START}&period2={now}"
           f"&interval=1d&events=div,split")
    j = json.loads(fetch(url))["chart"]["result"][0]
    ts = j.get("timestamp") or []
    q = j["indicators"]["quote"][0]
    adj = (j["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
    rows = []
    for t, a, v in zip(ts, adj, q.get("volume") or [None] * len(ts)):
        if a is not None and a > 0:
            rows.append((dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(), a, v or 0))
    return rows


def bucket_of(r, vr):
    out = []
    if r <= -0.06:
        out.append("drop6")
    elif r <= -0.03:
        out.append("drop3")
    elif r >= 0.06:
        out.append("jump6")
    elif r >= 0.03:
        out.append("jump3")
    if vr is not None and vr >= 2:
        out.append("vol2dn" if r < 0 else "vol2up")
    return out


def events_for(sym, rows, spy):
    """-> list of (bucket, trend, date, {h: (up, beat, excess)})"""
    dates = [d for d, _, _ in rows]
    px = [a for _, a, _ in rows]
    vol = [v for _, _, v in rows]
    out, base, last = [], [], {}
    for i in range(200, len(rows) - 1):
        r = px[i] / px[i - 1] - 1
        if abs(r) > 0.5:  # bad tick / split glitch
            continue
        prior = vol[i - 50:i]
        avgv = sum(prior) / 50 if all(prior) else None
        vr = vol[i] / avgv if avgv and vol[i] else None
        trend = "above200" if px[i - 1] > sum(px[i - 200:i]) / 200 else "below200"
        res = {}
        for h in HORIZONS:
            if i + h >= len(rows):
                continue
            s0, s1 = spy.get(dates[i]), spy.get(dates[i + h])
            if not s0 or not s1:
                continue
            ret = px[i + h] / px[i] - 1
            mret = s1 / s0 - 1
            res[h] = (ret > 0, ret > mret, ret - mret)
        if not res:
            continue
        if i % 5 == 0:
            base.append((trend, dates[i], res))
        for b in bucket_of(r, vr):
            if i - last.get(b, -10**9) < GAP:
                continue
            last[b] = i
            out.append((b, trend, dates[i], res))
    return out, base


def wilson(k, n, z=1.96):
    if not n:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(100 * (c - m), 1), round(100 * (c + m), 1)


def clustered_t(evts, h, base_beat):
    """t-stat of (beat share - baseline), clustered by month."""
    by_m = {}
    for _, _, d, res in evts:
        if h in res:
            by_m.setdefault(d[:7], []).append(1.0 if res[h][1] else 0.0)
    means = [st.mean(v) - base_beat for v in by_m.values()]
    if len(means) < 8:
        return None, len(means)
    sd = st.pstdev(means)
    return (round(st.mean(means) / (sd / math.sqrt(len(means))), 2) if sd else None), len(means)


def summarize(evts, base_by_trend, trend=None):
    sel = [e for e in evts if trend is None or e[1] == trend]
    out = {}
    for h in HORIZONS:
        rs = [e[3][h] for e in sel if h in e[3]]
        bs = [b[2][h] for b in base_by_trend(trend) if h in b[2]]
        if len(rs) < 30 or not bs:
            out[str(h)] = {"n": len(rs)}
            continue
        up = sum(r[0] for r in rs)
        beat = sum(r[1] for r in rs)
        b_up = 100 * sum(b[0] for b in bs) / len(bs)
        b_beat = 100 * sum(b[1] for b in bs) / len(bs)
        t, months = clustered_t(sel, h, b_beat / 100)
        halves = {}
        for name, lo, hi in (("2005-2015", "0000", "2016"), ("2016-2026", "2016", "9999")):
            hs = [e[3][h] for e in sel if h in e[3] and lo <= e[2] < hi]
            hb = [b[2][h] for b in base_by_trend(trend) if h in b[2] and lo <= b[1] < hi]
            if len(hs) >= 20 and hb:
                halves[name] = round(100 * sum(r[1] for r in hs) / len(hs) - 100 * sum(b[1] for b in hb) / len(hb), 1)
        edge = round(100 * beat / len(rs) - b_beat, 1)
        same_way = len(halves) == 2 and all((v > 0) == (edge > 0) for v in halves.values())
        out[str(h)] = {
            "n": len(rs), "months": months,
            "pUp": round(100 * up / len(rs), 1), "baseUp": round(b_up, 1),
            "pBeat": round(100 * beat / len(rs), 1), "baseBeat": round(b_beat, 1),
            "beatCI": wilson(beat, len(rs)), "edge": edge, "t": t, "halves": halves,
            "medExcess": round(100 * st.median(r[2] for r in rs), 2),
            "reliable": bool(t is not None and abs(t) >= 2 and same_way),
        }
    return out


def main():
    spy_rows = daily("SPY")
    spy = {d: a for d, a, _ in spy_rows}
    got, failed = {}, []

    def one(sym):
        try:
            return sym, daily(sym)
        except Exception as e:
            return sym, e

    with ThreadPoolExecutor(6) as ex:
        for sym, res in ex.map(one, UNIVERSE):
            if isinstance(res, Exception) or len(res) < 400:
                failed.append(sym)
            else:
                got[sym] = res
    evts, base = [], []
    for sym, rows in got.items():
        e, b = events_for(sym, rows, spy)
        evts += e
        base += b

    def base_by_trend(trend):
        return [b for b in base if trend is None or b[0] == trend]

    buckets = {}
    for bk in ("drop3", "drop6", "jump3", "jump6", "vol2dn", "vol2up"):
        sel = [e for e in evts if e[0] == bk]
        buckets[bk] = {
            "all": summarize(sel, base_by_trend),
            "above200": summarize(sel, base_by_trend, "above200"),
            "below200": summarize(sel, base_by_trend, "below200"),
        }
    out = {
        "asOf": dt.date.today().isoformat(),
        "universe": len(got), "failed": failed,
        "span": [spy_rows[0][0], spy_rows[-1][0]],
        "baseline": summarize([("base", t, d, r) for t, d, r in base], base_by_trend),
        "buckets": buckets,
        "notes": ["Survivorship: today's large companies only.",
                  "Non-blind: rules chosen after seeing history.",
                  "reliable = |t|>=2 (month-clustered) and both halves (2005-15, 2016-26) agree in sign."],
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "move_odds.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({k: {t: v[t].get("63") for t in v} for k, v in buckets.items()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
