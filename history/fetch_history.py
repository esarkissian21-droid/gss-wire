"""Download weekly adjusted closes for history/tickers.txt into history/data/{T}.csv.
Runs on GitHub Actions (the analyzer workspace has no route to price sites).
Source: Yahoo Finance chart API (adjusted for splits and dividends); fallback Stooq (split-adjusted)."""
import csv, io, json, os, sys, time, urllib.request
from datetime import datetime, timezone

UA = "Mozilla/5.0 (gss-wire history fetch; personal research)"
START = 1325376000  # 2012-01-01

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def yahoo(t):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?period1={START}"
           f"&period2={int(time.time())}&interval=1wk&events=div%2Csplit")
    j = json.loads(get(url))["chart"]["result"][0]
    ts = j["timestamp"]; q = j["indicators"]
    adj = q.get("adjclose", [{}])[0].get("adjclose") or q["quote"][0]["close"]
    close = q["quote"][0]["close"]
    rows = []
    for i, s in enumerate(ts):
        if adj[i] is None or close[i] is None:
            continue
        d = datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%d")
        rows.append((d, round(adj[i], 4), round(close[i], 4)))
    return rows, "yahoo-adjclose"

def stooq(t):
    raw = get(f"https://stooq.com/q/d/l/?s={t.lower()}.us&i=w").decode()
    rows = []
    for r in csv.DictReader(io.StringIO(raw)):
        if r.get("Date", "") >= "2012-01-01" and r.get("Close"):
            rows.append((r["Date"], float(r["Close"]), float(r["Close"])))
    if not rows:
        raise ValueError("stooq returned no rows")
    return rows, "stooq-close"

def main():
    tickers = []
    for line in open("history/tickers.txt"):
        line = line.split("#")[0].strip()
        tickers += line.split()
    os.makedirs("history/data", exist_ok=True)
    report = {}
    for t in tickers:
        for fn in (yahoo, stooq):
            try:
                rows, src = fn(t)
                with open(f"history/data/{t}.csv", "w", newline="") as f:
                    w = csv.writer(f); w.writerow(["date", "adjclose", "close"]); w.writerows(rows)
                report[t] = {"source": src, "rows": len(rows), "first": rows[0][0], "last": rows[-1][0]}
                break
            except Exception as e:
                report[t] = {"error": f"{fn.__name__}: {e}"[:200]}
            time.sleep(1)
        time.sleep(1)
        print(t, report[t])
    report["_fetchedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    json.dump(report, open("history/data/_report.json", "w"), indent=1)
    ok = sum(1 for k, v in report.items() if isinstance(v, dict) and "rows" in v)
    print(f"{ok}/{len(tickers)} tickers fetched")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
