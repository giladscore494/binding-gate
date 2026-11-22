# הגדרה חד-פעמית לבעלים / One-time owner setup

English version: [below](#english).

## הגדרה חד-פעמית: מה אתה עושה, פעם אחת

אחרי הדף הזה אין לך שום דבר נוסף לעשות בשלב ה-DEV: כל הריצות, הבדיקות והדוחות רצים ב-GitHub Actions מה-workflow **dev pipeline** (`.github/workflows/dev_pipeline.yml`), בלי לערוך אף קובץ. ה-orchestrator (בינה מלאכותית שיכולה להפעיל workflows ולקרוא את התוצאות שלהם דרך ה-API של GitHub, אבל **לעולם לא רואה ערך של secret**) מפעיל את השלבים וקורא את התוצאות.

שלושה דברים, לפי הסדר:

1. ליצור שישה repository secrets (שלב 1).
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
| `SERPAPI_API_KEY` | מפתח של SerpApi (תוכנית **Free**) | להירשם ב-serpapi.com לתוכנית החינמית (250 חיפושים בחודש), ואז Dashboard ← Your Private API Key. **לא להזין אמצעי תשלום.** |
| `RUNS_ENCRYPTION_PASSPHRASE` | סיסמה ארוכה **שאתה ממציא** (למשל 6 מילים אקראיות) | לשמור אותה אצלך (מנהל סיסמאות). בה מוצפנות ריצות ה-web החי (שכבה 3); בלעדיה אי אפשר לפענח אותן אחר כך. |

`ANTHROPIC_API_KEY` ו-`DEEPSEEK_API_KEY` **לא** נדרשים: לא להוסיף. secret שחסר לא שובר כלום: המודל שלו מדולג עם אזהרה.

### שלב 2 — מגבלת הוצאה חודשית אצל כל ספק (רשת ביטחון חיצונית)

התקציב הכולל של המחקר הוא **50 דולר, קשיח**. בתוך GitHub שומרים עליו התקרות של כל ריצה ו-**ה-ledger** (שמחשב את ההוצאה בפועל מכל הריצות הקודמות ומסרב לכל ריצה שעלולה לעבור את 50). המגבלות אצל הספקים תופסות רק מה שהקוד לא יכול לתפוס (באג, מחיר שגוי). סכומים מוצעים (מ-docs/RUNNING.md; ביחד 42 דולר):

| ספק | מגבלה מוצעת | איפה (הנתיבים **לא אומתו**) |
|---|---|---|
| OpenAI | 9 דולר | platform.openai.com ← Settings ← Organization ← Limits (תקציב חודשי) |
| Google Gemini | 14 דולר | Google Cloud console ← Billing ← Budgets & alerts (תקציב רק שולח התראה ולא עוצר; לעצירה קשיחה: יתרה משולמת מראש, או לכבות billing אחרי הריצות) |
| Z.ai | 12 דולר | פלטפורמת ה-API של z.ai: יתרה משולמת מראש; להטעין רק את הסכום הזה |
| Moonshot | 7 דולר | platform.moonshot.ai: יתרה משולמת מראש; להטעין רק את הסכום הזה |
| SerpApi | 0 דולר | להישאר בתוכנית Free; לא להזין אמצעי תשלום |

### שלב 3 — להגיד ל-orchestrator

לכתוב לו, למשל: "הוספתי את ששת ה-secrets והגדרתי מגבלות הוצאה". מכאן הוא מפעיל בעצמו את `check-secrets` (חינם), ואחר כך את השלבים בתשלום לפי הסדר, וקורא את התוצאות מה-artifacts. אם משהו נכשל בגלל מפתח או הגדרה אצל ספק, הוא יגיד לך בדיוק מה לתקן.

### אם אתה מעדיף ללחוץ בעצמך

GitHub ← הריפו ← לשונית **Actions** ← בעמודה השמאלית **dev pipeline** ← כפתור **Run workflow** (מימין) ← למלא בדיוק לפי הטבלה של השלב ← הכפתור הירוק **Run workflow**. הסדר: `check-secrets`, `smoke`, `dev-baseline`, `dev-rest`, `layer3-dev` (או `all-dev`, שמריץ את כולם ברצף). לא להפעיל שתי ריצות במקביל: ה-ledger מסרב לריצה בתשלום כשריצה אחרת עדיין פעילה.

**check-secrets** (חינם: אילו secrets קיימים — רק שמות —, בדיקות preflight חינמיות, והתקציב שנשאר)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `check-secrets` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets) | לא מסומן |

**smoke** (תקרה 0.80 דולר; נכשל בכוונה אם צריכת הטוקנים שנמדדה הייתה מעבירה ריצה מתוכננת את התקרה שלה)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `smoke` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets) | מסומן |

**dev-baseline** (תקרה 4.80 דולר; בסוף מוצגת החלטת כלל העצירה: CONTINUE / STOP / IN-BETWEEN / MEASUREMENT-CHECK)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `dev-baseline` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets) | מסומן |

**dev-rest** (תקרות 10.30 דולר ביחד; מסרב אם ההחלטה האחרונה של dev-baseline אינה CONTINUE)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `dev-rest` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets) | מסומן |

**layer3-dev** (תקרות 1.40 דולר; web חי, חיפושים בתוכנית החינמית של SerpApi; עד כ-5 שעות)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `layer3-dev` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets) | מסומן |

**all-dev** (הכול ברצף: smoke ← dev-baseline ← עוצר אם לא CONTINUE ← dev-rest ← layer3-dev; תקרות 17.30 דולר ביחד)

| שדה | ערך |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `all-dev` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets) | מסומן |

התוצאות: בעמוד הריצה, תחת **Artifacts** ובסיכום (Summary) של כל job.

---

## English

After this page there is nothing else for you to do in the DEV phase: every run, check and report runs in GitHub Actions from the workflow **dev pipeline** (`.github/workflows/dev_pipeline.yml`), with no file edited. The orchestrator (an AI that can start workflows and read their results through the GitHub API, but **never sees a secret value**) starts the stages and reads the results.

Three things, in order:

1. Create six repository secrets (step 1).
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
| `SERPAPI_API_KEY` | SerpApi key (**Free** plan) | sign up at serpapi.com for the free plan (250 searches per month), then Dashboard → Your Private API Key. **Do not enter a payment method.** |
| `RUNS_ENCRYPTION_PASSPHRASE` | a long passphrase **you make up** (for example six random words) | keep it yourself (a password manager). The live-web (layer 3) runs are encrypted with it; without it they cannot be decrypted later. |

`ANTHROPIC_API_KEY` and `DEEPSEEK_API_KEY` are **not** needed: do not add them. A missing secret breaks nothing: its model is skipped with a warning.

### Step 2 — a monthly spending limit at each provider (outer safety net)

The study's total budget is **USD 50, hard**. Inside GitHub it is enforced by every run's caps and by **the ledger** (which computes the actual spend of every earlier run and refuses any run that could exceed 50). Provider limits only catch what the code cannot (a bug, a wrong price). Suggested amounts (from docs/RUNNING.md; 42 USD together):

| Provider | Suggested limit | Where (paths **unverified**) |
|---|---|---|
| OpenAI | 9 USD | platform.openai.com → Settings → Organization → Limits (monthly budget) |
| Google Gemini | 14 USD | Google Cloud console → Billing → Budgets & alerts (a budget only alerts, it does not stop; for a hard stop: a prepaid balance, or disable billing after the runs) |
| Z.ai | 12 USD | z.ai API platform: prepaid balance; top up only this amount |
| Moonshot | 7 USD | platform.moonshot.ai: prepaid balance; top up only this amount |
| SerpApi | 0 USD | stay on the Free plan; never enter a payment method |

### Step 3 — tell the orchestrator

Write, for example: "I added the six secrets and set the spending limits". From there it starts `check-secrets` (free), then the paid stages in order, and reads the results from the artifacts. If something fails because of a key or a provider setting, it will tell you exactly what to fix.

### If you prefer to click yourself

GitHub → repository → **Actions** tab → **dev pipeline** in the left column → **Run workflow** (right side) → fill in exactly as in the stage's table → the green **Run workflow** button. Order: `check-secrets`, `smoke`, `dev-baseline`, `dev-rest`, `layer3-dev` (or `all-dev`, which runs them all in sequence). Do not start two runs at once: the ledger refuses a paid run while another run is still active.

**check-secrets** (free: which secrets exist — names only —, the free preflights, and the budget left)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `check-secrets` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets) | unchecked |

**smoke** (cap 0.80 USD; fails on purpose if the measured token usage would take a planned run over its cap)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `smoke` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets) | checked |

**dev-baseline** (cap 4.80 USD; ends with the stop-rule decision CONTINUE / STOP / IN-BETWEEN / MEASUREMENT-CHECK)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `dev-baseline` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets) | checked |

**dev-rest** (caps 10.30 USD together; refused unless the newest dev-baseline decision is CONTINUE)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `dev-rest` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets) | checked |

**layer3-dev** (caps 1.40 USD; live web, searches on SerpApi's free plan; up to about 5 hours)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `layer3-dev` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets) | checked |

**all-dev** (everything in sequence: smoke → dev-baseline → stops unless CONTINUE → dev-rest → layer3-dev; caps 17.30 USD together)

| Field | Value |
|---|---|
| Use workflow from (Branch) | `main` |
| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `all-dev` |
| I confirm that paid API calls may be made (needed for every stage except check-secrets) | checked |

Results: on the run's page under **Artifacts**, and in each job's Summary.
