# Architecture

This document describes the design of binding-gate across all ten planned pull
requests. PR 1 delivered the skeleton, the frozen data contracts and the
pre-registration. PR 2 delivered the Deceptive Grounding adapter, the model
clients, the answer parser, the `no_gate` arm, the provenance scorer, the runner
and the baseline report (section 8a). Everything marked "PR n" with n ≥ 3 does
not exist yet.

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
                                                              │       (PR 2, 5)
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
| Primary scorer | no | string matching with `textnorm.normalize_text` |
| Secondary scorer (LLM judge) | yes | cross-check only; never decides a hypothesis |

## 2. Repository layout and ownership

```
ARCHITECTURE.md, PREREGISTRATION.md, README.md, LICENSE, pyproject.toml
audit/test_access_log.jsonl      committed log of every test-split access attempt
configs/                         versioned run configs (one TOML file per run)
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
  datasets/      one module per dataset adapter                                 PR 2, 7, 8
  models/        one module per model client                                    PR 2
  decomposers/   gate function (A)                                              PR 3
  checks/        gate functions (C) and (D), one module per check               PR 4
  gates/         gate function (E)                                              PR 4
  arms/          one module per experimental arm                                PR 2, 4, 5, 8
  scorers/       one module per scorer                                          PR 2, 5
tests/                           one or more test files per module
.github/workflows/tests.yml      pytest on Python 3.11 and 3.13
.github/workflows/baseline.yml   manually started dry run / smoke run / dev baseline   PR 2
```

Ownership of later work. Each PR adds new files in the places listed and does
not modify files owned by another PR of the same wave.

| PR | Wave | Adds |
|---|---|---|
| 2 | B | `datasets/deceptive_grounding.py`, `models/*`, `arms/no_gate.py`, `scorers/provenance.py`, `parsing.py` (answer format + parser), `runner.py`, `cli.py`, `configs/dev_baseline*.toml`; as delivered also `answering.py`, `pricing.py`, `baseline_report.py`, `configs/dev_smoke.toml`, `configs/prices.toml`, `.github/workflows/baseline.yml`, `docs/RUNNING_BASELINE.md`, `NOTICE` |
| 3 | C | `decomposers/*`, `evaluation/decomposer.py`, its configs |
| 4 | C | `checks/*`, `gates/*`, `arms/gated.py` |
| 5 | C | `arms/prompt_entity_check.py`, `arms/faithfulness_checker.py`, `arms/llm_judge_checks.py`, `scorers/llm_judge.py`, ablation configs |
| 6 | C | `stats/*`, `plots/*`, `configs/test_*.toml`, the one-command frozen test run |
| 7 | C | `datasets/grab_rag.py`, its configs |
| 8 | D | `research/*` (re-search loop, search client, snapshot store), `datasets/sealqa.py`, `arms/gated_research.py` |
| 9 | D | `mcp/*` (server packaging) |
| 10 | D | `report/*`, `audit_tool/*` |

Waves: [1] → [2] → [3, 4, 5, 6, 7 in parallel] → [8, 9, 10 in parallel].

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

| What | Names |
|---|---|
| Datasets | `deceptive_grounding`, `grab_rag`, `sealqa` |
| Arms | `no_gate`, `prompt_entity_check`, `faithfulness_checker`, `llm_judge_checks`, `gated`, `gated_research` |
| Checks | `quote_verbatim`, `entity_in_window`, `qualifiers_in_window`, `no_closer_competitor`, `premise_present` |
| Scorers | `provenance` (deterministic, primary), `llm_judge` (secondary) |
| `provenance` labels | `target_only`, `competitor_only`, `both`, `neither` |
| `ModelCall.purpose` | `answer`, `decompose`, `judge`, `check` |
| Gold key read by `provenance` | `gold["document_roles"]`: map of document id → `"target"`, `"competitor"` or `"other"` |

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
  `no_closer_competitor` is PR 4's design decision; it must be deterministic and
  must use only the question, the constraints and the document text.
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
- Recommended for PR 2 and PR 6: a manually triggered GitHub Actions workflow
  whose inputs are the config path (and, for test runs, the freeze tag), with
  API keys taken from repository secrets, so that a run is started by choosing a
  file rather than by editing settings.

## 6. Split and test-set lock

- `split.assign_split(dataset, group_id)` hashes the group id with a
  pre-registered salt (PREREGISTRATION.md section 4). No split file exists;
  all conditions of one group always share a split.
- `split.select_dev(items)` returns dev items. There is deliberately no public
  function returning test items except `testlock.load_test_split`, which
  requires: a named tag `prereg-freeze-*`; HEAD equal to that tag; a clean
  working tree; `PREREGISTRATION.md` status FROZEN; a stated purpose.
- Every call, granted or refused, appends a hash-chained line to
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
  deliberately (location decided in PR 6) together with their manifests.

## 8. Requirements on the PR 2 runner

These are fixed here because every later PR relies on them.

1. Loop over item × arm × model × repeat; catch arm exceptions and write a
   `TraceRecord` with `error` set instead of skipping the item.
2. Hand arms `item.arm_input()` only.
3. Within a run, reuse the response to an identical `ModelRequest` for the same
   client and repeat index, so that arms sharing the answer prompt are compared
   on the same candidate answer (PREREGISTRATION.md section 5).
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
  benchmark-specific prompt layout; PR 7 and PR 8 add theirs to
  `answering.PRESENTATIONS`.
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
  the cache is rebuilt from the trace.
- **`[runner]` config table** (optional, hashed when present): `concurrency`,
  `max_usd` (hard cap per model label), `max_usd_by_model`, `prices`.
  `config.py` passes it through; `runner.RunnerSettings` validates it.
- **Run directory** additionally holds `invocations.jsonl` (one line per start,
  resume and end of the runner: selected models, dataset fingerprint, price
  file hash, spend).
- **Provenance scorer labels**: the four reserved labels, plus `abstained` and
  `unparseable` for final answers that are not scoreable answers;
  `details["outcome"]` is the outcome category of PREREGISTRATION.md 6.3. The
  minimum quote length is applied by the scorer.
- **Test split.** The runner reaches test items only by calling
  `testlock.load_test_split` (`--test-unlock-tag`, `--purpose`); PR 6 builds the
  one-command frozen test run on top of that.
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
