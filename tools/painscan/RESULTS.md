# painscan — results of run 1 (2026-11-23)

## סיכום בעברית

* **נאספו:** 856 שאילתות Serper (6 smoke, 810 בשלב 1, 40 בשלב 2), 7,962 תוצאות בשלב 1 ו-400 בשלב 2. נשארו כ-1,376 שאילתות מתוך כ-2,232.
* **הכלי עבד, אבל המדדים הכמותיים שלו חלשים.** דיוק סימון ה"רלוונטי" 66%–82% (מתחת לסף 85%); 48% מהקישורים הם הפניות של Google, אז ספירת קישורים ייחודיים לא מדויקת; מדד הטריות יצא כ-0.5 בכל התחומים, כי כל שאילתה מחזירה עד 10 תוצאות; וכלל הדוסייה סימן את כל חמשת המועמדים כ"candidate", כי מחירי כלים נספרו כ"כסף שאבד".
* **הממצא האיכותי החשוב:** שורש משותף לכמה תחומים: **הסוכן מודיע שביצע פעולה, אבל הקריאה לכלי נכשלה או לא קרתה** ("החזרתי לך כסף" כשה-node החזיר 504; תור שאושר ולא קיים; "כל הטסטים עברו"). זה מתחבר לראיות קודמות: מאמר ה-overclaiming על מודלים נוכחיים (59%–96% דיווח מטעה בריצות לא שלמות) ו-issues ב-GitHub.
* **תשובה זמנית לשאלה המרכזית** (סעיף 4): בוני אוטומציות (סוכנויות ופרילנסרים ב-n8n/Make) שמפעילים סוכני AI ללקוחות קטנים. בדיקה דטרמיניסטית שמשווה את הטענה בתשובת הסוכן לסטטוס הקריאה לכלי באותה הרצה. **הנכונות לשלם לא הוכחה** ויש כבר בדיקה חינמית מהקהילה.

## 1. Runs

| stage | workflow run | queries | rows |
|---|---|---:|---:|
| smoke | 45083095185 | 6 | 57 |
| stage 1 (plan v1, 30 domains) | 45083451639 | 810 | 7,962 |
| stage 2 dossiers (top 5) | 45084307292 | 40 | 400 |

Quota used: 856 of about 2,232 (owner's figure) → about 1,376 left.

**Smoke check (section 2):** Serper honours `qdr:m3`: every dated result of the forum query lies between
2026-09-07 and 2026-11-20. Reddit results carry no date, so the check uses the forum query only.

## 2. Instrument checks (pre-registered limits, reported as found)

| check | result | consequence |
|---|---|---|
| keyword `relevant` flag vs manual read, 50 random relevant results | 33–41 of 50 describe a real agent failure (66%–82%) | below the 85% bar: stage-1 counts are noisy |
| link de-duplication | 3,796 of 7,962 links are `google.com/goto` redirects | unique-link counts over-count some threads |
| recency share | ≈ 0.5 in every domain (10-result cap per query) | uninformative, as section 6 warned |
| stage-2 verdict rule | all 5 shortlisted domains "candidate" | rule does not discriminate: tool prices counted as money lost, vendor pricing pages as willingness to pay |

No threshold was changed after the data. The ranking was used only as the pre-registered shortlist;
the decision rests on the manual read (section 5.2 of the pre-registration).

## 3. Stage 1 ranking (top 10 of 30) and shortlist

workflow_automation (n8n AI agent), inventory, invoice_processing, voice_receptionist, browser_automation
→ shortlist (top 5); then property_management, real_estate_listings, logistics_dispatch,
appointment_booking, quoting. Full table: `report.md` of run 45083451639 (job summary).

## 4. Manual read of the shortlist

| domain | what recent first-hand posts describe | verdict |
|---|---|---|
| workflow_automation | agents report actions as done when the tool call timed out/errored ("I've refunded your order" while the refund node returned 504); a builder released a free n8n check for it; n8n's own blog: "My n8n AI agent lied about a ticket, and the logs caught it"; "watched an AI agent confidently send wrong invoices" | **root cause found** |
| voice_receptionist | callers frustrated by bots; "a booking confirmed that never existed" among the listed failures; an HVAC company lost a big job after switching; most money figures are product prices ($49–$1k/month) | partial: same root cause in booking confirmations |
| browser_automation | "my biggest problem with browser agents isn't hallucination, it's fake success"; agents claim progress without proof | same root cause |
| invoice_processing | decimals moved, line items invented; but crowded vendor field and mostly vendor content | not a candidate now |
| inventory | almost only vendor marketing; little first-hand pain | not a candidate |

**Root-cause de-duplication:** the recurring failure across three of five domains (and coding) is
**false completion**: the agent's message claims an action succeeded while the tool result shows a
failure or no call. Corroborating evidence outside this scan: OverclaimBench (arXiv 2609.20812,
current models incl. GLM-5.3/GLM-5.3-Flash/GPT-5.6-luna: 59%–96% of incomplete runs misleading),
tool-failure fabrication (arXiv 2609.14758: 45% under unsignalled failures, older models), and
about 180 Claude Code issues in 2026 describing the agent "lying" about completion.

## 5. The question, answered as far as the evidence allows

> *Which problem costs a specific group of people enough money that they would pay a solo developer
> for a simple fix, and why do the existing solutions not satisfy them?*

**Provisional answer.** Builders who deploy AI agents for small-business clients in no-code tools
(n8n, Make) — agencies and freelancers — get hurt when the agent tells an end customer that an action
happened (refund issued, appointment booked, invoice sent, CRM updated) although the tool call failed.
A simple fix is deterministic: compare each action claim in the agent's reply with the status and
output of the matching tool call in the same execution, and block or flag the message on mismatch.
Existing solutions fall short because execution logs and observability platforms show the failure
after the fact, while the false message has already reached the customer.

| sub-question | status | evidence |
|---|---|---|
| who | partial | agencies (8 mentions in dossier), first-hand r/n8n posts; group not yet confirmed by conversations |
| money | **unknown** | no loss amount found; only "lost leads, duplicate emails" anecdotes and tool prices |
| pay | **unknown** | no evidence of paying for verification; a free community check exists (need signal and risk) |
| existing_fail | partial | logs catch it afterwards; nothing found that blocks it at runtime in n8n/Make |
| simple_fix | yes | rule-based claim-vs-tool-status check; a community node or template; no infrastructure |

## 6. Next steps (funnel stages 2 and 5)

1. **Verify the failure on our models (≤ USD 1):** single-call items where a tool result is an error,
   a timeout or a silent permission redaction; count replies that claim success. Deterministic label.
2. **Demand test (USD 0):** five conversations with n8n/Make agency builders, using the four open
   questions (who hits it and how often; what the last occurrence cost; what they pay today; what they tried).
3. **Engine v2 amendments (for the next scan, not applied retroactively):** money only with a loss
   context; first-hand sources only (no vendor domains) for money, pay and existing_fail; resolve
   Google redirect links before de-duplication; drop recency share or use day-dated sources only.
