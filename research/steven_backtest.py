#!/usr/bin/env python3
"""Point-in-time backtest of 'Steven's' small-cap value screen (shared on social media, Nov 2026).

Rules as stated: viable profitable business (net margin well above 2%), price/sales under 2,
market cap under $3B (ideally $2B), long-term debt/equity under 0.5 (ideally net cash), US only.
Claim tested: adding quality rules to 'cheap small caps' picks better stocks than cheapness alone.

Method: every June 15 and December 15, 2012-2025, rebuild the screen from SEC XBRL frames using the
latest quarter whose filings were due by then (Q1 for June, Q3 for December), price it with Yahoo
closes on that date (split-unadjusted for market cap), and measure the next 52 weeks' total return
(dividend-adjusted) against IWM (Russell 2000) and SPY. Equal weight within each list.

Known biases, reported with the results:
  * Survivorship: tickers come from today's SEC list and Yahoo, so companies that were delisted or
    went bankrupt are missing. This flatters every list's absolute return, the 'cheap only' control
    most (weak companies fail more), so the quality-vs-cheap comparison is if anything conservative.
  * Frames return the latest filed value for a period, so later restatements can leak in (rare).
  * The rules were written in 2026 by someone who lived through this period: non-blind.

Runs on GitHub Actions (needs SEC_CONTACT). Writes research/steven_backtest.json.
"""
import datetime as dt
import json
import os
import statistics as st
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import screens  # noqa: E402

CASH = ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndDueFromBanks"]
DEBT_NAMES = ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligations", "DebtLongtermAndShorttermCombinedAmount",
              "LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligationsNoncurrent", "SeniorLongTermNotes",
              "LongTermNotesPayable"]
DEBT_CUR = ["LongTermDebtCurrent", "DebtCurrent"]
DEBT_ST = ["ShortTermBorrowings", "NotesPayableCurrent"]
UA_PX = "Mozilla/5.0 (gss-wire research; personal use)"


class LocFrames(screens.Frames):
    """Frames that also remember each filer's location code (loc, e.g. 'US-NY', 'K3' for Hong Kong)."""

    def __init__(self, fetch):
        super().__init__(fetch)
        self.loc = {}

    def get(self, tax, concept, unit, period):
        key = (tax, concept, unit, period)
        if key in self.cache:
            return self.cache[key]
        url = screens.FRAMES.format(tax=tax, concept=concept, unit=unit, period=period)
        self.calls += 1
        try:
            data = json.loads(self.fetch(url))
            rows = data.get("data", [])
            out = {int(d["cik"]): float(d["val"]) for d in rows}
            for d in rows:
                if d.get("loc"):
                    self.loc[int(d["cik"])] = d["loc"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            out = {}
        self.cache[key] = out
        return out


def cohorts(first=2012, last=2025):
    out = []
    for y in range(first, last + 1):
        out.append((dt.date(y, 6, 15), (y, 1)))
        out.append((dt.date(y, 12, 15), (y, 3)))
    return out


def fundamentals_at(fr, y, q):
    ni = screens.ttm(fr, screens.NI, "USD", y, q)
    rev = screens.ttm(fr, screens.REV, "USD", y, q)
    eq = screens.instant(fr, screens.EQUITY, "USD", y, q)
    cash = screens.instant(fr, CASH, "USD", y, q)
    shares = {}
    for tax, c in screens.SHARES:
        for cik, v in screens.instant(fr, [c], "shares", y, q, tax).items():
            shares.setdefault(cik, v)
    lt = screens.instant(fr, DEBT_NAMES, "USD", y, q)
    totals = screens.instant(fr, ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligations", "DebtLongtermAndShorttermCombinedAmount"], "USD", y, q)
    cur = screens.instant(fr, DEBT_CUR, "USD", y, q)
    stb = screens.instant(fr, DEBT_ST, "USD", y, q)
    out = {}
    for cik in set(rev) & set(ni):
        has_debt_tag = cik in lt or cik in cur or cik in stb
        debt = None
        if has_debt_tag:
            debt = lt.get(cik, 0.0) + (0.0 if cik in totals and lt.get(cik) == totals[cik] else cur.get(cik, 0.0)) + stb.get(cik, 0.0)
        out[cik] = {"rev": rev[cik], "ni": ni[cik], "eq": eq.get(cik), "cash": cash.get(cik), "shares": shares.get(cik),
                    "debt": debt, "loc": fr.loc.get(cik, "")}
    return out


def yahoo_weekly(sym, fetch):
    """{friday: (close_split_adjusted, adjclose)} and splits [(date, ratio)] since 2011."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1=1293840000&period2={int(time.time())}"
           f"&interval=1wk&events=div%2Csplit")
    j = json.loads(fetch(url))["chart"]["result"][0]
    ts = j.get("timestamp") or []
    q = j["indicators"]["quote"][0]["close"]
    adj = (j["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q
    px = {}
    for t, c, a in zip(ts, q, adj):
        if c is None or a is None:
            continue
        d = dt.datetime.fromtimestamp(t, dt.timezone.utc).date()
        px[d] = (c, a)
    splits = []
    for s in ((j.get("events") or {}).get("splits") or {}).values():
        try:
            splits.append((dt.datetime.fromtimestamp(s["date"], dt.timezone.utc).date(), s["numerator"] / s["denominator"]))
        except (KeyError, ZeroDivisionError):
            pass
    return px, splits


_SORTED = {}


def at(px, day, tol=10):
    """Latest date in px on or before day (within tol days)."""
    import bisect
    keys = _SORTED.get(id(px))
    if keys is None or len(keys) != len(px):
        keys = _SORTED[id(px)] = sorted(px)
    i = bisect.bisect_right(keys, day) - 1
    if i < 0 or (day - keys[i]).days > tol:
        return None
    return keys[i]


def raw_close(px, splits, d):
    """Undo later split adjustments so price x filed share count = market cap on that date."""
    c = px[d][0]
    for sd, ratio in splits:
        if sd > d:
            c *= ratio
    return c


def rules(m, margin_min, cap_max=3e9, need_net_cash=False, quality=True):
    if not m["loc"].startswith("US"):
        return False
    if m.get("mktCap") is None or not (3e8 <= m["mktCap"] <= cap_max):
        return False
    if m["rev"] <= 0 or m["mktCap"] / m["rev"] >= 2:
        return False
    if not quality:
        return True
    if m["ni"] <= 0 or 100 * m["ni"] / m["rev"] < margin_min:
        return False
    if not m["eq"] or m["eq"] <= 0 or m["debt"] is None or m["debt"] / m["eq"] >= 0.5:
        return False
    if need_net_cash and (m["cash"] is None or m["cash"] < m["debt"]):
        return False
    return True


VARIANTS = {
    "steven_8": dict(margin_min=8),
    "steven_5": dict(margin_min=5),
    "steven_10": dict(margin_min=10),
    "steven_8_netcash": dict(margin_min=8, need_net_cash=True),
    "steven_8_under2B": dict(margin_min=8, cap_max=2e9),
    "cheap_only": dict(margin_min=0, quality=False),
}


def main():
    contact = os.environ.get("SEC_CONTACT", "").strip()
    if not contact:
        print("SEC_CONTACT missing", file=sys.stderr)
        return 1

    def sec(url):
        for k in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": contact, "Accept-Encoding": "identity"})
                with urllib.request.urlopen(req, timeout=90) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise
                time.sleep(3 * (k + 1))
            except Exception:
                time.sleep(3 * (k + 1))
        raise RuntimeError("SEC fetch failed: " + url)

    def yfetch(url):
        for k in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA_PX})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise
                time.sleep(5 * (k + 1))
        raise RuntimeError("price fetch failed")

    fr = LocFrames(lambda u: (time.sleep(0.12), sec(u))[1])
    tick = json.loads(sec(screens.TICKERS))
    cik2t = {}
    for v in tick.values():
        cik2t.setdefault(int(v["cik_str"]), v["ticker"])

    snaps = {}
    for day, (y, q) in cohorts():
        snaps[day] = fundamentals_at(fr, y, q)
        print(day, "companies", len(snaps[day]), "SEC calls so far", fr.calls, flush=True)

    # price only plausible candidates: US, revenue between $150M and $15B, current ticker
    need = set()
    for day, f in snaps.items():
        for cik, m in f.items():
            if m["loc"].startswith("US") and 1.5e8 <= m["rev"] <= 1.5e10 and cik in cik2t and m.get("shares"):
                need.add(cik2t[cik])
    print("tickers to price:", len(need), flush=True)
    prices, failed = {}, 0
    for sym in sorted(need) + ["IWM", "SPY"]:
        try:
            prices[sym] = yahoo_weekly(sym.replace(".", "-"), yfetch)
        except Exception:
            failed += 1
        time.sleep(0.25)
    print("priced", len(prices), "failed", failed, flush=True)

    def fwd(sym, day):
        px, _ = prices[sym]
        d0 = at(px, day)
        d1 = at(px, day + dt.timedelta(weeks=52))
        if not d0 or not d1 or d1 <= d0:
            return None
        return px[d1][1] / px[d0][1] - 1

    results = {v: [] for v in VARIANTS}
    detail = {}
    today = dt.date.today()
    for day, f in sorted(snaps.items()):
        if day + dt.timedelta(weeks=52) > today:
            continue
        iwm, spy = fwd("IWM", day), fwd("SPY", day)
        lists = {v: [] for v in VARIANTS}
        missing_ticker = {v: 0 for v in VARIANTS}
        for cik, m in f.items():
            t = cik2t.get(cik)
            mm = dict(m)
            if t and t in prices and m.get("shares"):
                px, splits = prices[t]
                d0 = at(px, day)
                if d0:
                    mm["mktCap"] = raw_close(px, splits, d0) * m["shares"]
            for v, kw in VARIANTS.items():
                if mm.get("mktCap") is None:
                    # no current ticker or price: could be a delisted company (survivorship)
                    if m["loc"].startswith("US") and m["rev"] > 0 and kw.get("quality", True) is False:
                        pass
                    continue
                if rules(mm, **kw):
                    r = fwd(t, day)
                    if r is not None:
                        lists[v].append((t, r))
        for v in VARIANTS:
            rs = [r for _, r in lists[v]]
            if not rs:
                continue
            ew = st.mean(rs)
            results[v].append({"date": day.isoformat(), "n": len(rs), "ret": round(ew, 4),
                               "vsIWM": None if iwm is None else round((1 + ew) / (1 + iwm) - 1, 4),
                               "vsSPY": None if spy is None else round((1 + ew) / (1 + spy) - 1, 4),
                               "hitIWM": None if iwm is None else round(sum(1 for r in rs if r > iwm) / len(rs), 3),
                               "medianRet": round(st.median(rs), 4)})
        detail[day.isoformat()] = {"iwm": iwm, "spy": spy, "steven_8": sorted(t for t, _ in lists["steven_8"])}

    summary = {}
    for v, rows in results.items():
        if not rows:
            continue
        ex = [r["vsIWM"] for r in rows if r["vsIWM"] is not None]
        dec = [r for r in rows if r["date"].endswith("12-15")]
        growth = 1.0
        bench = 1.0
        for r in dec:
            growth *= 1 + r["ret"]
            i = detail[r["date"]]["iwm"]
            bench *= 1 + (i or 0)
        early = [r["vsIWM"] for r in rows if r["date"] < "2019-01-01" and r["vsIWM"] is not None]
        late = [r["vsIWM"] for r in rows if r["date"] >= "2019-01-01" and r["vsIWM"] is not None]
        summary[v] = {"cohorts": len(rows), "avgStocks": round(st.mean(r["n"] for r in rows), 1),
                      "meanExcessVsIWM": round(st.mean(ex), 4), "medianExcessVsIWM": round(st.median(ex), 4),
                      "cohortsBeatIWM": sum(1 for x in ex if x > 0),
                      "earlyMean": round(st.mean(early), 4) if early else None, "lateMean": round(st.mean(late), 4) if late else None,
                      "decCohortsGrowthOf1": round(growth, 2), "iwmGrowthOf1": round(bench, 2),
                      "avgStockHitRateVsIWM": round(st.mean(r["hitIWM"] for r in rows if r["hitIWM"] is not None), 3)}
    out = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "secCalls": fr.calls,
           "tickersPriced": len(prices), "priceFailures": failed, "summary": summary, "cohorts": results, "detail": detail}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steven_backtest.json")
    json.dump(out, open(path, "w"), indent=1, default=str)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
