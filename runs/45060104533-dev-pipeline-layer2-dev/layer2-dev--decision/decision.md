## Layer-2 stop rule (section 10, layer 2): **STOP**

workflow run 45060104533; rate of a model = max(R2E, R2P); continue at >= 10%, stop below 5% (PROPOSED, as section 10)

| model | decision | R2E (wrong entity, peer shown) | R2P (wrong period or scale, all) | records | other_wrong + unparseable |
|---|---|---:|---:|---:|---:|
| google-low | STOP | 0.0% | 0.0% | 378 | 0.8% |
| openai-low | STOP | 0.0% | 0.0% | 378 | 0.5% |
| zai-low | STOP | 0.0% | 0.0% | 378 | 0.8% |
