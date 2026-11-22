# Architecture

This document describes the design of binding-gate. All ten planned pull
requests are merged: PR 1 (skeleton, frozen data contracts, pre-registration),
PR 2 (Deceptive Grounding adapter, model clients, parser, `no_gate`, provenance
scorer, runner, baseline report; section 8a), PR 3 (decomposer and its separate
measurement), PR 4 (checks, verdict, `gated` arm, calibration), PR 5 (comparison
arms, LLM-judge scorer, ablation configs), PR 6 (statistics, plots, frozen test
run), PR 7 (GRAB-RAG, layer 2), PR 8 (SealQA live-web layer, re-search loop,
snapshots), PR 9 (MCP server) and PR 10 (public report, human-audit tool).
PR 11 refreshed the documentation, added the operating guide `docs/RUNNING.md`
and let `cli analyze` handle layer-3 runs. PR 12 closed engineering gaps before
the keys are connected: decomposer cache preload on resume, seeding a run's
response cache from earlier dev runs (section 8a), a public read-only lock
check (section 6), the complete `configs/test_layer1.toml`, the MiniCheck
preparation step of the workflow, and per-model judge budgets. PR 13 fitted the
paid plan into a hard USD 50 budget (`configs/budget.toml`; PREREGISTRATION.md
section 9): pre-registered stratified subsamples (`sampling.py`, `[sample]`
table), the `seeds/*` seed wildcard and seed-aware dry-run estimates, a web
search quota (`[runner] max_searches`), per-model reasoning assumptions in the
price file, and a DeepSeek client (`models/deepseek_chat.py`). "PR n" below
names the PR that added a part. No model has been run yet; the pre-registration
is still DRAFT.

What is measured and how success is judged is defined in
[PREREGISTRATION.md](PREREGISTRATION.md). This file is about how the code is
organised so that the measurement is reproducible and so that several people (or
coding agents) can work in parallel without inventing incompatible formats.

## 1. Components and data flow

```
 DatasetAdapter ──► Item ──► split (hash of group_id) ──► dev items
   (PR 2,7,8)        │                                    test items (only via testlock)
                     │ item.arm_input()   (gold, group and stratum removed)
                     ▼
                ┌─ Arm ─────────────────────────────────────────────────────────┐
                │                                                               │
                │  question ──► Decomposer (A) ──► QuestionConstraints          │
                │                 (model, PR 3)                                 │
                │  question + documents ──► ModelClient ──► raw text            │
                │                              (PR 2)         │ parser (PR 2)   │
                │                                             ▼                 │
                │                                      AnswerCandidate (B)      │
                │                                             │ only if answered│
                │                                             ▼                 │
                │  Gate (E) runs BindingChecks (C, D) ──► CheckResult* ──► Verdict
                │   (deterministic, PR 4)                 ACCEPT / RESEARCH / ABSTAIN
                │                                             │                 │
                │        RESEARCH + live web: re-search loop (PR 8) ──► new Attempt
                │                                             ▼                 │
                └──────────────────────────────────────► ArmOutput (final_answer)
                                                              │
                     Item (with gold) + final_answer ──► Scorer ──► ScoreResult
                                                              │       (PR 2, 7, 8)
                                                              ▼
                                   TraceRecord ──► TraceWriter ──► runs/<run_id>/traces.jsonl
                                                                   runs/<run_id>/manifest.json
                                                              │
                                                              ▼
                                         statistics, plots, report (PR 6, 10)
```

One run = one config file. For every item × arm × model × repeat the runner
(PR 2) calls the arm, scores the final answer and appends exactly one
`TraceRecord`. Statistics read trace files only; they never call models.

### Determinism boundary

| Step | Uses a model? | Requirement |
|---|---|---|
| Decomposer (A) | yes | every call logged; accuracy measured separately (PR 3) |
| Answering model | yes | every call logged |
| Answer parser (B) | no | pure function of the raw text |
| Binding checks (C), premise matching (D) | no | pure functions; same input, same output on any machine |
| Verdict (E) | no | pure function of the check results |
| Web search and page fetch (layer 3) | no (external service) | every search response, robots.txt and page stored as a snapshot; the gate and the scorer read only stored snapshots |
| Primary scorers (`provenance`, `grab_rag_answer`, `sealqa_answer`) | no | string matching with `textnorm.normalize_text` |
| Secondary scorer (LLM judge) | yes | cross-check only; never decides a hypothesis |
| Statistics, report, audit analysis | no | read trace and analysis files only |

## 2. Repository layout and ownership

```
ARCHITECTURE.md, PREREGISTRATION.md, README.md, LICENSE, NOTICE, pyproject.toml
audit/test_access_log.jsonl      committed log of every test-split access attempt (empty today)
configs/                         versioned run configs, one TOML file per run (dev_*, test_layer1)
configs/prices.toml              pinned price file used by every cost estimate and budget cap  PR 2
configs/decomposer_eval_dev.toml config of the decomposer measurement (not a run config)      PR 3
configs/rescore/                 re-scoring configs of existing traces (LLM judge)            PR 5
configs/mcp/                     example configuration of the MCP server                      PR 9
docs/                            RUNNING.md (full operating guide), RUNNING_BASELINE.md, MCP.md PR 2, 9, 11
src/binding_gate/
  contracts.py   schema_lock.py  frozen data contracts + their fingerprint      PR 1
  interfaces.py                  Protocols for all component kinds              PR 1
  registry.py                    registration by adding a module                PR 1
  config.py                      run config loading and hashing                 PR 1
  split.py       testlock.py     dev/test split and the test-set lock           PR 1
  trace.py                       append-only traces and run manifest            PR 1
  textnorm.py                    the one normalisation for "verbatim"           PR 1
  canonical.py   gitinfo.py      canonical JSON / hashing, git helpers          PR 1
  secrets.py                     the only place environment variables are read  PR 1
  parsing.py     answering.py    answer format + parser; shared answer step     PR 2
  runner.py      cli.py          the run loop, budget caps, dry run; commands   PR 2
  pricing.py     baseline_report.py  cost model; section 10 stop-rule report    PR 2
  judging.py                     judge-family rule and judge plumbing           PR 5
  frozen_run.py                  the one-command frozen test run                PR 6
  datasets/      one module per dataset adapter                                 PR 2, 7, 8
  models/        one module per model client (plain HTTPS)                      PR 2
  decomposers/   gate function (A), its cache, the layer-1 reference            PR 3
  evaluation/    separate measurement of the decomposer                         PR 3
  checks/        gate functions (C) and (D), one module per check               PR 4
  gates/         gate function (E) (`binding`), dev-only calibration            PR 4
  arms/          one module per experimental arm                                PR 2, 4, 5, 8
  scorers/       one module per scorer                                          PR 2, 5, 7, 8
  stats/         metrics, cluster bootstrap, H1-H4, analysis                    PR 6
  plots/         figures (optional extra `plots`)                               PR 6
  research/      layer 3: search client, fetcher, robots, snapshots, re-search  PR 8
  mcp/           the gate as an MCP server (optional extra `mcp`)               PR 9
  report/        public report generator (section 15)                           PR 10
  audit_tool/    human audit: sample, label, analyse (section 13)               PR 10
tests/                           one or more test files per module
.github/workflows/tests.yml      pytest on Python 3.11 and 3.13
.github/workflows/baseline.yml   manually started dev runs (dry run, smoke, paid run) PR 2, 8, 12
```

Ownership as delivered. Each PR added files in the places listed and did not
modify files owned by another PR of the same wave (waves: [1] → [2] →
[3, 4, 5, 6, 7] → [8, 9, 10]).

| PR | Wave | Added |
|---|---|---|
| 2 | B | `datasets/deceptive_grounding.py`, `models/*`, `arms/no_gate.py`, `scorers/provenance.py`, `parsing.py`, `answering.py`, `runner.py`, `cli.py`, `pricing.py`, `baseline_report.py`, `configs/dev_baseline.toml`, `configs/dev_smoke.toml`, `configs/prices.toml`, `.github/workflows/baseline.yml`, `docs/RUNNING_BASELINE.md`, `NOTICE` |
| 3 | C | `decomposers/*` (`model_decomposer`, cache, prompt format, layer-1 reference), `evaluation/decomposer.py`, `configs/decomposer_eval_dev.toml` |
| 4 | C | `checks/*`, `gates/binding.py`, `gates/calibration.py`, `arms/gated.py`, `configs/dev_gated.toml` |
| 5 | C | `arms/prompt_entity_check.py`, `arms/faithfulness_checker.py`, `arms/llm_judge_checks.py`, `scorers/llm_judge.py`, `judging.py`, `configs/dev_baselines.toml`, `configs/dev_ablation_*.toml`, `configs/rescore/dev_llm_judge.toml` |
| 6 | C | `stats/*`, `plots/*`, `frozen_run.py`, `configs/test_layer1.toml` (still a draft, section 17 of the pre-registration) |
| 7 | C | `datasets/grab_rag.py`, `scorers/grab_rag_answer.py`, `configs/dev_grab_rag_*.toml` |
| 8 | D | `research/*`, `datasets/sealqa.py`, `arms/gated_research.py` (arms `gated_research` and `no_gate_live`), `scorers/sealqa_answer.py`, `configs/dev_live*.toml`, auxiliary-call pricing in `runner.py` |
| 9 | D | `mcp/*`, `configs/mcp/server.example.toml`, `docs/MCP.md` |
| 10 | D | `report/*`, `audit_tool/*` |

**Shared files.** There is no registry file that every PR edits. The only files
more than one PR may touch are: `pyproject.toml` (append one line under
`[project.optional-dependencies]` for your own extra), `secrets.py` (append one
entry to `KNOWN_SECRETS`), `.gitignore`, and the "Contract change log" at the end
of this file. Each is append-only by convention, so merges are trivial.

## 3. Frozen contracts

All defined in `src/binding_gate/contracts.py` as immutable, self-validating
dataclasses with exact JSON round-trips (`to_dict`/`from_dict`,
`to_json`/`from_json`). Unknown or missing keys are errors.

| Contract | Fields |
|---|---|
| `Entity` | `name`, `aliases[]`, `kind?` |
| `Qualifier` | `kind` (time, place, version, unit, other), `text`, `aliases[]` |
| `Presupposition` | `text`, `key_terms[]` |
| `QuestionConstraints` | `question`, `entity`, `attribute`, `qualifiers[]`, `presuppositions[]` |
| `Document` | `id`, `text`, `sha256` (of text, verified), `url?`, `metadata{}` (no gold) |
| `EvidenceRef` | `id`, `sha256`, `url?` |
| `AnswerCandidate` | `status` (answered, abstained, unparseable), `raw`, `value?`, `quote?`, `source_id?`, `reason?` |
| `CheckResult` | `check`, `status` (pass, fail, not_applicable), `details{}` |
| `Verdict` | `decision` (accept, research, abstain), `reasons[]`, `check_results[]`, `research_request?` |
| `Item` | `id`, `group_id`, `dataset`, `question`, `documents[]`, `gold{}`, `stratum?`, `schema_version` |
| `ArmInput` | `item_id`, `question`, `documents[]` |
| `Message` | `role`, `content` |
| `TokenUsage` | `input_tokens`, `output_tokens` |
| `ModelRequest` | `messages[]`, `params{}` |
| `ModelResponse` | `text`, `model`, `usage?`, `raw{}` |
| `ModelCall` | `purpose`, `client`, `request`, `started_at`, `ended_at`, `response?` xor `error?` |
| `Decomposition` | `constraints`, `model_calls[]` |
| `Attempt` | `answer`, `evidence[]` (EvidenceRef), `verdict?` |
| `ArmOutput` | `attempts[]`, `final_answer`, `constraints?`, `model_calls[]` |
| `ScoreResult` | `scorer`, `label`, `details{}` |
| `TraceRecord` | `run_id`, `config_hash`, `git_commit`, `item_id`, `group_id`, `dataset`, `split`, `arm`, `model`, `repeat`, `started_at`, `ended_at`, `stratum?`, `constraints?`, `attempts[]`, `final_answer?`, `model_calls[]`, `scores[]`, `error?`, `schema_version` |
| `RunManifest` | `run_id`, `config{}`, `config_hash`, `git_commit`, `git_dirty`, `package_version`, `python_version`, `started_at`, `config_path?`, `test_unlock_tag?`, `ended_at?`, `trace_count?`, `traces_sha256?`, `schema_version` |

Prompts, raw responses and token usage are in `TraceRecord.model_calls`
(each `ModelCall` holds the full request and response).

### Invariants enforced by the contracts

- An `answered` candidate has value, quote and source id; the other two statuses
  have none. Unparseable output is a first-class status, not a missing record.
- A `Verdict` cannot be `accept` while any recorded check failed; `research`
  carries a `research_request`; `research` and `abstain` carry reasons.
- A gate verdict can only be attached to an `answered` candidate. Abstentions
  and unparseable outputs pass through a gated arm unchanged, so a gate cannot
  hide format failures.
- `final_answer` is the last attempt's candidate if it was accepted or not
  gated, and an abstention otherwise. Only a `research` verdict may be followed
  by another attempt.
- A `TraceRecord` has either an outcome or an `error`, never both, never neither.
- `Document.sha256` must match its text; `RunManifest.config_hash` must match its
  config.

### Versioning

`contracts.SCHEMA_VERSION` (currently 1) is stored in every `Item`,
`TraceRecord` and `RunManifest`; loading a record with any other version fails.
Other contracts stored on their own use
`to_envelope()` → `{"schema_version", "kind", "data"}`.

`schema_fingerprint()` hashes the shape of all contracts (field names, declared
types, which fields are required, enum values). `schema_lock.py` records the
fingerprint per version and `tests/test_schema_version.py` fails when the
contracts stop matching the fingerprint of the current version. To change a
contract: edit it, bump `SCHEMA_VERSION`, add the new fingerprint line, and add a
row to the change log in section 10. A PR in a parallel wave that needs a
contract change must land it as its own small PR first, so the other PRs can
rebase onto it.

### Reserved names (frozen so that parallel PRs agree)

Registered components (`registry.available(kind)`) and the names the analysis
reads, as of PR 11:

| What | Names |
|---|---|
| Datasets | `deceptive_grounding`, `grab_rag`, `sealqa` (`sealqa` declares `pin_method = "prepare"`: downloaded and converted by `cli dataset-prepare`; no `pin_method` means a git clone) |
| Model clients | `openai_responses`, `anthropic_messages`, `google_gemini`, `moonshot_chat` |
| Decomposers | `model_decomposer` |
| Checks | `quote_verbatim`, `entity_in_window`, `qualifiers_in_window`, `no_closer_competitor`, `premise_present` |
| Gates | `binding` |
| Arms | `no_gate`, `no_gate_live` (layer-3 baseline, run with label `no_gate`), `prompt_entity_check`, `faithfulness_checker`, `llm_judge_checks`, `gated`, `gated_research` |
| Scorers | `provenance` (deterministic, primary, layers 1-2), `grab_rag_answer` (deterministic, layer 2), `sealqa_answer` (deterministic, primary, layer 3), `llm_judge` (secondary) |
| Search clients (not a registry kind) | `serpapi_google` (`research/search.py`, `SEARCH_CLIENTS`) |
| Arm labels read by the statistics and the audit | `no_gate` (reference), `gated` (H1, H2), `faithfulness_checker` (H3), `gated_research` (H4); ablations `gated_minus_<check>` in the dev configs and in `configs/test_layer1.toml` (one scheme since PR 12) |
| `provenance` labels | `target_only`, `competitor_only`, `both`, `neither`, plus `abstained`, `unparseable` |
| `grab_rag_answer` labels | `correct`, `misled`, `ambiguous`, `other_wrong`, `abstained`, `unparseable` |
| `sealqa_answer` labels | `correct`, `incorrect`, `abstained`, `unparseable` |
| `ModelCall.purpose` | `answer`, `decompose`, `check` (LLM-judge arm), `judge` (secondary scorer), `search`, `fetch` (layer 3) |
| Gold keys | `gold["document_roles"]` (map of document id → `"target"`, `"competitor"` or `"other"`; `provenance`, `grab_rag_answer`), `gold["answers"]` (`grab_rag_answer`, `sealqa_answer`), `gold["planted_answer"]` (`grab_rag_answer`) |

Conventions that go with them:

- **Ablations are config, not code.** The `gated` arm takes
  `options.checks = [<check names>]`. A per-check ablation is a config that lists
  the arm twice with different `label`s and one check removed. PR 5 therefore
  needs no change to PR 4's code.
- **Nested components** are named in arm options as `{name = "...", options = {...}}`
  and built by the arm through `registry.create`. The runner passes the model
  client under test to the arm factory as the keyword argument `model`.
- **Gold never reaches an arm.** Adapters put anything that reveals the answer,
  including which document is about which entity, in `Item.gold`, not in
  `Document.metadata`. How the gate finds candidate competing entities for
  `no_closer_competitor` was PR 4's design decision (the lead term of each shown
  document, pre-registration section 5.1); it is deterministic and uses only the
  question, the constraints and the document text.
- **Dataset field names are not part of any contract.** The PR 2, 7 and 8
  adapters map whatever the real files contain onto `Item`.

## 4. Extension points

Seven component kinds, each a `typing.Protocol` in `interfaces.py`:
`DatasetAdapter`, `ModelClient`, `Decomposer`, `BindingCheck`, `Gate`, `Arm`,
`Scorer`.

To add a component, create a new module in the matching sub-package and decorate
its factory:

```python
# src/binding_gate/checks/quote_verbatim.py
from binding_gate.registry import register

@register("check", "quote_verbatim")
class QuoteVerbatim:
    name = "quote_verbatim"
    def check(self, constraints, answer, documents): ...
```

`registry.create("check", "quote_verbatim", **options)` imports every module of
`binding_gate.checks` on first use, so adding the file is the registration.
Registering the same name twice raises an error.

## 5. Configuration

- One TOML file per run under `configs/`, selected by path. Format and
  validation: `config.py`; example: `configs/example.toml`.
- The **config hash** is the SHA-256 of the canonical JSON of the parsed,
  normalised config. Comments and formatting do not change it; any change of
  meaning does. It is stored in the manifest and in every trace record.
- **No experiment setting is read from an environment variable.** Environment
  variables are for secrets only, and `secrets.py` is the single place where
  they are listed and read. Secrets never appear in configs, traces or logs.
- Dev runs are started from the manually triggered GitHub Actions workflow
  `.github/workflows/baseline.yml`, whose inputs are the config path, a model
  label, a dry-run box, a spend confirmation and (PR 12) the id of the workflow
  run that holds the config's seed runs, if it names any, with API keys taken from
  repository secrets, so that a run is started by choosing a file rather than by
  editing settings (operating guide: `docs/RUNNING.md`). The frozen test run,
  the decomposer measurement, calibration, re-scoring, analysis, audit and
  report are command-line tools only.

## 6. Split and test-set lock

- `split.assign_split(dataset, group_id)` hashes the group id with a
  pre-registered salt (PREREGISTRATION.md section 4). No split file exists;
  all conditions of one group always share a split.
- `split.select_dev(items)` returns dev items. There is deliberately no public
  function returning test items except `testlock.load_test_split`, which
  requires: a named tag `prereg-freeze-*`; HEAD equal to that tag; a clean
  working tree; `PREREGISTRATION.md` status FROZEN; a stated purpose.
- `testlock.check_unlock` (PR 12) evaluates the same conditions read-only: it
  reports why the lock would refuse and writes nothing, so it is not an access.
  `frozen_run.py` uses it to stop before any preflight or paid call; the access
  itself, and the log entry of a refusal, still go through `load_test_split`.
- Every call of `load_test_split`, granted or refused, appends a hash-chained line to
  `audit/test_access_log.jsonl`. The log is committed after each test run; CI
  verifies the chain and that no access was granted while the status is DRAFT.
- Limitation: this is auditability, not access control. Someone determined can
  call `assign_split` directly, read the data by hand, or rewrite the log before
  committing. The lock prevents accidental use and makes supported use visible.

## 7. Traces

- `TraceWriter.start(runs_dir, config, repo_root=...)` creates
  `runs/<run_id>/`, writes `manifest.json`, and opens `traces.jsonl` in append
  mode. It refuses an existing directory, foreign records and duplicate
  `(item, arm, model, repeat)` keys. Each record is flushed and fsynced.
- `close()` completes the manifest with end time, record count and the SHA-256
  of the trace file. A run that died keeps an incomplete manifest and can be
  continued with `TraceWriter.resume` under the same config hash and commit.
- Re-scoring writes `scores.<scorer>.jsonl` beside the trace and never edits it.
- `runs/` is git-ignored. Result sets that back a published number are added
  deliberately together with their manifests; the location
  (`results/layer1/<run_id>/` is proposed) is still open (pre-registration
  section 17).
- Live-web runs also hold `runs/<run_id>/snapshots/` (pre-registration
  section 14); the workflow uploads such run directories only encrypted.

## 8. Requirements on the PR 2 runner

These are fixed here because every later PR relies on them.

1. Loop over item × arm × model × repeat; catch arm exceptions and write a
   `TraceRecord` with `error` set instead of skipping the item.
2. Hand arms `item.arm_input()` only.
3. Within a run, reuse the response to an identical `ModelRequest` for the same
   client and repeat index, so that arms sharing the answer prompt are compared
   on the same candidate answer (PREREGISTRATION.md section 5). A dev run may
   also reuse, by explicit opt-in, the answers of earlier dev runs under the same
   key (PR 12; section 8a, "Seeding from earlier runs").
4. Dev runs use `split.select_dev`; test runs use `testlock.load_test_split`
   and record the tag in `RunManifest.test_unlock_tag`.
5. Retries on transport errors are bounded, configured in the run config, and
   every failed call is logged as a `ModelCall` with `error`.
6. No output of an arm is ever discarded or repaired silently.

## 8a. What PR 2 delivered, and the conventions later PRs must follow

- **Answer step shared by all arms** (`answering.py`). An arm that starts from
  the model's own answer builds its request with `build_answer_request(arm_input,
  presentation)` and sends it with `call_model(model, request, purpose)`.
  Identical requests are what makes the runner's cache hand `gated`,
  `faithfulness_checker` and `llm_judge_checks` the candidate that `no_gate`
  produced. A *presentation* (selected by name in the arm options) is the
  benchmark-specific prompt layout; PR 7 and PR 8 added theirs
  (`grab_rag_context`, `sealqa_live_web`) to `answering.PRESENTATIONS`.
- **Retries** happen only in `call_model`, under the client's `retry` policy
  (from `[models.options]`); every attempt is a `ModelCall`. When all attempts
  fail it raises `ModelCallFailed`, which carries the logged calls; an arm lets
  it propagate and the runner writes the error record with those calls.
- **Component construction.** Models: `factory(label=<config label>, **options)`;
  the label becomes `client.name`. Arms: `factory(model=<client>, **options)`.
  Scorers and datasets: `factory(**options)`. A model client may offer
  `preflight()` (free existence check), `retry`, `model_id`, `secret_name` and
  `max_output_tokens`; the runner uses them when present.
- **Model clients** are plain HTTPS with the standard library (no SDKs; reasons
  in `models/_http.py`). `ModelResponse.model` is the provider-reported id;
  `ModelResponse.raw` holds `requested_model`, `finish_reason`, `truncated`,
  `reasoning_tokens`, the provider's usage object and the sampling parameters
  that were sent. `TokenUsage.output_tokens` is the billed output including
  hidden reasoning tokens.
- **Cache.** Key = SHA-256 of `{client label, repeat, request}`. A reused
  response is flagged `raw["cache_hit"] = true` and is not charged. On resume
  the cache is rebuilt from the trace, and (PR 12) each `model_decomposer`'s
  process cache is seeded from the decompose calls already in the trace
  (`ModelDecomposer.preload_from_call`, which re-parses each recorded reply), so
  no question is decomposed twice in one run and every segment uses the same
  constraints. The resume entry of `invocations.jsonl` records the counts
  (`decompositions_preloaded`).
- **Seeding from earlier runs (PR 12, opt-in).** `[runner] seed_responses_from =
  ["seeds/<run id>", ...]` (paths relative to the repository root) loads the
  successful, non-cached `answer` calls of the model under test from earlier dev
  run directories into the cache, under the same key as the live cache (model
  label, repeat, request), after the run's own trace on resume; the first listed
  seed wins a duplicate key. A run that sends the same answer request for the
  same model label and repeat then reuses that response: it is recorded with
  `raw["cache_hit"] = true` and `raw["seeded_from"] = <seed run id>` and costs
  nothing. This is how `dev_gated`, the ablations and the comparison baselines
  reuse the `no_gate` answers of the dev baseline. Refused, before any paid call
  and before a run directory exists (`runner.load_seed_responses`): any seeding
  in a test run; a seed that is not a dev run (manifest split, test unlock tag
  or any record's split); a different dataset spec or dataset fingerprint (from
  the seed's `invocations.jsonl`); a different model spec for a selected label
  (transport options `timeout_seconds`, `max_retries`, `backoff_seconds`,
  `max_backoff_seconds` aside); a live-web config or seed; a seed whose answer
  calls match no request of this run (a different answer prompt or
  presentation); a closed seed whose `traces.jsonl` no longer matches its
  manifest hash; the run seeding itself. The dry run lists the seeded responses
  per model; the `started`/`resumed` entries of `invocations.jsonl` record each
  seed. The workflow downloads seed runs into `seeds/` from the `runs-*`
  artifacts of the workflow run named in its `seed_run_id` input. PR 13: the
  entry `"seeds/*"` (alone) stands for every directory under `seeds/` with a
  `manifest.json`, so the committed dev configs name their seeds before the run
  ids exist; a paid run refuses when it matches nothing; the dry run leaves the
  seeded answer calls out of its call counts and estimates (and therefore out of
  the low-estimate check against the cap) and says so when no seed was found.
- **Subsamples (PR 13).** The optional top-level `[sample]` table (`per_stratum`,
  `seed`; hashed when present; allowed on the test split, not combined with
  `limit`) keeps, after the split has been selected, the `per_stratum` items with
  the smallest SHA-256(`"<seed>\n<item id>"`) in every stratum
  (`binding_gate.sampling`). The analysis labels a run with a subsample
  exploratory. Re-scoring configs accept the same table.
- **Search quota (PR 13).** `[runner] max_searches` caps the non-cached `search`
  calls of one invocation, lowered to the searches the SerpApi account check
  reports as left; a unit that could exceed it (counted at the sum of
  `max_attempts` of the live-web arms, units in flight included) is not started
  and the run stays unfinished and resumable (`RunSummary.search_limit_reached`).
- **`[runner]` config table** (optional, hashed when present): `concurrency`,
  `max_usd` (hard cap per model label), `max_usd_by_model`, `prices`,
  `seed_responses_from` (PR 12), `max_searches` (PR 13).
  `config.py` passes it through; `runner.RunnerSettings` validates it.
- **Run directory** additionally holds `invocations.jsonl` (one line per start,
  resume and end of the runner: selected models, dataset fingerprint, price
  file hash, spend).
- **Provenance scorer labels**: the four reserved labels, plus `abstained` and
  `unparseable` for final answers that are not scoreable answers;
  `details["outcome"]` is the outcome category of PREREGISTRATION.md 6.3. The
  minimum quote length is applied by the scorer.
- **Test split.** The runner reaches test items only by calling
  `testlock.load_test_split` (`--test-unlock-tag`, `--purpose`); PR 6 built the
  one-command frozen test run on top of that (`frozen_run.py`).
- **Split unit for Deceptive Grounding.** `Item.group_id` is the normalised
  (X, C) pair (`datasets.deceptive_grounding.pair_group_id`), not the triple;
  the triple id is in `gold["triple_id"]`. Statistics (PR 6) must cluster on
  `group_id`.
- **Baseline report** (`baseline_report.py`, `cli report`) implements only the
  section 10 stop rule from traces. It is not the statistics package (PR 6).

## 9. Out of scope

- No general fact-checking: the gate does not decide whether a source is true,
  only whether the evidence is about what the question asked.
- No retrieval or ranking improvements; the re-search loop (PR 8) only reissues
  a targeted query.
- No model training or fine-tuning.
- No free-text answers: every arm must produce the structured answer.
- No semantic or embedding-based matching in the gate's checks; they are
  deliberately string-based and deterministic, with the weaknesses listed in the
  pre-registration's threats to validity.
- No redistribution of third-party data: benchmark files are downloaded by the
  user into `data/raw/` (git-ignored); live page snapshots are private unless
  their licence allows publication; the Stanford news-intermediary outputs
  (arXiv 2605.22785, no licence file) are never redistributed.
- No access control around the test split (see section 6).
- No production hardening of the MCP server (PR 9) beyond what the experiment
  needs.

## 10. Contract change log

| Schema version | Date | Change | Migration |
|---|---|---|---|
| 1 | 2026-11-22 | Initial contracts (PR 1). | none |

PR 2 (2026-11-22) changed no contract; the schema version and fingerprint are
unchanged. It added one optional run-config table, `[runner]` (see section 8a),
which is not a data contract; configs without it keep their hash.

PRs 3-12 (2026-11-22) changed no contract: `contracts.py` and `schema_lock.py`
are as in PR 1, `SCHEMA_VERSION` is 1 and the fingerprint is unchanged. New
names (arms, scorers, purposes, gold keys; section 3), new files written
beside a run (`scores.<scorer>.jsonl`, `snapshots/`, `frozen_run.jsonl`,
`analysis/`), the PR 12 response flag `raw["seeded_from"]` and the PR 12
`[runner]` key `seed_responses_from` are conventions, not data contracts. PR 13
changed no contract either: the `[sample]` table, `[runner] max_searches`, the
optional per-price `reasoning_tokens_high` and the `deepseek_chat` client are
config and component additions.
