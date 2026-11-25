#!/usr/bin/env python3
"""Test of a social-media claim (Nov 24 2026): the RSP/SPY ratio (equal-weight vs cap-weight S&P 500)
"trends in big waves" ("stick with the wave") and "turns at the edges" of its range.

Two rule families, each traded weekly with NO look-ahead (signal at Friday close, held the next week):
  A. Wave (trend): hold RSP when the ratio is above its N-week average, else SPY. N = 10, 20, 26, 40, 52.
  B. Edges (fade): position of the ratio in its trailing L-week range. Top fraction T -> hold SPY
     ("broadening is tired, go with the leaders"), bottom T -> hold RSP; in between, hold 50/50.
     L = 26, 52, 104; T = 0.1, 0.2.
Benchmarks: 50/50 RSP+SPY rebalanced weekly (the fair 'no view' comparison), SPY, RSP.
Costs: 0.10% per full switch. Dividend-adjusted weekly closes from Yahoo, so fund fees are included.
Also: persistence check = correlation of each 13-week ratio change with the next 13-week change.

Non-blind: the claim was made in 2026 by someone who watched this history. Writes rsp_spy_backtest.json.
"""
import datetime as dt
import json
import math
import os
import statistics as st
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (gss-wire research; personal use)"
COST = 0.001


def weekly(sym):
    """Friday (last trading day of each week) dividend-adjusted closes, built from DAILY bars.
    Weekly bars with events can carry extra mid-week rows (dividend dates) that look like price
    glitches and create fake mean reversion, so daily data is used and resampled here."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1=1041379200&period2={int(time.time())}"
           "&interval=1d")
    for k in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                j = json.loads(r.read())["chart"]["result"][0]
            break
        except Exception:
            time.sleep(5 * (k + 1))
    else:
        raise RuntimeError("price fetch failed " + sym)
    adj = j["indicators"]["adjclose"][0]["adjclose"]
    out = {}
    for t, a in zip(j["timestamp"], adj):
        if a is None:
            continue
        d = dt.datetime.fromtimestamp(t, dt.timezone.utc).date()
        out[d - dt.timedelta(days=d.weekday())] = a  # later days overwrite: last close of the week
    return out


def stats(rets, dates):
    if not rets:
        return None
    g = 1.0
    for r in rets:
        g *= 1 + r
    yrs = (dates[-1] - dates[0]).days / 365.25
    cagr = g ** (1 / yrs) - 1 if yrs > 0 else None
    vol = st.pstdev(rets) * math.sqrt(52)
    peak, dd, v = 1.0, 0.0, 1.0
    for r in rets:
        v *= 1 + r
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return {"cagr": round(100 * cagr, 2), "vol": round(100 * vol, 1), "maxDD": round(100 * dd, 1)}


def run_rule(weights, rsp_r, spy_r):
    """weights[i] = RSP share decided at close of week i, applied to week i+1 returns."""
    out, prev = [], None
    for i in range(len(rsp_r) - 1):
        w = weights[i]
        if w is None:
            w = 0.5
        r = w * rsp_r[i + 1] + (1 - w) * spy_r[i + 1]
        if prev is not None and w != prev:
            r -= COST * abs(w - prev)
        prev = w
        out.append(r)
    return out


def main():
    rsp, spy = weekly("RSP"), weekly("SPY")
    days = sorted(set(rsp) & set(spy))
    print("weeks RSP", len(rsp), "SPY", len(spy), "common", len(days), flush=True)
    if len(days) < 300:
        raise RuntimeError("not enough overlapping weekly data")
    px_r = [rsp[d] for d in days]
    px_s = [spy[d] for d in days]
    ratio = [a / b for a, b in zip(px_r, px_s)]
    rsp_r = [0.0] + [px_r[i] / px_r[i - 1] - 1 for i in range(1, len(days))]
    spy_r = [0.0] + [px_s[i] / px_s[i - 1] - 1 for i in range(1, len(days))]
    start = 104  # give every rule its full lookback before scoring
    D = days[start + 1:]

    def slice_r(full):
        return full[start:]

    bench = {"50/50": [0.5 * a + 0.5 * b for a, b in zip(rsp_r[start + 1:], spy_r[start + 1:])],
             "SPY": spy_r[start + 1:], "RSP": rsp_r[start + 1:]}
    res = {"benchmarks": {k: stats(v, D) for k, v in bench.items()}, "wave": {}, "edges": {}}
    half = len(D) // 2

    def score(name, rets, group):
        b = bench["50/50"]
        ex = [a - c for a, c in zip(rets, b)]
        s = stats(rets, D)
        # yearly excess vs 50/50, then t-stat on those (year blocks reduce overlap problems)
        yrs = {}
        for d, e in zip(D, ex):
            yrs.setdefault(d.year, []).append(e)
        yearly = [sum(v) for v in yrs.values() if len(v) > 40]
        t = round(st.mean(yearly) / (st.stdev(yearly) / math.sqrt(len(yearly))), 2) if len(yearly) > 2 else None
        s.update({"excessVs5050PerYr": round(100 * st.mean(ex) * 52, 2),
                  "yearsBeat5050": f"{sum(1 for y in yearly if y > 0)}/{len(yearly)}", "t": t,
                  "firstHalfExcess": round(100 * st.mean(ex[:half]) * 52, 2),
                  "secondHalfExcess": round(100 * st.mean(ex[half:]) * 52, 2),
                  "switchesPerYr": None})
        res[group][name] = s

    for n in (10, 20, 26, 40, 52):
        w = []
        for i in range(len(ratio)):
            if i < n:
                w.append(None)
                continue
            ma = st.mean(ratio[i - n + 1:i + 1])
            w.append(1.0 if ratio[i] > ma else 0.0)
        rets = run_rule(w, rsp_r, spy_r)[start:]
        score(f"N={n}", rets, "wave")
        ws = [x for x in w[start:] if x is not None]
        res["wave"][f"N={n}"]["switchesPerYr"] = round(sum(1 for a, b in zip(ws, ws[1:]) if a != b) / (len(ws) / 52), 1)

    for L in (26, 52, 104):
        for T in (0.1, 0.2):
            w = []
            for i in range(len(ratio)):
                if i < L:
                    w.append(None)
                    continue
                win = ratio[i - L + 1:i + 1]
                lo, hi = min(win), max(win)
                pos = 0.5 if hi == lo else (ratio[i] - lo) / (hi - lo)
                w.append(0.0 if pos >= 1 - T else (1.0 if pos <= T else 0.5))
            rets = run_rule(w, rsp_r, spy_r)[start:]
            score(f"L={L},T={T}", rets, "edges")

    # persistence: does the last 13 weeks' ratio change predict the next 13 weeks'?
    ch = [ratio[i] / ratio[i - 13] - 1 for i in range(13, len(ratio))]
    pairs = [(ch[i], ch[i + 13]) for i in range(0, len(ch) - 13, 13)]
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    corr = st.correlation(xs, ys) if len(pairs) > 3 else None
    res["persistence13w"] = {"pairs": len(pairs), "corr": round(corr, 3) if corr is not None else None,
                             "note": "non-overlapping 13-week blocks; |corr| under ~0.2 with this n is noise"}
    res["ratioNow"] = {"date": days[-1].isoformat(), "ratio": round(ratio[-1], 4),
                       "pos52w": round((ratio[-1] - min(ratio[-52:])) / (max(ratio[-52:]) - min(ratio[-52:])), 2),
                       "above26wAvg": ratio[-1] > st.mean(ratio[-26:])}
    res["meta"] = {"from": D[0].isoformat(), "to": D[-1].isoformat(), "weeks": len(D), "costPerSwitch": COST,
                   "label": "non-blind", "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    json.dump(res, open(os.path.join(HERE, "rsp_spy_backtest.json"), "w"), indent=1)
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
