"""Small factories for valid contract objects and throwaway git repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

from binding_gate.config import RunConfig, parse_config
from binding_gate.contracts import (
    AnswerCandidate,
    Attempt,
    CheckResult,
    CheckStatus,
    Decision,
    Document,
    Entity,
    Item,
    Message,
    ModelCall,
    ModelRequest,
    ModelResponse,
    Presupposition,
    Qualifier,
    QualifierKind,
    QuestionConstraints,
    ScoreResult,
    Split,
    TokenUsage,
    TraceRecord,
    Verdict,
)

T0 = "2026-11-22T10:00:00+00:00"
T1 = "2026-11-22T10:00:05+00:00"
COMMIT = "a" * 40


def constraints() -> QuestionConstraints:
    return QuestionConstraints(
        question="What was the recommended starting dose of Alphadrug for adults in 2024?",
        entity=Entity(name="Alphadrug", aliases=("alpha-drug",), kind="drug"),
        attribute="recommended starting dose",
        qualifiers=(
            Qualifier(QualifierKind.TIME, "2024"),
            Qualifier(QualifierKind.OTHER, "adults", aliases=("adult patients",)),
        ),
        presuppositions=(Presupposition("Alphadrug has a recommended dose", ("Alphadrug",)),),
    )


def documents() -> tuple[Document, ...]:
    return (
        Document.create("doc-1", "In 2024 the starting dose of Alphadrug for adults was 10 mg."),
        Document.create("doc-2", "Betadrug is started at 50 mg.", url="https://example.org/beta"),
    )


def answer() -> AnswerCandidate:
    return AnswerCandidate.answered(
        value="10 mg",
        quote="the starting dose of Alphadrug for adults was 10 mg",
        source_id="doc-1",
        raw='{"value": "10 mg"}',
    )


def accept() -> Verdict:
    return Verdict(Decision.ACCEPT, check_results=(CheckResult("quote_verbatim", CheckStatus.PASS),))


def abstain() -> Verdict:
    return Verdict(
        Decision.ABSTAIN,
        reasons=("entity not near quote",),
        check_results=(CheckResult("entity_in_window", CheckStatus.FAIL, {"window": 200}),),
    )


def research() -> Verdict:
    return Verdict(Decision.RESEARCH, reasons=("qualifier 2024 missing",), research_request="Alphadrug dose 2024")


def model_call() -> ModelCall:
    return ModelCall(
        purpose="answer",
        client="model-a",
        request=ModelRequest((Message("system", "Answer with a quote."), Message("user", "Q?")), {"temperature": 0}),
        response=ModelResponse("raw text", "provider/model-a-2026", TokenUsage(120, 30), {"finish": "stop"}),
        started_at=T0,
        ended_at=T1,
    )


def item(item_id: str = "item-1", group_id: str = "group-1", dataset: str = "toy") -> Item:
    return Item(
        id=item_id,
        group_id=group_id,
        dataset=dataset,
        question=constraints().question,
        documents=documents(),
        gold={"document_roles": {"doc-1": "target", "doc-2": "competitor"}},
        stratum="complete/class_proximate",
    )


def run_config(**overrides: object) -> RunConfig:
    raw: dict = {
        "config_version": 1,
        "name": "unit-test",
        "split": "dev",
        "repeats": 3,
        "dataset": {"name": "toy"},
        "models": [{"name": "fake", "label": "model-a"}],
        "arms": [{"name": "no_gate"}],
        "scorers": [{"name": "provenance"}],
    }
    raw.update(overrides)
    return parse_config(raw)


def trace_record(
    *,
    run_id: str = "run-1",
    config_hash: str | None = None,
    git_commit: str = COMMIT,
    item_id: str = "item-1",
    repeat: int = 0,
) -> TraceRecord:
    return TraceRecord(
        run_id=run_id,
        config_hash=config_hash or run_config().config_hash,
        git_commit=git_commit,
        item_id=item_id,
        group_id="group-1",
        dataset="toy",
        split=Split.DEV,
        arm="gated",
        model="model-a",
        repeat=repeat,
        started_at=T0,
        ended_at=T1,
        stratum="complete/class_proximate",
        constraints=constraints(),
        attempts=(Attempt(answer(), tuple(d.ref() for d in documents()), accept()),),
        final_answer=answer(),
        model_calls=(model_call(), model_call()),
        scores=(ScoreResult("provenance", "target_only", {"matched": ["doc-1"]}),),
    )


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def init_repo(path: Path, *, prereg_status: str = "FROZEN") -> Path:
    """Create a git repository with one commit containing a PREREGISTRATION.md."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    (path / "PREREGISTRATION.md").write_text(f"# Pre-registration\n\n**Status:** {prereg_status}\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path
