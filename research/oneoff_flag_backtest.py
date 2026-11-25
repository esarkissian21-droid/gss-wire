#!/usr/bin/env python3
"""Point-in-time test of a proposed Grade A red flag: profit propped up by non-operating gains.

Proposed rule (Nov 24 2026, after Amazon's Anthropic gains slipped past the one-off check):
flag a company when trailing-12-month net income is more than K x trailing-12-month operating income
(or operating income is zero/negative while net income is positive). Proposed K = 1.25.
Companies that don't report operating income (most banks and insurers) are never flagged.

Questions:
  1. Broad market: among profitable US companies with $2B+ revenue, do flagged companies do worse
     than unflagged ones over the next 52 weeks (vs SPY)?
  2. Grade A: does removing flagged names from an approximate Grade A list change its results?
  3. Fragility: do the answers hold for K = 1.1, 1.25, 1.5, 2.0 and in both halves of the period?

Method: every June 15 and December 15, 2012-2025, rebuild fundamentals from SEC XBRL frames for the
latest quarter whose filings were due (Q1 for June, Q3 for December), price with Yahoo weekly closes,
52-week dividend-adjusted forward returns, equal weight.

Grade A approximation (the live rules, v1-2026-11-22): US, Quality screen pass (ROE > 15%, net margin >
10%, 5-yr EPS growth > 10%, debt/equity < 0.5), P/E 0-25, price above its 40-week average and up 0-60%
over 52 weeks, revenue >= $500M, no existing red flags.

Biases: survivorship (today's tickers only), restatements can leak in through frames, and the rule was
written in 2026 after seeing one case: non-blind. Writes research/oneoff_flag_backtest.json.
"""
import datetime as dt
import json
import os
import statistics as st
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import screens  # noqa: E402
from steven_backtest import LocFrames, at, cohorts, raw_close, yahoo_weekly, UA_PX  # noqa: E402

OPI = ["OperatingIncomeLoss"]
KS = [1.1, 1.25, 1.5, 2.0]
PROPOSED_K = 1.25


def flagged(m, k):
    o, n = m.get("opi"), m.get("netIncome")
    if o is None or n is None or n <= 0:
        return False
    return o <= 0 or n > k * o


def grade_a_at(m, price, px, splits, d0):
    """Approximate live Grade A on a cohort date. m = screens.fundamentals_at row."""
    if not m.get("us") or (m.get("revenue") or 0) < screens.MIN_REVENUE:
        return False
    if not screens.passes(m, screens.SCREENS["Quality"]):
        return False
    eps = m.get("epsTTM")
    if not eps or eps <= 0:
        return False
    pe = price / eps
    if not 0 < pe <= 25:
        return False
    mm = dict(m, pe=pe)
    if m.get("shares") and m.get("equity") and m["equity"] > 0:
        mm["pb"] = price * m["shares"] / m["equity"]
    if screens.red_flags(mm):
        return False
    keys = sorted(k for k in px if k <= d0)[-41:]
    if len(keys) < 41:
        return False
    closes = [px[k][0] for k in keys]
    ma40 = st.mean(closes[-40:])
    chg52 = 100 * (closes[-1] / closes[0] - 1)
    return closes[-1] > ma40 and 0 < chg52 <= 60


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
        rows, _ = screens.fundamentals_at(fr, y, q)
        opi = screens.ttm(fr, OPI, "USD", y, q)
        for cik, m in rows.items():
            m["opi"] = opi.get(cik)
            m["us"] = fr.loc.get(cik, "").startswith("US")
        snaps[day] = rows
        print(day, "companies", len(rows), "with op income", len(opi), "SEC calls", fr.calls, flush=True)

    need = set()
    for day, f in snaps.items():
        for cik, m in f.items():
            if cik not in cik2t or not m.get("us") or not m.get("shares"):
                continue
            broad = (m.get("revenue") or 0) >= 2e9 and (m.get("netIncome") or 0) > 0 and m.get("opi") is not None
            ga_pre = (m.get("revenue") or 0) >= screens.MIN_REVENUE and screens.passes(m, screens.SCREENS["Quality"])
            if broad or ga_pre:
                need.add(cik2t[cik])
    print("tickers to price:", len(need), flush=True)
    prices, failed = {}, 0
    for sym in sorted(need) + ["SPY"]:
        try:
            prices[sym] = yahoo_weekly(sym.replace(".", "-"), yfetch)
        except Exception:
            failed += 1
        time.sleep(0.2)
    print("priced", len(prices), "failed", failed, flush=True)

    def fwd(sym, day):
        px, _ = prices[sym]
        d0, d1 = at(px, day), at(px, day + dt.timedelta(weeks=52))
        if not d0 or not d1 or d1 <= d0:
            return None
        return px[d1][1] / px[d0][1] - 1

    today = dt.date.today()
    per = []
    examples = []
    for day, f in sorted(snaps.items()):
        if day + dt.timedelta(weeks=52) > today or "SPY" not in prices:
            continue
        spy = fwd("SPY", day)
        if spy is None:
            continue
        broad = []  # (ratio-flags per K, excess)
        ga = []     # (flags per K, excess, ticker)
        for cik, m in f.items():
            t = cik2t.get(cik)
            if not t or t not in prices or not m.get("us"):
                continue
            px, splits = prices[t]
            d0 = at(px, day)
            if not d0:
                continue
            r = fwd(t, day)
            if r is None:
                continue
            ex = (1 + r) / (1 + spy) - 1
            flags = {k: flagged(m, k) for k in KS}
            if (m.get("revenue") or 0) >= 2e9 and (m.get("netIncome") or 0) > 0 and m.get("opi") is not None:
                broad.append((flags, ex))
            if grade_a_at(m, raw_close(px, splits, d0), px, splits, d0):
                ga.append((flags, ex, t))
                if flags[PROPOSED_K]:
                    examples.append({"date": day.isoformat(), "t": t, "ni": m["netIncome"], "opi": m["opi"],
                                     "excess": round(ex, 4)})
        row = {"date": day.isoformat(), "spy": round(spy, 4), "broadN": len(broad), "gaN": len(ga)}
        for k in KS:
            fl = [e for fg, e in broad if fg[k]]
            un = [e for fg, e in broad if not fg[k]]
            gall = [e for _, e, _ in ga]
            gkeep = [e for fg, e, _ in ga if not fg[k]]
            gout = [e for fg, e, _ in ga if fg[k]]
            row[str(k)] = {
                "broadFlaggedN": len(fl),
                "broadFlagged": round(st.mean(fl), 4) if fl else None,
                "broadUnflagged": round(st.mean(un), 4) if un else None,
                "gaAll": round(st.mean(gall), 4) if gall else None,
                "gaKept": round(st.mean(gkeep), 4) if gkeep else None,
                "gaRemovedN": len(gout),
                "gaRemoved": round(st.mean(gout), 4) if gout else None,
            }
        per.append(row)
        print(day, "broad", len(broad), "gradeA", len(ga), "flagged in GA", row[str(PROPOSED_K)]["gaRemovedN"], flush=True)

    def summarize(rows):
        out = {}
        for k in KS:
            s = str(k)
            gaps = [r[s]["broadFlagged"] - r[s]["broadUnflagged"] for r in rows
                    if r[s]["broadFlagged"] is not None and r[s]["broadUnflagged"] is not None and r[s]["broadFlaggedN"] >= 5]
            ga_diff = [r[s]["gaKept"] - r[s]["gaAll"] for r in rows if r[s]["gaKept"] is not None and r[s]["gaAll"] is not None]
            removed = [r[s]["gaRemoved"] for r in rows if r[s]["gaRemoved"] is not None]
            t = None
            if len(gaps) > 2 and st.pstdev(gaps) > 0:
                t = round(st.mean(gaps) / (st.stdev(gaps) / len(gaps) ** 0.5), 2)
            out[s] = {
                "broadCohorts": len(gaps),
                "broadAvgFlaggedPerCohort": round(st.mean(r[s]["broadFlaggedN"] for r in rows), 1) if rows else None,
                "broadFlaggedMinusUnflagged": round(st.mean(gaps), 4) if gaps else None,
                "broadCohortsFlaggedWorse": sum(1 for g in gaps if g < 0),
                "broadT": t,
                "gaCohortsWithRemovals": len(removed),
                "gaNamesRemoved": sum(r[s]["gaRemovedN"] for r in rows),
                "gaRemovedAvgExcess": round(st.mean(removed), 4) if removed else None,
                "gaKeptMinusAll": round(st.mean(ga_diff), 4) if ga_diff else None,
            }
        return out

    early = [r for r in per if r["date"] < "2019-01-01"]
    late = [r for r in per if r["date"] >= "2019-01-01"]
    out = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "label": "non-blind (rule written in 2026 after seeing one case)",
           "proposedK": PROPOSED_K, "secCalls": fr.calls, "tickersPriced": len(prices), "priceFailures": failed,
           "avgGradeAPerCohort": round(st.mean(r["gaN"] for r in per), 1) if per else None,
           "summary": {"all": summarize(per), "2012-2018": summarize(early), "2019-2025": summarize(late)},
           "gradeAExamplesFlagged": examples, "cohorts": per}
    json.dump(out, open(os.path.join(HERE, "oneoff_flag_backtest.json"), "w"), indent=1)
    print(json.dumps(out["summary"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
