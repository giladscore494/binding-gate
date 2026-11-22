"""Gate ``binding`` (E): the decision table, check selection by config, determinism."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import gate_fakes as g
from binding_gate import registry
from binding_gate.contracts import CheckResult, CheckStatus, Decision, Verdict
from binding_gate.gates.binding import (
    ABSTAIN_CHECKS,
    ALL_CHECKS,
    DEFAULT_PARAMETERS,
    RESEARCH_CHECKS,
    decide,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
P, F, N = CheckStatus.PASS, CheckStatus.FAIL, CheckStatus.NOT_APPLICABLE

CX = (
    "Abatacept is a fusion protein. Abatacept showed modest benefit in lupus nephritis. "
    "Unlike rituximab, abatacept was well tolerated in a small series."
)
CY = "Rituximab has emerged for lupus nephritis. Rituximab improved proteinuria in case series of adults."
DOCS = (g.doc("cx_doc", CX), g.doc("cy_doc", CY))


def gate(checks=ALL_CHECKS, **parameters):
    return registry.create("gate", "binding", checks=list(checks), **parameters)


def results(**statuses):
    return [CheckResult(name, status, {"reason": f"{name}_reason"} if status is F else {}) for name, status in statuses.items()]


def test_reserved_names_and_table_partition():
    assert ALL_CHECKS == ("quote_verbatim", "entity_in_window", "qualifiers_in_window", "no_closer_competitor", "premise_present")
    assert set(ABSTAIN_CHECKS) | set(RESEARCH_CHECKS) == set(ALL_CHECKS)
    assert not set(ABSTAIN_CHECKS) & set(RESEARCH_CHECKS)


# -- decision table -------------------------------------------------------------- #


@pytest.mark.parametrize(
    "statuses,decision",
    [
        ({"quote_verbatim": P, "entity_in_window": P, "qualifiers_in_window": N, "no_closer_competitor": N, "premise_present": N}, Decision.ACCEPT),
        ({"quote_verbatim": N}, Decision.ACCEPT),  # not_applicable never blocks
        ({"quote_verbatim": F, "entity_in_window": P}, Decision.ABSTAIN),
        ({"quote_verbatim": P, "premise_present": F}, Decision.ABSTAIN),
        ({"quote_verbatim": F, "entity_in_window": F, "no_closer_competitor": F}, Decision.ABSTAIN),  # abstain wins
        ({"premise_present": F, "qualifiers_in_window": F}, Decision.ABSTAIN),
        ({"quote_verbatim": P, "entity_in_window": F}, Decision.RESEARCH),
        ({"quote_verbatim": P, "qualifiers_in_window": F}, Decision.RESEARCH),
        ({"quote_verbatim": P, "no_closer_competitor": F}, Decision.RESEARCH),
        ({"entity_in_window": F, "qualifiers_in_window": F, "no_closer_competitor": F}, Decision.RESEARCH),
        ({"some_future_check": F, "entity_in_window": F}, Decision.ABSTAIN),  # unknown failures are conservative
    ],
)
def test_decision_table(statuses, decision):
    verdict = decide(results(**statuses), g.constraints())
    assert verdict.decision is decision
    assert [r.check for r in verdict.check_results] == list(statuses)  # every result, in order
    failed = [name for name, status in statuses.items() if status is F]
    if decision is Decision.ACCEPT:
        assert verdict.research_request is None and verdict.reasons
    else:
        assert verdict.reasons == tuple(f"{name}: {name}_reason" for name in failed)
    assert (verdict.research_request is not None) == (decision is Decision.RESEARCH)
    Verdict.from_json(verdict.to_json())  # satisfies the frozen contract and round-trips


def test_research_request_is_canonical_json_built_from_the_constraints():
    c = g.constraints(entity="abatacept", aliases=("Orencia",), qualifiers=(("lupus nephritis", ()), ("adults", ())))
    verdict = decide(results(quote_verbatim=P, entity_in_window=F, qualifiers_in_window=F), c)
    request = json.loads(verdict.research_request)
    assert request == {
        "entity": "abatacept",
        "aliases": ["Orencia"],
        "attribute": "evidence for use",
        "qualifiers": ["lupus nephritis", "adults"],
        "failed_checks": ["entity_in_window", "qualifiers_in_window"],
        "query": "abatacept evidence for use lupus nephritis adults",
    }
    assert verdict.research_request == json.dumps(request, sort_keys=True, separators=(",", ":"))


def test_reason_without_a_reason_code_still_names_the_check():
    verdict = decide([CheckResult("entity_in_window", F, {})], g.constraints())
    assert verdict.reasons == ("entity_in_window: failed",)


# -- the gate end to end ---------------------------------------------------------- #


def test_gate_accepts_a_bound_quote_and_blocks_the_competitor_quote():
    good = gate().evaluate(g.constraints(), g.answer("Abatacept showed modest benefit in lupus nephritis", "cx_doc"), DOCS)
    assert good.decision is Decision.ACCEPT
    assert [r.status for r in good.check_results] == [P, P, P, P, N]
    bad = gate().evaluate(g.constraints(), g.answer("Rituximab improved proteinuria in case series", "cy_doc"), DOCS)
    assert bad.decision is Decision.RESEARCH
    assert bad.reasons == ("entity_in_window: entity_not_in_document", "no_closer_competitor: competitor_closer")
    fabricated = gate().evaluate(g.constraints(), g.answer("Abatacept cured every patient with nephritis", "cx_doc"), DOCS)
    assert fabricated.decision is Decision.ABSTAIN and fabricated.reasons[0] == "quote_verbatim: quote_not_found"


def test_gate_runs_exactly_the_configured_checks_in_order():
    selected = ["premise_present", "quote_verbatim"]
    verdict = gate(selected).evaluate(g.constraints(), g.answer("Rituximab improved proteinuria in case series", "cy_doc"), DOCS)
    assert [r.check for r in verdict.check_results] == selected
    assert verdict.decision is Decision.ACCEPT  # the binding checks that would block are disabled


def test_ablation_by_config_changes_the_verdict_only_through_the_check_list():
    candidate = g.answer("Rituximab improved proteinuria in case series", "cy_doc")
    full = gate().evaluate(g.constraints(), candidate, DOCS)
    minus_entity = gate([c for c in ALL_CHECKS if c != "entity_in_window"]).evaluate(g.constraints(), candidate, DOCS)
    minus_both = gate([c for c in ALL_CHECKS if c not in {"entity_in_window", "no_closer_competitor"}]).evaluate(
        g.constraints(), candidate, DOCS
    )
    assert full.decision is Decision.RESEARCH and minus_entity.decision is Decision.RESEARCH
    assert minus_entity.reasons == ("no_closer_competitor: competitor_closer",)
    assert minus_both.decision is Decision.ACCEPT
    shared = {r.check: r for r in full.check_results}
    for result in minus_both.check_results:
        assert result == shared[result.check]  # a check's result does not depend on the others


def test_shared_parameters_reach_the_checks():
    candidate = g.answer("Unlike rituximab, abatacept was well tolerated", "cx_doc")
    assert gate().evaluate(g.constraints(), candidate, DOCS).decision is Decision.RESEARCH
    assert gate(competitor_tie="pass").evaluate(g.constraints(), candidate, DOCS).decision is Decision.ACCEPT
    short = g.answer("Abatacept showed modest benefit", "cx_doc")
    assert gate().evaluate(g.constraints(), short, DOCS).decision is Decision.ABSTAIN
    assert gate(min_quote_tokens=4).evaluate(g.constraints(), short, DOCS).decision is Decision.ACCEPT
    details = gate(window_tokens=7).evaluate(g.constraints(), short, DOCS).check_results[1].details
    assert details["window_tokens"] == 7
    assert set(DEFAULT_PARAMETERS) == {
        "window_tokens", "min_quote_tokens", "lead_max_tokens", "lead_min_repeats", "premise_scope", "competitor_tie",
        "entity_scope", "qualifier_scope", "table_binding", "binding_scope",  # PR 18, defaults = pre-PR-18 behaviour
    }


@pytest.mark.parametrize(
    "checks,parameters,message",
    [
        ([], {}, "non-empty"),
        ("quote_verbatim", {}, "non-empty list"),
        (["quote_verbatim", "quote_verbatim"], {}, "more than once"),
        (["quote_verbatim", "entity_near"], {}, "unknown check"),
        (["quote_verbatim"], {"window": 3}, "unknown gate parameter"),
        (["entity_in_window"], {"window_tokens": -1}, "window_tokens"),
    ],
)
def test_gate_configuration_is_validated(checks, parameters, message):
    with pytest.raises(ValueError, match=message):
        registry.create("gate", "binding", checks=checks, **parameters)


# -- determinism ----------------------------------------------------------------- #

_SCRIPT = """
import json, sys
sys.path.insert(0, {tests!r})
import gate_fakes as g
from binding_gate import registry
from binding_gate.gates.binding import ALL_CHECKS
docs = (g.doc("cx_doc", {cx!r}), g.doc("cy_doc", {cy!r}))
gate = registry.create("gate", "binding", checks=list(ALL_CHECKS))
c = g.constraints(aliases=("Orencia", "CTLA4-Ig"), qualifiers=(("lupus nephritis", ("LN",)), ("adults", ())),
                  presuppositions=(("p", ("abatacept", "lupus nephritis")),))
quotes = [("Abatacept showed modest benefit in lupus nephritis", "cx_doc"),
          ("Rituximab improved proteinuria in case series", "cy_doc"),
          ("Unlike rituximab, abatacept was well tolerated", "cx_doc"),
          ("not a quote from anywhere at all", "cx_doc")]
for quote, source in quotes:
    sys.stdout.write(gate.evaluate(c, g.answer(quote, source), docs).to_json() + "\\n")
"""


def test_same_input_gives_identical_output_bytes_across_processes_and_hash_seeds():
    script = _SCRIPT.format(tests=str(REPO_ROOT / "tests"), cx=CX, cy=CY)
    outputs = []
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(REPO_ROOT / "src")}
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, env=env, check=True)
        outputs.append(done.stdout)
    assert outputs[0] == outputs[1] == outputs[2]
    assert len(outputs[0].splitlines()) == 4
    # and in this process, with fresh gate instances, the same bytes again
    first = [line for line in outputs[0].decode("utf-8").splitlines()]
    c = g.constraints(aliases=("Orencia", "CTLA4-Ig"), qualifiers=(("lupus nephritis", ("LN",)), ("adults", ())),
                      presuppositions=(("p", ("abatacept", "lupus nephritis")),))
    again = gate().evaluate(c, g.answer("Abatacept showed modest benefit in lupus nephritis", "cx_doc"), DOCS).to_json()
    assert again == first[0]
