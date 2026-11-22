"""Helpers for the layer-2 (sec_financials) tests: a generated fixture dataset and a scripted model.

Nothing touches the network: the dataset is generated from tests/sec_fixture.py (invented EDGAR
responses) with an injected transport and an invented contact address.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import sec_fixture as fx
from binding_gate.contracts import ModelRequest, ModelResponse, TokenUsage
from binding_gate.datasets import sec_build
from binding_gate.models._http import PreflightResult, RetryPolicy
from binding_gate.registry import register

FAKE_EMAIL = "owner-test@example.org"


def write_dataset(root: Path, *, n_extra: int = 0, companies=None) -> dict:
    """Generate the fixture dataset into ``root``; returns the dataset options of a run config."""
    if not (root / sec_build.MANIFEST_NAME).is_file():
        fake = fx.FakeEdgar(companies if companies is not None else fx.make_companies(n_extra))
        sec_build.prepare(root, contact_email=FAKE_EMAIL, transport=fake, sleep=lambda s: None)
    manifest = json.loads((root / sec_build.MANIFEST_NAME).read_text(encoding="utf-8"))
    return {"root": str(root), "data_sha256": manifest["content_sha256"], "build_config": "configs/datasets/sec_financials_v1.toml"}


_DOC = re.compile(r"\[Document \d+ — source: (\w+)\]\n(.*?)(?=\n\n\[Document |\n\nQuestion: )", re.DOTALL)
ROW_BY_METRIC = {
    "total revenue": ("Total revenues", "Total interest and noninterest income"),
    "operating income": ("Operating income",),
    "net income": ("Net income attributable to the company", "Net income"),
    "total assets": ("Total assets",),
    "cash and cash equivalents": ("Cash and cash equivalents",),
}
SCALE_WORD = {"Thousands": "thousand", "Millions": "million"}


def scripted_answer(content: str, *, document: int = 0, column: int = 0) -> dict:
    """Answer from the ``document``-th shown document: the metric's row, ``column``-th year, quoted verbatim."""
    question = content.split("\n\nQuestion: ", 1)[1]
    labels = next(v for k, v in ROW_BY_METRIC.items() if k in question)
    docs = _DOC.findall(content)
    source, text = docs[min(document, len(docs) - 1)]
    scale = ""
    for line in text.splitlines():
        if ": CONSOLIDATED" in line:
            match = re.search(r"\$ in (Thousands|Millions)", line)
            scale = SCALE_WORD[match.group(1)] if match else ""
        for label in labels:
            if line.startswith(label + " | "):
                cells = line.split(" | ")[1:]
                cell = cells[min(column, len(cells) - 1)]
                number = cell.split(": ", 1)[1].replace("$", "").strip()
                quote = " | ".join([label, cell]) if column == 0 else cell
                value = f"${number} {scale}".strip()
                return {"status": "answered", "value": value, "quote": quote, "source_id": source}
    return {"status": "abstained", "reason": "not found"}


@register("model", "sec_scripted_fake")
class SecScriptedFake:
    """Reads the metric's row from the FIRST shown document (so a peer shown first yields a wrong entity)."""

    def __init__(self, *, label: str | None = None, model_id: str = "fake-cheap", column: int = 0):
        self.name = label or model_id
        self.model_id = model_id
        self.column = column
        self.max_output_tokens = 1024
        self.retry = RetryPolicy(max_retries=0, backoff_seconds=0.0)

    def preflight(self) -> PreflightResult:
        return PreflightResult(self.name, self.model_id, True, "listed")

    def complete(self, request: ModelRequest) -> ModelResponse:
        answer = scripted_answer(request.messages[-1].content, column=self.column)
        return ModelResponse(text=json.dumps(answer), model=self.model_id, usage=TokenUsage(1000, 40))
