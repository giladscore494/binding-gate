"""Arms ``gated_research`` and ``no_gate_live``: layer 3, live web search (PREREGISTRATION.md sections 5, 7, 14).

The agent protocol (identical first attempt for both arms)
---------------------------------------------------------
1. **Query.** The harness searches the question itself
   (``binding_gate.research.query.first_query``: whitespace collapsed). The
   model under test never chooses a query.
2. **Search.** One web search (``search`` option; the study uses Serper's Google
   results since PR 27, SerpApi before; ``binding_gate.research.search``), logged as a call with
   ``purpose = "search"``. At most ``search.options.max_results`` organic results
   (default 10) are kept, in rank order.
3. **Pages.** The result URLs are fetched in rank order until
   ``documents_per_attempt`` pages (default 5) were fetched successfully or the
   results run out (``binding_gate.research.fetch``: robots.txt respected, size
   and time limits, every outcome stored and logged with ``purpose = "fetch"``,
   failures never retried).
4. **Documents.** Each page's stored extracted text, cut to its first
   ``max_document_chars`` characters (default 8,000), becomes a document
   ``web1`` ... ``web5`` (``binding_gate.research.session``). The answer request
   is built by ``binding_gate.answering`` with the presentation
   ``sealqa_live_web``. Searches, pages and therefore answer requests are shared
   by both arms, all models and all repeats of a run, so within a run the
   runner's response cache hands ``gated_research`` exactly the candidate that
   ``no_gate_live`` got (same model, same repeat).
5. **Answer** in the structure of section 6.2 (value, verbatim quote, source id,
   or an abstention).

``no_gate_live`` stops here: the candidate is the final answer (the live
baseline; in run configs it carries the label ``no_gate``, the reference arm of
the statistics).

``gated_research`` continues
----------------------------
6. Abstained or unparseable candidates pass through unchanged (no verdict).
7. For an answered candidate: constraints from the decomposer named in
   ``options.decomposer`` (one decomposition per question per process, shared
   with ``gated`` arms through ``binding_gate.arms.gated.MEMO``), then the
   ``binding`` gate with ``options.checks`` and ``options.parameters`` - built
   through the registry exactly as in ``gated``. The gate sees only the
   documents built from stored snapshots.
8. ACCEPT -> final answer. ABSTAIN -> final abstention.
9. RESEARCH and fewer than ``max_attempts`` attempts so far -> ONE targeted
   re-search with the query built deterministically from the verdict's
   ``research_request`` (``binding_gate.research.query.research_query``:
   ``"<entity>" <qualifiers> <attribute>``). Its result URLs are fetched as in
   step 3, skipping every URL already used for this item, and the new pages
   (only those) are shown in a new answer request; the new candidate is judged
   against the same constraints (steps 6-8). The prompt does not mention the
   earlier attempt: the re-search changes only the evidence.
10. RESEARCH at the last allowed attempt, a re-search that fails after the
    search client's retries, or one that yields no new page -> final
    abstention whose reason says so (the last attempt keeps its RESEARCH
    verdict; no attempt without evidence is invented).

``max_attempts`` = 2 (the first attempt plus one re-search) is **PROPOSED -
requires owner approval before freeze**; values up to 3 are accepted.

Every attempt is recorded as an ``Attempt`` with its candidate, its evidence
references (``EvidenceRef(id=<snapshot id>, sha256=<hash of the shown text>,
url=<final URL>)``; ``web<n>`` of that attempt is ``evidence[n-1]``) and its
verdict. ``ArmOutput.model_calls`` lists every call in order: search, fetches,
answer, decomposition, then search, fetches, answer of each re-search.

Failures. A failed first search or a failed answer or decomposition call
raises ``ModelCallFailed`` with every call made so far, so the runner writes an
error record (section 6.4: errors stay in every denominator).

Options (both arms unless noted)
--------------------------------
``search``                 ``{name = "serper_google", label = "search", options = {...}}`` (required)
``fetch``                  fetch limits (``binding_gate.research.fetch.FetchSettings``)
``documents_per_attempt``  default 5 (PROPOSED)
``max_document_chars``     default 8,000 (PROPOSED)
``presentation``           default ``sealqa_live_web``
``decomposer``, ``checks``, ``parameters``  as in ``gated`` (``gated_research`` only, required)
``max_attempts``           ``gated_research`` only, 1-3, default 2 (PROPOSED)

The arms need the run context the runner provides (``binding_gate.research.context``);
tests may pass ``session=`` (a ``ResearchSession``) directly.
"""

from __future__ import annotations

import threading
from typing import Any, Mapping

from binding_gate import registry
from binding_gate.answering import PRESENTATIONS, ModelCallFailed, answer_once
from binding_gate.arms.gated import MEMO, decomposer_spec
from binding_gate.canonical import sha256_json
from binding_gate.contracts import (
    AnswerCandidate,
    AnswerStatus,
    ArmInput,
    ArmOutput,
    Attempt,
    Decision,
    Decomposition,
    Document,
    ModelCall,
)
from binding_gate.decomposers.cache import as_cache_hit
from binding_gate.gates.binding import NAME as GATE_NAME
from binding_gate.interfaces import ModelClient
from binding_gate.research import context as run_context
from binding_gate.research.fetch import FetchSettings
from binding_gate.research.query import first_query, research_query
from binding_gate.research.search import normalise_search_spec
from binding_gate.research.session import (
    DEFAULT_DOCUMENTS_PER_ATTEMPT,
    DEFAULT_MAX_DOCUMENT_CHARS,
    Evidence,
    ResearchSession,
    session_for,
)

NAME = "gated_research"
LIVE_BASELINE_NAME = "no_gate_live"
DEFAULT_PRESENTATION = "sealqa_live_web"
DEFAULT_MAX_ATTEMPTS = 2
MAX_ATTEMPTS_LIMIT = 3


def _positive_int(name: str, value: Any, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or (maximum is not None and value > maximum):
        bound = f" and at most {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be an integer >= 1{bound}")
    return value


class _LiveWebArm:
    """The first-attempt protocol shared by both arms (steps 1-5 of the module docstring)."""

    name = "live_web"

    def __init__(
        self,
        *,
        model: ModelClient,
        search: Mapping[str, Any] | None = None,
        fetch: Mapping[str, Any] | None = None,
        documents_per_attempt: int = DEFAULT_DOCUMENTS_PER_ATTEMPT,
        max_document_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
        presentation: str = DEFAULT_PRESENTATION,
        session: ResearchSession | None = None,
    ):
        if presentation not in PRESENTATIONS:
            raise ValueError(f"unknown presentation {presentation!r}; available: {sorted(PRESENTATIONS)}")
        if search is None:
            raise ValueError(f"{self.name} needs options.search (the web search client)")
        self.model = model
        self.presentation = presentation
        self.search_spec = normalise_search_spec(search)
        self.fetch_settings = FetchSettings.from_options(fetch)
        self.documents_per_attempt = _positive_int("documents_per_attempt", documents_per_attempt, 20)
        self.max_document_chars = _positive_int("max_document_chars", max_document_chars)
        if session is None:
            context = run_context.current()
            if context is None:
                raise ValueError(
                    f"{self.name} needs the run context that the runner provides "
                    "(binding_gate.research.context), or an explicit session"
                )
            session = session_for(
                context, self.search_spec, self.fetch_settings.to_dict(), max_document_chars=self.max_document_chars
            )
        self.session = session

    # -- what the runner's dry run needs ------------------------------------ #

    max_attempts = 1

    def extra_attempt_bound(self) -> dict[str, int]:
        """Calls one extra (re-search) attempt can make at most, by purpose."""
        max_results = int(self.search_spec["options"].get("max_results", 10))
        return {"search": 1, "fetch": max_results, "answer": 1}

    # -- steps 1-5 ----------------------------------------------------------- #

    def gather(self, query: str, exclude: frozenset[str], calls_so_far: tuple[ModelCall, ...]) -> Evidence:
        try:
            return self.session.gather(
                query,
                documents_per_attempt=self.documents_per_attempt,
                max_document_chars=self.max_document_chars,
                exclude_urls=exclude,
            )
        except ModelCallFailed as exc:
            raise ModelCallFailed(f"web search failed: {exc}", calls_so_far + exc.model_calls) from exc

    def answer(
        self, arm_input: ArmInput, documents: tuple[Document, ...], calls_so_far: tuple[ModelCall, ...]
    ) -> tuple[AnswerCandidate, tuple[ModelCall, ...]]:
        shown = ArmInput(item_id=arm_input.item_id, question=arm_input.question, documents=documents)
        try:
            return answer_once(self.model, shown, self.presentation)
        except ModelCallFailed as exc:
            raise ModelCallFailed(str(exc), calls_so_far + exc.model_calls) from exc

    def first_attempt(self, arm_input: ArmInput) -> tuple[Evidence, AnswerCandidate, tuple[ModelCall, ...]]:
        evidence = self.gather(first_query(arm_input.question), frozenset(), ())
        answer, answer_calls = self.answer(arm_input, evidence.documents, evidence.calls)
        return evidence, answer, evidence.calls + answer_calls


@registry.register("arm", LIVE_BASELINE_NAME)
class NoGateLiveArm(_LiveWebArm):
    """The live baseline: the first attempt of the protocol, no gate (label it ``no_gate`` in configs)."""

    name = LIVE_BASELINE_NAME

    def run(self, arm_input: ArmInput) -> ArmOutput:
        evidence, answer, calls = self.first_attempt(arm_input)
        return ArmOutput(attempts=(Attempt(answer=answer, evidence=evidence.refs),), final_answer=answer, model_calls=calls)


@registry.register("arm", NAME)
class GatedResearchArm(_LiveWebArm):
    """``gated`` plus the targeted re-search loop (see the module docstring)."""

    name = NAME

    def __init__(
        self,
        *,
        model: ModelClient,
        decomposer: Mapping[str, Any] | None = None,
        checks: list[str] | None = None,
        parameters: Mapping[str, Any] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        **options: Any,
    ):
        if decomposer is None:
            raise ValueError("gated_research needs options.decomposer (as in gated)")
        if checks is None:
            raise ValueError("gated_research needs options.checks (as in gated)")
        if parameters is not None and not isinstance(parameters, Mapping):
            raise ValueError("parameters must be a table")
        self.max_attempts = _positive_int("max_attempts", max_attempts, MAX_ATTEMPTS_LIMIT)
        self.decomposer_spec = decomposer_spec(decomposer)
        self.gate = registry.create("gate", GATE_NAME, checks=list(checks), **dict(parameters or {}))
        self._decomposer: Any = None
        self._lock = threading.Lock()
        super().__init__(model=model, **options)

    def _get_decomposer(self) -> Any:
        with self._lock:
            if self._decomposer is None:
                spec = self.decomposer_spec
                self._decomposer = registry.create("decomposer", spec["name"], **spec["options"])
            return self._decomposer

    def decompose(self, question: str, calls_so_far: tuple[ModelCall, ...]) -> tuple[Decomposition, tuple[ModelCall, ...]]:
        key = sha256_json({"decomposer": self.decomposer_spec, "question": question})
        try:
            decomposition, reused = MEMO.get_or_compute(key, lambda: self._get_decomposer().decompose(question))
        except Exception as exc:  # noqa: BLE001 - keep every call made so far in the error record
            raise ModelCallFailed(
                f"decomposition failed: {type(exc).__name__}: {exc}",
                calls_so_far + tuple(getattr(exc, "model_calls", ())),
            ) from exc
        calls = decomposition.model_calls
        return decomposition, (as_cache_hit(calls) if reused else tuple(calls))

    def run(self, arm_input: ArmInput) -> ArmOutput:
        evidence, answer, calls = self.first_attempt(arm_input)
        used = evidence.urls_used
        attempts: list[Attempt] = []
        constraints = None
        while True:
            if answer.status is not AnswerStatus.ANSWERED:
                attempts.append(Attempt(answer=answer, evidence=evidence.refs))
                final = answer
                break
            if constraints is None:
                decomposition, decompose_calls = self.decompose(arm_input.question, calls)
                calls += decompose_calls
                constraints = decomposition.constraints
            verdict = self.gate.evaluate(constraints, answer, evidence.documents)
            attempts.append(Attempt(answer=answer, evidence=evidence.refs, verdict=verdict))
            reasons = "; ".join(verdict.reasons)
            if verdict.decision is Decision.ACCEPT:
                final = answer
                break
            if verdict.decision is Decision.ABSTAIN:
                final = AnswerCandidate.abstained(raw="", reason=f"gate abstain: {reasons}")
                break
            if len(attempts) >= self.max_attempts:
                final = AnswerCandidate.abstained(
                    raw="", reason=f"gate research: {reasons} (maximum of {self.max_attempts} attempts reached)"
                )
                break
            assert verdict.research_request is not None
            query = research_query(verdict.research_request)
            try:
                evidence = self.gather(query, used, calls)
            except ModelCallFailed as exc:
                calls = exc.model_calls
                final = AnswerCandidate.abstained(raw="", reason=f"gate research: {reasons} (re-search failed: {exc})"[:2000])
                break
            calls += evidence.calls
            used = used | evidence.urls_used
            if not evidence.documents:
                final = AnswerCandidate.abstained(raw="", reason=f"gate research: {reasons} (the re-search found no new page)")
                break
            answer, answer_calls = self.answer(arm_input, evidence.documents, calls)
            calls += answer_calls
        return ArmOutput(attempts=tuple(attempts), final_answer=final, constraints=constraints, model_calls=calls)
