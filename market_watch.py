"""Market-hours watch: runs inside each wire pass while the US market is open.

For every stock on the watch list (your companies, the shortlist and the Grade A list) it checks the
day's move, trading volume and the 200-day trend. When something trips, one message per stock says:
  what happened (move, volume, how much of it is just the whole market),
  why (filings and trusted headlines from the last day, or "nothing found yet"),
  the odds from history (research/move_odds.json), labelled reliable or not,
  what your own rules say (shortlist tier, sell rule, Grade A status).
Every alert with odds is logged as a prediction and scored ~3 months later (Brier score vs a coin flip).
"""
import datetime as dt
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    NY = None

HERE = os.path.dirname(os.path.abspath(__file__))
ODDS_PATH = os.path.join(HERE, "research", "move_odds.json")
GRADE_PATH = os.path.join(HERE, "history", "screens", "grade_a.json")
EARN_PATH = os.path.join(HERE, "earnings.json")
EARN_LEAD_DAYS = 3


def earnings_due(earn, today, lead=EARN_LEAD_DAYS):
    """Earnings reports from today through `lead` days ahead. earn = earnings.json contents."""
    out = []
    for e in (earn or {}).get("list", []):
        try:
            d = dt.date.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        if 0 <= (d - today).days <= lead:
            out.append(e)
    return out


def earnings_message(e, today):
    d = dt.date.fromisoformat(e["date"])
    days = (d - today).days
    when = "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days} days")
    hhmm = e.get("timeUTC")
    if hhmm:
        h, m = (int(x) for x in hhmm.split(":"))
        utc = dt.datetime.combine(d, dt.time(h, m), tzinfo=dt.timezone.utc)
        when += " (call " + utc.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%-I:%M %p PT") + ")"
    src = ("Date confirmed by two sources." if e.get("confirmed") else
           "Date from one source; companies sometimes move it, so check their investor page.")
    return (f"{e['t']} reports {e.get('period', 'earnings')} {when}, {d.strftime('%a %b %-d')}.",
            "Prices often jump on earnings day. Your rules: don't open a new position or a paper option right "
            "before the report; after it, re-check the thesis and sell rule. " + src)
UA = "Mozilla/5.0 (gss-wire watch; personal use)"
MAX_TICKERS = 45
HORIZON_DAYS = 63  # trading days for predictions (about 3 months)
# Typical share of a day's volume traded by N minutes after the open (heavy at the open and close).
VOLUME_CURVE = [(0, 0.0), (30, 0.12), (60, 0.20), (120, 0.33), (180, 0.44), (240, 0.55), (300, 0.66),
                (360, 0.80), (390, 1.0)]
BUCKET_TEXT = {"drop3": "drops of 3-6%", "drop6": "drops of 6% or more", "jump3": "jumps of 3-6%",
               "jump6": "jumps of 6% or more", "vol2dn": "heavy-volume down days", "vol2up": "heavy-volume up days"}


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def session(ts):
    """'open' during 9:30-16:00 New York time on weekdays, else None. Holidays show up as no bar for today."""
    ny = ts.astimezone(NY) if NY else ts - dt.timedelta(hours=5)
    if ny.weekday() >= 5:
        return None, ny
    mins = ny.hour * 60 + ny.minute
    return ("open" if 9 * 60 + 30 <= mins <= 16 * 60 + 10 else None), ny


def volume_done(mins):
    """Expected fraction of a normal day's volume traded by `mins` minutes after the open."""
    mins = max(0, min(390, mins))
    for (a, fa), (b, fb) in zip(VOLUME_CURVE, VOLUME_CURVE[1:]):
        if mins <= b:
            return fa + (fb - fa) * (mins - a) / (b - a)
    return 1.0


def parse_chart(raw, today_ny):
    j = json.loads(raw)["chart"]["result"][0]
    ts = j.get("timestamp") or []
    q = j["indicators"]["quote"][0]
    rows = [(dt.datetime.fromtimestamp(t, NY or dt.timezone.utc).date().isoformat(), c, v)
            for t, c, v in zip(ts, q["close"], q.get("volume") or [None] * len(ts)) if c is not None]
    if len(rows) < 60:
        return None
    meta = j.get("meta", {})
    price = meta.get("regularMarketPrice") or rows[-1][1]
    traded_today = rows[-1][0] == today_ny
    prior = rows[:-1] if traded_today else rows
    prev = prior[-1][1]
    vols = [v for _, _, v in prior[-50:] if v]
    closes = [c for _, c, _ in prior]
    return {
        "price": price, "prev": prev, "pct": 100 * (price / prev - 1), "tradedToday": traded_today,
        "vol": (rows[-1][2] or 0) if traded_today else 0, "avgVol": sum(vols) / len(vols) if vols else None,
        "ma200": sum(closes[-200:]) / len(closes[-200:]) if len(closes) >= 200 else None,
        "chg52": 100 * (price / closes[-252] - 1) if len(closes) >= 252 else None,
        "hist": {d: c for d, c, _ in rows},
    }


def chart_url(sym):
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d"


def odds_for(odds, bucket, trend):
    """-> (p_beat or None, sentence, base rate or None)."""
    b = ((odds or {}).get("buckets") or {}).get(bucket) or {}
    r = (b.get(trend) or {}).get(str(HORIZON_DAYS)) or {}
    if not r.get("pBeat"):
        r = (b.get("all") or {}).get(str(HORIZON_DAYS)) or {}
        trend = None
    if not r.get("pBeat"):
        return None, "Odds: no history for this yet (base-rate study still running).", None
    where = {"above200": " in an uptrend", "below200": " in a downtrend", None: ""}[trend]
    span = "-".join(s[2:4] for s in (odds.get("span") or ["2005", "2026"]))
    line = (f"Odds (~{odds.get('universe', 100)} big US stocks, 20{span}): after {BUCKET_TEXT[bucket]}{where}, "
            f"the stock beat the S&P 500 over the next 3 months {r['pBeat']:.0f}% of the time "
            f"vs {r['baseBeat']:.0f}% on ordinary days (n={r['n']:,}).")
    if r.get("pUp") and r.get("baseUp"):
        line += f" Higher in price 3 months later: {r['pUp']:.0f}% vs {r['baseUp']:.0f}% normally."
    if r.get("reliable"):
        line += f" This edge held up in both halves of the history (t={r['t']})."
    else:
        line += " Not a reliable edge: treat it as close to a coin flip."
    for h, words in (("5", "next week"), ("21", "next month")):
        s = (b.get("all") or {}).get(h) or {}
        if s.get("reliable"):
            line += (f" Shorter term, the {words} it beat the market {s['pBeat']:.0f}% of the time vs "
                     f"{s['baseBeat']:.0f}% (reliable).")
    return r["pBeat"] / 100, line, (r["baseBeat"] / 100 if r.get("baseBeat") else None)


def why_lines(items, limit=3):
    out = []
    for it in sorted(items, key=lambda i: i.get("date") or "", reverse=True)[:limit]:
        label = {"filing": "Filing", "insider": "Insider", "news": "Headline", "fed": "Fed"}.get(it.get("kind"), "Item")
        out.append(f"{label}: {it.get('title', '')[:140]}" + (f" ({it['source']})" if it.get("source") and it.get("kind") == "news" else ""))
    return out


def fmt_pct(x):
    return f"{'+' if x >= 0 else '−'}{abs(x):.1f}%"


def run_watch(cfg, state, fetch, ts, recent_items, extra_why=None, workers=8):
    """One watch pass. Returns (pushes [(title, msg, prio, tags, click)], log_lines). Mutates state['watch']."""
    w = state.setdefault("watch", {})
    w.setdefault("preds", [])
    pushes, logs = [], []
    stamp = ts.isoformat(timespec="seconds")

    # Grade A list changes (the weekly screens job writes grade_a.json; the wire announces it)
    grade = load(GRADE_PATH, {}) or {}
    if grade.get("asOf") and grade.get("asOf") != w.get("gradeSeen"):
        names = list(grade.get("list", {}))
        if not w.get("gradeSeen"):
            msg = (f"{len(names)} companies pass the Grade A rules ({grade.get('version')}): {', '.join(names)}. "
                   "The watch now follows them during market hours.")
            pushes.append(("Grade A list is live", msg, 3, ["star"], None))
        elif grade.get("added") or grade.get("removed"):
            parts = []
            if grade.get("added"):
                parts.append("New: " + ", ".join(grade["added"]))
            for t, why in (grade.get("removed") or {}).items():
                parts.append(f"Out: {t} ({'; '.join(why[:2])})")
            pushes.append((f"Grade A changes · {grade['asOf']}", "\n".join(parts), 4, ["star"], None))
        w["gradeSeen"] = grade["asOf"]

    sess, ny = session(ts)
    today = ny.date().isoformat()

    # Earnings in the next 3 days (earnings.json, refreshed weekly). One alert per company per report.
    seen = w.setdefault("earnSeen", [])
    for e in earnings_due(load(EARN_PATH, {}), ny.date()):
        key = f"{e['t']}-{e['date']}"
        if key in seen:
            continue
        title, msg = earnings_message(e, ny.date())
        pushes.append((title, msg, 3, ["calendar"], f"https://finance.yahoo.com/quote/{e['t']}"))
        logs.append({"id": f"earn-{key}", "date": stamp, "kind": "earnings", "asset": e["t"], "source": "earnings",
                     "title": title, "priority": 3, "seenAt": stamp, "pushed": True})
        seen.append(key)
    w["earnSeen"] = seen[-200:]
    if w.get("day") != today:
        w["day"], w["fired"] = today, {}
    shortlist = cfg.get("shortlist", {})
    tickers = []
    for t in ([c["ticker"] for c in cfg.get("companies", [])] + list(shortlist) + cfg.get("watch_extra", [])
              + list(grade.get("list", {}))):
        if t not in tickers:
            tickers.append(t)
    tickers = tickers[:MAX_TICKERS]
    need_scoring = [p for p in w["preds"] if "out" not in p and p["due"] <= today][:8]
    if sess != "open" and not need_scoring:
        return pushes, logs

    def get(sym):
        try:
            return sym, parse_chart(fetch(chart_url(sym), UA), today)
        except Exception:
            return sym, None

    want = ["SPY"] + (tickers if sess == "open" else []) + [p["t"] for p in need_scoring]
    with ThreadPoolExecutor(workers) as ex:
        quotes = dict(ex.map(get, list(dict.fromkeys(want))))
    spy = quotes.get("SPY")

    # score predictions that are due
    for p in need_scoring:
        q = quotes.get(p["t"])
        if not q or not spy:
            continue
        days = sorted(d for d in q["hist"] if d > p["d"])
        if len(days) < HORIZON_DAYS:
            continue
        end = days[HORIZON_DAYS - 1]
        if end not in spy["hist"]:
            continue
        ret = q["hist"][end] / p["px"] - 1
        mret = spy["hist"][end] / p["spy"] - 1
        p["out"] = {"end": end, "ret": round(100 * ret, 2), "mkt": round(100 * mret, 2), "beat": ret > mret}
    scored = [p for p in w["preds"] if "out" in p]
    if scored:
        brier = sum((p["p"] - (1 if p["out"]["beat"] else 0)) ** 2 for p in scored) / len(scored)
        # Fair test: the odds must beat simply guessing the ordinary-day rate (base), not just a coin flip.
        based = [p for p in scored if p.get("base") is not None]
        brier_base = (sum((p["base"] - (1 if p["out"]["beat"] else 0)) ** 2 for p in based) / len(based)
                      if based else None)
        brier_same = (sum((p["p"] - (1 if p["out"]["beat"] else 0)) ** 2 for p in based) / len(based)
                      if based else None)
        w["score"] = {"n": len(scored), "beat": sum(p["out"]["beat"] for p in scored),
                      "brier": round(brier, 4), "coinFlip": 0.25,
                      "brierBase": round(brier_base, 4) if brier_base is not None else None,
                      "brierVsBaseN": len(based),
                      "brierSameSet": round(brier_same, 4) if brier_same is not None else None}

    if sess != "open" or not spy or not spy["tradedToday"]:
        return pushes, logs  # closed, or a market holiday

    mins_in = (ny.hour * 60 + ny.minute) - (9 * 60 + 30)
    frac = max(0.12, volume_done(mins_in))
    odds = load(ODDS_PATH, {})
    by_asset = {}
    for it in recent_items:
        by_asset.setdefault(it.get("asset"), []).append(it)
    lookups = 0
    for t in tickers:
        q = quotes.get(t)
        if not q or not q["tradedToday"]:
            continue
        fired = w["fired"].setdefault(t, {})
        pct = q["pct"]
        level = int(abs(pct) // 3) * 3
        vol_x = q["vol"] / (q["avgVol"] * frac) if q["avgVol"] and mins_in >= 45 else None
        above = q["ma200"] is not None and q["price"] > q["ma200"]
        prev_above = q["ma200"] is not None and q["prev"] > q["ma200"]
        triggers = []
        if level >= 3 and level > fired.get("move", 0):
            fired["move"] = level
            triggers.append("move")
        if vol_x and vol_x >= 2 and abs(pct) >= 1.5 and not fired.get("vol"):
            fired["vol"] = True
            triggers.append("volume")
        watched_trend = t in shortlist or t in grade.get("list", {})
        if watched_trend and q["ma200"] and above != prev_above and not fired.get("ma"):
            fired["ma"] = True
            triggers.append("trend")
        if not triggers:
            continue

        mkt = spy["pct"]
        own = pct - mkt
        how = [f"{t} {fmt_pct(pct)} at ${q['price']:,.2f} · {ny.strftime('%-I:%M %p')} ET"]
        if vol_x:
            how.append(f"Volume so far is {vol_x:.1f}× a normal day's pace.")
        how.append(f"S&P 500 {fmt_pct(mkt)} today, so about {fmt_pct(own)} of this is {t}'s own move."
                   if abs(mkt) >= 0.5 else "The overall market is roughly flat, so this move is about the company.")
        if "trend" in triggers:
            how.append(f"It just crossed {'above' if above else 'below'} its 200-day average (${q['ma200']:,.2f}).")

        items = list(by_asset.get(t, []))
        if not items and extra_why and lookups < 4:
            lookups += 1
            try:
                items = extra_why(t, (grade.get("list", {}).get(t) or {}).get("name"))
            except Exception:
                items = []
        why = why_lines(items) or ["Nothing in filings or trusted headlines from the last day yet. "
                                   "Moves with no news often come from sector or analyst moves."]

        bucket = None
        if "move" in triggers or abs(pct) >= 3:
            bucket = ("drop6" if pct <= -6 else "drop3") if pct < 0 else ("jump6" if pct >= 6 else "jump3")
        elif "volume" in triggers:
            bucket = "vol2dn" if pct < 0 else "vol2up"
        p_beat, odds_line, p_base = odds_for(odds, bucket, "above200" if prev_above else "below200") if bucket else (None, "", None)

        rules = []
        if t in shortlist:
            s = shortlist[t]
            rules.append(f"Shortlist Tier {s.get('tier', '?')}. Sell rule: {s.get('sell', 'n/a')}")
        if t in grade.get("list", {}):
            rules.append("Grade A: " + ("still passes on price." if above else "AT RISK: price is now below its 200-day average."))

        pred_note = ""
        if p_beat is not None:
            due = (ny.date() + dt.timedelta(days=92)).isoformat()
            pid = f"{t}-{today}-{bucket}"
            if not any(p["id"] == pid for p in w["preds"]):
                w["preds"].append({"id": pid, "t": t, "d": today, "px": round(q["price"], 4),
                                   "spy": round(spy["price"], 4), "bucket": bucket, "p": round(p_beat, 3),
                                   "base": round(p_base, 3) if p_base is not None else None,
                                   "reliable": "reliable edge" in odds_line, "due": due})
                pred_note = f"Logged as a prediction; scored around {due}."

        sections = [" ".join(how[1:]), "Why: " + " | ".join(why)]
        if odds_line:
            sections.append(odds_line)
        if rules:
            sections.append("Your rules: " + " ".join(rules))
        if pred_note:
            sections.append(pred_note)
        prio = 4 if (abs(pct) >= 6 or t in shortlist or "trend" in triggers and t in grade.get("list", {})) else 3
        tags = ["chart_with_downwards_trend"] if pct < 0 else ["chart_with_upwards_trend"]
        title = how[0]
        pushes.append((title, "\n".join(sections), prio, tags, f"https://finance.yahoo.com/quote/{t}"))
        logs.append({"id": f"watch-{t}-{stamp}", "date": stamp, "kind": "watch", "asset": t, "source": "watch",
                     "title": title, "priority": prio, "seenAt": stamp, "pushed": True,
                     "note": "; ".join(triggers) + (f"; pBeat={p_beat:.2f}" if p_beat is not None else "")})
    w["preds"] = w["preds"][-600:]
    w["lastScan"] = {"at": stamp, "tickers": len(tickers), "quotes": sum(1 for t in tickers if quotes.get(t)),
                     "alerts": len(logs)}
    return pushes, logs


# ---------------------------------------------------------------------------------------------------------------
# Price snapshot: every wire pass (day and night) saves fresh prices for the analyzer to pick up.
SNAPSHOT_BASE = ["SPY", "VTI", "GLD", "SLV", "GC=F", "SI=F"]


def snapshot_list(cfg):
    out = []
    for t in (SNAPSHOT_BASE + [c["ticker"] for c in cfg.get("companies", [])] + list(cfg.get("shortlist", {}))
              + cfg.get("snapshot_extra", [])):
        if t not in out:
            out.append(t)
    return out


def parse_snapshot(raw):
    j = json.loads(raw)["chart"]["result"][0]
    meta = j.get("meta", {})
    ts = j.get("timestamp") or []
    closes = (j["indicators"]["quote"][0].get("close") or [])
    rows = [(dt.datetime.fromtimestamp(t, NY or dt.timezone.utc).date().isoformat(), c)
            for t, c in zip(ts, closes) if c is not None]
    price = meta.get("regularMarketPrice")
    if price is None and rows:
        price = rows[-1][1]
    if price is None:
        return None
    mtime = meta.get("regularMarketTime")
    as_of = dt.datetime.fromtimestamp(mtime, dt.timezone.utc).isoformat(timespec="minutes") if mtime else None
    last_day = rows[-1][0] if rows else None
    prior = [c for d, c in rows if d != (as_of and dt.datetime.fromtimestamp(mtime, NY or dt.timezone.utc).date().isoformat())]
    prev = prior[-1] if prior else meta.get("chartPreviousClose")
    week_ago = prior[-5] if len(prior) >= 5 else None
    return {"price": round(price, 4), "asOf": as_of, "lastBar": last_day,
            "prevClose": round(prev, 4) if prev else None,
            "dayPct": round(100 * (price / prev - 1), 2) if prev else None,
            "weekPct": round(100 * (price / week_ago - 1), 2) if week_ago else None,
            "currency": meta.get("currency"), "src": "Yahoo Finance chart API"}


def snapshot(cfg, state, fetch, ts, workers=8):
    """Refresh state['quotes'] {sym: {...}}. Keeps the last good value (marked stale) when a fetch fails."""
    q = state.setdefault("quotes", {})

    def get(sym):
        try:
            return sym, parse_snapshot(fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1mo&interval=1d", UA))
        except Exception:
            return sym, None

    syms = snapshot_list(cfg)
    ok = 0
    with ThreadPoolExecutor(workers) as ex:
        for sym, res in ex.map(get, syms):
            if res:
                res["fetchedAt"] = ts.isoformat(timespec="minutes")
                q[sym] = res
                ok += 1
            elif sym in q:
                q[sym]["stale"] = True
    gc, si = q.get("GC=F"), q.get("SI=F")
    if gc and si and si.get("price"):
        state["quotesRatio"] = {"ratio": round(gc["price"] / si["price"], 2), "asOf": ts.isoformat(timespec="minutes"),
                                "note": "COMEX front-month futures (usually 0-1.5% above spot)"}
    state["quotesMeta"] = {"at": ts.isoformat(timespec="minutes"), "ok": ok, "of": len(syms)}
    return ok, len(syms)
