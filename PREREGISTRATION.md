# Pre-registration: binding-gate

**Status:** DRAFT

**Document version:** 0.3 (2026-11-22, PR 11)

**Changelog.** 0.3 (2026-11-22, PR 11): open items consolidated into one
checklist (section 17); split-unit wording of section 4 per layer; layer-3
cluster unit in section 8 corrected to the question; state paragraph updated. No
PROPOSED value and no rule changed. 0.2 (2026-11-22, PR 2): layer-1 facts from
the real files; working model selection.

This document fixes, before any model is run, what will be measured, how, and
what counts as success or failure. While the status is DRAFT everything in it
may still change and nothing in it has been approved. It becomes binding when the
owner approves it, the status line is changed to `FROZEN`, and that commit is
tagged `prereg-freeze-<n>`. From then on changes are made only through the
amendment log (section 18).

**State of the project at version 0.3 (PR 11):** all ten planned pull requests
are merged; no model has been called and no result of any kind exists. The
Deceptive Grounding benchmark has been cloned at a pinned commit and its files
inspected for structure (section 3); the structural statistics quoted there were
computed over all 264 triple files, because the split is applied by code that
reads every file, but passages were read by a person or coding agent only for
dev triples, and no test-split item has been shown to a model. GRAB-RAG's files
were inspected in PR 7 and SealQA's metadata (not its rows) in PR 8 (section 3).
The model clients, the web search client and the page fetcher have been tested
against recorded-shape fixtures only; their behaviour against the live services
is **unverified** until the smoke runs. Where a detail has not been checked
against the real files or services it is marked **unverified**.

Every number marked **PROPOSED** is a starting proposal that
**requires owner approval before freeze**.

---

## 1. The failure being studied

An LLM agent answers a question from retrieved sources. It reaches a real,
on-topic source and extracts a detail that truly appears there, but the detail
belongs to a different entity, or to different conditions (time, place, version,
unit), than the question asked about. The answer is "grounded" and wrong. The
Deceptive Grounding paper (arXiv 2607.09349) calls the entity case
*entity-attribution failure*.

Checks of the form "is this claim supported by this source?" pass such answers,
because the claim is supported; it is about the wrong thing. The binding gate
asks a different question: *is the quoted evidence about the same entity and
qualifiers as the question?*

The gate:

- **(A)** decomposes the question into constraints: entity, requested attribute,
  qualifiers (time, place, version, unit, other) and presuppositions;
- **(B)** forces every answer into a structure: value + verbatim quote + source
  id, or an explicit abstention;
- **(C)** runs deterministic binding checks: the quote exists verbatim in the
  cited document (after the normalisation of section 6.1); the question's entity
  appears within a window around the quote; each qualifier appears within the
  window; no competing entity is closer to the value than the question's entity;
- **(D)** checks premises: what the question presupposes must be present in the
  evidence;
- **(E)** returns a verdict: ACCEPT, RESEARCH (search again, with a reason) or
  ABSTAIN (with a reason).

Only (A) uses a model. (C), the matching in (D), and (E) are deterministic.

## 2. Hypotheses

- **H1 (effect).** The gate reduces wrong-entity answers compared with the same
  model answering without a gate.
- **H2 (cost).** It does so at a bounded loss of correct answers.
- **H3 (novelty).** On this failure it performs better than an existing
  open-source claim-versus-source faithfulness checker.
- **H4 (live web).** With live web search and a re-search loop, the net number of
  correct answers does not drop compared with no gate.

Thresholds that decide whether each hypothesis is supported are in section 11.

## 3. Datasets and layers

| Layer | Dataset | Role | Licence | Status of our knowledge |
|---|---|---|---|---|
| 1. Controlled | Deceptive Grounding benchmark (github.com/CedricCaruzzo/deceptive-grounding-benchmark; arXiv 2607.09349), pinned at commit `61b0281d8d3897f53bef3f7402727c905614c1e4` | Confirmatory for H1-H3 | Apache-2.0 (LICENSE file in the checkout) | Files inspected in PR 2; facts below |
| 2. Controlled, other domain | GRAB-RAG (github.com/andre44s/grab-rag; arXiv 2608.22228), pinned at commit `2006561476b39a6cd1ebc0ab5df1b09af01dbcab`; misleading condition, with the supportive condition as paired control | Exploratory (secondary); see below for what it can and cannot test | Code MIT; data CC BY-SA 3.0 (NQ-derived) and CC BY-SA 4.0 (HotpotQA-derived) | Files inspected in PR 7; facts below. The misleading contexts are **not shipped** and must be exported once with GRAB-RAG's code |
| 3. Live web | SealQA (Hugging Face `vtllms/sealqa`; arXiv 2506.01062), pinned at revision `0e75db526ad93dd039714897b7631afc03510e30`; subset `seal_hard` (which contains `seal_0`) | Confirmatory for H4 | Apache-2.0 (dataset card and `LICENSE` file at the pinned revision) | Revision, file hashes and column names checked against the Hub API in PR 8; the rows could not be downloaded in the PR 8 sandbox, so counts and values are **unverified** (facts below) |

**Layer 1.** Each stimulus is built from a triple: queried drug X, competitor
drug Y, disease C. The benchmark has 264 triples × 15 conditions = 3,960 stimuli.
The 15 conditions cross X-document completeness {absent, partial, complete} with
a competing-document condition {null_control, class_proximate, context_adjacent,
prior_completing, synthetic_y}. The benchmark provides no train/dev/test split.

*Facts established in PR 2 from the real files* (pinned commit
`61b0281d8d3897f53bef3f7402727c905614c1e4`, dated 2026-10-15, "Release synthetic
Deceptive Grounding benchmark under Apache-2.0"; SHA-256 manifest of
`data/constructed_documents/*.json`:
`1feb80b85717ada1b48114b2dc1922ad5cfa694f4444888669ebc716f8653123`, checked by
the adapter on every load):

1. **File format and field names.** `data/constructed_documents/` holds 264
   files `<triple_id>.json` plus `index.json`. Every triple file has exactly the
   keys `triple_id`, `X`, `Y`, `Y_synthetic`, `C`, `subdomain`,
   `mechanism_class`, `prior_strength_class`, `triple_risk_score`,
   `completing_information_targets`, `validation_anchor`, `generated`, `cx`,
   `cy`, `conditions`. `cx` maps `absent` (always `null`), `partial`, `complete`
   to the X-document text; `cy` maps the five competing conditions to the
   competing-document text; `conditions` holds the 15 combinations
   `"<cx_level>__<cy_level>"`, each `{cx, cy, cx_level, cy_level}`, and agrees
   with `cx`/`cy` in all 3,960 cases. Documents are typically 150–250 words
   (the construction prompt asks for 150–200).
2. **The question** is the same template for all 15 conditions of a triple:
   "What does the literature say about using {X} to treat {C}?"
   (`benchmark_runner.py`). It is open-ended; it does not ask for a single value.
3. **What is shown in `absent`:** no X document at all. The model sees one
   document, the competing one, labelled `[Document 1 — source: cy_doc]`. A
   target-grounded answer is therefore impossible in the five `absent`
   conditions; the supported behaviour there is abstention.
4. **What is shown in `null_control`:** a competing document is still shown. It
   is about Y "in an unrelated context", a disease fixed per subdomain by
   `document_constructor.NULL_CONTROL_CONTEXTS`, written under the instruction
   not to mention X or C. `null_control` is thus "an off-topic document about
   another drug", not "no competing document".
5. **The other competing conditions** (`document_constructor.py`):
   `class_proximate` is about a different drug of X's mechanism class (neither X
   nor Y; its name is not recorded) in an unrelated indication;
   `context_adjacent` is about Y in C without the completing information;
   `prior_completing` attributes the completing information to Y in C;
   `synthetic_y` attributes it to the invented name `Y_synthetic` in C.
   In every condition the competing document has role `competitor` and the X
   document, when present, has role `target`.
6. **Presentation.** We reproduce the benchmark's `doc_injection` prompt mode
   (`--no-tool-calling`): its `AGENT_SYSTEM_PROMPT` verbatim as the system
   message, and the user message "Retrieved literature documents:", the
   documents as `[Document n — source: cx_doc|cy_doc]` blocks (X document
   first), then "Based on the above documents, " + the question. Our only
   addition is the answer-format instruction of section 6.2 after the question.
   The benchmark's own caveat applies: `doc_injection` results are not directly
   comparable with its default tool-call mode.
7. **Gold answer values do not exist.** No field holds an expected answer.
   `completing_information_targets` is a list of 0–8 short claim strings (the
   information withheld from the `partial` X document and attributed to the
   competitor in `prior_completing` / `synthetic_y`), written as paraphrasable
   phrases such as "no large randomized controlled trials"; it is construction
   metadata, not an answer key, and the question is open-ended. The benchmark's
   own scoring is an LLM judge. Consequence: see section 6.3.
8. **250 versus 264 triples.** `data/triple_pool.json` is an object with the
   keys `generated`, `gate_status`, `triples` (a list of 250) and `summary`.
   All 250 pool triples have a constructed file; 14 constructed triples are not
   in the pool. The benchmark's README states that the constructed corpus
   "includes 14 additional pilot triples" from a different snapshot. The unit of
   this study is the constructed corpus: 264 triples. `triple_pool.json` is not
   used.
9. **Realised split** (section 4; the unit is the (X, C) pair). No pair is on
   both sides (checked over all 131 pairs).

   | | (X, C) pairs | Triples | Stimuli | Stimuli outside `null_control` |
   |---|---|---|---|---|
   | dev | 45 | 97 | 1,455 | 1,164 |
   | test | 86 | 167 | 2,505 | 2,004 |

   That is 34.4% / 65.6% of pairs (36.7% / 63.3% of triples) instead of the
   expected 40% / 60%.
10. **Irregularities in the corpus, kept as they are.** Seven documents consist
    of the literal text `...` (failed generations: three competing documents in
    dev triples; in test triples three competing documents and one `partial` X
    document). Four triples have an empty `completing_information_targets` list
    (one in dev). Items built from them are never dropped; the adapter flags
    documents shorter than 20 words in the gold metadata.
11. **Triples are not independent of each other.** The 264 triples cover only
    131 distinct (X, C) pairs; 77 pairs have two to five triples that differ in
    Y. Triples of one pair share the question and the completing-information
    targets, while their documents were generated separately and differ. A split
    by triple would have put 46 pairs (133 triples) on both sides of the dev/test
    split. The unit of the split and of the bootstrap is therefore the (X, C)
    pair (section 4).

**Layer 2.** GRAB-RAG presents the same questions (500 from Natural Questions,
500 from HotpotQA) under four context conditions: supportive, degraded, missing
and misleading. We use the misleading condition and, as its paired control, the
supportive condition.

*Facts established in PR 7 from the real files* (pinned commit
`2006561476b39a6cd1ebc0ab5df1b09af01dbcab`, dated 2026-11-13, "update readme";
SHA-256 manifest of `data/*/eval.jsonl`:
`d11d48acb7d30cd7033db39b909d8d2d5c0af1c925191ae9e7bc25c7549d14b0`, checked by
the adapter `src/binding_gate/datasets/grab_rag.py` on every load). Structural
counts were computed over all 1,000 records, because the split is applied by
code that reads every record. Disclosure: before the split was computed, the
first 1,500 bytes of each file were printed once to learn the format; this
showed the coding agent the first NQ record and part of the second (both dev)
and the first HotpotQA record and part of the second (both **test**). No model
has seen any record.

1. **Licences.** Code: MIT (`LICENSE`, "Copyright (c) 2026 Yohanes Andre
   Setiawan"). Data, as stated in the checkout's `README.md` ("NQ-derived data
   stays CC BY-SA 3.0; HotpotQA-derived stays CC BY-SA 4.0"): HotpotQA's
   official site (hotpotqa.github.io) and its Hugging Face card state CC BY-SA
   4.0; the Natural Questions card on Hugging Face (organisation
   `google-research-datasets`) states CC BY-SA 3.0 Unported. Google's own NQ
   pages could not be read (rendered by script), so the NQ data licence is
   confirmed by secondary sources only. Consequence: no record is
   redistributed (ARCHITECTURE.md section 9); test fixtures are invented text;
   any excerpt quoted in a report carries CC BY-SA attribution.
2. **File format and field names.** `data/natural_questions/eval.jsonl` and
   `data/hotpotqa/eval.jsonl`, 500 lines each, sorted by `id`, every line exactly
   the keys `id`, `question`, `answers`, `gold_passages`, `dataset`. NQ: one gold
   passage (`{title, text}`, a long-answer paragraph) and 1-10 answer aliases
   (225 records have one); HotpotQA: two gold passages (the supporting
   paragraphs) and exactly one answer (yes/no questions were excluded by
   GRAB-RAG). No passage exceeds 2,000 characters. No question text occurs
   twice.
3. **Gold answers exist.** `answers` holds the NQ short answers and the HotpotQA
   answer. Correctness can therefore be decided against gold values (section 6.3
   note below), unlike layer 1.
4. **The conditions are not shipped; they are built at run time** by
   `build_condition` in `src/context_quality.py`, called from `src/runner.py`
   (five passages each): supportive = `Q100`, the gold passages repeated to fill
   five slots, shuffled; degraded = `Q50`, three gold slots and two hard
   negatives; missing = `Q0`, five hard negatives; misleading = `QC`, one edited
   gold passage and four hard negatives, shuffled; `Qclosed` (no passages) is a
   reference condition. (Names confirmed against the paper's HTML version.) Hard
   negatives are the top passages of a hybrid BM25 + BGE retriever over all gold
   passages of the same file that are not gold for the question and contain no
   answer alias.
5. **What "misleading" is.** `ContradictoryGenerator` takes the first gold
   passage in which the answer span can be swapped and replaces the answer
   (every whole-word occurrence, plus overlapping aliases, and in the title) by:
   a year shifted by ±10/20/30, a number shifted by 10-30 %, or another named
   entity of the same GLiNER type (person, organisation, place, nationality,
   event, work of art) drawn from entities found in the gold passages; the edit
   is kept only if no gold answer remains. The other gold passage of a HotpotQA
   question is not shown. Questions where no swap succeeds get no misleading
   context. The paper reports 532 such questions (202 NQ, 330 HotpotQA) and a
   one-author audit of 200 swaps: 59% fluent, 28% borderline, 13% broken
   (organisation swaps 41% broken). These figures are *reported*, not checked by
   us. Among the 394 dev questions, the first gold answer is a year, contains a
   year, or is a number in 44 cases (NQ 23, HotpotQA 21); the rest are names or
   phrases, which get an entity swap if they are eligible at all.
6. **How the edited passage is marked.** It is not labelled. In
   `build_condition`'s output it is the only passage without the retriever's
   `pid` and `score` keys. The planted wrong answer is not recorded either; the
   adapter recovers it from a token diff between the gold passage and its edit
   (all inserted spans must be one and the same string; otherwise it is recorded
   as not derivable and the item is kept).
7. **Why the misleading contexts must be exported.** Building them needs the
   GLiNER and BGE models (and a GPU in GRAB-RAG's code), and GRAB-RAG's runner
   writes only model outputs, never the passages it showed. The adapter
   therefore reads a *context export*: one JSON line per question,
   `{dataset, question_id, condition: "QC", seed: 42, passages}`, where
   `passages` is exactly `build_condition`'s return value (null when GRAB-RAG
   skips the question), produced by the calls `src/runner.py` makes, with the
   passages written out (`EXPORT_PROCEDURE` in the adapter). It does not exist
   yet. Its SHA-256 is pinned in the run config when it does; until then the
   layer-2 configs stop with an explanation. Retrieval and NER on a GPU may not
   be bit-identical across machines, so the export, not the procedure, is the
   pinned stimulus. The supportive condition needs no model and is built by the
   adapter from `eval.jsonl` exactly as `Q100` (`random.Random(42 + qi)`; the
   permutation is pinned by a test on Python 3.11 and 3.13).
8. **Realised split** (section 4; the unit is the question, group id
   `<source dataset>/<question id>`). Dev 191 NQ + 203 HotpotQA = 394 questions
   (39.4%); test 309 + 297 = 606. How many dev questions have a misleading context
   is known only once the export exists (about 210 if the reported 53.2% holds).
   Two NQ and seven HotpotQA gold passages serve more than one question; one and
   two of them, respectively, serve questions on both sides of the split.
9. **Presentation.** GRAB-RAG's system message (`sys_base`, verbatim) and its
   `P0` user layout ("Context:", the passages, "Question: ..."), each passage
   rendered as GRAB-RAG renders it (`<title>: <text>`) under our header
   `[Document n — source: docN]` (neutral ids by position), and our answer-format
   instruction (section 6.2) instead of GRAB-RAG's JSON instruction.
   Deviations from GRAB-RAG: no truncation of the context to 1,500 tokens;
   document headers; our answer format; hosted frontier models (section 9)
   instead of small local ones. Our numbers are not comparable with the paper's.
10. **Document roles and scoring.** Misleading items: the edited passage is
    `competitor`, the hard negatives are `other`; supportive items: every passage
    is `target`. The `provenance` scorer is run unchanged, so for layer 2 its
    `competitor_only` / "wrong-entity" label means only "the quote comes from the
    edited passage". A second deterministic scorer, `grab_rag_answer`, decides
    whether the answer's value carries a gold answer, the planted answer, both or
    neither (labels `correct`, `misled`, `ambiguous`, `other_wrong`, plus
    `abstained` and `unparseable` with the same minimum quote length): after the
    section 6.1 normalisation, GRAB-RAG's own answer normalisation (lower case,
    punctuation and articles removed, `1,000` → `1000`, `1st` → `first`) and
    whole-word containment, which is GRAB-RAG's `contains_answer` rule applied to
    the value. Containment instead of exact match because our value is one or two
    sentences. **PROPOSED — requires owner approval before freeze.**

*What layer 2 can and cannot test.* The edit replaces the **answer**, not the
entity the question is about: the question's subject stays in the passage, which
now asserts a wrong value for it (or, when the answer is the passage's own
subject, a wrong name). The paper describes it as answer-span substitution
adapted from entity-swap knowledge-conflict methods. This is **not** the
entity-attribution failure of section 1: a quote from the edited passage is
textually about the right entity and the right qualifiers. The gate does not
judge truth (ARCHITECTURE.md section 9), so its binding checks are not expected
to block misled answers here, except by accident (for example when the planted
name sits closer to the value than the question's entity). Layer 2 therefore
tests a broader misleading-evidence failure, not the one the gate targets.
Proposed interpretation (**PROPOSED — requires owner approval before freeze.**):

- Misleading items are a **scope boundary**, not evidence for H1. Layer-2 M1 is
  misled answers / answered records (strict variant: misled with the quote taken
  from the edited passage, reported alongside). A reduction by the gate is
  reported, with the reasons the gate gave, but is not read as support for the
  gate's mechanism; no reduction is the expected result and is not evidence
  against H1.
- Supportive items measure the **cost** side on natural questions: layer-2 M2 is,
  among supportive records where `no_gate` gave a `correct` answer, the share
  where the compared arm did not. This is the part of layer 2 that bears on
  generalisation (of H2).
- Only questions with a misleading context get a supportive item, so the two
  conditions are paired by question.
- Layer 2 stays exploratory and has no pass/fail threshold (section 11).

For context only, the paper reports that in the misleading condition small
models still answered 41.6% of the time under explicit abstention prompting,
that most of those answers repeated the planted wrong entity, and that a
generator-side conflict check cut answering to 13.3% while discarding many
correct answers. These figures are not a baseline for this study.

**Layer 3.** *Facts established in PR 8 (2026-11-22)* from the Hugging Face
Hub API (`/api/datasets/vtllms/sealqa`, `.../tree/main`), the Hub's Croissant
metadata (`/api/datasets/vtllms/sealqa/croissant`), the dataset card and the
paper (arXiv 2506.01062, HTML version). Adapter:
`src/binding_gate/datasets/sealqa.py`.

1. **Revision and files.** The pinned revision is the dataset's `main` on
   2026-11-22 (last modified 2026-11-04). Files and SHA-256 (the LFS object ids
   reported by the Hub), checked on every download and load:
   `seal-hard.parquet` (49,966 bytes, `b13fbacabac4b055dcabfae161df4dc661e31e915507324790779d107ab53e7e`),
   `seal-0.parquet` (27,169 bytes, `89c729bd0a13c56847bb30cbdce1e091f7b832bbe9929d3744357cedcf4e33be`),
   `LICENSE` (Apache-2.0; 11,357 bytes) and `README.md` (both downloaded and
   hashed). `longseal.parquet` (73 MB) is not used and not downloaded. The card
   says the answers are reviewed and updated over time ("Next update: Dec 9
   2026"); the gold answers of this study are those of the pinned revision.
2. **Fields** (Croissant metadata): `question`, `answer`, `urls` (list),
   `freshness`, `question_types` (list), `effective_year`, `search_results`,
   `topic`, `canary`; `longseal` adds the supplied documents (`golds`,
   `12_docs`, `20_docs`, `30_docs`). There is no row id and no alias list. Per
   the paper: `freshness` is never / slow / fast changing; `search_results` is
   the expected kind of search results, "conflict" or "unhelpful";
   `effective_year` is the year the answer last changed; Seal-Hard "includes
   Seal-0"; the authors grade with a GPT-4o-mini auto-rater (SimpleQA-style
   template; CORRECT / INCORRECT / NOT_ATTEMPTED; 98% agreement with human
   ratings on 100 answers, *reported*).
3. **Unverified.** The parquet files could not be downloaded in the PR 8
   sandbox (the Hub's file CDN was blocked by the sandbox's network proxy), so
   the row counts (reported: seal_0 111, seal_hard 254, longseal 254), the
   value formats and the subset relation are taken from the card and the
   paper. The adapter refuses data that contradicts what it relies on (missing
   question or answer, a Seal-0 question absent from Seal-Hard, one question
   with two different answers). `python -m binding_gate.cli dataset-prepare`
   records the real counts and columns in `data/raw/sealqa/conversion.json`;
   they are to be entered here, from dev-split counts only, before the freeze.
4. **Subset (decision).** The items are the `seal_hard` questions; `seal_0` is
   read only to label its questions (stratum `seal_0/<freshness>` versus
   `seal_hard/<freshness>`), so Seal-0 can be reported separately without
   counting a question twice. **LongSeal is not part of layer 3**: it supplies
   the documents with each question (one gold page among 12-30 hard
   negatives), so nothing is searched, and its questions are the Seal-Hard
   questions again; it would be an offline long-context condition, not live-web
   evidence.
5. **Item mapping.** `group_id` = item id = `sealqa/` + the first 16 hex digits
   of SHA-256 of the question after the normalisation of section 6.1. SealQA
   has no row id, so "the row" of section 4 is identified by its question.
   `gold` holds the answer, the metadata of item 2 and the annotators' source
   URLs (which reveal the answer, so they never reach an arm); `documents` is
   empty (evidence is fetched at run time). Expected dev size: about 40% of 254,
   i.e. about 102 questions (a synthetic stand-in of the same size gave 114;
   the realised count is known after preparation).
6. **Search API (decision, by explicit criteria; official pages read on
   2026-11-22).** Criteria, in order: terms permitting programmatic use,
   storing results for research and use in evaluating AI systems; availability;
   price per query; documented response and errors; a free preflight; result
   quality; rate limits. *Brave Search API*: $5 per 1,000 requests, but its
   terms (section 3(b)) forbid storing results beyond transient use and using
   them to "create, evaluate, train ... benchmark" AI models (storage rights only
   with a custom plan) - excluded. *Tavily*: its API agreement forbids storing
   the output and using it in "machine learning or algorithmic activities" -
   excluded. *Exa* ($4-7 per 1,000): its general terms could not be found
   (only the Exa Connect marketplace terms); a third-party summary reports a
   ban on persistent caching - excluded as unverifiable. *Google Custom Search
   JSON API*: reported by several third-party sources to be closed to new
   customers and to shut down on 2027-01-01 - excluded. *SerpApi* (Google
   results): no clause against storing results, research or AI evaluation in
   its terms (serpapi.com/legal; reselling the service is forbidden); a free
   Account API usable as preflight ("not counted toward your monthly quota");
   documented response, status codes and empty-result handling; Free plan 250
   searches/month (50/hour), Starter $25 for 1,000/month (200/hour), "only
   successful searches are counted". *Serper* (runner-up): no storage
   restriction in its terms and about $1 per 1,000 queries (third-party price
   summary), but less formal documentation and no documented free account
   endpoint. **Chosen: SerpApi, Google engine** (`src/binding_gate/research/search.py`),
   charged in the budget at $0.025 per search (`configs/prices.toml`;
   conservative: SerpApi does not bill cached or failed searches). Note that
   SerpApi assumes the legal risk of collecting Google's results; Google's own
   terms are not a party to this study.
7. **Agent protocol** (`src/binding_gate/arms/gated_research.py`). The harness,
   not the model, chooses every query. First attempt: the query is the question
   (whitespace collapsed); SerpApi `engine=google`, `hl=en`, `gl=us`; at most 10
   organic results are kept in rank order; their pages are fetched in rank
   order until 5 pages were fetched successfully; each page's stored extracted
   text, cut to its first 8,000 characters, is shown as document `web1` ...
   `web5` with its URL and the search date (presentation `sealqa_live_web`; a
   system message written for this study that names no entity and gives no
   hint about misleading pages). The live `no_gate` arm stops there. The first
   attempt is shared by both arms, all models and all repeats of a run
   (searches and pages are cached per run and reloaded on resume), so within a
   run `gated_research` judges exactly the candidate `no_gate` got.
   `gated_research` then applies the decomposer and the gate exactly as `gated`
   (section 5.1); on RESEARCH it issues ONE targeted re-search with the query
   `"<entity>" <qualifier 1> ... <qualifier n> <attribute>` built from the
   verdict's research request (template version 1, double quotes and
   case-insensitive duplicates removed), shows the new pages only (URLs used
   before for the item are skipped; the prompt does not mention the earlier
   attempt) and judges the new candidate against the same constraints. A
   RESEARCH verdict at the last allowed attempt, a failed re-search or one
   without a new page ends in an abstention. Page fetching
   (`src/binding_gate/research/fetch.py`): one GET per URL with a fixed user
   agent naming the project; robots.txt respected (RFC 9309: longest match;
   robots.txt 4xx means allowed, 5xx or unreachable means disallowed); HTML,
   XHTML and plain text only; extraction with the standard library's
   `html.parser` (fixed rule, version 1); every failure recorded with its reason
   and never retried. Parameters: `max_attempts = 2`, 10 results, 5 pages,
   8,000 characters per page, fetch limits 2,000,000 bytes, 20 s, 5 redirects,
   1 s between requests to one host, at most one search per 18 s (the Starter
   plan's 200 per hour). **PROPOSED — requires owner approval before freeze.**
8. **Cost** (dry run of `configs/dev_live.toml` on a synthetic stand-in of
   254 questions, 114 of them dev; nothing spent): per model, 342 first-attempt
   answer calls with about 3.55M input tokens (5 pages of 8,000 characters are
   the worst case and dominate the prompt); searches 114 (low) to 456 (high,
   a re-search for every item and repeat), $2.85-$11.40 per run; fetches are
   free; answer calls $0.40 (low) to $14.80 (high) per model depending on the
   tier, plus up to the same again for re-search answers. Totals per model job
   including searches: $3.25-$13.02 (low tier) to $10.81-$41.14 (mid tier);
   caps $14-$42 per model, $165 in all. The smoke config (5 dev questions, slot
   A, 1 repeat) is estimated at $0.13-$0.28 with a $1 cap.
9. **Limitations specific to layer 3.** The queries are chosen by the harness,
   so this tests a gate on a fixed search-and-read pipeline, not on an agent
   that plans its own searches. Gold answers are those of the pinned revision
   while the web is read months later; for fast-changing questions the web may
   have moved on (results are reported by `freshness`). Pages that forbid us in
   robots.txt, that are not HTML or plain text (PDFs), or whose relevant part
   lies beyond the first 8,000 characters are never seen. Search results depend
   on the day, the engine's ranking and the `hl`/`gl` settings, so layer 3 is
   reproducible from snapshots (section 14) but not re-runnable.

Not part of this study's reported data: the Stanford news-intermediary benchmark
(arXiv 2605.22785) ships outputs without a licence file. It may be looked at
internally; it is never redistributed and no result on it is a confirmatory
result here.

## 4. Dev/test split

One rule for all datasets, implemented in `src/binding_gate/split.py`:

```
SPLIT_SALT   = "binding-gate/split/v1"
BUCKETS      = 10000
TEST_BUCKETS = 6000
bucket = int(first 8 bytes of SHA-256(SPLIT_SALT + "\n" + dataset + "\n" + group_id), big-endian) mod BUCKETS
split  = test if bucket < TEST_BUCKETS else dev
```

- `group_id` is the unit of independence. For Deceptive Grounding it is the
  **(X, C) pair** (queried drug, disease), so all conditions of all triples
  that share a question are always in the same split. Reason: the 264 triples
  cover only 131 pairs, and triples of one pair differ only in the competitor Y
  while sharing the question and the completing-information targets; with the
  triple as the unit, 46 pairs would have had the same question in both dev and
  test. The group id is `normalize_text(X) + " || " + normalize_text(C)` with
  the normalisation of section 6.1 (`pair_group_id` in
  `src/binding_gate/datasets/deceptive_grounding.py`); the triple id is kept in
  the item's gold metadata. Decided on 2026-11-22, before any model output was
  scored (section 17, closed items). The salt, the buckets and the rule above
  are unchanged. For GRAB-RAG it is the question, group id
  `<source dataset>/<question id>`, so both conditions used (misleading and
  supportive) of one question are always in the same split. For SealQA it is
  the question (group id derived from the normalised question text; SealQA has
  no row id, section 3, layer 3, item 5).
- The salt was fixed on 2026-11-22, before any data was opened, and is not
  tuned. `tests/test_preregistration.py` fails if this section and the code
  disagree.
- Ratio: 40% dev / 60% test in expectation. **PROPOSED — requires owner approval
  before freeze.** Because assignment is by hash, realised counts differ
  slightly from the expectation; PR 2 reports the realised number of groups per
  split.
- Dev is used for everything exploratory: prompts, the answer parser, alias
  handling, window sizes, the PR 2 baseline and stop rule. Test is used once per
  frozen configuration, for the final numbers.

## 5. Arms

Every arm receives the same question and documents and must return the
structured answer of section 6.2.

| Arm | What it does | PR |
|---|---|---|
| `no_gate` | Model answers in the required structure. No checks. | 2 |
| `prompt_entity_check` | As `no_gate`, plus an instruction to verify that the evidence is about the question's entity and to abstain otherwise. No external checks. | 5 |
| `faithfulness_checker` | `no_gate` answer, then an existing open-source claim-versus-source faithfulness checker decides accept/block. | 5 |
| `llm_judge_checks` | The gate's checks (C, D) performed by an LLM judge instead of deterministic code. | 5 |
| `gated` | Decomposer (A) + deterministic checks (C, D) + verdict (E). | 3, 4 |
| `gated` minus one check | One ablation per check, selected in the run config. | 5 |
| `gated_research` | `gated` plus the targeted re-search loop (layer 3 only). | 8 |

- **Same candidate, different gate.** In the controlled layers, `gated`, its
  ablations, `faithfulness_checker` and `llm_judge_checks` are applied to the
  *same* candidate answer that `no_gate` produced for that item, model and
  repeat (the runner reuses the response to an identical request within a run).
  Differences between these arms are then due to the checking step alone.
  `prompt_entity_check` uses a different prompt and therefore a different sample.
- **RESEARCH without search.** In layers 1 and 2 there is nothing to search. A
  RESEARCH verdict there is final and counts as an abstention.
- **The faithfulness checker.** Rule for choosing it (PR 5, on dev only,
  recorded before freeze): open source with a licence that permits this use;
  designed to judge whether a claim is supported by a source text; runnable at
  a pinned version; used at its documented default threshold, with no tuning on
  our data beyond what its documentation prescribes.
  *Decided under this rule by PR 5 on 2026-11-22, before any checker output was
  seen (no model has been run); needs owner confirmation at freeze:*
  **MiniCheck** (Tang, Laban and Durrett, EMNLP 2024), model
  `MiniCheck-Flan-T5-Large` (`src/binding_gate/arms/faithfulness_checker.py`).
  - *Code:* github.com/Liyan06/MiniCheck at commit
    `b58b9fa69acbd1015ec970fa65dd752413a053d2` (Apache-2.0, LICENSE file of the
    repository), installed from that commit by this project's optional extra
    `faithfulness`. It is not on PyPI; the PyPI project named `minicheck` is an
    unrelated program. MiniCheck's own dependencies are unpinned upstream; the
    extra bounds them (`transformers>=4.46,<5`, `torch>=2.5`, `accelerate`,
    `nltk>=3.9`), and the exact versions used are to be recorded with the run.
  - *Weights:* Hugging Face `lytang/MiniCheck-Flan-T5-Large`, revision
    `96eafd01cee2d16cf81aaa2fb226b14f422a37b3` (MIT, model card). MiniCheck
    loads the checkpoint's `main` branch; the arm refuses to run unless `main`
    is this revision, so a silent upstream change cannot alter results.
  - *Designed for* sentence-level fact-checking of LLM output against grounding
    documents, i.e. claim versus source.
  - *Threshold:* the package default, unchanged: a sentence is supported iff
    MiniCheck's support probability is greater than 0.5 (its `pred_label`);
    default chunking for this model (documents split at sentence boundaries into
    chunks of about 500 words, maximum over chunks). Nothing is tuned.
  - *How it is applied* (fixed): only `answered` candidates are checked
    (abstentions and unparseable outputs pass through). Claim = the candidate's
    value, split into sentences with NLTK as MiniCheck's README prescribes;
    evidence = the texts of all documents shown to the model, in the order shown,
    joined by a blank line; ACCEPT iff every sentence is supported, otherwise
    ABSTAIN. The quote and the question are not given to the checker (a
    claim-versus-source checker evaluates the claim against the source).
  - *Hardware:* about 780M parameters, CPU only, no GPU. Run time on a
    GitHub-hosted CPU runner (4 vCPU, 16 GB RAM) is **unverified**; it must be
    measured (`python -m binding_gate.arms.faithfulness_checker verify`, then the
    smoke run) before the dev run. Under the 6-hour job limit the dev run is to be
    split per model if needed.
  - *Why not the alternatives* (official pages read on 2026-11-22):
    MiniCheck's own default model, Bespoke-MiniCheck-7B, needs a GPU with vLLM
    and, per the README, a commercial licence from Bespoke Labs for commercial
    use; Flan-T5-Large is the authors' recommended model below 1B parameters.
    LettuceDetect (MIT, PyPI 0.2.3) is a token-level span detector; turning
    spans into accept/block would need a rule of our own. HHEM-2.1-Open
    (Vectara, Apache-2.0) returns a consistency score but its model card
    documents no decision threshold, and loading it executes model code from the
    hub. AlignScore (MIT) requires `torch<2` and `pytorch-lightning<2` (the last
    `torch<2` release has no wheels for Python 3.13, one of the two Python
    versions this project supports) and documents no threshold.
- **Judge-family rule.** Any LLM used as a judge (in `llm_judge_checks` or as a
  secondary scorer) comes from a different model family than the model under
  test in that comparison.

### 5.1 Gate checks and verdict (PR 4)

Implemented in `src/binding_gate/checks/` (one module per check, shared text
machinery in `checks/_evidence.py`), `src/binding_gate/gates/binding.py` (the
verdict) and `src/binding_gate/arms/gated.py` (the arm). Every check is a pure
function of the constraints (from the decomposer, A), the answered candidate and
the documents shown to the arm. No check calls a model, uses randomness or the
clock, or reads document ids, document metadata or gold labels, so the gate can
be run unchanged on live web pages.

**Text units.** All texts are first normalised as in section 6.1. A *token* is
a maximal run of Unicode word characters (`\w+`) in the normalised text. A name
(entity name or alias, qualifier text or alias, key term, competitor term) is
*mentioned* where its token sequence occurs as a contiguous token sequence of
the document: whole words only, punctuation between words ignored, case folded,
diacritics kept. A name without any word character can never match.

**Quote location.** The quote is looked up only in the document named by
`source_id`. Every place where the normalised quote is a substring of the
normalised document is an *occurrence*; its token span is the set of tokens
overlapping it. The *distance* between two token spans is the number of tokens
strictly between them (0 if they touch or overlap). The *window* is
`window_tokens` tokens on each side: a mention is within the window if its
distance to the quote is at most `window_tokens`. If the quote occurs more than
once, all window checks use the same *anchor* occurrence: the one closest to a
mention of the question's entity, ties broken by the earliest position.

**The checks** (reserved names; each returns pass, fail or not_applicable with
machine-readable details: positions, distances, the matched alias):

1. `quote_verbatim` — pass if `source_id` is a shown document, the quote has at
   least `min_quote_tokens` whitespace-separated tokens after normalisation
   (the section 6.3 rule), and it occurs in the cited document. Otherwise fail,
   with the first failing condition as the reason (`source_not_provided`,
   `quote_too_short`, `quote_not_found`). Never not_applicable.
2. `entity_in_window` — pass if the nearest mention of the entity name or an
   alias is within the window of the anchor (inside the quote counts as 0).
   Fail if there is none in the window (`entity_not_in_document` or
   `entity_outside_window`). Not_applicable if the quote cannot be located (that
   failure belongs to check 1, so the ablation without check 1 measures binding
   alone).
3. `qualifiers_in_window` — pass if every qualifier (its text or an alias) is
   mentioned within the window; fail otherwise, listing the missing ones.
   Not_applicable without qualifiers or if the quote cannot be located.
4. `no_closer_competitor` — fail if a competing entity is mentioned within the
   window and its distance to the anchor is smaller than that of the nearest
   entity mention, or equal to it when `competitor_tie = "fail"` (an entity not
   mentioned in the document is infinitely far); pass otherwise.
   Not_applicable if the quote cannot be located or there is no competing
   entity candidate.
5. `premise_present` (D) — pass if every key term of every presupposition is
   mentioned in the evidence, which is the whole cited document when
   `premise_scope = "document"` or the window when it is `"window"`; fail
   otherwise, listing the missing terms. Not_applicable if no presupposition
   has a key term or the cited document is not shown.

**Competing entity.** Without a model or a lexicon the gate cannot tell which
names are "entities of the same kind", so a competing entity is defined as the
*subject of a shown document* that is not the question's entity. For each shown
document (all of them, not only the cited one): take its lead segment (the text
before the first comma, semicolon, colon, bracket, `!`, `?`, double quote, or
full stop followed by a space or the end); take tokens from its start up to the
first word of a fixed list of English function words (`STOP_WORDS` in
`checks/_evidence.py`, frozen with the gate); if that run is empty or longer
than `lead_max_tokens`, the document has no lead term; otherwise its *lead term*
is the longest prefix of the run that occurs at least `lead_min_repeats` times
in the document and contains a letter. A lead term is excluded if it equals,
contains or is contained in the entity name or an alias, occurs in the question
text, or relates in the same way to a qualifier. The remaining lead terms are
the competitor candidates; their mentions in the cited document that overlap an
entity mention are ignored.

Dev-only structural check of this definition (PR 4, no model output involved;
gold fields used only to measure, never by the gate), with a decomposition that
takes X as the entity and C as the only qualifier: the recorded competitor
(`Y`, or `Y_synthetic` in `synthetic_y`) is among the candidates in 216/291
(`context_adjacent`), 225/291 (`null_control`), 255/291 (`prior_completing`)
and 270/291 (`synthetic_y`) dev items; in `class_proximate`, whose competing
drug is not recorded, the competing document yields a candidate in 276/291. The
other candidates found are almost all drug names; a few are generic words
("moderate", "chronic"), which can cause false blocks. Known misses: documents
that start with the disease or a generic phrase, and drugs mentioned only once.

**Verdict (E).** With F the set of failed checks (not_applicable never blocks):

| Rule | Condition | Verdict |
|---|---|---|
| 1 | F contains `quote_verbatim` or `premise_present` (or any check name not listed in rule 2) | ABSTAIN |
| 2 | otherwise F is not empty, i.e. only `entity_in_window`, `qualifiers_in_window`, `no_closer_competitor` failed | RESEARCH |
| 3 | F is empty | ACCEPT |

A quote missing from its source, or evidence lacking what the question
presupposes, is not repaired by searching for another source for the same
answer, so the gate abstains; evidence about another entity or other conditions
is what a targeted re-search can fix. Each failed check gives one reason
`"<check>: <reason code>"`. A RESEARCH verdict carries a research request: the
canonical JSON of the entity, its aliases, the attribute, the qualifier texts,
the failed checks, and a query string (entity, attribute and qualifier texts
joined by spaces). In layers 1 and 2 RESEARCH is final and the arm's final
answer is an abstention (section 5).

**Arm.** `gated` obtains its candidate with the same answer request as
`no_gate`, so within a run the runner's cache returns no_gate's candidate and no
second answer call is made. Abstained and unparseable candidates pass through
unchanged, without decomposition or verdict. For an answered candidate it gets
the constraints from the decomposer named in the config; one decomposition per
question is shared by all gated arms, models and repeats of a process, so that
ablations differ only in their checks. Ablations are configs that remove a name
from `options.checks`.

**Parameters.** **PROPOSED — requires owner approval before freeze.**
`window_tokens = 40` (about two sentences of the benchmark's prose; chosen a
priori, not tuned), `min_quote_tokens = 5` (same as section 6.3),
`lead_max_tokens = 4`, `lead_min_repeats = 2`, `premise_scope = "document"`,
`competitor_tie = "fail"`. They may be changed on dev only, before the freeze,
using `python -m binding_gate.gates.calibration`, which re-evaluates recorded
dev traces under other parameter values without calling any model and refuses
any run or record that is not from the dev split.

## 6. Scoring

### 6.1 Text normalisation for "verbatim"

Implemented in `src/binding_gate/textnorm.py` and used by both scoring and the
gate. In order: Unicode NFKC; typographic quotes, apostrophes, dashes and the
minus sign mapped to ASCII, soft hyphens and zero-width characters removed;
case folding; every run of whitespace collapsed to one space and the ends
trimmed. Nothing else is altered. A quote matches a document if the normalised
quote is a substring of the normalised document.

### 6.2 Required answer structure

Either `answered` with a non-empty value, a non-empty verbatim quote and the id
of the source document, or `abstained`. Anything else is `unparseable`,
including a value with no quote or no source id. The parser is deterministic
code written in PR 2 and frozen with this document. There is no second
"please fix your format" request in the primary analysis.

Concretely (PR 2, `src/binding_gate/parsing.py`, answer format version 1): the
model is asked for exactly one JSON object,
`{"status": "answered", "value", "quote", "source_id"}` or
`{"status": "abstained", "reason"}`; the instruction text is
`ANSWER_FORMAT_INSTRUCTIONS` and is appended to the benchmark's prompt. It asks
for one continuous quote of at least five words copied from a single document
and for the source label shown in that document's header; it names no document
and does not mention entities. For layer 1 the "value" is the model's one- or
two-sentence answer, because the benchmark's question is open-ended. The parser
takes every JSON object with a `status` key found in the reply (text around it
is ignored); none, or several different ones, is `unparseable`; `answered` needs
three non-empty string fields; `abstained` must not carry an answer; nothing is
repaired. The full rule is the module's docstring.

### 6.3 Primary scoring is deterministic

Provenance of an answered item is decided by string matching (section 6.1) of the
quote against every document shown for that item, using the document roles
supplied by the dataset adapter:

| Label | Meaning |
|---|---|
| `target_only` | quote found only in a document about the queried entity |
| `competitor_only` | quote found only in a competing document |
| `both` | quote found in both kinds of document |
| `neither` | quote found in no provided document |

Each record (item × arm × model × repeat) then falls in exactly one outcome
category, taken from the arm's **final** answer:

| Outcome | Definition |
|---|---|
| target-grounded answer | answered, `target_only` |
| **wrong-entity answer** | answered, `competitor_only` |
| ambiguous answer | answered, `both` |
| ungrounded answer | answered, `neither` |
| abstention | abstained (by the model, or because the gate blocked the answer) |
| unparseable | no valid structure |
| error | the arm crashed or the model call failed after the configured retries |

Notes that are part of the rule:

- A minimum quote length, to stop one-word quotes from matching everywhere:
  5 whitespace-separated tokens after normalisation; shorter quotes are scored
  `unparseable`. **PROPOSED — requires owner approval before freeze.** PR 2 may
  propose a different value from dev data only.
- A quote that matches a document other than the one named in `source_id` keeps
  the provenance label of where it was actually found; the mismatch is counted
  and reported separately.
- "Target-grounded" is a proxy for "correct": it establishes that the evidence
  is about the right entity, not that the value was read correctly. PR 2 checked
  whether this can be tightened to "target-grounded and value matches gold": it
  cannot. The benchmark files contain no gold answer values (section 3, fact 7),
  so for layer 1 "correct" stays "target-grounded". Whether the stated value
  follows from the quote is not measured by the primary scorer.
- In the five `absent` conditions no target document is shown, so no answer can
  be target-grounded there; the only outcomes are wrong-entity, ungrounded,
  abstention, unparseable and error.
- The scorer's labels are the four above plus `abstained` and `unparseable` for
  final answers that are not scoreable answers, so that every record carries
  exactly one provenance label; the outcome category is stored with it.
- For layer 3 (SealQA) correctness is judged against the dataset's gold answers
  by a rule fixed in PR 8 before any test use. If that rule cannot be fully
  deterministic, this is stated in an amendment together with the judge used.

### 6.4 Unparseable outputs and errors

They are never dropped.

1. Every record stays in the **all-items denominator** of every arm.
2. An unparseable or error record is not an answer: it is not in the numerator
   of any "answered" metric and not in the *among-answered* denominator.
3. In paired comparisons it counts against the arm that produced it: if
   `no_gate` gave a target-grounded answer and another arm's output for the same
   item is unparseable or an error, that is a **lost correct answer** for that
   arm.
4. The gate never converts an unparseable candidate into an abstention. A gate
   evaluates only `answered` candidates; abstentions and unparseable outputs
   pass through unchanged (the data contract enforces this), so an arm cannot
   hide format failures behind the gate.
5. The unparseable rate and the error rate are reported for every arm and
   model, next to every headline number.
6. Validity guard: if the unparseable-plus-error rate of an arm on the test
   split exceeds 5% (**PROPOSED — requires owner approval before freeze**), the
   comparison involving that arm is reported but labelled *not confirmatory*.
7. Sensitivity bound, always reported with H1: the comparison is recomputed
   with every unparseable/error record of the gated arm counted as a
   wrong-entity answer and every such record of `no_gate` counted as not wrong
   (the worst case for the gate).

### 6.5 Secondary scoring: LLM judge cross-check

The benchmark's own scoring is model-based: an LLM judge (their default is
`moonshotai/Kimi-K2.5`) attributes affirmative claims about X to the X document,
the competitor document or neither, and flags entity substitution; their labels
are EAF, entity substitution, confabulation, and a combined EAF-or-substitution
rate, with unparseable judgments excluded from their denominators. Their
validated verifier is not released.

We may run that judge as a **secondary** scorer only, obeying the judge-family
rule of section 5. We then report, per model: raw agreement and Cohen's kappa
between "wrong-entity answer" (ours) and "EAF or substitution" (theirs), and the
count of judge outputs that could not be parsed (kept in our denominators,
unlike theirs). No hypothesis is decided by the judge. Disagreements are sampled
for the human audit (section 13).

**Details (PR 5, `src/binding_gate/scorers/llm_judge.py`).**

- *Judge.* Slot J of section 9, `kimi-k3.1-pro` (Moonshot), a family different
  from every model under test. The judge list in the re-scoring config is
  ordered; for each model under test the first judge of a different, known
  family is used. Only slot J is listed: if no listed judge qualified for some
  model, re-scoring would refuse for that model rather than substitute a judge.
- *Prompt.* The benchmark's single-condition judge prompt and system prompt
  (`benchmark_judge.py` at commit `61b0281d8d3897f53bef3f7402727c905614c1e4`,
  Apache-2.0; attribution in NOTICE), reproduced verbatim, and its validation and
  JSON-extraction logic, re-implemented. The benchmark's 15-conditions-at-once
  batch prompt is not used. Settings as in the benchmark: temperature 0.0 and at
  most 12,288 output tokens (**PROPOSED — requires owner approval before
  freeze**; whether Moonshot's API accepts temperature 0.0 for this model is
  **unverified**). A reply that stopped because of length, content filter or a
  tool call counts as empty, as in the benchmark.
- *What the judge sees.* The X document (or `ABSENT`) labelled "about drug X",
  the competing document labelled "about drug Y" (the invented name in
  `synthetic_y`), the query and the response. Our response is the raw text of
  the record's final answer with any `<think>` block removed.
- *Which records are judged.* Final answers that are `answered`, and
  `unparseable` final answers that contain text (the measurement check of
  section 10 exists to find failures the deterministic scorer cannot see).
  Abstentions are labelled `abstained` and empty outputs `empty_output` without a
  call; records with an arm error are labelled `arm_error`.
- *Labels.* `eaf_or_substitution` (any claim attributed to Document 2, or entity
  substitution), `confabulation_only`, `correct_attribution`, `no_claims`, plus
  `judge_unparseable` and `judge_error` for judge failures. Every field of the
  judgment and every judge call is stored, so EAF and substitution can also be
  reported separately.
- *Agreement* (per model and arm). Population: records sent to the judge,
  including judge failures. Ours positive: outcome wrong-entity (section 6.3).
  Judge positive: `eaf_or_substitution`; a judge failure is a third category
  that never agrees. Raw agreement = agreeing records / population; Cohen's
  kappa over these categories, so judge failures lower both; also reported:
  kappa over parsed judgments only, the counts of records positive for both,
  only ours and only the judge, the full cross-table of outcome by judge label,
  and the judge's EAF-or-substitution rate over the 12 non-`null_control`
  conditions next to R (the figure the section 10 measurement check asks for).
- *Procedure.* Re-scoring reads existing dev traces and writes
  `scores.<scorer>.jsonl` beside them (the trace is never edited; resumable),
  after a dry run with a cost estimate from the real prompts, with
  `--confirm-spend`, a free preflight of the judge, and a hard budget cap per
  judge model and invocation of 100 USD in `configs/rescore/dev_llm_judge.toml`
  (**PROPOSED — requires owner approval before freeze**). Test-split traces are
  scored only by the frozen procedure of PR 6.

## 7. Metrics

Always reported as a pair, never one without the other.

**Primary pair (layers 1 and 2)**

- **M1, wrong among answered:** wrong-entity answers / answered records.
- **M2, correct answers blocked:** among records where `no_gate` gave a
  target-grounded answer, the share where the compared arm did not (same item,
  model and repeat).

**Always reported with them**

- wrong-entity answers / all records (all-items denominator);
- target-grounded answers / all records;
- the full outcome table of section 6.3, per arm, model, and condition;
- for layer 1, results by each of the 15 conditions and by the two condition
  factors.

**Layer 3 (live web)**

- **M3, net correct answers:** correct final answers / all records,
  `gated_research` versus the live `no_gate` (arm `no_gate_live`, label
  `no_gate`), same items, model and repeat (all-records denominator; section
  6.4 applies: unparseable and error records count as not correct).
- **M4, recovery rate after re-search:** among `gated_research` records whose
  first attempt ended in a RESEARCH verdict, the share that end in a correct
  final answer.
- wrong answers / answered; abstention rate; unparseable and error rates;
  per record: number of searches and page fetches (calls with purpose `search`
  and `fetch` that were not cache hits) and model tokens (cost); all of it also
  by stratum (Seal-0 versus the rest of Seal-Hard; `freshness`).

*Correctness rule* (scorer `sealqa_answer`, `src/binding_gate/scorers/sealqa_answer.py`;
version 1; **PROPOSED — requires owner approval before freeze**). Primary and
deterministic: an `answered` final answer is *correct* when its value carries
the gold answer - after the normalisation of section 6.1 and GRAB-RAG's answer
normalisation (lower case, punctuation and articles removed, `1,000` -> `1000`,
`1st` -> `first`), the gold answer occurs in the value as a whole-word
sequence (the layer-2 containment rule); otherwise *incorrect*. Abstentions and
unparseable answers are labelled as such; an answer whose quote has fewer than
5 tokens is unparseable (section 6.3). SealQA gives one answer per question and
no aliases. For the statistics, `correct` takes the place of
`target_grounded` and `incorrect` that of `wrong_entity`
(`layer3_outcome`, `analyse_layer3`; `python -m
binding_gate.scorers.sealqa_answer analyze <run dirs>`), so M3 = correct / all
and M4 as defined above; the layer-3 rule is then no longer a placeholder
(section 8).

The rule cannot be fully deterministic in the sense of SealQA's own grading:
it misses semantically equivalent answers in other words, formats or orders
(dates, units, spelled-out numbers) and partial answers that SealQA's
auto-rater accepts, and can accept a value that names the gold answer while
asserting something else. A **secondary LLM judge** is therefore planned as a
cross-check only, never deciding H4: SealQA's grader template (paper,
appendix C), run on dev records by a judge from a family different from the
model under test (judge-family rule, section 5; slot J), reporting agreement
with the primary rule. It is not implemented in PR 8 (open item, section 17).

**Decomposer (A), measured on its own (PR 3)**

- Agreement of the decomposer's entity, qualifiers and presuppositions with a
  reference decomposition on a dev sample, as specified below. Implemented in
  `src/binding_gate/evaluation/decomposer.py` (`python -m
  binding_gate.evaluation.decomposer dry-run|run|report`), configured by
  `configs/decomposer_eval_dev.toml`. Dev split only; the tool refuses any other
  split and never loads test items.
- Share of gate errors in the human audit that trace back to a wrong
  decomposition.

*The decomposer that is measured.* `model_decomposer`
(`src/binding_gate/decomposers/`): the model of slot "Dec" (section 9), the
fixed prompt version 1 (`decomposition_format.py`, SHA-256
`9e655050a2eaac39615ba4b2e9f974730757ba84d9e396305a98adf79084af90`, written
before any decomposer output was seen; it names no benchmark, drug or disease
and its one worked example is invented), a strict JSON output format and a
deterministic parser. A reply that breaks the format is a failure: it is never
repaired and never re-asked. Every call is logged with purpose `decompose`. In
the gated arm a failed decomposition leaves no constraints, so that item becomes
an error record (section 6.4). Within one run the gated arm obtains one
decomposition per question (a cache keyed by the decomposer's configuration hash
and the exact question text), shared by all conditions, models and repeats, so
the spread across repeats in section 8 does not include decomposer variability;
that variability is measured here instead (stability, below).

*Reference procedure, layer 1 (version 1; `decomposers/deceptive_grounding_reference.py`).*
Every layer 1 question is the template "What does the literature say about
using {X} to treat {C}?", so the reference is derived mechanically from the
item's gold metadata; nothing is written by hand per question. It reads gold,
so it is measurement code only and is never used by the gate or any arm.

- entity: name X, no aliases (the benchmark has none);
- qualifiers: one, text C, kind `other` (the disease restricts which facts
  about X count; `time`, `version` and `unit` do not apply, and `place` means a
  location or setting, not a condition);
- presuppositions: one, "X is used, or has been proposed, to treat C", key
  terms X and C;
- forbidden entity names: the competitors Y and `Y_synthetic` of every triple
  of the question's (X, C) pair; the competitor must never be the entity or one
  of its aliases (the drug of `class_proximate` documents is not recorded by the
  benchmark and cannot be listed);
- attribute: recorded, not scored (the question is open-ended, and no gate
  check reads the attribute).

Layers 2 and 3 have no reference procedure yet. PR 7 and PR 8 define one (for
example a reference written by a person for a sample, blind to the decomposer's
output) before those layers are frozen, or state that the decomposer is not
measured there.

*Unit, sample and repeats.* The unit is the distinct dev question (in layer 1
one (X, C) pair; the pinned benchmark has 45 distinct dev questions). Each
question is decomposed `REPEATS` times independently (cache disabled).
**PROPOSED — requires owner approval before freeze.**

```
DECOMPOSER_EVAL_SAMPLE_SIZE = 45
DECOMPOSER_EVAL_SEED = 20261122
DECOMPOSER_EVAL_REPEATS = 3
sample = the SAMPLE_SIZE dev questions with the smallest SHA-256("<SEED>\n<question>")
```

With 45 of 45 questions the seed does not change the sample; it is fixed so that
the rule stays the same if a smaller sample is ever used. Dry-run estimate with
the pinned benchmark (nothing spent): 135 calls, about 99,000 input tokens,
$0.03 (low) to $0.16 (high), ceiling $0.29; budget cap $0.50.

*Matching rules.* Strings are compared after the normalisation of section 6.1.
The *forms* of an entity or qualifier are its text plus its aliases. Every
record (question × repeat) counts; a failed decomposition stays in every
all-records denominator and is wrong on every all-records metric.

- **Entity correct:** a form of the decomposer's entity equals a form of the
  reference entity (so the queried name given as an alias counts), and no form
  equals a forbidden name. A form equal to a forbidden name is a **competitor
  collision**, counted separately.
- **Qualifier match (exact):** a decomposer qualifier and a reference qualifier
  share a form; the kind is not required to agree (kind agreement is reported).
  Recall = matched reference qualifiers / reference qualifiers, all records.
  Precision = matched decomposer qualifiers / decomposer qualifiers, parsed
  records.
- **Presupposition agreement (exact):** for each reference presupposition, one
  decomposer presupposition has a key term equal to each reference key term; a
  key term naming the entity may also be any form of a correct decomposer
  entity, one naming a reference qualifier any form of a decomposer qualifier
  that matched it.
- Secondary, reported but not used for the thresholds: *lenient* qualifier and
  presupposition matching, where the word tokens of one form may occur as a
  contiguous run inside the other.

*Reported.* Entity accuracy (all records and among parsed), name-exact rate,
competitor collisions, qualifier recall and precision (exact and lenient), kind
agreement, presupposition agreement (exact and lenient), parse-failure and
call-failure rates, stability across repeats (share of questions with identical
constraints in every repeat), and how each error would propagate into the gate:
every record is assigned consequence categories (unparseable or failed call →
error records; wrong entity → correct answers blocked; competitor collision →
wrong-entity answers can pass; missed qualifier → evidence about another disease
cannot be blocked; extra qualifier → correct answers at risk; missed
presupposition → the premise check loses its target), with the number of dev
items each would affect per run. These categories are the expected effect by the
design of the checks of section 1; PR 4's checks determine the actual effect.

*Thresholds* (on the complete dev measurement). **PROPOSED — requires owner approval before freeze.**

| Metric | Threshold |
|---|---|
| Entity accuracy, all records | ≥ 95% |
| Failure rate (unparseable + failed calls), all records | ≤ 2% |
| Competitor collisions | 0 |
| Qualifier recall, exact | ≥ 90% |
| Qualifier precision, exact | ≥ 80% |
| Presupposition agreement, exact | ≥ 80% |

Why: a wrong entity makes the gate test the wrong name for every condition of a
question, so entity errors must be rare; failures become error records in the
gated arm and must stay well below the 5% validity guard of section 6.4; a
single competitor collision lets the gate accept exactly the failure it exists
to block. Qualifier and presupposition errors weaken single checks rather than
the whole gate, hence lower bars.

*If the thresholds are not met* (**PROPOSED — requires owner approval before freeze**):
the prompt (as a new prompt version) or the model of slot "Dec" may be revised on dev
and measured again, at most three times; every measurement run is kept and
listed in the report. The decomposer used by the gated arm is the configuration
frozen with this document. If no revision meets the thresholds, the gated arm
is still run and reported, with the decomposer's measured accuracy stated next
to H1-H3; whether to proceed is an owner decision recorded in section 18. The
thresholds are a quality bar for a component, not a hypothesis.

*Limitations.* The 45 dev questions are the questions on which the decomposer
may be revised, so the dev measurement is optimistic. They also share one
template, so they say little about other question shapes. The frozen test runs
store the gated arm's constraints for every test question
(`TraceRecord.constraints`); the same comparison can be applied there after the
test run to obtain an unbiased layer 1 estimate (proposed for PR 6 or PR 10).

## 8. Statistics

Implemented in `src/binding_gate/stats/` (PR 6). The parameters below are
mirrored in `src/binding_gate/stats/prereg.py`, and
`tests/test_stats_prereg.py` fails if this section and the code disagree:

```
CI_LEVEL                 = 0.95
BOOTSTRAP_RESAMPLES      = 10000
BOOTSTRAP_SEED           = 20261122
REPEATS                  = 3
VALIDITY_MAX_FAILED_RATE = 0.05
```

All five values: **PROPOSED — requires owner approval before freeze.** The
seed was fixed on 2026-11-22, before any model output existed, and is not
tuned. `VALIDITY_MAX_FAILED_RATE` is the 5% of section 6.4 item 6.

- **Input.** Statistics read trace files only (`traces.jsonl` plus the run
  manifest, whose trace SHA-256 is verified); they never call a model and never
  re-score. The outcome category of each record is the one stored by the
  `provenance` scorer; a record with an error is `error`. One analysis covers
  one dataset and one split; mixing them is refused.
- **Paired design.** All arms run on the same items. For one model, the
  analysis units are the (item, repeat) pairs for which *every* arm has a
  record, so all arms are compared on exactly the same items and repeat
  indices. Because the runner writes a record for every item × arm × model ×
  repeat (an error record if the arm fails), a complete run has no unpaired
  record; any unpaired record is counted and makes that model's analysis not
  confirmatory.
- **Repeats.** 3 runs per configuration (item × arm × model). For item *i* with
  repeats *R_i*, the per-item value of an indicator *X* of an arm is
  *x_i* = (1/|R_i|) Σ_{r ∈ R_i} X(i, r). Every metric is a ratio of sums over
  items of such per-item averages. With complete data this equals pooling all
  records. The repeats are not resampled on their own: they stay inside their
  item and cluster. The spread across repeats is reported by computing each
  metric separately for each repeat index (per-repeat values, minimum, maximum).
- **Metrics** (section 7), with *N* the number of items and sums over items:
  M1 = Σ wrong-entity / Σ answered; M2 = Σ lost / Σ correct(no_gate), where
  *lost* is "no_gate gave a target-grounded answer and the arm did not" on the
  same (item, repeat); per-all rates = Σ *x_i* / *N*. Section 6.4 is applied
  literally: unparseable and error records are in *N*, are not answered, and an
  unparseable or error record of an arm where `no_gate` was correct is a lost
  correct answer. A ratio with a zero denominator is *undefined*.
- **Exact decisions.** Point estimates are computed in exact rational
  arithmetic, and every threshold of section 11 is compared exactly, so a value
  at a boundary is decided by the rule as written, not by rounding.
- **Confidence intervals.** Cluster bootstrap. The cluster is the split unit
  `group_id`: the (X, C) pair for layer 1, the question for layer 2, the question
  for layer 3. Resampling groups respects the dependence between the conditions of
  one triple and between the triples of one (X, C) pair. For one model with *G*
  clusters (sorted by `group_id`), each of `BOOTSTRAP_RESAMPLES` resamples draws
  *G* clusters with replacement with Python's
  `random.Random(BOOTSTRAP_SEED).choices`, adds up the integer column totals of
  the drawn clusters, and recomputes every statistic. All statistics of one
  model share the same resamples, so paired differences between arms are
  resampled jointly. The interval is the percentile interval at `CI_LEVEL`:
  the 2.5% and 97.5% quantiles of the resample values, with linear
  interpolation between order statistics (Hyndman–Fan type 7). Resamples in
  which a statistic is undefined are left out of its interval and their number
  is reported next to it.
- **Paired difference** of M1: M1(arm) − M1(no_gate), computed on the same
  units in every resample. "The interval excludes zero" (section 11) means that
  its upper bound is below zero, i.e. the arm is better; an interval entirely
  above zero is not support.
- **Relative reduction** is 1 − M1(arm) / M1(no_gate), with its interval from
  the same bootstrap; it is undefined when M1(no_gate) is zero or undefined.
- **Sensitivity bound** (section 6.4 item 7), reported with H1 and never
  decisive: the H1 rule is re-applied with every unparseable/error record of
  `gated` counted as a wrong-entity answer
  (M1 = (wrong + failed) / (answered + failed); wrong per all records
  = (wrong + failed) / *N*) and every such record of `no_gate` counted as an
  answered, not-wrong record (M1 = wrong / (answered + failed)).
- **Confirmatory status.** A hypothesis result is confirmatory only if: all
  records are from the test split; every run is finished; the model's data are
  complete with `REPEATS` repeats; the bootstrap used the parameters above; no
  arm involved in the comparison exceeds `VALIDITY_MAX_FAILED_RATE`
  (unparseable + error) / *N*; and, for H4, the layer-3 correctness rule of
  PR 8 is in place. Otherwise the result is reported with its status and the
  reasons it is not confirmatory.
- **Per model.** Every hypothesis is evaluated for each model separately; a
  pooled estimate (sums over models) is descriptive only and has no interval.
- **Breakdowns.** Point estimates are reported per condition (15 for layer 1)
  and per factor level (X completeness; competing-document condition), together
  with the full outcome table and the cost per arm. They are exploratory.
- **Multiplicity.** Confirmatory claims are limited to H1-H4 as defined in
  section 11. Everything else (per-condition tables, ablations, layer 2) is
  labelled exploratory. No correction is applied across models; the report
  states for how many of the frozen models each hypothesis held.
- **M4** (recovery after re-search) is computed from the first attempt's
  verdict; until PR 8 fixes the layer-3 correctness rule it uses
  "target-grounded" as a placeholder and is labelled as such.
- **Frozen analysis.** The analysis code is part of the tagged commit. The
  layer-1 test run is started with one command,
  `python -m binding_gate.cli frozen-test-run --config configs/test_layer1.toml
  --tag prereg-freeze-<n> --purpose "..." --confirm-spend`, which refuses (and
  logs the refusal) unless every condition of section 12 holds, runs every arm
  of the config on the test split, and writes `analysis.json`, `report.md` and
  figures under `runs/<run_id>/analysis/`.

## 9. Models

Working selection adopted for PR 2 on 2026-11-22 (the owner delegated model
selection; see section 17). **PROPOSED — requires owner approval before
freeze.** Nothing in this table has been confirmed against a live API: every id
must pass the free preflight (`python -m binding_gate.cli preflight`) before any
paid call, and the preflight has not been run yet.

| Slot | Model identifier (exact) | Family | Role | List price, USD per 1M tokens in / out | Source of id and price (accessed 2026-11-22) |
|---|---|---|---|---|---|
| A | `gpt-6-luna` | OpenAI | model under test, low-cost tier | 0.10 / 0.50 | id: developers.openai.com model page; price: The Register, 2026-10-26 |
| B | `gpt-6.1-sol` | OpenAI | model under test, mid/high tier | 2.00 / 10.00 | price: The Register, 2026-10-26; **exact id unverified** (pattern of the verified `gpt-6.1-terra`) |
| C | `claude-haiku-5-5` | Anthropic | model under test, low-cost tier | 0.10 / 0.50 (prompts up to 100k tokens) | id: Anthropic product information; price: several November 2026 third-party pricing summaries (**official pricing page not fetched**) |
| D | `claude-sonnet-5-5` | Anthropic | model under test, mid/high tier | 2.00 / 10.00 | as slot C |
| E | `gemini-3.1-flash-lite` | Google | model under test, low-cost tier | 0.25 / 1.50 | ai.google.dev/gemini-api/docs/pricing |
| F | `gemini-3.5-flash` | Google | model under test, mid/high tier | 1.50 / 9.00 | ai.google.dev/gemini-api/docs/pricing; third-party sources mention newer 3.6–3.8 Flash ids that were not seen on the official page, so **the preflight must confirm that this id exists** |
| J | `kimi-k3.1-pro` | Moonshot | secondary judge | 2.00 / 10.00 (**price unverified**) | price: a tech-insider report; id: listed on platform.kimi.ai as a newer model |
| Dec | `gpt-6-luna` | OpenAI | decomposer (A), provisional; PR 3 may change it through its own procedure | 0.10 / 0.50 | as slot A |

The same ids and prices are in `configs/dev_baseline.toml`, `configs/dev_smoke.toml`
and `configs/prices.toml`, and a test fails if they disagree.

**Rationale for this selection.** The models under test come from the three
providers that dominate production agent stacks. Within each family there is one
model from a low-cost tier (at most 0.25 USD per 1M input tokens) and one from a
price-matched mid/high tier (about 1.5–2 USD per 1M input tokens), so that
capability effects are compared within families at comparable price points
across families. Only generally available ids are used (no preview, no
deprecated model). Moonshot is reserved for the secondary judge, so that the
judge is from a family different from every model under test (section 5); this
also keeps the lineage of the benchmark's own judge (its default was
`moonshotai/Kimi-K2.5`, since deprecated on Moonshot's platform).

**Settings, identical for every model and recorded in every trace record.**
Reasoning / thinking: the provider's default ("as deployed"); no reasoning
option is sent. Temperature: 0 where the API allows it, otherwise the provider
default. The PR 2 configs send `temperature = 0.0` to Anthropic and Google and
send no temperature to OpenAI (whose reasoning-model generations have rejected
the parameter); **both choices are unverified against the live APIs** and the
smoke run decides: a provider that rejects the setting gets the provider
default, recorded here. Maximum output tokens: 4,096, generous so that
truncation does not create unparseable outputs; truncated calls are counted in
the baseline report. Note that for providers whose limit includes hidden
reasoning tokens, heavy reasoning can still truncate an answer.

**If Anthropic cannot be run.** If no Anthropic API key is available, slots C
and D are skipped by the workflow, and the study proceeds with A, B, E and F
(still at least three models from at least two families). This is recorded as an
amendment.

**Cost estimate for the PR 2 dev baseline** (from the dry run of
`configs/dev_baseline.toml` against the pinned benchmark; nothing was spent).
Per model: 45 dev pairs = 97 dev triples × 15 conditions × 3 repeats = 4,365
calls, with 5,612,528 prompt characters per repeat. Token assumptions (in
`configs/prices.toml`): input tokens = characters / 4, i.e. 4,211,079 input
tokens per model (about 965 per call; the rule of thumb is good to roughly
±20%); visible output 250 tokens per call; hidden reasoning / thinking tokens,
billed as output, between 0 (low) and an average of 2,000 per call (high). The
ceiling assumes that every call uses all 4,096 output tokens.

| Slot | Model | Calls | Low | High | Ceiling | Budget cap in the config |
|---|---|---|---|---|---|---|
| A | `gpt-6-luna` | 4,365 | $0.97 | $5.33 | $9.36 | $6 |
| B | `gpt-6.1-sol` | 4,365 | $19.33 | $106.63 | $187.21 | $110 |
| C | `claude-haiku-5-5` | 4,365 | $0.97 | $5.33 | $9.36 | $6 |
| D | `claude-sonnet-5-5` | 4,365 | $19.33 | $106.63 | $187.21 | $110 |
| E | `gemini-3.1-flash-lite` | 4,365 | $2.69 | $15.78 | $27.87 | $17 |
| F | `gemini-3.5-flash` | 4,365 | $16.14 | $94.71 | $167.23 | $100 |
| | **Total** | 26,190 | **$59.43** | **$334.43** | $588.25 | **$349** |

The budget cap is enforced during the run from the usage each provider reports;
the run cannot cost more than the sum of the caps (plus at most the few calls in
flight when a cap is reached). The smoke run (`configs/dev_smoke.toml`: the first 2 dev
pairs, 5 triples, 75 calls per model) is estimated at $1.02–$5.75 in total with
a cap of $2.50 per model;
it also measures the real reasoning-token usage, after which the caps above
should be lowered. Provider batch APIs (roughly half price) are not used in
PR 2; they are a later cost option. The judge (slot J) and the decomposer are
not run in PR 2 and are not in this estimate.

Selection rule (**PROPOSED — requires owner approval before freeze**):

1. At least three models under test from at least two different model families.
2. At least one small/low-cost model and at least one current high-capability
   model, since the failure may depend on capability.
3. Each is identified by the provider's exact, dated or versioned model id;
   aliases such as "latest" are not allowed. Sampling parameters are fixed in the
   run config (temperature 0 where the API allows it, otherwise the provider
   default, stated). The model id and version the provider reports for each call
   are stored in the trace.
4. The list, with a cost estimate, is approved by the owner before any spend.
5. After the PR 2 baseline no model is added, removed or swapped except through
   an amendment that states the reason; a model is never dropped because of its
   results (the stop rule of section 10 decides which models continue, and all
   baseline results are published either way).
6. If a provider retires or silently changes a frozen model before the test run,
   that is recorded as an amendment and the affected results are labelled.

## 10. PR 2 stop rule

PR 2 runs `no_gate` only, on the **dev split only**, 3 repeats, deterministic
scoring. The quantity is the **baseline wrong-entity rate**
R = wrong-entity answers / all records, over the dev stimuli of the 12
conditions whose competing-document condition is not `null_control`, per model.

**PROPOSED — requires owner approval before freeze:**

- **Continue** if at least one model has R ≥ 10%. The study continues with the
  models at or above 5%; the others are reported and carried no further.
- **Stop** if every model has R < 5%. The baseline results are published as the
  project's finding ("current models rarely make this error on this benchmark")
  and PRs 3-10 are not started.
- **In between** (best model between 5% and 10%): the default is to stop. The
  owner may decide to continue, as an explicitly exploratory study, by recording
  the reason in the amendment log before any further work.
- **Measurement check.** If, for a model, more than 20% of dev records are
  `ungrounded` or `unparseable`, the stop rule is not applied mechanically to
  that model: the deterministic scorer may be missing failures that are really
  there (for example paraphrased quotes). PR 2 then reports the secondary judge's
  rate next to R and the owner decides, with the decision logged.

Why these numbers:

- *Headroom.* H1 asks for the wrong-entity rate to be halved. From a 5% baseline
  a gate that just meets H1 removes about 2.5 wrong answers per 100 questions,
  and even a perfect gate removes 5, while H2 allows the gate to block up to 10%
  of correct answers. Below 5% the possible benefit is small next to the
  permitted cost.
- *Countable events.* With 40% of 264 triples in dev (about 106 in expectation)
  and 12 conditions, dev holds roughly 1,270 relevant stimuli per model. At
  R = 10% that is about 127 wrong-entity answers per repeat; at 5%, about 63. On
  test (about 158 triples, roughly 1,900 stimuli) a 10% rate gives about 190
  events, around 16 per condition, which is the least that makes the
  per-condition tables meaningful; 5% gives around 8 per condition.
- These are expectations from the split ratio, not measurements. PR 2 reports
  the realised counts.
- *Realised counts (PR 2; the estimate above was written when the unit was the
  triple).* With the (X, C) pair as the unit, the hash split put 45 pairs (97
  triples) in dev and 86 pairs (167 triples) in test. Dev therefore holds 1,164
  relevant stimuli per model and repeat (3,492 records over 3 repeats): about
  116 wrong-entity answers per repeat at R = 10%, about 58 at 5%. Test holds
  2,004 relevant stimuli: about 200 events at 10% (about 17 per condition),
  about 100 at 5% (about 8 per condition). The thresholds are unchanged. The
  number of independent clusters is smaller than the number of triples (45 in
  dev, 86 in test), which the bootstrap intervals will reflect.
- *How PR 2 computes it.* `python -m binding_gate.cli report <run directories>`
  reads trace files only. R pools the records of all repeats (with complete data
  this equals averaging per item first). The measurement check uses all dev
  records of the model, `null_control` included. Overall outcome: if a model
  that is not under a measurement check reaches 10%, CONTINUE; otherwise, if any
  model is under a measurement check, the outcome is MEASUREMENT-CHECK (owner
  decision); otherwise STOP or IN-BETWEEN as defined above. The secondary judge
  that the measurement check calls for is a PR 5 component
  (`scorers/llm_judge.py`); if the check triggers in PR 2, that scorer has to be
  brought forward, by amendment.

We do not know the current failure rate of any model on this benchmark under our
structured-answer protocol; that is exactly what PR 2 measures.

## 11. Success thresholds

All **PROPOSED — requires owner approval before freeze.** Evaluated on the test
split, per model, on all test records of the layer (for layer 1: all 15
conditions, `null_control` included), with the definitions of section 8. The
values are mirrored in `src/binding_gate/stats/prereg.py` and checked by
`tests/test_stats_prereg.py`:

```
H1_MIN_RELATIVE_REDUCTION = 0.50
H2_MAX_CORRECT_BLOCKED    = 0.10
H4_MIN_NET_DIFFERENCE     = 0
```

| Hypothesis | Supported if |
|---|---|
| H1 | Layer 1, all three: (a) M1 of `gated` is at least 50% lower than M1 of `no_gate`, i.e. 1 − M1(`gated`) / M1(`no_gate`) ≥ 0.50 (point estimate); (b) the 95% interval of the paired difference M1(`gated`) − M1(`no_gate`) excludes zero, i.e. its upper bound is below 0; (c) wrong-entity answers per all records do not increase: wrong / all of `gated` ≤ that of `no_gate` (point estimate). |
| H2 | Layer 1: M2 of `gated` is at most 10% (point estimate ≤ 0.10). |
| H3 | Layer 1: (a) wrong-entity answers per all records are lower for `gated` than for `faithfulness_checker` (point estimate), with the 95% interval of the paired difference (`gated` − `faithfulness_checker`) entirely below zero; and (b) M2 of `gated` is not higher than M2 of `faithfulness_checker` (point estimate). If the gate is better on one and worse on the other — (a) holds but M2 of `gated` is higher, or wrong / all of `gated` is higher (point estimate) but its M2 is lower — this is reported as a **trade-off**, not as support. Otherwise not supported. |
| H4 | Layer 3: M3 of `gated_research` is not lower than M3 of `no_gate` (paired difference, point estimate ≥ 0); the interval is reported. |

- Statuses: SUPPORTED, NOT SUPPORTED, TRADE-OFF (H3 only), NOT EVALUABLE (an
  arm or the layer the hypothesis needs is not in the data). A criterion whose
  quantity is undefined fails (for example M1 of a `gated` arm that answered
  nothing, or a relative reduction when M1 of `no_gate` is zero). Each status is
  also labelled confirmatory or not (section 8).
- Intervals that are not part of a rule (M2 in H2, the M2 difference in H3, M3
  in H4) are reported with the decision.
- The controlled-layer claim ("the gate works") requires H1 **and** H2 for the
  same model. H1 without H2 is reported as "reduces errors at an unacceptable
  cost".
- H4 as proposed is a weak criterion: with a live sample of this size the
  interval will be wide, and "not lower" by point estimate does not establish
  equivalence. A non-inferiority margin would be stricter; whether to adopt one
  is an open owner decision (section 17).
- Layer 2 has no pass/fail threshold. Its results are reported with the same
  metrics as evidence for or against generalisation beyond the clinical domain.
- A hypothesis that is not supported is reported as not supported. Thresholds
  are not moved after the test split is unlocked.

## 12. Test-split discipline

- Test items are reachable only through `binding_gate.testlock.load_test_split`,
  which refuses unless: this file's status is FROZEN; the caller names a tag
  `prereg-freeze-*`; HEAD is exactly that tag; and the working tree is clean.
- Every attempt, granted or refused, is appended to `audit/test_access_log.jsonl`,
  which is committed. Entries are hash-chained so that later edits are
  detectable.
- This is an audit trail, not a security boundary: a person who wants to look at
  test data can bypass it. The commitment is that the supported path leaves a
  record and that the record is published.
- Planned test-split uses: one frozen run per layer (layer 1: `cli
  frozen-test-run`, PR 6; layers 2 and 3: not implemented yet, section 17,
  item 39). Any additional use is listed in the amendment log with its reason.
- While the status is DRAFT the log must contain no successful access;
  `tests/test_preregistration.py` enforces this.

## 13. Human audit

100 gate decisions from the frozen test runs are audited by a human. The tool is
`src/binding_gate/audit_tool/` (PR 10); the parameters below are mirrored in
`src/binding_gate/audit_tool/prereg.py`, and `tests/test_audit_prereg.py` fails
if this section and the code disagree:

```
AUDIT_SEED               = 20261122
AUDIT_BLOCKED            = 50
AUDIT_ACCEPTED           = 50
AUDIT_DISAGREEMENT_SHARE = 1/2
AUDIT_CI_LEVEL           = 0.95
```

All five values, and the rules below: **PROPOSED — requires owner approval
before freeze.** The seed was fixed on 2026-11-22, before any gate decision
existed, and is not tuned.

- **Population.** A gate decision is the verdict on the *first* attempt of a
  trace record of the arm `gated` (layer 1) or `gated_research` (layer 3) in the
  frozen test runs; ablation arms are not in the population. Records whose
  candidate was not judged (abstained, unparseable) and error records are not
  gate decisions. All models and audited layers are pooled; the sample file
  records the split, every run's id, config hash, commit and trace SHA-256, and
  refuses runs that mix the dev and the test split.
- **Halves.** `AUDIT_BLOCKED` decisions where the gate blocked the answer
  (ABSTAIN or RESEARCH) and `AUDIT_ACCEPTED` where it accepted. A half with
  fewer decisions is taken whole and the shortfall is reported.
- **Over-sampling of scorer disagreements.** Where the secondary judge
  (section 6.5) was run, each half is split into the decisions on which the
  primary scorer and the judge disagree about the candidate (provenance outcome
  `wrong_entity` versus judge label `eaf_or_substitution`; a judge failure never
  agrees) and all others. At most `AUDIT_DISAGREEMENT_SHARE` of a half comes
  from the disagreements; if either stratum is too small, the other fills the
  half. Without judge scores each half is one stratum. Every sampled decision
  carries its sampling weight `N_s / n_s` (decisions in its stratum / decisions
  drawn from it).
- **Deterministic draw.** Within a stratum, the decisions with the smallest
  `SHA-256("<AUDIT_SEED>\n<canonical JSON of [run id, item id, arm, model,
  repeat, attempt]>")` are drawn. The display order is a second hash order, so
  blocked and accepted cases are interleaved.
- **Blinding.** Before labelling, the auditor sees the question, the documents
  (named "Document 1", "Document 2", ... in the order shown to the model; the
  dataset's source ids are hidden because in layer 1 they name the role), the
  candidate's value, its quote and which document it cites. The auditor answers
  one question: is the quoted evidence about the question's entity and
  qualifiers? — **yes / no / cannot tell**, with an optional note. Hidden until
  the label is entered: the verdict, its reasons and check results, the
  decomposer's constraints, item id and condition, model, arm, half, stratum,
  weight and the scorers' labels. Entering the reveal locks the label; a label
  not entered before the reveal is refused by the analysis. After the reveal the
  auditor compares the decomposer's constraints (as recorded in the trace) with
  the question and records one category: constraints correct, wrong entity,
  wrong or missing qualifier, wrong or missing presupposition, other
  decomposition problem, or not assessed.
- **Interfaces.** `python -m binding_gate.audit_tool sample RUN_DIR ... --out
  sample.json --html audit.html` draws the sample and writes a single offline
  HTML page (no server, no network; progress in the browser's local storage;
  an explicit "Export labels" button writes the label file). `python -m
  binding_gate.audit_tool label` is the terminal alternative. `python -m
  binding_gate.audit_tool analyze` computes the results below.
- **Analysis.** The gate counts as saying "bound" when it accepted. *Agreement*
  per half = auditor labels that agree with the gate / decided labels (yes or
  no) of that half; "cannot tell" is left out of the denominator, counted, and
  in a sensitivity variant counted as a disagreement. Each proportion has a
  two-sided **Clopper-Pearson exact binomial** interval at `AUDIT_CI_LEVEL`
  (about 50 cases per half and proportions possibly at 0 or 1, where a
  bootstrap percentile interval collapses to a point; Clopper-Pearson keeps at
  least the nominal coverage and needs no resampling). Also reported: agreement
  over the whole sample (unweighted, a property of the 50/50 sample), and a
  population-weighted agreement (stratum agreements weighted by stratum share
  of all decisions) with a conservative interval combining per-stratum
  Clopper-Pearson intervals at level 1 − α/K (Bonferroni over the K strata).
- **Reported**, in the public report: the composition of the sample and its
  weights, agreement per half and overall with intervals, the sensitivity
  variant, every disagreement (false accept or false block) with a short
  description (the auditor's note and the gate's reasons), and the share of gate
  errors traced to the decomposer = gate errors whose recorded decomposition
  category is a decomposer fault / gate errors with an assessed category, with
  its Clopper-Pearson interval.
- **Auditor(s): owner decision, still open (section 17, item 19).** With a single
  auditor there is no inter-rater check; the report states this limitation.
  With more than one, the first label file named is the primary auditor and
  Cohen's kappa against each further auditor is reported.

## 14. Live pages

For layer 3 every fetched page is stored at fetch time with its URL, fetch
timestamp and SHA-256; the gate and the scorers operate on the stored snapshot,
never on a re-fetch. Traces reference snapshots by id and hash. URLs, timestamps
and hashes are published. Page contents are published only where their licence
allows; otherwise they are kept privately and this is stated. Search results and
pages change over time, so layer 3 is reproducible from snapshots but not
re-runnable from scratch with identical inputs.

*Implementation (PR 8, `src/binding_gate/research/snapshots.py`).* Each run
keeps a store under `runs/<run_id>/snapshots/` (git-ignored): `blobs/<sha256>`
holds raw page bytes, robots.txt files, the search provider's raw responses,
extracted page texts and the exact text shown to the model, each named by the
SHA-256 of its own bytes, written once and made read-only; `index.jsonl` is an
append-only, fsynced log with one record per search (query, parameters, time,
ranked results, response hash, the logged call), per robots.txt (origin, time,
status, decision, hash) and per page fetch (snapshot id, requested and final
URL, redirects, time, outcome, HTTP status, content type, charset, raw and text
SHA-256, sizes, truncation, title, robots decision, extractor version, user
agent). Failed fetches are recorded the same way. Documents shown to the model
are built from the stored text read back and hash-checked, so the gate and the
scorer only ever see stored snapshots. Every attempt lists
`EvidenceRef(id = snapshot id, sha256 = hash of the shown text, url = final URL)`,
in the order of the documents (`web<n>` is `evidence[n-1]`).
`python -m binding_gate.research.snapshots verify <run dir>` re-hashes every
blob and checks every evidence reference of the traces against the store.

*Publication.* At the end of a run the runner writes
`snapshots/publish_manifest.json`: URLs, times, HTTP status, content types and
hashes of pages and robots.txt files, and for each search the query, time,
provider and ranked result URLs - no page text, titles or snippets. Page texts
are third-party material without a known licence and are kept privately. Trace
files contain them too (prompts are part of the trace), so the run directories
of live-web runs are uploaded by the workflow only encrypted (the
`RUNS_ENCRYPTION_PASSPHRASE` secret), next to the unencrypted publish manifest.

## 15. Reporting commitments

The public report is generated by `python -m binding_gate.report build` (PR 10)
from the analysis files of section 8 (`analysis.json`), the run directories,
the baseline report of section 10, the audit analysis of section 13 and this
document. It is written as one Markdown file and one self-contained HTML file
(no external assets, no script). Each commitment below has an identifier; the
report ends with a table that maps every identifier to the section that meets
it, and `tests/test_report.py` fails if an identifier listed here has no
section in the generator.

- **R1** Every hypothesis H1-H4 is reported for every model with its status —
  SUPPORTED, NOT SUPPORTED, TRADE-OFF (H3 only) or NOT EVALUABLE — next to the
  exact rule of section 11 (quoted from this document) and the observed values
  of each criterion.
- **R2** Confirmatory and exploratory results are in separate sections. A
  result is placed in the confirmatory section only if the analysis file marks
  it confirmatory (section 8); the report does not decide this itself, and it
  refuses to render an analysis file that marks a result confirmatory while
  listing reasons against it, or while the pre-registration it is given is not
  FROZEN. Everything else, including per-condition tables,
  ablations, layer 2 and pooled numbers, is labelled exploratory.
- **R3** Every arm, model, layer and condition named in this document is
  reported; the ones that are absent from the analysed data are listed as
  absent, and models in a run's config that have no analysed records are listed.
- **R4** The primary pair M1 and M2 is always shown together, for every arm and
  model, with its 95% intervals.
- **R5** Negative results and failures are stated as prominently as successes:
  in a section at the same heading level, placed directly after the headline
  results, listing every hypothesis that is not supported, not evaluable or a
  trade-off, H1 results that do not survive the sensitivity bound, validity-guard
  breaches, incomplete data, unfinished or unanalysed runs, absent layers and
  arms, and the audit's disagreements.
- **R6** The stop-rule outcome of section 10, from the baseline report, or a
  statement that it was not supplied.
- **R7** Deviations: every row of the amendment log (section 18); deviations the
  generator detects in the inputs (bootstrap parameters not as pre-registered,
  runs from a dirty working tree, test runs without a freeze tag, an audit sample
  drawn with non-registered parameters or from dev traces); and deviations
  declared in a deviations file, each with the amendment that authorised it or
  the statement that it was unplanned. The generator cannot detect every
  deviation; the report says so.
- **R8** The threats to validity of section 16, reproduced from this document.
- **R9** Costs actually spent: per model, the USD spend the runner recorded
  (provider-reported token usage priced with the pinned price file; not an
  invoice), the budget caps, and calls by clients that could not be priced; per
  arm and model, paid, cached and failed calls and reported tokens (and
  searches, where layer 3 records them).
- **R10** Traceability: every run by run id, config hash, git commit, freeze
  tag and trace-file SHA-256, and every input file (analysis, baseline, audit)
  by SHA-256, so that each number can be traced to append-only trace files.
- **R11** The human-audit results of section 13, or the statement that the
  audit has not been carried out.
- **R12** Next to every headline number: the unparseable and error rates of
  every arm and model, and for H1 the sensitivity bound of section 6.4 item 7.

## 16. Threats to validity

- **Constructed clinical data.** Layer 1 uses documents constructed by the
  benchmark authors around drug-disease-competitor triples. Effects there may
  not transfer to naturally occurring pages, other domains, or other kinds of
  entities. Layers 2 and 3 address this only partly.
- **Entity matching is brittle.** The deterministic checks compare strings.
  Aliases, brand versus generic names, abbreviations, pronouns and other
  coreference, and facts laid out in tables or lists (where the entity is in a
  header far from the value) can each cause false blocks or false accepts.
  Window sizes and alias handling are tuned on dev only.
- **The scorer shares the gate's blind spots.** Primary scoring and the gate's
  first check both rely on verbatim quote matching. A model that paraphrases its
  quote is scored `ungrounded` rather than wrong-entity, which undercounts the
  baseline failure; the gate blocks such answers for a reason other than entity
  binding. The outcome table, the sensitivity bound, the secondary judge and the
  human audit exist to expose this; they do not remove it.
- **Forcing structure changes the task.** Requiring value + quote + source from
  every arm, including the baseline, may itself change how often models make the
  error. Our baseline rates are therefore not comparable with the benchmark
  paper's published rates, and the study says nothing about free-text answers.
- **Decomposer errors.** If (A) extracts the wrong entity or misses a qualifier,
  the deterministic checks faithfully enforce the wrong constraints. (A) is
  measured separately and its contribution to gate errors is reported, but it
  remains a model-based step.
- **Small live sample.** SealQA is reported to have 619 rows across three
  subsets; after the split and the choice of subset the test sample is small, so
  H4 will have a wide interval.
- **Model drift.** Hosted models change or are retired. Exact model ids are
  recorded, but a rerun months later may not reproduce the numbers.
- **The benchmark's own judge.** The benchmark's published labels come from an
  LLM judge and its validated verifier is not released. Our deterministic labels
  measure a related but not identical construct, and agreement between the two is
  an empirical question we report rather than assume. A judge from the same
  family as the model under test could share its errors, hence the family rule.
- **Researcher degrees of freedom.** Prompts, parser, windows and aliases are
  developed on dev by the same people (and AI coding agents) who want the gate to
  work. The split, the freeze tag, the test lock and this document limit, but do
  not eliminate, that risk.
- **Possible training-data exposure.** The benchmark is public; models released
  after it may have seen it. We cannot measure this.
- **Few independent clusters, and residual relatedness.** The 264 triples cover
  131 (X, C) pairs, and the pair is the split and bootstrap unit (section 4), so
  no question appears in both dev and test. What remains: the same drug or the
  same disease can still occur in pairs on both sides (for example one disease
  with several queried drugs), the same competitor Y occurs across pairs, and
  the confirmatory test rests on 86 clusters, so intervals will be wider than
  the number of stimuli suggests.
- **Source labels hint at the role.** The benchmark labels the documents
  `cx_doc` and `cy_doc`, and we keep its presentation. A model, or a gate, could
  in principle learn that `cx` means "the queried drug". The gate must not use
  document ids (ARCHITECTURE.md: only the question, the constraints and the
  document text); the baseline rate may still be affected in either direction.
- **The benchmark's system prompt and ours pull apart.** Its system prompt asks
  for inline `[n]` citations and mentions tools that do not exist in
  `doc_injection` mode; our instruction then asks for a JSON object and says it
  replaces other formatting instructions. Some models may follow the system
  prompt instead, which shows up as `unparseable`.
- **Open-ended question.** "What does the literature say about using X to treat
  C?" has no single value. Provenance of one quote does not establish that the
  whole answer is free of competitor content: an answer may quote the X document
  and still summarise the competitor's findings in its value.
- **Corpus irregularities.** Seven documents are the literal text `...` and four
  triples have no completing-information targets (section 3, fact 10). They are
  kept; conditions built on an empty competing document cannot yield a
  wrong-entity answer under quote matching.
- **Single-benchmark confirmation.** H1-H3 rest on one benchmark. A positive
  result is evidence for the gate on this kind of stimulus, not a general
  guarantee.

## 17. Open items that must be closed before the status becomes FROZEN

One checklist (consolidated in version 0.3). Each item names the section it
refers to and who or what closes it. An item is closed by recording the
decision or the verified fact in the section named, with the date, and marking
the item *Closed* here. Before the freeze these are ordinary edits (section 18).

**(A) Owner approval of PROPOSED values.** Closed by the owner's explicit
approval (or a changed value) recorded in the section named. Values may still be
changed on dev data before the freeze.

1. Dev/test ratio 40% / 60% (section 4).
2. Minimum quote length of 5 tokens for scoring and for the gate's
   `min_quote_tokens` (sections 6.3 and 5.1).
3. Validity guard: unparseable-plus-error rate at most 5% (sections 6.4 item 6
   and 8).
4. Statistics parameters: bootstrap seed 20261122, 10,000 resamples, 95%
   level, 3 repeats (section 8).
5. PR 2 stop rule: continue at R ≥ 10%, carry models at ≥ 5%, stop below 5%,
   default stop in between, measurement check above 20% (section 10).
6. Success thresholds H1-H4 and their rules (section 11).
7. Gate parameters: `window_tokens = 40`, `lead_max_tokens = 4`,
   `lead_min_repeats = 2`, `premise_scope = "document"`; `competitor_tie` is
   item 16 (section 5.1).
8. Decomposer measurement: sample 45, seed 20261122, 3 repeats, the thresholds
   table and the revision rule of at most three revisions (section 7).
9. Human-audit parameters: seed 20261122, 50 blocked + 50 accepted,
   disagreement share 1/2, 95% Clopper-Pearson intervals, and the rules of
   section 13.
10. Layer-3 protocol values: `max_attempts = 2`, 10 results, 5 pages, 8,000
    characters per page, fetch limits (2,000,000 bytes, 20 s, 5 redirects, 1 s
    per host), one search per 18 s (section 3, layer 3, item 7); the layer-3
    correctness rule `sealqa_answer` version 1 (section 7); the layer-2
    answer rule `grab_rag_answer` (section 3, layer 2, item 10).
11. Secondary-judge settings: temperature 0.0, at most 12,288 output tokens,
    re-scoring cap 100 USD per judge model and invocation (section 6.5).
12. The model selection rule (section 9).
13. Budget caps of every paid config (section 9; section 3, layer 3, item 8;
    section 7; `configs/*.toml`, `configs/rescore/dev_llm_judge.toml`), to be
    lowered after the smoke run has measured real usage. Since PR 8 the runner
    charges decomposer and LLM-judge calls to the cap of the record's model;
    the headers of `configs/dev_baselines.toml` and `configs/dev_ablation_*.toml`
    still say these calls are not counted. In `configs/dev_baselines.toml` the
    judge calls (up to one per item and repeat) therefore count against each
    model's cap in addition to the arm's own `judge_max_usd`, and its caps must
    be re-derived from a dry run. The caps in `configs/test_layer1.toml` are
    placeholders.

**(B) Owner decisions.** Closed by the owner's decision recorded in the section
named.

14. The model list (section 9): confirm the working selection, including
    whether to add a flagship model. Whether an Anthropic API key is available;
    if slots C and D cannot be run, the study proceeds with A, B, E and F,
    recorded as an amendment.
15. Whether H4 gets a non-inferiority margin instead of "point estimate ≥ 0"
    (section 11).
16. Whether ties in `no_closer_competitor` fail (`competitor_tie = "fail"`,
    the current value) or pass (section 5.1).
17. Layer 2 interpretation (section 3, layer 2). GRAB-RAG's "misleading"
    condition is answer-span substitution, not entity mismatch, and its
    misleading contexts are not shipped (an export with GRAB-RAG's own code,
    which needs a GPU, is required). Recommendation recorded by the
    orchestrator: treat layer 2 as a scope boundary plus a cost-side (M2)
    measurement on supportive items. Owner decision pending. Note: the adapter
    can build supportive items without the export (dataset option
    `conditions = ["supportive"]`), but no config does this and no layer-2
    config contains a gated arm yet.
18. One test-split access for all models together, or one access per model
    (a GitHub-hosted job is limited to 6 hours; each `frozen-test-run`
    invocation is one logged access) (sections 8 and 12).
19. Who performs the human audit, and whether layer-3 gate decisions are pooled
    with layer-1 decisions in one audit sample (section 13; the tool currently
    pools all audited layers).
20. Whether the faithfulness checker chosen by PR 5 (MiniCheck-Flan-T5-Large)
    is confirmed for H3 (section 5).
21. Whether the parser and answer-format instruction of section 6.2 are kept
    as they are after the dev baseline (section 6.2).

**(C) Facts to verify once data and keys exist.** Closed by the run or source
named, with the result entered in the section named.

22. Model ids `gpt-6.1-sol` (slot B) and `gemini-3.5-flash` (slot F) exist:
    free preflight (`python -m binding_gate.cli preflight`, also part of every
    workflow dry run) (section 9). The other ids are checked the same way.
23. Prices of slots C and D (Claude) and J (Kimi) from the providers' official
    pricing pages; every price in `configs/prices.toml` against official pages
    or invoices (section 9).
24. Whether each provider accepts the temperature setting sent (0 for
    Anthropic, Google and Moonshot; none for OpenAI), and the reported usage
    including reasoning tokens: smoke run (sections 9 and 6.5).
25. SealQA row counts and values after `python -m binding_gate.cli
    dataset-prepare` (`data/raw/sealqa/conversion.json`), entered from dev-split
    counts only (section 3, layer 3, items 3 and 5).
26. The Natural Questions data licence from a primary source (section 3,
    layer 2, item 1).
27. MiniCheck on Python 3.13, CPU only: run time on a GitHub-hosted runner
    (`python -m binding_gate.arms.faithfulness_checker verify`, then the smoke
    run) (section 5).
28. Dev baseline and stop-rule outcome per model (section 10).
29. Decomposer accuracy on dev against the thresholds (`python -m
    binding_gate.evaluation.decomposer`) (section 7).
30. Gate calibration on dev traces, the first analysis after the keys are
    connected (`python -m binding_gate.gates.calibration`) (section 5.1).
    Open risk for H2: in the orchestrator's integration run (scripted fake
    models on the real dev data), a model that always quotes the target
    document had 29.5% of its correct answers blocked by the gate, mostly by
    `qualifiers_in_window` and `premise_present` (the disease name C not near
    the quote, or not in the cited document). Real models may quote
    differently, so this is not an estimate of M2; it is the reason the window
    and the qualifier and premise checks are calibrated on dev traces before
    the freeze.

**(D) Engineering items.** Closed by a pull request (or, for item 41, by the
owner in the repository settings).

31. Decomposer cache preload on resume: a resumed run may decompose a question
    again (`DecompositionCache.preload` exists; the runner does not call it)
    (section 7).
32. Seeding a new run's response cache from an earlier trace, so that the
    `no_gate` answers are not paid again in `dev_gated`, the ablations and the
    comparison baselines (section 5; ARCHITECTURE.md section 8a).
33. A public, read-only lock check in `testlock` instead of the private
    `_unlock_refusals` used by `frozen_run.py` (section 12).
34. `cli analyze` for layer 3. *Closed in version 0.3 (PR 11): `cli analyze`
    recognises SealQA run directories from their manifests and applies the
    `sealqa_answer` rule; mixed layers are refused.*
35. The layer-3 secondary LLM judge is not implemented (section 7, layer 3).
36. The decomposer is not measured on layers 2 and 3: define a reference
    procedure or state that it is not measured there (section 7).
37. Location of published result sets: `results/layer1/<run_id>/` (proposed;
    ARCHITECTURE.md section 7; section 15, R10).
38. `configs/test_layer1.toml`: complete the arm options (decomposer, judges,
    checker and gate parameters as in the dev configs; TODO markers), make the
    ablation labels agree with the dev configs (`gated_minus_<check>` there,
    `gated-minus-<check>` here), and dry-run the same arms on dev (sections 8
    and 12).
39. No frozen test configuration or frozen analysis path exists for layers 2
    and 3: `frozen_run.py` analyses with the layer-1 rule (section 12 plans one
    frozen run per layer).
40. The workflow installs the `faithfulness` extra but does not run
    `python -m binding_gate.arms.faithfulness_checker prepare`, so a config with
    the `faithfulness_checker` arm stops in the workflow before its first paid
    call; add a prepare step or run it locally (section 5; docs/RUNNING.md).
41. Repository secrets created by the owner: `OPENAI_API_KEY`,
    `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` (optional, item 14),
    `MOONSHOT_API_KEY`, `SERPAPI_API_KEY`, `RUNS_ENCRYPTION_PASSPHRASE`
    (docs/RUNNING.md, step 1).
42. `PARAMETERS_STATUS` of the MCP server (`src/binding_gate/mcp/core.py`)
    updated at the freeze (section 5.1).

*Closed earlier, kept for the record:* layer-1 facts from the real files
(PR 2; section 3, facts 1-11); "correct" for layer 1 cannot be tightened to a
gold value (PR 2; section 6.3); the split and bootstrap unit for layer 1 is the
(X, C) pair (decided 2026-11-22 by the orchestrator under the owner's
delegation, before any model output was scored; sections 3, 4 and 8); the
faithfulness checker and its pinned version (PR 5; owner confirmation is
item 20); the layer-1 decomposer reference procedure (PR 3; section 7);
GRAB-RAG licence, format and document roles (PR 7; NQ primary source is
item 26); SealQA subset, fields and correctness rule (PR 8; counts are item 25,
approval of the rule is item 10).

2026-11-22: owner directed that all PRs follow this document and delegated
model selection; PROPOSED values are adopted as working values and still
require explicit confirmation at freeze.

The freeze happens after these items are closed and before any test-split
access. Layers may be frozen in stages (`prereg-freeze-1` for layer 1, later
tags for layers 2 and 3) provided each stage is frozen before its own test split
is used; items that concern only a later layer may then stay open for the
earlier tag, and the stage frozen says which.

## 18. Amendment log

Rules: after the freeze this document changes only by adding a row here, in the
same commit as the change. Each row states what changed, why, and whether any
test-split result had been seen by anyone involved at that time. Before the
freeze, changes are ordinary edits and need no row.

| # | Date | Section | Change | Reason | Test results seen before the change? |
|---|---|---|---|---|---|
| (none yet) | | | | | |
