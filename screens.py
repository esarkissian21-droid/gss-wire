#!/usr/bin/env python3
"""Stock screens built from SEC EDGAR XBRL 'frames' (official filings), not a website's copy.

One frames request returns a single figure (say, net income for CY2026Q3) for every
filer, so ~150 requests cover the US market. Per company it derives:
  TTM net income, revenue and diluted EPS (Q4 = annual - Q1..Q3 when Q4 isn't filed),
  quarterly year-over-year EPS and sales growth, 5-year annual EPS growth,
  ROE, net margin, debt/equity, current ratio, shares outstanding.
Prices (Yahoo) are fetched only for companies that can still pass, then:
  P/E, P/B, 200-day average, 52-week change.
Screens = the playbook's three (Value, Quality, Growth); momentum is reported as a tie-breaker.

Differences from Finviz, stated plainly:
  * "EPS growth this year" uses trailing-12-month EPS vs the TTM a year earlier
    (Finviz uses analyst estimates, which EDGAR doesn't have).
  * Debt = long-term debt (current + noncurrent) + short-term borrowings, as tagged.
  * Banks and insurers usually have no current ratio; Value screen marks that "n/a".
  * Foreign filers on IFRS (20-F/40-F, e.g. TSM, PDD, AEM) aren't covered.

Env: SEC_CONTACT ("Name email", required by the SEC), OUT_DIR (default history/screens).
"""
import datetime as dt
import json
import os
import statistics as st
import sys
import time
import urllib.error
import urllib.request

FRAMES = "https://data.sec.gov/api/xbrl/frames/{tax}/{concept}/{unit}/{period}.json"
TICKERS = "https://www.sec.gov/files/company_tickers.json"

REV = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
       "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense"]
NI = ["NetIncomeLoss"]
EPS = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]
EQUITY = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
DEBT_PARTS = {"lt": ["LongTermDebt", "LongTermDebtNoncurrent"], "ltc": ["LongTermDebtCurrent"],
              "st": ["ShortTermBorrowings", "CommercialPaper"]}
CA, CL = ["AssetsCurrent"], ["LiabilitiesCurrent"]
SHARES = [("dei", "EntityCommonStockSharesOutstanding"), ("us-gaap", "CommonStockSharesOutstanding")]

SCREENS = {
    "Value": [("pb", "<", 2), ("pe", "<", 15), ("de", "<", 0.5), ("cr", ">", 1.5)],
    "Quality": [("roe", ">", 15), ("margin", ">", 10), ("eps5y", ">", 10), ("de", "<", 0.5)],
    "Growth": [("epsYr", ">", 25), ("epsQ", ">", 25), ("salesQ", ">", 25), ("roe", ">", 15)],
}
MIN_REVENUE = 500e6  # skip micro-caps: TTM revenue of at least $500M


# ---------------------------------------------------------------- periods
def quarter_periods(today):
    """Latest calendar quarter most filers have reported (10-Qs are due ~40-45 days after quarter end)."""
    y, q = today.year, (today.month - 1) // 3  # q = last completed quarter number (0 means Q4 of last year)
    if q == 0:
        y, q = y - 1, 4
    end = dt.date(y, 3 * q, 1) + dt.timedelta(days=31)
    end = end.replace(day=1) - dt.timedelta(days=1)
    if (today - end).days < 50:  # too fresh: step back one quarter
        y, q = (y, q - 1) if q > 1 else (y - 1, 4)
    return y, q


def back(y, q, n):
    k = y * 4 + (q - 1) - n
    return k // 4, k % 4 + 1


# ---------------------------------------------------------------- fetch layer
class Frames:
    def __init__(self, fetch):
        self.fetch, self.cache, self.calls, self.missing = fetch, {}, 0, []

    def get(self, tax, concept, unit, period):
        key = (tax, concept, unit, period)
        if key in self.cache:
            return self.cache[key]
        url = FRAMES.format(tax=tax, concept=concept, unit=unit, period=period)
        self.calls += 1
        try:
            data = json.loads(self.fetch(url))
            out = {int(d["cik"]): float(d["val"]) for d in data.get("data", [])}
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            out = {}
            self.missing.append(f"{concept} {period}")
        self.cache[key] = out
        return out

    def first(self, concepts, unit, period, tax="us-gaap"):
        """Merge several concept names: first one a company reports wins."""
        out = {}
        for c in concepts:
            for cik, v in self.get(tax, c, unit, period).items():
                out.setdefault(cik, v)
        return out


def quarterly(fr, concepts, unit, y, q):
    """Value for calendar quarter (y, q); Q4 derived as annual minus Q1..Q3 when not filed."""
    direct = fr.first(concepts, unit, f"CY{y}Q{q}")
    if q != 4:
        return direct
    annual = fr.first(concepts, unit, f"CY{y}")
    parts = [fr.first(concepts, unit, f"CY{y}Q{k}") for k in (1, 2, 3)]
    out = dict(direct)
    for cik, a in annual.items():
        if cik not in out and all(cik in p for p in parts):
            out[cik] = a - sum(p[cik] for p in parts)
    return out


def ttm(fr, concepts, unit, y, q):
    qs = [quarterly(fr, concepts, unit, *back(y, q, k)) for k in range(4)]
    return {cik: sum(d[cik] for d in qs) for cik in qs[0] if all(cik in d for d in qs)}


def instant(fr, concepts, unit, y, q, tax="us-gaap"):
    a = fr.first(concepts, unit, f"CY{y}Q{q}I", tax)
    b = fr.first(concepts, unit, "CY%dQ%dI" % back(y, q, 1), tax)
    return {**b, **a}


# ---------------------------------------------------------------- metrics
def growth(new, old):
    if new is None or old is None or old <= 0:
        return None
    return 100 * (new / old - 1)


def cagr(new, old, years):
    if new is None or old is None or old <= 0 or new <= 0:
        return None
    return 100 * ((new / old) ** (1 / years) - 1)


def fundamentals(fr, today):
    y, q = quarter_periods(today)
    ni, rev, eps = ttm(fr, NI, "USD", y, q), ttm(fr, REV, "USD", y, q), ttm(fr, EPS, "USD-per-shares", y, q)
    py, pq = back(y, q, 4)
    eps_prev = ttm(fr, EPS, "USD-per-shares", py, pq)
    eps_q, eps_q_prev = quarterly(fr, EPS, "USD-per-shares", y, q), quarterly(fr, EPS, "USD-per-shares", py, pq)
    rev_q, rev_q_prev = quarterly(fr, REV, "USD", y, q), quarterly(fr, REV, "USD", py, pq)
    ay = y - 1 if q < 4 else y  # latest full calendar year
    eps_a, eps_a5 = fr.first(EPS, "USD-per-shares", f"CY{ay}"), fr.first(EPS, "USD-per-shares", f"CY{ay - 5}")
    eq = instant(fr, EQUITY, "USD", y, q)
    eq_prev = instant(fr, EQUITY, "USD", py, pq)
    debt = {}
    for part, names in DEBT_PARTS.items():
        for cik, v in instant(fr, names, "USD", y, q).items():
            debt.setdefault(cik, {})[part] = v
    ca, cl = instant(fr, CA, "USD", y, q), instant(fr, CL, "USD", y, q)
    shares = {}
    for tax, c in SHARES:
        for cik, v in instant(fr, [c], "shares", y, q, tax).items():
            shares.setdefault(cik, v)

    out = {}
    for cik in set(ni) | set(rev):
        r = rev.get(cik)
        n = ni.get(cik)
        e = eq.get(cik)
        avg_eq = st.mean([x for x in (e, eq_prev.get(cik)) if x and x > 0]) if (e and e > 0) else None
        d = debt.get(cik)
        total_debt = None if d is None else (d.get("lt", 0) + d.get("ltc", 0) + d.get("st", 0))
        m = {
            "period": f"CY{y}Q{q}",
            "revenue": r, "netIncome": n, "epsTTM": eps.get(cik), "equity": e, "shares": shares.get(cik),
            "roe": None if (n is None or not avg_eq) else 100 * n / avg_eq,
            "margin": None if (n is None or not r or r <= 0) else 100 * n / r,
            "de": de_ratio(total_debt, e, classified=(cik in ca and cik in cl)),
            "debtTagged": d is not None,
            "cr": None if (cik not in ca or not cl.get(cik)) else ca[cik] / cl[cik],
            "epsYr": growth(eps.get(cik), eps_prev.get(cik)),
            "epsQ": growth(eps_q.get(cik), eps_q_prev.get(cik)),
            "salesQ": growth(rev_q.get(cik), rev_q_prev.get(cik)),
            "eps5y": cagr(eps_a.get(cik), eps_a5.get(cik), 5),
        }
        out[cik] = m
    return out, (y, q)


def de_ratio(total_debt, equity, classified):
    """Debt/equity. No debt tags: treat as debt-free only for companies with a classified balance sheet
    (current assets/liabilities), since banks and insurers fund themselves in ways these tags miss."""
    if equity is None or equity <= 0:
        return None
    if total_debt is None:
        return 0.0 if classified else None
    return total_debt / equity


def check(m, rules):
    res = []
    for k, op, lim in rules:
        v = m.get(k)
        res.append((k, None if v is None else (v < lim if op == "<" else v > lim)))
    return res


def passes(m, rules):
    return all(ok is True for _, ok in check(m, rules))


def could_pass(m, rules):
    """True unless a non-price check already fails (unknowns don't rule a company out yet)."""
    return all(ok is not False for k, ok in check(m, rules) if k not in ("pe", "pb"))


# ---------------------------------------------------------------- prices
def yahoo_daily(sym, fetch):
    now = int(time.time())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={now - 400 * 86400}"
           f"&period2={now}&interval=1d")
    j = json.loads(fetch(url))["chart"]["result"][0]
    closes = [c for c in j["indicators"]["quote"][0]["close"] if c is not None]
    if len(closes) < 210:
        return None
    return {"price": closes[-1], "ma200": st.mean(closes[-200:]), "chg52": 100 * (closes[-1] / closes[-252] - 1) if len(closes) >= 252 else None}


def run(fetch_sec, fetch_px, today, always=(), sleep=time.sleep):
    fr = Frames(lambda u: (sleep(0.15), fetch_sec(u))[1])
    funda, (y, q) = fundamentals(fr, today)
    tick = json.loads(fetch_sec(TICKERS))
    cik2t, t2cik = {}, {}
    for v in tick.values():
        cik2t.setdefault(int(v["cik_str"]), v["ticker"])
        t2cik[v["ticker"]] = int(v["cik_str"])
    rows, need_px = {}, set()
    for cik, m in funda.items():
        t = cik2t.get(cik)
        if not t:
            continue
        big = (m["revenue"] or 0) >= MIN_REVENUE
        tracked = t in always
        if not (big or tracked):
            continue
        alive = [s for s, rules in SCREENS.items() if could_pass(m, rules)]
        if alive or tracked:
            rows[t] = dict(m, cik=cik)
            need_px.add(t)
    for t in always:
        if t not in rows:
            rows[t] = {"cik": t2cik.get(t), "note": "not in SEC US-GAAP frames (foreign IFRS filer or no recent 10-Q)"}
    px_fail = []
    for t in sorted(need_px):
        try:
            p = yahoo_daily(t.replace(".", "-"), fetch_px)
        except Exception as e:
            p, _ = None, px_fail.append(f"{t}: {type(e).__name__}")
        sleep(0.3)
        if not p:
            continue
        m = rows[t]
        m.update(p)
        if m.get("epsTTM") and m["epsTTM"] > 0:
            m["pe"] = p["price"] / m["epsTTM"]
        if m.get("shares") and m.get("equity") and m["equity"] > 0:
            m["pb"] = p["price"] * m["shares"] / m["equity"]
            m["mktCap"] = p["price"] * m["shares"]
        m["momentum"] = None if p.get("chg52") is None else (p["price"] > p["ma200"] and p["chg52"] > 0)
    results = {s: [] for s in SCREENS}
    for t, m in rows.items():
        m["screens"] = {}
        for s, rules in SCREENS.items():
            c = check(m, rules)
            m["screens"][s] = "pass" if all(ok is True for _, ok in c) else (
                "fail" if any(ok is False for _, ok in c) else "unknown")
            if m["screens"][s] == "pass":
                results[s].append(t)
    for s in results:
        results[s].sort(key=lambda t: (not rows[t].get("momentum"), -(rows[t].get("mktCap") or 0)))
    def rnd(v):
        return round(v, 2) if isinstance(v, float) else v
    return {
        "asOf": today.isoformat(), "fundamentalsThrough": f"CY{y}Q{q}",
        "source": "SEC EDGAR XBRL frames (us-gaap/dei) + Yahoo Finance prices",
        "rules": {s: [f"{k} {op} {lim}" for k, op, lim in r] for s, r in SCREENS.items()},
        "minRevenue": MIN_REVENUE, "companiesWithData": len(funda), "considered": len(rows),
        "secCalls": fr.calls, "missingFrames": fr.missing[:40], "priceFailures": px_fail[:40],
        "passes": results,
        "rows": {t: {k: rnd(v) for k, v in m.items()} for t, m in rows.items()},
    }


def main():
    contact = os.environ.get("SEC_CONTACT", "").strip()
    if not contact:
        print("SEC_CONTACT secret is missing (the SEC requires a name and email).", file=sys.stderr)
        return 1
    out_dir = os.environ.get("OUT_DIR", "history/screens")

    def sec(url):
        req = urllib.request.Request(url, headers={"User-Agent": contact, "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    def px(url):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (gss-wire screens; personal research)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()

    cfg = json.load(open("config.json"))
    always = [c["ticker"] for c in cfg["companies"]]
    res = run(sec, px, dt.date.today(), always)
    os.makedirs(out_dir, exist_ok=True)
    for name in ("latest.json", f"{res['asOf']}.json"):
        with open(os.path.join(out_dir, name), "w") as f:
            json.dump(res, f, indent=0, sort_keys=True, default=str)
    print(json.dumps({k: res[k] for k in ("asOf", "fundamentalsThrough", "companiesWithData", "considered", "secCalls")}))
    for s, ts in res["passes"].items():
        print(f"{s}: {len(ts)} pass -> {', '.join(ts[:25])}")
    print("watchlist:", {t: res["rows"][t].get("screens", res["rows"][t].get("note")) for t in always})
    print("missing frames:", res["missingFrames"][:10], "price failures:", res["priceFailures"][:10])
    return 0


if __name__ == "__main__":
    sys.exit(main())
