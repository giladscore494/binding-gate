"""The run context that the runner hands to components that need the run directory.

Live-web arms (``gated_research``, ``no_gate_live``) store every search result
and every fetched page under ``runs/<run_id>/snapshots/`` (PREREGISTRATION.md
section 14), so they need to know the run directory, which does not exist when
the config is written. The runner therefore activates a :class:`RunContext`
while it builds the arms::

    with activate(RunContext(run_dir=writer.run_dir)):
        arms = build_arms(...)

and an arm that needs it reads :func:`current` in its constructor. Arms are
built in the runner's main thread before any unit runs, so the value is
captured once per arm instance; nothing reads it later from worker threads.

A dry run activates ``RunContext(run_dir=None, dry_run=True)``: live-web arms
then use stand-ins that make no network request and return worst-case sized
results, so that every request can be counted and priced.

Components built outside the runner (tests, notebooks) either activate a
context themselves or receive their services directly.
"""

from __future__ import annotations

import contextlib
import contextvars
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class RunContext:
    """What the runner tells components about the run being executed."""

    run_dir: Path | None
    dry_run: bool = False
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    """Identifies one runner invocation, so that per-run caches are never shared between runs."""

    def __post_init__(self) -> None:
        if not self.dry_run and self.run_dir is None:
            raise ValueError("a run context for a real run needs the run directory")


_CURRENT: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar("binding_gate_run_context", default=None)


def current() -> RunContext | None:
    """The active run context, or ``None`` outside the runner."""
    return _CURRENT.get()


@contextlib.contextmanager
def activate(context: RunContext) -> Iterator[RunContext]:
    """Make ``context`` the active run context inside the ``with`` block."""
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)
