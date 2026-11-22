"""The separate decomposer measurement: config, sample, matching rules, metrics, dry run and paid run (fakes only)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import decomposer_fakes
import dg_fixture
import run_helpers
from binding_gate import registry
from binding_gate.contracts import (
    Entity,
    Presupposition,
    Qualifier,
    QualifierKind,
    QuestionConstraints,
    RunManifest,
    Split,
)
from binding_gate.datasets.deceptive_grounding import DATASET_NAME
from binding_gate.decomposers import cache as cache_module
from binding_gate.evaluation import decomposer as ev
from binding_gate.split import assign_split
from binding_gate.testlock import ACCESS_LOG_RELPATH

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "configs" / "decomposer_eval_dev.toml"
REAL_CHECKOUT = REPO_ROOT / "data" / "raw" / "deceptive-grounding-benchmark"


@pytest.fixture(autouse=True)
def _clean():
    cache_module.clear_shared_caches()
    decomposer_fakes.CALLS.clear()
    yield
    cache_module.clear_shared_caches()
    decomposer_fakes.CALLS.clear()


def raw_config(dataset_options: dict, *, behaviour: str = "reference", size: int = 3, repeats: int = 2,
               max_usd: float | None = 1.0, model_options: dict | None = None) -> dict:
    runner: dict = {"prices": "configs/prices.toml"}
    if max_usd is not None:
        runner["max_usd"] = max_usd
    return {
        "eval_version": 1,
        "name": "eval-test",
        "split": "dev",
        "repeats": repeats,
        "sample": {"size": size, "seed": 7},
        "dataset": {"name": "deceptive_grounding", "options": dataset_options},
        "reference": {"name": "deceptive_grounding"},
        "decomposer": {
            "name": "model_decomposer",
            "options": {"model": {"name": "decomposer_fake", "label": "decomposer",
                                  "options": {"model_id": "fake-cheap", "behaviour": behaviour, **(model_options or {})}}},
        },
        "runner": runner,
        "thresholds": {
            "entity_accuracy_min": 0.95, "failure_rate_max": 0.02, "competitor_collisions_max": 0,
            "qualifier_recall_min": 0.9, "qualifier_precision_min": 0.8, "presupposition_agreement_min": 0.8,
        },
    }  # fmt: skip


@pytest.fixture
def env(tmp_path):
    """``(repo, dataset options)``: a git repo with test prices and a 3-dev / 2-test synthetic checkout."""
    repo = run_helpers.make_repo(tmp_path)
    options = dg_fixture.write_checkout(tmp_path / "dg", n_dev=3, n_test=2)
    return repo, options


def config_of(options: dict, **kwargs) -> ev.EvalConfig:
    return ev.parse_eval_config(raw_config(options, **kwargs))


# --- config ------------------------------------------------------------------------------------------ #


def test_committed_config_loads_and_matches_the_preregistration():
    config = ev.load_eval_config(CONFIG_PATH)
    assert config.split == "dev" and config.reference == "deceptive_grounding"
    model = config.decomposer.options["model"]
    assert model["name"] == "openai_responses" and model["options"]["model_id"] == "gpt-6-luna"
    prereg = (REPO_ROOT / "PREREGISTRATION.md").read_text(encoding="utf-8")
    section = prereg.split("**Decomposer (A), measured on its own (PR 3)**", 1)[1].split("## 8.", 1)[0]

    def declared(name: str) -> int:
        match = re.search(rf"^{name}\s*=\s*(\d+)\s*$", section, re.MULTILINE)
        assert match, name
        return int(match.group(1))

    assert declared("DECOMPOSER_EVAL_SAMPLE_SIZE") == config.sample_size
    assert declared("DECOMPOSER_EVAL_SEED") == config.sample_seed
    assert declared("DECOMPOSER_EVAL_REPEATS") == config.repeats
    t = config.thresholds
    rows = {
        "Entity accuracy, all records": f"≥ {t.entity_accuracy_min:.0%}",
        "Failure rate (unparseable + failed calls), all records": f"≤ {t.failure_rate_max:.0%}",
        "Competitor collisions": f"{t.competitor_collisions_max}",
        "Qualifier recall, exact": f"≥ {t.qualifier_recall_min:.0%}",
        "Qualifier precision, exact": f"≥ {t.qualifier_precision_min:.0%}",
        "Presupposition agreement, exact": f"≥ {t.presupposition_agreement_min:.0%}",
    }
    for label, value in rows.items():
        assert f"| {label} | {value} |" in section, label
    from binding_gate.decomposers.decomposition_format import prompt_sha256

    assert prompt_sha256() in section
    # Slot "Dec" of section 9 is the model in the config.
    assert "| Dec | `gpt-6-luna` |" in prereg


def test_config_validation(env):
    _repo, options = env
    good = raw_config(options)
    assert ev.parse_eval_config(good).config_hash == ev.parse_eval_config(good).config_hash
    for change, message in [
        ({"split": "test"}, "split must be 'dev'"),
        ({"eval_version": 2}, "eval_version"),
        ({"extra": 1}, "unknown top-level"),
        ({"repeats": 0}, "repeats"),
        ({"reference": {"name": "grab_rag"}}, "must be one of"),
        ({"sample": {"size": 3}}, "missing key"),
        ({"runner": {"max_usd": -1}}, "max_usd"),
        ({"thresholds": {**good["thresholds"], "entity_accuracy_min": 1.5}}, "between 0 and 1"),
        ({"thresholds": {"entity_accuracy_min": 0.9}}, "missing key"),
    ]:
        with pytest.raises(ev.EvalError, match=re.escape(message)):
            ev.parse_eval_config({**good, **change})
    assert config_of(options, size=2).config_hash != config_of(options, size=3).config_hash


# --- units and the sample ------------------------------------------------------------------------- #


def test_units_are_distinct_dev_questions_with_their_reference(env):
    _repo, options = env
    sample, fingerprint, available = ev.load_units(config_of(options))
    assert available == 3 and len(sample) == 3 and len(fingerprint) == 64
    for unit in sample:
        assert unit.n_items == 15
        assert all(assign_split(DATASET_NAME, g) is Split.DEV for g in unit.group_ids)
        assert unit.reference.entity.name in unit.question
        assert len(unit.forbidden) == 2


def test_sample_rule_is_deterministic_and_independent_of_order(env):
    _repo, options = env
    units, _fp, _n = ev.load_units(config_of(options))
    two = ev.select_sample(units, 2, 7)
    assert ev.select_sample(list(reversed(units)), 2, 7) == two
    ranked = sorted(units, key=lambda u: ev.sample_rank(7, u.question))
    assert {u.question for u in two} == {u.question for u in ranked[:2]}
    assert two == sorted(two, key=lambda u: u.question)
    with pytest.raises(ev.EvalError, match="larger than"):
        ev.select_sample(units, 4, 7)


def test_triples_sharing_a_question_form_one_unit(tmp_path):
    triples = [dg_fixture.triple(i) for i in range(12)]
    dev = next(t for t in triples if assign_split(DATASET_NAME, f"{t['X']} || {t['C']}") is Split.DEV)
    index = int(dev["X"].removeprefix("xanadrug"))
    options = dg_fixture.write_checkout(tmp_path / "dg", triples=[dev, dg_fixture.triple(index, rival=1)])
    units, _fp, available = ev.load_units(config_of(options, size=1))
    assert available == 1 and units[0].n_items == 30
    assert len(units[0].forbidden) == 4  # Y and Y_synthetic of both triples


# --- matching rules ---------------------------------------------------------------------------------- #

REFERENCE = QuestionConstraints(
    question="What does the literature say about using drugx to treat disease c?",
    entity=Entity("drugx", (), "drug"),
    attribute="ref",
    qualifiers=(Qualifier(QualifierKind.OTHER, "disease c"),),
    presuppositions=(Presupposition("drugx is used to treat disease c.", ("drugx", "disease c")),),
)
FORBIDDEN = ("drugy", "drugy-syn")


def predicted(*, name="drugx", aliases=(), qualifiers=(("other", "disease c", ()),), key_terms=(("drugx", "disease c"),)):
    return QuestionConstraints(
        question=REFERENCE.question,
        entity=Entity(name, tuple(aliases)),
        attribute="anything",
        qualifiers=tuple(Qualifier(k, t, tuple(a)) for k, t, a in qualifiers),
        presuppositions=tuple(Presupposition("p", tuple(terms)) for terms in key_terms),
    )


def test_exact_match_after_normalisation():
    c = ev.compare(predicted(name="DrugX ", qualifiers=(("place", "Disease  C", ()),),
                             key_terms=(("DRUGX", "disease c"),)), REFERENCE, FORBIDDEN)
    assert c["entity"] == {"correct": True, "name_exact": True, "matched_via": "name",
                           "competitor_collisions": [], "n_aliases": 0}
    assert c["qualifiers"]["reference_matched_exact"] == 1 and c["qualifiers"]["predicted_matched_exact"] == 1
    assert c["qualifiers"]["kind_agree"] == 0 and c["qualifiers"]["matched_kinds"] == ["place"]
    assert c["presuppositions"]["agree_exact"] is True
    assert ev.consequences("parsed", c) == []


def test_alias_handling_and_competitor_collisions():
    via_alias = ev.compare(predicted(name="Brandix", aliases=["drugx"], key_terms=(("Brandix", "disease c"),)),
                           REFERENCE, FORBIDDEN)
    assert via_alias["entity"]["correct"] and via_alias["entity"]["matched_via"] == "alias"
    assert not via_alias["entity"]["name_exact"]
    assert via_alias["presuppositions"]["agree_exact"]  # a form of the correct entity counts as the key term

    collision = ev.compare(predicted(aliases=["DrugY"]), REFERENCE, FORBIDDEN)
    assert collision["entity"]["correct"] is False and collision["entity"]["competitor_collisions"] == ["drugy"]
    assert ev.consequences("parsed", collision) == ["entity_form_is_competitor"]

    wrong = ev.compare(predicted(name="disease c"), REFERENCE, FORBIDDEN)
    assert wrong["entity"]["matched_via"] is None
    assert "entity_wrong" in ev.consequences("parsed", wrong)


def test_qualifier_recall_precision_and_lenient_matching():
    split = ev.compare(predicted(qualifiers=(("other", "disease", ()), ("other", "the literature", ()))),
                       REFERENCE, FORBIDDEN)
    q = split["qualifiers"]
    assert (q["reference"], q["predicted"]) == (1, 2)
    assert (q["reference_matched_exact"], q["predicted_matched_exact"]) == (0, 0)
    assert (q["reference_matched_lenient"], q["predicted_matched_lenient"]) == (1, 1)
    assert ev.consequences("parsed", split) == ["qualifier_missed", "qualifier_extra"]

    alias = ev.compare(predicted(qualifiers=(("other", "DC", ("disease c",)),)), REFERENCE, FORBIDDEN)
    assert alias["qualifiers"]["reference_matched_exact"] == 1
    assert alias["presuppositions"]["agree_exact"]
    assert ev.lenient_equal("disease c", "chronic disease c") and not ev.lenient_equal("disease", "diseases")


def test_presupposition_agreement_needs_every_reference_key_term():
    missing = ev.compare(predicted(key_terms=(("drugx",), ("disease c",))), REFERENCE, FORBIDDEN)
    assert missing["presuppositions"]["agree_exact"] is False  # the terms are split over two premises
    lenient = ev.compare(predicted(key_terms=(("drugx", "treat disease c"),)), REFERENCE, FORBIDDEN)
    assert lenient["presuppositions"]["agree_exact"] is False
    assert lenient["presuppositions"]["agree_lenient"] is True
    none = ev.compare(predicted(key_terms=()), REFERENCE, FORBIDDEN)
    assert ev.consequences("parsed", none) == ["presupposition_missed"]


def test_a_failed_decomposition_matches_nothing():
    c = ev.compare(None, REFERENCE, FORBIDDEN)
    assert c["parsed"] is False and c["entity"]["correct"] is False
    assert c["qualifiers"]["reference"] == 1 and c["qualifiers"]["reference_matched_exact"] == 0
    assert ev.consequences("unparseable", c) == ["decomposition_unparseable"]
    assert ev.consequences("call_failed", c) == ["decomposition_call_failed"]


# --- metrics ----------------------------------------------------------------------------------------- #


def _unit(question_id: str, n_items: int = 15) -> ev.QuestionUnit:
    return ev.QuestionUnit(REFERENCE.question, question_id, ("g",), n_items, REFERENCE, FORBIDDEN)


def _record(question_id: str, repeat: int, constraints, status: str = "parsed") -> dict:
    return ev.make_record(_unit(question_id), repeat, status=status, constraints=constraints, reason=None,
                          raw=None, model_calls=(), cost_usd=0.0)


def test_metrics_use_all_records_denominators_and_count_consequences():
    records = [
        _record("q1", 0, predicted()),
        _record("q1", 1, predicted()),
        _record("q2", 0, predicted(name="other")),
        _record("q2", 1, None, status="unparseable"),
    ]
    m = ev.compute_metrics(records, repeats=2)
    assert (m["n_records"], m["n_questions"]) == (4, 2)
    assert m["entity_accuracy"] == 0.5 and m["entity_accuracy_among_parsed"] == pytest.approx(2 / 3)
    assert m["parse_failure_rate"] == 0.25 and m["failure_rate"] == 0.25 and m["call_failure_rate"] == 0
    assert m["qualifier_recall_exact"] == 0.75  # the failed record's reference qualifier is missed
    assert m["qualifier_precision_exact"] == 1.0
    # The wrong-entity record still names X and C as key terms, so it agrees; the failed one does not.
    assert m["presupposition_agreement_exact"] == 0.75
    assert m["stability"]["questions_with_identical_constraints_across_repeats"] == 0.5
    assert m["stability"]["questions_with_consistent_entity_correctness"] == 1.0  # q2 is wrong both times
    assert m["consequences"]["entity_wrong"]["records"] == 1
    assert m["consequences"]["entity_wrong"]["expected_items_affected_per_run"] == 7.5
    assert m["consequences"]["decomposition_unparseable"]["records"] == 1
    assert m["records_consistent_with_reference"] == 2

    checks = ev.evaluate_thresholds(m, ev.load_eval_config(CONFIG_PATH).thresholds)
    assert checks["all_passed"] is False
    assert {c["metric"]: c["passed"] for c in checks["checks"]}["qualifier_precision_exact"] is True


def test_threshold_with_no_value_does_not_pass():
    m = ev.compute_metrics([_record("q", 0, predicted(qualifiers=()))], repeats=1)
    assert m["qualifier_precision_exact"] is None
    checks = {c["metric"]: c for c in ev.evaluate_thresholds(m, ev.load_eval_config(CONFIG_PATH).thresholds)["checks"]}
    assert checks["qualifier_precision_exact"]["passed"] is False


# --- dry run ------------------------------------------------------------------------------------------- #


def test_dry_run_counts_calls_and_costs_without_calling_anything(env):
    repo, options = env
    config = config_of(options, size=3, repeats=2)
    plan = ev.estimate(config, repo_root=repo)
    assert decomposer_fakes.CALLS == []
    assert (plan.calls, plan.sample_size, plan.dev_questions_available) == (6, 3, 3)
    assert plan.priced and plan.model_id == "fake-cheap" and plan.cap == 1.0
    assert plan.cost_low < plan.cost_high < plan.cost_ceiling
    expected_low = (plan.input_tokens * 1.0 + 6 * 100 * 2.0) / 1_000_000  # test prices: 1/2 USD, 100 visible tokens
    assert plan.cost_low == pytest.approx(expected_low)
    assert "DRY RUN" in plan.format() and len(plan.sample_question_ids) == 3
    assert not (repo / "runs").exists()


def test_cli_dry_run_and_refusals(env, capsys, tmp_path):
    repo, options = env
    path = tmp_path / "eval.toml"
    path.write_text(json_to_toml(raw_config(options)), encoding="utf-8")
    out_json = tmp_path / "plan.json"
    assert ev.main(["dry-run", "--config", str(path), "--repo-root", str(repo), "--json", str(out_json)]) == 0
    assert json.loads(out_json.read_text())["calls"] == 6
    assert ev.main(["run", "--config", str(path), "--repo-root", str(repo)]) == ev.EXIT_FAILURE
    assert "REFUSED" in capsys.readouterr().err
    bad = tmp_path / "bad.toml"
    bad.write_text(json_to_toml({**raw_config(options), "split": "test"}), encoding="utf-8")
    assert ev.main(["dry-run", "--config", str(bad), "--repo-root", str(repo)]) == ev.EXIT_FAILURE
    assert decomposer_fakes.CALLS == []


def test_cli_run_without_the_api_key_stops_before_anything(env, tmp_path, monkeypatch, capsys):
    repo, options = env
    raw = raw_config(options)
    raw["decomposer"]["options"]["model"] = {"name": "openai_responses", "label": "decomposer",
                                             "options": {"model_id": "fake-cheap", "max_output_tokens": 100}}
    path = tmp_path / "eval.toml"
    path.write_text(json_to_toml(raw), encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = ev.main(["run", "--config", str(path), "--repo-root", str(repo), "--confirm-spend",
                    "--runs-dir", str(tmp_path / "runs")])
    assert code == ev.EXIT_NO_KEY and "OPENAI_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


# --- paid run (with the fake model) -------------------------------------------------------------------- #


def test_paid_run_safeguards(env, tmp_path):
    repo, options = env
    runs = tmp_path / "runs"
    with pytest.raises(ev.EvalError, match="--confirm-spend"):
        ev.run_evaluation(config_of(options), repo_root=repo, runs_dir=runs, confirm_spend=False)
    with pytest.raises(ev.EvalError, match="no budget cap"):
        ev.run_evaluation(config_of(options, max_usd=None), repo_root=repo, runs_dir=runs, confirm_spend=True)
    with pytest.raises(ev.EvalError, match="no price"):
        ev.run_evaluation(config_of(options, model_options={"model_id": "unpriced"}), repo_root=repo,
                          runs_dir=runs, confirm_spend=True)
    with pytest.raises(ev.EvalError, match="low cost estimate"):
        ev.run_evaluation(config_of(options, max_usd=0.000001), repo_root=repo, runs_dir=runs, confirm_spend=True)
    with pytest.raises(ev.EvalError, match="preflight failed"):
        ev.run_evaluation(config_of(options, model_options={"preflight_ok": False}), repo_root=repo,
                          runs_dir=runs, confirm_spend=True, log=lambda _m: None)
    raw = raw_config(options)
    raw["decomposer"]["options"]["cache"] = "process"
    with pytest.raises(ev.EvalError, match="must not set 'cache'"):
        ev.run_evaluation(ev.parse_eval_config(raw), repo_root=repo, runs_dir=runs, confirm_spend=True)
    assert decomposer_fakes.CALLS == [] and not runs.exists()


def test_paid_run_writes_records_report_and_manifest(env, tmp_path):
    repo, options = env
    config = config_of(options, size=3, repeats=2)
    summary = ev.run_evaluation(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True,
                                config_path="configs/x.toml", log=lambda _m: None)
    assert len(decomposer_fakes.CALLS) == 6  # 3 questions x 2 independent repeats: no cache
    assert summary.records == 6 and not summary.aborted
    run_dir = summary.run_dir
    assert run_dir.name.startswith("decomposer-eval-")
    manifest = RunManifest.from_json((run_dir / ev.MANIFEST_NAME).read_text())
    records_bytes = (run_dir / ev.RECORDS_NAME).read_bytes()
    assert manifest.trace_count == 6 and manifest.ended_at is not None
    assert manifest.config == config.to_dict() and manifest.config_path == "configs/x.toml"
    import hashlib

    assert manifest.traces_sha256 == hashlib.sha256(records_bytes).hexdigest()
    records = ev.read_records(run_dir)
    assert [r["repeat"] for r in records] == [0, 0, 0, 1, 1, 1]
    first = records[0]
    assert first["status"] == "parsed" and first["consequences"] == []
    assert first["model_calls"][0]["purpose"] == "decompose"
    assert first["cost_usd"] == pytest.approx((800 * 1.0 + 200 * 2.0) / 1_000_000)
    report = json.loads((run_dir / ev.REPORT_NAME).read_text())
    assert report["complete"] and report["verdict"] == "MEETS PROPOSED THRESHOLDS"
    assert report["metrics"]["entity_accuracy"] == 1.0
    assert report["metrics"]["stability"]["questions_with_identical_constraints_across_repeats"] == 1.0
    assert report["spend_usd"] == pytest.approx(6 * 0.0012)
    assert report["decomposer"]["cache"] == "none"
    assert ev.report_from_run_dir(run_dir)["metrics"] == report["metrics"]
    assert "MEETS" in ev.format_report(report)
    assert (repo / ACCESS_LOG_RELPATH).exists() is False or (repo / ACCESS_LOG_RELPATH).read_bytes() == b""


@pytest.mark.parametrize(
    ("behaviour", "metric", "value", "consequence"),
    [
        ("garbage", "parse_failure_rate", 1.0, "decomposition_unparseable"),
        ("crash", "call_failure_rate", 1.0, "decomposition_call_failed"),
        ("wrong_entity", "entity_accuracy", 0.0, "entity_wrong"),
        ("competitor_alias", "competitor_collisions", 6, "entity_form_is_competitor"),
        ("extra_qualifier", "qualifier_precision_exact", 0.5, "qualifier_extra"),
    ],
)
def test_paid_run_records_each_kind_of_error(env, tmp_path, behaviour, metric, value, consequence):
    repo, options = env
    summary = ev.run_evaluation(config_of(options, behaviour=behaviour), repo_root=repo,
                                runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda _m: None)
    report = summary.report
    assert report["metrics"][metric] == value
    assert report["metrics"]["consequences"][consequence]["records"] == 6
    assert report["metrics"]["consequences"][consequence]["expected_items_affected_per_run"] == 45
    assert report["verdict"] == "BELOW PROPOSED THRESHOLDS"
    records = ev.read_records(summary.run_dir)
    if behaviour == "garbage":
        assert records[0]["raw"] == "The entity is probably the drug." and records[0]["reason"]
        assert records[0]["constraints"] is None and len(records[0]["model_calls"]) == 1
    if behaviour == "crash":
        assert records[0]["cost_usd"] == 0 and records[0]["model_calls"][0]["error"]


def test_budget_cap_stops_the_run_and_leaves_an_incomplete_report(env, tmp_path):
    repo, options = env
    # Reported usage: 3,500 input tokens at 1 USD per million = 0.0035 USD per call. The low estimate
    # (about 0.0056 USD) fits under the cap, but reported usage reaches it after two calls.
    config = config_of(options, size=3, repeats=2, max_usd=0.006,
                       model_options={"input_tokens": 3500, "output_tokens": 0})
    messages: list[str] = []
    summary = ev.run_evaluation(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True,
                                log=messages.append)
    assert summary.aborted and summary.records == 2 and len(decomposer_fakes.CALLS) == 2
    assert summary.report["verdict"] == "INCOMPLETE" and summary.report["aborted_by_budget_cap"]
    assert any("BUDGET CAP REACHED" in m for m in messages)
    assert ev.report_from_run_dir(summary.run_dir)["complete"] is False


def test_cli_run_and_report(env, tmp_path, capsys):
    repo, options = env
    path = tmp_path / "eval.toml"
    path.write_text(json_to_toml(raw_config(options)), encoding="utf-8")
    runs = tmp_path / "runs"
    assert ev.main(["run", "--config", str(path), "--repo-root", str(repo), "--confirm-spend",
                    "--runs-dir", str(runs)]) == 0
    (run_dir,) = runs.iterdir()
    out = tmp_path / "report.json"
    assert ev.main(["report", str(run_dir), "--json", str(out)]) == 0
    assert json.loads(out.read_text())["verdict"] == "MEETS PROPOSED THRESHOLDS"
    assert "entity accuracy" in capsys.readouterr().out


# --- the real benchmark (local only; it is never committed) -------------------------------------------- #


@pytest.mark.skipif(not REAL_CHECKOUT.is_dir(), reason="benchmark not cloned into data/raw/ (it is never committed)")
def test_dry_run_of_the_committed_config_on_the_real_checkout():
    config = ev.load_eval_config(CONFIG_PATH)
    plan = ev.estimate(config, repo_root=REPO_ROOT)
    assert (plan.dev_questions_available, plan.sample_size, plan.calls) == (45, 45, 135)
    assert plan.cost_ceiling is not None and plan.cost_ceiling < config.max_usd
    units, _fp, _n = ev.load_units(config)
    assert sum(u.n_items for u in units) == 1455  # every dev stimulus belongs to one sampled question
    assert all(u.reference.entity.name not in u.forbidden for u in units)
    assert decomposer_fakes.CALLS == []


# --- helpers ------------------------------------------------------------------------------------------- #


def json_to_toml(raw: dict) -> str:
    """Enough TOML for the evaluation config shapes used here (nested tables, scalars)."""

    def value(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, str):
            return json.dumps(v)
        raise TypeError(v)

    lines: list[str] = []

    def table(prefix: str, data: dict) -> None:
        lines.append(f"[{prefix}]")
        lines.extend(f"{k} = {value(v)}" for k, v in data.items() if not isinstance(v, dict))
        for k, v in data.items():
            if isinstance(v, dict):
                table(f"{prefix}.{k}", v)

    lines.extend(f"{k} = {value(v)}" for k, v in raw.items() if not isinstance(v, dict))
    for k, v in raw.items():
        if isinstance(v, dict):
            table(k, v)
    return "\n".join(lines) + "\n"
