#!/usr/bin/env python3
"""Two chart rules from a social-media Broadcom post (Nov 24 2026), tested on ~116 large US stocks, 2005-2026.

1. Bearish divergence: the stock makes a new 52-week closing high (weekly closes) while its 14-week RSI
   is at least 5 points LOWER than the RSI at its previous 52-week high 8-40 weeks earlier
   ("price up, strength down"). Claim: it falls afterwards.
   Compared with: new 52-week highs WITHOUT that divergence (same situation, no warning), and all weeks.
2. 200-week average as a buy point: the first week the stock closes below its 200-week average after
   at least 52 weeks above it. Claim: a good place to buy.
   Compared with: all weeks.

Outcomes: return vs SPY over the next 13 and 26 weeks (and 52 for rule 2). One event per stock per 13 weeks.
t-stats clustered by calendar month (events in the same month move together); split 2005-2015 vs 2016-2026.
Weekly closes come from DAILY dividend-adjusted prices (weekly bars had glitches, see Input 18).
Biases: today's large companies only (survivorship), non-blind. Writes research/divergence_backtest.json.
"""
import datetime as dt
import json
import math
import os
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from move_odds import UNIVERSE, daily  # noqa: E402

GAP = 13


def weekly(rows):
    out = {}
    for d, a, _ in rows:
        day = dt.date.fromisoformat(d)
        out[day - dt.timedelta(days=day.weekday())] = a
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
        if i == n:
            ag, al = st.mean(gains), st.mean(losses)
        elif i > n:
            ag = (ag * (n - 1) + gains[-1]) / n
            al = (al * (n - 1) + losses[-1]) / n
        else:
            continue
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def clustered(events):
    """events: list of (date, excess). Mean, month-clustered t."""
    if len(events) < 5:
        return None
    by = {}
    for d, e in events:
        by.setdefault((d.year, d.month), []).append(e)
    m = [st.mean(v) for v in by.values()]
    t = st.mean(m) / (st.stdev(m) / math.sqrt(len(m))) if len(m) > 2 and st.stdev(m) > 0 else None
    return {"n": len(events), "months": len(m), "mean": round(100 * st.mean(e for _, e in events), 2),
            "beatShare": round(100 * sum(1 for _, e in events if e > 0) / len(events), 1),
            "t": None if t is None else round(t, 2)}


def main():
    syms = UNIVERSE + ["SPY"]
    def safe(sym):
        time.sleep(0.2)
        try:
            return daily(sym)
        except Exception:
            return None

    with ThreadPoolExecutor(6) as ex:
        data = dict(zip(syms, ex.map(safe, syms)))
    print("fetched", sum(1 for v in data.values() if v), "of", len(syms), flush=True)
    spy = weekly(data["SPY"])
    weeks = sorted(spy)
    idx = {w: i for i, w in enumerate(weeks)}
    sp = [spy[w] for w in weeks]

    def excess(px, i, h):
        if i + h >= len(px) or px[i] is None or px[i + h] is None:
            return None
        return (px[i + h] / px[i]) / (sp[i + h] / sp[i]) - 1

    groups = {k: {"13": [], "26": [], "52": []} for k in ("divergence", "highNoDivergence", "allWeeks", "below200w")}
    examples = []
    for s in UNIVERSE:
        w = weekly(data.get(s) or [])
        px = [w.get(x) for x in weeks]
        # forward-fill short gaps
        for i in range(1, len(px)):
            if px[i] is None and px[i - 1] is not None and weeks[i] in w:
                px[i] = px[i - 1]
        valid = [p for p in px if p is not None]
        if len(valid) < 300:
            continue
        r = rsi([p if p is not None else 0 for p in px])
        last_high = None  # (index, rsi) of previous 52-week high
        last_ev = {"div": -99, "nodiv": -99, "b200": -99}
        above_run = 0
        for i in range(260, len(px)):
            p = px[i]
            if p is None or any(x is None for x in px[i - 252 // 5:i]):
                continue
            window = [x for x in px[i - 52:i] if x is not None]
            new_high = p > max(window)
            # all weeks baseline (every 13th week to keep samples comparable)
            if i % GAP == 0:
                for h in ("13", "26", "52"):
                    e = excess(px, i, int(h))
                    if e is not None:
                        groups["allWeeks"][h].append((weeks[i], e))
            if new_high and r[i] is not None:
                div = (last_high is not None and 8 <= i - last_high[0] <= 40 and r[i] <= last_high[1] - 5)
                key = "div" if div else "nodiv"
                if i - last_ev[key] >= GAP:
                    g = "divergence" if div else "highNoDivergence"
                    for h in ("13", "26"):
                        e = excess(px, i, int(h))
                        if e is not None:
                            groups[g][h].append((weeks[i], e))
                    last_ev[key] = i
                    if div and s in ("AVGO", "NVDA", "MSFT", "AAPL") and len(examples) < 30:
                        examples.append({"t": s, "week": weeks[i].isoformat(), "rsi": round(r[i], 1),
                                         "prevRsi": round(last_high[1], 1)})
                last_high = (i, r[i])
            # 200-week average
            if i >= 200 and all(x is not None for x in px[i - 199:i + 1]):
                ma = st.mean(px[i - 199:i + 1])
                if p >= ma:
                    above_run += 1
                else:
                    if above_run >= 52 and i - last_ev["b200"] >= GAP:
                        for h in ("13", "26", "52"):
                            e = excess(px, i, int(h))
                            if e is not None:
                                groups["below200w"][h].append((weeks[i], e))
                        last_ev["b200"] = i
                    above_run = 0

    cut = dt.date(2016, 1, 1)
    summary = {}
    for g, hs in groups.items():
        summary[g] = {}
        for h, ev in hs.items():
            if not ev:
                continue
            summary[g][h + "w"] = {"all": clustered(ev),
                                   "2005-2015": clustered([x for x in ev if x[0] < cut]),
                                   "2016-2026": clustered([x for x in ev if x[0] >= cut])}
    out = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "universe": len(UNIVERSE), "label": "non-blind; survivorship (today's large caps)",
           "summary": summary, "examples": examples}
    json.dump(out, open(os.path.join(HERE, "divergence_backtest.json"), "w"), indent=1, default=str)
    print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
