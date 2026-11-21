"""Experimental arms.

Empty in PR 1 by design. Implemented by PR 2 (no-gate baseline), 4 (gated), 5 (comparison baselines), 8 (re-search).
Add one module per component in this package and decorate its factory with
``@register("arm", "<name>")``; see ``binding_gate.registry``. Do not import
components here: discovery imports every module of this package automatically.
"""
