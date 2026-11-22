#!/usr/bin/env python3
"""gss-wire: a personal, always-on news wire.

Every run (GitHub Actions, every 5 minutes) it checks:
  1. SEC EDGAR filings for each company in config.json (official, free)
  2. Federal Reserve monetary-policy press releases (official RSS)
  3. Headlines from Google News RSS, kept only when they come from a trusted
     outlet and mention something that can move a stock
New items go to your phone through ntfy. Everything seen is appended to
wire_log.jsonl so Claude's daily analyzer run can read it.

Standard library only. Secrets come from environment variables:
  NTFY_TOPIC   your private ntfy channel name
  SEC_CONTACT  "Your Name your@email.com" (the SEC asks automated tools to identify themselves)
"""
import datetime as dt
import email.utils
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "wire_log.jsonl")
LOG_KEEP = 1500
SEEN_DAYS = 45
GENERIC_UA = "gss-wire/1.0 (personal news monitor; +https://github.com)"

FORM_RULES = {
    # form: (priority, plain-English label)
    "8-K": (3, "8-K current report"),
    "8-K/A": (3, "8-K amendment"),
    "6-K": (3, "6-K report (foreign company: often earnings or news)"),
    "10-Q": (3, "10-Q quarterly report"),
    "10-K": (3, "10-K annual report"),
    "20-F": (3, "20-F annual report"),
    "40-F": (3, "40-F annual report"),
    "SC 13D": (4, "13D: a big holder plans to push for change"),
    "SC 13D/A": (3, "13D amendment from a big holder"),
    "425": (4, "425: merger or deal communication"),
    "SC TO-T": (5, "Tender offer for the company's shares"),
    "SC 14D9": (4, "Company response to a tender offer"),
    "NT 10-Q": (4, "Late quarterly report notice"),
    "NT 10-K": (4, "Late annual report notice"),
}
ITEMS = {
    "1.01": "new material agreement", "1.02": "agreement terminated", "1.03": "bankruptcy",
    "1.05": "cybersecurity incident", "2.01": "acquisition or sale completed", "2.02": "earnings results",
    "2.03": "new debt", "2.04": "debt acceleration", "2.05": "restructuring or exit costs", "2.06": "asset impairment",
    "3.01": "delisting notice", "3.02": "unregistered share sale", "3.03": "shareholder rights changed",
    "4.01": "auditor change", "4.02": "past financials no longer reliable", "5.01": "change in control",
    "5.02": "executive or director change", "5.03": "bylaws change", "5.07": "shareholder vote results",
    "7.01": "Reg FD disclosure", "8.01": "other events", "9.01": "exhibits",
}
URGENT_ITEMS = {"1.03", "1.05", "2.04", "2.06", "3.01", "4.01", "4.02", "5.01"}
HIGH_ITEMS = {"2.02", "5.02", "2.01", "2.05"}

STRONG = re.compile(
    r"\b(probe|investigat\w*|subpoena\w*|sec\b|doj\b|lawsuit|sues|sued|fraud|indict\w*|charged|"
    r"resign\w*|steps? down|ousted|fired|appoint\w*|names? (new )?(ceo|cfo|chief)|ceo|cfo|"
    r"guidance|outlook|forecast\w*|earnings|results|profit|revenue|beats?|miss(es|ed)?|"
    r"downgrad\w*|upgrad\w*|price target|acquir\w*|acquisition|merger|takeover|buyout|bid\b|stake|"
    r"spin-?off|separation|dividend|buyback|repurchase|offering|bankrupt\w*|default\w*|recall\w*|"
    r"halt\w*|suspend\w*|delist\w*|export|ban\w*|sanction\w*|tariff\w*|strike|outage|explosion|"
    r"cyber\w*|breach|hack\w*|record|plunge\w*|soar\w*|surge\w*|tumble\w*|crash\w*|slump\w*|jump\w*|"
    r"sink\w*|sank|rall(y|ies|ied)|selloff|sell-off|lowest|highest)\b",
    re.I,
)
METALS_STRICT = re.compile(
    r"\b(record|plunge\w*|soar\w*|surge\w*|tumble\w*|crash\w*|slump\w*|biggest|lowest|highest|"
    r"central bank\w*|fed\b|federal reserve|rate (hike|cut)|ratio)\b",
    re.I,
)


# ---------------------------------------------------------------- utilities
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def http_get(url, ua=GENERIC_UA, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def ntfy_push(topic, title, message, priority=3, tags=None, click=None):
    body = {"topic": topic, "title": title[:120], "message": message[:900], "priority": int(priority)}
    if tags:
        body["tags"] = tags
    if click:
        body["click"] = click
    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": GENERIC_UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def norm_words(title):
    t = re.sub(r"\s+-\s+[^-]+$", "", title.lower())  # drop " - Source"
    return {w for w in re.findall(r"[a-z0-9$%.]+", t) if len(w) > 2}


def similar(a, b, cut=0.6):
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= cut


def local_hour(ts, tzname):
    if ZoneInfo is None:
        return ts.hour
    return ts.astimezone(ZoneInfo(tzname)).hour


def in_quiet_hours(ts, cfg):
    q = cfg.get("quiet_hours") or {}
    if not q:
        return False
    h = local_hour(ts, cfg.get("timezone", "UTC"))
    s, e = q.get("start", 21), q.get("end", 5)
    return (h >= s or h < e) if s > e else (s <= h < e)


# ---------------------------------------------------------------- sources
def sec_items(company, raw_json):
    """Turn EDGAR's submissions JSON into wire items."""
    data = json.loads(raw_json)
    rec = data.get("filings", {}).get("recent", {})
    out = []
    n = len(rec.get("accessionNumber", []))
    cik_int = int(company["cik"])
    for i in range(min(n, 40)):
        acc = rec["accessionNumber"][i]
        form = rec["form"][i]
        items = [x.strip() for x in (rec.get("items", [""] * n)[i] or "").split(",") if x.strip()]
        doc = (rec.get("primaryDocument", [""] * n)[i] or "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc.replace('-', '')}/{doc}" if doc else \
              f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={company['cik']}"
        rule = FORM_RULES.get(form)
        prio, label = (rule if rule else (0, form))
        if form in ("8-K", "8-K/A") and items:
            if any(x in URGENT_ITEMS for x in items):
                prio = 5
            elif any(x in HIGH_ITEMS for x in items):
                prio = 4
            words = [ITEMS.get(x, x) for x in items if x != "9.01"]
            if words:
                label = f"{form}: " + "; ".join(words)
        out.append({
            "id": f"sec:{acc}", "source": "SEC EDGAR", "kind": "filing", "asset": company["ticker"],
            "date": rec["filingDate"][i], "title": f"{company['name']} filed {label}",
            "form": form, "items": items, "url": url, "priority": prio,
            "when": dt.datetime.fromisoformat(rec["filingDate"][i]).replace(tzinfo=dt.timezone.utc),
        })
    return out


def rss_items(raw_xml):
    """Parse RSS 2.0 items: title, link, pubDate, source."""
    root = ET.fromstring(raw_xml)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None and src_el.text else ""
        pub = it.findtext("pubDate") or ""
        try:
            when = email.utils.parsedate_to_datetime(pub)
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            when = None
        if title:
            out.append({"title": title, "link": link, "guid": guid, "source": source, "when": when})
    return out


def news_url(query):
    q = urllib.parse.quote(f"{query} when:2d")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def news_items(subject, raw_xml, cfg, strict=False):
    trusted = {s.lower() for s in cfg.get("trusted_sources", [])}
    out = []
    for r in rss_items(raw_xml):
        headline = re.sub(r"\s+-\s+[^-]+$", "", r["title"]).strip()
        src = r["source"] or (r["title"].rsplit(" - ", 1)[-1] if " - " in r["title"] else "")
        ok_src = src.lower() in trusted
        pattern = METALS_STRICT if strict else STRONG
        strong = bool(pattern.search(headline))
        out.append({
            "id": "news:" + r["guid"][-160:], "source": src or "news", "kind": "news", "asset": subject,
            "date": (r["when"] or now_utc()).date().isoformat(), "title": headline, "url": r["link"],
            "priority": 3 if (ok_src and strong) else 0, "trusted": ok_src, "strong": strong, "when": r["when"],
        })
    return out


def fed_items(raw_xml):
    out = []
    for r in rss_items(raw_xml):
        t = r["title"]
        prio = 5 if re.search(r"FOMC statement|federal funds|rate decision|monetary policy statement", t, re.I) else 4
        out.append({
            "id": "fed:" + r["guid"], "source": "Federal Reserve", "kind": "fed", "asset": "MACRO",
            "date": (r["when"] or now_utc()).date().isoformat(), "title": t, "url": r["link"], "priority": prio,
            "when": r["when"],
        })
    return out


# ---------------------------------------------------------------- core run
def run(cfg, state, fetch, push, ts=None, sec_contact=None, sleep=time.sleep):
    """One pass. Returns (state, log_lines, pushes). fetch(url, ua) -> bytes; push(title, msg, prio, tags, click)."""
    ts = ts or now_utc()
    first = not state.get("seen")
    state.setdefault("seen", {})
    state.setdefault("seeded", [])
    state.setdefault("errors", {})
    state.setdefault("recent_titles", [])
    state.setdefault("news_count", {})
    today = ts.astimezone(ZoneInfo(cfg.get("timezone", "UTC"))).date().isoformat() if ZoneInfo else ts.date().isoformat()
    if state.get("news_count_day") != today:
        state["news_count"], state["news_count_day"] = {}, today

    collected, problems, fetched_ok = [], [], set()

    def guard(name, fn):
        try:
            res = fn()
            if state["errors"].get(name):
                state["errors"][name] = 0
            for r in res:
                r["_src"] = name
            fetched_ok.add(name)
            return res
        except Exception as e:  # keep going if one source fails
            state["errors"][name] = state["errors"].get(name, 0) + 1
            if state["errors"][name] == 12:  # about an hour of failures at 5-minute runs
                problems.append(f"{name}: {type(e).__name__}")
            return []

    if sec_contact:
        for c in cfg["companies"]:
            url = f"https://data.sec.gov/submissions/CIK{c['cik']}.json"
            collected += guard(f"SEC {c['ticker']}", lambda c=c, url=url: sec_items(c, fetch(url, sec_contact)))
            sleep(0.25)  # far under the SEC's 10 requests per second
    elif not state.get("warned_no_sec"):
        problems.append("SEC filings are off: add the SEC_CONTACT secret")
        state["warned_no_sec"] = True

    if cfg.get("fed_feed"):
        collected += guard("Fed", lambda: fed_items(fetch(cfg["fed_feed"], GENERIC_UA)))

    for c in cfg["companies"]:
        collected += guard(f"News {c['ticker']}", lambda c=c: news_items(c["ticker"], fetch(news_url(c["news_query"]), GENERIC_UA), cfg))
    for t in cfg.get("topics", []):
        collected += guard(f"News {t['key']}", lambda t=t: news_items(t["key"], fetch(news_url(t["news_query"]), GENERIC_UA), cfg, strict=t.get("strict")))

    quiet = in_quiet_hours(ts, cfg)
    lim = cfg.get("limits", {})
    per_day = lim.get("news_pushes_per_company_per_day", 4)
    per_run = lim.get("news_pushes_per_company_per_run", 1)
    run_count, log_lines, pushes = {}, [], []
    stamp = ts.isoformat(timespec="seconds")
    recent = [(set(r["w"]), r["t"]) for r in state["recent_titles"]]

    for it in collected:
        if it["id"] in state["seen"]:
            continue
        state["seen"][it["id"]] = stamp
        pushed = False
        reason = ""
        age_h = (ts - it["when"]).total_seconds() / 3600 if it.get("when") else 0
        if first or it["_src"] not in state["seeded"]:
            reason = "first check of this source: marked as already seen"
        elif age_h > 36:
            reason = "older than 36 hours: logged only"
        elif it["priority"] <= 0:
            reason = "logged only"
        elif quiet and it["priority"] < cfg.get("quiet_hours", {}).get("min_priority", 4):
            reason = "quiet hours: logged only"
        else:
            words = norm_words(it["title"])
            if it["kind"] == "news":
                a = it["asset"]
                if any(similar(words, w) for w, _ in recent):
                    reason = "same story already sent"
                elif state["news_count"].get(a, 0) >= per_day or run_count.get(a, 0) >= per_run:
                    reason = "news limit reached: logged only"
            if not reason:
                tag = {"filing": ["page_facing_up"], "fed": ["bank"], "news": ["newspaper"]}[it["kind"]]
                if it["priority"] >= 5:
                    tag = ["rotating_light"] + tag
                push(f"{it['asset']} · {it['source']}", it["title"], it["priority"], tag, it["url"])
                pushed = True
                pushes.append(it)
                if it["kind"] == "news":
                    a = it["asset"]
                    state["news_count"][a] = state["news_count"].get(a, 0) + 1
                    run_count[a] = run_count.get(a, 0) + 1
                recent.append((words, stamp))
        entry = {k: it.get(k) for k in ("id", "date", "kind", "asset", "source", "title", "url", "priority", "form", "items")}
        entry.update(seenAt=stamp, pushed=pushed, note=reason)
        log_lines.append({k: v for k, v in entry.items() if v not in (None, [], "")})

    for src in sorted(fetched_ok):
        if src not in state["seeded"]:
            state["seeded"].append(src)
    if first:
        n = len(cfg["companies"])
        push("gss-wire is live", f"Watching {n} companies, the Fed and gold/silver headlines. "
             f"Filed {len(collected)} existing items as already seen. New ones will ping you here.", 3, ["white_check_mark"], None)
    for p in problems:
        push("gss-wire needs attention", p, 3, ["warning"], None)

    # prune memory
    cutoff = (ts - dt.timedelta(days=SEEN_DAYS)).isoformat(timespec="seconds")
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    tcut = (ts - dt.timedelta(days=3)).isoformat(timespec="seconds")
    state["recent_titles"] = [{"w": sorted(w), "t": t} for w, t in recent if t >= tcut][-300:]
    state["lastRun"] = stamp
    state["lastCounts"] = {"checked": len(collected), "new": len(log_lines), "pushed": len(pushes)}
    return state, log_lines, pushes


def append_log(lines):
    old = []
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, encoding="utf-8") as f:
            old = f.read().splitlines()
    new = old + [json.dumps(l, ensure_ascii=False, sort_keys=True) for l in lines]
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(new[-LOG_KEEP:]) + ("\n" if new else ""))


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC secret is missing; nothing to send to.", file=sys.stderr)
        return 1
    sec_contact = os.environ.get("SEC_CONTACT", "").strip() or None
    cfg = load_json(CONFIG_PATH, None)
    state = load_json(STATE_PATH, {})

    def fetch(url, ua):
        return http_get(url, ua)

    def push(title, msg, prio, tags, click):
        ntfy_push(topic, title, msg, prio, tags, click)

    state, lines, pushes = run(cfg, state, fetch, push, sec_contact=sec_contact)
    append_log(lines)
    save_json(STATE_PATH, state)
    print(json.dumps(state.get("lastCounts", {})), "errors:", {k: v for k, v in state["errors"].items() if v})
    return 0


if __name__ == "__main__":
    sys.exit(main())
