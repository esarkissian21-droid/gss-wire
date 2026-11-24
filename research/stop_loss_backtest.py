#!/usr/bin/env python3
"""Do stop-losses help a long-term stock picker? Weekly adjusted closes 2012-2026 (history/data).

Universe: the 20 largest S&P 500 stocks at the end of 2011 (chosen before the test period).
Each trade: buy at a weekly close, hold 52 weeks. Entries every 4 weeks per stock (overlapping trades;
results are also shown for non-overlapping yearly entries as a check).
Rules compared, all judged on weekly closes (a real stop would trigger intraweek, usually a bit earlier):
  hold                 hold the full 52 weeks
  stop X%              sell when the close is X% below cost; the money then sits in SPY until week 52
  review15/sell25      sell at -25%, but only if the stock is also below its 52-week-earlier price
                       trend (40-week average) at that point -- a proxy for "the thesis is broken"
Costs: 0.1% per trade. Taxes ignored (stops in a taxable account would add short-term gains/losses).
Non-blind: thresholds come from the playbook discussion, not tuned here. Survivorship is limited by the
fixed 2011 list, but it is still only 20 large companies.
"""
import csv, json, os, statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "history", "data")
UNIVERSE = "AAPL XOM MSFT IBM CVX GE PG JNJ T PFE WMT KO GOOGL ORCL WFC JPM INTC PM MRK VZ".split()
H, STEP, COST = 52, 4, 0.001


def load(t):
    rows = [(r["date"], float(r["adjclose"])) for r in csv.DictReader(open(os.path.join(DATA, f"{t}.csv"))) if r["adjclose"]]
    return rows


spy = dict(load("SPY"))


def trade(px, dates, i, rule):
    p0 = px[i]
    for k in range(1, H + 1):
        p = px[i + k]
        dd = p / p0 - 1
        hit = False
        if rule[0] == "stop" and dd <= -rule[1]:
            hit = True
        elif rule[0] == "trend" and dd <= -0.25:
            ma = st.mean(px[max(0, i + k - 40): i + k + 1])
            hit = p < ma
        if hit:
            d0, d1 = dates[i + k], dates[i + H]
            if d0 in spy and d1 in spy:
                after = spy[d1] / spy[d0]
            else:
                after = 1.0
            return (1 + dd) * (1 - COST) * after * (1 - COST) - 1, True, px[i + H] / p - 1
    return px[i + H] / p0 - 1, False, None


RULES = {"hold": ("hold",), "stop -8%": ("stop", 0.08), "stop -15%": ("stop", 0.15), "stop -25%": ("stop", 0.25),
         "-25% only if trend broken": ("trend",)}


def run(step):
    res = {name: [] for name in RULES}
    stops = {name: [] for name in RULES}
    for t in UNIVERSE:
        rows = load(t)
        dates = [d for d, _ in rows]
        px = [p for _, p in rows]
        for i in range(40, len(px) - H, step):
            for name, rule in RULES.items():
                r, stopped, after = trade(px, dates, i, rule)
                res[name].append(r)
                if stopped:
                    stops[name].append(after)
    out = {}
    hold = res["hold"]
    for name in RULES:
        xs = sorted(res[name])
        n = len(xs)
        out[name] = {
            "trades": n, "meanPct": round(100 * st.mean(xs), 2), "medianPct": round(100 * st.median(xs), 2),
            "worst5pctPct": round(100 * xs[int(0.05 * n)], 1), "lossOver30Pct": round(100 * sum(x <= -0.30 for x in xs) / n, 1),
            "stoppedPct": round(100 * len(stops[name]) / n, 1),
            "stoppedThenRecoveredPct": round(100 * sum(a > 0 for a in stops[name]) / len(stops[name]), 1) if stops[name] else None,
            "beatsHoldPct": round(100 * sum(a > b for a, b in zip(res[name], hold)) / n, 1),
        }
    return out


out = {"universe": UNIVERSE, "horizonWeeks": H, "costPerTrade": COST,
       "overlapping_every4w": run(STEP), "nonOverlapping_every52w": run(52),
       "notes": ["Weekly closes; real stops fill intraweek, usually slightly better or worse.",
                 "After a stop the money sits in SPY for the rest of the year (not cash).",
                 "Taxes ignored; stops in taxable accounts add short-term gains/losses.",
                 "20 large 2011 leaders only; small or speculative stocks may behave differently (more momentum in losers)."]}
json.dump(out, open(os.path.join(HERE, "stop_loss_backtest.json"), "w"), indent=1)
for k in ("overlapping_every4w", "nonOverlapping_every52w"):
    print(k)
    for name, v in out[k].items():
        print(f"  {name:28} {v}")
