# painscan v2 rounds

One file per round, committed BEFORE dispatch (its commit time is the round's pre-registration).
Format (`round_NN.toml`):

```toml
round = 1
purpose = "why this round, which sub-question it targets"

[[query]]
candidate = "false_completion"   # candidate id (tools/painscan/candidates/<id>.md)
question = "money"               # who | money | pay | existing_fail | find
source = "reddit"                # reddit | hn | forums | web | site:<domain>
tbs = "qdr:y"                    # "" (none) or qdr:<h|d|w|m|y>[n]
text = "AI agent said refund done but it failed"
```

At most 60 queries per round. Labelling, kill memos and decisions are done by chat sub-agents
(no LLM API); results come back through the run log (gzip+base64).
