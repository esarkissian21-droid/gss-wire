"""Backtest gold/silver ratio rules, scored in ounces of gold.

The silver trading stake starts as 1.000 oz of gold. Rules move parts of it
into silver when silver is cheap versus gold (high ratio) and back into gold
when the ratio falls. Doing nothing (holding gold) scores exactly 1.000.

Weekly closes, Jan 2012 - Nov 2026 (Alpha Vantage GOLD_SILVER_HISTORY).
No parameter in this file was tuned on this data: levels come from the
playbook (set Nov 2026) or are round, a-priori choices; sensitivity runs
below show how much the answer moves when they change.
"""
import json
import sys

import numpy as np
import pandas as pd

D = pd.read_csv(__import__("os").path.join(__import__("os").path.dirname(__import__("os").path.abspath(__file__)), "merged_weekly.csv"), parse_dates=["date"]).sort_values("date").reset_index(drop=True)
D["ratio"] = D.gold / D.silver

COSTS = {"etf": 0.0025, "physical": 0.07}  # cost per conversion, each direction


def tranche_machine(r, buy_lv, sell_lv, cost, buy_ok=None, sell_ok=None, pairing="lifo"):
    """Playbook mechanics as three independent thirds of the stake.
    Each third starts as 1/3 oz of gold, switches into silver when the ratio
    reaches its buy level and back into gold at its paired sell level, and can
    repeat. Pairing "lifo" (default): the third bought at the highest ratio is
    sold first (90->72, 86->66, 81->60). "fifo": 81->72, 86->66, 90->60."""
    T = len(r)
    pairs = [(0, 2), (1, 1), (2, 0)] if pairing == "lifo" else [(0, 0), (1, 1), (2, 2)]
    gold = [1 / 3] * 3
    silv = [0.0] * 3
    trades, eq = [], np.empty(T)
    for t in range(T):
        x = r[t]
        for slot, (bk, sk) in enumerate(pairs):
            if silv[slot] > 0 and x <= sell_lv[t][sk] and (sell_ok is None or sell_ok[t]):
                gold[slot], silv[slot] = silv[slot] / x * (1 - cost), 0.0
                trades.append((t, "sell", slot, x))
            elif gold[slot] > 0 and x >= buy_lv[t][bk] and (buy_ok is None or buy_ok[t]):
                silv[slot], gold[slot] = gold[slot] * x * (1 - cost), 0.0
                trades.append((t, "buy", slot, x))
        eq[t] = sum(gold) + sum(silv) / x
    return eq, trades


def trend_only(r, lookback, cost):
    """Hold the whole stake in silver while the ratio is below where it was
    `lookback` weeks ago (silver outperforming), otherwise in gold."""
    T = len(r)
    gold, silv, trades, eq = 1.0, 0.0, [], np.empty(T)
    for t in range(T):
        x = r[t]
        want_silver = t >= lookback and x < r[t - lookback]
        if want_silver and gold > 0:
            silv, gold = gold * x * (1 - cost), 0.0
            trades.append((t, "buy", 0, x))
        elif not want_silver and silv > 0:
            gold, silv = silv / x * (1 - cost), 0.0
            trades.append((t, "sell", 0, x))
        eq[t] = gold + silv / x
    return eq, trades


def strategies(df, cost, fixed_buy=(81, 86, 90), fixed_sell=(72, 66, 60), pct_buy=(0.70, 0.80, 0.90),
               pct_sell=(0.30, 0.20, 0.10), ma=13, tl=26, window=260, pairing="lifo"):
    r = df.ratio.to_numpy()
    T = len(r)
    fb = np.tile(fixed_buy, (T, 1)).astype(float)
    fs = np.tile(fixed_sell, (T, 1)).astype(float)
    roll = df.ratio.rolling(window, min_periods=window)
    pb = np.column_stack([roll.quantile(q).shift(1).to_numpy() for q in pct_buy])
    ps = np.column_stack([roll.quantile(q).shift(1).to_numpy() for q in pct_sell])
    pb = np.nan_to_num(pb, nan=np.inf)   # no trading until 5 years of history exist
    ps = np.nan_to_num(ps, nan=-np.inf)
    m = df.ratio.rolling(ma).mean().to_numpy()
    falling = np.nan_to_num(r < m, nan=False)     # ratio below its average: silver starting to win
    rising = np.nan_to_num(r > m, nan=False)
    out = {}
    out["Hold gold (do nothing)"] = (np.ones(T), [])
    out["Hold silver"] = (np.r_[[1.0], (r[0] * (1 - cost)) / r[1:]], [(0, "buy", 0, r[0])])
    out["A. Your fixed lines 81/86/90 -> 72/66/60"] = tranche_machine(r, fb, fs, cost, pairing=pairing)
    out["B. Moving bands (5-yr 70/80/90th -> 30/20/10th pct)"] = tranche_machine(r, pb, ps, cost, pairing=pairing)
    out["C. Fixed lines + trend filter"] = tranche_machine(r, fb, fs, cost, buy_ok=falling, sell_ok=rising, pairing=pairing)
    out["D. Trend only (26-week)"] = trend_only(r, tl, cost)
    return out


def summarize(df, res):
    years = (df.date.iloc[-1] - df.date.iloc[0]).days / 365.25
    rows = []
    for name, (eq, trades) in res.items():
        eq = np.asarray(eq)
        peak = np.maximum.accumulate(eq)
        rows.append({
            "strategy": name,
            "gold_oz_end": round(float(eq[-1]), 3),
            "per_year_%": round((float(eq[-1]) ** (1 / years) - 1) * 100, 2),
            "worst_drop_%": round(float((eq / peak - 1).min()) * 100, 1),
            "trades": len(trades),
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    out = {}
    for label, cost in COSTS.items():
        res = strategies(D, cost)
        s = summarize(D, res)
        out[label] = s
        print(f"\n=== Full period {D.date.iloc[0].date()} to {D.date.iloc[-1].date()} | cost per conversion {cost:.2%} ({label}) ===")
        print(s.to_string(index=False))
    json.dump({k: v.to_dict("records") for k, v in out.items()}, open("results_full.json", "w"), indent=1)
