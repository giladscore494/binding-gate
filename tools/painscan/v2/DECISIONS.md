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
