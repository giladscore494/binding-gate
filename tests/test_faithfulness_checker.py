"""Arm faithfulness_checker: decision mapping with a fake backend; MiniCheck pins; lazy heavy imports."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

import pr5_fakes as f
from binding_gate import registry
from binding_gate.arms import faithfulness_checker as fc
from binding_gate.contracts import AnswerCandidate, AnswerStatus, CheckStatus, Decision

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def backend(monkeypatch):
    """Register a scripted backend under the name "fake" and return a handle to configure it."""
    fc.reset_backends()
    holder: dict[str, f.ScriptedBackend] = {}

    def factory(**options):
        holder["backend"] = f.ScriptedBackend(**options)
        return holder["backend"]

    monkeypatch.setitem(fc.BACKENDS, "fake", factory)
    yield holder
    fc.reset_backends()


def make_arm(behaviour="target", **options):
    model = f.FamilyFake(label="m", behaviour=behaviour)
    return registry.create("arm", "faithfulness_checker", model=model, backend="fake", **options)


def test_supported_claim_is_accepted_and_the_candidate_is_the_final_answer(backend):
    output = make_arm().run(f.dg_arm_input())
    (attempt,) = output.attempts
    assert attempt.verdict.decision is Decision.ACCEPT
    assert output.final_answer == attempt.answer and attempt.answer.status is AnswerStatus.ANSWERED
    (result,) = attempt.verdict.check_results
    assert result.check == "faithfulness_checker" and result.status is CheckStatus.PASS
    assert result.details["evidence"] == "all_documents"
    assert result.details["sentences"][0]["supported"] is True


def test_evidence_is_all_shown_documents_and_the_claim_is_the_value(backend):
    arm_input = f.dg_arm_input()
    make_arm().run(arm_input)
    ((document, claims),) = backend["backend"].calls
    assert document == "\n\n".join(d.text for d in arm_input.documents)
    assert claims == ["a value."]  # the scripted model's value, split into sentences by the backend


def test_one_unsupported_sentence_blocks_the_answer(backend):
    arm = make_arm(backend_options={"verdicts": [True, False]})
    candidate = AnswerCandidate.answered(value="First. Second.", quote="q " * 5, source_id="cx_doc", raw="{}")
    verdict = arm.decide(f.dg_arm_input().documents, candidate)
    assert verdict.decision is Decision.ABSTAIN
    assert verdict.reasons == ("1 of 2 sentence(s) of the answer not supported by the shown documents according to the faithfulness checker",)
    assert [s["supported"] for s in verdict.check_results[0].details["sentences"]] == [True, False]


def test_blocked_answer_becomes_an_abstention_with_the_reason(backend):
    output = make_arm(backend_options={"verdicts": [False]}).run(f.dg_arm_input())
    assert output.attempts[0].verdict.decision is Decision.ABSTAIN
    assert output.final_answer.status is AnswerStatus.ABSTAINED
    assert output.final_answer.reason.startswith("blocked by faithfulness_checker:")


def test_cited_document_mode_uses_only_the_cited_document_and_blocks_an_unknown_source(backend):
    arm = make_arm(evidence="cited_document")
    arm_input = f.dg_arm_input()
    output = arm.run(arm_input)
    ((document, _claims),) = backend["backend"].calls
    assert document == arm_input.documents[0].text  # the scripted model cites cx_doc
    assert output.attempts[0].verdict.decision is Decision.ACCEPT
    stranger = AnswerCandidate.answered(value="v.", quote="q", source_id="doc-9", raw="{}")
    verdict = arm.decide(arm_input.documents, stranger)
    assert verdict.decision is Decision.ABSTAIN and "not one of the shown documents" in verdict.reasons[0]
    assert len(backend["backend"].calls) == 1  # the backend was not asked


@pytest.mark.parametrize("behaviour,status", [("abstain", AnswerStatus.ABSTAINED), ("garbage", AnswerStatus.UNPARSEABLE)])
def test_abstentions_and_unparseable_candidates_pass_through_unchecked(backend, behaviour, status):
    output = make_arm(behaviour=behaviour).run(f.dg_arm_input())
    assert output.final_answer.status is status and output.attempts[0].verdict is None
    assert backend["backend"].calls == []


def test_backend_failure_raises_and_keeps_the_answer_call_for_the_error_record(backend):
    arm = make_arm(backend_options={"fail": True})
    with pytest.raises(fc.FaithfulnessCheckFailed) as caught:
        arm.run(f.dg_arm_input())
    (call,) = caught.value.model_calls
    assert call.purpose == "answer" and call.response is not None


def test_one_backend_instance_is_shared_by_all_arm_instances(backend):
    first, second = make_arm(), make_arm()
    assert first.backend is second.backend


def test_options_are_validated(backend):
    with pytest.raises(ValueError, match="evidence"):
        make_arm(evidence="everything")
    with pytest.raises(ValueError, match="unknown faithfulness backend"):
        registry.create("arm", "faithfulness_checker", model=f.FamilyFake(label="m"), backend="nope")


def test_building_the_default_arm_for_the_dry_run_imports_no_heavy_dependency():
    import fakes

    fc.reset_backends()
    # The dry run's recording client, like this fake, is not a provider client: no readiness check.
    arm = registry.create("arm", "faithfulness_checker", model=fakes.ScriptedFake(label="m"))
    assert isinstance(arm.backend, fc.MiniCheckBackend)
    assert not {"torch", "transformers", "minicheck"} & set(sys.modules)
    fc.reset_backends()


@pytest.mark.skipif(importlib.util.find_spec("minicheck") is not None, reason="MiniCheck is installed here")
def test_without_the_extra_the_checker_refuses_with_an_install_hint():
    with pytest.raises(fc.FaithfulnessCheckerUnavailable, match=r"faithfulness"):
        fc.MiniCheckBackend().check("document", ["claim"])
    # In a real run (a provider client) the arm refuses when it is built, before any paid call.
    fc.reset_backends()
    with pytest.raises(fc.FaithfulnessCheckerUnavailable, match="b58b9fa"):
        registry.create("arm", "faithfulness_checker", model=f.FamilyFake(label="m", provider="openai"))
    fc.reset_backends()
    assert not {"torch", "transformers", "minicheck"} & set(sys.modules)


def test_pins_agree_with_pyproject_and_the_preregistration():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    (line,) = [l for l in pyproject.splitlines() if l.startswith("faithfulness = ")]
    assert f"git+{fc.MINICHECK_REPOSITORY}.git@{fc.MINICHECK_COMMIT}" in line
    prereg = (REPO_ROOT / "PREREGISTRATION.md").read_text(encoding="utf-8")
    section5 = prereg.split("## 5. Arms", 1)[1].split("## 6.", 1)[0]
    bullet = section5.split("- **The faithfulness checker", 1)[1].split("\n- **Judge-family rule", 1)[0]
    assert "TODO" not in bullet
    for pin in (fc.MINICHECK_COMMIT, fc.MINICHECK_WEIGHTS_REVISION, fc.MINICHECK_CHECKPOINT):
        assert pin in bullet
    assert re.fullmatch(r"[0-9a-f]{40}", fc.MINICHECK_COMMIT) and re.fullmatch(r"[0-9a-f]{40}", fc.MINICHECK_WEIGHTS_REVISION)
    assert fc.MiniCheckBackend().describe()["commit"] == fc.MINICHECK_COMMIT
