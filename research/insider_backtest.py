#!/usr/bin/env python3
"""Backtest of insider-buying signals from the SEC's Insider Transactions Data Sets (Forms 3/4/5, 2006-).

Claim tested (shared on social media, Nov 2026): pull new Form 4s daily, keep only code P
(open-market purchases with their own cash), and flag when 3+ officers/directors of the same company
buy in the same week; such buys beat the market.

Signals (dated by FILING date, i.e. when the public could see them):
  single      one officer/director open-market buy of $10k+ (baseline; a random 30% sample is priced)
  c2_7d       2+ different officers/directors buying within 7 days
  c3_7d       3+ within 7 days (the claim)
  c3_30d      3+ within 30 days
  ceo_100k    CEO/CFO/president/chair buy of $100k+
One event per company per signal per 90 days. Entry: first weekly close at least one day after the
filing date; returns over 13/26/52 weeks vs IWM (small caps, where most of these happen) and SPY.
Stocks under $2 at entry are skipped.

Biases (reported): tickers are those on the filing, priced by Yahoo today, so delisted/renamed
companies drop out (survivorship; coverage % is reported). Non-blind: the rule came after the history.
Runs on GitHub Actions (needs SEC_CONTACT). Writes research/insider_backtest.json.
"""
import bisect
import csv
import datetime as dt
import io
import json
import os
import random
import re
import statistics as st
import sys
import time
import urllib.error
import urllib.request
import zipfile

ZIP_URL = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{y}q{q}_form345.zip"
UA_PX = "Mozilla/5.0 (gss-wire research; personal use)"
BOSS = re.compile(r"\b(ceo|chief executive|cfo|chief financial|president|chair)", re.I)
csv.field_size_limit(10 ** 8)


def parse_date(s):
    s = (s or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%b-%y"):
        try:
            return dt.datetime.strptime(s.title() if "-" in s and s[3:6].isalpha() else s, fmt).date()
        except ValueError:
            pass
    return None


def read_tsv(zf, name):
    member = next((n for n in zf.namelist() if n.upper().endswith(name)), None)
    if not member:
        return []
    with zf.open(member) as f:
        text = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
        return list(csv.DictReader(text, delimiter="\t"))


def quarter_events(raw):
    """Open-market purchases by officers/directors in one quarterly file -> list of buy records."""
    zf = zipfile.ZipFile(io.BytesIO(raw))
    subs = {r["ACCESSION_NUMBER"]: r for r in read_tsv(zf, "SUBMISSION.TSV") if (r.get("DOCUMENT_TYPE") or "").strip() == "4"}
    owners = {}
    for r in read_tsv(zf, "REPORTINGOWNER.TSV"):
        a = r["ACCESSION_NUMBER"]
        if a in subs:
            owners.setdefault(a, []).append(r)
    buys = {}
    for r in read_tsv(zf, "NONDERIV_TRANS.TSV"):
        a = r["ACCESSION_NUMBER"]
        if a not in subs or (r.get("TRANS_CODE") or "").strip() != "P" or (r.get("TRANS_ACQUIRED_DISP_CD") or "").strip() != "A":
            continue
        try:
            v = float(r.get("TRANS_SHARES") or 0) * float(r.get("TRANS_PRICEPERSHARE") or 0)
        except ValueError:
            continue
        buys[a] = buys.get(a, 0.0) + v
    out = []
    for a, value in buys.items():
        s = subs[a]
        own = owners.get(a, [])
        rel = " ".join((o.get("RPTOWNER_RELATIONSHIP") or "") for o in own)
        titles = " ".join((o.get("RPTOWNER_TITLE") or "") for o in own)
        if not re.search(r"officer|director", rel, re.I):
            continue  # 10%-holder funds etc. are not "bosses"
        fd = parse_date(s.get("FILING_DATE"))
        sym = (s.get("ISSUERTRADINGSYMBOL") or "").strip().upper()
        if not fd or not sym or sym in ("NONE", "N/A", "NA"):
            continue
        out.append({"cik": (s.get("ISSUERCIK") or "").strip(), "sym": sym, "date": fd, "value": value,
                    "owner": ",".join(sorted((o.get("RPTOWNERCIK") or "").strip() for o in own)),
                    "boss": bool(BOSS.search(titles)), "planned": (s.get("AFF10B5ONE") or "").strip().lower() in ("1", "true")})
    return out


def find_events(buys, rng):
    by = {}
    for b in buys:
        if b["value"] >= 10_000:
            by.setdefault(b["cik"], []).append(b)
    ev = {k: [] for k in ("single", "c2_7d", "c3_7d", "c3_30d", "ceo_100k")}
    last = {}

    def add(kind, b):
        key = (kind, b["cik"])
        if key in last and (b["date"] - last[key]).days <= 90:
            return
        last[key] = b["date"]
        ev[kind].append({"sym": b["sym"], "date": b["date"], "cik": b["cik"]})

    for cik, lst in by.items():
        lst.sort(key=lambda x: x["date"])
        for i, b in enumerate(lst):
            if rng.random() < 0.3:
                add("single", b)
            if b["boss"] and b["value"] >= 100_000:
                add("ceo_100k", b)
            for days, need, kind in ((7, 2, "c2_7d"), (7, 3, "c3_7d"), (30, 3, "c3_30d")):
                owners = {x["owner"] for x in lst[:i + 1] if (b["date"] - x["date"]).days <= days}
                if len(owners) >= need:
                    add(kind, b)
    return ev


def yahoo_weekly(sym, fetch):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1=1136073600&period2={int(time.time())}"
           f"&interval=1wk&events=div%2Csplit")
    j = json.loads(fetch(url))["chart"]["result"][0]
    ts = j.get("timestamp") or []
    q = j["indicators"]["quote"][0]["close"]
    adj = (j["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q
    rows = []
    for t, c, a in zip(ts, q, adj):
        if c is None or a is None:
            continue
        d = dt.datetime.fromtimestamp(t, dt.timezone.utc).date()
        rows.append((d + dt.timedelta(days=(4 - d.weekday()) % 7), c, a))  # label each bar with its Friday
    rows.sort()
    return rows


def ret(series, d0, weeks):
    days = [r[0] for r in series]
    i = bisect.bisect_left(days, d0 + dt.timedelta(days=1))
    if i >= len(series):
        return None, None
    j = bisect.bisect_left(days, series[i][0] + dt.timedelta(weeks=weeks))
    if j >= len(series) or (series[j][0] - series[i][0]).days > weeks * 7 + 10:
        return None, series[i][1]
    return series[j][2] / series[i][2] - 1, series[i][1]


def main():
    contact = os.environ.get("SEC_CONTACT", "").strip()
    if not contact:
        print("SEC_CONTACT missing", file=sys.stderr)
        return 1

    def get(url, ua, tries=4):
        for k in range(tries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": ua})
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise
                time.sleep(4 * (k + 1))
            except Exception:
                time.sleep(4 * (k + 1))
        raise RuntimeError("fetch failed: " + url)

    rng = random.Random(7)
    buys, quarters = [], 0
    today = dt.date.today()
    for y in range(2006, today.year + 1):
        for q in range(1, 5):
            if dt.date(y, 3 * q, 1) > today:
                break
            try:
                raw = get(ZIP_URL.format(y=y, q=q), contact)
            except Exception as e:
                print(f"{y}q{q}: {type(e).__name__}", flush=True)
                continue
            b = quarter_events(raw)
            buys += b
            quarters += 1
            print(f"{y}q{q}: {len(b)} officer/director open-market buy filings", flush=True)
            time.sleep(0.5)
    ev = find_events(buys, rng)
    print({k: len(v) for k, v in ev.items()}, flush=True)

    syms = sorted({e["sym"] for v in ev.values() for e in v})
    print("tickers to price:", len(syms), flush=True)
    px, failed = {}, 0
    for s in syms + ["IWM", "SPY"]:
        try:
            px[s] = yahoo_weekly(s.replace(".", "-"), lambda u: get(u, UA_PX, 2))
        except Exception:
            failed += 1
        time.sleep(0.2)
    print("priced", len(px), "failed", failed, flush=True)

    H = (13, 26, 52)
    res = {}
    for kind, lst in ev.items():
        rows = {h: [] for h in H}
        priced = 0
        for e in lst:
            if e["sym"] not in px:
                continue
            r0, entry = ret(px[e["sym"]], e["date"], 13)
            if entry is None or entry < 2:
                continue
            priced += 1
            for h in H:
                r, _ = ret(px[e["sym"]], e["date"], h)
                ri, _ = ret(px["IWM"], e["date"], h)
                rs, _ = ret(px["SPY"], e["date"], h)
                if r is None or ri is None or rs is None:
                    continue
                rows[h].append({"d": e["date"].isoformat(), "x_iwm": (1 + r) / (1 + ri) - 1, "x_spy": (1 + r) / (1 + rs) - 1})
        summ = {"events": len(lst), "priced": priced, "coverage": round(priced / max(1, len(lst)), 3)}
        for h in H:
            xs = rows[h]
            if not xs:
                continue
            xi = [x["x_iwm"] for x in xs]
            # month-clustered t-stat: average events within each month, then t over months
            bym = {}
            for x in xs:
                bym.setdefault(x["d"][:7], []).append(x["x_iwm"])
            mm = [st.mean(v) for v in bym.values()]
            t = st.mean(mm) / (st.stdev(mm) / len(mm) ** 0.5) if len(mm) > 2 and st.stdev(mm) > 0 else None
            early = [x["x_iwm"] for x in xs if x["d"] < "2016-01-01"]
            late = [x["x_iwm"] for x in xs if x["d"] >= "2016-01-01"]
            summ[str(h)] = {"n": len(xs), "meanVsIWM": round(st.mean(xi), 4), "medianVsIWM": round(st.median(xi), 4),
                            "beatIWM": round(sum(1 for v in xi if v > 0) / len(xi), 3),
                            "meanVsSPY": round(st.mean(x["x_spy"] for x in xs), 4),
                            "tMonths": None if t is None else round(t, 2), "months": len(mm),
                            "early2006_15": round(st.mean(early), 4) if early else None,
                            "late2016_": round(st.mean(late), 4) if late else None}
        res[kind] = summ
    out = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "quarters": quarters,
           "buyFilings": len(buys), "tickersPriced": len(px), "priceFailures": failed, "summary": res}
    json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "insider_backtest.json"), "w"), indent=1)
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
