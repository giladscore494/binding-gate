## Layer-1 STOP validation (EXPLORATORY, post hoc; decides nothing)

- PREREGISTRATION.md section 18, amendment 1 (2026-11-22); decided after the layer-1 dev baseline (no_gate, deterministic provenance scoring) gave STOP under section 10 (workflow run 45053502161).
- Subsample: 12 items per condition, seed 20261122, excluded absent/null_control, partial/null_control, complete/null_control (configs/rescore/validation_judge_structured.toml).
- Judge: kimi-k3.1-pro (benchmark judge prompt); intervals: cluster bootstrap by group_id, seed 20261122, 10000 resamples, level 19/20.

### Judge rates (all subsample records; 95% interval)

| model | arm | records | EAF or substitution | EAF | substitution | confabulation | judge unparseable / error | unscored |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| google-low | structured | 144 | 0.7% [0.0, 2.3] | 0.7% | 0.0% | 0.0% | 0 / 0 | 0 |
| google-low | free_form | 144 | 2.1% [0.0, 4.7] | 2.1% | 0.0% | 0.0% | 1 / 0 | 0 |
| openai-low | structured | 144 | 1.4% [0.0, 3.6] | 1.4% | 0.0% | 0.0% | 0 / 0 | 0 |
| openai-low | free_form | 144 | 1.4% [0.0, 3.8] | 1.4% | 0.7% | 0.0% | 2 / 0 | 0 |
| zai-low | structured | 144 | 0.7% [0.0, 2.4] | 0.7% | 0.0% | 0.0% | 1 / 0 | 0 |
| zai-low | free_form | 144 | 4.2% [1.0, 8.0] | 4.2% | 0.7% | 0.0% | 8 / 0 | 0 |

### V1: agreement with the deterministic label (structured answers)

| model | deterministic wrong-entity | judge EAF or substitution | raw agreement | kappa | kappa (parsed only) | only ours | only judge |
|---|---:|---:|---:|---:|---:|---:|---:|
| google-low | 1.4% | 0.7% | 96.9% | -0.014 | -0.014 | 2 | 1 |
| openai-low | 1.4% | 1.4% | 97.9% | 0.489 | 0.489 | 1 | 1 |
| zai-low | 0.7% | 0.7% | 99.0% | 0.663 | 1.000 | 0 | 0 |

### V2: free-form minus structured, paired by item (95% interval)

| model | paired items | structured | free form | difference | difference (both determinate) | only structured | only free form |
|---|---:|---:|---:|---:|---:|---:|---:|
| google-low | 144 | 0.7% | 2.1% | 1.4% [0.0, 3.5] | 1.4% [0.0, 3.5] | 0 | 2 |
| openai-low | 144 | 1.4% | 1.4% | 0.0% [-2.0, 2.1] | 0.0% [-2.0, 2.2] | 1 | 1 |
| zai-low | 144 | 0.7% | 4.2% | 3.5% [0.7, 7.1] | 3.7% [0.7, 7.4] | 0 | 5 |

- NOTE: google-low / free_form: 1 unparseable judge output(s) and 0 failed judge call(s)
- NOTE: openai-low / free_form: 2 unparseable judge output(s) and 0 failed judge call(s)
- NOTE: zai-low / structured: 1 unparseable judge output(s) and 0 failed judge call(s)
- NOTE: zai-low / free_form: 8 unparseable judge output(s) and 0 failed judge call(s)
