## Smoke usage check (measured tokens vs the budget's assumptions)

**OK - every planned run stays under its cap**

| model id | calls | mean output tok | assumed high | mean reasoning | output ratio | input ratio | cost ratio |
|---|---:|---:|---:|---:|---:|---:|---:|
| `glm-5.3` | 15 (+0 without usage) | 109.8 | 2250 | 0.0 | 0.0488 | 0.8114 | 0.1399 |
| `glm-5.3-flash` | 15 (+0 without usage) | 95.3 | 2250 | 23.6 | 0.0424 | 0.8114 | 0.1296 |
| `gpt-6-luna` | 15 (+0 without usage) | 79.5 | 750 | 0.0 | 0.106 | 0.7898 | 0.2452 |
| `gpt-6.1-sol` | 15 (+0 without usage) | 121.3 | 2250 | 23.7 | 0.0539 | 0.7898 | 0.1117 |

| run | planned high | x ratio | projected high | cap | |
|---|---:|---:|---:|---:|---|
| smoke | 0.48 | 1.00 | 0.48 | 0.80 | ok |
| decomposer_eval | 0.24 | 0.25 | 0.06 | 0.40 | ok |
| dev_baseline | 4.52 | 1.00 | 4.52 | 4.80 | ok |
| dev_repeats | 0.46 | 1.00 | 0.46 | 0.55 | ok |
| dev_gated | 0.06 | 1.00 | 0.06 | 0.30 | ok |
| dev_gated_scope | 0.06 | 1.00 | 0.06 | 0.30 | ok |
| dev_ablation_quote_verbatim | 0.06 | 1.00 | 0.06 | 0.30 | ok |
| dev_ablation_entity_in_window | 0.06 | 1.00 | 0.06 | 0.30 | ok |
| dev_ablation_qualifiers_in_window | 0.06 | 1.00 | 0.06 | 0.30 | ok |
| dev_ablation_no_closer_competitor | 0.06 | 1.00 | 0.06 | 0.30 | ok |
| dev_ablation_premise_present | 0.06 | 1.00 | 0.06 | 0.30 | ok |
| dev_baselines | 0.95 | 1.00 | 0.95 | 1.10 | ok |
| dev_llm_judge_checks | 1.51 | 1.00 | 1.51 | 1.60 | ok |
| rescore_dev_llm_judge | 4.52 | 1.00 | 4.52 | 4.75 | ok |
| dev_live_smoke | 0.02 | 0.25 | 0.00 | 0.10 | ok |
| dev_live | 1.20 | 1.00 | 1.20 | 1.30 | ok |
| test_layer1 | 15.85 | 1.00 | 15.85 | 16.75 | ok |
| test_layer1_midtier | 6.55 | 1.00 | 6.55 | 6.95 | ok |
| test_live | 1.63 | 1.00 | 1.63 | 1.80 | ok |
| dev_sec_smoke | 0.03 | 1.00 | 0.03 | 0.30 | ok |
| dev_sec_baseline | 1.55 | 1.00 | 1.55 | 1.70 | ok |
| dev_sec_gated | 0.15 | 1.00 | 0.15 | 0.30 | ok |
| dev_sec_gated_scope | 0.15 | 1.00 | 0.15 | 0.30 | ok |
| test_sec | 2.55 | 1.00 | 2.55 | 2.75 | ok |

