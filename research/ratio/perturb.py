"""Perturbation test of the gold/silver ratio playbook (method shared on social media, Nov 2026).

Each run nudges every line by up to +/-5 ratio points (keeping buy lines above sell lines and in order)
and adds 1% random noise to every historical ratio, then reruns the three-thirds playbook with ETF costs.
1,000 runs per dataset. Score = ounces of gold at the end per 1 oz at the start (holding gold = 1.00).
"""
import csv
import os
import json

import numpy as np

from ratio_backtest import tranche_machine

COST = 0.0025
RUNS = 1000
rng = np.random.default_rng(42)


def weekly():
    rows = list(csv.DictReader(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "merged_weekly.csv"))))
    return np.array([float(r["ratio"]) for r in rows]), rows[0]["date"], rows[-1]["date"]


def monthly(a="1972-01", b="2026-10"):
    rows = [r for r in csv.DictReader(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "history", "long", "pink_metals_monthly.csv")))
            if a <= r["month"] <= b and r["gold_usd_oz"] and r["silver_usd_oz"]]
    return np.array([float(r["gold_usd_oz"]) / float(r["silver_usd_oz"]) for r in rows]), rows[0]["month"], rows[-1]["month"]


def run(r, buy, sell):
    T = len(r)
    eq, trades = tranche_machine(r, np.tile(buy, (T, 1)), np.tile(sell, (T, 1)), COST)
    return eq[-1], len(trades)


def perturbed_lines():
    while True:
        b = np.array([81, 86, 90]) + rng.uniform(-5, 5, 3)
        s = np.array([72, 66, 60]) + rng.uniform(-5, 5, 3)
        if b[0] < b[1] < b[2] and s[0] > s[1] > s[2] and s[0] < b[0]:
            return b, s


def test(r, label, years):
    base, base_trades = run(r, np.array([81, 86, 90.0]), np.array([72, 66, 60.0]))
    res = {"lines": [], "noise": [], "both": []}
    for _ in range(RUNS):
        b, s = perturbed_lines()
        res["lines"].append(run(r, b, s)[0])
        noisy = r * (1 + rng.normal(0, 0.01, len(r)))
        res["noise"].append(run(noisy, np.array([81, 86, 90.0]), np.array([72, 66, 60.0]))[0])
        res["both"].append(run(noisy, b, s)[0])
    out = {"label": label, "years": years, "original": round(float(base), 3), "originalTrades": base_trades}
    for k, v in res.items():
        v = np.array(v)
        out[k] = {"beatGold": round(float((v > 1.0).mean()), 3), "median": round(float(np.median(v)), 3),
                  "p5": round(float(np.percentile(v, 5)), 3), "p95": round(float(np.percentile(v, 95)), 3),
                  "medianPerYear": round(100 * (float(np.median(v)) ** (1 / years) - 1), 2)}
    return out


if __name__ == "__main__":
    out = []
    r, a, b = weekly()
    out.append(test(r, f"Weekly {a} to {b}", 14.9))
    r, a, b = monthly()
    out.append(test(r, f"Monthly {a} to {b}", 54.8))
    for a, b, yrs in (("1972-01", "1991-12", 20), ("1992-01", "2011-12", 20), ("2012-01", "2026-10", 14.8)):
        r, _, _ = monthly(a, b)
        out.append(test(r, f"Monthly {a} to {b}", yrs))
    json.dump(out, open("perturb_results.json", "w"), indent=1)
    for o in out:
        print(o["label"], "| original", o["original"], f"({o['originalTrades']} trades)")
        for k in ("lines", "noise", "both"):
            d = o[k]
            print(f"   {k:6} beat gold {100*d['beatGold']:.0f}% | median {d['median']} ({d['medianPerYear']}%/yr) | 5th-95th pct {d['p5']} to {d['p95']}")
