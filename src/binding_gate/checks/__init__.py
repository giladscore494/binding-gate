"""Deterministic binding and premise checks (gate functions C and D).

Empty in PR 1 by design. Implemented by PR 4.
Add one module per component in this package and decorate its factory with
``@register("check", "<name>")``; see ``binding_gate.registry``. Do not import
components here: discovery imports every module of this package automatically.
"""
