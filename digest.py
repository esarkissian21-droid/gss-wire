#!/usr/bin/env python3
"""Sunday digest + blind forward test for gss-wire.

Runs weekly on GitHub Actions (Sunday ~5 p.m. PT):
  1. Downloads weekly closes (Yahoo Finance chart API) for gold/silver futures, SPY,
     the watchlist and the fixed 2011 top-20 list.
  2. Forward test: applies forward/rules_frozen.json mechanically to every completed
     week since the start week, logs signals to forward/ledger.jsonl and fills in
     outcomes once their horizons pass. Nobody (including Claude) picks or edits entries.
  3. Sends one digest message to Telegram (and ntfy).

Env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, NTFY_TOPIC (any one is enough), DRY_RUN=1 to print only.
"""
import datetime as dt
import hashlib
import json
import os
import statistics as st
import sys
import time
import urllib.request

import wire

HERE = os.path.dirname(os.path.abspath(__file__))
FWD = os.path.join(HERE, "forward")
RULES_PATH = os.path.join(FWD, "rules_frozen.json")
PRED_PATH = os.path.join(FWD, "predictions_v1.json")
LEDGER_PATH = os.path.join(FWD, "ledger.jsonl")
FSTATE_PATH = os.path.join(FWD, "state.json")
UA = "Mozilla/5.0 (gss-wire weekly digest; personal research)"
PRED = None  # loaded in main()


# ---------------------------------------------------------------- data
def friday_of(d):
    return d + dt.timedelta(days=(4 - d.weekday()) % 7) if d.weekday() <= 4 else d - dt.timedelta(days=d.weekday() - 4)


def yahoo_weekly(symbol, fetch, years=2):
    """{friday_date: adjusted close} for completed and partial weeks."""
    now = int(time.time())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(symbol)}"
           f"?period1={now - years * 366 * 86400}&period2={now}&interval=1wk&events=div%2Csplit")
    j = json.loads(fetch(url))["chart"]["result"][0]
    ind = j["indicators"]
    closes = (ind.get("adjclose") or [{}])[0].get("adjclose") or ind["quote"][0]["close"]
    out = {}
    for ts, c in zip(j["timestamp"], closes):
        if c is None:
            continue
        d = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()
        if d.weekday() == 6:  # Yahoo labels some weeks with the Sunday before
            d += dt.timedelta(days=1)
        out[friday_of(d)] = float(c)  # a later row in the same week (partial-week duplicate) wins
    return out


def http(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def load_prices(rules, fetch, today, sleep=time.sleep):
    syms = ["GC=F", "SI=F", "SPY"] + rules["universes"]["fair"] + rules["universes"]["watchlist_hindsight"]
    prices, failed = {}, []
    for s in dict.fromkeys(syms):
        try:
            series = yahoo_weekly(s, fetch)
            # only completed weeks: drop the week that hasn't closed yet
            prices[s] = {d: v for d, v in series.items() if d < today or (d == today and today.weekday() >= 5)}
        except Exception as e:
            failed.append(f"{s}: {type(e).__name__}")
        sleep(0.4)
    return prices, failed


# ---------------------------------------------------------------- forward test
def rules_hash():
    with open(RULES_PATH, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def read_ledger():
    if not os.path.exists(LEDGER_PATH):
        return []
    with open(LEDGER_PATH, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def write_ledger(rows):
    os.makedirs(FWD, exist_ok=True)
    with open(LEDGER_PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def zone_of(pred, r):
    for z in pred["zones"]:
        if z["lo"] <= r < z["hi"]:
            return z
    return None


def forward_step(rules, prices, fstate, ledger, rhash, pred=None):
    """Log new signals for completed weeks >= startWeek, then score anything whose horizon has passed."""
    start = dt.date.fromisoformat(rules["startWeek"])
    ratio = {d: prices["GC=F"][d] / prices["SI=F"][d] for d in prices.get("GC=F", {}) if d in prices.get("SI=F", {})}
    weeks = sorted(ratio)
    spy = prices.get("SPY", {})
    last = dt.date.fromisoformat(fstate["lastWeek"]) if fstate.get("lastWeek") else None
    new = []
    episodes = fstate.setdefault("episodes", {})
    last_big = fstate.setdefault("lastBig", {})
    for i, w in enumerate(weeks):
        if w < start or (last and w <= last) or i == 0:
            continue
        prev, now = ratio[weeks[i - 1]], ratio[w]
        R = rules["ratio"]
        for direction, lines in (("up", R["upLines"]), ("down", R["downLines"])):
            for L in lines:
                crossed = prev < L <= now if direction == "up" else prev > L >= now
                if not crossed:
                    continue
                key = f"{direction}{L}"
                lastw = episodes.get(key)
                ep = lastw is None or (w - dt.date.fromisoformat(lastw)).days > 7 * R["episodeCooldownWeeks"]
                episodes[key] = w.isoformat()
                new.append({"id": f"f-{w}-ratio-{key}", "kind": "ratio", "week": w.isoformat(), "dir": direction, "line": L,
                            "ratioAt": round(now, 3), "episode": ep, "rules": rhash, "out": {}})
        # big weeks
        for group, tickers in (("fair", rules["universes"]["fair"]), ("watchlist_hindsight", rules["universes"]["watchlist_hindsight"])):
            for t in tickers:
                s = prices.get(t, {})
                pw = weeks[i - 1]
                if w not in s or pw not in s or w not in spy:
                    continue
                mv = s[w] / s[pw] - 1
                if abs(mv) < rules["bigWeek"]["threshold"]:
                    continue
                lb = last_big.get(t)
                if lb and (w - dt.date.fromisoformat(lb)).days <= 7 * rules["bigWeek"]["noRepeatWeeks"]:
                    last_big[t] = w.isoformat()
                    continue
                last_big[t] = w.isoformat()
                new.append({"id": f"f-{w}-big-{t}", "kind": "bigweek", "week": w.isoformat(), "group": group, "t": t,
                            "move": round(mv, 4), "priceAt": s[w], "spyAt": spy[w], "rules": rhash, "out": {}})
        # momentum check every N weeks from the start week
        if (w - start).days % (7 * rules["momentum"]["everyWeeks"]) == 0 and w in spy:
            checks = {}
            for t in rules["universes"]["fair"]:
                s = prices.get(t, {})
                hist = [s[x] for x in weeks[max(0, i - 39):i + 1] if x in s]
                yr = w - dt.timedelta(weeks=52)
                back = s.get(yr) or s.get(yr - dt.timedelta(days=7)) or s.get(yr + dt.timedelta(days=7))
                if len(hist) < 40 or back is None or w not in s:
                    continue
                checks[t] = {"pass": s[w] > st.mean(hist) and s[w] > back, "p": s[w]}
            if checks:
                new.append({"id": f"f-{w}-momentum", "kind": "momentum", "week": w.isoformat(), "spyAt": spy[w],
                            "checks": checks, "rules": rhash, "out": {}})
        # frozen probability forecast for this week
        if pred and w >= dt.date.fromisoformat(pred["startWeek"]):
            z = zone_of(pred, now)
            if z:
                new.append({"id": f"f-{w}-pred", "kind": "pred", "week": w.isoformat(), "ratioAt": round(now, 3),
                            "zone": z["zone"], "p": z["pLower"], "base": pred["baseRateLower"], "model": pred["version"], "out": {}})
        fstate["lastWeek"] = w.isoformat()
    ledger = ledger + new

    # scoring
    scored_now = 0
    for row in ledger:
        w0 = dt.date.fromisoformat(row["week"])
        if row["kind"] == "ratio":
            hs = rules["ratio"]["horizonsWeeks"]
        elif row["kind"] == "bigweek":
            hs = rules["bigWeek"]["horizonsWeeks"]
        elif row["kind"] == "pred":
            hs = [int(h) for h in row["p"]]
        else:
            hs = rules["momentum"]["horizonsWeeks"]
        for h in hs:
            k = str(h)
            if k in row["out"]:
                continue
            wh = w0 + dt.timedelta(weeks=h)
            if row["kind"] == "pred":
                if wh not in ratio:
                    continue
                y = 1.0 if ratio[wh] < row["ratioAt"] else 0.0
                p, b = row["p"][k], row["base"][k]
                row["out"][k] = {"lower": bool(y), "brier": round((p - y) ** 2, 4), "baseBrier": round((b - y) ** 2, 4)}
            elif row["kind"] == "ratio":
                if wh not in ratio:
                    continue
                later = ratio[wh]
                res = row["ratioAt"] / later - 1 if row["dir"] == "up" else later / row["ratioAt"] - 1
                row["out"][k] = {"goldOz": round(res, 4), "right": res > 0, "ratio": round(later, 3)}
            elif row["kind"] == "bigweek":
                s = prices.get(row["t"], {})
                if wh not in s or wh not in spy:
                    continue
                ex = (s[wh] / row["priceAt"]) / (spy[wh] / row["spyAt"]) - 1
                row["out"][k] = {"excess": round(ex, 4), "right": ex < 0}
            else:
                if wh not in spy:
                    continue
                g = {"pass": [], "fail": []}
                for t, c in row["checks"].items():
                    s = prices.get(t, {})
                    if wh in s:
                        g["pass" if c["pass"] else "fail"].append((s[wh] / c["p"]) / (spy[wh] / row["spyAt"]) - 1)
                if not (g["pass"] and g["fail"]):
                    continue
                mp, mf = st.mean(g["pass"]), st.mean(g["fail"])
                row["out"][k] = {"passMean": round(mp, 4), "failMean": round(mf, 4), "right": mp > mf,
                                 "nPass": len(g["pass"]), "nFail": len(g["fail"])}
            scored_now += 1
    return ledger, fstate, new, scored_now


def forward_summary(ledger):
    sig = [r for r in ledger if r["kind"] in ("ratio", "bigweek") and (r["kind"] != "bigweek" or r["group"] == "fair")]
    scored = [r for r in sig if r["out"]]
    right = 0
    for r in scored:
        k = max(r["out"], key=int)  # longest horizon reached so far
        right += bool(r["out"][k]["right"])
    preds = {}
    for r in ledger:
        if r["kind"] == "pred":
            for k, o in r["out"].items():
                preds.setdefault(k, []).append(o)
    brier = {k: {"n": len(v), "model": round(sum(o["brier"] for o in v) / len(v), 4),
                 "base": round(sum(o["baseBrier"] for o in v) / len(v), 4), "coin": 0.25} for k, v in preds.items()}
    return {"logged": len(ledger), "signals": len(sig), "scored": len(scored), "right": right, "brier": brier}


# ---------------------------------------------------------------- digest text
def pct(x, dp=1):
    return ("+" if x > 0 else "") + f"{100 * x:.{dp}f}%".replace("-", "−")


def zone_text(r):
    if r >= 90:
        return "at or above 90: last silver third per your lines"
    if r >= 86:
        return "86 to 90: second silver third per your lines (replay: weakest line)"
    if r >= 81:
        return "81 to 86: first silver third per your lines"
    if r > 72:
        return "72 to 81: no-action zone"
    if r > 66:
        return "66 to 72: first third back to gold"
    if r > 60:
        return "60 to 66: second third back to gold"
    return "60 or below: last third back to gold"


def wire_week(log_path, since):
    pushed = []
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    l = json.loads(line)
                except ValueError:
                    continue
                if l.get("pushed") and l.get("seenAt", "") >= since:
                    pushed.append(l)
    return pushed


def build_digest(cfg, prices, today, ledger, wire_pushed, failed):
    lines = []
    gc, si, spy = prices.get("GC=F", {}), prices.get("SI=F", {}), prices.get("SPY", {})
    common = sorted(set(gc) & set(si))
    if len(common) >= 2:
        w, pw = common[-1], common[-2]
        r, pr = gc[w] / si[w], gc[pw] / si[pw]
        lines.append(f"Week ending {w.strftime('%b %-d')}")
        lines.append(f"Gold ${gc[w]:,.0f} ({pct(gc[w] / gc[pw] - 1)}) · Silver ${si[w]:,.2f} ({pct(si[w] / si[pw] - 1)})")
        lines.append(f"Ratio {r:.1f} (was {pr:.1f}): {zone_text(r)}")
        pz = zone_of(PRED, r) if PRED else None
        if pz:
            p52 = pz["pLower"]["52"]
            lines.append(f"Model odds ratio is lower in a year: {100 * p52:.0f}% (1972-2026 history for this zone"
                         f"{', thin evidence' if pz['nEff52'] < 5 else ''}; any month: {100 * PRED['baseRateLower']['52']:.0f}%)")
        for h in cfg.get("holdings", []):
            if h.get("asset") == "silver":
                val = h["oz"] * si[w]
                goz = h["ratioAt"] / r - 1
                lines.append(f"Your {h['oz']:g} oz bar: ${val:,.0f} vs ${h['cost']:,.0f} paid ({pct(val / h['cost'] - 1)}); "
                             f"in gold terms {pct(goz)} (bought at ratio {h['ratioAt']})")
    else:
        lines.append("Metals prices unavailable this week.")
    if len(spy) >= 2:
        ws = sorted(spy)
        spy_w = spy[ws[-1]] / spy[ws[-2]] - 1
        lines.append(f"S&P 500 (SPY) {pct(spy_w)} on the week")
        moves = []
        for c in cfg.get("companies", []):
            s = prices.get(c["ticker"], {})
            if ws[-1] in s and ws[-2] in s:
                moves.append((s[ws[-1]] / s[ws[-2]] - 1, c["ticker"]))
        if moves:
            moves.sort()
            txt = ", ".join(f"{t} {pct(m)}" for m, t in sorted(moves, key=lambda x: -abs(x[0]))[:5])
            lines.append(f"Biggest watchlist moves: {txt}")
            ups = [t for m, t in moves if m >= 0.15]
            downs = [t for m, t in moves if m <= -0.15]
            if ups:  # rule p3 (approved Nov 22 2026): 3-month wait after a 15%+ up week
                until = (dt.date.fromisoformat(ws[-1]) + dt.timedelta(days=91)).strftime("%b %-d")
                lines.append(f"15%+ up week, wait list (don't buy until {until}): {', '.join(ups)}")
            if downs:
                lines.append(f"15%+ down week (review the story, don't auto-buy): {', '.join(downs)}")
    if wire_pushed:
        kinds = {}
        for l in wire_pushed:
            kinds[l.get("kind", "other")] = kinds.get(l.get("kind", "other"), 0) + 1
        names = {"filing": "filings", "insider": "insider buys", "news": "headlines", "fed": "Fed"}
        lines.append("Wire alerts this week: " + ", ".join(f"{n} {names.get(k, k)}" for k, n in kinds.items()))
        for l in sorted(wire_pushed, key=lambda x: -x.get("priority", 0))[:3]:
            lines.append(f"  • {l.get('asset')}: {l.get('title', '')[:110]}")
    else:
        lines.append("Wire alerts this week: none")
    soon = [e for e in cfg.get("calendar", []) if today.isoformat() <= e["date"] <= (today + dt.timedelta(days=14)).isoformat()]
    if soon:
        lines.append("Coming up: " + "; ".join(f"{dt.date.fromisoformat(e['date']).strftime('%a %b %-d')} {e['what']}" for e in soon))
    fs = forward_summary(ledger)
    if fs.get("brier"):
        k = max(fs["brier"], key=int)
        b = fs["brier"][k]
        lines.append(f"Forecast accuracy ({k}-week, {b['n']} scored, lower is better): model {b['model']:.3f} vs base rate {b['base']:.3f} vs coin flip 0.250")
    if fs["signals"] == 0:
        lines.append("Blind forward test: running from the week ending Nov 27; no signals yet.")
    else:
        sc = f"{fs['right']} of {fs['scored']} right so far" if fs["scored"] else "none old enough to score yet"
        lines.append(f"Blind forward test: {fs['signals']} signals logged since Nov 27, {sc}.")
    if failed:
        lines.append(f"(Couldn't fetch {len(failed)} price series this week.)")
    return "\n".join(lines)


def send(title, msg, prio=3):
    sent = 0
    tok, chat = os.environ.get("TELEGRAM_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tok and chat:
        wire.telegram_push(tok, chat, title, msg, prio)
        sent += 1
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        try:
            wire.ntfy_push(topic, title, msg, prio, ["bar_chart"])
            sent += 1
        except Exception as e:
            print("ntfy failed:", e)
    return sent


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    rules = wire.load_json(RULES_PATH, None)
    cfg = wire.load_json(wire.CONFIG_PATH, {})
    rhash = rules_hash()
    global PRED
    PRED = wire.load_json(PRED_PATH, None)
    prices, failed = load_prices(rules, http, today)
    fstate = wire.load_json(FSTATE_PATH, {})
    ledger, fstate, new, scored_now = forward_step(rules, prices, fstate, read_ledger(), rhash, PRED)
    fstate.update(rulesHash=rhash, lastRun=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                  lastNew=len(new), lastScored=scored_now)
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat(timespec="seconds")
    msg = build_digest(cfg, prices, today, ledger, wire_week(wire.LOG_PATH, since), failed)
    print(msg)
    print(f"\nforward: +{len(new)} signals, {scored_now} outcomes filled, rules {rhash}; failed: {failed}")
    if os.environ.get("DRY_RUN") == "1":
        return 0
    os.makedirs(FWD, exist_ok=True)
    write_ledger(ledger)
    wire.save_json(FSTATE_PATH, fstate)
    return 0 if send("Weekly digest", msg, 3) else 1


if __name__ == "__main__":
    sys.exit(main())
