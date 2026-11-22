## Secrets (names only) and free preflights

| secret | set | needed by the USD 50 study |
|---|---|---|
| `OPENAI_API_KEY` | NO | yes |
| `ANTHROPIC_API_KEY` | NO | no (optional) |
| `GEMINI_API_KEY` | NO | yes |
| `MOONSHOT_API_KEY` | NO | yes |
| `DEEPSEEK_API_KEY` | NO | no (optional) |
| `ZAI_API_KEY` | NO | yes |
| `SERPAPI_API_KEY` | NO | yes |
| `RUNS_ENCRYPTION_PASSPHRASE` | NO | yes |
| `SEC_CONTACT_EMAIL` | NO | yes |

| target | model / provider id | status | detail |
|---|---|---|---|
| openai-low (configs/dev_smoke.toml) | `gpt-6-luna` | **SKIPPED** | secret OPENAI_API_KEY is not set |
| openai-mid (configs/dev_smoke.toml) | `gpt-6.1-sol` | **SKIPPED** | secret OPENAI_API_KEY is not set |
| google-low (configs/dev_smoke.toml) | `gemini-3.1-flash-lite` | **SKIPPED** | secret GEMINI_API_KEY is not set |
| google-mid (configs/dev_smoke.toml) | `gemini-3.5-flash` | **SKIPPED** | secret GEMINI_API_KEY is not set |
| zai-low (configs/dev_smoke.toml) | `glm-5.3-flash` | **SKIPPED** | secret ZAI_API_KEY is not set |
| zai-mid (configs/dev_smoke.toml) | `glm-5.3` | **SKIPPED** | secret ZAI_API_KEY is not set |
| judge judge-kimi-k3.1-pro (arm llm_judge_checks) (configs/dev_llm_judge_checks.toml) | `kimi-k3.1-pro` | **SKIPPED** | secret MOONSHOT_API_KEY is not set |
| search search (arm no_gate) (configs/dev_live_smoke.toml) | `serpapi/google` | **SKIPPED** | secret SERPAPI_API_KEY is not set |
