"""The ONE place where environment variables are documented and read.

Policy (see README.md): environment variables are used only for secrets such as
API keys. Everything that changes what an experiment does lives in a versioned
config file under ``configs/`` and is selected per run by file path.

A pull request that needs a new secret adds one entry to :data:`KNOWN_SECRETS`
and reads it with :func:`get_secret`. Secret values must never be written to
traces, manifests, logs or error messages.
"""

from __future__ import annotations

import os

KNOWN_SECRETS: dict[str, str] = {
    # "ENV_VAR_NAME": "what it is for and which component reads it",
    # No secrets are needed in PR 1: nothing in this PR calls a model or the network.
    "OPENAI_API_KEY": "OpenAI API key; read by models/openai_responses.py (PR 2)",
    "ANTHROPIC_API_KEY": "Anthropic API key; read by models/anthropic_messages.py (PR 2)",
    "GEMINI_API_KEY": "Google Gemini API key; read by models/google_gemini.py (PR 2)",
    "MOONSHOT_API_KEY": "Moonshot (Kimi) API key; read by models/moonshot_chat.py (PR 2)",
    "SERPAPI_API_KEY": "SerpApi key for live web search (layer 3); read by research/search.py (PR 8)",
    "RUNS_ENCRYPTION_PASSPHRASE": (
        "passphrase with which .github/workflows/baseline.yml encrypts the run artifacts of live-web runs, "
        "which contain third-party page text (PR 8); read by the workflow only, never by Python code"
    ),
}


class SecretError(RuntimeError):
    """Raised when a secret is undocumented or not set."""


def has_secret(name: str) -> bool:
    """True if the documented secret ``name`` is set to a non-empty value (the value is not returned)."""
    if name not in KNOWN_SECRETS:
        raise SecretError(f"{name} is not documented in binding_gate.secrets.KNOWN_SECRETS")
    return bool(os.environ.get(name))


def get_secret(name: str) -> str:
    """Return the value of a documented secret from the environment."""
    if name not in KNOWN_SECRETS:
        raise SecretError(
            f"{name} is not documented in binding_gate.secrets.KNOWN_SECRETS; "
            "add it there before reading it"
        )
    value = os.environ.get(name)
    if not value:
        raise SecretError(f"environment variable {name} is not set ({KNOWN_SECRETS[name]})")
    return value
