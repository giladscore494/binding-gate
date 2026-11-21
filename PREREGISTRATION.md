# Pre-registration: binding-gate

**Status:** DRAFT

**Document version:** 0.1 (2026-11-22)

This document fixes, before any model is run, what will be measured, how, and
what counts as success or failure. While the status is DRAFT everything in it
may still change and nothing in it has been approved. It becomes binding when the
owner approves it, the status line is changed to `FROZEN`, and that commit is
tagged `prereg-freeze-<n>`. From then on changes are made only through the
amendment log (section 18).

**State of the project when this draft was written:** no model has been called,
no benchmark file has been downloaded or opened, and no result of any kind
exists. Statements about external datasets below come from their public
descriptions; where a detail has not been checked against the real files it is
marked **unverified**.

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
| 1. Controlled | Deceptive Grounding benchmark (github.com/CedricCaruzzo/deceptive-grounding-benchmark; arXiv 2607.09349) | Confirmatory for H1-H3 | Apache-2.0 | Structure known from the public description; files not yet inspected |
| 2. Controlled, other domain | GRAB-RAG (arXiv 2608.22228; code stated at github.com/andre44s/grab-rag), misleading-context condition | Generalisation (secondary) | **unverified** | Repository not yet opened |
| 3. Live web | SealQA (Hugging Face `vtllms/sealqa`) | Confirmatory for H4 | Apache-2.0 | Not yet downloaded |

**Layer 1.** Each stimulus is built from a triple: queried drug X, competitor
drug Y, disease C. The benchmark has 264 triples × 15 conditions = 3,960 stimuli.
The 15 conditions cross X-document completeness {absent, partial, complete} with
a competing-document condition {null_control, class_proximate, context_adjacent,
prior_completing, synthetic_y}. The benchmark provides no train/dev/test split.
Unverified, to be resolved by PR 2 from the real files and recorded as an
amendment before freeze: the file format and field names; what exactly is shown
in the `null_control` and `absent` conditions; whether gold answer values exist;
and a discrepancy in the benchmark's own README, which describes
`data/triple_pool.json` as holding 250 triples while
`data/constructed_documents/` is described as holding 264 triple files.

**Layer 2.** GRAB-RAG presents the same questions (from Natural Questions and
HotpotQA) under four context conditions: supportive, degraded, missing and
misleading. We use the misleading condition. Unverified: the licence, the data
format, and whether misleading contexts are entity or qualifier mismatches of
the kind the gate targets. For context only, its authors report that in the
misleading condition small models still answered 41.6% of the time under explicit
abstention prompting, and that a generator-side conflict check cut this to 13.3%
while discarding many correct answers. These figures are not a baseline for this
study; our arms are measured on our own runs.

**Layer 3.** SealQA is reported to have 619 rows (seal_0: 111, seal_hard: 254,
longseal: 254). These counts are *reported*, not checked by us. The subset(s)
used, the field names and the answer-correctness rule are decided by PR 8 on the
dev split and recorded as an amendment before any test-split use.

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
  triple, so all 15 conditions of a triple are always in the same split. For
  GRAB-RAG it is the question (all four context conditions together). For SealQA
  it is the row.
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
- **The faithfulness checker** is TODO. Rule for choosing it (PR 5, on dev only,
  recorded as an amendment before freeze): open source with a licence that
  permits this use; designed to judge whether a claim is supported by a source
  text; runnable at a pinned version; used at its documented default threshold,
  with no tuning on our data beyond what its documentation prescribes.
- **Judge-family rule.** Any LLM used as a judge (in `llm_judge_checks` or as a
  secondary scorer) comes from a different model family than the model under
  test in that comparison.

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
  is about the right entity, not that the value was read correctly. If the
  benchmark files contain gold values that allow deterministic value matching, a
  stricter definition (target-grounded and value matches gold) is adopted by
  amendment before freeze. Unverified until PR 2.
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

- **M3, net correct answers:** correct final answers / all items, `gated_research`
  versus `no_gate`.
- **M4, recovery rate after re-search:** among first attempts that ended in a
  RESEARCH verdict, the share that end in a correct final answer.
- wrong answers / answered; abstention rate; number of searches and model
  tokens per item (cost).

**Decomposer (A), measured on its own (PR 3)**

- Agreement of the decomposer's entity, attribute and qualifiers with a
  reference decomposition on a dev sample; how the reference is produced is TODO
  for PR 3 and is recorded before freeze.
- Share of gate errors in the human audit that trace back to a wrong
  decomposition.

## 8. Statistics

- **Paired design.** All arms run on the same items; comparisons are differences
  within item.
- **Repeats.** 3 runs per configuration (item × arm × model). Per-item outcomes
  are averaged over the 3 repeats before comparison; the spread across repeats
  is reported.
- **Confidence intervals.** 95% percentile intervals from a cluster bootstrap
  that resamples *groups* (triples, questions, rows), 10,000 resamples, fixed
  seed recorded in the run config. Resampling groups respects the dependence
  between the conditions of one triple. (Interval level and resample count:
  **PROPOSED — requires owner approval before freeze.**)
- **Relative reduction** is the ratio of M1 between arms, with its interval from
  the same bootstrap.
- **Per model.** Every hypothesis is evaluated for each model separately; a
  pooled estimate is descriptive only.
- **Multiplicity.** Confirmatory claims are limited to H1-H4 as defined in
  section 11. Everything else (per-condition tables, ablations, layer 2) is
  labelled exploratory. No correction is applied across models; the report
  states for how many of the frozen models each hypothesis held.
- Implemented in PR 6; the analysis code is frozen (tagged) before the test
  split is unlocked.

## 9. Models

TODO, to be filled in and frozen **before the PR 2 dev baseline is run**:

| Slot | Model identifier (exact, versioned) | Family | Role |
|---|---|---|---|
| A | TODO | TODO | model under test |
| B | TODO | TODO | model under test |
| C | TODO | TODO | model under test |
| J | TODO | TODO (different family from the model it judges) | secondary judge |
| D | TODO | TODO | decomposer (A) |

Selection rule (**PROPOSED — requires owner approval before freeze**):

1. At least three models under test from at least two different model families.
2. At least one small/low-cost model and at least one current high-capability
   model, since the failure may depend on capability.
3. Each is identified by the provider's exact, dated or versioned model id;
   aliases such as "latest" are not allowed. Sampling parameters are fixed in the
   run config (temperature 0 where the API allows it, otherwise the provider
   default, stated).
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

We do not know the current failure rate of any model on this benchmark under our
structured-answer protocol; that is exactly what PR 2 measures.

## 11. Success thresholds

All **PROPOSED — requires owner approval before freeze.** Evaluated on the test
split, per model.

| Hypothesis | Supported if |
|---|---|
| H1 | Layer 1: M1 of `gated` is at least 50% lower than M1 of `no_gate` (relative reduction, point estimate), the 95% interval of the paired difference excludes zero, and wrong-entity answers per all records do not increase. |
| H2 | Layer 1: M2 of `gated` is at most 10%. |
| H3 | Layer 1: wrong-entity answers per all records are lower for `gated` than for `faithfulness_checker`, with the 95% interval of the paired difference excluding zero, and M2 of `gated` is not higher than M2 of `faithfulness_checker`. If the gate is better on one and worse on the other, this is reported as a trade-off, not as support. |
| H4 | Layer 3: M3 of `gated_research` is not lower than M3 of `no_gate` (paired difference, point estimate ≥ 0); the interval is reported. |

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
- Planned test-split uses: one frozen run per layer (PR 6 for layer 1; PRs 7
  and 8 for layers 2 and 3). Any additional use is listed in the amendment log
  with its reason.
- While the status is DRAFT the log must contain no successful access;
  `tests/test_preregistration.py` enforces this.

## 13. Human audit

- 100 gate decisions from the frozen test runs are audited by a human (PR 10
  provides the tool).
- Sampling (**PROPOSED — requires owner approval before freeze**): fixed seed;
  50 decisions where the gate blocked the answer (ABSTAIN or RESEARCH) and 50
  where it accepted; where the secondary judge was run, disagreements between
  the two scorers are over-sampled within each half and the sampling weights are
  reported.
- The auditor sees the question, the documents, the candidate answer and quote,
  and answers: is the quoted evidence about the question's entity and
  qualifiers (yes / no / cannot tell)? The gate's verdict and reasons are hidden
  until the label is entered.
- Reported: agreement between gate and auditor with its interval, every
  disagreement with a short description, and the share of gate errors caused by
  the decomposer.
- Auditor(s): TODO (owner decision). With a single auditor there is no
  inter-rater check; this is stated in the report.

## 14. Live pages

For layer 3 every fetched page is stored at fetch time with its URL, fetch
timestamp and SHA-256; the gate and the scorers operate on the stored snapshot,
never on a re-fetch. Traces reference snapshots by id and hash. URLs, timestamps
and hashes are published. Page contents are published only where their licence
allows; otherwise they are kept privately and this is stated. Search results and
pages change over time, so layer 3 is reproducible from snapshots but not
re-runnable from scratch with identical inputs.

## 15. Reporting commitments

- All hypotheses, arms, models and layers named here are reported, including
  null and negative results, the stop-rule outcome, runs that failed, and
  unparseable/error rates.
- Every reported number is traceable to append-only trace files that carry the
  config hash and git commit of the run.
- Exploratory analyses are labelled as such.
- Deviations from this document are listed in the report, each with the
  amendment that authorised it or a statement that it was unplanned.
- Costs (tokens, searches) are reported per arm.

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
- **Single-benchmark confirmation.** H1-H3 rest on one benchmark. A positive
  result is evidence for the gate on this kind of stimulus, not a general
  guarantee.

## 17. Open items that must be closed before the status becomes FROZEN

1. Owner approval of every PROPOSED value (sections 4, 6.3, 6.4, 8, 9, 10, 11, 13).
2. The model list of section 9 (needed already before PR 2).
3. Layer 1 facts marked unverified in section 3, from inspection of the real
   files (PR 2).
4. Whether "correct" for layer 1 can be tightened from target-grounded to
   target-grounded with a matching gold value (section 6.3).
5. The faithfulness checker and its pinned version (PR 5).
6. The decomposer reference procedure (PR 3).
7. GRAB-RAG licence, format and document roles (PR 7); SealQA subset, fields and
   correctness rule (PR 8).
8. Whether H4 gets a non-inferiority margin instead of "point estimate ≥ 0".
9. Who performs the human audit.

Items 3-7 are resolved on dev data only. The freeze happens after they are
closed and before any test-split access; layers may be frozen in stages
(`prereg-freeze-1` for layer 1, later tags for layers 2 and 3) provided each
stage is frozen before its own test split is used.

## 18. Amendment log

Rules: after the freeze this document changes only by adding a row here, in the
same commit as the change. Each row states what changed, why, and whether any
test-split result had been seen by anyone involved at that time. Before the
freeze, changes are ordinary edits and need no row.

| # | Date | Section | Change | Reason | Test results seen before the change? |
|---|---|---|---|---|---|
| (none yet) | | | | | |
