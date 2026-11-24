#!/usr/bin/env python3
"""Creator's 3-month trend rule (Nov 24 2026), tested on weekly closes 2012-2026.

Rule: stock above its 200-day average (40-week average) and that average rising (above its level 4 weeks ago);
3-month rate of change (13-week ROC) "reset" near zero (within +/-band) after having been high (>= peak at some
point in the last 13 weeks). Buy at that weekly close, hold 13 weeks (about 3 months).
Compared with: the same stock's average 13-week return when merely above a rising 200-day (no reset), all weeks,
and SPY over the same 13 weeks. Universe: 20 largest S&P 500 stocks at end-2011 (fixed in advance).
One signal per stock per 13 weeks. Month-clustered t-stat on excess vs SPY. Perturbation grid over the
three free settings to see if any result depends on one lucky choice. Non-blind; no costs (one round trip).
"""
import csv, json, math, os, statistics as st
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "history", "data")
U = "AAPL XOM MSFT IBM CVX GE PG JNJ T PFE WMT KO GOOGL ORCL WFC JPM INTC PM MRK VZ".split()
H = 13
def load(t):
    return [(r["date"], float(r["adjclose"])) for r in csv.DictReader(open(os.path.join(DATA, f"{t}.csv"))) if r["adjclose"]]
spy = dict(load("SPY"))
data = {t: load(t) for t in U}

def study(L=13, peak=0.10, band=0.02):
    sig, trend, allw = [], [], []
    for t, rows in data.items():
        d = [a for a, _ in rows]; p = [b for _, b in rows]
        last = -99
        for i in range(48, len(p) - H):
            ma = st.mean(p[i - 39:i + 1]); ma4 = st.mean(p[i - 43:i - 3])
            fwd = p[i + H] / p[i] - 1
            s0, s1 = spy.get(d[i]), spy.get(d[i + H])
            if not s0 or not s1: continue
            ex = fwd - (s1 / s0 - 1)
            allw.append((d[i], fwd, ex))
            up = p[i] > ma and ma > ma4
            if not up: continue
            trend.append((d[i], fwd, ex))
            roc = p[i] / p[i - L] - 1
            was_high = max(p[j] / p[j - L] - 1 for j in range(i - 13, i)) >= peak
            if abs(roc) <= band and was_high and i - last >= H:
                sig.append((d[i], fwd, ex)); last = i
    def summ(x):
        if not x: return {"n": 0}
        months = {}
        for dd, _, e in x: months.setdefault(dd[:7], []).append(e)
        mm = [st.mean(v) for v in months.values()]
        t = st.mean(mm) / (st.pstdev(mm) / math.sqrt(len(mm))) if len(mm) > 5 and st.pstdev(mm) else None
        return {"n": len(x), "months": len(mm), "avgRet": round(100 * st.mean(r for _, r, _ in x), 2),
                "avgExcessVsSPY": round(100 * st.mean(e for _, _, e in x), 2),
                "pctBeatSPY": round(100 * sum(e > 0 for _, _, e in x) / len(x), 1),
                "pctUp": round(100 * sum(r > 0 for _, r, _ in x) / len(x), 1),
                "worst": round(100 * min(r for _, r, _ in x), 1), "t": round(t, 2) if t else None}
    return {"signal": summ(sig), "aboveRising200_noFilter": summ(trend), "allWeeks": summ(allw)}

base = study()
grid = []
for L in (8, 13, 26):
    for peak in (0.06, 0.10, 0.15):
        for band in (0.02, 0.04):
            r = study(L, peak, band)["signal"]
            grid.append({"rocWeeks": L, "peak": peak, "band": band, **r})
halves = {}
for lo, hi in (("2012", "2019"), ("2019", "2027")):
    pass
out = {"base(13w ROC, peak 10%, band 2%)": base, "grid": grid,
       "gridSummary": {"cells": len(grid), "cellsBeatingSPY": sum(g.get("avgExcessVsSPY", 0) > 0 for g in grid if g["n"]),
                       "medianExcess": st.median(g["avgExcessVsSPY"] for g in grid if g["n"])}}
json.dump(out, open(os.path.join(HERE, "roc_reset_backtest.json"), "w"), indent=1)
print(json.dumps(base, indent=1)); print(out["gridSummary"])
for g in grid: print(g["rocWeeks"], g["peak"], g["band"], g["n"], g.get("avgExcessVsSPY"), g.get("pctBeatSPY"), g.get("t"))
