"""Component registration without a shared file.

Each component kind has its own sub-package (``binding_gate.checks``,
``binding_gate.arms``, ...). A pull request adds a NEW module to the relevant
sub-package and decorates its factory::

    # src/binding_gate/checks/quote_verbatim.py
    from binding_gate.registry import register

    @register("check", "quote_verbatim")
    class QuoteVerbatim:
        name = "quote_verbatim"
        def __init__(self, **options): ...
        def check(self, constraints, answer, documents): ...

Nothing else has to be edited: :func:`create` imports every module of the
sub-package on first use, which runs the decorators. Two factories registering
the same name is an error, so parallel pull requests cannot silently shadow each
other.

A factory is any callable returning a component: ``factory(**options)``. Options
come from the run config (see ``binding_gate.config.ComponentSpec``); the caller
may pass extra keyword arguments such as an already-built model client.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Callable, TypeVar

KIND_PACKAGES: dict[str, str] = {
    "dataset": "binding_gate.datasets",
    "model": "binding_gate.models",
    "decomposer": "binding_gate.decomposers",
    "check": "binding_gate.checks",
    "gate": "binding_gate.gates",
    "arm": "binding_gate.arms",
    "scorer": "binding_gate.scorers",
}

_F = TypeVar("_F", bound=Callable[..., Any])

_factories: dict[str, dict[str, Callable[..., Any]]] = {kind: {} for kind in KIND_PACKAGES}
_discovered: set[str] = set()


class RegistryError(LookupError):
    """Raised for unknown kinds, unknown names and duplicate registrations."""


def _check_kind(kind: str) -> None:
    if kind not in KIND_PACKAGES:
        raise RegistryError(f"unknown component kind {kind!r}; known: {sorted(KIND_PACKAGES)}")


def register(kind: str, name: str) -> Callable[[_F], _F]:
    """Class/function decorator that registers a factory under ``(kind, name)``."""
    _check_kind(kind)
    if not isinstance(name, str) or not name:
        raise RegistryError("component name must be a non-empty string")

    def decorator(factory: _F) -> _F:
        existing = _factories[kind].get(name)
        if existing is not None and existing is not factory:
            raise RegistryError(
                f"{kind} {name!r} is already registered by "
                f"{existing.__module__}.{getattr(existing, '__qualname__', existing)}"
            )
        _factories[kind][name] = factory
        return factory

    return decorator


def discover(kind: str) -> None:
    """Import every module of the kind's sub-package (idempotent)."""
    _check_kind(kind)
    if kind in _discovered:
        return
    package = importlib.import_module(KIND_PACKAGES[kind])
    for module in pkgutil.iter_modules(package.__path__, prefix=package.__name__ + "."):
        importlib.import_module(module.name)
    _discovered.add(kind)


def available(kind: str) -> list[str]:
    """Sorted names of all registered components of ``kind``."""
    discover(kind)
    return sorted(_factories[kind])


def create(kind: str, name: str, **options: Any) -> Any:
    """Build the component registered as ``(kind, name)`` with the given options."""
    discover(kind)
    try:
        factory = _factories[kind][name]
    except KeyError:
        raise RegistryError(
            f"no {kind} named {name!r}; available: {available(kind) or 'none yet'}"
        ) from None
    return factory(**options)
