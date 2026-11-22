# painscan — pre-registration (fixed 2026-11-23, before any search)

## 1. Question

Which problems with AI agents are (a) **recent**, (b) **costing money**, (c) **asked to be solved**,
(d) **not already crowded**, and (e) **within reach of one founder who does not write code, with a
budget of tens of dollars and AI coding tools** — and can be checked by deterministic rules?

This follows the closed binding-gate study (docs/FINAL_REPORT.md). Its lessons apply here:
measure before building, a source only speaks for what it measured, rules are fixed before data,
and negative results are written as negative results.

## 2. Data

* Serper (Google results) through `SERPER_API_KEY`, free tier. Remaining quota before this study:
  about 2,232 queries (owner's figure). Plan: 6 smoke + 810 main queries = 816; about 1,400 stay in
  reserve for one targeted follow-up pass.
* Sources: reddit (`site:reddit.com`), Hacker News, seven product/automation forums
  (Shopify, n8n, Zapier, QuickBooks, Cursor, OpenAI, Make), and the open web.
  Only search-result titles and snippets are used; no page is fetched.
* Windows: **recent = last 3 months** (`qdr:m3`) and **baseline = last 12 months** (`qdr:y`).
  The smoke run checks that Serper honours `qdr:m3` (dates of results under m3 vs m1 vs y vs none).
  If it does not, the full run uses `qdr:m` (last month) and this is recorded as amendment 1.
* 30 domains × fixed templates (tools/painscan/plan.toml): 660 pain queries (both windows),
  90 demand queries (12 months), 60 competition queries (no date filter).

## 3. Filters fixed before data (plan.toml, per domain, one-line reason each)

| filter | meaning | points |
|---|---|---:|
| source_of_truth | an external reference exists, so a wrong output is catchable by rule | +1 |
| smb_reachable | buyers are small businesses/prosumers a solo founder reaches online | +1 |
| israel_edge | Hebrew / Israeli-market knowledge helps | +0.5 |
| regulated | medical, legal, tax, insurance, hiring law | −2 |
| bigtech_building | a platform owner already ships the check | −1 |

## 4. Scoring fixed before data

Per result: `relevant` = mentions AI/agent/automation **and** at least one pain term;
`cost` = money, hours, refund, lost customer, fine; `demand` = "is there a tool / looking for /
would pay / how do you verify". Per domain (unique links):

* recent pain = relevant results in the 3-month window
* recency share = recent / (12-month ∪ recent)
* cost share = recent results with a cost term / recent pain
* demand = demand-query results that are tool-seeking or relevant
* competitors = distinct vendor hosts in competition queries (social/news/listing sites excluded)

**Composite** = pct-rank(recent pain) + pct-rank(recency share) + pct-rank(cost share)
+ pct-rank(demand) − pct-rank(competitors) + feasibility points.
A domain needs **≥ 10 recent relevant results** to be ranked (evidence floor).

## 5. Decision rule fixed before data

1. The top 5 ranked domains go to a manual read of their recent examples: what exactly fails,
   is it checkable by a rule, who pays, and who already sells a fix (web search).
2. A domain becomes a **candidate** only if the manual read finds a concrete, recurring failure
   that a deterministic rule can catch, and a buyer reachable by the owner. Otherwise it is
   written down as "not a candidate" with the reason.
3. Candidates then follow the existing protocol (5 criteria, ≥ 8/10, no 0; baseline document
   before code; baseline ≤ USD 5).

## 6. Known limits (written before data)

* Search results measure **how much people write**, not how often a failure happens or what it costs.
* Google caps each query at 10 results; volume saturates for big topics, so recency share and
  cost share matter more than raw counts there.
* Keyword relevance is noisy; the manual read of top domains is the check, and its agreement with
  the keyword flag on ≥ 50 sampled results is reported.
* Developers write online far more than business users: business-agent pain is under-counted.
* Feasibility attributes are the analyst's judgement, fixed in advance with reasons, not data.

## 7. Stage 2 — the question engine (fixed before data)

Every shortlisted problem must answer **one question**:

> *Which problem costs a specific group of people enough money that they would pay a solo developer
> for a simple fix, and why do the existing solutions not satisfy them?*

It is split into five sub-questions. For each top domain, 8 extra queries (plan.toml
`dossier_template`, 12-month window except pricing) collect evidence; fixed rules give each
sub-question a status:

| sub-question | evidence rule | answered when |
|---|---|---|
| who (a specific group) | named segment (plumber, dentist, freight broker, Shopify store, …) in results | one segment in ≥ 3 distinct results |
| money (what it costs them) | a $ amount in a relevant or money-query result | ≥ 3 results |
| pay (willingness / current spend) | price per month, "worth paying", "would pay" | ≥ 3 results |
| existing_fail (why current tools fall short) | switched / alternative / still manual / doesn't support / too expensive | ≥ 3 results |
| simple_fix (solo-buildable) | plan attributes: source_of_truth and smb_reachable and not regulated | all three true |

**Verdict:** a problem is a *candidate* only when all five are *answered*; otherwise it is *not yet*,
and each unanswered sub-question is turned into a fixed question for a customer conversation.
Search evidence can make a problem a candidate for conversations; only conversations and a paid
pilot can show willingness to pay (stage 5 of the funnel). The analyst then writes the answer to
the question in one paragraph per candidate, citing the evidence links, and marks what is verified.

## 8. Funnel (adopted 2026-11-23)

1. Find pain (Serper, papers, GitHub issues, forums) → 2. verify the failure (current models,
baseline ≤ USD 5) → 3. commercial viability (defined customer, money lost, competitors,
substitutes, willingness to pay) → 4. solo feasibility (a reliable minimal version without a team,
costly infrastructure or unusual legal liability) → 5. demand test (conversations, small demo,
attempt at a paid pilot) **before** full development. A frequent failure alone (≥ 10%) does not
send an idea to product development; a reachable customer group that already tries to solve the
problem and first proof of willingness to pay are also required.
