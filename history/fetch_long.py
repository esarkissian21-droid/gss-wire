"""Long-run monthly data for the ratio study. Runs on GitHub Actions.
- World Bank 'Pink Sheet' monthly commodity prices (1960-present): gold, silver, copper.
- FRED (St. Louis Fed) monthly series, no key needed: UNRATE, USREC, BAA, GS10, INDPRO.
Writes history/long/*.csv and history/long/_report.json."""
import csv, io, json, os, re, sys, time, urllib.request
from datetime import datetime, timezone

UA = "Mozilla/5.0 (gss-wire research; personal use)"
OUT = "history/long"

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()

def pink_sheet():
    page = get("https://www.worldbank.org/en/research/commodity-markets").decode("utf-8", "ignore")
    links = re.findall(r'https?://[^"\']*CMO-Historical-Data-Monthly\.xlsx', page)
    if not links:
        raise RuntimeError("Pink Sheet monthly xlsx link not found on the World Bank page")
    url = links[0]
    raw = get(url)
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb["Monthly Prices"]
    rows = list(ws.iter_rows(values_only=True))
    hdr_i = next(i for i, r in enumerate(rows) if r and any(isinstance(c, str) and c.strip().lower() == "gold" for c in r))
    hdr = [str(c).strip().lower() if c else "" for c in rows[hdr_i]]
    cols = {k: hdr.index(k) for k in ("gold", "silver", "copper") if k in hdr}
    out = []
    for r in rows[hdr_i + 1:]:
        m = str(r[0] or "")
        mm = re.match(r"^(\d{4})M(\d{2})$", m)
        if not mm:
            continue
        vals = {}
        for k, i in cols.items():
            try:
                vals[k] = float(r[i])
            except (TypeError, ValueError):
                vals[k] = None
        out.append((f"{mm.group(1)}-{mm.group(2)}", vals.get("gold"), vals.get("silver"), vals.get("copper")))
    with open(f"{OUT}/pink_metals_monthly.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["month", "gold_usd_oz", "silver_usd_oz", "copper_usd_mt"]); w.writerows(out)
    return {"source": url, "rows": len(out), "first": out[0][0], "last": out[-1][0]}

def fred(series):
    raw = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}").decode()
    rows = list(csv.reader(io.StringIO(raw)))
    with open(f"{OUT}/fred_{series}.csv", "w", newline="") as f:
        csv.writer(f).writerows(rows)
    return {"rows": len(rows) - 1, "first": rows[1][0], "last": rows[-1][0]}

def main():
    os.makedirs(OUT, exist_ok=True)
    rep = {}
    try:
        rep["pink"] = pink_sheet()
    except Exception as e:
        rep["pink"] = {"error": f"{type(e).__name__}: {e}"[:300]}
    for s in ("UNRATE", "USREC", "BAA", "GS10", "INDPRO"):
        try:
            rep[s] = fred(s)
        except Exception as e:
            rep[s] = {"error": f"{type(e).__name__}: {e}"[:200]}
        time.sleep(1)
    rep["_fetchedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    json.dump(rep, open(f"{OUT}/_report.json", "w"), indent=1)
    print(json.dumps(rep, indent=1))
    return 0 if "rows" in rep.get("pink", {}) else 1

if __name__ == "__main__":
    sys.exit(main())
