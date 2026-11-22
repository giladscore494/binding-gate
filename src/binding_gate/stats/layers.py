"""Each layer analysed with its own pre-registered rule (PR 16; PREREGISTRATION.md sections 7, 8, 11, 12).

One analysis covers one layer. The layer of a run directory is the layer of the
dataset in its manifest (``prereg.LAYER_BY_DATASET``); run directories of
different layers given together are refused.

* layer 1, ``deceptive_grounding``: outcomes from ``provenance``; decides H1-H3;
* layer 2, ``grab_rag``: outcomes from ``grab_rag_answer``; metrics reported,
  no decision (no pass/fail threshold);
* layer 3, ``sealqa``: outcomes from ``sealqa_answer``; decides H4 (M3), with
  M4 reported.

Layer 1 is ``binding_gate.stats.analysis.analyse_run_dirs`` unchanged (its
output is byte-identical to what it was before PR 16); layer 2 is
``binding_gate.scorers.grab_rag_answer.analyse_layer2``; layer 3 is
``binding_gate.scorers.sealqa_answer.analyse_layer3``. Used by ``cli analyze``
and by the frozen test run, so both give the same numbers for the same traces.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from binding_gate.stats import prereg
from binding_gate.stats.bootstrap import StatsError

LAYER3_ANALYZE_HINT = "python -m binding_gate.scorers.sealqa_answer analyze RUN_DIR [RUN_DIR ...]"


@dataclass(frozen=True)
class LayerRule:
    layer: int
    dataset: str
    scorer: str
    outcome_rule: str
    decision: str


LAYER_RULES: Mapping[int, LayerRule] = {
    1: LayerRule(1, "deceptive_grounding", "provenance", "provenance", "H1-H3 (section 11)"),
    2: LayerRule(2, "grab_rag", "grab_rag_answer", "grab_rag_answer (layer 2)",
                 "none: reporting only, no pass/fail threshold (section 11)"),
    3: LayerRule(3, "sealqa", "sealqa_answer", "sealqa_answer (layer 3)", "H4 (section 11)"),
}


class LayerError(StatsError):
    """Raised when run directories cannot be analysed as one layer."""


def layer_of_dataset(dataset: str) -> int:
    layer = prereg.LAYER_BY_DATASET.get(dataset)
    if layer is None:
        raise LayerError(f"dataset {dataset!r} belongs to no pre-registered layer ({sorted(prereg.LAYER_BY_DATASET)})")
    return layer


def run_datasets(run_dirs: Sequence[str | Path]) -> dict[str, str]:
    """``run dir -> dataset name`` from each run manifest's config (unreadable manifests are left out;
    the analysis itself then reports them)."""
    from binding_gate.trace import TraceError, read_manifest

    found: dict[str, str] = {}
    for run_dir in run_dirs:
        try:
            dataset = read_manifest(run_dir).config.get("dataset", {})
        except (TraceError, ValueError, OSError):
            continue
        name = dataset.get("name") if isinstance(dataset, dict) else None
        if isinstance(name, str):
            found[str(run_dir)] = name
    return found


def layer_of_run_dirs(run_dirs: Sequence[str | Path]) -> int | None:
    """The one layer of ``run_dirs`` (``None`` when no manifest names a dataset); refuses mixed layers."""
    datasets = run_datasets(run_dirs)
    by_layer: dict[int | None, list[str]] = {}
    for run_dir, name in datasets.items():
        by_layer.setdefault(prereg.LAYER_BY_DATASET.get(name), []).append(run_dir)
    if None in by_layer:
        raise LayerError(
            f"run directories {sorted(by_layer[None])} hold a dataset of no pre-registered layer; "
            "analyse each dataset separately"
        )
    if len(by_layer) > 1:
        described = "; ".join(f"layer {layer}: {sorted(dirs)}" for layer, dirs in sorted(by_layer.items()))  # type: ignore[arg-type]
        hint = f" (layer 3: {LAYER3_ANALYZE_HINT})" if 3 in by_layer else ""
        raise LayerError(
            f"run directories of different layers were given together ({described}); "
            f"analyse each dataset separately{hint}"
        )
    return next(iter(by_layer), None)


def _analyser(layer: int | None) -> Callable[..., dict[str, Any]]:
    if layer == 2:
        from binding_gate.scorers.grab_rag_answer import analyse_layer2

        return analyse_layer2
    if layer == 3:
        from binding_gate.scorers.sealqa_answer import analyse_layer3

        return analyse_layer3
    from binding_gate.stats.analysis import analyse_run_dirs

    return analyse_run_dirs


def analyse_layer(
    run_dirs: Sequence[str | Path], *, expected_layer: int | None = None, **kwargs: Any
) -> tuple[dict[str, Any], LayerRule | None]:
    """Analyse ``run_dirs`` with the rule of their layer; returns ``(analysis, rule)``.

    ``expected_layer`` (the frozen run passes the layer of its config) makes a run
    directory of another layer an error instead of a silent switch of rule.
    """
    layer = layer_of_run_dirs(run_dirs)
    if expected_layer is not None and layer != expected_layer:
        raise LayerError(f"expected run directories of layer {expected_layer}, found layer {layer}")
    analysis = _analyser(layer)(run_dirs, **kwargs)
    if layer is not None and analysis.get("layer") != layer:  # pragma: no cover - manifests and traces disagree
        raise LayerError(f"the manifests say layer {layer} but the traces are layer {analysis.get('layer')}")
    return analysis, LAYER_RULES.get(layer) if layer is not None else None
