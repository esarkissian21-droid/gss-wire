"""Strategy tracker: user-defined stock-picking rules, run weekly, logged, and scored vs SPY.

Pure functions; screens.py supplies company rows and price histories.
"""
import datetime as dt
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC_PATH = os.path.join(HERE, "strategies", "strategies.json")
LEDGER_PATH = os.path.join(HERE, "strategies", "ledger.jsonl")


def ok(v, op, lim):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return False
    return {"<": v < lim, "<=": v <= lim, ">": v > lim, ">=": v >= lim, "==": v == lim}[op]


def passes(m, conds):
    return all(ok(m.get(k), op, lim) for k, op, lim in conds)


def prefilter_universe(spec, rows):
    """Tickers that clear each strategy's no-price checks (so screens.py knows which prices to fetch)."""
    need = set()
    for s in spec["strategies"]:
        for t, m in rows.items():
            if passes(m, s.get("prefilters", [])):
                need.add(t)
    return need


def sector_of(m):
    """2-digit SIC major group (e.g. 63 = insurance carriers, 35 = industrial machinery & computers)."""
    sic = str(m.get("sic") or "")
    return sic[:2] if len(sic) >= 2 else None


def ranked(strategy, rows):
    elig = {t: m for t, m in rows.items() if passes(m, strategy.get("prefilters", [])) and passes(m, strategy.get("filters", []))}
    for k, _, _ in strategy["rank"]:
        elig = {t: m for t, m in elig.items() if m.get(k) is not None}
    if not elig:
        return [], {}
    score = {t: 0.0 for t in elig}
    n = len(elig)
    for k, direction, w in strategy["rank"]:
        order = sorted(elig, key=lambda t: elig[t][k], reverse=(direction == "desc"))
        for i, t in enumerate(order):
            score[t] += w * (i / max(1, n - 1))  # 0 = best
    return sorted(score, key=lambda t: (score[t], t)), score


def pick(strategy, rows):
    order, score = ranked(strategy, rows)
    cap = strategy.get("maxPerSector")
    chosen, per = [], {}
    for t in order:
        sec = sector_of(rows[t]) if cap else None
        if cap and sec is not None and per.get(sec, 0) >= cap:
            continue
        chosen.append(t)
        if sec is not None:
            per[sec] = per.get(sec, 0) + 1
        if len(chosen) >= strategy["top"]:
            break
    return [{"t": t, "score": round(score[t], 3), "sector": rows[t].get("industry"), **{k: rows[t].get(k) for k, _, _ in strategy["rank"]}} for t in chosen]


def beta(closes, spy_closes):
    """Beta from aligned daily closes: {date: close}. Uses up to the last 252 common days."""
    days = sorted(set(closes) & set(spy_closes))[-253:]
    if len(days) < 120:
        return None
    r = [closes[days[i]] / closes[days[i - 1]] - 1 for i in range(1, len(days))]
    m = [spy_closes[days[i]] / spy_closes[days[i - 1]] - 1 for i in range(1, len(days))]
    mm, rm = sum(m) / len(m), sum(r) / len(r)
    cov = sum((a - rm) * (b - mm) for a, b in zip(r, m))
    var = sum((b - mm) ** 2 for b in m)
    return cov / var if var else None


def read_ledger():
    if not os.path.exists(LEDGER_PATH):
        return []
    with open(LEDGER_PATH) as f:
        return [json.loads(l) for l in f if l.strip()]


def write_ledger(rows):
    with open(LEDGER_PATH, "w") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def price_on(series, day, tol=5):
    """Close on `day` or the nearest earlier trading day within tol days."""
    for k in range(tol + 1):
        d = (day - dt.timedelta(days=k)).isoformat()
        if d in series:
            return series[d]
    return None


def log_and_score(spec, rows, today, histories, spy_hist, ledger):
    """Append this week's picks (from each strategy's startDate) and fill outcomes whose horizon has passed.
    histories: {ticker: {date_iso: close}}; spy_hist the same for SPY."""
    new = []
    for s in spec["strategies"]:
        if today < dt.date.fromisoformat(s["startDate"]):
            continue
        if any(r["strategy"] == s["id"] and r["date"] == today.isoformat() for r in ledger):
            continue
        picks = pick(s, rows)
        spy_px = price_on(spy_hist, today)
        entry = {"strategy": s["id"], "date": today.isoformat(), "spyAt": spy_px, "out": {},
                 "picks": [{"t": p["t"], "px": price_on(histories.get(p["t"], {}), today)} for p in picks]}
        new.append(entry)
    ledger = ledger + new
    scored = 0
    for e in ledger:
        spec_s = next((s for s in spec["strategies"] if s["id"] == e["strategy"]), None)
        hs = spec_s["horizonsWeeks"] if spec_s else [13, 26, 52]
        d0 = dt.date.fromisoformat(e["date"])
        for h in hs:
            k = str(h)
            if k in e["out"]:
                continue
            d1 = d0 + dt.timedelta(weeks=h)
            if d1 > today:
                continue
            spy1 = price_on(spy_hist, d1)
            if not spy1 or not e.get("spyAt"):
                continue
            ex = []
            for p in e["picks"]:
                p1 = price_on(histories.get(p["t"], {}), d1)
                if p1 and p.get("px"):
                    ex.append((p1 / p["px"]) / (spy1 / e["spyAt"]) - 1)
            if not ex:
                continue
            e["out"][k] = {"meanExcess": round(sum(ex) / len(ex), 4), "beat": sum(1 for x in ex if x > 0), "n": len(ex)}
            scored += 1
    return ledger, new, scored


def tickers_to_price(ledger, today):
    """Past picks still awaiting an outcome need prices too."""
    out = set()
    for e in ledger:
        if len(e["out"]) < 3 and (today - dt.date.fromisoformat(e["date"])).days < 400:
            out.update(p["t"] for p in e["picks"])
    return out
