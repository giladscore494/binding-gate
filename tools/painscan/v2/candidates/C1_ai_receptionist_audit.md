# C1 ai_receptionist_audit — KILLED at S1k (2026-11-23)

**Card (S0).** Segment: small service businesses (HVAC, plumbing, restaurants) using AI phone receptionists.
Failure: mishandled calls (bookings not in calendar, no human transfer, callers hang up) lose jobs.
Fix: audit call logs vs calendar/CRM, flag lost calls with $ value, ≥ $30/month. Round-0 evidence: 26 first-hand rows, 5 with a loss (e.g. an HVAC company lost a big job after switching).

**Kill memo (S1k, sub-agent, WebSearch).**
* Rule 1 (free/bundled fix) HOLDS partly: vendors ship call logs, transcripts, outcomes (Smith.ai via CallRail review; CallRail Conversation Intelligence tags "appointment scheduled").
* Rule 2 (shipped by others) HOLDS for the voice-agent builder segment: Cekura ($0.05/monitored call, $30/seat), Coval ($100–4,500/month), Hamming; ServiceTitan and Jobber pull HVAC owners into platform-native AI receptionists.
* Rule 3 regulated: does not hold for HVAC/restaurants (clinics excluded: HIPAA).
* Rule 4 channels: unclear (Jobber community thread is the only confirmed on-topic venue).
* Strongest reason to fail: owners who get burned turn the AI off or go back to a human — churn, not a purchase.

**Verdict: KILL** (rules 1 and 2 hold). Sources: https://www.callrail.com/blog/smith-ai-review · https://www.coval.ai/blog/hamming-vs-cekura · https://community.getjobber.com/discussions/operations-forum/ai-receptionist-vs-outsourced-vs-owners/7842 · https://www.cloudtalk.io/blog/goodcall-pricing/
