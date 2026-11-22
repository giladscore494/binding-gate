"""Decomposer ``model_decomposer``: gate function (A) with a model, a fixed prompt and a strict parser.

``decompose(question)`` sends the fixed prompt of
:mod:`binding_gate.decomposers.decomposition_format` to the decomposer's own
model client (never the model under test), parses the reply with the
deterministic parser and returns a :class:`~binding_gate.contracts.Decomposition`
whose ``model_calls`` hold every logged call (``purpose = "decompose"``).

Failures are explicit, never repaired:

* the reply breaks the output format -> :class:`DecompositionFailed`
  (``reason``, ``raw``, ``model_calls``);
* the call failed after the client's retries ->
  :class:`binding_gate.answering.ModelCallFailed` (``model_calls``).

Both carry ``model_calls``, so a caller that lets them propagate gets an error
record with the calls in the trace (the runner reads ``exc.model_calls``).
Retries happen only in :func:`binding_gate.answering.call_model`, under the
decomposer client's ``retry`` policy, exactly as for answer calls.

Options (``{name = "model_decomposer", options = {...}}``)
---------------------------------------------------------
``model``           REQUIRED. The decomposer's model client as a nested component
                    ``{name = "<model component>", label = "<trace label>",
                    options = {...}}``; built with ``registry.create("model", ...)``.
                    ``label`` defaults to ``"decomposer"``.
``prompt_version``  prompt version (default and only version: 1).
``cache``           ``"process"`` (default): one decomposition per question per
                    run, shared by every decomposer instance with the same
                    configuration (see :mod:`binding_gate.decomposers.cache`).
                    ``"none"``: every call asks the model (used by the separate
                    accuracy measurement, which needs independent repeats).
                    Not part of the configuration hash.

How the gated arm (PR 4) uses it -- through the registry only
---------------------------------------------------------------
::

    spec = options["decomposer"]          # {name = "model_decomposer", options = {...}}
    decomposer = registry.create("decomposer", spec["name"], **spec.get("options", {}))
    decomposition = decomposer.decompose(arm_input.question)

PR 4 must not import this module. Anything it needs is on the object
(``name``, ``config_hash``, ``client``) or on the exceptions (``model_calls``).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Sequence

from binding_gate import registry
from binding_gate.answering import call_model
from binding_gate.canonical import canonical_json, sha256_json
from binding_gate.contracts import Decomposition, ModelCall, ModelRequest
from binding_gate.decomposers.cache import DecompositionCache, as_cache_hit, shared_cache
from binding_gate.decomposers.decomposition_format import (
    PROMPT_VERSION,
    build_decompose_request,
    parse_decomposition,
    prompt_sha256,
)
from binding_gate.registry import register

DECOMPOSER_NAME = "model_decomposer"
PURPOSE = "decompose"
DEFAULT_MODEL_LABEL = "decomposer"
CACHE_MODES = ("process", "none")
_MODEL_SPEC_KEYS = {"name", "label", "options"}


class DecompositionFailed(RuntimeError):
    """The decomposer's reply could not be parsed. Carries the reply and every logged call."""

    def __init__(self, reason: str, *, raw: str, model_calls: Sequence[ModelCall], cached: bool = False):
        super().__init__(f"decomposition failed: {reason}")
        self.reason = reason
        self.raw = raw
        self.model_calls = tuple(model_calls)
        self.cached = cached


def normalise_model_spec(model: Any) -> dict[str, Any]:
    """Validate the nested model component and fill in the default label."""
    if not isinstance(model, Mapping):
        raise ValueError(
            "decomposer option 'model' must be a table {name = ..., label = ..., options = {...}}"
        )
    unknown = sorted(set(model) - _MODEL_SPEC_KEYS)
    if unknown:
        raise ValueError(f"decomposer option 'model': unknown key(s) {unknown}")
    name = model.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("decomposer option 'model.name' must be a non-empty string")
    label = model.get("label", DEFAULT_MODEL_LABEL)
    if not isinstance(label, str) or not label:
        raise ValueError("decomposer option 'model.label' must be a non-empty string")
    options = model.get("options", {})
    if not isinstance(options, Mapping):
        raise ValueError("decomposer option 'model.options' must be a table")
    try:
        canonical_json(options)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"decomposer option 'model.options' must be JSON-compatible: {exc}") from exc
    return {"name": name, "label": label, "options": dict(options)}


def decomposer_config_hash(model_spec: Mapping[str, Any], prompt_version: int = PROMPT_VERSION) -> str:
    """Hash of everything that changes what the decomposer produces (cache key part)."""
    return sha256_json(
        {
            "decomposer": DECOMPOSER_NAME,
            "prompt_version": prompt_version,
            "prompt_sha256": prompt_sha256(prompt_version),
            "model": normalise_model_spec(model_spec),
        }
    )


@register("decomposer", DECOMPOSER_NAME)
class ModelDecomposer:
    """See the module docstring for options and behaviour."""

    name = DECOMPOSER_NAME

    def __init__(
        self,
        *,
        model: Mapping[str, Any],
        prompt_version: int = PROMPT_VERSION,
        cache: str = "process",
        sleep: Callable[[float], None] = time.sleep,
    ):
        if isinstance(prompt_version, bool) or not isinstance(prompt_version, int):
            raise ValueError("prompt_version must be an integer")
        prompt_sha256(prompt_version)  # raises for an unknown version
        if cache not in CACHE_MODES:
            raise ValueError(f"cache must be one of {list(CACHE_MODES)}, got {cache!r}")
        self.model_spec = normalise_model_spec(model)
        self.prompt_version = prompt_version
        self.config_hash = decomposer_config_hash(self.model_spec, prompt_version)
        self.client = registry.create(
            "model", self.model_spec["name"], label=self.model_spec["label"], **self.model_spec["options"]
        )
        self.cache_mode = cache
        self.cache: DecompositionCache | None = shared_cache(self.config_hash) if cache == "process" else None
        self._sleep = sleep

    def describe(self) -> dict[str, Any]:
        """What this decomposer is, for manifests and reports. Contains no secret."""
        return {
            "name": self.name,
            "config_hash": self.config_hash,
            "prompt_version": self.prompt_version,
            "prompt_sha256": prompt_sha256(self.prompt_version),
            "model": self.model_spec,
            "cache": self.cache_mode,
        }

    def request_for(self, question: str) -> ModelRequest:
        """The exact request ``decompose`` sends for ``question`` (used by dry runs; calls nothing)."""
        return build_decompose_request(question, self.prompt_version)

    def decompose(self, question: str) -> Decomposition:
        """Constraints for ``question``; raises :class:`DecompositionFailed` or ``ModelCallFailed``."""
        if not isinstance(question, str) or not question.strip():
            raise ValueError("the question must be a non-empty string")
        if self.cache is None:
            return self._decompose_now(question)
        return self.cache.get_or_compute(
            question,
            self._decompose_now,
            cacheable_errors=(DecompositionFailed,),
            rebuild_error=_rebuild_cached_failure,
        )

    def _decompose_now(self, question: str) -> Decomposition:
        request = self.request_for(question)
        response, calls = call_model(self.client, request, PURPOSE, sleep=self._sleep)
        parsed = parse_decomposition(response.text, question)
        if parsed.constraints is None:
            raise DecompositionFailed(parsed.reason or "unparseable", raw=response.text, model_calls=calls)
        return Decomposition(constraints=parsed.constraints, model_calls=calls)


def _rebuild_cached_failure(exc: BaseException) -> BaseException:
    assert isinstance(exc, DecompositionFailed)
    return DecompositionFailed(exc.reason, raw=exc.raw, model_calls=as_cache_hit(exc.model_calls), cached=True)
