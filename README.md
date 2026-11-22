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
The wire opens every new Form 4 for your companies. Open-market purchases (code P) of $10k or more ping you;
$100k+, a CEO/CFO/president/chair buyer, or two different insiders buying within 30 days raise it to high priority.
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
