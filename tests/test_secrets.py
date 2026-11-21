"""Environment variables are for documented secrets only."""

from __future__ import annotations

import pytest

from binding_gate import secrets
from binding_gate.secrets import SecretError, get_secret


def test_undocumented_secret_is_refused(monkeypatch):
    monkeypatch.setenv("SOME_RANDOM_KEY", "value")
    with pytest.raises(SecretError, match="not documented"):
        get_secret("SOME_RANDOM_KEY")


def test_documented_secret_is_read_and_must_be_set(monkeypatch):
    monkeypatch.setitem(secrets.KNOWN_SECRETS, "EXAMPLE_API_KEY", "example provider key")
    monkeypatch.delenv("EXAMPLE_API_KEY", raising=False)
    with pytest.raises(SecretError, match="not set"):
        get_secret("EXAMPLE_API_KEY")
    monkeypatch.setenv("EXAMPLE_API_KEY", "s3cr3t")
    assert get_secret("EXAMPLE_API_KEY") == "s3cr3t"
