# binding-gate — final report (study closed 2026-11-23)

## סיכום בעברית

המחקר נסגר בתוצאה שלילית. ההשערה הייתה שסוכן שעונה מתוך מסמכים או מהרשת מצטט ראיה של **הישות
הלא נכונה**, ושער דטרמיניסטי יתפוס את זה בלי מודל נוסף. בשלוש שכבות, על מודלים זולים מהדור הנוכחי:

* **שכבה 1 (מסמכים רפואיים מוגשים):** שיעור הכשל 1.6%–2.4%, מתחת לסף 5%: עצירה לפי כלל שנקבע מראש.
  שופט LLM מצא אותו מספר טעויות או פחות, ותשובה חופשית לא חשפה כשל נסתר.
* **שכבה 2 (דוחות SEC):** 0% אצל כל המודלים: עצירה.
* **שכבה 3 (חיפוש חי, SealQA):** בלי השער כ-54%–56% מהתשובות שגויות, אבל רובן לא שגיאות שיוך ישות
  אלא תשובה לא נכונה. השער הוריד את השגויות ל-2%–3.5%, כי נמנע ב-95% מהשאלות, והתשובות הנכונות
  ירדו מ-12%–14% ל-0%–1%. ההשערה H4 (תועלת נטו ≥ 0) לא נתמכה אצל אף מודל.

הלקח המרכזי: הכשל שהשער נבנה לתקן כמעט לא קורה במודלים הנוכחיים כשהראיות מוגשות מסודרות, ובמקום
שבו יש הרבה טעויות, השער עוצר תשובות במקום לתקן אותן. הוצאו $12.65 מתוך $50. חלק ה-test לא נפתח מעולם.

## 1. What was tested

**Hypothesis.** Agents that answer from retrieved documents or the web often cite evidence about the
**wrong entity** (a competitor, another drug, another period), and a deterministic gate (verbatim
quote, entity and qualifiers in the quote window, no closer competitor, premise present) catches it
without another model. Outcomes: ACCEPT / RESEARCH / ABSTAIN.

**Method.** Pre-registration (PREREGISTRATION.md), frozen contracts, a locked test split with an
access log, hash-pinned datasets, an enforced USD 50 ledger, and stop rules fixed before each
baseline: below 5% wrong-entity rate STOP, at or above 10% continue.

**Models** (lowest reasoning setting): `gpt-6-luna`, `gemini-3.1-flash-lite`, `glm-5.3-flash`;
judge `kimi-k3.1-pro` (no temperature sent).

## 2. Results (dev split only)

| Layer | Data | Measure | Result | Decision | Run |
|---|---|---|---|---|---|
| 1 | Deceptive Grounding, medical documents, 1,455 records per model | wrong-entity rate R | 2.4% / 2.0% / 1.6% (google / openai / zai) | **STOP** | 45053502161 |
| 1-V1 | 144-item subsample per model | Kimi judge vs deterministic label | judge found the same number of failures or fewer (agreement 97%–99%) | metric does not undercount | 45062860998 |
| 1-V2 | same items, free-form answers | judge-scored failure rate | 1.4%–4.2% (GLM +3.5 points) | structure does not hide the failure | 45062860998 |
| 2 | SEC filings, 378 records per model | max(wrong entity, wrong period/scale) | 0.0% for all models; other wrong + unparseable 0.5%–0.8% | **STOP** | 45060104533 |
| 3 | SealQA, live web (Serper), 86 dev items per model | no gate vs gated + one re-search | see below | **H4 not supported** | 45075784298 |

**Layer 3 detail** (86 items per model, one repeat; counts from `layer3_summary.json`):

| model | arm | correct | wrong | abstained |
|---|---|---:|---:|---:|
| gemini-3.1-flash-lite | no gate | 12 (14.0%) | 48 (55.8%) | 26 |
| gemini-3.1-flash-lite | gated research | 0 (0.0%) | 3 (3.5%) | 82 |
| gpt-6-luna | no gate | 10 (11.6%) | 46 (53.5%) | 29 |
| gpt-6-luna | gated research | 1 (1.2%) | 2 (2.3%) | 82 |

The SealQA scorer maps every incorrect answer to the `wrong_entity` outcome, so the 54%–56% are
wrong answers in general, mostly "not found / outdated", not entity-binding failures (lesson 4
below). The gate removed wrong answers by abstaining on 95% of questions and removed nearly every
correct answer with them: net value negative for both models.

**Spend:** USD 12.65 of 50 (ledger after run 45075784298). **Test split:** never unlocked; no
test item was shown to a model (audit/test_access_log.jsonl).

## 3. Conclusion

Negative, and clean. With evidence served in order, current cheap models almost never cite the
wrong entity (layers 1–2, below the pre-registered 5%). Where errors are frequent (layer 3), they
are not the failure the gate targets, and the gate converts them into abstentions at the cost of
the correct answers. The original paper's 8%–87% failure rates were measured on older models and do
not carry over to the 2026 generation. No metric was swapped to rescue the result.

## 4. Lessons (rules for the next study)

1. **Measure before building.** 26 PRs were built before a USD 1–2 baseline. Next time: a baseline
   of at most USD 5 and one working day comes first.
2. **A source speaks only for the models it measured.** A paper on last year's models is not
   evidence that the failure exists today.
3. **Stop rules are fixed before the data and not changed after.** A clean negative is a result.
4. **The metric must measure the failure the mechanism fixes,** not a nearby one (SealQA failures
   are mostly "not found", not "wrong entity").
5. **Check API parameters before a paid run** (Kimi temperature, SEC User-Agent, model ids).
6. **Errors must be visible in logs;** a judge that fails silently looks like a result.
7. **Report correct answers next to wrong answers.** A gate that abstains looks excellent on error
   rate alone; the net effect (H4) is what matters.

## 5. What stays in the repository

The `binding_gate` package, configs, workflows and tests stay in place, **frozen and unmaintained**,
so every number above can be re-derived from the recorded runs (`results-log` branch). They were not
moved to a legacy folder because the workflows, tests and recorded manifests reference their paths;
moving them would break reproducibility without helping the next study. The next study,
`tools/painscan` (PR 28), is independent of this code.
