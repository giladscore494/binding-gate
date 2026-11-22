"""PR 17: the frozen test-run workflow (.github/workflows/frozen_test.yml) and its helpers (binding_gate.frozen_ci).

Wiring and refusal conditions are checked statically; the access-log handling, the lock in a tag checkout
with main's log, the audit branch and the results staging run on temporary git repositories (a local bare
repository stands in for GitHub). The repository's real audit/test_access_log.jsonl is never written.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

import builders as b
from binding_gate import frozen_ci, results_log, testlock
from binding_gate.config import load_config
from binding_gate.secrets import KNOWN_SECRETS
from binding_gate.stats import prereg
from test_frozen_run import TAG, frozen_repo, run_frozen

REAL_REPO = Path(__file__).resolve().parents[1]
PATH = REAL_REPO / ".github" / "workflows" / "frozen_test.yml"
TEXT = PATH.read_text(encoding="utf-8")
DOC = yaml.safe_load(TEXT)
JOBS = DOC["jobs"]


def steps(job: str) -> dict[str, dict]:
    return {s.get("name") or s.get("uses"): s for s in JOBS[job]["steps"]}


# --------------------------------------------------------------------------- #
# Inputs and their refusals (nothing run, nothing logged)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tag", ["v1", "prereg-freeze-", "prereg-freeze-1; rm -rf /", "prereg-freeze-1 x", "Prereg-freeze-1",
                                 "prereg-freeze-$(id)", "refs/tags/prereg-freeze-1", "prereg-freeze-" + "a" * 70, ""])
def test_a_wrong_tag_pattern_is_refused(tag):
    reasons = frozen_ci.validate("layer1", tag, "true")
    assert len(reasons) == 1 and "freeze tag" in reasons[0]


@pytest.mark.parametrize("confirm", ["false", "", "False", "no", False])
def test_a_missing_confirmation_is_refused(confirm):
    reasons = frozen_ci.validate("layer2_sec", "prereg-freeze-1", confirm)
    assert len(reasons) == 1 and "confirm_spend" in reasons[0]


def test_valid_inputs_and_the_cli_exit_codes(capsys):
    assert frozen_ci.validate("layer3_live", "prereg-freeze-layer3.2", "true") == []
    assert frozen_ci.validate("layer9", "prereg-freeze-1", "true")[0].startswith("layer must be one of")
    assert frozen_ci.main(["validate", "--layer", "layer2_sec", "--tag", "prereg-freeze-1", "--confirm", "true"]) == 0
    out = capsys.readouterr().out
    assert "config=configs/test_sec.toml" in out and "per_model_jobs=false" in out
    assert frozen_ci.main(["validate", "--layer", "layer1", "--tag", "v1", "--confirm", "false"]) == frozen_ci.EXIT_REFUSED
    out = capsys.readouterr().out
    assert out.count("::error::REFUSED") == 2 and "config=" not in out and "nothing was logged" in out


def test_layers_map_to_the_preregistered_frozen_configs():
    assert list(frozen_ci.LAYER_CONFIGS) == ["layer1", "layer1_midtier", "layer2_sec", "layer3_live"]
    section12 = (REAL_REPO / "PREREGISTRATION.md").read_text(encoding="utf-8").split("## 12.", 1)[1].split("## 13.", 1)[0]
    expected_layer = {"layer1": 1, "layer1_midtier": 1, "layer2_sec": 2, "layer3_live": 3}
    for layer, config in frozen_ci.LAYER_CONFIGS.items():
        assert f"`{config}`" in section12, config
        loaded = load_config(REAL_REPO / config)
        assert loaded.split.value == "test" and prereg.LAYER_BY_DATASET[loaded.dataset.name] == expected_layer[layer]
    assert frozen_ci.PER_MODEL_JOBS is False  # section 17 item 18 is still open


def test_section_17_item_18_is_still_open_so_all_models_run_in_one_job():
    prereg_text = (REAL_REPO / "PREREGISTRATION.md").read_text(encoding="utf-8")
    item = prereg_text.split("18. One test-split access for all models together", 1)[1][:300]
    assert "6 hours" in item
    assert "strategy" not in JOBS["run"]  # no per-model matrix
    frozen = steps("run")["Frozen test run (paid; one logged test-split access)"]
    assert "--model" not in frozen["run"]
    assert frozen["timeout-minutes"] == 330 < JOBS["run"]["timeout-minutes"] == 350  # later steps still upload


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def test_started_by_hand_with_the_documented_inputs():
    on = DOC.get("on", DOC.get(True))
    assert set(on) == {"workflow_dispatch"}
    inputs = on["workflow_dispatch"]["inputs"]
    assert list(inputs) == ["layer", "tag", "confirm_spend"]
    assert inputs["layer"]["type"] == "choice" and inputs["layer"]["options"] == list(frozen_ci.LAYER_CONFIGS)
    assert inputs["tag"]["type"] == "string" and "default" not in inputs["tag"]
    assert inputs["confirm_spend"]["type"] == "boolean" and inputs["confirm_spend"]["default"] is False
    assert DOC["permissions"] == {"contents": "read", "actions": "read"}
    assert DOC["concurrency"] == {"group": "frozen-test-run", "cancel-in-progress": False}
    assert DOC["env"]["TAG"] == "${{ inputs.tag }}" and DOC["env"]["LAYER"] == "${{ inputs.layer }}"
    for job in JOBS.values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step.get("name")  # inputs reach shells only as variables


def test_the_plan_refuses_before_anything_else_and_every_later_job_needs_it():
    plan = list(steps("plan"))
    validate = steps("plan")["Refuse unless the layer, the freeze-tag pattern and confirm_spend are right"]
    assert "binding_gate.frozen_ci validate --layer \"$LAYER\" --tag \"$TAG\" --confirm \"$CONFIRM\"" in validate["run"]
    assert validate["env"] == {"CONFIRM": "${{ inputs.confirm_spend }}"}
    assert plan.index(validate["name"]) < plan.index("Refuse unless the tag exists") < \
        plan.index("Refuse while the access-log pull request of an earlier frozen run is not merged")
    assert "frozen_ci pending-audit" in steps("plan")["Refuse while the access-log pull request of an earlier frozen run is not merged"]["run"]
    assert JOBS["ledger"]["needs"] == "plan" and JOBS["run"]["needs"] == ["plan", "ledger"]
    assert "secrets." not in yaml.safe_dump(JOBS["plan"]) and "secrets." not in yaml.safe_dump(JOBS["ledger"])


def test_the_ledger_checks_the_layer_config_at_the_tag_first():
    (check,) = [s for s in JOBS["ledger"]["steps"] if s.get("uses") == "./.github/actions/ledger-check"]
    assert check["with"]["mode"] == "check" and check["with"]["config"] == "${{ needs.plan.outputs.config }}"
    assert check["with"]["model"] == "all" and check["with"]["github-token"] == "${{ github.token }}"
    checkout = JOBS["ledger"]["steps"][0]
    assert checkout["uses"] == "actions/checkout@v4" and checkout["with"]["ref"] == "refs/tags/${{ inputs.tag }}"
    assert not any(s.get("uses") == "./.github/actions/ledger-check" for s in JOBS["run"]["steps"])  # it would dirty the tree


def test_the_run_job_checks_out_exactly_the_tag_and_satisfies_the_lock():
    run = steps("run")
    names = list(run)
    checkout = JOBS["run"]["steps"][0]
    assert checkout["with"] == {"ref": "refs/tags/${{ inputs.tag }}", "fetch-depth": 0}
    frozen = run["Frozen test run (paid; one logged test-split access)"]
    command = frozen["run"]
    assert 'python -m binding_gate.cli frozen-test-run --config "$CONFIG" --tag "$TAG"' in command
    assert "--confirm-spend" in command and "--purpose" in command and "--runs-dir runs" in command
    order = ["Bring the access log up to date with main (new entries chain onto main's last entry)",
             "Get the dataset at its pinned version (into data/raw/, git-ignored)",
             "Prepare and verify the faithfulness checker (pinned MiniCheck weights; no model API)",
             "The working tree is clean apart from the access log (the lock's condition; read-only, not an access)",
             frozen["name"], "Keep the access log for the pull request"]
    assert [names.index(n) for n in order] == sorted(names.index(n) for n in order)
    assert "frozen_ci sync-access-log" in run[order[0]]["run"] and "frozen_ci clean-check" in run[order[3]]["run"]
    # nothing before the paid step writes into the working tree: helper files go to $RUNNER_TEMP
    for name in names[: names.index(frozen["name"])]:
        script = run[name].get("run", "")
        for redirect in re.findall(r">>?\s*(\S+)", script):
            assert redirect.startswith(('"$RUNNER_TEMP', '"$GITHUB_OUTPUT"', "/dev/null")), (name, redirect)
    action = (REAL_REPO / ".github" / "actions" / "get-dataset" / "action.yml").read_text(encoding="utf-8")
    assert "dataset-pin.txt" in action and "> dataset-pin.txt" not in action and '"$RUNNER_TEMP/dataset-pin.txt"' in action
    assert run["Get the dataset at its pinned version (into data/raw/, git-ignored)"]["env"] == \
        {"SEC_CONTACT_EMAIL": "${{ secrets.SEC_CONTACT_EMAIL }}"}


def test_only_the_paid_step_holds_model_keys_and_no_job_with_keys_can_write():
    used = set(re.findall(r"secrets\.([A-Z_]+)", TEXT))
    assert used <= set(KNOWN_SECRETS)
    assert {"OPENAI_API_KEY", "GEMINI_API_KEY", "ZAI_API_KEY", "MOONSHOT_API_KEY", "SERPAPI_API_KEY"} <= used
    for job_name, job in JOBS.items():
        text = yaml.safe_dump(job)
        if "permissions" in job:
            assert job_name in ("audit-log", "results-log") and "secrets." not in text, job_name
        for step in job["steps"]:
            assert "secrets." not in str(step.get("with", "")), (job_name, step.get("name"))
    assert JOBS["audit-log"]["permissions"] == {"contents": "write", "pull-requests": "write"}
    assert JOBS["results-log"]["permissions"] == {"contents": "write"}
    model_keys = [n for n, s in steps("run").items() if "OPENAI_API_KEY" in str(s.get("env", ""))]
    assert model_keys == ["Frozen test run (paid; one logged test-split access)"]


def test_layer3_page_text_is_uploaded_only_encrypted():
    run = steps("run")
    clear = run["Upload runs/ of layers 1 and 2 (the budget ledger reads their spend)"]
    assert "private_outputs != 'true'" in clear["if"] and clear["with"]["path"] == "runs/"
    encrypt = run["Encrypt runs/ of a live-web run (third-party page text is never uploaded in clear)"]
    assert "private_outputs == 'true'" in encrypt["if"] and "--symmetric --cipher-algo AES256" in encrypt["run"]
    assert "rm -f private/runs.tar.gz" in encrypt["run"]
    assert run["Upload the encrypted runs/ of a live-web run"]["with"]["path"] == "private/runs.tar.gz.gpg"
    refuse = run["Refuse a live-web run whose run directory could not be encrypted"]
    names = list(run)
    assert names.index(refuse["name"]) < names.index("Frozen test run (paid; one logged test-split access)")
    assert "binding_gate.layer3_summary" in run["Layer-3 summary inside the job (costs, metrics, outcomes; no page text, no quotes)"]["run"]
    stage = run["Results for the results-log branch (allow-list of binding_gate.report.publish)"]
    assert "frozen_ci stage-results --runs-dir runs --layer \"$LAYER\"" in stage["run"]


def test_the_access_log_returns_to_main_through_a_checked_branch_and_a_pull_request():
    audit = steps("audit-log")
    assert JOBS["audit-log"]["needs"] == ["plan", "run"] and "needs.run.result != 'skipped'" in JOBS["audit-log"]["if"]
    command = audit["Check that only this run's entries were appended, and push them on a branch"]["run"]
    assert "binding_gate.frozen_ci audit-branch" in command and "--tag \"$TAG\"" in command
    pr = audit["Open the pull request into main"]
    assert "gh pr create --base main --head \"$BRANCH\"" in pr["run"] and pr["env"]["GH_TOKEN"] == "${{ github.token }}"
    results = steps("results-log")["Commit them to results-log under frozen/<layer>/<workflow run id>/"]
    assert "binding_gate.frozen_ci commit-results" in results["run"]


# --------------------------------------------------------------------------- #
# The access log on real git repositories
# --------------------------------------------------------------------------- #


def tagged_project(tmp_path: Path):
    """A remote with main, a freeze tag, and main's log one (earlier) entry ahead of the tag; a clone at the tag."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    repo = frozen_repo(tmp_path / "work")
    b.git(repo, "branch", "-M", "main")
    b.git(repo, "remote", "add", "origin", str(bare))
    b.git(repo, "push", "-q", "origin", "main", "--tags")
    # an earlier frozen run (another layer) left an entry that reached main after the tag
    with pytest.raises(testlock.TestSplitLocked):
        testlock.load_test_split([], repo_root=repo, confirm_tag="prereg-freeze-0", purpose="earlier refused attempt")
    b.git(repo, "add", testlock.ACCESS_LOG_RELPATH)
    b.git(repo, "commit", "-q", "-m", "audit: earlier entry")
    b.git(repo, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    b.git(clone, "-c", "advice.detachedHead=false", "checkout", "-q", f"refs/tags/{TAG}")
    return bare, repo, clone


def test_the_tag_checkout_with_mains_log_opens_the_lock_and_the_new_entry_chains_onto_main(tmp_path):
    bare, _repo, clone = tagged_project(tmp_path)
    base = tmp_path / "base.jsonl"
    info = frozen_ci.sync_access_log(clone, base)
    assert info["tag_entries"] == 0 and info["base_entries"] == 1
    assert frozen_ci.clean_check(clone) == []  # only the access log differs from the tag
    check = testlock.check_unlock(clone, TAG, "frozen run from the workflow")
    assert check.would_unlock, check.reasons  # HEAD == tag, clean tree, FROZEN: the lock's own conditions
    result = run_frozen(clone)  # the real one-command frozen run in the tag checkout
    assert result.analysis_dir is not None
    new = (clone / testlock.ACCESS_LOG_RELPATH).read_text(encoding="utf-8")
    added = frozen_ci.appended_entries(base.read_text(encoding="utf-8"), new, tag=TAG,
                                       tag_commit=b.git(clone, "rev-parse", "HEAD"))
    assert [e["event"] for e in added] == [testlock.EVENT_LOADED]
    assert len(testlock.read_access_log(clone / testlock.ACCESS_LOG_RELPATH)) == 2

    pushed = frozen_ci.audit_branch(clone, clone / testlock.ACCESS_LOG_RELPATH, base, tag=TAG, run_id=77, layer="layer1")
    assert pushed == {**pushed, "branch": "audit-log/frozen-77", "entries_added": 1, "pushed": True}
    changed = b.git(bare, "diff", "--name-only", "main", "audit-log/frozen-77").split()
    assert changed == [testlock.ACCESS_LOG_RELPATH]  # nothing else changes on main
    assert b.git(bare, "rev-parse", "audit-log/frozen-77~1") == b.git(bare, "rev-parse", "main")
    assert b.git(bare, "show", f"audit-log/frozen-77:{testlock.ACCESS_LOG_RELPATH}") == new.rstrip("\n")
    again = frozen_ci.audit_branch(clone, clone / testlock.ACCESS_LOG_RELPATH, base, tag=TAG, run_id=77, layer="layer1")
    assert again["already_pushed"] is True and again["entries_added"] == 1  # a re-run attempt of the job

    # the next frozen run refuses while that branch is not merged, and goes ahead once it is
    b.git(clone, "fetch", "-q", "origin")
    assert frozen_ci.pending_audit_branches(clone) == ["audit-log/frozen-77"]
    assert frozen_ci.main(["pending-audit", "--repo", str(clone)]) == frozen_ci.EXIT_REFUSED
    b.git(bare, "update-ref", "refs/heads/main", b.git(bare, "rev-parse", "audit-log/frozen-77"))
    b.git(clone, "fetch", "-q", "origin")
    assert frozen_ci.pending_audit_branches(clone) == []


def test_the_audit_branch_refuses_anything_but_an_append_of_this_tags_entries(tmp_path):
    bare, repo, clone = tagged_project(tmp_path)
    base = tmp_path / "base.jsonl"
    frozen_ci.sync_access_log(clone, base)
    assert testlock.load_test_split([], repo_root=clone, confirm_tag=TAG, purpose="granted access") == []
    log = clone / testlock.ACCESS_LOG_RELPATH
    text = log.read_text(encoding="utf-8")
    tag_commit = b.git(clone, "rev-parse", "HEAD")
    base_text = base.read_text(encoding="utf-8")
    edited = text.replace("earlier refused attempt", "edited", 1)
    with pytest.raises(frozen_ci.FrozenCIError, match="changed or removed"):
        frozen_ci.appended_entries(base_text, edited, tag=TAG, tag_commit=tag_commit)
    with pytest.raises(frozen_ci.FrozenCIError, match="names tag"):
        frozen_ci.appended_entries(base_text, text, tag="prereg-freeze-2", tag_commit=tag_commit)
    with pytest.raises(frozen_ci.FrozenCIError, match="made at"):
        frozen_ci.appended_entries(base_text, text, tag=TAG, tag_commit="0" * 40)
    assert frozen_ci.appended_entries(base_text, base_text, tag=TAG, tag_commit=tag_commit) == []
    assert frozen_ci.audit_branch(clone, base, base, tag=TAG, run_id=5, layer="layer1")["entries_added"] == 0
    # main moved while the run ran: the entries cannot be chained automatically
    (repo / "NOTE.md").write_text("x", encoding="utf-8")
    with pytest.raises(testlock.TestSplitLocked):
        testlock.load_test_split([], repo_root=repo, confirm_tag="prereg-freeze-0", purpose="another")
    b.git(repo, "add", "-A")
    b.git(repo, "commit", "-q", "-m", "main moved")
    b.git(repo, "push", "-q", "origin", "main")
    b.git(clone, "fetch", "-q", "origin")
    with pytest.raises(frozen_ci.FrozenCIError, match="changed while the frozen run ran"):
        frozen_ci.audit_branch(clone, log, base, tag=TAG, run_id=6, layer="layer1")


def test_a_main_log_that_does_not_continue_the_tags_log_is_refused(tmp_path):
    bare, repo, clone = tagged_project(tmp_path)
    # a second freeze tag on a commit whose log holds an entry that main's log does not have
    b.git(repo, "-c", "advice.detachedHead=false", "checkout", "-q", f"refs/tags/{TAG}")
    with pytest.raises(testlock.TestSplitLocked):
        testlock.load_test_split([], repo_root=repo, confirm_tag="prereg-freeze-x", purpose="side entry")
    b.git(repo, "add", "-A")
    b.git(repo, "commit", "-q", "-m", "side entry")
    b.git(repo, "tag", "prereg-freeze-2")
    b.git(repo, "push", "-q", "origin", "prereg-freeze-2")
    b.git(clone, "fetch", "-q", "--tags", "origin")
    b.git(clone, "-c", "advice.detachedHead=false", "checkout", "-q", "refs/tags/prereg-freeze-2")
    with pytest.raises(frozen_ci.FrozenCIError, match="does not continue the log of the tag"):
        frozen_ci.sync_access_log(clone, tmp_path / "base.jsonl")
    assert frozen_ci.main(["sync-access-log", "--repo", str(clone), "--base-out", str(tmp_path / "b2")]) == 1


# --------------------------------------------------------------------------- #
# Results for the results-log branch
# --------------------------------------------------------------------------- #


def test_layer1_results_are_published_with_traces_and_committed_under_frozen(tmp_path):
    repo = frozen_repo(tmp_path / "p")
    result = run_frozen(repo)
    staged = tmp_path / "staged"
    summary = frozen_ci.stage_results(repo / "runs", "layer1", staged)
    run_id = result.run_dir.name
    (row,) = summary["runs"]
    assert row["published"] is True
    files = {p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file()}
    assert {f"{run_id}/manifest.json", f"{run_id}/analysis/analysis.json", f"{run_id}/analysis/report.md",
            f"{run_id}/frozen_run.jsonl", f"{run_id}/results_manifest.json", f"{run_id}/traces.jsonl",
            "frozen_summary.json"} <= files
    assert frozen_ci.publish_exemptions(staged, "layer1") == [f"{run_id}/traces.jsonl"]  # no figures drawn here
    assert results_log.staging_refusals(staged, exempt=frozen_ci.publish_exemptions(staged, "layer1")) == []
    assert any("traces.jsonl" in p for p in results_log.staging_refusals(staged))  # only layer 1 may carry traces
    assert frozen_ci.publish_exemptions(staged, "layer2_sec") == []
    figure = staged / run_id / "analysis" / "figures" / "primary_pair.png"
    figure.parent.mkdir(parents=True)
    figure.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)
    assert f"{run_id}/analysis/figures/primary_pair.png" in frozen_ci.publish_exemptions(staged, "layer2_sec")
    assert results_log.staging_refusals(staged, exempt=frozen_ci.publish_exemptions(staged, "layer1")) == []
    figure.write_bytes(b"not a png")
    assert any("not a PNG" in p for p in results_log.staging_refusals(staged, exempt=frozen_ci.publish_exemptions(staged, "layer1")))
    figure.unlink()

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    b.git(repo, "branch", "-M", "main")
    b.git(repo, "remote", "add", "origin", str(bare))
    b.git(repo, "push", "-q", "origin", "main")
    out = frozen_ci.commit_results(repo, staged, layer="layer1", run_id=31)
    assert out["result"] == "pushed"
    listed = b.git(bare, "ls-tree", "-r", "--name-only", results_log.BRANCH).split()
    assert f"frozen/layer1/31/{run_id}/traces.jsonl" in listed and "frozen/layer1/31/frozen_summary.json" in listed
    with pytest.raises(results_log.ResultsLogError, match="traces.jsonl"):
        frozen_ci.commit_results(repo, staged, layer="layer2_sec", run_id=32)  # layer 2 traces never go there


def test_an_unfinished_run_stages_only_its_manifest_invocations_and_record(tmp_path):
    repo = frozen_repo(tmp_path / "p")
    result = run_frozen(repo)
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    (result.run_dir / "manifest.json").write_text(json.dumps({**manifest, "ended_at": None}), encoding="utf-8")
    staged = tmp_path / "staged"
    (row,) = frozen_ci.stage_results(repo / "runs", "layer3_live", staged)["runs"]
    assert row["published"] is False and "unfinished" in row["publish_refused"]
    files = {p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file()}
    assert files == {f"{result.run_dir.name}/manifest.json", f"{result.run_dir.name}/invocations.jsonl",
                     f"{result.run_dir.name}/frozen_run.jsonl", "frozen_summary.json"}
    assert results_log.staging_refusals(staged) == []


def test_the_real_repository_access_log_was_not_written():
    assert (REAL_REPO / testlock.ACCESS_LOG_RELPATH).read_bytes() == b""


# --------------------------------------------------------------------------- #
# Documentation: the owner page gives every dialog; the final list of secrets
# --------------------------------------------------------------------------- #

OWNER = (REAL_REPO / "docs" / "OWNER_SETUP.md").read_text(encoding="utf-8")
FINAL_SECRETS = ("OPENAI_API_KEY", "GEMINI_API_KEY", "ZAI_API_KEY", "MOONSHOT_API_KEY", "SERPAPI_API_KEY",
                 "RUNS_ENCRYPTION_PASSPHRASE", "SEC_CONTACT_EMAIL")


def test_the_owner_page_gives_the_frozen_run_dialog_of_every_layer_in_both_languages():
    inputs = DOC.get("on", DOC.get(True))["workflow_dispatch"]["inputs"]
    hebrew, english = OWNER.split("## English", 1)
    for part, checked in ((hebrew, "מסומן"), (english, "checked")):
        for layer in frozen_ci.LAYER_CONFIGS:
            assert f"| {inputs['layer']['description']} | `{layer}` |" in part, layer
        assert part.count(f"| {inputs['tag']['description']} |") == len(frozen_ci.LAYER_CONFIGS)
        assert part.count(f"| {inputs['confirm_spend']['description']} | {checked} |") == len(frozen_ci.LAYER_CONFIGS)
        for secret in FINAL_SECRETS:
            assert f"| `{secret}` |" in part, secret
        assert "results-log" in part and "Allow GitHub Actions to create and approve pull requests" in part


def test_the_final_secret_list_is_the_study_list_and_the_readme_names_it():
    from binding_gate.pipeline import STUDY_SECRETS

    assert tuple(STUDY_SECRETS) == FINAL_SECRETS
    readme = (REAL_REPO / "README.md").read_text(encoding="utf-8")
    top = readme[: readme.index("## The failure studied")]
    assert all(f"`{s}`" in top for s in FINAL_SECRETS) and "results-log" in top and "frozen_test.yml" in top
    running = (REAL_REPO / "docs" / "RUNNING.md").read_text(encoding="utf-8")
    assert "frozen_test.yml" in running and "results-log" in running and "No step needs a local command line" in running
