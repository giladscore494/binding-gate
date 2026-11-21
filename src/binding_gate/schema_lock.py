"""Fingerprint of the contracts for each released schema version.

How to change a contract
------------------------
1. Edit ``contracts.py``.
2. Increase ``contracts.SCHEMA_VERSION`` by one.
3. Add a NEW line below mapping the new version to the new fingerprint
   (``python -c "from binding_gate.contracts import schema_fingerprint as f; print(f())"``).
   Never edit or delete an existing line: reviewers reject pull requests that do.
4. Describe the change and any migration in ARCHITECTURE.md ("Contract change log").

``tests/test_schema_version.py`` fails when the contracts no longer match the
fingerprint recorded for the current version.
"""

from __future__ import annotations

SCHEMA_FINGERPRINTS: dict[int, str] = {
    1: "8291a946d0ca3669e2a77eea819809eb754b9f8003afb9f5c8b3c6995db238d3",
}
