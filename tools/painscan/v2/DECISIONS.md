# DECISIONS (painscan v2)

One line per decision; Israel time; rule applied; reason.

| time (IL) | decision | rule | reason |
|---|---|---|---|
| 2026-11-23 02:11 | Architecture v2 adopted with amendment 1: no LLM API; LLM work by chat sub-agents; S2 removed | owner | owner's instruction |
| 2026-11-23 02:36 | Serper balance endpoint confirmed: 1,376 credits (run 45089409261) | ARCH §6 balance check | instrument check before any v2 search |
| 2026-11-23 02:45 | Round 0 done: 2,894 run-1 rows re-labelled by 4 sub-agents (spec fixed in LABEL_SPEC, snippet-only): 499 first-hand, 35 state a loss, 8 a pay signal | ARCH §9 step 1 | zero-cost reuse; confirms money/pay are rarely searchable |
| 2026-11-23 02:45 | "false completion" alone has 12 first-hand rows in round 0: merged into C2 (n8n silent failures) and C1 (bookings confirmed that never happened) instead of a standalone candidate | ARCH §3.2(c) split/merge by root cause | too thin alone; same root cause appears inside both |
| 2026-11-23 02:45 | Candidates opened: C1 ai_receptionist_audit (26 first-hand, 5 with loss), C2 n8n_silent_failures (33, 2), C3 construction_ai_estimating_check (7, 1; owner-fit, weakest evidence) | ARCH §3 S0 (up to 3 in S1-S4) | strongest clusters with a nameable segment and a deterministic check |
| 2026-11-23 02:55 | C1 KILLED at S1k (rules 1-2 hold: vendor call logs; Cekura/Coval monitoring; owners churn instead of buying) | ARCH §3 S1k | candidates/C1 |
| 2026-11-23 02:58 | C2 KILLED at S1k (rules 1-2 hold: free built-ins and ≥5 free/cheap monitors; no payment signal for false completion) | ARCH §3 S1k | candidates/C2 |
| 2026-11-23 02:58 | C3 survives S1k with low confidence → S1, round 1 = 10-query instrument pilot (ARCH §7) | ARCH §3, §7 | only live candidate; pilot validates the custom-round instrument |
| 2026-11-23 03:08 | Round 1 (pilot, 10 queries, run 45089848774): 91 unique results; 6 first-hand AI-takeoff findings, all reddit snippet-only (S1 counts at most 2) → S1 not passed; losses found are human estimating errors ($18k, post-bid conversion error, underbid) | ARCH §3.1, §3 S1 | strict reddit cap |
| 2026-11-23 03:08 | Instrument pilot verdict: custom rounds, balance check and log transport work; web queries return mostly vendor content; Hebrew queries (gl=us) return guides/law firms only → Israeli lane needs owner conversations, not Serper | ARCH §7 | stop-and-look done; proceed |
| 2026-11-23 03:08 | C3 reframed: "estimate errors (AI or human) caught before the bid", same buyer and same deterministic check | ARCH §3.2(c) | round 1 shows the loss sits in estimating errors generally |
| 2026-11-23 03:08 | Intake note (not a candidate yet): contractors' customers use AI to contest prices/change orders | ARCH §3 S0 | one first-hand report; needs ≥ a segment and a fix before opening |
| 2026-11-23 03:10 | Round 2 for C3 S1: ≤ 20 queries on non-reddit first-hand sources (reviews, ContractorTalk) and on paid estimate-review services (pay signal) | ARCH §3.2(a),(b) | weakest sub-questions: first-hand non-reddit, money, pay |
| 2026-11-23 03:18 | Round 2 (16 queries, run 45089994244): S1 counts pass for reframed C3, but rule 2 now holds (Piper AI Final Bid Review, EstimateHawk, $99 human estimate reviews, offshore takeoff ~$11/h) and S4 fails (non-estimator founder) → C3 KILLED | ARCH §3 S1k rule 2 (new evidence), S4 | candidates/C3 |
| 2026-11-23 03:18 | Programme: 3 killed before S5, 26 of 300 v2 queries used → no programme stop; open C4–C6 (lane A) for kill memos | ARCH §5 | stop needs 6 kills before S5 with the query cap reached, or 3 kills at S5 |
| 2026-11-23 03:24 | C6 KILLED at S1k (rule 2: Double, Dext, Client Hub ship the exception check) | ARCH §3 S1k | candidates/C6 |
| 2026-11-23 03:26 | C4 KILLED at S1k (rules 1–2: free explanation; Jobber/Housecall Pro itemise) | ARCH §3 S1k | candidates/C4 |
| 2026-11-23 03:26 | C5 survives S1k (low confidence) → S1 round 3 (≤ 15 queries): first-hand losses from AI listing errors on non-reddit sources, high-volume sellers with source data, and price signals | ARCH §3 S1, §3.2(a,b) | weakest: money, who (sellers with a source of truth) |
| 2026-11-23 03:34 | Round 3 (12 queries, run 45090333159): 5 non-reddit first-hand findings (eBay Community, Amazon Seller Central); loss stated in 1 (shipping overcharge) → S1 loss count not yet met; pay signal: listing audit services $99–957 one-off | ARCH §3 S1 | evidence rows added |
| 2026-11-23 03:34 | C5 split (ARCH §3.2c): C5b "listing_drift_monitor" — platform-inserted AI changes (eBay Magical Listing, weight/size, country of origin; Amazon AI summaries) checked against the seller's own original listing (source of truth exists) → kill memo before more search | ARCH §3.2(c), §3 order (S1k first) | the reframe removes C5's main risk (no source of truth) |
