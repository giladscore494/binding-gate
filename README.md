# binding-gate

A verification gate for LLM agents that answer questions from web search or
retrieved documents.

**Status: pre-registration draft. Nothing has been measured yet.** This
repository currently contains the project skeleton, the frozen data formats and
a draft of the study plan. There are no results, and no claim that the gate
works.

## The problem

An agent finds a real, on-topic source and extracts a detail that really is in
that source, but the detail belongs to a *different entity* or to *different
conditions* than the question asked about: the dose of another drug, the figure
for another year, the limit for another product version. The answer is quoted,
sourced and wrong. The Deceptive Grounding paper
([arXiv 2607.09349](https://arxiv.org/abs/2607.09349)) describes this as
entity-attribution failure.

Existing checkers mostly ask: *is the claim supported by the source?* Here it
is. binding-gate asks a different question:
*is the evidence about the same entity and qualifiers as the question?*

## The idea

1. **Decompose the question** into constraints: entity, requested attribute,
   qualifiers (time, place, version, unit, other) and presuppositions. This is
   the only step that uses a model.
2. **Force a structured answer:** value + verbatim quote + source id, or an
   explicit abstention.
3. **Check the binding deterministically:** the quote exists verbatim in the
   source; the question's entity appears near the quote; every qualifier appears
   near the quote; no competing entity is closer to the value.
4. **Check the premises:** what the question takes for granted must be in the
   evidence.
5. **Return a verdict:** ACCEPT, RESEARCH (search again, with a reason) or
   ABSTAIN (with a reason).

Steps 3-5 are plain code: same input, same output, no model call.

## How it will be evaluated

The plan is written down before any experiment is run, in
[PREREGISTRATION.md](PREREGISTRATION.md): hypotheses, datasets, the dev/test
split, the scoring rule, baselines, statistics, a rule for stopping the project
early if current models rarely make this error, and proposed success thresholds.
It is a **draft**; the numeric thresholds in it are proposals that have not been
approved.

Scoring is deterministic: every arm, including the baseline without a gate, must
return value + quote + source, and the quote is matched against the provided
documents to decide which entity's document it came from. An LLM judge is used
only as a secondary cross-check.

The design for all planned work is in [ARCHITECTURE.md](ARCHITECTURE.md).

## What is in this repository today

| Piece | Where |
|---|---|
| Frozen data contracts with a schema version | `src/binding_gate/contracts.py`, `schema_lock.py` |
| Interfaces for datasets, models, decomposer, checks, gate, arms, scorers | `src/binding_gate/interfaces.py` |
| Component registration by adding a module | `src/binding_gate/registry.py` |
| Versioned run configs with a stable hash | `src/binding_gate/config.py`, `configs/` |
| Deterministic, leakage-free dev/test split | `src/binding_gate/split.py` |
| Test-set lock with a committed access log | `src/binding_gate/testlock.py`, `audit/test_access_log.jsonl` |
| Append-only run traces and run manifest | `src/binding_gate/trace.py` |
| The text normalisation used for "verbatim" | `src/binding_gate/textnorm.py` |

Not here yet: dataset adapters, model clients, the decomposer, the checks, the
gate, baselines, statistics. They arrive in later pull requests, listed in
ARCHITECTURE.md.

## Honest limitations

- **Unproven.** No experiment has been run. The gate may not reduce errors, or
  may block too many correct answers. The pre-registration includes a rule to
  stop the project if the baseline failure is too rare to matter.
- **String matching is brittle.** Aliases, abbreviations, pronouns and tables
  can defeat a check that looks for an entity name near a quote.
- **One step still uses a model.** If the question is decomposed wrongly, the
  deterministic checks enforce the wrong constraints.
- **Narrow evidence base.** The main benchmark uses constructed clinical
  documents; the live-web sample is small.
- **The test-set lock is an audit trail, not security.** It prevents accidental
  use of test data and records every supported access; it cannot stop someone
  determined to bypass it.
- **Not medical software.** The clinical benchmark is a test bed. Nothing here
  is suitable for making clinical decisions.

The full list is in the "Threats to validity" section of the pre-registration.

## Running the tests

Requires Python 3.11 or newer and git. The package itself has no runtime
dependencies.

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

The tests use temporary directories and temporary git repositories; they make no
network requests and call no model.

## Configuration and secrets

Every run is described by one versioned file under `configs/` and selected by
that file's path (see `configs/example.toml`). Experiment settings are never
read from environment variables. Environment variables are used only for
secrets such as API keys; `src/binding_gate/secrets.py` is the single place
where they are listed. This pull request needs none.

## Third-party data

No benchmark data is included in or redistributed by this repository. Dataset
adapters (later pull requests) read files that you download yourself under each
dataset's own licence.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
