"""Versioned run configuration.

A run is fully described by one TOML file under ``configs/``, chosen per run by
its path. No experiment setting is read from environment variables (only secrets
are; see ``binding_gate.secrets``).

File format (``config_version = 1``)::

    config_version = 1
    name = "dev-baseline"            # human-readable run name
    description = "optional text"
    split = "dev"                    # "dev" or "test"
    repeats = 3                      # runs per item x arm x model
    limit = 20                       # optional, dev only: first N groups (smoke runs)

    [dataset]
    name = "deceptive_grounding"     # registry name of a dataset adapter
    [dataset.options]                # passed to the adapter factory

    [[models]]
    name = "some_client"             # registry name of a model client
    label = "model-a"                # optional; defaults to name; written to traces
    [models.options]

    [[arms]]
    name = "no_gate"
    [arms.options]

    [[scorers]]
    name = "provenance"

The config hash is the SHA-256 of the canonical JSON of the *parsed and
normalised* config (defaults filled in), so comments, key order and whitespace do
not change it, while any change of meaning does. The hash is written to the run
manifest and to every trace record.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from binding_gate.canonical import canonical_json, sha256_json
from binding_gate.contracts import Split

CONFIG_VERSION = 1

_TOP_LEVEL_KEYS = {
    "config_version",
    "name",
    "description",
    "split",
    "repeats",
    "limit",
    "dataset",
    "models",
    "arms",
    "scorers",
}
_COMPONENT_KEYS = {"name", "label", "options"}


class ConfigError(ValueError):
    """Raised when a run config is malformed."""


@dataclass(frozen=True)
class ComponentSpec:
    """A component chosen by registry ``name`` with its ``options``.

    ``label`` identifies this configured instance in traces; it defaults to
    ``name`` and must be unique among the arms (and among the models) of a run,
    which is how two ablations of the same arm are told apart.
    """

    name: str
    label: str
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "options": self.options}


@dataclass(frozen=True)
class RunConfig:
    """A validated run configuration. Build with :func:`load_config` or :func:`parse_config`."""

    name: str
    split: Split
    dataset: ComponentSpec
    models: tuple[ComponentSpec, ...]
    arms: tuple[ComponentSpec, ...]
    scorers: tuple[ComponentSpec, ...]
    repeats: int
    limit: int | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """The normalised config; this exact structure is what gets hashed."""
        return {
            "config_version": CONFIG_VERSION,
            "name": self.name,
            "description": self.description,
            "split": self.split.value,
            "repeats": self.repeats,
            "limit": self.limit,
            "dataset": self.dataset.to_dict(),
            "models": [m.to_dict() for m in self.models],
            "arms": [a.to_dict() for a in self.arms],
            "scorers": [s.to_dict() for s in self.scorers],
        }

    @property
    def config_hash(self) -> str:
        return sha256_json(self.to_dict())


def _component(where: str, raw: Any) -> ComponentSpec:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where} must be a table")
    unknown = sorted(set(raw) - _COMPONENT_KEYS)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{where}.name must be a non-empty string")
    label = raw.get("label", name)
    if not isinstance(label, str) or not label:
        raise ConfigError(f"{where}.label must be a non-empty string")
    options = raw.get("options", {})
    if not isinstance(options, Mapping):
        raise ConfigError(f"{where}.options must be a table")
    try:
        canonical_json(options)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{where}.options must contain only JSON-compatible values "
            f"(no dates, NaN or infinity): {exc}"
        ) from exc
    return ComponentSpec(name=name, label=label, options=dict(options))


def _components(where: str, raw: Any, *, required: bool) -> tuple[ComponentSpec, ...]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ConfigError(f"{where} must be an array of tables ([[{where}]])")
    specs = tuple(_component(f"{where}[{i}]", item) for i, item in enumerate(raw))
    if required and not specs:
        raise ConfigError(f"at least one [[{where}]] entry is required")
    labels = [s.label for s in specs]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        raise ConfigError(f"{where}: labels must be unique, duplicated: {duplicates}")
    return specs


def _positive_int(where: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where} must be an integer >= 1")
    return value


def parse_config(raw: Mapping[str, Any]) -> RunConfig:
    """Validate an already-parsed config mapping."""
    unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(f"unknown top-level key(s) {unknown}")
    if raw.get("config_version") != CONFIG_VERSION:
        raise ConfigError(
            f"config_version must be {CONFIG_VERSION}, got {raw.get('config_version')!r}"
        )
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError("name must be a non-empty string")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ConfigError("description must be a string")
    try:
        split = Split(raw.get("split"))
    except ValueError:
        raise ConfigError(f"split must be 'dev' or 'test', got {raw.get('split')!r}") from None
    repeats = _positive_int("repeats", raw.get("repeats"))
    limit = raw.get("limit")
    if limit is not None:
        limit = _positive_int("limit", limit)
        if split is Split.TEST:
            raise ConfigError("limit is not allowed on the test split: test runs are always complete")
    if "dataset" not in raw:
        raise ConfigError("a [dataset] table is required")
    return RunConfig(
        name=name,
        description=description,
        split=split,
        repeats=repeats,
        limit=limit,
        dataset=_component("dataset", raw["dataset"]),
        models=_components("models", raw.get("models"), required=True),
        arms=_components("arms", raw.get("arms"), required=True),
        scorers=_components("scorers", raw.get("scorers"), required=False),
    )


def load_config(path: str | Path) -> RunConfig:
    """Read and validate a TOML run config."""
    path = Path(path)
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    try:
        return parse_config(raw)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None
