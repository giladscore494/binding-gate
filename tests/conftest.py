"""Shared pytest setup.

All component modules are imported once, before any test runs, so that their
``@register`` decorators land in the real registry tables. Some registry tests
temporarily swap those tables; without this, a module first imported during
such a test would stay unregistered for the rest of the session.
"""

from __future__ import annotations

from binding_gate import registry

for _kind in registry.KIND_PACKAGES:
    registry.discover(_kind)

import fakes  # noqa: E402,F401 - registers the scripted fake model client

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_fake_call_log():
    """The scripted fake logs every call in a module-level list; start each test with it empty."""
    fakes.CALLS.clear()
    yield
    fakes.CALLS.clear()
