"""Canonical JSON and hashing helpers shared by config hashing, traces and logs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialise ``value`` to a canonical JSON string.

    Keys are sorted, no insignificant whitespace is emitted and non-ASCII
    characters are kept as-is, so the same data always yields the same bytes
    (UTF-8). Raises ``TypeError``/``ValueError`` for values JSON cannot represent
    (including NaN and infinity).
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_text(text: str) -> str:
    """Hex SHA-256 of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    """Hex SHA-256 of the canonical JSON form of ``value``."""
    return sha256_text(canonical_json(value))


def utc_now_iso() -> str:
    """Current time as an ISO-8601 UTC timestamp with second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
