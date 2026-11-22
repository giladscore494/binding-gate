"""Registration by adding a module, without editing any shared file."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from binding_gate import registry
from binding_gate.registry import RegistryError, available, create, register


@pytest.fixture
def isolated_registry(monkeypatch):
    """Give each test empty factory tables so nothing leaks between tests."""
    monkeypatch.setattr(registry, "_factories", {kind: {} for kind in registry.KIND_PACKAGES})
    monkeypatch.setattr(registry, "_discovered", set())


def test_every_kind_has_an_importable_package():
    assert set(registry.KIND_PACKAGES) == {"dataset", "model", "decomposer", "check", "gate", "arm", "scorer"}
    for kind in registry.KIND_PACKAGES:
        assert isinstance(available(kind), list)


def test_pr2_and_pr4_components_are_registered():
    # Not isolated on purpose: this is the real registry after discovery.
    assert "deceptive_grounding" in available("dataset")
    assert {"openai_responses", "anthropic_messages", "google_gemini", "moonshot_chat"} <= set(available("model"))
    assert {"no_gate", "gated"} <= set(available("arm"))
    assert "provenance" in available("scorer")
    # test doubles (tests/gate_fakes.py registers "fake_decomposer") are excluded
    assert [n for n in available("decomposer") if not n.startswith("fake_")] == ["model_decomposer"]  # PR 3
    # PR 4: the five reserved check names and the verdict gate
    assert available("check") == sorted(
        ["quote_verbatim", "entity_in_window", "qualifiers_in_window", "no_closer_competitor", "premise_present"]
    )
    assert available("gate") == ["binding"]


def test_register_and_create_with_options(isolated_registry):
    @register("check", "always_pass")
    class AlwaysPass:
        name = "always_pass"

        def __init__(self, window: int = 100):
            self.window = window

    assert available("check") == ["always_pass"]
    assert create("check", "always_pass", window=250).window == 250
    assert available("gate") == []


def test_duplicate_names_are_an_error(isolated_registry):
    register("arm", "no_gate")(lambda: "first")
    with pytest.raises(RegistryError, match="already registered"):
        register("arm", "no_gate")(lambda: "second")
    assert create("arm", "no_gate") == "first"


def test_same_name_in_different_kinds_is_fine(isolated_registry):
    register("arm", "x")(lambda: "arm")
    register("scorer", "x")(lambda: "scorer")
    assert (create("arm", "x"), create("scorer", "x")) == ("arm", "scorer")


def test_unknown_kind_and_name(isolated_registry):
    with pytest.raises(RegistryError, match="unknown component kind"):
        register("plugin", "x")
    with pytest.raises(RegistryError, match="unknown component kind"):
        available("plugin")
    with pytest.raises(RegistryError, match="no check named 'missing'"):
        create("check", "missing")


def test_dropping_a_module_into_the_package_registers_it(isolated_registry, monkeypatch, tmp_path: Path):
    package = tmp_path / "fake_checks_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "quote_verbatim.py").write_text(
        textwrap.dedent(
            """
            from binding_gate.registry import register

            @register("check", "quote_verbatim")
            class QuoteVerbatim:
                name = "quote_verbatim"
            """
        )
    )
    (package / "entity_in_window.py").write_text(
        textwrap.dedent(
            """
            from binding_gate.registry import register

            @register("check", "entity_in_window")
            def build(**options):
                return ("entity_in_window", options)
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(registry.KIND_PACKAGES, "check", "fake_checks_pkg")
    try:
        assert available("check") == ["entity_in_window", "quote_verbatim"]
        assert create("check", "entity_in_window", window=5) == ("entity_in_window", {"window": 5})
        assert create("check", "quote_verbatim").name == "quote_verbatim"
    finally:
        for name in [m for m in sys.modules if m.startswith("fake_checks_pkg")]:
            del sys.modules[name]
