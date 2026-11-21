"""schema_version is enforced on load, and contract changes force a version bump."""

from __future__ import annotations

import dataclasses

import pytest

import builders as b
from binding_gate import contracts
from binding_gate.contracts import (
    SCHEMA_VERSION,
    ContractError,
    Item,
    RunManifest,
    TraceRecord,
    from_envelope,
    schema_fingerprint,
    to_envelope,
)
from binding_gate.schema_lock import SCHEMA_FINGERPRINTS


def test_contracts_match_the_fingerprint_locked_for_this_version():
    assert SCHEMA_VERSION in SCHEMA_FINGERPRINTS, "add the new version to schema_lock.py"
    assert schema_fingerprint() == SCHEMA_FINGERPRINTS[SCHEMA_VERSION], (
        "A contract changed without a schema version bump. Increase "
        "contracts.SCHEMA_VERSION and add the new fingerprint to schema_lock.py "
        "(see the instructions in that file)."
    )


def test_current_version_is_the_newest_locked_version():
    assert SCHEMA_VERSION == max(SCHEMA_FINGERPRINTS)
    assert sorted(SCHEMA_FINGERPRINTS) == list(range(1, SCHEMA_VERSION + 1))
    assert len(set(SCHEMA_FINGERPRINTS.values())) == len(SCHEMA_FINGERPRINTS)


def test_fingerprint_changes_when_a_contract_changes(monkeypatch):
    @dataclasses.dataclass(frozen=True)
    class TokenUsage(contracts.Contract):  # same name, one extra field
        input_tokens: int
        output_tokens: int
        cached_tokens: int = 0

    changed = tuple(TokenUsage if c is contracts.TokenUsage else c for c in contracts.CONTRACT_TYPES)
    monkeypatch.setattr(contracts, "CONTRACT_TYPES", changed)
    assert schema_fingerprint() != SCHEMA_FINGERPRINTS[SCHEMA_VERSION]


@pytest.mark.parametrize("cls,sample", [(Item, b.item()), (TraceRecord, b.trace_record())])
@pytest.mark.parametrize("bad_version", [0, SCHEMA_VERSION + 1, str(SCHEMA_VERSION), None, True])
def test_records_of_another_version_are_refused(cls, sample, bad_version):
    data = {**sample.to_dict(), "schema_version": bad_version}
    with pytest.raises(ContractError, match="schema_version"):
        cls.from_dict(data)


@pytest.mark.parametrize("cls,sample", [(Item, b.item()), (TraceRecord, b.trace_record())])
def test_records_without_a_version_are_refused(cls, sample):
    data = sample.to_dict()
    assert data["schema_version"] == SCHEMA_VERSION
    del data["schema_version"]
    with pytest.raises(ContractError, match="schema_version"):
        cls.from_dict(data)


def test_persisted_record_types_all_carry_schema_version():
    for cls in (Item, TraceRecord, RunManifest):
        assert "schema_version" in {f.name for f in dataclasses.fields(cls)}


def test_envelope_enforces_version_and_kind():
    envelope = to_envelope(b.constraints())
    assert envelope["schema_version"] == SCHEMA_VERSION and envelope["kind"] == "QuestionConstraints"
    with pytest.raises(ContractError, match="schema_version"):
        from_envelope({**envelope, "schema_version": SCHEMA_VERSION + 1})
    with pytest.raises(ContractError, match="unknown contract kind"):
        from_envelope({**envelope, "kind": "Nope"})
    with pytest.raises(ContractError, match="exactly the keys"):
        from_envelope({"kind": "Entity", "data": {}})
