"""Hand-built trace records with a chosen outcome, for the statistics tests."""

from __future__ import annotations

from binding_gate.contracts import (
    AnswerCandidate,
    Attempt,
    Decision,
    Split,
    TraceRecord,
    Verdict,
)
from binding_gate.scorers.provenance import OUTCOME_BY_LABEL

import builders as b

LABEL_BY_OUTCOME = {outcome: label for label, outcome in OUTCOME_BY_LABEL.items()}
CONFIG_HASH = "f" * 64


def _answered() -> AnswerCandidate:
    return AnswerCandidate.answered(
        value="10 mg", quote="the starting dose of Alphadrug for adults", source_id="doc-1", raw="{}"
    )


def record(
    outcome: str,
    *,
    model: str = "m",
    arm: str = "no_gate",
    item: str = "i1",
    group: str = "g1",
    repeat: int = 0,
    stratum: str | None = "complete/class_proximate",
    split: Split = Split.TEST,
    dataset: str = "deceptive_grounding",
    research_first: bool = False,
) -> TraceRecord:
    common = dict(
        run_id="run-1",
        config_hash=CONFIG_HASH,
        git_commit=b.COMMIT,
        item_id=item,
        group_id=group,
        dataset=dataset,
        split=split,
        arm=arm,
        model=model,
        repeat=repeat,
        started_at=b.T0,
        ended_at=b.T1,
        stratum=stratum,
    )
    if outcome == "error":
        return TraceRecord(**common, error="RuntimeError: boom")
    if outcome == "abstention":
        final = AnswerCandidate.abstained(raw="{}", reason="no evidence")
    elif outcome == "unparseable":
        final = AnswerCandidate.unparseable(raw="garbage", reason="no json")
    else:
        final = _answered()
    if research_first:
        first = Attempt(_answered(), verdict=Verdict(Decision.RESEARCH, reasons=("q",), research_request="again"))
        last = Attempt(final) if final.status.value != "answered" else Attempt(final, verdict=b.accept())
        attempts = (first, last)
    else:
        attempts = (Attempt(final),)
    label = LABEL_BY_OUTCOME[outcome]
    score = b.ScoreResult("provenance", label, {"outcome": outcome})
    return TraceRecord(**common, attempts=attempts, final_answer=final, scores=(score,))


def grid(spec: dict[str, list[str]], *, model: str = "m", items_per_group: int = 1, **kwargs) -> list[TraceRecord]:
    """Records from ``{arm: [outcome per unit]}``; unit k is item ``i<k>`` of group
    ``g<k // items_per_group>``, repeat 0."""
    records = []
    for arm, outcomes in spec.items():
        for k, outcome in enumerate(outcomes):
            records.append(
                record(outcome, model=model, arm=arm, item=f"i{k:03d}", group=f"g{k // items_per_group:03d}", **kwargs)
            )
    return records
