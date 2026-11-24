"""Probe free option-chain sources from the GitHub runner (Cboe delayed quotes, Yahoo). Writes research/options_probe.json."""
import json, os, urllib.request, time
UA = "Mozilla/5.0 (gss-wire research; personal use)"
out = {}
def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()
for sym in ("TRV", "CBOE", "SSD"):
    try:
        j = json.loads(get(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"))
        opts = j["data"]["options"]
        sample = [o for o in opts if o.get("open_interest")][:3]
        out[sym] = {"source": "cboe", "n": len(opts), "price": j["data"].get("current_price"),
                    "keys": sorted(opts[0].keys()) if opts else [], "sample": sample, "timestamp": j.get("timestamp")}
    except Exception as e:
        out[sym] = {"cboe_error": f"{type(e).__name__}: {e}"}
    time.sleep(1)
try:
    j = json.loads(get("https://query2.finance.yahoo.com/v7/finance/options/TRV"))
    out["yahoo"] = {"ok": True, "keys": list(j.get("optionChain", {}).keys())}
except Exception as e:
    out["yahoo"] = {"error": f"{type(e).__name__}: {e}"}
json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "options_probe.json"), "w"), indent=1, default=str)
print(json.dumps({k: (v.get("n"), v.get("cboe_error") or v.get("error")) for k, v in out.items()}))
