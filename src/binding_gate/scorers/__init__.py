"""Scorers.

Empty in PR 1 by design. Implemented by PR 2 (deterministic provenance), 5 (secondary LLM judge).
Add one module per component in this package and decorate its factory with
``@register("scorer", "<name>")``; see ``binding_gate.registry``. Do not import
components here: discovery imports every module of this package automatically.
"""
