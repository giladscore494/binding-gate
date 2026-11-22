# Running the PR 2 baseline

> **The full operating sequence** (all secrets, the decomposer evaluation, the
> gated runs and calibration, baselines and ablations, layer 3, the freeze, the
> frozen test run, the audit and the report) is in [RUNNING.md](RUNNING.md),
> in English and Hebrew. This file covers its first steps (dry run, smoke run,
> dev baseline) and stays as written for PR 2; the secrets table below lists
> only the PR 2 keys.

The baseline is started from GitHub, by choosing a config file. Nothing is
configured through environment variables; API keys are repository secrets.

**Nothing in this procedure has been run against a live API yet.** The model
clients were tested against recorded-shape fixtures only. The smoke run (step 3)
exists to find out, for a few dollars at most, whether the live APIs behave as coded.

## 0. One-time setup: repository secrets

GitHub → repository → **Settings → Secrets and variables → Actions → New repository secret**.

| Secret name | For | Needed by |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI | slots A, B |
| `ANTHROPIC_API_KEY` | Anthropic | slots C, D |
| `GEMINI_API_KEY` | Google Gemini | slots E, F |
| `MOONSHOT_API_KEY` | Moonshot / Kimi | secondary judge (not used by PR 2 runs) |

A model whose secret is missing is **skipped with a warning**; the other models
still run. If slots C and D cannot be run, PREREGISTRATION.md section 9 says how
the study proceeds.

## 1. Where to click

GitHub → repository → **Actions** → workflow **baseline** (left column) →
**Run workflow** (right side). Fill the dialog as in the tables below, then
press the green **Run workflow** button.

## 2. Dry run (free; no API key is used for the estimate)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baseline.toml` |
| Model label from the config, or "all" | `all` |
| Dry run | checked |
| I confirm that paid API calls may be made | unchecked |

What it does per model: clones the benchmark at the pinned commit, prints the
number of dev groups, items and calls and the cost estimate (low / high /
ceiling / budget cap), then runs the free **preflight** that asks the provider
whether the configured model id exists. A red job here means a model id was not
found (or a key is invalid); it cost nothing. Fix the id through an amendment to
PREREGISTRATION.md section 9 and the configs, not by hand.

## 3. Smoke run (estimate 1 to 6 USD in total; hard cap 2.50 USD per model)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_smoke.toml` |
| Model label from the config, or "all" | `all` |
| Dry run | unchecked |
| I confirm that paid API calls may be made | checked |

75 calls per model (first 2 dev groups, i.e. 2 (X, C) pairs with 5 triples; 1 repeat). Afterwards open the run's
summary page and the `baseline-report` artifact and check, per model:

- no `error` records and no HTTP 400 in the job log (a 400 that mentions
  `temperature` means the provider rejects that setting: see the header of the
  config files);
- "unparseable share" is small (if it is large, the answer-format instruction or
  the parser needs work on dev before the full run);
- "tokens reported" shows the real reasoning-token usage. Use it to lower the
  budget caps in `configs/dev_baseline.toml` if the real usage is far below the
  high estimate.

The smoke numbers are not a result and are not reported as one.

## 4. Full dev baseline (4,365 calls per model)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baseline.toml` |
| Model label from the config, or "all" | `all` |
| Dry run | unchecked |
| I confirm that paid API calls may be made | checked |

Each model runs as its own parallel job. To run or re-run a single model, enter
its label instead of `all`: `openai-low`, `openai-mid`, `anthropic-low`,
`anthropic-mid`, `google-low` or `google-mid`.

Results: each job uploads an artifact `runs-<model label>` (manifest, complete
traces, per-model report). The final job **baseline report** prints the
stop-rule outcome (CONTINUE / STOP / IN-BETWEEN / MEASUREMENT-CHECK) per model
and overall on the run's summary page and uploads it as artifact
`baseline-report`.

## Safeguards

- A paid run is refused unless "Dry run" is unchecked **and** the confirmation
  box is checked.
- Before any paid call the run checks: a budget cap and a price exist for the
  model; the low cost estimate fits under the cap; the model id exists
  (preflight).
- **Budget cap** (`[runner] max_usd` in the config, per model): when the usage a
  provider reports reaches the cap, no further call is started for that model;
  the job ends red with "ABORTED: budget cap reached" and its partial traces are
  uploaded. Worst case for `configs/dev_baseline.toml`: 349 USD (sum of the
  caps; dry-run estimate 59 to 334 USD); for `configs/dev_smoke.toml`: 15 USD.
- Only the dev split is used. The test split is reachable only through the test
  lock, which refuses while PREREGISTRATION.md is a DRAFT.

## Limits of the workflow

- A job that is cancelled or times out (350 minutes) keeps its partial traces as
  an artifact, but the workflow cannot continue it: a re-run starts that model
  again from zero. Continuing an interrupted run is supported locally
  (`python -m binding_gate.cli run ... --resume runs/<run_id>`).
- Provider batch APIs (roughly half price) are not used in PR 2; they are a
  possible later cost option.

## Running locally (optional)

```bash
python -m pip install -e ".[dev]"
git clone https://github.com/CedricCaruzzo/deceptive-grounding-benchmark data/raw/deceptive-grounding-benchmark
git -C data/raw/deceptive-grounding-benchmark checkout 61b0281d8d3897f53bef3f7402727c905614c1e4
python -m binding_gate.cli dry-run   --config configs/dev_smoke.toml
python -m binding_gate.cli preflight --config configs/dev_smoke.toml          # needs the API keys
python -m binding_gate.cli run       --config configs/dev_smoke.toml --confirm-spend
python -m binding_gate.cli report    runs/<run_id>
```

---

## הוראות הפעלה בעברית

**הרצף המלא** (כל ה-secrets, כל השלבים עד ההקפאה, ריצת ה-test, ה-audit והדוח) נמצא ב-[RUNNING.md](RUNNING.md). הקובץ הזה מכסה רק את השלבים הראשונים.

**שום דבר כאן עדיין לא הורץ מול API אמיתי.** הלקוחות נבדקו רק מול תשובות לדוגמה. ריצת ה-smoke (שלב ב) נועדה לבדוק בדולרים בודדים שה-API האמיתי מתנהג כמו בקוד.

**הכנה חד-פעמית:** ב-GitHub ← הריפו ← Settings ← Secrets and variables ← Actions ← New repository secret, להוסיף: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` (ו-`MOONSHOT_API_KEY` לשופט המשני; לא נדרש לריצות של PR 2). מודל שהמפתח שלו חסר מדולג עם אזהרה, והשאר ממשיכים.

**איפה לוחצים:** GitHub ← הריפו ← לשונית **Actions** ← בעמודה השמאלית workflow בשם **baseline** ← כפתור **Run workflow** מימין ← למלא את השדות לפי הטבלה ← הכפתור הירוק **Run workflow**.

### שלב א — Dry run (חינם)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baseline.toml` |
| Model label from the config, or "all" | `all` |
| Dry run | מסומן |
| I confirm that paid API calls may be made | לא מסומן |

מה יוצא: מספר הקבוצות, הפריטים והקריאות, הערכת עלות לכל מודל, ובדיקת preflight חינמית שמזהה המודל קיים אצל הספק. Job אדום בשלב הזה = מזהה מודל לא נמצא או מפתח לא תקין; לא עלה כסף.

### שלב ב — Smoke (הערכה: 1 עד 6 דולר בסך הכול; תקרה קשיחה 2.50 דולר למודל)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_smoke.toml` |
| Model label from the config, or "all" | `all` |
| Dry run | לא מסומן |
| I confirm that paid API calls may be made | מסומן |

אחרי הריצה לפתוח את עמוד הסיכום ואת ה-artifact בשם `baseline-report` ולבדוק לכל מודל: אין רשומות `error`, שיעור `unparseable` נמוך, וכמה טוקנים של reasoning דווחו בפועל.

### שלב ג — Baseline מלא (4,365 קריאות למודל)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_baseline.toml` |
| Model label from the config, or "all" | `all` |
| Dry run | לא מסומן |
| I confirm that paid API calls may be made | מסומן |

להרצת מודל בודד: במקום `all` לכתוב את התווית שלו — `openai-low`, `openai-mid`, `anthropic-low`, `anthropic-mid`, `google-low` או `google-mid`.

התוצאה: ה-job האחרון, **baseline report**, מדפיס בעמוד הסיכום את תוצאת כלל העצירה (CONTINUE / STOP / IN-BETWEEN / MEASUREMENT-CHECK) לכל מודל ובסך הכול. תקרת התקציב הקשיחה לריצה המלאה: 349 דולר לכל היותר (סכום התקרות למודל); הערכת ה-dry run היא 59 עד 334 דולר.
