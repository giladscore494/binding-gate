# Running the study: the full operating sequence

This guide lists every step from connecting the API keys to the public report,
in order. Hebrew version: [below](#הוראות-הפעלה-בעברית-הרצף-המלא).

**Nothing here has been run against a live API yet.** The model clients, the web
search client and the page fetcher were tested against recorded-shape fixtures
only. The smoke runs (steps 3 and 7) exist to find out cheaply whether the live
services behave as coded. All costs below are dry-run estimates or budget caps
from the configs, not measurements; every cap is a PROPOSED value
(PREREGISTRATION.md section 17, items 13 and 43).

## The USD 50 budget at a glance

The owner's total budget for every paid API and search call of the study is
**USD 50, hard**. Every paid run is listed in `configs/budget.toml`
(PREREGISTRATION.md section 9, "Budget plan"); the caps in the configs are hard
limits in the runner, and together they are at most 50 USD.

| Step | Run (config) | Low | High | Cap |
|---|---|---|---|---|
| 3 | smoke, all six models under test (`configs/dev_smoke.toml`) | 0.18 | 0.48 | 0.80 |
| 4 | dev baseline, three primary models (`configs/dev_baseline.toml`) | 1.68 | 4.52 | 4.80 |
| 4 | secondary judge on 60 dev items × 3 models (`configs/rescore/dev_llm_judge.toml`) | 0.92 | 4.52 | 4.75 |
| 5 | decomposer measurement, up to 4 times (`configs/decomposer_eval_dev.toml`) | 0.11 | 0.24 | 0.40 |
| 6 | gated arm (`configs/dev_gated.toml`; answers reused) | 0.00 | 0.06 | 0.30 |
| 6 | five ablations (`configs/dev_ablation_*.toml`; answers reused) | 0.00 | 0.30 | 1.50 |
| 6 | nondeterminism check (`configs/dev_repeats.toml`) | 0.17 | 0.46 | 0.55 |
| 6 | comparison baselines, 300 dev items (`configs/dev_baselines.toml`) | 0.37 | 0.95 | 1.10 |
| 6 | LLM-judge arm, 60 dev items (`configs/dev_llm_judge_checks.toml`) | 0.00 | 1.51 | 1.60 |
| 7 | layer 3 smoke (`configs/dev_live_smoke.toml`) | 0.01 | 0.02 | 0.10 |
| 7 | layer 3 dev (`configs/dev_live.toml`) | 0.45 | 1.20 | 1.30 |
| 8 | layer 2 cost side (`configs/dev_grab_rag_supportive.toml`) | 0.09 | 0.36 | 0.40 |
| 10 | **layer 1 test, confirmatory** (`configs/test_layer1.toml`) | 5.98 | 15.85 | 16.75 |
| 10 | mid-tier check, 225 test items (`configs/test_layer1_midtier.toml`) | 2.47 | 6.55 | 6.95 |
| later | layer 3 test and layer 2 test (configs not written yet; section 17, item 39) | 0.73 | 2.19 | 2.40 |
| | **Total** | **13.16** | **39.21** | **43.70** |

What makes it fit: three low-cost primary models (`openai-low`, `google-low`,
`zai-low`) on every item with one repeat, at their lowest reasoning setting;
every gate arm reuses the answer `no_gate` already paid for; every dev run after
the baseline reuses the baseline's answers (seed runs, below); the mid-tier
models (`openai-mid`, `google-mid`, `zai-mid`), the
judges and the nondeterminism check run on fixed subsamples; web search uses
SerpApi's free plan. The full-scale design of earlier versions is kept, not
run, under `configs/extended/` ("if funding becomes available").

**Providers (PR 14).** The study uses only the providers the owner funds:
OpenAI (slots A, B and the decomposer), Google (E, F), Z.ai (H `glm-5.3-flash`,
I `glm-5.3`) and Moonshot (the judge J). Anthropic and DeepSeek are not used;
their configs exist only under `configs/extended/`. The GLM models cannot switch
their thinking off: they run at `reasoning_effort = "low"`, their lowest
setting, and their estimates assume 100 to 2,000 hidden tokens per call.

**If a cap stops a run.** A run whose model reaches its cap stops that model and
stays unfinished. It can be resumed only with the same config (a changed cap
changes the config). On dev: re-derive the cap from the reserve in a pull
request and start a new run that reuses the stopped run's answers (seed runs).
The test configs' caps are re-derived from the usage measured by the smoke run
and the dev baseline before the freeze, so that a test run is never stopped.

## Provider spending limits (outer safety net)

Set, once, a monthly spending limit in every provider's dashboard, so that no
bug can spend more than planned. Suggested values (the planned HIGH spend of the
whole study at that provider, rounded up; together 42 USD, under the 50):

| Provider | Suggested limit | Where (menu paths **unverified**: not read on the providers' official pages in PR 13/14) |
|---|---|---|
| OpenAI (slots A, B, decomposer) | 9 USD | platform.openai.com → Settings → Organization → Limits (monthly budget / usage limit) |
| Google Gemini (slots E, F) | 14 USD | Google Cloud console → Billing → Budgets & alerts (a budget alerts but does not stop spending; for a hard stop use a prepaid balance or disable billing after the run) |
| Z.ai (slots H, I) | 12 USD | z.ai API platform (the account that holds `ZAI_API_KEY`): prepaid balance; top up only this amount |
| Moonshot (judge J) | 7 USD | platform.moonshot.ai: prepaid balance; top up only this amount |
| SerpApi (web search) | 0 USD | stay on the Free plan (250 searches per month); never enter a payment method |

With a prepaid balance the top-up itself is the hard limit. The runner's caps
remain the primary control; these limits only catch what the runner cannot
(a bug, a wrong price in `configs/prices.toml`).

Two kinds of step:

- **Workflow** steps run in GitHub Actions, workflow **baseline**
  (`.github/workflows/baseline.yml`). They only run `python -m binding_gate.cli
  run` (or its dry run) on the config you choose, on the dev split, after the
  free preparation that config needs (dataset, MiniCheck weights, seed runs).
- **CLI-only** steps have no workflow. They run on your own computer (section
  "Local setup") with the exact command given.

## Where to click (every workflow step)

GitHub → repository → **Actions** → workflow **baseline** (left column) →
**Run workflow** (right side). Fill the dialog exactly as in the step's table,
then press the green **Run workflow** button. "checked" / "unchecked" refer to
the two boxes. The dialog shows these fields:

| Field in the dialog | Input name in the workflow |
|---|---|
| Use workflow from (Branch) | the branch |
| Run config (path in the repository) | `config` |
| Model label from the config, or "all" (one parallel job per model) | `model` |
| Dry run: count calls and estimate cost; no model API is called | `dry_run` |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | `confirm_spend` |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | `seed_run_id` |

A paid run happens only with "Dry run" unchecked **and** the confirmation box
checked. Model labels: primary `openai-low`, `google-low`, `zai-low`; mid-tier
`openai-mid`, `google-mid`, `zai-mid` (only in the smoke run and the mid-tier
test run). `configs/dev_llm_judge_checks.toml`,
`configs/dev_live_smoke.toml` and `configs/dev_grab_rag_supportive.toml` have
`openai-low` only; `configs/dev_live.toml` has `openai-low` and `google-low`.
With `all`, each model runs as its own parallel job, except live-web configs
(layer 3), which run as one job over every model whose API key is set.

Results of a workflow run are on the run's page under **Artifacts**:
`runs-<model label>` (manifest, traces, per-model baseline report) and
`baseline-report` (stop-rule outcome over all models of the run); for live-web
runs `runs-encrypted-all-models` (the run directory, encrypted) and
`snapshot-manifest-all-models` (URLs, times, hashes; no page text). To analyse
them locally, unzip each `runs-<model label>` artifact into `runs/`
(`unzip runs-openai-low.zip -d runs/`), or with the GitHub CLI:
`gh run download <workflow run id> -n runs-openai-low -D runs/`.

## Local setup (for CLI-only steps)

```bash
git clone https://github.com/giladscore494/binding-gate && cd binding-gate
python -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev,plots]"
git clone https://github.com/CedricCaruzzo/deceptive-grounding-benchmark data/raw/deceptive-grounding-benchmark
git -C data/raw/deceptive-grounding-benchmark checkout 61b0281d8d3897f53bef3f7402727c905614c1e4
```

`runs/`, `seeds/`, `data/raw/` and `.venv/` are git-ignored. CLI steps that make
paid calls read the API keys from environment variables of your shell (the same
names as the repository secrets of step 1, e.g. `export OPENAI_API_KEY=...`);
nothing else is read from the environment. Steps without paid calls
(calibration, analysis, audit, report, snapshot check) need no key.

## 1. Add the repository secrets (once)

GitHub → repository → **Settings → Secrets and variables → Actions → New
repository secret**. Name exactly as below, value = the key.

| Secret name | For | Needed by |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI | slots A, B; the decomposer (slot Dec, `gpt-6-luna`) in every gated config |
| `GEMINI_API_KEY` | Google Gemini | slots E, F |
| `ZAI_API_KEY` | Z.ai (GLM) | slots H (`glm-5.3-flash`), I (`glm-5.3`) |
| `MOONSHOT_API_KEY` | Moonshot / Kimi | the judge (slot J): `configs/dev_llm_judge_checks.toml` and the judge re-scoring |
| `SERPAPI_API_KEY` | SerpApi (web search, Free plan) | layer 3: `configs/dev_live_smoke.toml`, `configs/dev_live.toml` |
| `RUNS_ENCRYPTION_PASSPHRASE` | a long passphrase you choose and keep | layer 3: the run artifacts are encrypted with it; a paid live-web run refuses without it |

These six are all the study needs. The workflow also passes
`ANTHROPIC_API_KEY` and `DEEPSEEK_API_KEY` if they exist, but no config of the
USD 50 study uses them: do not add them. A model
whose key is missing is **skipped with a warning**; the other models still run. A missing key of an auxiliary client (the decomposer's OpenAI key,
the judge's Moonshot key, the SerpApi key) skips every model of a config that
uses that client.

## 2. Dry run (free)

Workflow.

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baseline.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | checked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | unchecked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty |

Per model: gets the benchmark at its pinned commit, prints the number of dev
groups, items and calls and the cost estimate (low / high / ceiling / cap), then
runs the free **preflight** that asks the provider whether the model id exists.
A red job means a model id was not found or a key is invalid; it cost nothing.
For Z.ai the preflight calls `GET https://api.z.ai/api/paas/v4/models`, an
endpoint that Z.ai's documentation does not describe (**unverified**): if the
`zai-low` job fails there with HTTP 404, the id is not the problem - a pull
request sets `preflight_list_models = false` for the Z.ai models, and the first
smoke-run call (refused for free if the id is wrong) checks the id instead.
This closes PREREGISTRATION.md section 17, item 22 for the ids that pass. A
wrong id is fixed through the configs and section 9 (a pull request), not in
the dialog. Do the same dry run for any other config before its first paid run
(only "Run config" changes, and for configs that reuse answers also "Seed runs").

## 3. Smoke run (estimate 0.18-0.48 USD; caps 0.80 USD in total)

Workflow.

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_smoke.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty |

15 calls per model (the first dev (X, C) pair: 1 triple × 15 conditions,
1 repeat), all six models under test. Check per model in the `baseline-report`
artifact and the job log: no `error` records and no HTTP 400 (a 400 that
mentions `temperature`, `reasoning`, `thinkingLevel`, `thinking` or
`reasoning_effort` means the provider rejects that setting; section 17, item 24:
for OpenAI change `effort = "none"` to `"minimal"`, then `"low"`; for Gemini
`"minimal"` to `"low"`; for Z.ai remove `temperature` (provider default), in
every config, in a pull request); a small "unparseable share"; and the reasoning
tokens actually reported, against the 500 per call assumed in
`configs/prices.toml` (2,000 for the GLM models `zai-low` and `zai-mid`, whose
thinking cannot be switched off; if they are higher, the caps of later configs
are re-derived from the reserve before step 4). Check also that the model id
the provider returned (`provider_reported_model` in the trace) is the
configured one. The smoke numbers are not a result.

## 4. Dev baseline, stop-rule report and secondary judge (caps 4.80 + 4.75 USD)

Workflow.

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baseline.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty |

1,455 calls per primary model (one repeat). **Keep the number at the end of
this workflow run's URL** (`.../actions/runs/<number>`): it is the "Seed runs"
value of steps 6. To re-run one model, enter its label instead of `all`. The
final job **baseline report** prints the stop-rule outcome (CONTINUE / STOP /
IN-BETWEEN / MEASUREMENT-CHECK) per model and overall on the run's summary page
and uploads `baseline-report/baseline_report.json` (keep it: it is the
`--baseline` input of step 11). The same report locally, CLI:

```bash
python -m binding_gate.cli report runs/<run_id> [runs/<run_id> ...] --json baseline.json
```

STOP ends the study (PREREGISTRATION.md section 10). Models below 5% are carried
no further: remove them from the later configs in a pull request.

Then the secondary judge on the pre-registered 60-item subsample of the three
baseline runs (CLI only; needs `MOONSHOT_API_KEY`; one invocation over the three
run directories; cap 4.75 USD). Download the three `runs-<label>` artifacts into
`runs/` first:

```bash
python -m binding_gate.scorers.llm_judge dry-run --config configs/rescore/dev_llm_judge.toml runs/<run_id A> runs/<run_id E> runs/<run_id H>
python -m binding_gate.scorers.llm_judge run --confirm-spend --config configs/rescore/dev_llm_judge.toml runs/<run_id A> runs/<run_id E> runs/<run_id H>
python -m binding_gate.scorers.llm_judge agreement runs/<run_id A> runs/<run_id E> runs/<run_id H> --json judge_agreement.json
```

Read the `_scored` rates in the agreement output: they compare the judge and our
scorer on the judged records only.

## 5. Decomposer evaluation (CLI only; estimate 0.03-0.06 USD, cap 0.10 USD)

Not in the workflow. Needs `OPENAI_API_KEY` in your shell and the benchmark in
`data/raw/` (local setup).

```bash
python -m binding_gate.evaluation.decomposer dry-run --config configs/decomposer_eval_dev.toml
python -m binding_gate.evaluation.decomposer run --config configs/decomposer_eval_dev.toml --confirm-spend
python -m binding_gate.evaluation.decomposer report runs/<run_id> --json decomposer_report.json
```

The report compares the result with the thresholds of PREREGISTRATION.md
section 7 (section 17, item 29). The budget allows up to three revisions.

## 6. Dev runs that reuse the baseline's answers

### Seed runs (how the answers are reused)

`dev_gated.toml`, the five ablation configs, `dev_repeats.toml`,
`dev_baselines.toml` and `dev_llm_judge_checks.toml` contain
`seed_responses_from = ["seeds/*"]`. In the dialog, "Seed runs" =
**the number of the step-4 workflow run** (the number at the end of its URL).
The workflow then downloads the step-4 run directories into `seeds/`, and the
runner takes every answer the baseline already received from its traces
instead of paying for it again; each reused answer is recorded in the new trace
with `cache_hit` and `seeded_from` and costs nothing. Only the other calls are
paid (decomposer, judge, `prompt_entity_check`, repeats 1-2 of
`dev_repeats.toml`; MiniCheck runs on the CPU for free). The dry run with the
same "Seed runs" value shows, per model, how many answer calls are seeded
(`SEEDED RESPONSES`) and leaves them out of its estimate. A paid run refuses if
no seed run was downloaded (so nothing is paid twice by mistake), and refuses a
seed that is not a dev run, that used another dataset pin, another model spec
for the same label (timeouts and retries aside) or another answer prompt, a
live-web run, or a seed trace modified after its run closed; a test run never
uses seeds. Locally: `gh run download <number> -n runs-openai-low -D seeds/`
(one per model label), then run the CLI as usual.

### 6a. Gated arm, then calibration (cap 0.30 USD; re-runs after calibration cost the same)

Workflow.

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_gated.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | the number of the step-4 workflow run (see "Seed runs") |

Calibration (CLI only, no key, no model call; reads the downloaded
`runs-<label>` artifacts of this run and the dev data):

```bash
python -m binding_gate.gates.calibration runs/<run_id> [runs/<run_id> ...] \
    --window-tokens 20,40,60,80 --premise-scope document,window --competitor-tie fail,pass \
    --json calibration.json
python -m binding_gate.cli analyze runs/<run_id> [runs/<run_id> ...] --out analysis-dev-gated
```

The calibration row with the configured values must show
`changed_vs_recorded = 0`. This is the first analysis after the keys are
connected (section 17, item 30: the open risk for H2). Any parameter changed
because of it is changed in the configs and in PREREGISTRATION.md section 5.1
through a pull request, before the freeze. Dev analyses are exploratory.

### 6b. Nondeterminism check (estimate 0.17-0.46 USD, cap 0.55 USD)

Workflow.

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_repeats.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | the number of the step-4 workflow run (see "Seed runs") |

75 dev items (5 per condition), 3 repeats; repeat 0 comes from step 4.
Analysis, CLI: `python -m binding_gate.cli analyze runs/<run_id> [...] --out analysis-dev-repeats`
(its "Spread across repeats" table; labelled not confirmatory).

### 6c. Comparison baselines (estimate 0.37-0.95 USD, cap 1.10 USD)

Workflow.

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baselines.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | the number of the step-4 workflow run (see "Seed runs") |

300 dev items (20 per condition): `no_gate` (reused), `prompt_entity_check`
(paid) and `faithfulness_checker`. Before the preflight each job downloads the
pinned MiniCheck weights (about 3 GB, cached between runs), runs
`python -m binding_gate.arms.faithfulness_checker prepare` and `verify`, and stops
if the weights are not the pinned revision or the two test examples are not
labelled as expected; nothing is paid by then. `verify` prints the time per check
in the job log (section 17, item 27). The same locally (CLI; CPU only):

```bash
python -m pip install -e ".[faithfulness]"
python -m binding_gate.arms.faithfulness_checker prepare
python -m binding_gate.arms.faithfulness_checker verify
python -m binding_gate.cli dry-run --config configs/dev_baselines.toml
python -m binding_gate.cli run --config configs/dev_baselines.toml --confirm-spend
```

An interrupted run is continued with `--resume runs/<run_id>`.

### 6d. Per-check ablations (cap 0.30 USD per file)

Workflow, five runs, one per file:

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | one of `configs/dev_ablation_quote_verbatim.toml`, `configs/dev_ablation_entity_in_window.toml`, `configs/dev_ablation_qualifiers_in_window.toml`, `configs/dev_ablation_no_closer_competitor.toml`, `configs/dev_ablation_premise_present.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | the number of the step-4 workflow run (see "Seed runs") |

Analysis of each, CLI: `python -m binding_gate.cli analyze runs/<run_id> [...] --out analysis-<name>`.

### 6e. LLM-judge arm (estimate up to 1.51 USD, cap 1.60 USD)

Workflow. Needs `MOONSHOT_API_KEY`. Its upper bound first (CLI, no key):
`python -m binding_gate.arms.llm_judge_checks estimate --config configs/dev_llm_judge_checks.toml`.

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_llm_judge_checks.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | the number of the step-4 workflow run (see "Seed runs") |

60 dev items (4 per condition), slot A; the judge-only cap `judge_max_usd` is
1.60 USD.

## 7. Layer 3 (live web): smoke, then dev, in the same month

Needs `SERPAPI_API_KEY`, `RUNS_ENCRYPTION_PASSPHRASE`, `OPENAI_API_KEY` (slot A
and the decomposer) and `GEMINI_API_KEY` (slot E). The workflow downloads SealQA
at its pinned revision itself (`cli dataset-prepare`). SerpApi's Free plan
allows 250 searches per month and 50 per hour: the smoke run uses at most 10,
the dev run at most 240 (`[runner] max_searches`), and searches are spaced 72 s
apart. Each run also stops by itself when the account reports fewer searches
left; it is then resumed (CLI, `--resume runs/<run_id>`, same config) after the
monthly quota renews.

Smoke (5 dev questions, slot A, 1 repeat; estimate 0.01-0.02 USD, cap 0.10 USD):

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_live_smoke.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty |

Dev run (every dev question, slots A and E, 1 repeat, one job; estimate
0.45-1.20 USD, caps 1.30 USD; about 108 questions at 72 s per search: up to
about 4.5 hours; run a dry run of the same config first):

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_live.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty |

The job has a 350-minute limit; an interrupted run is continued locally with
`--resume`. Afterwards, CLI (no key needed): download the artifact
`runs-encrypted-all-models`, unzip it, then

```bash
gpg --decrypt runs.tar.gz.gpg > runs.tar.gz && tar -xzf runs.tar.gz   # passphrase: RUNS_ENCRYPTION_PASSPHRASE
python -m binding_gate.research.snapshots verify runs/<run_id>
python -m binding_gate.cli analyze runs/<run_id> --out analysis-dev-live
python -m pip install -e ".[sealqa]"
python -m binding_gate.cli dataset-prepare --config configs/dev_live.toml
```

`cli analyze` recognises a SealQA run and applies the layer-3 rule
`sealqa_answer` (the same analysis as `python -m
binding_gate.scorers.sealqa_answer analyze runs/<run_id>`). `dataset-prepare`
writes `data/raw/sealqa/conversion.json`, whose dev-split counts go into
PREREGISTRATION.md section 3 (section 17, item 25). Decrypted page text is
third-party material: never commit or publish it.

## 8. Layer 2, cost side (estimate 0.09-0.36 USD, cap 0.40 USD)

Workflow. GRAB-RAG's supportive items (394 dev questions, slot A, `no_gate` and
`gated`); no export is needed. The misleading condition is not run (its configs
are under `configs/extended/`; section 17, item 17).

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_grab_rag_supportive.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty |

Analysis, CLI: `python -m binding_gate.cli analyze runs/<run_id> --out analysis-dev-grab-rag`.

## 9. Freeze

1. Close every item of PREREGISTRATION.md section 17 that applies to the layer
   being frozen (owner approvals and decisions recorded with their date),
   including the budget plan (item 43) and the caps re-derived from the
   measured dev usage (item 13).
2. In one pull request: approve `configs/test_layer1.toml` and
   `configs/test_layer1_midtier.toml`, update `PARAMETERS_STATUS` in
   `src/binding_gate/mcp/core.py` (item 42), change the status line of
   PREREGISTRATION.md to `**Status:** FROZEN` and its version. Merge it into
   `main`; the tests must pass.
3. Tag exactly that merge commit `prereg-freeze-1` (later layers: `-2`, `-3`).
   In GitHub: **Releases → Draft a new release → Choose a tag**, type
   `prereg-freeze-1`, **Create new tag on publish**, Target `main`, **Publish
   release**. Or CLI:

   ```bash
   git fetch origin && git tag -a prereg-freeze-1 -m "Pre-registration frozen (layer 1)" origin/main
   git push origin prereg-freeze-1
   ```

4. From now on PREREGISTRATION.md changes only through its amendment log
   (section 18).

## 10. Frozen test runs, layer 1 (CLI only)

Not in the workflow. On your computer, in a clean checkout of the tag:

```bash
git fetch --tags && git checkout prereg-freeze-1
git status                                   # must show a clean tree
python -m pip install -e ".[dev,plots,faithfulness]"
python -m binding_gate.arms.faithfulness_checker prepare
# benchmark in data/raw/ as in "Local setup"; OPENAI, GEMINI and ZAI keys in the shell
python -m binding_gate.cli frozen-test-run --config configs/test_layer1.toml \
    --tag prereg-freeze-1 --purpose "layer-1 confirmatory test run" --confirm-spend
```

Estimate 5.98-15.85 USD, caps 16.75 USD (the confirmatory run: three primary
models, 2,505 test items, one repeat). It refuses (and logs the refusal) unless
the tag, a clean tree and the FROZEN status hold; then it runs every arm on the
test split and writes `runs/<run_id>/analysis/` (`analysis.json`, `report.md`,
figures). If a key is absent, pass the other labels, e.g. `--model
openai-low,google-low` (one access for those models; whether to use one access
per model is section 17, item 18). Then the exploratory mid-tier check
(estimate 2.47-6.55 USD, caps 6.95 USD; a second logged access):

```bash
python -m binding_gate.cli frozen-test-run --config configs/test_layer1_midtier.toml \
    --tag prereg-freeze-1 --purpose "layer-1 mid-tier exploratory subsample" --confirm-spend
```

A run stopped by a budget cap is continued with `--resume runs/<run_id>` (a
further logged access). Then commit the access log:

```bash
git switch -c test-access-log-1
git add audit/test_access_log.jsonl
git commit -m "Test-split access log after the layer-1 frozen test runs"
git push -u origin test-access-log-1          # open and merge a pull request
```

Layers 2 and 3 have no frozen test configuration yet (section 17, item 39); the
budget plan reserves 2.40 USD of caps for them.

## 11. Human audit and public report (CLI only, no key)

```bash
python -m binding_gate.audit_tool sample runs/<test_run_id> --out audit/sample.json --html audit/audit.html
# label: open audit/audit.html in a browser (offline) and press "Export labels", or in the terminal:
python -m binding_gate.audit_tool label --sample audit/sample.json --out audit/labels.json --auditor "<name>"
python -m binding_gate.audit_tool analyze --sample audit/sample.json --labels audit/labels.json --out audit/audit.json
python -m binding_gate.report build --analysis runs/<test_run_id>/analysis --run-dir runs/<test_run_id> \
    --baseline baseline.json --audit audit/audit.json --out public-report/
```

`<test_run_id>` is the run of `configs/test_layer1.toml` (the mid-tier run is
not in the audit population; PREREGISTRATION.md section 13). The sample draws 50
blocked and 50 accepted gate decisions with the fixed seed; who audits is
section 17, item 19. The report (`public-report/report.md`,
`public-report/report.html`) refuses an analysis that marks a result
confirmatory while the pre-registration is not FROZEN.

---

## הוראות הפעלה בעברית: הרצף המלא

**שום דבר כאן עדיין לא הורץ מול API אמיתי.** הלקוחות של המודלים, של החיפוש ושל הורדת הדפים נבדקו רק מול תשובות לדוגמה. כל העלויות הן הערכות dry run או תקרות מהקונפיגים, לא מדידות; כל תקרה היא ערך PROPOSED (סעיף 17, פריטים 13 ו-43 ב-PREREGISTRATION.md).

### התקציב: 50 דולר, בקצרה

התקציב הכולל שלך לכל קריאה בתשלום (מודלים וחיפוש) בכל המחקר הוא **50 דולר, קשיח**. כל ריצה בתשלום רשומה ב-`configs/budget.toml` (סעיף 9, "Budget plan"); התקרות בקונפיגים הן גבולות קשיחים ב-runner, וביחד הן לכל היותר 50 דולר.

| שלב | ריצה (קונפיג) | נמוך | גבוה | תקרה |
|---|---|---|---|---|
| 3 | smoke, כל ששת המודלים הנבדקים (`configs/dev_smoke.toml`) | 0.18 | 0.48 | 0.80 |
| 4 | baseline על dev, שלושת המודלים הראשיים (`configs/dev_baseline.toml`) | 1.68 | 4.52 | 4.80 |
| 4 | שופט משני על 60 פריטי dev × 3 מודלים (`configs/rescore/dev_llm_judge.toml`) | 0.92 | 4.52 | 4.75 |
| 5 | מדידת ה-decomposer, עד 4 פעמים (`configs/decomposer_eval_dev.toml`) | 0.11 | 0.24 | 0.40 |
| 6 | ה-gate (`configs/dev_gated.toml`; תשובות משוחזרות) | 0.00 | 0.06 | 0.30 |
| 6 | חמישה ablations (`configs/dev_ablation_*.toml`; תשובות משוחזרות) | 0.00 | 0.30 | 1.50 |
| 6 | בדיקת אי-דטרמיניזם (`configs/dev_repeats.toml`) | 0.17 | 0.46 | 0.55 |
| 6 | השוואות, 300 פריטי dev (`configs/dev_baselines.toml`) | 0.37 | 0.95 | 1.10 |
| 6 | זרוע שופט LLM, 60 פריטי dev (`configs/dev_llm_judge_checks.toml`) | 0.00 | 1.51 | 1.60 |
| 7 | smoke של שכבה 3 (`configs/dev_live_smoke.toml`) | 0.01 | 0.02 | 0.10 |
| 7 | שכבה 3 על dev (`configs/dev_live.toml`) | 0.45 | 1.20 | 1.30 |
| 8 | שכבה 2, צד העלות (`configs/dev_grab_rag_supportive.toml`) | 0.09 | 0.36 | 0.40 |
| 10 | **test של שכבה 1, מאשש** (`configs/test_layer1.toml`) | 5.98 | 15.85 | 16.75 |
| 10 | בדיקת מודלי הביניים, 225 פריטי test (`configs/test_layer1_midtier.toml`) | 2.47 | 6.55 | 6.95 |
| בהמשך | test של שכבות 3 ו-2 (הקונפיגים עוד לא נכתבו; פריט 39) | 0.73 | 2.19 | 2.40 |
| | **סך הכול** | **13.16** | **39.21** | **43.70** |

למה זה נכנס: שלושה מודלים ראשיים זולים (`openai-low`, `google-low`, `zai-low`) על כל הפריטים עם חזרה אחת, ברמת ה-reasoning הנמוכה ביותר; כל זרועות ה-gate משתמשות בתשובה ש-`no_gate` כבר שילם עליה; כל ריצת dev אחרי ה-baseline משתמשת שוב בתשובות שלו (seed runs, למטה); מודלי הביניים (`openai-mid`, `google-mid`, `zai-mid`), השופטים ובדיקת אי-הדטרמיניזם רצים על מדגמים קבועים; החיפוש בתוכנית החינמית של SerpApi. התכנון המלא הקודם שמור, בלי להריץ, תחת `configs/extended/` ("אם יהיה מימון").

**הספקים (PR 14).** המחקר משתמש רק בספקים שיש לך אצלם חשבון בתשלום: OpenAI (סלוטים A, B וה-decomposer), Google (E, F), Z.ai (H `glm-5.3-flash`, I `glm-5.3`) ו-Moonshot (השופט J). אין שימוש ב-Anthropic וב-DeepSeek; הקונפיגים שלהם קיימים רק תחת `configs/extended/`. במודלי GLM אי אפשר לכבות את ה-thinking: הם רצים עם `reasoning_effort = "low"`, ההגדרה הנמוכה ביותר שלהם, וההערכות מניחות 100 עד 2,000 טוקנים נסתרים לקריאה.

**אם תקרה עוצרת ריצה.** ריצה שמודל בה הגיע לתקרה עוצרת את המודל הזה ונשארת לא גמורה. אפשר להמשיך אותה רק עם אותו קונפיג (שינוי תקרה משנה את הקונפיג). ב-dev: לחשב תקרה מחדש מהרזרבה ב-pull request ולהתחיל ריצה חדשה שמשתמשת שוב בתשובות של הריצה שנעצרה (seed runs). התקרות של קונפיגי ה-test מחושבות מחדש לפני ההקפאה לפי השימוש שנמדד ב-smoke וב-baseline, כדי שריצת test לעולם לא תיעצר.

### מגבלות הוצאה אצל הספקים (רשת ביטחון חיצונית)

פעם אחת: להגדיר מגבלת הוצאה חודשית בלוח הבקרה של כל ספק, כדי שאף באג לא יוכל להוציא יותר מהמתוכנן. ערכים מומלצים (ההוצאה הגבוהה המתוכננת של כל המחקר אצל אותו ספק, מעוגלת למעלה; ביחד 42 דולר, מתחת ל-50):

| ספק | מגבלה מומלצת | איפה (הנתיבים בתפריטים **לא אומתו**: לא נקראו בדפים הרשמיים של הספקים ב-PR 13/14) |
|---|---|---|
| OpenAI (סלוטים A, B, ה-decomposer) | 9 דולר | platform.openai.com ← Settings ← Organization ← Limits (תקציב חודשי / usage limit) |
| Google Gemini (סלוטים E, F) | 14 דולר | Google Cloud console ← Billing ← Budgets & alerts (תקציב שולח התראה אבל לא עוצר; לעצירה קשיחה — יתרה משולמת מראש, או לכבות billing אחרי הריצה) |
| Z.ai (סלוטים H, I) | 12 דולר | פלטפורמת ה-API של z.ai (החשבון של `ZAI_API_KEY`): יתרה משולמת מראש; להטעין רק את הסכום הזה |
| Moonshot (השופט J) | 7 דולר | platform.moonshot.ai: יתרה משולמת מראש; להטעין רק את הסכום הזה |
| SerpApi (חיפוש) | 0 דולר | להישאר בתוכנית Free (250 חיפושים בחודש); לא להזין אמצעי תשלום |

ביתרה משולמת מראש, ההטענה עצמה היא הגבול הקשיח. התקרות של ה-runner נשארות הבקרה הראשית; המגבלות האלה רק תופסות מה שה-runner לא יכול (באג, מחיר שגוי ב-`configs/prices.toml`).

שני סוגי שלבים:

- **Workflow** — רץ ב-GitHub Actions, ה-workflow בשם **baseline**. מריץ רק את `cli run` (או dry run) על הקונפיג שבוחרים, על ה-dev split, אחרי ההכנה החינמית שהקונפיג צריך (נתונים, משקולות MiniCheck, seed runs).
- **CLI בלבד** — אין לו workflow. רץ על המחשב שלך (ראו "הכנה מקומית") עם הפקודה המדויקת שכתובה.

### איפה לוחצים (בכל שלב workflow)

GitHub ← הריפו ← לשונית **Actions** ← בעמודה השמאלית **baseline** ← כפתור **Run workflow** מימין ← למלא את השדות בדיוק לפי הטבלה של השלב ← הכפתור הירוק **Run workflow**. "מסומן" / "לא מסומן" מתייחס לשתי תיבות הסימון. ריצה בתשלום קורית רק כש-"Dry run" לא מסומן **וגם** תיבת האישור מסומנת. השדה השישי, "Seed runs" (`seed_run_id`), נשאר ריק, חוץ מבשלב 6, שבו כותבים בו את המספר של ריצת ה-workflow של שלב 4.

תוויות המודלים: ראשיים `openai-low`, `google-low`, `zai-low`; מודלי ביניים `openai-mid`, `google-mid`, `zai-mid` (רק ב-smoke ובריצת ה-test של מודלי הביניים). ב-`configs/dev_llm_judge_checks.toml`, `configs/dev_live_smoke.toml` ו-`configs/dev_grab_rag_supportive.toml` יש רק `openai-low`; ב-`configs/dev_live.toml` יש `openai-low` ו-`google-low`. עם `all` כל מודל רץ כ-job נפרד במקביל, חוץ מקונפיגים של web חי (שכבה 3) שרצים כ-job אחד על כל המודלים שיש להם מפתח.

התוצאות נמצאות בעמוד הריצה תחת **Artifacts**: `runs-<תווית מודל>` (manifest, traces, דוח לכל מודל) ו-`baseline-report` (תוצאת כלל העצירה לכל המודלים). בריצות web חי: `runs-encrypted-all-models` (תיקיית הריצה, מוצפנת) ו-`snapshot-manifest-all-models` (כתובות, זמנים, hash; בלי טקסט של דפים). לניתוח מקומי לפרוס כל `runs-<תווית>` לתוך `runs/`: `unzip runs-openai-low.zip -d runs/`, או `gh run download <מזהה הריצה> -n runs-openai-low -D runs/`.

### הכנה מקומית (לשלבי CLI)

```bash
git clone https://github.com/giladscore494/binding-gate && cd binding-gate
python -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev,plots]"
git clone https://github.com/CedricCaruzzo/deceptive-grounding-benchmark data/raw/deceptive-grounding-benchmark
git -C data/raw/deceptive-grounding-benchmark checkout 61b0281d8d3897f53bef3f7402727c905614c1e4
```

שלבי CLI בתשלום קוראים את המפתחות ממשתני סביבה של ה-shell (אותם שמות כמו ה-secrets בשלב 1, למשל `export OPENAI_API_KEY=...`). שום הגדרה אחרת לא נקראת מהסביבה. שלבים בלי קריאות בתשלום (כיול, ניתוח, audit, דוח, בדיקת snapshots) לא צריכים מפתח.

### שלב 1 — הוספת secrets (פעם אחת)

GitHub ← הריפו ← **Settings ← Secrets and variables ← Actions ← New repository secret**. השם בדיוק כמו בטבלה, הערך = המפתח.

| שם ה-secret | עבור | נדרש ל- |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI | סלוטים A, B; ה-decomposer (`gpt-6-luna`) בכל קונפיג עם gate |
| `GEMINI_API_KEY` | Google Gemini | סלוטים E, F |
| `ZAI_API_KEY` | Z.ai (GLM) | סלוטים H (`glm-5.3-flash`), I (`glm-5.3`) |
| `MOONSHOT_API_KEY` | Moonshot / Kimi | השופט (סלוט J): `configs/dev_llm_judge_checks.toml` וה-re-scoring של השופט |
| `SERPAPI_API_KEY` | SerpApi (חיפוש, תוכנית Free) | שכבה 3: `configs/dev_live_smoke.toml`, `configs/dev_live.toml` |
| `RUNS_ENCRYPTION_PASSPHRASE` | סיסמה ארוכה שבוחרים ושומרים | שכבה 3: ה-artifacts מוצפנים בה; ריצה בתשלום של web חי מסרבת בלעדיה |

שש אלה הן כל מה שהמחקר צריך. ה-workflow מעביר גם `ANTHROPIC_API_KEY` ו-`DEEPSEEK_API_KEY` אם הם קיימים, אבל אף קונפיג של מחקר ה-50 דולר לא משתמש בהם: לא להוסיף אותם. מודל שהמפתח שלו חסר מדולג עם אזהרה, והשאר ממשיכים. מפתח חסר של רכיב עזר (מפתח OpenAI של ה-decomposer, מפתח Moonshot של השופט, מפתח SerpApi) מדלג על כל המודלים של קונפיג שמשתמש ברכיב הזה.

### שלב 2 — Dry run (חינם)

Workflow.

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baseline.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | לא מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק |

מה יוצא: מספר הקבוצות, הפריטים והקריאות, הערכת עלות לכל מודל, ובדיקת preflight חינמית שמזהה המודל קיים אצל הספק. Job אדום = מזהה מודל לא נמצא או מפתח לא תקין; לא עלה כסף. ב-Z.ai ה-preflight קורא ל-`GET https://api.z.ai/api/paas/v4/models`, endpoint שהתיעוד של Z.ai לא מתאר (**לא אומת**): אם ה-job של `zai-low` נכשל שם עם HTTP 404, הבעיה אינה המזהה — pull request מגדיר `preflight_list_models = false` למודלי Z.ai, והקריאה הראשונה של ה-smoke (שנדחית בחינם אם המזהה שגוי) בודקת את המזהה במקומו. מזהה שגוי מתקנים ב-pull request (קונפיגים וסעיף 9), לא בדיאלוג. לפני ריצה ראשונה בתשלום של כל קונפיג אחר עושים אותו dry run (משנים רק את "Run config", ובקונפיגים שמשתמשים שוב בתשובות גם את "Seed runs").

### שלב 3 — Smoke (הערכה 0.18 עד 0.48 דולר; תקרות 0.80 דולר בסך הכול)

Workflow.

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_smoke.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק |

15 קריאות למודל (זוג ה-(X, C) הראשון של dev: שלישייה אחת × 15 תנאים, חזרה אחת), כל ששת המודלים הנבדקים. לבדוק לכל מודל ב-artifact `baseline-report` וביומן: אין רשומות `error` ואין HTTP 400 (400 שמזכיר `temperature`, `reasoning`, `thinkingLevel`, `thinking` או `reasoning_effort` = הספק דוחה את ההגדרה; פריט 24: ב-OpenAI להחליף `effort = "none"` ל-`"minimal"` ואז ל-`"low"`; ב-Gemini `"minimal"` ל-`"low"`; ב-Z.ai להוריד את `temperature` (ברירת המחדל של הספק), בכל הקונפיגים, ב-pull request); שיעור `unparseable` נמוך; וכמה טוקני reasoning דווחו בפועל, מול ה-500 לקריאה שמונחים ב-`configs/prices.toml` (2,000 למודלי GLM `zai-low` ו-`zai-mid`, שאי אפשר לכבות בהם את ה-thinking; אם יותר — מחשבים מחדש את התקרות של הקונפיגים הבאים מהרזרבה לפני שלב 4). לבדוק גם שמזהה המודל שהספק החזיר (`provider_reported_model` ב-trace) הוא המזהה שבקונפיג. מספרי ה-smoke אינם תוצאה.

### שלב 4 — Baseline על dev, דוח כלל העצירה ושופט משני (תקרות 4.80 + 4.75 דולר)

Workflow.

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baseline.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק |

1,455 קריאות לכל מודל ראשי (חזרה אחת). **לשמור את המספר בסוף ה-URL של ריצת ה-workflow הזאת** (`.../actions/runs/<מספר>`): זה הערך של "Seed runs" בשלב 6. להרצה חוזרת של מודל אחד כותבים את התווית שלו במקום `all`. ה-job האחרון, **baseline report**, מדפיס בעמוד הסיכום את התוצאה (CONTINUE / STOP / IN-BETWEEN / MEASUREMENT-CHECK) ומעלה את `baseline-report/baseline_report.json` — לשמור, זה הקלט `--baseline` של שלב 11. אותו דוח מקומית: `python -m binding_gate.cli report runs/<run_id> [...] --json baseline.json`.

STOP מסיים את המחקר. מודלים מתחת ל-5% לא ממשיכים: מוציאים אותם מהקונפיגים הבאים ב-pull request.

אחר כך השופט המשני על המדגם הקבוע של 60 פריטים מתוך שלוש ריצות ה-baseline (CLI בלבד; צריך `MOONSHOT_API_KEY`; הפעלה אחת על שלוש תיקיות הריצה; תקרה 4.75 דולר). קודם להוריד את שלושת ה-artifacts `runs-<תווית>` לתוך `runs/`:

```bash
python -m binding_gate.scorers.llm_judge dry-run --config configs/rescore/dev_llm_judge.toml runs/<run_id A> runs/<run_id E> runs/<run_id H>
python -m binding_gate.scorers.llm_judge run --confirm-spend --config configs/rescore/dev_llm_judge.toml runs/<run_id A> runs/<run_id E> runs/<run_id H>
python -m binding_gate.scorers.llm_judge agreement runs/<run_id A> runs/<run_id E> runs/<run_id H> --json judge_agreement.json
```

בפלט ההסכמה לקרוא את השיעורים עם `_scored`: הם משווים בין השופט לבין ה-scorer שלנו רק על הרשומות שנשפטו.

### שלב 5 — מדידת ה-decomposer (CLI בלבד; הערכה 0.03 עד 0.06 דולר, תקרה 0.10)

אין workflow. צריך `OPENAI_API_KEY` ב-shell ואת הבנצ'מרק ב-`data/raw/`.

```bash
python -m binding_gate.evaluation.decomposer dry-run --config configs/decomposer_eval_dev.toml
python -m binding_gate.evaluation.decomposer run --config configs/decomposer_eval_dev.toml --confirm-spend
python -m binding_gate.evaluation.decomposer report runs/<run_id> --json decomposer_report.json
```

הדוח משווה לספים של סעיף 7 (פריט 29). התקציב מאפשר עד שלושה תיקונים.

### שלב 6 — ריצות dev שמשתמשות שוב בתשובות של ה-baseline

#### Seed runs (איך התשובות משוחזרות)

ב-`dev_gated.toml`, בחמשת קונפיגי ה-ablation, ב-`dev_repeats.toml`, ב-`dev_baselines.toml` וב-`dev_llm_judge_checks.toml` כתוב `seed_responses_from = ["seeds/*"]`. בדיאלוג, "Seed runs" = **המספר של ריצת ה-workflow של שלב 4** (המספר בסוף ה-URL שלה). ה-workflow מוריד את תיקיות הריצה של שלב 4 לתוך `seeds/`, וה-runner לוקח כל תשובה שה-baseline כבר קיבל מה-traces שלו במקום לשלם עליה שוב; כל תשובה כזאת נרשמת ב-trace החדש עם `cache_hit` ו-`seeded_from` ועולה 0. משלמים רק על שאר הקריאות (decomposer, שופט, `prompt_entity_check`, חזרות 1-2 של `dev_repeats.toml`; MiniCheck רץ על CPU בחינם). ה-dry run עם אותו ערך "Seed runs" מראה לכל מודל כמה קריאות תשובה נלקחות מה-seed (`SEEDED RESPONSES`) ולא כולל אותן בהערכה. ריצה בתשלום מסרבת אם לא הורדה אף ריצת seed (כדי ששום דבר לא ישולם פעמיים בטעות), ומסרבת ל-seed שאינו ריצת dev, שהשתמש בגרסת נתונים אחרת, ב-spec מודל אחר לאותה תווית (חוץ מ-timeouts ו-retries) או ב-prompt תשובה אחר, לריצת web חי, או ל-trace של seed ששונה אחרי שהריצה נסגרה; ריצת test אף פעם לא משתמשת ב-seeds. מקומית: `gh run download <מספר> -n runs-openai-low -D seeds/` (אחת לכל תווית מודל), ואז להריץ את ה-CLI כרגיל.

#### 6א — ה-gate, ואז כיול (תקרה 0.30 דולר; הרצה חוזרת אחרי כיול עולה אותו דבר)

Workflow.

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_gated.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | המספר של ריצת ה-workflow של שלב 4 (ראו "Seed runs") |

כיול — CLI בלבד, בלי מפתח ובלי קריאה למודל, על ה-artifacts של הריצה הזאת ועל נתוני ה-dev:

```bash
python -m binding_gate.gates.calibration runs/<run_id> [runs/<run_id> ...] \
    --window-tokens 20,40,60,80 --premise-scope document,window --competitor-tie fail,pass \
    --json calibration.json
python -m binding_gate.cli analyze runs/<run_id> [runs/<run_id> ...] --out analysis-dev-gated
```

השורה עם הערכים המוגדרים חייבת להראות `changed_vs_recorded = 0`. זה הניתוח הראשון אחרי חיבור המפתחות (פריט 30: הסיכון הפתוח ל-H2). פרמטר שמשתנה בעקבותיו משתנה בקונפיגים ובסעיף 5.1 ב-pull request, לפני ההקפאה. ניתוחי dev הם גישושיים.

#### 6ב — בדיקת אי-דטרמיניזם (הערכה 0.17 עד 0.46 דולר, תקרה 0.55)

Workflow.

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_repeats.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | המספר של ריצת ה-workflow של שלב 4 (ראו "Seed runs") |

75 פריטי dev (5 לכל תנאי), 3 חזרות; חזרה 0 באה משלב 4. ניתוח (CLI): `python -m binding_gate.cli analyze runs/<run_id> [...] --out analysis-dev-repeats` (הטבלה "Spread across repeats"; מסומן לא מאשש).

#### 6ג — השוואות (הערכה 0.37 עד 0.95 דולר, תקרה 1.10)

Workflow.

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baselines.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | המספר של ריצת ה-workflow של שלב 4 (ראו "Seed runs") |

300 פריטי dev (20 לכל תנאי): `no_gate` (משוחזר), `prompt_entity_check` (בתשלום) ו-`faithfulness_checker`. לפני ה-preflight כל job מוריד את המשקולות המוצמדות של MiniCheck (כ-3GB, נשמרות ב-cache בין ריצות), מריץ `python -m binding_gate.arms.faithfulness_checker prepare` ו-`verify`, ונעצר אם המשקולות אינן הגרסה המוצמדת או ששתי דוגמאות הבדיקה לא סווגו כצפוי; עד אז לא שולם כלום. `verify` מדפיס ביומן את הזמן לבדיקה (פריט 27). אותו דבר מקומית (CLI; CPU בלבד):

```bash
python -m pip install -e ".[faithfulness]"
python -m binding_gate.arms.faithfulness_checker prepare
python -m binding_gate.arms.faithfulness_checker verify
python -m binding_gate.cli dry-run --config configs/dev_baselines.toml
python -m binding_gate.cli run --config configs/dev_baselines.toml --confirm-spend
```

ריצה שנקטעה ממשיכים עם `--resume runs/<run_id>`.

#### 6ד — Ablations (תקרה 0.30 דולר לקובץ)

Workflow, חמש ריצות, אחת לכל קובץ:

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | אחד מ: `configs/dev_ablation_quote_verbatim.toml`, `configs/dev_ablation_entity_in_window.toml`, `configs/dev_ablation_qualifiers_in_window.toml`, `configs/dev_ablation_no_closer_competitor.toml`, `configs/dev_ablation_premise_present.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | המספר של ריצת ה-workflow של שלב 4 (ראו "Seed runs") |

ניתוח של כל אחד (CLI): `python -m binding_gate.cli analyze runs/<run_id> [...] --out analysis-<name>`.

#### 6ה — זרוע שופט LLM (הערכה עד 1.51 דולר, תקרה 1.60)

Workflow. צריך `MOONSHOT_API_KEY`. קודם החסם העליון (CLI, בלי מפתח): `python -m binding_gate.arms.llm_judge_checks estimate --config configs/dev_llm_judge_checks.toml`.

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_llm_judge_checks.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | המספר של ריצת ה-workflow של שלב 4 (ראו "Seed runs") |

60 פריטי dev (4 לכל תנאי), סלוט A; התקרה של השופט עצמו, `judge_max_usd`, היא 1.60 דולר.

### שלב 7 — שכבה 3 (web חי): smoke ואז dev, באותו חודש

צריך `SERPAPI_API_KEY`, `RUNS_ENCRYPTION_PASSPHRASE`, `OPENAI_API_KEY` (סלוט A וה-decomposer) ו-`GEMINI_API_KEY` (סלוט E). ה-workflow מוריד את SealQA בגרסה המוצמדת בעצמו. תוכנית Free של SerpApi מאפשרת 250 חיפושים בחודש ו-50 בשעה: ה-smoke משתמש בלכל היותר 10, ריצת ה-dev בלכל היותר 240 (`[runner] max_searches`), והחיפושים במרווח של 72 שניות. כל ריצה גם נעצרת לבד כשהחשבון מדווח שנשארו פחות חיפושים; אז ממשיכים אותה (CLI, `--resume runs/<run_id>`, אותו קונפיג) אחרי שהמכסה החודשית מתחדשת.

Smoke (5 שאלות dev, סלוט A, חזרה אחת; הערכה 0.01 עד 0.02 דולר, תקרה 0.10):

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_live_smoke.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק |

ריצת dev (כל שאלות ה-dev, סלוטים A ו-E, חזרה אחת, job אחד; הערכה 0.45 עד 1.20 דולר, תקרות 1.30; כ-108 שאלות ב-72 שניות לחיפוש: עד כ-4.5 שעות; קודם dry run של אותו קונפיג):

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_live.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק |

מגבלת ה-job היא 350 דקות; ריצה שנקטעה ממשיכים מקומית עם `--resume`. אחר כך, CLI (בלי מפתח): להוריד את ה-artifact `runs-encrypted-all-models`, לפרוס אותו, ואז:

```bash
gpg --decrypt runs.tar.gz.gpg > runs.tar.gz && tar -xzf runs.tar.gz   # הסיסמה: RUNS_ENCRYPTION_PASSPHRASE
python -m binding_gate.research.snapshots verify runs/<run_id>
python -m binding_gate.cli analyze runs/<run_id> --out analysis-dev-live
python -m pip install -e ".[sealqa]"
python -m binding_gate.cli dataset-prepare --config configs/dev_live.toml
```

`cli analyze` מזהה ריצת SealQA ומפעיל את כלל שכבה 3 (`sealqa_answer`). `dataset-prepare` כותב את `data/raw/sealqa/conversion.json`; את הספירות של ה-dev split מכניסים לסעיף 3 (פריט 25). טקסט של דפים אחרי פענוח הוא חומר של צד שלישי: לא לעשות לו commit ולא לפרסם.

### שלב 8 — שכבה 2, צד העלות (הערכה 0.09 עד 0.36 דולר, תקרה 0.40)

Workflow. הפריטים התומכים של GRAB-RAG (394 שאלות dev, סלוט A, `no_gate` ו-`gated`); לא צריך export. התנאי המטעה לא רץ (הקונפיגים שלו תחת `configs/extended/`; פריט 17).

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_grab_rag_supportive.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק |

ניתוח (CLI): `python -m binding_gate.cli analyze runs/<run_id> --out analysis-dev-grab-rag`.

### שלב 9 — הקפאה

1. לסגור כל פריט בסעיף 17 שנוגע לשכבה שמוקפאת (אישורים והחלטות עם תאריך), כולל תוכנית התקציב (פריט 43) והתקרות שחושבו מחדש לפי השימוש שנמדד ב-dev (פריט 13).
2. ב-pull request אחד: לאשר את `configs/test_layer1.toml` ואת `configs/test_layer1_midtier.toml`, לעדכן את `PARAMETERS_STATUS` ב-`src/binding_gate/mcp/core.py` (פריט 42), לשנות את שורת הסטטוס ל-`**Status:** FROZEN` ואת הגרסה. למזג ל-`main`; הבדיקות חייבות לעבור.
3. לתייג בדיוק את ה-merge commit בשם `prereg-freeze-1` (לשכבות הבאות `-2`, `-3`). ב-GitHub: **Releases ← Draft a new release ← Choose a tag** ← להקליד `prereg-freeze-1` ← **Create new tag on publish** ← Target: `main` ← **Publish release**. או ב-CLI:

   ```bash
   git fetch origin && git tag -a prereg-freeze-1 -m "Pre-registration frozen (layer 1)" origin/main
   git push origin prereg-freeze-1
   ```

4. מעכשיו PREREGISTRATION.md משתנה רק דרך יומן התיקונים (סעיף 18).

### שלב 10 — ריצות ה-test המוקפאות, שכבה 1 (CLI בלבד)

אין workflow. על המחשב שלך, ב-checkout נקי של התג:

```bash
git fetch --tags && git checkout prereg-freeze-1
git status                                   # חייב להיות נקי
python -m pip install -e ".[dev,plots,faithfulness]"
python -m binding_gate.arms.faithfulness_checker prepare
# הבנצ'מרק ב-data/raw/ כמו ב"הכנה מקומית"; מפתחות OPENAI, GEMINI ו-ZAI ב-shell
python -m binding_gate.cli frozen-test-run --config configs/test_layer1.toml \
    --tag prereg-freeze-1 --purpose "layer-1 confirmatory test run" --confirm-spend
```

הערכה 5.98 עד 15.85 דולר, תקרות 16.75 (הריצה המאששת: שלושת המודלים הראשיים, 2,505 פריטי test, חזרה אחת). הפקודה מסרבת (ורושמת את הסירוב) אם התג, העץ הנקי או הסטטוס FROZEN לא מתקיימים; אחרת מריצה את כל הזרועות על ה-test וכותבת `runs/<run_id>/analysis/`. אם מפתח חסר, לתת את שאר התוויות, למשל `--model openai-low,google-low` (גישה אחת לכל המודלים האלה; האם גישה לכל מודל בנפרד — פריט 18). אחר כך בדיקת מודלי הביניים הגישושית (הערכה 2.47 עד 6.55 דולר, תקרות 6.95; גישה שנייה שנרשמת):

```bash
python -m binding_gate.cli frozen-test-run --config configs/test_layer1_midtier.toml \
    --tag prereg-freeze-1 --purpose "layer-1 mid-tier exploratory subsample" --confirm-spend
```

ריצה שנעצרה בתקרה ממשיכים עם `--resume runs/<run_id>` (גישה נוספת שנרשמת). אחר כך commit ליומן הגישה:

```bash
git switch -c test-access-log-1
git add audit/test_access_log.jsonl
git commit -m "Test-split access log after the layer-1 frozen test runs"
git push -u origin test-access-log-1          # לפתוח pull request ולמזג
```

לשכבות 2 ו-3 עדיין אין קונפיג test מוקפא (פריט 39); תוכנית התקציב שומרת להן 2.40 דולר של תקרות.

### שלב 11 — audit אנושי ודוח ציבורי (CLI בלבד, בלי מפתח)

```bash
python -m binding_gate.audit_tool sample runs/<test_run_id> --out audit/sample.json --html audit/audit.html
# תיוג: לפתוח את audit/audit.html בדפדפן (בלי אינטרנט) וללחוץ "Export labels", או בטרמינל:
python -m binding_gate.audit_tool label --sample audit/sample.json --out audit/labels.json --auditor "<name>"
python -m binding_gate.audit_tool analyze --sample audit/sample.json --labels audit/labels.json --out audit/audit.json
python -m binding_gate.report build --analysis runs/<test_run_id>/analysis --run-dir runs/<test_run_id> \
    --baseline baseline.json --audit audit/audit.json --out public-report/
```

`<test_run_id>` הוא הריצה של `configs/test_layer1.toml` (ריצת מודלי הביניים לא באוכלוסיית ה-audit; סעיף 13). המדגם: 50 החלטות חסומות ו-50 מאושרות עם seed קבוע; מי מתייג — פריט 19. הדוח (`public-report/report.md`, `public-report/report.html`) מסרב לניתוח שמסמן תוצאה כמאששת כל עוד ה-pre-registration לא FROZEN.
