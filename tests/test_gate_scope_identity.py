"""PR 18: with its new options at their defaults the gate is byte-identical to the gate before PR 18.

``tests/gate_identity_pr17.json`` was computed with main at d8c33f8 (PRs 1-17), before any PR 18 change to
``src/`` (see ``tests/gate_identity.py``). The verdicts (``Verdict.to_json()``: decision, reasons, every check
result with its details, research request) of every case of the synthetic Deceptive Grounding corpus (the inputs
of the end-to-end test) and of the invented SEC EDGAR corpus (the layer-2 fixtures) must hash to the same values
under four sets of the pre-existing parameters, whether the new options are left out, given at their default
values one by one, or given as ``binding_scope = "window"``.
"""

from __future__ import annotations

import json

import pytest

import gate_identity

GOLDEN = json.loads(gate_identity.GOLDEN.read_text(encoding="utf-8"))

VARIANTS = {
    "options_left_out": None,
    "options_at_defaults": {"entity_scope": "window", "qualifier_scope": "window", "table_binding": False},
    "profile_window": {"binding_scope": "window"},
}


@pytest.fixture(scope="module")
def cases(tmp_path_factory):
    return gate_identity.corpora(tmp_path_factory.mktemp("identity"))


def test_the_golden_file_was_made_before_pr18_and_covers_both_corpora():
    assert GOLDEN["source"].startswith("main at d8c33f8")
    groups = GOLDEN["groups"]
    assert {g.split("/")[0] for g in groups} == {"dg", "sec"}
    assert {g.split("/", 1)[1] for g in groups} == set(gate_identity.PARAMETER_SETS)
    assert all(v["n"] > 400 for v in groups.values())


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_default_options_give_byte_identical_verdicts(cases, variant):
    assert gate_identity.all_digests(cases, VARIANTS[variant]) == GOLDEN["groups"]


def test_a_non_default_option_does_change_something_on_the_sec_corpus(cases):
    """The identity above is not vacuous: the SEC corpus is sensitive to the new options."""
    changed = gate_identity.all_digests(cases, {"binding_scope": "subject_table"})
    assert changed["sec/proposed"] != GOLDEN["groups"]["sec/proposed"]
