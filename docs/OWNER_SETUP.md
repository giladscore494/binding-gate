# הגדרה חד-פעמית לבעלים / One-time owner setup

English version: [below](#english).

## הגדרה חד-פעמית: מה אתה עושה, פעם אחת

אחרי הדף הזה אין לך שום דבר נוסף לעשות: כל הריצות, הבדיקות והדוחות רצים ב-GitHub Actions, בלי לערוך אף קובץ ובלי שורת פקודה מקומית. ה-orchestrator (בינה מלאכותית שיכולה להפעיל workflows ולקרוא את הלוגים ואת ה-branches שלהם דרך ה-API של GitHub, אבל **לעולם לא רואה ערך של secret** ולא יכולה להוריד artifacts) מפעיל את השלבים וקורא את התוצאות מה-branch `results-log`.

שלושה דברים, לפי הסדר:

1. ליצור שבעה repository secrets (שלב 1).
2. להגדיר מגבלת הוצאה חודשית אצל כל ספק (שלב 2).
3. להגיד ל-orchestrator שסיימת (שלב 3).

**לעולם לא** להדביק מפתח בצ'אט, ב-issue, ב-pull request או בקובץ. המקום היחיד למפתח הוא תיבת ה-Secret ב-GitHub.

### שלב 1 — ליצור את ה-secrets

הנתיב בתפריט, לכל secret בנפרד: GitHub ← הריפו `giladscore494/binding-gate` ← **Settings → Secrets and variables → Actions → New repository secret**. בשדה **Name** כותבים את השם בדיוק כמו בטבלה (אותיות גדולות, קו תחתון), בשדה **Secret** מדביקים את הערך, ולוחצים **Add secret**.

| שם ה-secret (Name) | מה להדביק (Secret) | איפה משיגים (נתיבים אצל הספקים **לא אומתו**: לא נקראו בדפים הרשמיים) |
|---|---|---|
| `OPENAI_API_KEY` | מפתח API של OpenAI | platform.openai.com ← API keys ← Create new secret key |
| `GEMINI_API_KEY` | מפתח API של Google Gemini | aistudio.google.com ← Get API key (חשבון ה-billing שאתה מממן) |
| `ZAI_API_KEY` | מפתח API של Z.ai (GLM) | z.ai, פלטפורמת ה-API ← API Keys |
| `MOONSHOT_API_KEY` | מפתח API של Moonshot / Kimi | platform.moonshot.ai ← API Keys |
| `SERPER_API_KEY` | מפתח API של Serper (שכבת החינם) | להירשם ב-serper.dev (בהרשמה מקבלים 2,500 חיפושים חינם, בלי כרטיס אשראי), ואז ב-dashboard של serper.dev להעתיק את ה-API key. **לא להזין אמצעי תשלום.** (PR 27: ה-secret הישן `SERPAPI_API_KEY` כבר לא בשימוש; אפשר למחוק אותו.) |
| `RUNS_ENCRYPTION_PASSPHRASE` | סיסמה ארוכה **שאתה ממציא** (למשל 6 מילים אקראיות) | לשמור אותה אצלך (מנהל סיסמאות). בה מוצפנות ריצות ה-web החי (שכבה 3); בלעדיה אי אפשר לפענח אותן אחר כך. |
| `SEC_CONTACT_EMAIL` | כתובת האימייל **שלך** | נדרש **רק לשכבה 2** (דוחות SEC): כללי הגישה של SEC מחייבים כל לקוח אוטומטי להצהיר על אימייל ליצירת קשר. הוא נשמר כ-secret כדי שלעולם לא יופיע בריפו הציבורי, ב-artifacts או בלוגים. |

`ANTHROPIC_API_KEY` ו-`DEEPSEEK_API_KEY` **לא** נדרשים: לא להוסיף. secret שחסר לא שובר כלום: המודל שלו מדולג עם אזהרה (בלי `SEC_CONTACT_EMAIL` רק שכבה 2 לא רצה).

**הגדרה אופציונלית אחת:** Settings → Actions → General → בתחתית, **Allow GitHub Actions to create and approve pull requests** — לסמן. כך ה-workflow של ריצת ה-test המוקפאת פותח בעצמו את ה-pull request שמחזיר את יומן הגישה (`audit/test_access_log.jsonl`) ל-main. אם לא מסמנים, ה-branch בכל זאת נדחף וה-orchestrator פותח את ה-pull request.

### שלב 2 — מגבלת הוצאה חודשית אצל כל ספק (רשת ביטחון חיצונית)

התקציב הכולל של המחקר הוא **50 דולר, קשיח**. בתוך GitHub שומרים עליו התקרות של כל ריצה ו-**ה-ledger** (שמחשב את ההוצאה בפועל מכל הריצות הקודמות, כולל ה-snapshots שנשמרים ב-branch `results-log`, ומסרב לכל ריצה שעלולה לעבור את 50). המגבלות אצל הספקים תופסות רק מה שהקוד לא יכול לתפוס (באג, מחיר שגוי). סכומים מוצעים (מ-docs/RUNNING.md; ביחד 42 דולר):

| ספק | מגבלה מוצעת | איפה (הנתיבים **לא אומתו**) |
|---|---|---|
| OpenAI | 9 דולר | platform.openai.com ← Settings ← Organization ← Limits (תקציב חודשי) |
| Google Gemini | 14 דולר | Google Cloud console ← Billing ← Budgets & alerts (תקציב רק שולח התראה ולא עוצר; לעצירה קשיחה: יתרה משולמת מראש, או לכבות billing אחרי הריצות) |
| Z.ai | 12 דולר | פלטפורמת ה-API של z.ai: יתרה משולמת מראש; להטעין רק את הסכום הזה |
| Moonshot | 7 דולר | platform.moonshot.ai: יתרה משולמת מראש; להטעין רק את הסכום הזה |
| Serper | 0 דולר | להישאר בשכבת החינם (2,500 חיפושים); לא להזין אמצעי תשלום |
| SEC EDGAR | 0 דולר | חינם; אין חשבון ואין מפתח |

### שלב 3 — להגיד ל-orchestrator

לכתוב לו, למשל: "הוספתי את שבעת ה-secrets והגדרתי מגבלות הוצאה". מכאן הוא מפעיל בעצמו את `check-secrets` (חינם), ואחר כך את השלבים לפי הסדר, וקורא את התוצאות מה-branch `results-log` (תיקייה `runs/<מספר הריצה>-<שלב>/` לכל ריצה). אם משהו נכשל בגלל מפתח או הגדרה אצל ספק, הוא יגיד לך בדיוק מה לתקן.

### אם אתה מעדיף ללחוץ בעצמך

GitHub ← הריפו ← לשונית **Actions** ← בעמודה השמאלית שם ה-workflow ← כפתור **Run workflow** (מימין) ← למלא בדיוק לפי הטבלה ← הכפתור הירוק **Run workflow**. לא להפעיל שתי ריצות בתשלום במקביל: ה-ledger מסרב לריצה בתשלום כשריצה אחרת עדיין פעילה.

#### Workflow ‏**dev pipeline** (שלב ה-DEV)

הסדר: `check-secrets`, `smoke`, `dev-baseline`, `dev-rest`, `sec-dataset-prepare` (ואז ה-orchestrator נועץ את ה-hash ב-pull request), `layer2-dev`, `layer3-dev` (או `all-dev`, שמריץ את כל השלבים בתשלום ברצף). אחרי STOP בשכבה 1: `validate-layer1` (תיקון 1); `dev-rest` ו-`all-dev` מסורבים.

**check-secrets** (חינם: אילו secrets קיימים (רק שמות), בדיקות preflight חינמיות, והתקציב שנשאר)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `check-secrets` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | לא מסומן |

**smoke** (תקרה 0.80 דולר; נכשל בכוונה אם צריכת הטוקנים שנמדדה הייתה מעבירה ריצה מתוכננת את התקרה שלה)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `smoke` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | מסומן |

**dev-baseline** (תקרה 4.80 דולר; בסוף מוצגת החלטת כלל העצירה: CONTINUE / STOP / IN-BETWEEN / MEASUREMENT-CHECK)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `dev-baseline` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | מסומן |

**dev-rest** (תקרות 10.20 דולר ביחד; מסרב אם ההחלטה האחרונה של dev-baseline אינה CONTINUE)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `dev-rest` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | מסומן |

**sec-dataset-prepare** (חינם: בונה את מאגר שכבה 2 מ-SEC EDGAR ומדפיס content_sha256; צריך את `SEC_CONTACT_EMAIL`)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `sec-dataset-prepare` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | לא מסומן |

**layer2-dev** (תקרות 2.60 דולר ביחד; שכבה 2 (דוחות SEC): smoke, בדיקת שימוש, baseline, כלל העצירה של שכבה 2 (PR 22), ואז gated ומועמדי binding scope רק אחרי CONTINUE; מסרב כל עוד המאגר לא נעוץ (UNPINNED))

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `layer2-dev` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | מסומן |

**layer3-dev** (תקרות 1.85 דולר; web חי, חיפושים בשכבת החינם של Serper; הרבה פחות מ-5 השעות של פעם, כי אין יותר המתנה של 72 שניות בין חיפושים)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `layer3-dev` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | מסומן |

**validate-layer1** (תקרות 23.65 דולר ביחד; תיקון 1 מ-22.11.2026, ניסיוני: בדיקה של החלטת ה-STOP של שכבה 1. רץ רק אם ההחלטה האחרונה של dev-baseline היא STOP סופי; לא דורש CONTINUE. שופט LLM על 144 פריטי dev לכל מודל מתשובות ה-baseline, תשובות חופשיות (free_form) לאותם פריטים, שופט עליהן, ודוח השוואה)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `validate-layer1` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | מסומן |

**all-dev** (הכול ברצף: smoke ← dev-baseline ← עוצר אם לא CONTINUE ← dev-rest ← layer2-dev ← layer3-dev; תקרות 19.80 דולר ביחד)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `all-dev` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | מסומן |

#### Workflow ‏**frozen test run** (אחרי ההקפאה בלבד)

ריצה אחת לכל שכבה, מהתג שה-orchestrator יוצר בהקפאה. בכל ריצה ה-workflow בודק את התג, את ה-ledger ואת מנעול ה-test, מריץ, ופותח pull request עם יומן הגישה.

**layer1** (שכבה 1, הריצה המאשרת (H1 עד H3); תקרות 16.75 דולר)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Layer whose frozen test config runs (PREREGISTRATION.md section 12) | `layer1` |
| Freeze tag prereg-freeze-<name>; the run uses exactly that commit | התג שה-orchestrator מסר, למשל `prereg-freeze-1` |
| I confirm that paid API calls and one logged test-split access may be made | מסומן |

**layer1_midtier** (שכבה 1, בדיקת המודלים הבינוניים (חקרנית); תקרות 6.95 דולר)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Layer whose frozen test config runs (PREREGISTRATION.md section 12) | `layer1_midtier` |
| Freeze tag prereg-freeze-<name>; the run uses exactly that commit | התג שה-orchestrator מסר, למשל `prereg-freeze-1` |
| I confirm that paid API calls and one logged test-split access may be made | מסומן |

**layer2_sec** (שכבה 2, דוחות SEC (H1 ו-H2); תקרות 2.75 דולר)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Layer whose frozen test config runs (PREREGISTRATION.md section 12) | `layer2_sec` |
| Freeze tag prereg-freeze-<name>; the run uses exactly that commit | התג שה-orchestrator מסר, למשל `prereg-freeze-1` |
| I confirm that paid API calls and one logged test-split access may be made | מסומן |

**layer3_live** (שכבה 3, web חי (H4); תקרות 2.40 דולר; הרבה פחות מ-5.5 השעות של פעם, כי אין יותר המתנה של 72 שניות בין חיפושים)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Layer whose frozen test config runs (PREREGISTRATION.md section 12) | `layer3_live` |
| Freeze tag prereg-freeze-<name>; the run uses exactly that commit | התג שה-orchestrator מסר, למשל `prereg-freeze-1` |
| I confirm that paid API calls and one logged test-split access may be made | מסומן |

#### Workflow ‏**baseline** (ריצה בודדת של config; בדרך כלל לא נחוץ)

לבדיקה חינמית (dry run) של config, למשל של smoke בשכבה 2:

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_sec_smoke.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | לא מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק |

ריצה בתשלום של אותו config (רק אם ה-orchestrator ביקש):

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_sec_smoke.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | לא מסומן |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | מסומן |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | ריק |

ה-workflows ‏**results log** ו-**tests** רצים לבד; אין להם טופס.

התוצאות: ב-branch ‏`results-log` (תיקייה לכל ריצה), בעמוד הריצה תחת **Artifacts**, ובסיכום (Summary) של כל job.

---

## English

After this page there is nothing else for you to do: every run, check and report runs in GitHub Actions, with no file edited and no local command line. The orchestrator (an AI that can start workflows and read their logs and branches through the GitHub API, but **never sees a secret value** and cannot download artifacts) starts the stages and reads the results from the branch `results-log`.

Three things, in order:

1. Create seven repository secrets (step 1).
2. Set a monthly spending limit at each provider (step 2).
3. Tell the orchestrator you are done (step 3).

**Never** paste a key into a chat, an issue, a pull request or a file. The only place for a key is GitHub's Secret box.

### Step 1 — create the secrets

Menu path, once per secret: GitHub → repository `giladscore494/binding-gate` → **Settings → Secrets and variables → Actions → New repository secret**. In **Name** type the name exactly as in the table (capitals, underscores), paste the value into **Secret**, press **Add secret**.

| Secret name (Name) | What to paste (Secret) | Where to get it (provider menu paths **unverified**: not read on the official pages) |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI API key | platform.openai.com → API keys → Create new secret key |
| `GEMINI_API_KEY` | Google Gemini API key | aistudio.google.com → Get API key (on the billing account you fund) |
| `ZAI_API_KEY` | Z.ai (GLM) API key | z.ai API platform → API Keys |
| `MOONSHOT_API_KEY` | Moonshot / Kimi API key | platform.moonshot.ai → API Keys |
| `SERPER_API_KEY` | Serper API key (free tier) | sign up at serper.dev (2,500 free searches on signup, no card), then copy the API key from the serper.dev dashboard. **Do not enter a payment method.** (PR 27: the old `SERPAPI_API_KEY` secret is no longer used; you may delete it.) |
| `RUNS_ENCRYPTION_PASSPHRASE` | a long passphrase **you make up** (for example six random words) | keep it yourself (a password manager). The live-web (layer 3) runs are encrypted with it; without it they cannot be decrypted later. |
| `SEC_CONTACT_EMAIL` | **your** e-mail address | needed **only for layer 2** (SEC filings): SEC's fair-access rules require every automated client to declare a contact e-mail. It is kept as a secret so that it never appears in the public repository, in artifacts or in logs. |

`ANTHROPIC_API_KEY` and `DEEPSEEK_API_KEY` are **not** needed: do not add them. A missing secret breaks nothing: its model is skipped with a warning (without `SEC_CONTACT_EMAIL` only layer 2 does not run).

**One optional setting:** Settings → Actions → General → at the bottom, **Allow GitHub Actions to create and approve pull requests** — check it. The frozen test-run workflow then opens, by itself, the pull request that brings the access log (`audit/test_access_log.jsonl`) back to main. If it stays unchecked, the branch is still pushed and the orchestrator opens the pull request.

### Step 2 — a monthly spending limit at each provider (outer safety net)

The study's total budget is **USD 50, hard**. Inside GitHub it is enforced by every run's caps and by **the ledger** (which computes the actual spend of every earlier run, including the snapshots kept on the branch `results-log`, and refuses any run that could exceed 50). Provider limits only catch what the code cannot (a bug, a wrong price). Suggested amounts (from docs/RUNNING.md; 42 USD together):

| Provider | Suggested limit | Where (paths **unverified**) |
|---|---|---|
| OpenAI | 9 USD | platform.openai.com → Settings → Organization → Limits (monthly budget) |
| Google Gemini | 14 USD | Google Cloud console → Billing → Budgets & alerts (a budget only alerts, it does not stop; for a hard stop: a prepaid balance, or disable billing after the runs) |
| Z.ai | 12 USD | z.ai API platform: prepaid balance; top up only this amount |
| Moonshot | 7 USD | platform.moonshot.ai: prepaid balance; top up only this amount |
| Serper | 0 USD | stay on the free tier (2,500 searches); never enter a payment method |
| SEC EDGAR | 0 USD | free; no account, no key |

### Step 3 — tell the orchestrator

Write, for example: "I added the seven secrets and set the spending limits". From there it starts `check-secrets` (free), then the stages in order, and reads the results from the branch `results-log` (folder `runs/<run number>-<stage>/` for each run). If something fails because of a key or a provider setting, it will tell you exactly what to fix.

### If you prefer to click yourself

GitHub → repository → **Actions** tab → the workflow's name in the left column → **Run workflow** (right side) → fill in exactly as in the table → the green **Run workflow** button. Do not start two paid runs at once: the ledger refuses a paid run while another run is still active.

#### Workflow **dev pipeline** (the DEV phase)

Order: `check-secrets`, `smoke`, `dev-baseline`, `dev-rest`, `sec-dataset-prepare` (then the orchestrator pins the hash in a pull request), `layer2-dev`, `layer3-dev` (or `all-dev`, which runs every paid stage in sequence). After the layer-1 STOP: `validate-layer1` (amendment 1); `dev-rest` and `all-dev` are refused.

**check-secrets** (free: which secrets exist (names only), the free preflights, and the budget left)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `check-secrets` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | unchecked |

**smoke** (cap 0.80 USD; fails on purpose if the measured token usage would take a planned run over its cap)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `smoke` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | checked |

**dev-baseline** (cap 4.80 USD; ends with the stop-rule decision CONTINUE / STOP / IN-BETWEEN / MEASUREMENT-CHECK)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `dev-baseline` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | checked |

**dev-rest** (caps 10.20 USD together; refused unless the newest dev-baseline decision is CONTINUE)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `dev-rest` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | checked |

**sec-dataset-prepare** (free: generates the layer-2 dataset from SEC EDGAR and prints content_sha256; needs `SEC_CONTACT_EMAIL`)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `sec-dataset-prepare` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | unchecked |

**layer2-dev** (caps 2.60 USD together; layer 2 (SEC filings): smoke, usage check, baseline, the layer-2 stop rule (PR 22), then gated and binding-scope candidates only after CONTINUE; refused while the dataset is UNPINNED)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `layer2-dev` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | checked |

**layer3-dev** (caps 1.85 USD; live web, searches on Serper's free tier; much shorter than the earlier 5 hours, since searches are no longer spaced 72 s apart)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `layer3-dev` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | checked |

**validate-layer1** (caps 23.65 USD together; amendment 1 of 2026-11-22, exploratory: checks the layer-1 STOP decision. Runs only if the newest dev-baseline decision is a final STOP; never needs CONTINUE. LLM judge on 144 dev items per model of the baseline answers, free-form (free_form) answers to the same items, the judge on those, and a comparison report)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `validate-layer1` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | checked |

**all-dev** (everything in sequence: smoke → dev-baseline → stops unless CONTINUE → dev-rest → layer2-dev → layer3-dev; caps 19.80 USD together)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `all-dev` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets and sec-dataset-prepare) | checked |

#### Workflow **frozen test run** (only after the freeze)

One run per layer, from the tag the orchestrator creates at the freeze. Each run checks the tag, the ledger and the test lock, runs, and opens a pull request with the access log.

**layer1** (layer 1, the confirmatory run (H1-H3); caps 16.75 USD)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Layer whose frozen test config runs (PREREGISTRATION.md section 12) | `layer1` |
| Freeze tag prereg-freeze-<name>; the run uses exactly that commit | the tag the orchestrator names, e.g. `prereg-freeze-1` |
| I confirm that paid API calls and one logged test-split access may be made | checked |

**layer1_midtier** (layer 1, the mid-tier check (exploratory); caps 6.95 USD)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Layer whose frozen test config runs (PREREGISTRATION.md section 12) | `layer1_midtier` |
| Freeze tag prereg-freeze-<name>; the run uses exactly that commit | the tag the orchestrator names, e.g. `prereg-freeze-1` |
| I confirm that paid API calls and one logged test-split access may be made | checked |

**layer2_sec** (layer 2, SEC filings (H1 and H2); caps 2.75 USD)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Layer whose frozen test config runs (PREREGISTRATION.md section 12) | `layer2_sec` |
| Freeze tag prereg-freeze-<name>; the run uses exactly that commit | the tag the orchestrator names, e.g. `prereg-freeze-1` |
| I confirm that paid API calls and one logged test-split access may be made | checked |

**layer3_live** (layer 3, live web (H4); caps 2.40 USD; much shorter than the earlier 5.5 hours, since searches are no longer spaced 72 s apart)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Layer whose frozen test config runs (PREREGISTRATION.md section 12) | `layer3_live` |
| Freeze tag prereg-freeze-<name>; the run uses exactly that commit | the tag the orchestrator names, e.g. `prereg-freeze-1` |
| I confirm that paid API calls and one logged test-split access may be made | checked |

#### Workflow **baseline** (a single config; normally not needed)

A free check (dry run) of one config, e.g. the layer-2 smoke:

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_sec_smoke.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | checked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | unchecked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty |

A paid run of the same config (only if the orchestrator asked for it):

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Run config (path in the repository) | `configs/dev_sec_smoke.toml` |
| Model label from the config, or "all" (one parallel job per model) | `all` |
| Dry run: count calls and estimate cost; no model API is called | unchecked |
| I confirm that paid API calls may be made (needed when dry run is unchecked) | checked |
| Seed runs: id of the earlier workflow run that holds them (empty unless the config names seed runs) | empty |

The workflows **results log** and **tests** start by themselves; they have no form.

Results: on the branch `results-log` (one folder per run), on the run's page under **Artifacts**, and in each job's Summary.
