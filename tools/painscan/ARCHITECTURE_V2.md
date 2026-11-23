# painscan v2 — architecture: from "find pain" to "try to prove payment"

Status: DESIGN (2026-11-23). Not built, not run. Nothing in this document has been measured;
every threshold below is fixed here, before data, and may only be changed by a dated amendment
written before the data it applies to.

## סיכום בעברית

* **המטרה היחידה:** למצוא בעיה אמיתית שמייסד יחיד שלא כותב קוד יכול לפתור, **ולנסות להוכיח
  שמשלמים עליה**. הוכחה = התחייבות של לקוחות היעד עצמם (תשלום, מקדמה, הזמנה חתומה עם מחיר), לא
  מסקנה מפורומים.
* **הביקורת על v2 המוצע:** הוא עדיין מנוע חיפוש. כללי העצירה ומדד היעילות שלו סופרים "ממצאים
  בפורומים", כלומר שלב 1 בלבד, ואין בו שום רכיב שמגיע לשלב 5. 400 שאילתות על השערה אחת
  ("השלמה כוזבת") הן השקעה בשאלה שחיפוש לא יכול לענות עליה: v1 כבר הראה ש"כמה זה עולה" ו"האם
  ישלמו" נשארו **לא ידוע** אחרי 856 שאילתות.
* **העיצוב החדש:** משפך של חמישה שלבים לכל מועמד, כשהבדיקה הזולה שהורגת הכי הרבה באה קודם.
  החיפוש מוגבל ל-40 שאילתות למועמד; רוב המאמץ עובר לשיחות עם לקוחות ולבקשת התחייבות עם מחיר.
* **מבחן הביקוש:** 10 שיחות (שאלות על התנהגות בעבר, לא על דעות), ובסוף כל שיחה הצעה לפיילוט ידני
  במחיר קבוע מראש. **GO** = לפחות 3 תשלומים מ-3 ארגונים שונים מתוך עד 15 הצעות, בתוך 4 שבועות.
  **KILL** = 0 תשלומים ופחות מ-3 הזמנות חתומות אחרי 10 הצעות, או פחות מ-5 שיחות מתוך 40 פניות.
* **הנדסה:** PR קטן אחד (מצב שאילתות מקובץ + בדיקת יתרה + תיקון קישורי הפניה). אין קוד ניקוד חדש.
  אין קוד מוצר לפני GO.
* **תקציב:** עד 300 שאילתות Serper, עד 10 דולר ל-LLM, כ-5 שעות בעלים בשבוע. לפחות 27 דולר
  נשארים למוצר של המועמד שיעבור.
* **הצעד הראשון:** סבב 0, בלי שאילתות ובלי כסף: שליפת 8,362 השורות של ריצה 1 מהלוג, תיוג ידני
  מחדש על ידי תת-סוכנים, והפקת 3 כרטיסי מועמד. במקביל הבעלים פותח את 10 השיחות על "השלמה כוזבת".
* **לפני קבלת כסף כלשהו:** לבדוק עם רואה חשבון מעמד עוסק (פטור/מורשה) ודרך גבייה. זו לא עצה משפטית.

---

## 1. Judgement of the proposed v2, from the goal backwards

The goal has two halves: (1) solvable by one non-coding founder, (2) try to prove payment.
v1's own result table says where the uncertainty sits: `who` partial, `money` **unknown**,
`pay` **unknown**, after 856 queries. Search did not fail for lack of volume. Forum text cannot
answer "will this person pay me"; only that person can.

| part of proposed v2 | serves the goal? | verdict |
|---|---|---|
| Breadth via Serper, 20–60 custom queries per round | partly: sharpens *who* and *what fails* | keep, but shrink (section 5) |
| Depth: 2–3 sub-agents read pages, competitor pricing | yes: cheap desk kill-checks | keep, and add an adversarial "kill memo" |
| Stop rule "≥3 new first-hand findings per round" | **no** | counts forum findings; a direction can pass this forever with zero payment evidence. Replaced by stage gates |
| Efficiency metric "queries per first-hand finding" | **no**, as the main metric | optimises the stage that is already cheap. Kept as a secondary instrument metric only |
| Exploration vs confirmation by re-running fixed queries | **no** | a second search over the same public text confirms that people write, not that they pay. Confirmation is the pre-registered demand test |
| Cap 400 queries, first target "false completion" | **no** | the hypothesis already has its search evidence; its open questions (money, pay) are not searchable. Spending 400 queries here repeats lesson 1 in a new form: research instead of the cheap decisive test |
| Evidence table and decision log "live in the chat, written at the end" | **no** | a chat session can end; "fixed before data" needs a commit timestamp. Log is committed per round |
| Stage 5 (proof of willingness to pay) | **missing entirely** | sections 3–4 |
| Reachability of buyers by *this* owner | **missing** | a problem whose buyers he cannot reach in public channels within two weeks is dead for him, however real. Made an early kill-check |
| Minimum price / unit economics | **missing** | "simple fix" for builders who expect free community nodes may have a price ceiling near zero. Made an S3 kill-check |
| Single hypothesis | risk | one candidate has a high prior of dying at S5. Portfolio of up to 3 in desk stages |
| Legal precondition for taking money | **missing** | section 4.6 |

Blunt summary: proposed v2 is a better v1. It would produce a better-documented list of
complaints and still end at "willingness to pay not shown".

My own prior on "false completion" as a business (an opinion, not a measurement): low to moderate.
The failure looks real, but the buyers are builders who already got a free community check, the
platform owner can ship the same rule, and no loss amount was found. That is exactly why it should
go to conversations now instead of receiving more search.

## 2. Design principles

1. **Cheapest decisive test first.** Order stages by (probability of killing) ÷ (cost). A desk
   kill-memo and ten conversations cost USD 0; they come before any build and before most search.
2. **Evidence ladder.** Every piece of evidence is tagged with a level; only L3 is proof.

   | level | what | example |
   |---|---|---|
   | L0 | words, opinions, compliments, forum posts | "I'd pay for that" |
   | L1 | time | a second call booked; shares a workflow export or logs |
   | L2 | reputation | introduces the decision maker or a peer |
   | L3 | money or a binding order | deposit, paid pilot, signed order with price and start date |

   This follows the customer-interview practice in *The Mom Test*: ask about specific past
   behaviour, treat compliments as non-evidence, and end each conversation by asking for a
   commitment of time, reputation or money
   ([summary by S. Rekhi](https://sachinrekhi.com/the-mom-test-rob-fitzpatrick)).
3. **Counts, not rates.** A niche B2B test here will see tens of people, not hundreds. Landing-page
   conversion rates need a minimum of about 200 unique visitors for even a directional read, and
   warm audiences overstate demand
   ([Kromatic, landing page smoke test](https://kromatic.com/real-startup-book/2-evaluative-market-experiments/value-proposition-test/landing-page-smoke-test/)).
   So thresholds are absolute numbers of commitments from independent organisations.
4. **Thresholds before data, committed to git before the test starts** (lesson 3). The same source
   gives the reason in one line: without a threshold set in advance, any result gets rationalised.
5. **No product code before GO.** Tooling: one PR. Product: zero PRs until a candidate passes S5.
6. **A source speaks only for what it measured** (lesson 2). A forum speaks for people who post.
   A model baseline speaks for those models in that prompt setup, not for a customer's n8n flow.

## 3. Per-candidate pipeline

One candidate = one root cause + one buyer segment. Up to 3 candidates may be in S1–S4 at once;
**one** in S5 at a time (owner hours). Each stage ends in PASS / KILL with the reason logged.

| stage | question | work | budget (max) | PASS rule (all required) | KILL rule (any) | artefact |
|---|---|---|---|---|---|---|
| S0 intake | is it a testable hypothesis? | orchestrator writes the card | 0 | names segment, failure, guessed cost, guessed fix | cannot name a segment | `candidates/<id>.md` (card) |
| S1 find and sharpen | do specific people describe it first-hand, recently? | ≤2 Serper rounds; 2 sub-agents read non-reddit pages | 40 queries; 0 USD; 0 owner hours | ≥5 first-hand findings (definition 3.1) from ≥3 independent sources, dated ≤12 months; ≥2 state a loss (money, or hours per occurrence) | <3 first-hand findings after 40 queries | evidence table rows with URL, quote, date, level |
| S1k kill memo | why is this *not* a business? | 1 separate sub-agent, instructed to argue for killing | 0 queries (WebSearch); 0 USD | none of the kill rules holds | a free or bundled fix that the sources call sufficient; platform owner has shipped it; regulated domain (medical, legal, tax, insurance, hiring); **fewer than 2 public channels where the segment gathers and where a research post is allowed** | `kill_memo` section in the card |
| S2 verify the failure | does it happen today? | AI-failure candidates: single-call baseline on the repo's cheap models, deterministic label. Non-AI operational pain: skip the model run; S1's dated first-hand findings stand in, and S5a re-checks | USD 2; 1 working day | failure rate ≥10% on ≥2 of 3 models (n ≥100 items per model) | <5% on all models. 5–10%: one repeat with doubled n, then decide by the same thresholds | baseline note with run id, counts, cost |
| S3 commercial viability | can the numbers work for one person? | desk: competitor prices, substitutes, segment size | 0 | a price P is fixed with P ≥ USD 30/month (or ≥ USD 300 one-off) **and** P ≤ 20% of the stated monthly loss; ≥200 reachable organisations in named channels | P cannot satisfy both bounds; segment under 200 | pricing line + competitor table in the card |
| S4 solo feasibility | can he deliver it? | checklist | 0 | a manual/concierge version deliverable in ≤10 owner hours per customer; an MVP an AI coding agent can build in ≤2 weeks on Flask/Postgres or as a plugin/template; running cost ≤ USD 20/month; no unusual liability | any item fails | checklist in the card |
| S5 demand test | will they commit? | section 4 | 4 weeks; ~5 owner hours/week; 0 USD LLM | section 4.4 GO | section 4.4 KILL | pre-registration, anonymised conversation log, verdict |

Order of execution inside a candidate: S0 → S1k (first, it is free) → S1 → S3 → S4 → S2 and S5a
in parallel → S5b. S2 is deliberately late: a failure baseline costs money and, by lesson 4,
measures the model rather than the customer's setting, so it must not gate free work.

### 3.1 Definitions fixed now

* **First-hand finding:** a statement by an identifiable author (account name, not necessarily a
  real name) describing their own experience of the failure, with enough detail to tell what
  failed. Vendor pages, listicles, news and "people say" do not count. One author = one finding
  regardless of how many posts.
* **Independent sources:** different authors on different sites or threads.
* **Reddit:** pages cannot be fetched. A reddit result counts as first-hand only if the snippet
  itself contains a first-person description; it is tagged `snippet-only` and can supply at most
  2 of the 5 findings S1 needs.
* **Loss:** an amount lost or hours spent per occurrence. A tool's price is not a loss (the v1 bug).
* **Independent organisations (S5):** different companies or freelancers, not colleagues.

### 3.2 Adaptation policy (pre-registered)

After each Serper round the orchestrator may only: (a) re-target queries at the weakest of the
five sub-questions for a live candidate; (b) add sources the last round showed to be first-hand
rich (for example low-star review pages of incumbents, vendor community forums, GitHub issues);
(c) split or merge candidates by root cause. It may not change any PASS/KILL rule, definition or
budget. Each round's queries file is committed before dispatch; each decision gets one log line
with its reason.

## 4. Demand test (S5) — how the architecture reaches proof

### 4.1 Pre-registration per candidate (committed before the first contact)

`candidates/<id>.md` gets a section containing: segment; the offer in two sentences; price P;
channels; the interview script; thresholds 4.4 (copied, not edited); start and end dates
(Israel time).

### 4.2 S5a — conversations (weeks 1–2)

Target 10 conversations of about 15–20 minutes, with at most 40 people asked. Script, in order:

1. When did this last happen to you? What exactly happened? (past behaviour, not opinion)
2. What did it cost: money, hours, a client?
3. What did you do about it? What do you use or pay for today?
4. Why is that not enough?
5. Only then: describe the fix in two sentences and make the S5b ask.

The orchestrator drafts scripts, posts and summaries. The owner holds the conversations; this is
sales work and matches his skills, not a detour.

### 4.3 S5b — the commitment ask (weeks 2–4)

Offer: a concierge pilot (the owner plus AI tools deliver the outcome by hand or with a minimal
script for 2–4 weeks) at price P, with a refundable deposit or first-month payment. Price is shown
in writing. A one-page plain landing page (hosted form tool or a static page; no custom backend)
states the offer, price, pilot start date and refund terms, and is used as the place where
commitments are recorded, **not** as a conversion-rate experiment. At most 15 people receive the
explicit ask.

No paid ads by default: ad spend is outside the project's budget rules, and the reference range of
USD 10–20 per day for 5–7 days ([Kromatic](https://kromatic.com/real-startup-book/2-evaluative-market-experiments/value-proposition-test/landing-page-smoke-test/))
would not reach an interpretable sample for a narrow B2B segment at this scale. An ad test may be
added later only by a dated amendment with its own money cap and threshold.

### 4.4 Thresholds (fixed)

| outcome | rule |
|---|---|
| **KILL: unreachable** | fewer than 5 conversations from 40 asks within 14 days |
| **KILL: no pain** | fewer than 3 of the first 10 describe a specific occurrence in the last 3 months; or ≥6 of 10 say their current or free solution is good enough |
| **Proceed to S5b** | ≥5 of 10 describe an occurrence in the last 3 months, ≥3 state a loss, ≥3 already spent money or built a workaround |
| **GO** | ≥3 L3 payments (deposit or paid pilot at ≥ P) from ≥3 independent organisations, out of ≤15 explicit asks, within 28 days of the first contact |
| **CONDITIONAL GO** | payment cannot yet be collected legally (4.6) and ≥5 signed written orders state price and start date. First action after this verdict is collecting payment; the candidate becomes GO only when ≥3 pay |
| **ONE ITERATION** (once only) | 1–2 payments, or 3–4 signed orders: change one thing (segment *or* price *or* offer), re-register, run 14 more days |
| **KILL: no payment** | 0 payments and <3 signed orders after 10 explicit asks |

Anything between "KILL: no pain" and "Proceed to S5b" after 10 conversations is KILL. Compliments,
sign-ups without price, "send me the link" and waitlist e-mails are L0 and count for nothing.

### 4.5 Sourcing contacts (public channels only)

Allowed:
* A post in a community where the segment gathers and whose rules permit research or feedback
  posts (vendor forums, HN "Ask", industry groups, Hebrew professional groups), asking for a short
  call, stating plainly who he is and that a paid pilot may be offered.
* A public reply in a thread where someone described the problem. Reddit posts and replies are
  made by the owner by hand, since the orchestrator cannot read reddit.
* Individual, hand-written messages through a platform's own messaging to people who posted
  publicly about the problem. No automation, no bulk sending.
* The owner's own network and warm introductions (L2 from earlier conversations). Contacts met
  through his employer are off limits unless the employer agrees.

Not allowed: scraping or compiling lists of e-mail addresses or phone numbers; bought lists;
automated DMs; unsolicited commercial e-mail or SMS (Israeli law restricts advertising messages
sent without prior consent; treat any bulk outreach as prohibited); pretending the product exists;
posing as a customer.

Data handling: the repository stores only an anonymised row per conversation (pseudonymous id,
segment, answers to the five questions, evidence level, date). Names and contact details stay in
the owner's own inbox and are never committed or sent to a model API.

### 4.6 Preconditions for taking money (verify; not legal advice)

Before any deposit is accepted: (a) confirm with an accountant the registration needed to invoice
(עוסק פטור / עוסק מורשה) and how it interacts with his salaried job; (b) choose a collection
route that works from Israel and issues a receipt; (c) written pilot terms: what is delivered,
by when, full refund if not delivered. Until (a)–(b) are done, only the CONDITIONAL GO path exists.
Start (a) in week 1 of the first S5 so it does not block a verdict.

### 4.7 Honesty rules for the test

The page and every conversation say that this is a pilot that starts on a stated date. No fake
"buy" button for a product presented as existing, no countdown timers or artificial scarcity
(these inflate conversion and vanish with a real product, per the Kromatic guidance above).

## 5. Budgets and programme-level stop rules

| resource | cap | split |
|---|---|---|
| Serper queries | **300** of ~1,376 | 10 pilot + 40 × up to 6 candidates + 50 reserve. The remaining ~1,076 stay untouched |
| LLM API (USD) | **10** of ~37 | ≤2 per candidate at S2. At least USD 27 is reserved for the MVP of a GO candidate |
| Owner hours | ~5 per week | S5 only; S0–S4 need about 30 minutes per week of review |
| Sub-agents | not counted in USD | ≤4 per round (2 readers, 1 kill memo, 1 second labeller during the pilot) |
| Calendar | 10 weeks | from the first S5 start |

Programme stop: (a) a GO; or (b) 3 candidates killed at S5; or (c) 6 candidates killed before S5
with the query cap reached; or (d) 10 weeks. Cases (b)–(d) end with a negative report written as
negative, and a decision by the owner on whether the segment choice (online builders vs. Israeli
traditional industry) was the error.

**Two lanes for intake.** Lane A: online SMB/builder problems found by search (fast S5, weeks).
Lane B: operational pain in Israeli construction/logistics/infrastructure, entering only from the
owner's own conversations and network, with zero Serper queries, because search under-counts
people who do not post (v1 limit) and his Hebrew and location are an edge there. Lane B candidates
run the same S1k–S5 gates; S1's "first-hand findings" are then anonymised conversation rows.
Expect longer sales cycles in lane B; the 28-day GO window still applies, with a signed order
(CONDITIONAL GO path) as the realistic first commitment.

## 6. Engineering (one small PR) and reuse

| component | decision | why it serves the goal |
|---|---|---|
| `painscan.py` `serper_search`, `run`, `flatten`, resume via `done.jsonl`, log dump (gzip+base64) | **reuse unchanged** | working transport; already tested |
| New run mode `custom`: reads queries from a committed file `tools/painscan/rounds/round_NN.toml` (text, source, tbs, candidate id) | **add** | configuration in files, commit timestamp = pre-registration of each round |
| Balance check before searching | **add** | call Serper's account endpoint and print the balance; refuse to run if balance < round size + 1,000 reserve. The endpoint and its field names must be confirmed in the 10-query pilot (lesson 5). Fallback if absent: a committed `quota_ledger.jsonl` (queries sent per run), which is kept in any case |
| Resolve `google.com/goto` redirect links to the target URL before de-duplication | **add** (pure string parsing) | 48% of v1 links were redirects; de-duplication was wrong |
| `classify`, `aggregate`, composite ranking, `dossier` verdict rules, `recent_tbs` vs baseline window | **drop for v2** (code stays for reproducibility of run 1; not called in `custom` mode) | measured weaknesses: 66–82% precision, recency ≈ 0.5 everywhere, dossier rule non-discriminating. Sub-agents reading pages replace keyword flags |
| `plan.toml` 30 domains and templates | **frozen**; not extended | v1 record |
| Run-1 data (7,962 + 400 rows in the logs of runs 45083451639 and 45084307292) | **reuse as round 0** | already paid for; see section 8 |
| `binding_gate` ledger (`src/binding_gate/ledger.py`, `configs/budget.toml`) and the baseline workflow | **reuse for S2 only if** a new item set fits the existing runner by config alone; otherwise one standalone script of under ~150 lines with a hard USD cap in the file. No extension of the frozen package | lesson 1: the baseline must be cheaper than the plumbing |
| Landing page | hosted form or static page; **no repo code** | proof needs a place to record a commitment, nothing more |
| Scoring/ranking code, dashboards, databases | **none** | no stage needs them |

Workflow dispatch for a round (after the PR; the orchestrator dispatches, the table documents it):

| field | value |
|---|---|
| Branch | `main` |
| mode | `custom` |
| queries_file | `tools/painscan/rounds/round_01.toml` |
| max_queries | `40` |
| confirm_quota | checked |

Records, all under `tools/painscan/` and committed after every round or S5 week (not at the end):
`rounds/round_NN.toml` (before dispatch), `candidates/<id>.md` (card, kill memo, pre-registration,
verdict), `EVIDENCE.md` (table: candidate, sub-question, URL or conversation id, quote, date,
level), `DECISIONS.md` (one line per decision: time in Israel time, decision, rule applied,
reason), `quota_ledger.jsonl`.

## 7. Instrument validation before scale (10 queries, then stop and look)

1. `custom` mode returns results; redirect links resolve; the balance check prints a number that
   drops by the number of queries sent.
2. Two sub-agents label the same 30 results as first-hand or not, using definition 3.1.
   Agreement ≥85% is required; below that, the definition is rewritten and the check repeated on
   30 new results (already-paid run-1 rows may be used, costing no queries).
3. First-hand yield: if under 20% of non-reddit results in the pilot are first-hand, the query
   templates are rewritten once (source restriction to forums, issue trackers and review pages)
   before any 40-query round.

## 8. Measuring efficiency against v1 and against build-first

All comparisons are descriptive: n is one programme per approach. No significance is claimed.

**Baselines (from the repo's records).**
* Build-first (binding-gate): 26 PRs and USD 12.65 before the idea was closed; a USD 1–2 baseline
  would have closed it; customer-originated evidence: none.
* v1 (painscan run 1): 856 queries; 1 root-cause hypothesis; verified loss amounts: 0;
  payment evidence: 0; candidates with a demand verdict: 0.
* The v1 count of first-hand findings under definition 3.1 is **not known**. Round 0 produces it:
  sub-agents re-label a fixed random sample of 300 run-1 rows (plus all rows of the five
  shortlisted domains) with definition 3.1. Cost: 0 queries, 0 USD.

**Metrics for v2 (reported per candidate and in total).**

| metric | v1 / build-first value | what "better" means |
|---|---|---|
| **Primary:** cost per demand verdict (GO or KILL at S5): Serper queries, USD, owner hours, calendar days | v1: no verdict from 856 queries. Build-first: no verdict from 26 PRs, USD 12.65 | any S5 verdict within ≤40 queries, ≤USD 2, ≤20 owner hours, ≤28 days per candidate |
| L3 commitments obtained | 0 / 0 | count; the only number that is proof |
| Spend before the first customer-originated evidence (first conversation) | never reached | queries, USD and PRs spent before conversation #1 |
| Product PRs merged before a GO | build-first: 26 | 0 |
| Serper queries per first-hand finding (definition 3.1) | from round 0 re-labelling | secondary instrument metric only |
| Share of candidates killed at free stages (S1k, S3, S4) vs at paid or owner-time stages | n/a | higher share killed for free = funnel ordered correctly |
| Kill-memo miss rate: candidates that passed S1k and later died at S5 for a reason the memo should have found (free substitute, platform feature) | n/a | should fall over time; each miss adds one check to the memo prompt by amendment |

A v2 that ends with every candidate killed, cheaply and with reasons, is a better result than v1
or build-first on these metrics, and is reported as a negative result on the goal itself.

## 9. First steps, in order

1. **Round 0 (0 queries, USD 0).** Orchestrator pulls the run-1 rows from the two run logs;
   sub-agents re-label per section 8 and write up to 3 candidate cards: `false_completion`
   (builders on n8n/Make) and the two best others by first-hand loss statements, looking first at
   property management, logistics dispatch and quoting, which ranked 6th–10th in run 1 and sit
   closer to the owner's long-term interest. Kill memos (S1k) for all three.
2. **In the same week, owner time:** commit the S5 pre-registration for `false_completion` with a
   price P from S3, and start S5a (first community post, first hand-written messages). Start the
   accountant question (4.6).
3. **Tooling PR** (custom mode, balance check, redirect resolution), then the 10-query pilot of
   section 7. Only after the pilot passes: 40-query rounds for candidates that survived S1k and
   still lack S1 evidence.
4. S2 baseline for `false_completion` (≤USD 2) only if S5a has not already killed it in week 1–2.

## 10. Known limits (written before data)

* Ten conversations and three payments are small numbers. GO means "enough to justify building a
  minimal product with the remaining ~USD 27", not "a market exists".
* People reachable in public communities differ from the segment as a whole; pilot payers are
  early adopters.
* Thresholds (3 of 15, 28 days, USD 30) are judgement calls fixed in advance, not derived from
  data. Their value is that they cannot be moved after the fact.
* The owner's hours are the scarcest input and the least controllable once his job starts. If a
  week yields under 2 owner hours, the S5 clock pauses for that week (maximum 2 pauses), recorded
  in `DECISIONS.md`.
* Legal and tax statements here are flags to verify with a professional, not advice.

## 11. Amendment 1 (2026-11-23, owner's decision, before any v2 data)

**No LLM API in this architecture.** GitHub Actions runs only Serper searches and deterministic steps
(round files, balance check, log dump). All language-model work — re-labelling results, reading pages,
kill memos, cards, competitor checks — is done by sub-agents spawned in the orchestrating Claude chat.
No paid model call is made, so the USD 10 LLM cap of section 5 is not used.

Consequences:
* **S2 (model baseline) is removed.** For every candidate, S1's dated first-hand findings stand in for
  "does it happen today", and S5a conversations re-check occurrence in the customer's own setting (the
  path section 3 already defined for non-AI operational pain). If a later decision restores S2, it needs
  a new dated amendment and the owner's approval of the spend.
* The remaining USD 37 of the binding-gate budget is not touched by painscan.
* Workflow modes added: `custom` (a committed round file, at most 60 queries) and `balance` (prints the
  Serper balance, spends nothing if the endpoint is free; confirmed in the first run).
