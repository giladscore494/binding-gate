## Smoke usage check (measured tokens vs the budget's assumptions)

**OK - every planned run stays under its cap**

| model id | calls | mean output tok | assumed high | mean reasoning | output ratio | input ratio | cost ratio |
|---|---:|---:|---:|---:|---:|---:|---:|
| `gemini-3.1-flash-lite` | 15 (+0 without usage) | 81.9 | 750 | None | 0.1092 | 0.8006 | 0.2306 |
| `gemini-3.5-flash` | 15 (+0 without usage) | 103.6 | 750 | None | 0.1381 | 0.8006 | 0.2545 |
| `glm-5.3` | 15 (+0 without usage) | 111.3 | 2250 | 0.0 | 0.0495 | 0.8114 | 0.1405 |
| `glm-5.3-flash` | 15 (+0 without usage) | 93.6 | 2250 | 8.2 | 0.0416 | 0.8114 | 0.1289 |
| `gpt-6-luna` | 15 (+0 without usage) | 79.9 | 750 | 0.0 | 0.1066 | 0.7898 | 0.2457 |
| `gpt-6.1-sol` | 15 (+0 without usage) | 125.9 | 2250 | 27.4 | 0.056 | 0.7898 | 0.1136 |

| run | planned high | x ratio | projected high | cap | |
|---|---:|---:|---:|---:|---|
| smoke | 0.48 | 0.25 | 0.12 | 0.80 | ok |
| decomposer_eval | 0.24 | 0.25 | 0.06 | 0.40 | ok |
| dev_baseline | 4.52 | 0.25 | 1.11 | 4.80 | ok |
| dev_repeats | 0.46 | 0.25 | 0.11 | 0.55 | ok |
| dev_gated | 0.06 | 0.25 | 0.01 | 0.30 | ok |
| dev_gated_scope | 0.06 | 0.25 | 0.01 | 0.30 | ok |
| dev_ablation_quote_verbatim | 0.06 | 0.25 | 0.01 | 0.30 | ok |
| dev_ablation_entity_in_window | 0.06 | 0.25 | 0.01 | 0.30 | ok |
| dev_ablation_qualifiers_in_window | 0.06 | 0.25 | 0.01 | 0.30 | ok |
| dev_ablation_no_closer_competitor | 0.06 | 0.25 | 0.01 | 0.30 | ok |
| dev_ablation_premise_present | 0.06 | 0.25 | 0.01 | 0.30 | ok |
| dev_baselines | 0.95 | 0.25 | 0.23 | 1.10 | ok |
| dev_llm_judge_checks | 1.51 | 1.00 | 1.51 | 1.60 | ok |
| rescore_dev_llm_judge | 4.52 | 1.00 | 4.52 | 4.75 | ok |
| dev_live_smoke | 0.02 | 0.25 | 0.00 | 0.10 | ok |
| dev_live | 1.20 | 0.25 | 0.29 | 1.30 | ok |
| test_layer1 | 15.85 | 0.25 | 3.89 | 16.75 | ok |
| test_layer1_midtier | 6.55 | 0.25 | 1.67 | 6.95 | ok |
| test_live | 1.63 | 0.25 | 0.40 | 1.80 | ok |
| dev_sec_smoke | 0.03 | 0.25 | 0.01 | 0.30 | ok |
| dev_sec_baseline | 1.55 | 0.25 | 0.38 | 1.70 | ok |
| dev_sec_gated | 0.15 | 0.25 | 0.04 | 0.30 | ok |
| dev_sec_gated_scope | 0.15 | 0.25 | 0.04 | 0.30 | ok |
| test_sec | 2.55 | 0.25 | 0.63 | 2.75 | ok |

