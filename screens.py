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
import math
import os
import statistics as st
import sys
import time
import urllib.error
import urllib.request

import strategies_engine as se

FRAMES = "https://data.sec.gov/api/xbrl/frames/{tax}/{concept}/{unit}/{period}.json"
TICKERS = "https://www.sec.gov/files/company_tickers.json"

REV = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
       "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense"]
NI = ["NetIncomeLoss"]
EPS = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]
EQUITY = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
DEBT_PARTS = {
    # long-term debt in total (current + noncurrent), else its noncurrent part
    "lt": ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligations", "DebtLongtermAndShorttermCombinedAmount",
           "LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligationsNoncurrent", "SeniorLongTermNotes",
           "UnsecuredLongTermDebt", "LongTermNotesPayable", "OtherLongTermDebtNoncurrent", "DebtInstrumentCarryingAmount"],
    "ltc": ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent", "DebtCurrent"],
    "st": ["ShortTermBorrowings", "CommercialPaper", "NotesPayableCurrent"],
}
TOTAL_FORMS = {"LongTermDebt", "LongTermDebtAndCapitalLeaseObligations", "DebtLongtermAndShorttermCombinedAmount"}
CA, CL = ["AssetsCurrent"], ["LiabilitiesCurrent"]
CASH = ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]
STRATEGY_MIN_REVENUE = 100e6  # strategies may look at smaller companies than the playbook screens
SHARES = [("dei", "EntityCommonStockSharesOutstanding"), ("us-gaap", "CommonStockSharesOutstanding")]

SCREENS = {
    "Value": [("pb", "<", 2), ("pe", "<", 15), ("de", "<", 0.5), ("cr", ">", 1.5)],
    "Quality": [("roe", ">", 15), ("margin", ">", 10), ("eps5y", ">", 10), ("de", "<", 0.5)],
    "Growth": [("epsYr", ">", 25), ("epsQ", ">", 25), ("salesQ", ">", 25), ("roe", ">", 15)],
}
MIN_REVENUE = 500e6  # skip micro-caps: TTM revenue of at least $500M

# Grade A (frozen 2026-11-22): passes Quality, sensibly priced, trend up but not chased, no one-off profits.
GRADE_A_VERSION = "v1-2026-11-22"
GRADE_A_RULES = ["passes the Quality screen", "P/E above 0 and at most 25", "debt/equity known and under 0.5",
                 "price above its 200-day average and up over 52 weeks", "up no more than 60% over 52 weeks",
                 "no red flags (one-off profits)", "TTM revenue at least $500M"]



# Options paper practice (rules set 2026-11-22/23): which allowed stocks currently pass the stock rules,
# with a rough cost for a 3-month at-the-money call (about 0.4 x volatility x sqrt(time) x price).
OPTION_EXCLUDE = {"CB", "ACGL", "SPNT", "AMZN", "NVDA", "TSM", "PDD", "AEM", "FIX", "NUTX"}
OPTION_TIER_A = ("TRV", "CBOE", "SSD")


def option_setups(rows, grade_list, today):
    out = []
    for t in list(OPTION_TIER_A) + [g for g in grade_list if g not in OPTION_TIER_A]:
        m = rows.get(t)
        if t in OPTION_EXCLUDE or not m or not m.get("price") or not m.get("vol3m"):
            continue
        if not m.get("momentum") or (m.get("chg52") or 0) > 60:
            continue
        if (m.get("jump5d63") or 0) >= 0.15:
            continue  # on the 3-month wait list after a 15%+ week
        vol = m["vol3m"]
        call_pct = 0.4 * vol * math.sqrt(0.25) * 100
        out.append({"t": t, "name": m.get("name", ""), "price": round(m["price"], 2),
                    "move3m": round(vol * math.sqrt(0.25) * 100, 1), "callPct": round(call_pct, 1),
                    "contractCost": round(call_pct / 100 * m["price"] * 100),
                    "note": ("Tier A. " if t in OPTION_TIER_A else "Grade A. ") +
                            "Check the earnings date first; one contract is 100 shares."})
    return {"asOf": today.isoformat(), "list": out}

def grade_a(m):
    """-> (is_grade_a, reasons it fails). Unknown data counts as a fail."""
    why = []
    if (m.get("screens") or {}).get("Quality") != "pass":
        why.append("Quality screen not passed")
    pe = m.get("pe")
    if pe is None or not 0 < pe <= 25:
        why.append(f"P/E {pe:.1f}" if isinstance(pe, (int, float)) else "P/E unknown")
    if m.get("de") is None or m["de"] >= 0.5:
        why.append("debt/equity unknown" if m.get("de") is None else f"debt/equity {m['de']:.2f}")
    if not m.get("momentum"):
        why.append("price trend not up")
    if m.get("chg52") is None or m["chg52"] > 60:
        why.append("52-week change unknown" if m.get("chg52") is None else f"up {m['chg52']:.0f}% in a year (chasing)")
    if m.get("oneOff") or m.get("flags"):
        why.append("red flags: " + ", ".join(m.get("flags") or ["one-off"]))
    if (m.get("revenue") or 0) < MIN_REVENUE:
        why.append("revenue under $500M")
    return not why, why


def grade_a_report(rows, prev, today):
    """Build the Grade A list and what changed since the previous list (prev = last grade_a.json or None)."""
    keep = ("name", "price", "pe", "roe", "margin", "de", "chg52", "ma200", "mktCap", "cik", "period")
    now = {}
    for t, m in rows.items():
        ok, _ = grade_a(m)
        if ok:
            now[t] = {k: m.get(k) for k in keep}
    before = set((prev or {}).get("list", {}))
    removed = {}
    for t in sorted(before - set(now)):
        _, why = grade_a(rows[t]) if t in rows else (False, ["no longer in the data"])
        removed[t] = why
    return {"asOf": today.isoformat(), "version": GRADE_A_VERSION, "rules": GRADE_A_RULES,
            "list": dict(sorted(now.items(), key=lambda kv: -(kv[1].get("mktCap") or 0))),
            "added": sorted(set(now) - before) if prev else [], "removed": removed if prev else {},
            "previousAsOf": (prev or {}).get("asOf")}


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
        self.loc = {}  # filer location code from frames, e.g. "US-CA"; foreign codes have no "US-" prefix

    def get(self, tax, concept, unit, period):
        key = (tax, concept, unit, period)
        if key in self.cache:
            return self.cache[key]
        url = FRAMES.format(tax=tax, concept=concept, unit=unit, period=period)
        self.calls += 1
        try:
            data = json.loads(self.fetch(url))
            out = {int(d["cik"]): float(d["val"]) for d in data.get("data", [])}
            for d in data.get("data", []):
                if d.get("loc"):
                    self.loc[int(d["cik"])] = d["loc"]
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
    """Metrics as of the latest quarter; companies that haven't filed it yet use the quarter before."""
    y, q = quarter_periods(today)
    cur, _ = fundamentals_at(fr, y, q)
    prev, _ = fundamentals_at(fr, *back(y, q, 1))
    out = dict(prev)
    for cik, m in cur.items():
        out[cik] = m
    return out, (y, q)


def fundamentals_at(fr, y, q):
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
    total_lt = instant(fr, sorted(TOTAL_FORMS), "USD", y, q)
    for cik, d in debt.items():
        if cik in total_lt and d.get("lt") == total_lt[cik]:
            d["ltc"] = 0.0  # total already includes the current portion
    ca, cl = instant(fr, CA, "USD", y, q), instant(fr, CL, "USD", y, q)
    cash = instant(fr, CASH, "USD", y, q)
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
            "de": de_ratio(total_debt, e),
            "debtTagged": d is not None,
            "cr": None if (cik not in ca or not cl.get(cik)) else ca[cik] / cl[cik],
            "epsYr": growth(eps.get(cik), eps_prev.get(cik)),
            "epsQ": growth(eps_q.get(cik), eps_q_prev.get(cik)),
            "salesQ": growth(rev_q.get(cik), rev_q_prev.get(cik)),
            "eps5y": cagr(eps_a.get(cik), eps_a5.get(cik), 5),
            "cash": cash.get(cik),
            "netCash": None if (cik not in cash or total_debt is None) else cash[cik] - total_debt,
            "loc": fr.loc.get(cik, ""),
            "us": fr.loc.get(cik, "").startswith("US"),
        }
        out[cik] = m
    return out, (y, q)


def de_ratio(total_debt, equity):
    """Debt/equity. If no standard debt label is found the answer is unknown, never zero:
    companies label debt in many ways, and guessing zero would let indebted companies pass."""
    if equity is None or equity <= 0 or total_debt is None:
        return None
    return total_debt / equity


def red_flags(m):
    """Signs a number is distorted by a one-off (asset sale, spin-off, tiny prior-year base, share-count mismatch)."""
    f = []
    if any((m.get(k) or 0) > 300 for k in ("epsYr", "epsQ")):
        f.append("earnings jumped over 300%: check for one-off gains or a tiny base")
    if (m.get("margin") or 0) > 80:
        f.append("profit margin over 80%: check for one-off gains")
    if m.get("pe") is not None and m["pe"] < 3:
        f.append("P/E under 3: usually a one-off gain")
    if m.get("pb") is not None and m["pb"] < 0.3:
        f.append("P/B under 0.3: possible share-count or spin-off mismatch")
    return f


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
    """Latest price, 200-day average, 52-week change, and the daily close history {date: close}."""
    now = int(time.time())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={now - 760 * 86400}"
           f"&period2={now}&interval=1d")
    j = json.loads(fetch(url))["chart"]["result"][0]
    raw = j["indicators"]["quote"][0]["close"]
    ts = j.get("timestamp") or []
    hist = {}
    for t, c in zip(ts, raw):
        if c is not None:
            hist[dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat()] = c
    closes = [c for c in raw if c is not None]
    if len(closes) < 210:
        return None
    rets = [math.log(b / a) for a, b in zip(closes[-64:-1], closes[-63:]) if a > 0 and b > 0]
    vol = st.pstdev(rets) * math.sqrt(252) if len(rets) > 20 else None
    jump = max((closes[i] / closes[i - 5] - 1 for i in range(len(closes) - 63, len(closes)) if i >= 5), default=0)
    return {"price": closes[-1], "ma200": st.mean(closes[-200:]),
            "chg52": 100 * (closes[-1] / closes[-252] - 1) if len(closes) >= 252 else None, "hist": hist,
            "vol3m": vol, "jump5d63": jump}


def run(fetch_sec, fetch_px, today, always=(), sleep=time.sleep, spec=None, ledger=None):
    fr = Frames(lambda u: (sleep(0.15), fetch_sec(u))[1])
    funda, (y, q) = fundamentals(fr, today)
    tick = json.loads(fetch_sec(TICKERS))
    cik2t, t2cik, names = {}, {}, {}
    for v in tick.values():
        cik2t.setdefault(int(v["cik_str"]), v["ticker"])
        t2cik[v["ticker"]] = int(v["cik_str"])
        names.setdefault(int(v["cik_str"]), v.get("title", ""))
    rows, need_px, big_rows = {}, set(), {}
    for cik, m in funda.items():
        t = cik2t.get(cik)
        if not t:
            continue
        big = (m["revenue"] or 0) >= MIN_REVENUE
        tracked = t in always
        if (m["revenue"] or 0) >= STRATEGY_MIN_REVENUE or tracked:
            big_rows[t] = dict(m, cik=cik, name=names.get(cik, ""))
        if not (big or tracked):
            continue
        alive = [s for s, rules in SCREENS.items() if could_pass(m, rules)]
        if alive or tracked:
            rows[t] = big_rows[t]
            need_px.add(t)
    ledger = ledger or []
    if spec:
        for t in se.prefilter_universe(spec, big_rows):
            rows[t] = big_rows[t]
            need_px.add(t)
        for t in se.tickers_to_price(ledger, today):
            need_px.add(t)
        for strat in spec["strategies"]:
            if strat.get("benchmark", "SPY") != "SPY":
                need_px.add(strat["benchmark"])
    for t in always:
        if t not in rows:
            rows[t] = {"cik": t2cik.get(t), "note": "not in SEC US-GAAP frames (foreign IFRS filer or no recent 10-Q)"}
    px_fail, histories = [], {}
    spy_hist = {}
    try:
        spy = yahoo_daily("SPY", fetch_px)
        spy_hist = spy["hist"] if spy else {}
    except Exception as e:
        px_fail.append(f"SPY: {type(e).__name__}")
    for t in sorted(need_px):
        try:
            p = yahoo_daily(t.replace(".", "-"), fetch_px)
        except Exception as e:
            p, _ = None, px_fail.append(f"{t}: {type(e).__name__}")
        sleep(0.3)
        if not p:
            continue
        histories[t] = p.pop("hist")
        if t not in rows:  # past strategy pick no longer in the universe: price only, for scoring
            continue
        m = rows[t]
        m.update(p)
        m["beta"] = se.beta(histories[t], spy_hist) if spy_hist else None
        if m.get("epsTTM") and m["epsTTM"] > 0:
            m["pe"] = p["price"] / m["epsTTM"]
        if m.get("shares") and m.get("equity") and m["equity"] > 0:
            m["pb"] = p["price"] * m["shares"] / m["equity"]
        if m.get("shares"):
            m["mktCap"] = p["price"] * m["shares"]
            if m.get("revenue") and m["revenue"] > 0:
                m["ps"] = m["mktCap"] / m["revenue"]
        m["momentum"] = None if p.get("chg52") is None else (p["price"] > p["ma200"] and p["chg52"] > 0)
    results = {s: [] for s in SCREENS}
    for t, m in rows.items():
        m["flags"] = red_flags(m)
        m["oneOff"] = bool(m["flags"])
        m["screens"] = {}
        for s, rules in SCREENS.items():
            c = check(m, rules)
            m["screens"][s] = "pass" if all(ok is True for _, ok in c) else (
                "fail" if any(ok is False for _, ok in c) else "unknown")
            if m["screens"][s] == "pass":
                results[s].append(t)
    for s in results:
        results[s].sort(key=lambda t: (not rows[t].get("momentum"), -(rows[t].get("mktCap") or 0)))
    strat_out, new_entries, scored = {}, [], 0
    if spec:
        # industry codes for the top of each capped strategy's ranking (SEC submissions JSON, one call each)
        for strat in spec["strategies"]:
            if not strat.get("maxPerSector"):
                continue
            order, _ = se.ranked(strat, rows)
            for t in order[:strat["top"] * 6]:
                m = rows[t]
                if m.get("sic") or not m.get("cik"):
                    continue
                try:
                    sub = json.loads(fetch_sec(f"https://data.sec.gov/submissions/CIK{int(m['cik']):010d}.json"))
                    m["sic"], m["industry"] = sub.get("sic"), sub.get("sicDescription")
                except Exception:
                    pass
                sleep(0.15)
        ledger, new_entries, scored = se.log_and_score(spec, rows, today, histories, spy_hist, ledger)
        for s in spec["strategies"]:
            picks = se.pick(s, rows)
            past = [e for e in ledger if e["strategy"] == s["id"]]
            done = {h: [e["out"][h] for e in past if h in e["out"]] for h in ("13", "26", "52")}
            strat_out[s["id"]] = {
                "name": s["name"], "description": s["description"], "startDate": s["startDate"],
                "live": today >= dt.date.fromisoformat(s["startDate"]),
                "picks": [dict(p, name=rows[p["t"]].get("name", ""), mktCap=rows[p["t"]].get("mktCap"),
                               pe=rows[p["t"]].get("pe"), de=rows[p["t"]].get("de"), flags=red_flags(rows[p["t"]]))
                          for p in picks],
                "weeksLogged": len(past),
                "record": {h: {"n": len(v), "meanExcess": round(sum(o["meanExcess"] for o in v) / len(v), 4) if v else None,
                               "weeksBeat": sum(1 for o in v if o["meanExcess"] > 0)} for h, v in done.items()},
            }

    def rnd(v):
        return round(v, 2) if isinstance(v, float) else v
    return {
        "asOf": today.isoformat(), "fundamentalsThrough": f"CY{y}Q{q}",
        "source": "SEC EDGAR XBRL frames (us-gaap/dei) + Yahoo Finance prices",
        "rules": {s: [f"{k} {op} {lim}" for k, op, lim in r] for s, r in SCREENS.items()},
        "minRevenue": MIN_REVENUE, "companiesWithData": len(funda), "considered": len(rows),
        "secCalls": fr.calls, "missingFrames": fr.missing[:40], "priceFailures": px_fail[:40],
        "passes": results,
        "strategies": strat_out, "_ledger": ledger, "_newEntries": len(new_entries), "_scored": scored,
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
    spec = json.load(open(se.SPEC_PATH)) if os.path.exists(se.SPEC_PATH) else None
    res = run(sec, px, dt.date.today(), always, spec=spec, ledger=se.read_ledger())
    if spec:
        se.write_ledger(res.pop("_ledger"))
    else:
        res.pop("_ledger", None)
    print("strategies:", json.dumps({k: [p["t"] for p in v["picks"]] for k, v in res["strategies"].items()}),
          "new ledger entries:", res.pop("_newEntries"), "outcomes filled:", res.pop("_scored"))
    os.makedirs(out_dir, exist_ok=True)
    ga_path = os.path.join(out_dir, "grade_a.json")
    prev = json.load(open(ga_path)) if os.path.exists(ga_path) else None
    ga = grade_a_report(res["rows"], prev, dt.date.fromisoformat(res["asOf"]))
    with open(ga_path, "w") as f:
        json.dump(ga, f, indent=0, sort_keys=True)
    with open(os.path.join(out_dir, "option_setups.json"), "w") as f:
        json.dump(option_setups(res["rows"], list(ga["list"]), dt.date.fromisoformat(res["asOf"])), f, indent=0)
    print(f"Grade A: {len(ga['list'])} -> {', '.join(ga['list'])}; added {ga['added']}; removed {list(ga['removed'])}")
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
