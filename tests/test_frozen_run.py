"""The one-command frozen test run: config validation, lock behaviour, end-to-end on synthetic data.

Every test that touches the test lock works in a temporary git repository with
its own audit log; the repository's real audit/test_access_log.jsonl is never
written (checked at the end of this file).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import builders as b
import dg_fixture
import fakes
import run_helpers as h
from binding_gate import frozen_run
from binding_gate.cli import main
from binding_gate.config import load_config
from binding_gate.contracts import (
    AnswerCandidate,
    ArmOutput,
    Attempt,
    CheckResult,
    CheckStatus,
    Decision,
    Split,
    Verdict,
)
from binding_gate.registry import create, register
from binding_gate.runner import RunnerSettings
from binding_gate.stats import prereg
from binding_gate.testlock import ACCESS_LOG_RELPATH, EVENT_LOADED, EVENT_REFUSED, TestSplitLocked, read_access_log
from binding_gate.trace import read_manifest, read_traces

REAL_REPO = Path(__file__).resolve().parents[1]
TAG = "prereg-freeze-1"
RESERVED_ARMS = {"no_gate", "prompt_entity_check", "faithfulness_checker", "llm_judge_checks", "gated", "gated_research"}
RESERVED_CHECKS = ["quote_verbatim", "entity_in_window", "qualifiers_in_window", "no_closer_competitor", "premise_present"]


@register("arm", "pr6_test_blocking_gate")
class BlockingGate:
    """Test stand-in for a gate: the no_gate candidate, blocked when it quotes the competing document."""

    name = "pr6_test_blocking_gate"

    def __init__(self, *, model):
        self.inner = create("arm", "no_gate", model=model)

    def run(self, arm_input):
        output = self.inner.run(arm_input)
        answer = output.final_answer
        if answer.status.value != "answered" or answer.source_id != "cy_doc":
            verdict = None if answer.status.value != "answered" else Verdict(
                Decision.ACCEPT, check_results=(CheckResult("quote_verbatim", CheckStatus.PASS),)
            )
            return ArmOutput((Attempt(answer, output.attempts[0].evidence, verdict),), answer, model_calls=output.model_calls)
        verdict = Verdict(
            Decision.ABSTAIN,
            reasons=("competing entity closer",),
            check_results=(CheckResult("no_closer_competitor", CheckStatus.FAIL),),
        )
        final = AnswerCandidate.abstained(raw=answer.raw, reason="blocked by the test gate")
        return ArmOutput((Attempt(answer, output.attempts[0].evidence, verdict),), final, model_calls=output.model_calls)


# -- the committed layer-1 test config ------------------------------------------------- #


def test_layer1_test_config_has_the_preregistered_shape():
    config = load_config(REAL_REPO / "configs" / "test_layer1.toml")
    assert config.split is Split.TEST and config.repeats == prereg.REPEATS and config.limit is None
    labels = [arm.label for arm in config.arms]
    assert labels[:5] == ["no_gate", "prompt_entity_check", "faithfulness_checker", "llm_judge_checks", "gated"]
    assert {arm.name for arm in config.arms} <= RESERVED_ARMS
    gated = {arm.label: arm for arm in config.arms if arm.name == "gated"}
    assert gated["gated"].options["checks"] == RESERVED_CHECKS
    for check in RESERVED_CHECKS:
        ablation = gated[f"gated-minus-{check}"]
        assert ablation.options["checks"] == [c for c in RESERVED_CHECKS if c != check]
    # same models, dataset pin and scorer as the dev baseline (PREREGISTRATION.md section 9)
    baseline = load_config(REAL_REPO / "configs" / "dev_baseline.toml")
    assert [m.label for m in config.models] == [m.label for m in baseline.models]
    assert [m.options["model_id"] for m in config.models] == [m.options["model_id"] for m in baseline.models]
    assert config.dataset == baseline.dataset and config.scorers == baseline.scorers
    settings = RunnerSettings.from_config(config)
    assert all(settings.cap_for(m.label) is not None for m in config.models)
    # passes every frozen-run config check that does not depend on git state
    reasons = frozen_run.config_refusals(config, REAL_REPO / "configs" / "test_layer1.toml", REAL_REPO, REAL_REPO / "runs")
    assert not [r for r in reasons if "tracked" not in r]


# -- a synthetic repository ------------------------------------------------------------ #


def frozen_repo(tmp_path: Path, *, status: str = "FROZEN", tag: bool = True, arms=None, repeats: int = 3):
    repo = h.make_repo(tmp_path, prereg_status=status)
    options = dg_fixture.write_checkout(tmp_path / "dg")
    raw = h.raw_config(
        options,
        split="test",
        repeats=repeats,
        arms=arms or [{"name": "no_gate"}, {"name": "pr6_test_blocking_gate", "label": "gated"}],
    )
    (repo / "configs" / "test_synth.toml").write_text(h.toml_text(raw), encoding="utf-8")
    (repo / ".gitignore").write_text("runs/\n", encoding="utf-8")
    b.git(repo, "add", "-A")
    b.git(repo, "commit", "-q", "-m", "freeze")
    if tag:
        b.git(repo, "tag", TAG)
    return repo


def run_frozen(repo: Path, **kwargs):
    kwargs.setdefault("tag", TAG)
    kwargs.setdefault("purpose", "unit test of the frozen run")
    kwargs.setdefault("confirm_spend", True)
    kwargs.setdefault("log", lambda message: None)
    kwargs.setdefault("figures", False)
    return frozen_run.frozen_test_run("configs/test_synth.toml", repo_root=repo, **kwargs)


def events(repo: Path) -> list[str]:
    return [entry["event"] for entry in read_access_log(repo / ACCESS_LOG_RELPATH)]


def test_refused_while_the_preregistration_is_a_draft(tmp_path):
    repo = frozen_repo(tmp_path, status="DRAFT")
    with pytest.raises(TestSplitLocked, match="not 'FROZEN'"):
        run_frozen(repo)
    assert events(repo) == [EVENT_REFUSED]
    assert fakes.CALLS == [] and not (repo / "runs").exists()


def test_refused_on_a_dirty_tree(tmp_path):
    repo = frozen_repo(tmp_path)
    (repo / "notes.txt").write_text("uncommitted", encoding="utf-8")
    with pytest.raises(TestSplitLocked, match="not clean"):
        run_frozen(repo)
    assert events(repo) == [EVENT_REFUSED] and fakes.CALLS == []


def test_refused_with_a_wrong_or_missing_tag(tmp_path):
    repo = frozen_repo(tmp_path)
    with pytest.raises(TestSplitLocked, match="starting with"):
        run_frozen(repo, tag="v1.0")
    with pytest.raises(TestSplitLocked, match="does not exist"):
        run_frozen(repo, tag="prereg-freeze-2")
    (repo / "later.txt").write_text("a later commit", encoding="utf-8")
    b.git(repo, "add", "-A")
    b.git(repo, "commit", "-q", "-m", "after the freeze")
    with pytest.raises(TestSplitLocked, match="is not the commit tagged"):
        run_frozen(repo)
    with pytest.raises(TestSplitLocked, match="purpose"):
        run_frozen(repo, purpose="  ")
    assert events(repo) == [EVENT_REFUSED] * 4 and fakes.CALLS == []


def test_config_problems_are_refused_before_the_lock_and_not_logged(tmp_path):
    repo = frozen_repo(tmp_path, repeats=2)
    with pytest.raises(frozen_run.FrozenRunError, match="repeats = 2"):
        run_frozen(repo)
    repo2 = frozen_repo(tmp_path / "second", arms=[{"name": "pr6_test_blocking_gate", "label": "gated"}])
    with pytest.raises(frozen_run.FrozenRunError, match="no arm labelled 'no_gate'"):
        run_frozen(repo2)
    (repo2 / "configs" / "untracked.toml").write_text((repo2 / "configs" / "test_synth.toml").read_text())
    with pytest.raises(frozen_run.FrozenRunError, match="not tracked"):
        frozen_run.frozen_test_run(
            "configs/untracked.toml", repo_root=repo2, tag=TAG, purpose="x", confirm_spend=True
        )
    repo3 = frozen_repo(tmp_path / "third")
    with pytest.raises(frozen_run.FrozenRunError, match="not git-ignored"):
        run_frozen(repo3, runs_dir="results")
    with pytest.raises(frozen_run.FrozenRunError, match="confirm-spend"):
        run_frozen(repo3, confirm_spend=False)
    for r in (repo, repo2, repo3):
        assert not (r / ACCESS_LOG_RELPATH).exists()
    dev = load_config(repo3 / "configs" / "test_synth.toml")
    assert frozen_run.config_refusals(
        type(dev)(**{**dev.__dict__, "split": Split.DEV}), repo3 / "configs" / "test_synth.toml", repo3, repo3 / "runs"
    )


def test_granted_when_every_condition_holds_runs_and_analyses(tmp_path):
    repo = frozen_repo(tmp_path)
    with pytest.raises(TestSplitLocked):
        run_frozen(repo, tag="prereg-freeze-9")  # a refused attempt first: both are logged
    result = run_frozen(repo)
    assert events(repo) == [EVENT_REFUSED, EVENT_LOADED]
    loaded = read_access_log(repo / ACCESS_LOG_RELPATH)[-1]
    assert loaded["confirm_tag"] == TAG and loaded["n_groups"] == 2

    manifest = read_manifest(result.run_dir)
    assert manifest.test_unlock_tag == TAG and manifest.ended_at is not None and not manifest.git_dirty
    records = list(read_traces(result.run_dir))
    assert {r.split for r in records} == {Split.TEST} and len(records) == 2 * 15 * 3 * 2 * 2

    analysis = json.loads((result.analysis_dir / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["split"] == "test" and analysis["layer"] == 1
    assert analysis["parameters"]["as_preregistered"] is True
    assert set(analysis["models"]) == {"good", "bad"}
    bad = analysis["models"]["bad"]
    assert bad["n_groups"] == 2 and bad["complete"]
    assert bad["estimates"]["arm/no_gate/m1"]["value"] == 1.0  # always quotes the competitor
    assert bad["estimates"]["arm/gated/m1"]["value"] is None  # the test gate blocked every answer
    assert bad["hypotheses"]["H1"]["status"] == "NOT SUPPORTED"
    assert bad["hypotheses"]["H3"]["status"] == "NOT EVALUABLE"
    report = (result.analysis_dir / "report.md").read_text(encoding="utf-8")
    assert "split `test`" in report and "## Model `bad`" in report

    lines = (result.run_dir / frozen_run.FROZEN_RUN_LOG).read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    assert record["tag"] == TAG and record["head_commit"] == b.git(repo, "rev-parse", "HEAD")
    assert record["access_log_after_load"]["last_event"] == EVENT_LOADED
    assert record["analysis"]["summary"]["H1"]["evaluated"] == ["bad", "good"]


def test_cli_frozen_test_run_and_analyze(tmp_path, capsys):
    repo = frozen_repo(tmp_path)
    args = ["frozen-test-run", "--config", "configs/test_synth.toml", "--repo-root", str(repo), "--tag", TAG,
            "--purpose", "cli test", "--no-figures"]
    assert main(args) == 1  # no --confirm-spend
    assert not (repo / ACCESS_LOG_RELPATH).exists()
    assert main([*args, "--tag", "prereg-freeze-7", "--confirm-spend"]) == 1  # locked: refused and logged
    assert events(repo) == [EVENT_REFUSED]
    assert main([*args, "--confirm-spend"]) == 0
    assert events(repo) == [EVENT_REFUSED, EVENT_LOADED]
    (run_dir,) = [p for p in (repo / "runs").iterdir() if p.is_dir()]
    assert (run_dir / "analysis" / "analysis.json").exists()
    capsys.readouterr()
    out = tmp_path / "reanalysis"
    assert main(["analyze", str(run_dir), "--out", str(out), "--no-figures"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["summary"]["H1"]["evaluated"] == ["bad", "good"]
    first = json.loads((run_dir / "analysis" / "analysis.json").read_text(encoding="utf-8"))
    second = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert first["models"] == second["models"]  # deterministic: same traces, same numbers


def test_the_real_repository_access_log_was_not_written():
    log = REAL_REPO / ACCESS_LOG_RELPATH
    assert not [e for e in read_access_log(log) if e.get("event") == EVENT_LOADED]
