"""GRAB-RAG end to end: the dry run in a fresh process and a scored run with a scripted model.

No network, no API key; the checkout and the export are the synthetic fixture.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import grab_rag_fixture as fx
import run_helpers as h
from binding_gate import runner
from binding_gate.config import load_config, parse_config
from binding_gate.contracts import ModelRequest, ModelResponse, TokenUsage
from binding_gate.models._http import PreflightResult, RetryPolicy
from binding_gate.registry import register
from binding_gate.split import select_dev
from binding_gate.testlock import ACCESS_LOG_RELPATH
from binding_gate.trace import read_traces

REPO_ROOT = Path(__file__).resolve().parents[1]
_DOC = re.compile(r"\[Document \d+ — source: (\w+)\]\n(.*?)(?=\n\n\[Document |\n\nQuestion: )", re.DOTALL)


@register("model", "grab_rag_keyword_fake")
class KeywordFake:
    """Answers with the first word matching ``keyword`` in any shown passage, quoting that
    passage; abstains when no passage has it. Never touches the network."""

    def __init__(self, *, label: str | None = None, model_id: str = "fake-cheap", keyword: str = r"Zorbland\d+"):
        self.name = label or model_id
        self.model_id = model_id
        self.max_output_tokens = 1024
        self.retry = RetryPolicy(max_retries=0, backoff_seconds=0.0)
        self.keyword = re.compile(keyword)

    def preflight(self) -> PreflightResult:
        return PreflightResult(self.name, self.model_id, True, "listed")

    def complete(self, request: ModelRequest) -> ModelResponse:
        text = json.dumps({"status": "abstained", "reason": "nothing found"})
        for source, passage in _DOC.findall(request.messages[-1].content):
            match = self.keyword.search(passage)
            if match:
                quote = " ".join(passage.split()[:10])
                text = json.dumps({"status": "answered", "value": match.group(), "quote": quote, "source_id": source})
                break
        return ModelResponse(text=text, model=self.model_id, usage=TokenUsage(100, 20))


def _raw_config(options: dict, models: list[dict]) -> dict:
    return {
        "config_version": 1,
        "name": "grab-rag-test",
        "split": "dev",
        "repeats": 1,
        "dataset": {"name": "grab_rag", "options": options},
        "models": models,
        "arms": [{"name": "no_gate", "options": {"presentation": "grab_rag_context"}}],
        "scorers": [{"name": "provenance"}, {"name": "grab_rag_answer"}],
        "runner": {"concurrency": 2, "max_usd": 5.0},
    }


def _toml(raw: dict) -> str:
    def value(v) -> str:
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        return json.dumps(v)

    lines = [f"{k} = {value(v)}" for k, v in raw.items() if not isinstance(v, (dict, list))]
    lines += ["[runner]"] + [f"{k} = {value(v)}" for k, v in raw["runner"].items()]
    lines += ["[dataset]", f"name = {value(raw['dataset']['name'])}", "[dataset.options]"]
    lines += [f"{k} = {value(v)}" for k, v in raw["dataset"]["options"].items()]
    for key in ("models", "arms", "scorers"):
        for entry in raw[key]:
            lines += [f"[[{key}]]"] + [f"{k} = {value(v)}" for k, v in entry.items() if k != "options"]
            if entry.get("options"):
                lines += [f"[{key}.options]"] + [f"{k} = {value(v)}" for k, v in entry["options"].items()]
    return "\n".join(lines) + "\n"


def test_dry_run_in_a_fresh_process_without_any_key(tmp_path):
    repo = h.make_repo(tmp_path)
    options = fx.write_checkout(tmp_path / "grab-rag")
    model = {"name": "openai_responses", "label": "openai-low", "options": {"model_id": "fake-cheap", "max_output_tokens": 4096}}
    raw = _raw_config(options, [model])
    config_path = repo / "configs" / "grab.toml"
    config_path.write_text(_toml(raw), encoding="utf-8")
    assert load_config(config_path).dataset.options == options
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    out = tmp_path / "estimate.json"
    result = subprocess.run(
        [sys.executable, "-m", "binding_gate.cli", "dry-run", "--config", str(config_path), "--repo-root", str(repo), "--json", str(out)],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "DRY RUN - no model API was called" in result.stdout
    # 3 dev questions per source dataset; the fixture skips qi 3 for QC. Which dev questions
    # have a misleading context depends on the file order, so count from the adapter itself.
    items = select_dev(runner.registry.create("dataset", "grab_rag", **options).iter_items())
    estimate = json.loads(out.read_text(encoding="utf-8"))
    assert estimate["n_items"] == len(items) and estimate["n_groups"] == len({i.group_id for i in items})
    assert set(estimate["items_by_stratum"]) == {
        "natural_questions/misleading", "natural_questions/supportive", "hotpotqa/misleading", "hotpotqa/supportive",
    }
    assert estimate["models"][0]["calls"] == len(items)
    assert not (repo / "runs").exists() and not (repo / ACCESS_LOG_RELPATH).exists()


def test_scored_run_labels_misled_answers_and_paired_abstentions(tmp_path):
    repo = h.make_repo(tmp_path)
    options = fx.write_checkout(tmp_path / "grab-rag")
    config = parse_config(_raw_config(options, [{"name": "grab_rag_keyword_fake", "label": "keyword"}]))
    summary = runner.execute_run(
        config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda message: None
    )
    records = list(read_traces(summary.run_dir))
    assert summary.closed and records and all(r.error is None for r in records)
    for record in records:
        provenance, answer = record.scores
        assert (provenance.scorer, answer.scorer) == ("provenance", "grab_rag_answer")
        if record.stratum.endswith("/misleading"):
            # The keyword is the planted name: the model repeats the plant and quotes the edited passage.
            assert provenance.label == "competitor_only" and answer.label == "misled"
            assert answer.details["quote_source"] == "misleading"
        else:
            assert provenance.label == "abstained" and answer.label == "abstained"
    assert {r.stratum.split("/")[1] for r in records} == {"misleading", "supportive"}
