"""Arm ``llm_judge_checks``: the gate's checks (C, D), performed by an LLM judge instead of code.

PREREGISTRATION.md section 5. This arm answers the question "is the binding
gate better than simply asking a strong LLM to do the same checks?". It judges
the same candidate as ``no_gate`` (the answer request is built with
``binding_gate.answering`` and is answered from the runner's cache), so any
difference from the ``gated`` arm comes from the checking step alone.

Flow for one item
-----------------
1. Ask the model under test for the structured answer (cache hit when
   ``no_gate`` is in the same run). Abstentions and unparseable candidates pass
   through unchanged, as in every gated arm.
2. For an ``answered`` candidate, send ONE request to the judge model: the
   question, the documents exactly as shown to the answering model, the
   candidate (value, quote, source id) and the definitions of the configured
   checks (:data:`CHECK_DEFINITIONS`; option ``checks``, default all five
   reserved check names). Logged with ``ModelCall.purpose = "check"``.
3. Parse the reply with :func:`parse_check_reply`, a deterministic function of
   the raw text (rules in its docstring). Nothing is repaired and there is no
   second request.
4. Verdict: ``accept`` if no check failed, otherwise ``abstain`` with one reason
   per failed check. (``research`` is never returned: in layers 1 and 2 it would
   count as an abstention anyway.)

Judge failures are counted, never hidden
----------------------------------------
If the judge's reply cannot be parsed or was truncated, or the judge call fails
after the client's retries, the arm raises; the runner then writes an ``error``
record that contains every call made for the item (answer and judge). Errors
stay in every denominator (PREREGISTRATION.md 6.4), so a judge that fails often
makes this arm look worse, not better. The same holds when the judge-family
rule cannot be satisfied, when the judge's free preflight fails, and when the
judge budget cap is reached. Problems that can be seen before any call are
raised when the arm is built for a real run (the runner builds arms before the
first paid call): a judge of the same family, or a missing judge API key.

Judge-family rule and fallbacks
-------------------------------
Option ``judges`` is an ordered list of judge model clients (``{name, label,
options}``); the first one whose family differs from the model under test is
used (``binding_gate.judging.choose_judge``). The default configs list only slot
J of PREREGISTRATION.md section 9, ``kimi-k3.1-pro`` (Moonshot), which differs
from every model under test. A fallback is used only if it is listed; if no
listed judge qualifies, the arm refuses: at construction when the family of the
model under test is known, otherwise at the first judge call.

Budget
------
The PR 2 runner does not charge judge calls (the judge is not in
``[[models]]``). This arm therefore enforces its own hard cap,
``judge_max_usd``, over all judge calls of all its instances in one process,
priced from ``prices`` (default ``configs/prices.toml``, relative to the
working directory, i.e. the repository root when run through the CLI). When
the cap is reached, the remaining items become error records; the cap starts
again from zero in a resumed process. ``python -m
binding_gate.arms.llm_judge_checks estimate --config ...`` prints an upper
bound of the judge cost before a run.

Changing :data:`JUDGE_SYSTEM_PROMPT`, :data:`CHECK_DEFINITIONS` or the request
layout changes the experiment: bump :data:`PROMPT_VERSION` (an amendment after
the freeze).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from binding_gate import secrets
from binding_gate.answering import (
    DEFAULT_PRESENTATION,
    PRESENTATIONS,
    ModelCallFailed,
    build_answer_request,
    call_model,
)
from binding_gate.contracts import (
    AnswerCandidate,
    AnswerStatus,
    ArmInput,
    ArmOutput,
    Attempt,
    CheckResult,
    CheckStatus,
    Decision,
    Document,
    Message,
    ModelRequest,
    Verdict,
)
from binding_gate.interfaces import ModelClient
from binding_gate.judging import (
    JudgeBudget,
    JudgeError,
    JudgeOutputUnparseable,
    JudgeUnavailable,
    choose_judge,
    json_objects_with_key,
    judge_price,
    model_family,
    parse_judges,
    preflight_once,
    shared_budget,
    strip_think,
)
from binding_gate.parsing import parse_answer
from binding_gate.registry import register

PROMPT_VERSION = 1

CHECK_NAMES = (
    "quote_verbatim",
    "entity_in_window",
    "qualifiers_in_window",
    "no_closer_competitor",
    "premise_present",
)
"""The reserved check names of ARCHITECTURE.md section 3, in canonical order."""

PASS, FAIL, NOT_APPLICABLE = "pass", "fail", "not_applicable"

ALLOWED_RESULTS: dict[str, frozenset[str]] = {
    "quote_verbatim": frozenset({PASS, FAIL}),
    "entity_in_window": frozenset({PASS, FAIL}),
    "qualifiers_in_window": frozenset({PASS, FAIL, NOT_APPLICABLE}),
    "no_closer_competitor": frozenset({PASS, FAIL}),
    "premise_present": frozenset({PASS, FAIL, NOT_APPLICABLE}),
}

CHECK_DEFINITIONS: dict[str, str] = {
    "quote_verbatim": (
        '"pass" if the quote appears word for word in the document whose source label is the '
        "candidate's source_id (only differences in capitalisation, whitespace, or typographic "
        'quotes and dashes are allowed); otherwise "fail".'
    ),
    "entity_in_window": (
        "identify the entity the question asks about. "
        '"pass" if that entity is named in the same passage as the quote, close to it, so that the '
        'quoted statement is explicitly about that entity; otherwise "fail".'
    ),
    "qualifiers_in_window": (
        "identify the qualifiers of the question (time, place, version, unit, population or other "
        'conditions). "pass" if each of them appears in the same passage as the quote; "fail" if any '
        'is missing there; "not_applicable" if the question has no qualifiers.'
    ),
    "no_closer_competitor": (
        '"fail" if the quoted statement is about another entity (for example a different drug, '
        "product, person or organisation) rather than the entity the question asks about, that is, "
        'if another entity is more closely tied to the quoted statement than the question\'s entity; '
        'otherwise "pass".'
    ),
    "premise_present": (
        "identify what the question presupposes (for example that a study, a use or a relationship "
        'exists). "pass" if the documents state it for the entity the question asks about; "fail" if '
        'they do not; "not_applicable" if the question presupposes nothing beyond the entity itself.'
    ),
}

JUDGE_SYSTEM_PROMPT = (
    "You are a careful verifier. You check whether a quoted piece of evidence really concerns "
    "exactly what a question asks about. You do not judge whether the documents are true. Follow "
    "the instructions exactly and reply with the requested JSON object only."
)


def format_documents(documents: Sequence[Document]) -> str:
    """Documents as the answering model saw them: ``[Document n — source: <id>]`` + text."""
    return "\n\n".join(
        f"[Document {number} — source: {document.id}]\n{document.text}"
        for number, document in enumerate(documents, start=1)
    )


def canonical_checks(checks: Sequence[str] | None) -> tuple[str, ...]:
    """Validated check names in canonical order (all five when ``None``)."""
    if checks is None:
        return CHECK_NAMES
    if isinstance(checks, (str, bytes)) or not isinstance(checks, Sequence) or not checks:
        raise ValueError("checks must be a non-empty list of reserved check names")
    unknown = sorted(set(checks) - set(CHECK_NAMES))
    if unknown:
        raise ValueError(f"unknown check name(s) {unknown}; reserved names: {list(CHECK_NAMES)}")
    if len(set(checks)) != len(checks):
        raise ValueError("checks must not repeat a name")
    return tuple(name for name in CHECK_NAMES if name in set(checks))


def build_check_request(
    question: str, documents: Sequence[Document], candidate: AnswerCandidate, checks: Sequence[str]
) -> ModelRequest:
    """The judge request for one answered candidate (fixed layout, see the module docstring)."""
    if candidate.status is not AnswerStatus.ANSWERED:
        raise ValueError("only answered candidates are judged")
    checks = canonical_checks(checks)
    definitions = "\n".join(f"- {name}: {CHECK_DEFINITIONS[name]}" for name in checks)
    shape = ", ".join(f'"{name}": "{"|".join(sorted(ALLOWED_RESULTS[name]))}"' for name in checks)
    user = (
        f"Question:\n{question}\n\n"
        f"Documents shown to the answering model:\n\n{format_documents(documents)}\n\n"
        "Candidate answer to verify:\n"
        f"- value: {candidate.value}\n"
        f"- quote: {candidate.quote}\n"
        f"- source_id: {candidate.source_id}\n\n"
        "Perform each check below on the candidate answer. Use only the question and the documents "
        "above; do not use outside knowledge.\n\n"
        f"Checks:\n{definitions}\n\n"
        "Reply with exactly one JSON object and nothing else:\n"
        f'{{"checks": {{{shape}}}, "explanation": "<at most two sentences>"}}\n'
        "Every listed check must appear exactly once, with one of the values allowed for it."
    )
    return ModelRequest(messages=(Message("system", JUDGE_SYSTEM_PROMPT), Message("user", user)))


@dataclass(frozen=True)
class ParsedChecks:
    """Outcome of :func:`parse_check_reply`: either ``results`` or ``error`` is set."""

    results: dict[str, str] | None
    explanation: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_check_reply(raw: str, checks: Sequence[str]) -> ParsedChecks:
    """Parse the judge's reply. Pure function of ``raw`` and the requested checks.

    Rules (nothing is repaired):

    1. ``<think>...</think>`` blocks are removed (as the benchmark does for its judge).
    2. Every top-level JSON object with a ``"checks"`` key is collected (text
       around it is ignored). None, or several that are not all equal -> failure.
    3. ``checks`` must be an object with exactly the requested check names.
    4. Each value must be a string; after trimming and case folding it must be
       one of the values allowed for that check (:data:`ALLOWED_RESULTS`).
    5. ``explanation`` is kept if it is a string; any other key is ignored.
    """
    requested = canonical_checks(checks)
    text = strip_think(raw if isinstance(raw, str) else "")
    if not text:
        return ParsedChecks(None, error="empty judge output")
    objects = json_objects_with_key(text, "checks")
    if not objects:
        return ParsedChecks(None, error="no JSON object with a checks field")
    if any(obj != objects[0] for obj in objects[1:]):
        return ParsedChecks(None, error="several different JSON objects with a checks field")
    obj = objects[0]
    results = obj["checks"]
    if not isinstance(results, dict):
        return ParsedChecks(None, error="checks is not an object")
    missing = [name for name in requested if name not in results]
    extra = sorted(set(results) - set(requested))
    if missing or extra:
        return ParsedChecks(None, error=f"checks must be exactly {list(requested)} (missing {missing}, extra {extra})")
    parsed: dict[str, str] = {}
    for name in requested:
        value = results[name]
        if not isinstance(value, str) or value.strip().casefold() not in ALLOWED_RESULTS[name]:
            return ParsedChecks(None, error=f"{name}: {value!r} is not one of {sorted(ALLOWED_RESULTS[name])}")
        parsed[name] = value.strip().casefold()
    explanation = obj.get("explanation")
    return ParsedChecks(parsed, explanation=explanation.strip() if isinstance(explanation, str) else None)


_STATUS = {PASS: CheckStatus.PASS, FAIL: CheckStatus.FAIL, NOT_APPLICABLE: CheckStatus.NOT_APPLICABLE}


def verdict_from_checks(parsed: ParsedChecks, judge_label: str, judge_model: str) -> Verdict:
    """``accept`` if no check failed, else ``abstain`` with one reason per failed check."""
    assert parsed.results is not None
    details = {"performed_by": "llm_judge", "judge": judge_label, "judge_model": judge_model}
    if parsed.explanation is not None:
        details["explanation"] = parsed.explanation
    results = tuple(CheckResult(name, _STATUS[value], details) for name, value in parsed.results.items())
    failed = [r.check for r in results if r.failed]
    if not failed:
        return Verdict(Decision.ACCEPT, check_results=results)
    return Verdict(
        Decision.ABSTAIN,
        reasons=tuple(f"{name} failed (judged by {judge_label})" for name in failed),
        check_results=results,
    )


# Registered only when imported as a module, not when run with ``python -m`` (the command
# below then runs the imported module, so the component is registered exactly once).
@register("arm", "llm_judge_checks") if __name__ != "__main__" else (lambda component: component)
class LlmJudgeChecksArm:
    """Options: ``judges`` (ordered list of ``{name, label, options}``, required);
    ``judge_max_usd`` (hard cap in US dollars, required); ``checks`` (default all
    five reserved names); ``presentation``; ``prices`` (default ``configs/prices.toml``)."""

    name = "llm_judge_checks"

    def __init__(
        self,
        *,
        model: ModelClient,
        judges: Sequence[dict[str, Any]] | None = None,
        judge_max_usd: float | None = None,
        checks: Sequence[str] | None = None,
        presentation: str = DEFAULT_PRESENTATION,
        prices: str = "configs/prices.toml",
    ):
        if presentation not in PRESENTATIONS:
            raise ValueError(f"unknown presentation {presentation!r}; available: {sorted(PRESENTATIONS)}")
        if judges is None:
            raise ValueError("llm_judge_checks needs options.judges (see PREREGISTRATION.md section 9, slot J)")
        if isinstance(judge_max_usd, bool) or not isinstance(judge_max_usd, (int, float)) or judge_max_usd <= 0:
            raise ValueError("llm_judge_checks needs options.judge_max_usd, a budget cap in US dollars > 0")
        if not isinstance(prices, str) or not prices:
            raise ValueError("prices must be a path")
        self.model = model
        self.presentation = presentation
        self.checks = canonical_checks(checks)
        self.judge_max_usd = float(judge_max_usd)
        self.prices = prices
        self.judges = tuple((spec, spec.build()) for spec in parse_judges("judges", judges))
        self.under_test_family = model_family(model)
        # Fail before any call when the rule is already known to be violated. A known family
        # means a real provider client (the dry run's recording client has none), so a missing
        # judge key is reported now instead of turning every judged item into an error record.
        self._choice = None
        if self.under_test_family is not None:
            self._choice = choose_judge(self.judges, self.under_test_family)
            secret = getattr(self._choice[1], "secret_name", "")
            if secret and not secrets.has_secret(secret):
                raise JudgeUnavailable(
                    f"judge {self._choice[0].label!r} needs the environment variable {secret}, which is not set"
                )

    def _judge(self, calls: Sequence[Any]) -> tuple[Any, Any]:
        if self._choice is not None:
            return self._choice
        try:
            return choose_judge(self.judges, self.under_test_family)
        except JudgeError as exc:
            raise type(exc)(str(exc), calls) from None

    def _budget(self, judge: Any) -> JudgeBudget:
        model_id = str(getattr(judge, "model_id", judge.name))

        def make() -> JudgeBudget:
            price_in, price_out, chars_per_token = judge_price(Path(self.prices), model_id)
            return JudgeBudget(
                max_usd=self.judge_max_usd,
                input_per_mtok=price_in,
                output_per_mtok=price_out,
                max_output_tokens=int(getattr(judge, "max_output_tokens", 0) or 0),
                chars_per_token=chars_per_token,
            )

        return shared_budget((self.name, judge.name, model_id, self.judge_max_usd, self.prices), make)

    def run(self, arm_input: ArmInput) -> ArmOutput:
        request = build_answer_request(arm_input, self.presentation)  # identical to no_gate -> cache hit
        response, calls = call_model(self.model, request, "answer")
        answer = parse_answer(response.text)
        evidence = tuple(d.ref() for d in arm_input.documents)
        if answer.status is not AnswerStatus.ANSWERED:
            return ArmOutput(attempts=(Attempt(answer, evidence),), final_answer=answer, model_calls=calls)

        spec, judge = self._judge(calls)
        try:
            preflight_once(judge)
            budget = self._budget(judge)
        except JudgeError as exc:
            raise type(exc)(str(exc), calls) from None
        except Exception as exc:  # noqa: BLE001 - e.g. missing key or price: keep the answer call in the record
            raise JudgeUnavailable(f"judge not usable: {type(exc).__name__}: {exc}", calls) from exc
        budget.ensure_available(calls)
        judge_request = build_check_request(arm_input.question, arm_input.documents, answer, self.checks)
        try:
            judge_response, judge_calls = call_model(judge, judge_request, "check")
        except ModelCallFailed as exc:
            budget.charge(exc.model_calls)
            raise ModelCallFailed(f"judge call failed: {exc}", tuple(calls) + exc.model_calls) from exc
        budget.charge(judge_calls)
        all_calls = tuple(calls) + tuple(judge_calls)

        if judge_response.raw.get("truncated") is True:
            raise JudgeOutputUnparseable("judge output was truncated (max_output_tokens reached)", all_calls)
        parsed = parse_check_reply(judge_response.text, self.checks)
        if not parsed.ok:
            raise JudgeOutputUnparseable(f"judge output unparseable: {parsed.error}", all_calls)
        verdict = verdict_from_checks(parsed, spec.label, str(getattr(judge, "model_id", judge.name)))
        if verdict.decision is Decision.ACCEPT:
            final = answer
        else:
            final = AnswerCandidate.abstained(raw="", reason=f"blocked by {self.name}: {'; '.join(verdict.reasons)}")
        attempt = Attempt(answer=answer, evidence=evidence, verdict=verdict)
        return ArmOutput(attempts=(attempt,), final_answer=final, model_calls=all_calls)


# --------------------------------------------------------------------------- #
# Upper bound of the judge cost of a run config (no model is called)
# --------------------------------------------------------------------------- #

PLACEHOLDER_CHARS = 300
"""Length assumed for the candidate's value and for its quote in the estimate."""


def estimate_judge_cost(config_path: str | Path, repo_root: str | Path = ".") -> list[dict[str, Any]]:
    """Upper bound of the judge cost of every ``llm_judge_checks`` arm in a run config.

    Assumes that EVERY candidate is answered (the real number of judge calls is
    the number of answered candidates, which is smaller). Prompt sizes are
    computed from the real dev items with a placeholder candidate of
    :data:`PLACEHOLDER_CHARS` characters per field; token and price
    assumptions come from the run's price file.
    """
    from binding_gate.config import load_config
    from binding_gate.pricing import load_prices
    from binding_gate.runner import RunnerSettings, load_plan

    config = load_config(config_path)
    repo_root = Path(repo_root)
    book = load_prices(repo_root / RunnerSettings.from_config(config).prices)
    plan = load_plan(config, repo_root=repo_root)
    placeholder = AnswerCandidate.answered(
        value="v" * PLACEHOLDER_CHARS, quote="q" * PLACEHOLDER_CHARS, source_id="placeholder", raw=""
    )
    out = []
    for spec in config.arms:
        if spec.name != "llm_judge_checks":
            continue
        checks = canonical_checks(spec.options.get("checks"))
        judge_spec = parse_judges("judges", spec.options.get("judges"))[0]
        judge = judge_spec.build()
        model_id = str(getattr(judge, "model_id", judge.name))
        chars = sum(
            sum(len(m.content) for m in build_check_request(i.question, i.documents, placeholder, checks).messages)
            for i in plan.items
        )
        calls = len(plan.items) * config.repeats * len(config.models)
        input_tokens = book.assumptions.input_tokens(chars) * config.repeats * len(config.models)
        price = book.get(model_id)
        visible = book.assumptions.visible_output_tokens
        max_output = int(getattr(judge, "max_output_tokens", 0) or 0)
        out.append(
            {
                "arm_label": spec.label,
                "judge": judge_spec.label,
                "judge_model_id": model_id,
                "max_judge_calls": calls,
                "input_tokens": input_tokens,
                "cost_low": price.cost(input_tokens, calls * (visible + book.assumptions.reasoning_tokens_low)),
                "cost_high": price.cost(input_tokens, calls * (visible + book.assumptions.reasoning_tokens_high)),
                "cost_ceiling": price.cost(input_tokens, calls * max_output),
                "judge_max_usd": spec.options.get("judge_max_usd"),
            }
        )
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m binding_gate.arms.llm_judge_checks",
        description="Upper bound of the judge cost of a run config. Calls no API, needs no key.",
    )
    parser.add_argument("command", choices=["estimate"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    try:
        rows = estimate_judge_cost(args.config, args.repo_root)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("JUDGE COST UPPER BOUND - assumes every candidate is answered; nothing was called.")
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    from binding_gate.arms.llm_judge_checks import main as _main

    sys.exit(_main())
