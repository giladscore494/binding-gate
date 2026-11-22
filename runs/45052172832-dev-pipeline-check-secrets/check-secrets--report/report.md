## Secrets (names only) and free preflights

| secret | set | needed by the USD 50 study |
|---|---|---|
| `OPENAI_API_KEY` | yes | yes |
| `ANTHROPIC_API_KEY` | NO | no (optional) |
| `GEMINI_API_KEY` | yes | yes |
| `MOONSHOT_API_KEY` | yes | yes |
| `DEEPSEEK_API_KEY` | NO | no (optional) |
| `ZAI_API_KEY` | yes | yes |
| `SERPAPI_API_KEY` | yes | yes |
| `RUNS_ENCRYPTION_PASSPHRASE` | yes | yes |
| `SEC_CONTACT_EMAIL` | yes | yes |

| target | model / provider id | status | detail |
|---|---|---|---|
| openai-low (configs/dev_smoke.toml) | `gpt-6-luna` | **OK** | openai lists 'gpt-6-luna' |
| openai-mid (configs/dev_smoke.toml) | `gpt-6.1-sol` | **OK** | openai lists 'gpt-6.1-sol' |
| google-low (configs/dev_smoke.toml) | `gemini-3.1-flash-lite` | **OK** | google lists 'gemini-3.1-flash-lite' |
| google-mid (configs/dev_smoke.toml) | `gemini-3.5-flash` | **OK** | google lists 'gemini-3.5-flash' |
| zai-low (configs/dev_smoke.toml) | `glm-5.3-flash` | **OK** | zai lists 'glm-5.3-flash' |
| zai-mid (configs/dev_smoke.toml) | `glm-5.3` | **OK** | zai lists 'glm-5.3' |
| judge judge-kimi-k3.1-pro (arm llm_judge_checks) (configs/dev_llm_judge_checks.toml) | `kimi-k3.1-pro` | **OK** | moonshot lists 'kimi-k3.1-pro' |
| search search (arm no_gate) (configs/dev_live_smoke.toml) | `serpapi/google` | **FAILED** | SerpApi account check failed: SerpApi HTTP 401: Invalid API key. Your API key should be here: https://serpapi.com/manage-api-key |
