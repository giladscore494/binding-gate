"""binding-gate: a deterministic verification gate against entity-attribution failures.

The package is organised so that every later pull request adds files in its own
directory (see ARCHITECTURE.md). This top-level module only exposes versions.
"""

from binding_gate.contracts import SCHEMA_VERSION

__version__ = "0.1.0"

__all__ = ["SCHEMA_VERSION", "__version__"]
