## Smoke usage check (measured tokens vs the budget's assumptions)

**OK - every planned run stays under its cap**

| model id | calls | mean output tok | assumed high | mean reasoning | output ratio | input ratio | cost ratio |
|---|---:|---:|---:|---:|---:|---:|---:|
| `gemini-3.1-flash-lite` | 6 (+0 without usage) | 87.5 | 750 | None | 0.1167 | 2.0742 | 0.8193 |
| `glm-5.3-flash` | 6 (+0 without usage) | 68.8 | 2250 | 41.3 | 0.0306 | 1.6799 | 0.4453 |
| `gpt-6-luna` | 6 (+0 without usage) | 71.5 | 750 | 0.0 | 0.0953 | 1.6216 | 0.7086 |

| run | planned high | x ratio | projected high | cap | |
|---|---:|---:|---:|---:|---|
| smoke | 0.48 | 1.00 | 0.48 | 0.80 | ok |
| decomposer_eval | 0.24 | 0.71 | 0.17 | 0.40 | ok |
| dev_baseline | 4.52 | 0.82 | 3.70 | 4.80 | ok |
| dev_repeats | 0.46 | 0.82 | 0.38 | 0.55 | stopped |
| dev_gated | 0.06 | 0.82 | 0.05 | 0.30 | stopped |
| dev_gated_scope | 0.06 | 0.82 | 0.05 | 0.30 | stopped |
| dev_ablation_quote_verbatim | 0.06 | 0.82 | 0.05 | 0.30 | stopped |
| dev_ablation_entity_in_window | 0.06 | 0.82 | 0.05 | 0.30 | stopped |
| dev_ablation_qualifiers_in_window | 0.06 | 0.82 | 0.05 | 0.30 | stopped |
| dev_ablation_no_closer_competitor | 0.06 | 0.82 | 0.05 | 0.30 | stopped |
| dev_ablation_premise_present | 0.06 | 0.82 | 0.05 | 0.30 | stopped |
| dev_baselines | 0.95 | 0.82 | 0.78 | 1.10 | stopped |
| dev_llm_judge_checks | 1.51 | 1.00 | 1.51 | 1.60 | stopped |
| rescore_dev_llm_judge | 4.52 | 1.00 | 4.52 | 4.75 | stopped |
| dev_live_smoke | 0.02 | 0.71 | 0.01 | 0.10 | ok |
| dev_live | 1.20 | 0.82 | 0.98 | 1.30 | ok |
| test_layer1 | 15.85 | 0.82 | 12.99 | 16.75 | stopped |
| test_layer1_midtier | 6.55 | 1.00 | 6.55 | 6.95 | stopped |
| test_live | 1.63 | 0.82 | 1.34 | 1.80 | ok |
| dev_sec_smoke | 0.03 | 0.82 | 0.02 | 0.30 | ok |
| dev_sec_baseline | 1.55 | 0.82 | 1.27 | 1.70 | ok |
| dev_sec_gated | 0.15 | 0.82 | 0.12 | 0.30 | ok |
| dev_sec_gated_scope | 0.15 | 0.82 | 0.12 | 0.30 | ok |
| test_sec | 2.55 | 0.82 | 2.09 | 2.75 | ok |
| validate_layer1_judge_structured | 10.87 | 1.00 | 10.87 | 11.45 | ok |
| validate_layer1_freeform | 0.43 | 0.82 | 0.35 | 0.50 | ok |
| validate_layer1_judge_freeform | 11.13 | 1.00 | 11.13 | 11.70 | ok |

