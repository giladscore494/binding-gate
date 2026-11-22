# Pre-registration: binding-gate

**Status:** DRAFT

**Document version:** 0.2 (2026-11-22, PR 2)

This document fixes, before any model is run, what will be measured, how, and
what counts as success or failure. While the status is DRAFT everything in it
may still change and nothing in it has been approved. It becomes binding when the
owner approves it, the status line is changed to `FROZEN`, and that commit is
tagged `prereg-freeze-<n>`. From then on changes are made only through the
amendment log (section 18).

**State of the project at version 0.2 (PR 2):** no model has been called and no
result of any kind exists. The Deceptive Grounding benchmark has been cloned at
a pinned commit and its files inspected for structure (section 3); the
structural statistics quoted there were computed over all 264 triple files,
because the split is applied by code that reads every file, but passages were
read by a person or coding agent only for dev triples, and no test-split item
has been shown to a model. The model clients written in PR 2 have been tested
against recorded-shape fixtures only; their behaviour against the live APIs is
**unverified** until the smoke run. Statements about the other datasets still
come from their public descriptions; where a detail has not been checked against
the real files it is marked **unverified**.

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
| 2. Controlled, other domain | GRAB-RAG (arXiv 2608.22228; code stated at github.com/andre44s/grab-rag), misleading-context condition | Generalisation (secondary) | **unverified** | Repository not yet opened |
| 3. Live web | SealQA (Hugging Face `vtllms/sealqa`) | Confirmatory for H4 | Apache-2.0 | Not yet downloaded |

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
  **(X, C) pair** (queried drug, disease), so all conditions of all triples
  that share a question are always in the same split. Reason: the 264 triples
  cover only 131 pairs, and triples of one pair differ only in the competitor Y
  while sharing the question and the completing-information targets; with the
  triple as the unit, 46 pairs would have had the same question in both dev and
  test. The group id is `normalize_text(X) + " || " + normalize_text(C)` with
  the normalisation of section 6.1 (`pair_group_id` in
  `src/binding_gate/datasets/deceptive_grounding.py`); the triple id is kept in
  the item's gold metadata. Decided on 2026-11-22, before any model output was
  scored (section 17, item 10). The salt, the buckets and the rule above are
  unchanged. For
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
  `group_id`: the (X, C) pair for layer 1, the question for layer 2, the row for
  layer 3. Resampling groups respects the dependence between the conditions of
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

1. Owner approval of every PROPOSED value (sections 4, 6.3, 6.4, 8, 9, 10, 11, 13).
2. The model list of section 9 (needed already before PR 2). *PR 2: filled in
   as a working selection; needs owner confirmation, and every id must pass the
   live preflight (slots B and F are explicitly unverified).*
3. Layer 1 facts marked unverified in section 3, from inspection of the real
   files (PR 2). *PR 2: resolved; see section 3, facts 1-11.*
4. Whether "correct" for layer 1 can be tightened from target-grounded to
   target-grounded with a matching gold value (section 6.3). *PR 2: resolved, it
   cannot; the files contain no gold answer values.*
5. The faithfulness checker and its pinned version (PR 5).
6. The decomposer reference procedure (PR 3).
7. GRAB-RAG licence, format and document roles (PR 7); SealQA subset, fields and
   correctness rule (PR 8).
8. Whether H4 gets a non-inferiority margin instead of "point estimate ≥ 0".
9. Who performs the human audit.
10. (PR 2) Whether the split and bootstrap unit for layer 1 stays the triple or
    becomes the (X, C) pair, given that with the triple as the unit 46 pairs
    would have triples on both sides of the split. *Closed: the unit is the
    (X, C) pair (sections 3, 4 and 8). 2026-11-22: decided by the orchestrator
    under the owner's delegation, before any model output was scored.*
11. (PR 2) Live confirmation of the model clients and settings by the smoke
    run: model ids, whether each API accepts `temperature = 0`, reported usage
    including reasoning tokens. Any change is recorded in section 9.
12. (PR 2) Confirmation of the prices in `configs/prices.toml` against the
    providers' official pricing pages or invoices.
13. (PR 2) Whether the provisional parser and answer-format instruction of
    section 6.2 are kept as they are after the dev baseline.

2026-11-22: owner directed that all PRs follow this document and delegated
model selection; PROPOSED values are adopted as working values for PR 2 and
still require explicit confirmation at freeze.

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
