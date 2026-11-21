"""Contracts: exact JSON round-trips and validation."""

from __future__ import annotations

import hashlib
import json

import pytest

import builders as b
from binding_gate.contracts import (
    AnswerCandidate,
    AnswerStatus,
    ArmInput,
    ArmOutput,
    Attempt,
    CheckResult,
    CheckStatus,
    ContractError,
    Decision,
    Decomposition,
    Document,
    Entity,
    EvidenceRef,
    Item,
    Message,
    ModelCall,
    ModelRequest,
    ModelResponse,
    Presupposition,
    Qualifier,
    QualifierKind,
    RunManifest,
    ScoreResult,
    TokenUsage,
    TraceRecord,
    Verdict,
    CONTRACT_TYPES,
    from_envelope,
    to_envelope,
)


def _manifest() -> RunManifest:
    config = b.run_config()
    return RunManifest(
        run_id="run-1",
        config=config.to_dict(),
        config_hash=config.config_hash,
        git_commit=b.COMMIT,
        git_dirty=False,
        package_version="0.1.0",
        python_version="3.13.0",
        started_at=b.T0,
    )


SAMPLES = [
    Entity("Alphadrug", ("alpha-drug",), "drug"),
    Qualifier(QualifierKind.UNIT, "mg"),
    Presupposition("Alphadrug exists", ("Alphadrug",)),
    b.constraints(),
    b.documents()[1].ref(),
    b.documents()[1],
    b.answer(),
    AnswerCandidate.abstained(raw="I cannot tell.", reason="no evidence"),
    AnswerCandidate.unparseable(raw="???", reason="no JSON object found"),
    CheckResult("entity_in_window", CheckStatus.NOT_APPLICABLE, {"why": "no qualifiers"}),
    b.accept(),
    b.abstain(),
    b.research(),
    b.item().arm_input(),
    b.item(),
    Message("user", ""),
    TokenUsage(1, 2),
    b.model_call().request,
    b.model_call().response,
    b.model_call(),
    ModelCall("answer", "model-a", b.model_call().request, b.T0, b.T1, error="HTTP 500"),
    Decomposition(b.constraints(), (b.model_call(),)),
    Attempt(b.answer(), (), b.accept()),
    ArmOutput((Attempt(b.answer()),), b.answer(), model_calls=(b.model_call(),)),
    ScoreResult("provenance", "competitor_only"),
    b.trace_record(),
    _manifest(),
]


def test_samples_cover_every_contract_type():
    assert {type(s) for s in SAMPLES} == set(CONTRACT_TYPES)


@pytest.mark.parametrize("obj", SAMPLES, ids=lambda o: type(o).__name__)
def test_round_trip_is_exact(obj):
    as_dict = obj.to_dict()
    assert json.loads(json.dumps(as_dict)) == as_dict, "to_dict must be plain JSON"
    assert type(obj).from_dict(as_dict) == obj
    assert type(obj).from_json(obj.to_json()) == obj
    assert type(obj).from_json(obj.to_json()).to_json() == obj.to_json()
    assert from_envelope(to_envelope(obj)) == obj


@pytest.mark.parametrize("obj", SAMPLES, ids=lambda o: type(o).__name__)
def test_unknown_field_is_rejected(obj):
    with pytest.raises(ContractError, match="unknown field"):
        type(obj).from_dict({**obj.to_dict(), "surprise": 1})


@pytest.mark.parametrize("obj", SAMPLES, ids=lambda o: type(o).__name__)
def test_missing_required_field_is_rejected(obj):
    data = obj.to_dict()
    data.pop(next(iter(data)))  # the first declared field is always required
    with pytest.raises(ContractError, match="missing field"):
        type(obj).from_dict(data)


def test_contracts_are_immutable():
    with pytest.raises(AttributeError):
        b.answer().value = "20 mg"  # type: ignore[misc]


def test_opaque_json_is_copied_and_must_be_serialisable():
    gold = {"roles": {"doc-1": "target"}}
    item = Item("i", "g", "toy", "Q?", gold=gold)
    gold["roles"]["doc-1"] = "tampered"
    assert item.gold == {"roles": {"doc-1": "target"}}
    with pytest.raises(ContractError, match="JSON"):
        Item("i", "g", "toy", "Q?", gold={"bad": object()})
    with pytest.raises(ContractError, match="JSON"):
        CheckResult("c", CheckStatus.PASS, {"x": float("nan")})


@pytest.mark.parametrize(
    "build",
    [
        lambda: Entity(""),
        lambda: Entity("X", aliases="not-a-list"),
        lambda: Entity("X", aliases=("",)),
        lambda: Qualifier("era", "2024"),
        lambda: Document("d", "text", "0" * 64),  # hash does not match text
        lambda: Document("d", "text", "XYZ"),
        lambda: Document.create("d", ""),
        lambda: EvidenceRef("d", "abc"),
        lambda: TokenUsage(-1, 0),
        lambda: TokenUsage(True, 0),
        lambda: ModelRequest(()),
        lambda: ModelCall("answer", "m", b.model_call().request, b.T0, b.T1),  # neither response nor error
        lambda: ModelCall("answer", "m", b.model_call().request, "yesterday", b.T1, error="x"),
        lambda: ModelCall("answer", "m", b.model_call().request, "2026-11-22T10:00:00", b.T1, error="x"),  # no offset
        lambda: Item("i", "g", "toy", "Q?", documents=(b.documents()[0], b.documents()[0])),
        lambda: Item("i", "", "toy", "Q?"),
    ],
)
def test_invalid_values_are_rejected(build):
    with pytest.raises(ContractError):
        build()


def test_document_hash_is_sha256_of_text():
    doc = Document.create("d", "héllo")
    assert doc.sha256 == hashlib.sha256("héllo".encode("utf-8")).hexdigest()
    assert doc.ref() == EvidenceRef("d", doc.sha256)


class TestAnswerCandidate:
    def test_answered_needs_all_three_parts(self):
        with pytest.raises(ContractError, match="value, quote and source_id"):
            AnswerCandidate(AnswerStatus.ANSWERED, "raw", value="10 mg", quote="q")
        with pytest.raises(ContractError):
            AnswerCandidate(AnswerStatus.ANSWERED, "raw", value="10 mg", quote="", source_id="d")

    def test_abstained_and_unparseable_carry_no_answer(self):
        with pytest.raises(ContractError, match="must not carry"):
            AnswerCandidate(AnswerStatus.ABSTAINED, "raw", value="10 mg")
        with pytest.raises(ContractError, match="must not carry"):
            AnswerCandidate(AnswerStatus.UNPARSEABLE, "raw", quote="q", reason="r")

    def test_unparseable_needs_reason(self):
        with pytest.raises(ContractError, match="reason"):
            AnswerCandidate(AnswerStatus.UNPARSEABLE, "raw")


class TestVerdict:
    def test_accept_is_impossible_with_a_failed_check(self):
        with pytest.raises(ContractError, match="checks failed"):
            Verdict(Decision.ACCEPT, check_results=(CheckResult("c", CheckStatus.FAIL),))

    def test_abstain_and_research_need_reasons(self):
        with pytest.raises(ContractError, match="reason"):
            Verdict(Decision.ABSTAIN)
        with pytest.raises(ContractError, match="reason"):
            Verdict(Decision.RESEARCH, research_request="look again")

    def test_research_request_only_for_research(self):
        with pytest.raises(ContractError, match="research_request"):
            Verdict(Decision.RESEARCH, reasons=("r",))
        with pytest.raises(ContractError, match="research_request"):
            Verdict(Decision.ABSTAIN, reasons=("r",), research_request="q")


class TestOutcomeInvariants:
    def test_gate_only_judges_answered_candidates(self):
        with pytest.raises(ContractError, match="answered candidate"):
            Attempt(AnswerCandidate.unparseable(raw="?", reason="r"), (), b.abstain())

    def test_ungated_or_accepted_final_answer_is_the_candidate(self):
        ArmOutput((Attempt(b.answer(), (), b.accept()),), b.answer())
        with pytest.raises(ContractError, match="must equal"):
            ArmOutput((Attempt(b.answer()),), AnswerCandidate.abstained(raw=""))

    def test_blocked_answer_becomes_an_abstention(self):
        blocked = AnswerCandidate.abstained(raw="", reason="gate: entity not near quote")
        ArmOutput((Attempt(b.answer(), (), b.abstain()),), blocked)
        ArmOutput((Attempt(b.answer(), (), b.research()),), blocked)
        with pytest.raises(ContractError, match="abstention"):
            ArmOutput((Attempt(b.answer(), (), b.abstain()),), b.answer())

    def test_unparseable_output_stays_unparseable(self):
        bad = AnswerCandidate.unparseable(raw="?", reason="r")
        assert ArmOutput((Attempt(bad),), bad).final_answer.status is AnswerStatus.UNPARSEABLE

    def test_only_research_leads_to_another_attempt(self):
        ArmOutput((Attempt(b.answer(), (), b.research()), Attempt(b.answer(), (), b.accept())), b.answer())
        with pytest.raises(ContractError, match="must be research"):
            ArmOutput((Attempt(b.answer(), (), b.abstain()), Attempt(b.answer())), b.answer())
        with pytest.raises(ContractError, match="at least one attempt"):
            ArmOutput((), b.answer())


class TestTraceRecord:
    def test_error_record_has_no_outcome(self):
        base = b.trace_record().to_dict()
        crashed = {**base, "attempts": [], "final_answer": None, "error": "TimeoutError"}
        assert TraceRecord.from_dict(crashed).error == "TimeoutError"
        with pytest.raises(ContractError, match="must not carry an outcome"):
            TraceRecord.from_dict({**base, "error": "TimeoutError"})
        with pytest.raises(ContractError, match="final_answer"):
            TraceRecord.from_dict({**base, "final_answer": None})

    def test_total_usage_sums_reported_usage(self):
        assert b.trace_record().total_usage() == TokenUsage(240, 60)

    def test_split_and_hash_are_validated(self):
        base = b.trace_record().to_dict()
        with pytest.raises(ContractError, match="split"):
            TraceRecord.from_dict({**base, "split": "train"})
        with pytest.raises(ContractError, match="config_hash"):
            TraceRecord.from_dict({**base, "config_hash": "abc"})


def test_arm_input_hides_gold_group_and_stratum():
    view = b.item().arm_input()
    assert isinstance(view, ArmInput)
    assert set(view.to_dict()) == {"item_id", "question", "documents"}
    assert "target" not in view.to_json() and "competitor" not in view.to_json()


def test_manifest_config_hash_must_match_config():
    data = _manifest().to_dict()
    data["config"]["repeats"] = 99
    with pytest.raises(ContractError, match="config_hash"):
        RunManifest.from_dict(data)


def test_model_response_usage_optional():
    assert ModelResponse("t", "m").usage is None
