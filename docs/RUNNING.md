# Running the study: the full operating sequence

This guide lists every step from connecting the API keys to the public report,
in order. Hebrew version: [below](#הוראות-הפעלה-בעברית-הרצף-המלא).

**Nothing here has been run against a live API yet.** The model clients, the web
search client and the page fetcher were tested against recorded-shape fixtures
only. The smoke runs (steps 3 and 8) exist to find out cheaply whether the live
services behave as coded. All costs below are dry-run estimates or budget caps
from the configs, not measurements; every cap is a PROPOSED value
(PREREGISTRATION.md section 17, item 13).

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
checked. Model labels in every config: `openai-low`, `openai-mid`,
`anthropic-low`, `anthropic-mid`, `google-low`, `google-mid` (the layer-3
smoke config has `openai-low` only). With `all`, each model runs as its own
parallel job, except live-web configs (layer 3), which run as one job over every
model whose API key is set.

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

`runs/`, `data/raw/` and `.venv/` are git-ignored. CLI steps that make paid
calls read the API keys from environment variables of your shell (the same
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
| `ANTHROPIC_API_KEY` | Anthropic (optional) | slots C, D; without it they are skipped and the study proceeds with A, B, E, F (PREREGISTRATION.md section 9) |
| `MOONSHOT_API_KEY` | Moonshot / Kimi | the judge (slot J): `configs/dev_baselines.toml` and the judge re-scoring |
| `SERPAPI_API_KEY` | SerpApi (web search) | layer 3: `configs/dev_live_smoke.toml`, `configs/dev_live.toml` |
| `RUNS_ENCRYPTION_PASSPHRASE` | a long passphrase you choose and keep | layer 3: the run artifacts are encrypted with it; a paid live-web run refuses without it |

A model whose key is missing is **skipped with a warning**; the other models
still run. A missing key of an auxiliary client (the decomposer's OpenAI key,
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
This closes PREREGISTRATION.md section 17, item 22 for the ids that pass. A
wrong id is fixed through the configs and section 9 (a pull request), not in
the dialog. Do the same dry run for any other config before its first paid run
(only "Run config" changes).

## 3. Smoke run (estimate 1-6 USD in total; cap 2.50 USD per model)

Workflow.

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_smoke.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty |

75 calls per model (2 dev (X, C) pairs, 5 triples, 1 repeat). Check per model in
the `baseline-report` artifact and the job log: no `error` records and no HTTP
400 (a 400 that mentions `temperature` means the provider rejects that setting;
section 17, item 24); a small "unparseable share"; the real reasoning-token
usage, from which the caps of later configs are lowered (item 13). The smoke
numbers are not a result.

## 4. Full dev baseline and stop-rule report (caps 349 USD in total)

Workflow.

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baseline.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty |

4,365 calls per model (dry-run estimate 59-334 USD in total). To re-run one
model, enter its label instead of `all`. The final job **baseline report**
prints the stop-rule outcome (CONTINUE / STOP / IN-BETWEEN / MEASUREMENT-CHECK)
per model and overall on the run's summary page and uploads
`baseline-report/baseline_report.json` (keep it: it is the `--baseline` input of
step 11). The same report locally, CLI:

```bash
python -m binding_gate.cli report runs/<run_id> [runs/<run_id> ...] --json baseline.json
```

STOP ends the study (PREREGISTRATION.md section 10). Models below 5% are carried
no further: remove them from the later configs in a pull request. If the outcome
is MEASUREMENT-CHECK, run the secondary judge on the baseline traces (CLI only;
needs `MOONSHOT_API_KEY`; cap 100 USD per judge model and invocation):

```bash
python -m binding_gate.scorers.llm_judge dry-run --config configs/rescore/dev_llm_judge.toml runs/<run_id>
python -m binding_gate.scorers.llm_judge run --confirm-spend --config configs/rescore/dev_llm_judge.toml runs/<run_id>
python -m binding_gate.scorers.llm_judge agreement runs/<run_id> --json judge_agreement.json
```

## 5. Decomposer evaluation (CLI only; estimate 0.03-0.16 USD, cap 0.50 USD)

Not in the workflow. Needs `OPENAI_API_KEY` in your shell and the benchmark in
`data/raw/` (local setup).

```bash
python -m binding_gate.evaluation.decomposer dry-run --config configs/decomposer_eval_dev.toml
python -m binding_gate.evaluation.decomposer run --config configs/decomposer_eval_dev.toml --confirm-spend
python -m binding_gate.evaluation.decomposer report runs/<run_id> --json decomposer_report.json
```

The report compares the result with the thresholds of PREREGISTRATION.md
section 7 (section 17, item 29).

## 6. Gated arm on dev, then calibration (caps 349 USD in total)

Gated run: workflow.

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_gated.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty, or the number of the step-4 workflow run if you added seed runs to the config (see "Reuse the step-4 answers") |

Without seed runs this is a new run and the `no_gate` answers of step 4 are paid
again. To avoid that, reuse them first (below). Calibration (CLI only, no key, no
model call; reads the downloaded `runs-<label>` artifacts of this run and the dev
data):

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


### Reuse the step-4 answers (optional, saves the answer-call cost of steps 6 and 7)

`dev_gated.toml`, the five ablation configs and `dev_baselines.toml` send exactly
the `no_gate` answer request of step 4 for every item, model and repeat. With
`seed_responses_from` in the config's `[runner]` table, the runner takes those
answers from the step-4 traces instead of paying for them again; each reused
answer is recorded in the new trace with `cache_hit` and `seeded_from` (the step-4
run id) and costs nothing. Only the other calls (decomposer, judge,
`prompt_entity_check`, MiniCheck on CPU) are paid. Steps, once per config:

1. Open the step-4 run in Actions. The number at the end of its URL
   (`.../actions/runs/<number>`) is the value of the field "Seed runs" in the
   dialog. Its artifacts `runs-<model label>` each contain one folder named by
   the run id of that model's job (`20261201T101500Z-...`); see it in the
   artifact, or locally with
   `gh run download <number> -n runs-openai-low -D seeds/` and `ls seeds/`.
2. In a pull request, add to `[runner]` of the config one entry per model of
   step 4 (paths always start with `seeds/`, where the workflow downloads them):

   ```toml
   seed_responses_from = ["seeds/<run id of openai-low>", "seeds/<run id of openai-mid>", "seeds/<run id of anthropic-low>", "seeds/<run id of anthropic-mid>", "seeds/<run id of google-low>", "seeds/<run id of google-mid>"]
   ```

3. Run the dry run of the config with "Seed runs" = that number. Its log lists,
   per model, how many answer calls are seeded (`SEEDED RESPONSES`); the cost
   estimate above it does not subtract them. Then the paid run with the same
   value.

The run refuses (before any paid call) a seed that is not a dev run, that used
another dataset pin, another model spec for the same label (timeouts and retries
aside) or another answer prompt, a live-web run, or a seed trace modified after
its run closed; a test run never uses seeds. A model with no seeded answer is
paid normally (warning in the log). Locally, put each seed folder under `seeds/`
with the `gh run download` command above (one per model label) and run the CLI
as usual.
## 7. Comparison baselines and per-check ablations

**Comparison baselines** (`configs/dev_baselines.toml`: `no_gate`,
`prompt_entity_check`, `faithfulness_checker`, `llm_judge_checks`): workflow
(caps 1,380 USD in total; each model's cap covers its answer calls and the judge
calls made for its candidates; dry-run estimate 123-1,332 USD in total, the
judge's share being at most 110 USD per model because only answered candidates are
judged). The judge's own cap, `judge_max_usd = 110`, applies per model. Print the
judge's upper bound, in total and per model job (CLI, no key):

```bash
python -m binding_gate.arms.llm_judge_checks estimate --config configs/dev_baselines.toml
```

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baselines.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty, or the number of the step-4 workflow run if you added seed runs to the config (see "Reuse the step-4 answers") |

Needs `MOONSHOT_API_KEY` (the judge). Before the preflight each job downloads the
pinned MiniCheck weights (about 3 GB, cached between runs), runs
`python -m binding_gate.arms.faithfulness_checker prepare` and `verify`, and stops
if the weights are not the pinned revision or the two test examples are not
labelled as expected; nothing is paid by then. `verify` prints the time per check
in the job log (section 17, item 27); a dry run of this config already does all
of this, for free. The same locally (CLI; all model keys and `MOONSHOT_API_KEY` in
your shell; CPU only):

```bash
python -m pip install -e ".[faithfulness]"
python -m binding_gate.arms.faithfulness_checker prepare
python -m binding_gate.arms.faithfulness_checker verify
python -m binding_gate.cli dry-run --config configs/dev_baselines.toml
python -m binding_gate.cli run --config configs/dev_baselines.toml --confirm-spend
```

An interrupted run is continued with `--resume runs/<run_id>`.

**Per-check ablations**: workflow, five runs, one per file (caps 349 USD per
file). Run each with:

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | one of `configs/dev_ablation_quote_verbatim.toml`, `configs/dev_ablation_entity_in_window.toml`, `configs/dev_ablation_qualifiers_in_window.toml`, `configs/dev_ablation_no_closer_competitor.toml`, `configs/dev_ablation_premise_present.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty, or the number of the step-4 workflow run if you added seed runs to the config (see "Reuse the step-4 answers") |

Analysis of each, CLI: `python -m binding_gate.cli analyze runs/<run_id> [...] --out analysis-<name>`.

## 8. Layer 3 (live web): smoke, then dev

Needs `SERPAPI_API_KEY`, `RUNS_ENCRYPTION_PASSPHRASE` and `OPENAI_API_KEY`
(slot A and the decomposer). The workflow downloads SealQA at its pinned
revision itself (`cli dataset-prepare`).

Smoke (5 dev questions, slot A, 1 repeat; estimate 0.13-0.28 USD, cap 1 USD):

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_live_smoke.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty |

Dev run (every dev question, 6 models, 3 repeats, one job; caps 165 USD in
total; run a dry run of the same config first):

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

Layer 2 (GRAB-RAG) is not in this sequence: its configs stop until the
misleading-context export exists, and its role is an open owner decision
(section 17, item 17).

## 9. Freeze

1. Close every item of PREREGISTRATION.md section 17 that applies to the layer
   being frozen (owner approvals and decisions recorded with their date).
2. In one pull request: approve `configs/test_layer1.toml` (its arms are those of
   the dev configs since PR 12; its caps are placeholders, item 13), update
   `PARAMETERS_STATUS` in `src/binding_gate/mcp/core.py` (item 42), change the
   status line of PREREGISTRATION.md to `**Status:** FROZEN` and its version.
   Merge it into `main`; the tests must pass.
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

## 10. Frozen test run, layer 1 (CLI only)

Not in the workflow. On your computer, in a clean checkout of the tag:

```bash
git fetch --tags && git checkout prereg-freeze-1
git status                                   # must show a clean tree
python -m pip install -e ".[dev,plots,faithfulness]"
python -m binding_gate.arms.faithfulness_checker prepare
# benchmark in data/raw/ as in "Local setup"; all model keys and MOONSHOT_API_KEY in the shell
python -m binding_gate.cli frozen-test-run --config configs/test_layer1.toml \
    --tag prereg-freeze-1 --purpose "layer-1 confirmatory test run" --confirm-spend
```

It refuses (and logs the refusal) unless the tag, a clean tree and the FROZEN
status hold; then it runs every arm on the test split and writes
`runs/<run_id>/analysis/` (`analysis.json`, `report.md`, figures). If the
Anthropic key is absent, pass the other labels with `--model
openai-low,openai-mid,google-low,google-mid` (one access for those models;
whether to use one access per model is section 17, item 18). A run stopped by a
budget cap is continued with `--resume runs/<run_id>` (a further logged
access). Then commit the access log:

```bash
git switch -c test-access-log-1
git add audit/test_access_log.jsonl
git commit -m "Test-split access log after the layer-1 frozen test run"
git push -u origin test-access-log-1          # open and merge a pull request
```

Layers 2 and 3 have no frozen test configuration yet (section 17, item 39).

## 11. Human audit and public report (CLI only, no key)

```bash
python -m binding_gate.audit_tool sample runs/<test_run_id> --out audit/sample.json --html audit/audit.html
# label: open audit/audit.html in a browser (offline) and press "Export labels", or in the terminal:
python -m binding_gate.audit_tool label --sample audit/sample.json --out audit/labels.json --auditor "<name>"
python -m binding_gate.audit_tool analyze --sample audit/sample.json --labels audit/labels.json --out audit/audit.json
python -m binding_gate.report build --analysis runs/<test_run_id>/analysis --run-dir runs/<test_run_id> \
    --baseline baseline.json --audit audit/audit.json --out public-report/
```

The sample draws 50 blocked and 50 accepted gate decisions with the fixed seed
(PREREGISTRATION.md section 13); who audits is section 17, item 19. The report
(`public-report/report.md`, `public-report/report.html`) refuses an analysis
that marks a result confirmatory while the pre-registration is not FROZEN.

---

## הוראות הפעלה בעברית: הרצף המלא

**שום דבר כאן עדיין לא הורץ מול API אמיתי.** הלקוחות של המודלים, של החיפוש ושל הורדת הדפים נבדקו רק מול תשובות לדוגמה. כל העלויות הן הערכות dry run או תקרות מהקונפיגים, לא מדידות; כל תקרה היא ערך PROPOSED (סעיף 17, פריט 13 ב-PREREGISTRATION.md).

שני סוגי שלבים:

- **Workflow** — רץ ב-GitHub Actions, ה-workflow בשם **baseline**. מריץ רק את `cli run` (או dry run) על הקונפיג שבוחרים, על ה-dev split, אחרי ההכנה החינמית שהקונפיג צריך (נתונים, משקולות MiniCheck, seed runs).
- **CLI בלבד** — אין לו workflow. רץ על המחשב שלך (ראו "הכנה מקומית") עם הפקודה המדויקת שכתובה.

### איפה לוחצים (בכל שלב workflow)

GitHub ← הריפו ← לשונית **Actions** ← בעמודה השמאלית **baseline** ← כפתור **Run workflow** מימין ← למלא את השדות בדיוק לפי הטבלה של השלב ← הכפתור הירוק **Run workflow**. "מסומן" / "לא מסומן" מתייחס לשתי תיבות הסימון. ריצה בתשלום קורית רק כש-"Dry run" לא מסומן **וגם** תיבת האישור מסומנת. השדה החמישי, "Seed runs" (`seed_run_id`), נשאר ריק, חוץ מבשלבים 6 ו-7 כשהוספת לקונפיג seed runs (ראו "שימוש חוזר בתשובות של שלב 4").

תוויות המודלים בכל קונפיג: `openai-low`, `openai-mid`, `anthropic-low`, `anthropic-mid`, `google-low`, `google-mid` (בקונפיג ה-smoke של שכבה 3 יש רק `openai-low`). עם `all` כל מודל רץ כ-job נפרד במקביל, חוץ מקונפיגים של web חי (שכבה 3) שרצים כ-job אחד על כל המודלים שיש להם מפתח.

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
| `ANTHROPIC_API_KEY` | Anthropic (אופציונלי) | סלוטים C, D; בלעדיו הם מדולגים והמחקר ממשיך עם A, B, E, F |
| `MOONSHOT_API_KEY` | Moonshot / Kimi | השופט (סלוט J): `configs/dev_baselines.toml` וה-re-scoring של השופט |
| `SERPAPI_API_KEY` | SerpApi (חיפוש) | שכבה 3: `configs/dev_live_smoke.toml`, `configs/dev_live.toml` |
| `RUNS_ENCRYPTION_PASSPHRASE` | סיסמה ארוכה שבוחרים ושומרים | שכבה 3: ה-artifacts מוצפנים בה; ריצה בתשלום של web חי מסרבת בלעדיה |

מודל שהמפתח שלו חסר מדולג עם אזהרה, והשאר ממשיכים. מפתח חסר של רכיב עזר (מפתח OpenAI של ה-decomposer, מפתח Moonshot של השופט, מפתח SerpApi) מדלג על כל המודלים של קונפיג שמשתמש ברכיב הזה.

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

מה יוצא: מספר הקבוצות, הפריטים והקריאות, הערכת עלות לכל מודל, ובדיקת preflight חינמית שמזהה המודל קיים אצל הספק. Job אדום = מזהה מודל לא נמצא או מפתח לא תקין; לא עלה כסף. מזהה שגוי מתקנים ב-pull request (קונפיגים וסעיף 9), לא בדיאלוג. לפני ריצה ראשונה בתשלום של כל קונפיג אחר עושים אותו dry run (משנים רק את "Run config").

### שלב 3 — Smoke (הערכה 1 עד 6 דולר בסך הכול; תקרה 2.50 דולר למודל)

Workflow.

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_smoke.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק |

75 קריאות למודל. לבדוק לכל מודל ב-artifact `baseline-report` וביומן: אין רשומות `error` ואין HTTP 400 (400 שמזכיר `temperature` = הספק דוחה את ההגדרה; סעיף 17, פריט 24); שיעור `unparseable` נמוך; כמה טוקני reasoning דווחו בפועל — לפי זה מורידים את התקרות של הקונפיגים הבאים (פריט 13). מספרי ה-smoke אינם תוצאה.

### שלב 4 — Baseline מלא ודוח כלל העצירה (תקרות: 349 דולר בסך הכול)

Workflow.

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baseline.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק |

4,365 קריאות למודל (הערכה 59 עד 334 דולר בסך הכול). להרצה חוזרת של מודל אחד כותבים את התווית שלו במקום `all`. ה-job האחרון, **baseline report**, מדפיס בעמוד הסיכום את התוצאה (CONTINUE / STOP / IN-BETWEEN / MEASUREMENT-CHECK) ומעלה את `baseline-report/baseline_report.json` — לשמור, זה הקלט `--baseline` של שלב 11. אותו דוח מקומית: `python -m binding_gate.cli report runs/<run_id> [...] --json baseline.json`.

STOP מסיים את המחקר. מודלים מתחת ל-5% לא ממשיכים: מוציאים אותם מהקונפיגים הבאים ב-pull request. אם התוצאה MEASUREMENT-CHECK, מריצים את השופט המשני על ה-traces (CLI בלבד; צריך `MOONSHOT_API_KEY`; תקרה 100 דולר):

```bash
python -m binding_gate.scorers.llm_judge dry-run --config configs/rescore/dev_llm_judge.toml runs/<run_id>
python -m binding_gate.scorers.llm_judge run --confirm-spend --config configs/rescore/dev_llm_judge.toml runs/<run_id>
python -m binding_gate.scorers.llm_judge agreement runs/<run_id> --json judge_agreement.json
```

### שלב 5 — מדידת ה-decomposer (CLI בלבד; הערכה 0.03 עד 0.16 דולר, תקרה 0.50)

אין workflow. צריך `OPENAI_API_KEY` ב-shell ואת הבנצ'מרק ב-`data/raw/`.

```bash
python -m binding_gate.evaluation.decomposer dry-run --config configs/decomposer_eval_dev.toml
python -m binding_gate.evaluation.decomposer run --config configs/decomposer_eval_dev.toml --confirm-spend
python -m binding_gate.evaluation.decomposer report runs/<run_id> --json decomposer_report.json
```

הדוח משווה לספים של סעיף 7 (סעיף 17, פריט 29).

### שלב 6 — ה-gate על dev, ואז כיול (תקרות: 349 דולר בסך הכול)

Workflow.

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_gated.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק — או, אם הוספת לקונפיג seed runs, המספר של ריצת ה-workflow של שלב 4 (ראו "שימוש חוזר בתשובות של שלב 4") |

בלי seed runs זו ריצה חדשה, והתשובות של `no_gate` משלב 4 משולמות שוב; כדי לחסוך — קודם שימוש חוזר בהן (למטה). כיול — CLI בלבד, בלי מפתח ובלי קריאה למודל, על ה-artifacts של הריצה הזאת ועל נתוני ה-dev:

```bash
python -m binding_gate.gates.calibration runs/<run_id> [runs/<run_id> ...] \
    --window-tokens 20,40,60,80 --premise-scope document,window --competitor-tie fail,pass \
    --json calibration.json
python -m binding_gate.cli analyze runs/<run_id> [runs/<run_id> ...] --out analysis-dev-gated
```

השורה עם הערכים המוגדרים חייבת להראות `changed_vs_recorded = 0`. זה הניתוח הראשון אחרי חיבור המפתחות (פריט 30: הסיכון הפתוח ל-H2). פרמטר שמשתנה בעקבותיו משתנה בקונפיגים ובסעיף 5.1 ב-pull request, לפני ההקפאה. ניתוחי dev הם גישושיים.

#### שימוש חוזר בתשובות של שלב 4 (אופציונלי; חוסך את עלות קריאות התשובה בשלבים 6 ו-7)

`dev_gated.toml`, חמשת קונפיגי ה-ablation ו-`dev_baselines.toml` שולחים בדיוק את בקשת התשובה של `no_gate` משלב 4 לכל פריט, מודל וחזרה. עם `seed_responses_from` בטבלת `[runner]` של הקונפיג, ה-runner לוקח את התשובות האלה מה-traces של שלב 4 במקום לשלם עליהן שוב; כל תשובה כזאת נרשמת ב-trace החדש עם `cache_hit` ו-`seeded_from` (מזהה הריצה של שלב 4) ועולה 0. משלמים רק על שאר הקריאות (decomposer, שופט, `prompt_entity_check`, MiniCheck על CPU). פעם אחת לכל קונפיג:

1. לפתוח ב-Actions את הריצה של שלב 4. המספר בסוף ה-URL שלה (`.../actions/runs/<מספר>`) הוא הערך של השדה "Seed runs" בדיאלוג. בכל artifact `runs-<תווית מודל>` שלה יש תיקייה אחת ששמה מזהה הריצה של ה-job של אותו מודל (`20261201T101500Z-...`); רואים אותה ב-artifact, או מקומית: `gh run download <מספר> -n runs-openai-low -D seeds/` ואז `ls seeds/`.
2. ב-pull request, להוסיף ל-`[runner]` של הקונפיג רשומה לכל מודל של שלב 4 (הנתיב תמיד מתחיל ב-`seeds/`, לשם ה-workflow מוריד אותן):

   ```toml
   seed_responses_from = ["seeds/<run id של openai-low>", "seeds/<run id של openai-mid>", "seeds/<run id של anthropic-low>", "seeds/<run id של anthropic-mid>", "seeds/<run id של google-low>", "seeds/<run id של google-mid>"]
   ```

3. להריץ dry run של הקונפיג עם "Seed runs" = אותו מספר. ביומן יופיע לכל מודל כמה קריאות תשובה נלקחות מה-seed (`SEEDED RESPONSES`); הערכת העלות שמעליו לא מורידה אותן. אחר כך הריצה בתשלום עם אותו ערך.

הריצה מסרבת (לפני כל קריאה בתשלום) ל-seed שאינו ריצת dev, שהשתמש בגרסת נתונים אחרת, ב-spec מודל אחר לאותה תווית (חוץ מ-timeouts ו-retries) או ב-prompt תשובה אחר, לריצת web חי, או ל-trace של seed ששונה אחרי שהריצה נסגרה; ריצת test אף פעם לא משתמשת ב-seeds. מודל בלי אף תשובה מה-seed משלם כרגיל (אזהרה ביומן). מקומית: לשים כל תיקיית seed תחת `seeds/` עם פקודת `gh run download` שלמעלה (אחת לכל תווית מודל) ולהריץ את ה-CLI כרגיל.

### שלב 7 — השוואות ו-ablations

**השוואות** (`configs/dev_baselines.toml`): workflow (תקרות 1,380 דולר בסך הכול; התקרה של כל מודל כוללת את קריאות התשובה שלו ואת קריאות השופט על התשובות שלו; הערכת dry run 123 עד 1,332 דולר בסך הכול, מתוכם השופט לכל היותר 110 דולר למודל, כי נשפטות רק תשובות שניתנו). גם התקרה של השופט עצמו, `judge_max_usd = 110`, היא לכל מודל. החסם העליון של השופט, בסך הכול ולכל job של מודל (CLI, בלי מפתח):

```bash
python -m binding_gate.arms.llm_judge_checks estimate --config configs/dev_baselines.toml
```

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baselines.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק — או, אם הוספת לקונפיג seed runs, המספר של ריצת ה-workflow של שלב 4 (ראו "שימוש חוזר בתשובות של שלב 4") |

צריך `MOONSHOT_API_KEY` (השופט). לפני ה-preflight כל job מוריד את המשקולות המוצמדות של MiniCheck (כ-3GB, נשמרות ב-cache בין ריצות), מריץ `python -m binding_gate.arms.faithfulness_checker prepare` ו-`verify`, ונעצר אם המשקולות אינן הגרסה המוצמדת או ששתי דוגמאות הבדיקה לא סווגו כצפוי; עד אז לא שולם כלום. `verify` מדפיס ביומן את הזמן לבדיקה (פריט 27); כבר dry run של הקונפיג הזה עושה את כל זה, בחינם. אותו דבר מקומית (CLI; כל מפתחות המודלים ו-`MOONSHOT_API_KEY` ב-shell; CPU בלבד):

```bash
python -m pip install -e ".[faithfulness]"
python -m binding_gate.arms.faithfulness_checker prepare
python -m binding_gate.arms.faithfulness_checker verify
python -m binding_gate.cli dry-run --config configs/dev_baselines.toml
python -m binding_gate.cli run --config configs/dev_baselines.toml --confirm-spend
```

ריצה שנקטעה ממשיכים עם `--resume runs/<run_id>`.

**Ablations** — workflow, חמש ריצות, אחת לכל קובץ (תקרות 349 דולר לקובץ):

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | אחד מ: `configs/dev_ablation_quote_verbatim.toml`, `configs/dev_ablation_entity_in_window.toml`, `configs/dev_ablation_qualifiers_in_window.toml`, `configs/dev_ablation_no_closer_competitor.toml`, `configs/dev_ablation_premise_present.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק — או, אם הוספת לקונפיג seed runs, המספר של ריצת ה-workflow של שלב 4 (ראו "שימוש חוזר בתשובות של שלב 4") |

ניתוח של כל אחד (CLI): `python -m binding_gate.cli analyze runs/<run_id> [...] --out analysis-<name>`.

### שלב 8 — שכבה 3 (web חי): smoke ואז dev

צריך `SERPAPI_API_KEY`, `RUNS_ENCRYPTION_PASSPHRASE` ו-`OPENAI_API_KEY` (סלוט A וה-decomposer). ה-workflow מוריד את SealQA בגרסה המוצמדת בעצמו.

Smoke (5 שאלות dev, סלוט A, חזרה אחת; הערכה 0.13 עד 0.28 דולר, תקרה 1 דולר):

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_live_smoke.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק |

ריצת dev (כל שאלות ה-dev, 6 מודלים, 3 חזרות, job אחד; תקרות 165 דולר בסך הכול; קודם dry run של אותו קונפיג):

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

שכבה 2 (GRAB-RAG) לא ברצף: הקונפיגים שלה נעצרים עד שיש export של ההקשרים המטעים, והתפקיד שלה הוא החלטה פתוחה שלך (פריט 17).

### שלב 9 — הקפאה

1. לסגור כל פריט בסעיף 17 שנוגע לשכבה שמוקפאת (אישורים והחלטות עם תאריך).
2. ב-pull request אחד: לאשר את `configs/test_layer1.toml` (הזרועות שלו זהות לקונפיגי ה-dev מאז PR 12; התקרות שלו placeholders, פריט 13), לעדכן את `PARAMETERS_STATUS` ב-`src/binding_gate/mcp/core.py` (פריט 42), לשנות את שורת הסטטוס ל-`**Status:** FROZEN` ואת הגרסה. למזג ל-`main`; הבדיקות חייבות לעבור.
3. לתייג בדיוק את ה-merge commit בשם `prereg-freeze-1` (לשכבות הבאות `-2`, `-3`). ב-GitHub: **Releases ← Draft a new release ← Choose a tag** ← להקליד `prereg-freeze-1` ← **Create new tag on publish** ← Target: `main` ← **Publish release**. או ב-CLI:

   ```bash
   git fetch origin && git tag -a prereg-freeze-1 -m "Pre-registration frozen (layer 1)" origin/main
   git push origin prereg-freeze-1
   ```

4. מעכשיו PREREGISTRATION.md משתנה רק דרך יומן התיקונים (סעיף 18).

### שלב 10 — ריצת ה-test המוקפאת, שכבה 1 (CLI בלבד)

אין workflow. על המחשב שלך, ב-checkout נקי של התג:

```bash
git fetch --tags && git checkout prereg-freeze-1
git status                                   # חייב להיות נקי
python -m pip install -e ".[dev,plots,faithfulness]"
python -m binding_gate.arms.faithfulness_checker prepare
# הבנצ'מרק ב-data/raw/ כמו ב"הכנה מקומית"; כל מפתחות המודלים ו-MOONSHOT_API_KEY ב-shell
python -m binding_gate.cli frozen-test-run --config configs/test_layer1.toml \
    --tag prereg-freeze-1 --purpose "layer-1 confirmatory test run" --confirm-spend
```

הפקודה מסרבת (ורושמת את הסירוב) אם התג, העץ הנקי או הסטטוס FROZEN לא מתקיימים; אחרת מריצה את כל הזרועות על ה-test וכותבת `runs/<run_id>/analysis/`. אם אין מפתח Anthropic: `--model openai-low,openai-mid,google-low,google-mid` (גישה אחת לכל המודלים האלה; האם גישה לכל מודל בנפרד — פריט 18). ריצה שנעצרה בתקרה ממשיכים עם `--resume runs/<run_id>` (גישה נוספת שנרשמת). אחר כך commit ליומן הגישה:

```bash
git switch -c test-access-log-1
git add audit/test_access_log.jsonl
git commit -m "Test-split access log after the layer-1 frozen test run"
git push -u origin test-access-log-1          # לפתוח pull request ולמזג
```

לשכבות 2 ו-3 עדיין אין קונפיג test מוקפא (פריט 39).

### שלב 11 — audit אנושי ודוח ציבורי (CLI בלבד, בלי מפתח)

```bash
python -m binding_gate.audit_tool sample runs/<test_run_id> --out audit/sample.json --html audit/audit.html
# תיוג: לפתוח את audit/audit.html בדפדפן (בלי אינטרנט) וללחוץ "Export labels", או בטרמינל:
python -m binding_gate.audit_tool label --sample audit/sample.json --out audit/labels.json --auditor "<name>"
python -m binding_gate.audit_tool analyze --sample audit/sample.json --labels audit/labels.json --out audit/audit.json
python -m binding_gate.report build --analysis runs/<test_run_id>/analysis --run-dir runs/<test_run_id> \
    --baseline baseline.json --audit audit/audit.json --out public-report/
```

המדגם: 50 החלטות חסומות ו-50 מאושרות עם seed קבוע (סעיף 13); מי מתייג — פריט 19. הדוח (`public-report/report.md`, `public-report/report.html`) מסרב לניתוח שמסמן תוצאה כמאששת כל עוד ה-pre-registration לא FROZEN.
