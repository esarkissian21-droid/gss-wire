"""Long-run gold/silver ratio study on World Bank monthly averages, 1972-2026.

Non-blind (the lines were chosen in 2026 after this history happened), but ~3x the
sample of the 2012-2026 weekly replay and it spans very different eras. Produces:
  * per-line episode results at 3/6/12 months, overall and by era
  * zone base rates: P(ratio lower in h months | ratio in zone) with Wilson intervals,
    using non-overlapping effective sample sizes
  * shrunk probabilities to freeze as forward-test predictions
"""
import csv
import os
import json
import math
import statistics as st

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "history", "long", "pink_metals_monthly.csv")
START = "1972-01"
END = "2026-10"
UP, DOWN = [81, 86, 90], [72, 66, 60]
H = [3, 6, 12]
ERAS = [("1972-1991", "1972-01", "1991-12"), ("1992-2011", "1992-01", "2011-12"), ("2012-2026", "2012-01", "2026-12")]
ZONES = [("below 60", 0, 60), ("60-66", 60, 66), ("66-72", 66, 72), ("72-81", 72, 81),
         ("81-86", 81, 86), ("86-90", 86, 90), ("90+", 90, 999)]


def load():
    rows = []
    for r in csv.DictReader(open(SRC)):
        if START <= r["month"] <= END and r["gold_usd_oz"] and r["silver_usd_oz"]:
            g, s = float(r["gold_usd_oz"]), float(r["silver_usd_oz"])
            rows.append((r["month"], g, s, g / s))
    return rows


def wilson(k, n, z=1.645):  # 90% interval
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - m), min(1.0, c + m))


def summ(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0}
    k = sum(1 for v in vals if v > 0)
    lo, hi = wilson(k, len(vals))
    return {"n": len(vals), "hit": round(100 * k / len(vals), 1), "median": round(100 * st.median(vals), 2),
            "lo": round(100 * lo), "hi": round(100 * hi)}


def main():
    rows = load()
    months = [r[0] for r in rows]
    ratio = [r[3] for r in rows]
    n = len(rows)

    # ---- line crosses (monthly), 3-month cooldown = one episode
    events, last = [], {}
    for i in range(1, n):
        prev, now = ratio[i - 1], ratio[i]
        for d, lines in (("up", UP), ("down", DOWN)):
            for L in lines:
                if (d == "up" and prev < L <= now) or (d == "down" and prev > L >= now):
                    key = (d, L)
                    ep = key not in last or i - last[key] > 3
                    last[key] = i
                    out = {}
                    for h in H:
                        if i + h < n:
                            later = ratio[i + h]
                            out[h] = now / later - 1 if d == "up" else later / now - 1
                    events.append({"m": months[i], "dir": d, "L": L, "r": round(now, 2), "ep": ep, "out": out})

    lines_out = []
    for d, lines in (("up", UP), ("down", DOWN)):
        for L in lines:
            eps = [e for e in events if e["dir"] == d and e["L"] == L and e["ep"]]
            row = {"dir": d, "level": L, "crosses": sum(1 for e in events if e["dir"] == d and e["L"] == L),
                   "episodes": len(eps), "h": {h: summ([e["out"].get(h) for e in eps]) for h in H}, "eras": {}}
            for name, a, b in ERAS:
                ee = [e for e in eps if a <= e["m"] <= b]
                row["eras"][name] = {"episodes": len(ee), "y1": summ([e["out"].get(12) for e in ee])}
            lines_out.append(row)

    # ---- zone base rates: P(ratio lower after h months | zone now)
    zones_out, preds = [], {}
    all_lower = {h: [ratio[i + h] < ratio[i] for i in range(n - h)] for h in H}
    base = {h: sum(all_lower[h]) / len(all_lower[h]) for h in H}
    for name, lo, hi in ZONES:
        zr = {"zone": name, "months": sum(1 for x in ratio if lo <= x < hi), "h": {}}
        preds[name] = {}
        for h in H:
            idx = [i for i in range(n - h) if lo <= ratio[i] < hi]
            k = sum(1 for i in idx if ratio[i + h] < ratio[i])
            m = len(idx)
            n_eff = m / h  # overlapping windows: about one independent observation per h months
            k_eff = k / h
            lo_w, hi_w = wilson(k_eff, n_eff)
            p_raw = k / m if m else None
            p_shrunk = (k_eff + 1) / (n_eff + 2) if m else 0.5  # pulls thin zones toward 50%
            eras = {}
            for en, a, b in ERAS:
                ii = [i for i in idx if a <= months[i] <= b]
                eras[en] = None if not ii else round(100 * sum(1 for i in ii if ratio[i + h] < ratio[i]) / len(ii))
            zr["h"][h] = {"n": m, "nEff": round(n_eff, 1), "pLower": None if p_raw is None else round(100 * p_raw, 1),
                          "lo": round(100 * lo_w), "hi": round(100 * hi_w), "pShrunk": round(100 * p_shrunk, 1), "eras": eras}
            preds[name][h] = round(p_shrunk, 3)
        zones_out.append(zr)

    out = {"source": "World Bank Pink Sheet monthly averages", "from": months[0], "to": months[-1], "months": n,
           "ratioRange": [round(min(ratio), 1), round(max(ratio), 1)],
           "baseLower": {h: round(100 * base[h], 1) for h in H},
           "lines": lines_out, "zones": zones_out, "predictions": preds,
           "events": [e for e in events if e["ep"]]}
    json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "long_ratio.json"), "w"), indent=1, default=str)
    return out


if __name__ == "__main__":
    o = main()
    print(o["from"], o["to"], o["months"], "months; ratio range", o["ratioRange"], "base P(lower):", o["baseLower"])
    print("\nLINES (episodes; 1y hit% [90% CI] median gold-oz; by era 1y hit/n)")
    for r in o["lines"]:
        y = r["h"][12]; m3 = r["h"][3]
        er = " | ".join(f'{k}: {v["y1"].get("hit","-")}%/{v["episodes"]}' for k, v in r["eras"].items())
        print(f'{r["dir"]:4} {r["level"]}: ep {r["episodes"]:2} (x{r["crosses"]:2}) 3m {m3.get("hit")}% | 1y {y.get("hit")}% [{y.get("lo")}-{y.get("hi")}] med {y.get("median")}% | {er}')
    print("\nZONES: P(ratio lower after h months)  raw% [90% CI on effective n] shrunk%  eras")
    for z in o["zones"]:
        for h in (3, 12):
            d = z["h"][h]
            print(f'{z["zone"]:9} {h:2}m  n={d["n"]:3} nEff={d["nEff"]:5} raw {d["pLower"]}% [{d["lo"]}-{d["hi"]}] shrunk {d["pShrunk"]}%  eras {d["eras"]}')
