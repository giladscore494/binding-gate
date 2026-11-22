## Layer-1 STOP validation (EXPLORATORY, post hoc; decides nothing)

- PREREGISTRATION.md section 18, amendment 1 (2026-11-22); decided after the layer-1 dev baseline (no_gate, deterministic provenance scoring) gave STOP under section 10 (workflow run 45053502161).
- Subsample: 12 items per condition, seed 20261122, excluded absent/null_control, partial/null_control, complete/null_control (configs/rescore/validation_judge_structured.toml).
- Judge: kimi-k3.1-pro (benchmark judge prompt); intervals: cluster bootstrap by group_id, seed 20261122, 10000 resamples, level 19/20.

### Judge rates (all subsample records; 95% interval)

| model | arm | records | EAF or substitution | EAF | substitution | confabulation | judge unparseable / error | unscored |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| google-low | structured | 144 | 0.0% [0.0, 0.0] | 0.0% | 0.0% | 0.0% | 0 / 97 | 0 |
| google-low | free_form | 144 | 0.0% [0.0, 0.0] | 0.0% | 0.0% | 0.0% | 0 / 144 | 0 |
| openai-low | structured | 144 | 0.0% [0.0, 0.0] | 0.0% | 0.0% | 0.0% | 0 / 97 | 0 |
| openai-low | free_form | 144 | 0.0% [0.0, 0.0] | 0.0% | 0.0% | 0.0% | 0 / 143 | 0 |
| zai-low | structured | 144 | 0.0% [0.0, 0.0] | 0.0% | 0.0% | 0.0% | 0 / 96 | 0 |
| zai-low | free_form | 144 | 0.0% [0.0, 0.0] | 0.0% | 0.0% | 0.0% | 0 / 144 | 0 |

### V1: agreement with the deterministic label (structured answers)

| model | deterministic wrong-entity | judge EAF or substitution | raw agreement | kappa | kappa (parsed only) | only ours | only judge |
|---|---:|---:|---:|---:|---:|---:|---:|
| google-low | 1.4% | 0.0% | 0.0% | 0.000 | n/a | 2 | 0 |
| openai-low | 1.4% | 0.0% | 0.0% | 0.000 | n/a | 2 | 0 |
| zai-low | 0.7% | 0.0% | 0.0% | 0.000 | n/a | 1 | 0 |

### V2: free-form minus structured, paired by item (95% interval)

| model | paired items | structured | free form | difference | difference (both determinate) | only structured | only free form |
|---|---:|---:|---:|---:|---:|---:|---:|
| google-low | 144 | 0.0% | 0.0% | 0.0% [0.0, 0.0] | n/a n/a | 0 | 0 |
| openai-low | 144 | 0.0% | 0.0% | 0.0% [0.0, 0.0] | n/a n/a | 0 | 0 |
| zai-low | 144 | 0.0% | 0.0% | 0.0% [0.0, 0.0] | n/a n/a | 0 | 0 |

- NOTE: google-low / structured: 0 unparseable judge output(s) and 97 failed judge call(s)
- NOTE: google-low / free_form: 0 unparseable judge output(s) and 144 failed judge call(s)
- NOTE: openai-low / structured: 0 unparseable judge output(s) and 97 failed judge call(s)
- NOTE: openai-low / free_form: 0 unparseable judge output(s) and 143 failed judge call(s)
- NOTE: zai-low / structured: 0 unparseable judge output(s) and 96 failed judge call(s)
- NOTE: zai-low / free_form: 0 unparseable judge output(s) and 144 failed judge call(s)
