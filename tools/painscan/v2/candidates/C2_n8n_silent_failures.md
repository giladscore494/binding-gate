# C2 n8n_silent_failures (incl. false completion) — KILLED at S1k (2026-11-23)

**Card (S0).** Segment: agencies/freelancers building n8n/Make/Zapier automations and AI agents for clients.
Failure: silent drops (leads lost for hours), agents claiming "refunded/booked/sent" while the tool node failed.
Fix: deterministic monitor (heartbeats, expected volume, claim-vs-tool-status check), ≥ $30/month per workflow. Round-0 evidence: 33 first-hand rows, 2 with a loss.

**Kill memo (S1k).**
* Rule 1 HOLDS: built-ins (Error Trigger, heartbeats, volume baselines) and free community monitors; for false completion, free fixes (read-back, proof IDs, fail on non-200, daily reconciliation) — and in the n8n thread on agents claiming actions that did not happen, nobody reports paying for a solution.
* Rule 2 HOLDS: NodeHero (free 5 workflows, $9–29/month), FlowSentinel (free 5 flows), OpsVeritas (free beta), Pulse, FlowGuard, Langfuse ($0–199/month) — most launched in the last ~6 months.
* Price floor: builders anchored at $0–29 per account; agencies absorb failures.

**Verdict: KILL.** "False completion" as a standalone business is killed with it (free workarounds; no payment signal).
Sources: https://community.n8n.io/t/those-of-you-managing-n8n-workflows-for-clients-how-do-you-find-out-when-something-breaks/304198 · https://community.n8n.io/t/has-your-agent-ever-claimed-it-did-something-that-didnt-actually-happen/316129 · https://trustmrr.com/startup/nodehero?metric=mrr · https://langfuse.com/pricing.md
