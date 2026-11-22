"""The PR 2 baseline report and the stop rule of PREREGISTRATION.md section 10."""

from __future__ import annotations

from pathlib import Path

import pytest

import builders as b
import run_helpers as h
from binding_gate import baseline_report as br
from binding_gate import runner
from binding_gate.contracts import (
    AnswerCandidate,
    Attempt,
    ModelCall,
    ModelRequest,
    ModelResponse,
    ScoreResult,
    Split,
    TokenUsage,
    TraceRecord,
)
from binding_gate.scorers.provenance import OUTCOME_BY_LABEL

PREREG = (Path(__file__).resolve().parents[1] / "PREREGISTRATION.md").read_text(encoding="utf-8")
CONDITIONS = [
    f"{cx}/{cy}"
    for cx in ("absent", "partial", "complete")
    for cy in ("null_control", "class_proximate", "context_adjacent", "prior_completing", "synthetic_y")
]


def record(model: str, group: int, stratum: str, label: str | None, repeat: int = 0, split=Split.DEV) -> TraceRecord:
    """A trace record whose provenance label is ``label`` (``None`` = the arm crashed)."""
    common = dict(
        run_id="run-1",
        config_hash="a" * 64,
        git_commit=b.COMMIT,
        item_id=f"g{group}/{stratum}",
        group_id=f"g{group}",
        dataset="deceptive_grounding",
        split=split,
        arm="no_gate",
        model=model,
        repeat=repeat,
        started_at=b.T0,
        ended_at=b.T1,
        stratum=stratum,
    )
    if label is None:
        return TraceRecord(**common, error="ModelCallFailed: HTTP 500")
    if label == "abstained":
        answer = AnswerCandidate.abstained(raw="{}")
    elif label == "unparseable":
        answer = AnswerCandidate.unparseable(raw="??", reason="no JSON")
    else:
        answer = AnswerCandidate.answered(value="v", quote="a b c d e f", source_id="cy_doc", raw="{}")
    call = ModelCall(
        purpose="answer",
        client=model,
        request=ModelRequest(b.model_call().request.messages),
        started_at=b.T0,
        ended_at=b.T1,
        response=ModelResponse("{}", f"{model}-served", TokenUsage(1000, 300), {"truncated": False, "reasoning_tokens": 200}),
    )
    details = {"outcome": OUTCOME_BY_LABEL[label]}
    if label in {"target_only", "competitor_only", "both"}:
        details.update(matched_document_ids=["cy_doc"], source_id_matches=label != "target_only")
    return TraceRecord(
        **common,
        attempts=(Attempt(answer),),
        final_answer=answer,
        model_calls=(call,),
        scores=(ScoreResult("provenance", label, details),),
    )


def model_records(model: str, wrong: int, *, groups: int = 10, other: str = "target_only", extra: dict | None = None):
    """``groups`` x 15 records; ``wrong`` of the 12 x groups non-null records are wrong-entity."""
    rows = []
    remaining = wrong
    extra = dict(extra or {})
    for group in range(groups):
        for stratum in CONDITIONS:
            label = other
            if not stratum.endswith("/null_control"):
                if remaining > 0:
                    label, remaining = "competitor_only", remaining - 1
                else:
                    for name, count in extra.items():
                        if count > 0:
                            label, extra[name] = name, count - 1
                            break
            rows.append(record(model, group, stratum, label))
    assert remaining == 0
    return rows


def test_r_is_wrong_entity_over_all_records_of_the_12_non_null_conditions():
    (model,) = br.build_report(model_records("m", 18))
    assert (model.n_groups, model.n_items, model.n_records, model.n_relevant) == (10, 150, 150, 120)
    assert model.wrong_entity == 18 and model.to_dict()["R"] == pytest.approx(0.15)
    assert model.outcomes_all["target_grounded"] == 150 - 18
    assert model.complete and model.decision == br.CONTINUE
    assert model.by_condition["absent/null_control"] == {"n": 10, "wrong_entity": 0}
    assert sum(c["n"] for c in model.by_condition.values()) == 150
    assert (model.paid_calls, model.input_tokens, model.output_tokens, model.reasoning_tokens) == (150, 150_000, 45_000, 30_000)
    assert model.served_models == ["m-served"]
    assert model.source_id_mismatches == 150 - 18  # the helper cites cy_doc for target-grounded answers


def test_wrong_entity_answers_in_null_control_do_not_count_towards_r():
    rows = [record("m", 0, s, "competitor_only" if s.endswith("/null_control") else "target_only") for s in CONDITIONS]
    (model,) = br.build_report(rows)
    assert model.wrong_entity == 0 and model.outcomes_all["wrong_entity"] == 3


@pytest.mark.parametrize(
    "wrong,decision",
    [(12, br.CONTINUE), (11, br.IN_BETWEEN), (6, br.IN_BETWEEN), (5, br.STOP), (0, br.STOP)],
)
def test_per_model_thresholds_are_exact_at_the_boundaries(wrong, decision):
    # 120 relevant records: 12 = exactly 10%, 6 = exactly 5%.
    (model,) = br.build_report(model_records("m", wrong))
    assert model.decision == decision


def test_errors_and_unparseable_records_stay_in_the_denominator():
    rows = model_records("m", 12)
    rows[1] = record("m", 0, CONDITIONS[1], None)
    rows[2] = record("m", 0, CONDITIONS[2], "unparseable")
    (model,) = br.build_report(rows)
    assert model.n_relevant == 120 and model.n_records == 150
    assert model.outcomes_relevant["error"] == 1 and model.outcomes_relevant["unparseable"] == 1
    assert model.failed_calls == 0 and model.paid_calls == 149


def test_measurement_check_triggers_above_20_percent_ungrounded_or_unparseable():
    # 150 records: 30 = exactly 20% -> not triggered; 31 -> triggered.
    (at_limit,) = br.build_report(model_records("m", 0, extra={"neither": 20, "unparseable": 10}))
    assert at_limit.unmeasured == 30 and not at_limit.measurement_check and at_limit.decision == br.STOP
    (over,) = br.build_report(model_records("m", 12, extra={"neither": 21, "unparseable": 10}))
    assert over.measurement_check and over.decision == br.MEASUREMENT_CHECK


def test_overall_outcome():
    def models(**spec):
        rows = []
        for name, (wrong, extra) in spec.items():
            rows += model_records(name, wrong, extra=extra)
        return br.build_report(rows)

    assert br.overall_decision(models(a=(12, {}), b=(0, {}))) == br.CONTINUE
    assert br.overall_decision(models(a=(5, {}), b=(0, {}))) == br.STOP
    assert br.overall_decision(models(a=(11, {}), b=(0, {}))) == br.IN_BETWEEN
    flagged = {"neither": 40}
    # A flagged model is not decided mechanically; without another model at 10% the owner decides.
    assert br.overall_decision(models(a=(0, flagged), b=(0, {}))) == br.MEASUREMENT_CHECK
    assert br.overall_decision(models(a=(12, flagged), b=(11, {}))) == br.MEASUREMENT_CHECK
    # A model that reaches 10% without a flag settles it.
    assert br.overall_decision(models(a=(0, flagged), b=(12, {}))) == br.CONTINUE
    with pytest.raises(br.ReportError):
        br.overall_decision([])


def test_incomplete_and_invalid_inputs():
    rows = model_records("m", 12)
    (model,) = br.build_report(rows + [record("m", 0, CONDITIONS[0], "target_only", repeat=1)])
    assert not model.complete and model.repeats == [0, 1]
    with pytest.raises(br.ReportError, match="duplicate"):
        br.build_report(rows + [rows[0]])
    with pytest.raises(br.ReportError, match="dev runs only"):
        br.build_report([record("m", 0, CONDITIONS[1], "target_only", split=Split.TEST)])
    with pytest.raises(br.ReportError, match="no record of arm"):
        br.build_report([])
    with pytest.raises(br.ReportError, match="null_control"):
        br.build_report([record("m", 0, "absent/null_control", "target_only")])


def test_report_from_real_run_directories_merges_per_model_runs(tmp_path):
    repo, config, _raw = h.setup(tmp_path)
    dirs = [
        runner.execute_run(
            config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, selection=label, log=lambda m: None
        ).run_dir
        for label in ("good", "bad")
    ]
    report = br.report_from_run_dirs(dirs)
    by_label = {m.model: m for m in report.models}
    # "bad" always quotes the competing document: R = 100%. "good" never does: R = 0%.
    assert by_label["bad"].wrong_entity == by_label["bad"].n_relevant == 3 * 12 * 2
    assert by_label["good"].wrong_entity == 0 and by_label["good"].outcomes_all["abstention"] == 3 * 5 * 2
    assert (by_label["bad"].decision, by_label["good"].decision) == (br.CONTINUE, br.STOP)
    assert report.overall == br.CONTINUE and not report.provisional
    text = report.format()
    for expected in ("OVERALL STOP-RULE OUTCOME: CONTINUE", "STOP-RULE OUTCOME for good: STOP", "72 / 72 = 100.0%", "measurement check"):
        assert expected in text
    out = tmp_path / "report.json"
    br.write_json(report, out)
    assert '"overall": "CONTINUE"' in out.read_text(encoding="utf-8")
    with pytest.raises(br.ReportError, match="duplicate"):
        br.report_from_run_dirs([dirs[0], dirs[0]])


def test_thresholds_match_the_preregistration_text():
    assert f"R ≥ {br.CONTINUE_PERCENT}%" in PREREG
    assert f"R < {br.CARRY_PERCENT}%" in PREREG
    assert f"more than {br.MEASUREMENT_CHECK_PERCENT}% of dev records" in PREREG


def test_top_errors_are_counted_truncated_and_scrubbed():
    from binding_gate.baseline_report import summarise_errors

    errors = ["ModelCallFailed: HTTP 400: Unsupported value 'none' for reasoning.effort"] * 3 + [
        "HTTP 401: key=SECRET123 Bearer abc.def sk-abcdefghijklmnop"
    ]
    top = summarise_errors(errors)
    assert top[0] == {"count": 3, "message": "ModelCallFailed: HTTP 400: Unsupported value 'none' for reasoning.effort"}
    assert "SECRET123" not in top[1]["message"] and "sk-abcdefghijklmnop" not in top[1]["message"]
    assert "abc.def" not in top[1]["message"]
    assert all(len(e["message"]) <= 300 for e in summarise_errors(["x" * 1000]))
