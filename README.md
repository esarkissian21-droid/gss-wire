# gss-wire

A personal, always-on news wire for a small watchlist. GitHub Actions runs `wire.py` every 20 minutes from 4 a.m. to 7 p.m. Pacific on weekdays and every 2 hours otherwise (about 1,300 of a private repo's 2,000 free minutes a month). Each run checks:

- **SEC EDGAR filings** for each company in `config.json`: 8-K, 6-K, 10-Q, 10-K, 13D, tender offers. Urgent 8-K items (bankruptcy, auditor change, restated financials, impairments, delisting, change in control) ping at top priority; earnings and executive changes at high priority.
- **Federal Reserve monetary-policy press releases** (official RSS feed). Rate decisions ping at top priority.
- **Headlines** from Google News RSS, sent only when they come from a trusted outlet and mention something that can move a stock (earnings, guidance, probes, executive changes, deals and so on). At most 1 headline per company per check and 4 per day, and repeats of the same story are dropped.

Alerts go to your phone through [ntfy](https://ntfy.sh). Between 9 p.m. and 5 a.m. Pacific, only high-priority alerts ring; everything else is logged.

Every item seen is appended to `wire_log.jsonl`, which Claude's daily analyzer run reads.

## Secrets (Settings → Secrets and variables → Actions)

| Name | Value |
|---|---|
| `NTFY_TOPIC` | your private ntfy channel name |
| `SEC_CONTACT` | your name and email, e.g. `Jane Doe jane@example.com`. The SEC asks automated tools to identify themselves. |

## Insider buys
The wire opens every new Form 4 for your companies. Open-market purchases (code P) of $10k or more ping you
at normal priority. Clusters (several insiders within 30 days) are noted in the alert but not escalated: in a
backtest of every Form 4 from 2006 to 2025 (research/insider_backtest.py) they did no better than single buys.
Pre-planned 10b5-1 buys are marked as less informative. Sales, grants and option exercises are logged only.
TSM, PDD and AEM are foreign filers and don't file Form 4s.

## Weekly digest and the blind forward test
`digest.py` runs Sunday ~5 p.m. PT (workflow "Weekly digest"): ratio and zone, your silver bar in dollars and gold
ounces, watchlist moves, the week's wire alerts, coming events (`calendar` in config.json) and the forward-test score.

`forward/rules_frozen.json` fixes the rules as of Nov 22 2026. From the week ending Nov 27 2026 the digest applies
them mechanically and appends to `forward/ledger.jsonl`, filling outcomes at 13/26/52 weeks. Never edit the frozen
file or the ledger by hand: git history is the proof that nothing was changed after the fact. To try new rules, add a
v2 file with its own start week and keep v1 running.

## Changing the watchlist

Edit `config.json`: add a company with its ticker, name, 10-digit SEC CIK and a news search. A new company's existing filings and headlines are marked as already seen, so adding one doesn't flood your phone.

## Turning it off

Actions tab → **wire** → **⋯** → **Disable workflow**.

## Market-hours watch (added Nov 22 2026)

Runs inside every wire pass (every 20 minutes on weekdays) while the US market is open. It follows your
companies, the shortlist (`config.json` → `shortlist`) and the Grade A list (`history/screens/grade_a.json`,
rebuilt each Sunday by the screens job). A stock triggers an alert on a move of 3%, 6%, 9%..., volume at 2× a
normal day's pace, or (shortlist/Grade A) a cross of its 200-day average. Each alert gives what happened, why
(filings and trusted headlines from the last day), the odds from `research/move_odds.json`, and what your rules
say. Every alert with odds is logged in `state.json` → `watch.preds` and scored ~3 months later.
Turn it off with `"watch": {"enabled": false}` in config.json.

Grade A rules (frozen, v1-2026-11-22): passes Quality; P/E 0-25; debt/equity known and under 0.5; price above
its 200-day average and up over 52 weeks, but up no more than 60%; no one-off-profit flags; revenue at least $500M.
