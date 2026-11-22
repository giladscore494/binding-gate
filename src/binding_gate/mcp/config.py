"""Server-level configuration of the MCP server, read from ONE TOML file.

The path is given on the command line (``--config``); without it the defaults
below apply. Nothing here is read from environment variables: the only
environment variables the server ever reads are API keys, through
``binding_gate.secrets``, and only when a decomposer is configured and called.

Format (every table is optional; unknown keys are errors)::

    config_version = 1                   # required when a file is given

    [server]
    name = "binding-gate"                # server name reported to MCP clients

    [limits]                             # input size limits (see Limits)
    max_documents = 50
    max_document_chars = 200000
    ...

    [decomposer]                         # optional; enables decompose_question and
    name = "model_decomposer"            # verify_answer calls without constraints.
    [decomposer.options]                 # Same format as options.decomposer of the
    prompt_version = 1                   # gated arm in a run config.
    [decomposer.options.model]
    name = "anthropic_messages"
    ...

Gate parameters and the list of checks are deliberately NOT part of this file:
their defaults are the PROPOSED values in ``binding_gate.gates.binding``
(PREREGISTRATION.md section 5.1), and a caller who wants other values (an
ablation, say) passes them per request, where they are echoed in the output.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from binding_gate.arms.gated import decomposer_spec

CONFIG_VERSION = 1
DEFAULT_SERVER_NAME = "binding-gate"
_TOP_LEVEL_KEYS = {"config_version", "server", "limits", "decomposer"}
_SERVER_KEYS = {"name"}


class ServerConfigError(ValueError):
    """The server configuration file is missing, malformed or invalid."""


@dataclass(frozen=True)
class Limits:
    """Size limits on tool inputs. Every value is a positive integer.

    Characters are Python string characters (Unicode code points). Inputs over a
    limit are refused with an error that names the field and the limit; nothing
    is truncated silently.
    """

    max_documents: int = 50
    """Documents per verify_answer call."""
    max_document_chars: int = 200_000
    """Characters of one document's text."""
    max_total_document_chars: int = 1_000_000
    """Characters of all documents' texts of one call together."""
    max_question_chars: int = 4_000
    """Characters of the question, the answer, the quote and a presupposition text."""
    max_id_chars: int = 200
    """Characters of a document id and of source_id."""
    max_url_chars: int = 2_048
    """Characters of a document url (stored, never fetched)."""
    max_term_chars: int = 500
    """Characters of an entity name, alias, kind, attribute, qualifier text or key term."""
    max_list_items: int = 50
    """Items of each list inside the constraints (aliases, qualifiers, presuppositions, key terms)."""

    def __post_init__(self) -> None:
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ServerConfigError(f"limits.{f.name} must be an integer >= 1, got {value!r}")

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class ServerConfig:
    """Parsed server configuration. ``decomposer`` is ``None`` when none is configured."""

    name: str = DEFAULT_SERVER_NAME
    limits: Limits = field(default_factory=Limits)
    decomposer: dict[str, Any] | None = None
    source: str | None = None
    """Path of the file this was read from (for messages only), or ``None`` for defaults."""


def parse_server_config(data: Mapping[str, Any], *, source: str | None = None) -> ServerConfig:
    """Validate the parsed TOML of a server configuration file."""
    where = f" in {source}" if source else ""
    if not isinstance(data, Mapping):
        raise ServerConfigError(f"the server configuration{where} must be a TOML table")
    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ServerConfigError(f"unknown top-level key(s) {unknown}{where}; allowed: {sorted(_TOP_LEVEL_KEYS)}")
    version = data.get("config_version")
    if isinstance(version, bool) or version != CONFIG_VERSION:
        raise ServerConfigError(f"config_version must be {CONFIG_VERSION}{where}, got {version!r}")

    server = data.get("server", {})
    if not isinstance(server, Mapping):
        raise ServerConfigError(f"[server] must be a table{where}")
    unknown = sorted(set(server) - _SERVER_KEYS)
    if unknown:
        raise ServerConfigError(f"[server]: unknown key(s) {unknown}{where}; allowed: {sorted(_SERVER_KEYS)}")
    name = server.get("name", DEFAULT_SERVER_NAME)
    if not isinstance(name, str) or not name.strip():
        raise ServerConfigError(f"server.name must be a non-empty string{where}")

    raw_limits = data.get("limits", {})
    if not isinstance(raw_limits, Mapping):
        raise ServerConfigError(f"[limits] must be a table{where}")
    allowed_limits = {f.name for f in dataclasses.fields(Limits)}
    unknown = sorted(set(raw_limits) - allowed_limits)
    if unknown:
        raise ServerConfigError(f"[limits]: unknown key(s) {unknown}{where}; allowed: {sorted(allowed_limits)}")
    limits = Limits(**dict(raw_limits))

    decomposer = None
    if "decomposer" in data:
        try:
            decomposer = decomposer_spec(data["decomposer"])
        except (TypeError, ValueError) as exc:
            raise ServerConfigError(f"[decomposer]{where}: {exc}") from exc

    return ServerConfig(name=name, limits=limits, decomposer=decomposer, source=source)


def load_server_config(path: str | Path | None) -> ServerConfig:
    """Read and validate the TOML file at ``path``; ``None`` gives the defaults (no decomposer)."""
    if path is None:
        return ServerConfig()
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ServerConfigError(f"cannot read the server configuration {str(path)!r}: {exc.strerror or exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ServerConfigError(f"{path} is not valid TOML: {exc}") from exc
    return parse_server_config(data, source=str(path))
