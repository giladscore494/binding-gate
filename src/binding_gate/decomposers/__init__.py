"""Question decomposers (gate function A).

Empty in PR 1 by design. Implemented by PR 3.
Add one module per component in this package and decorate its factory with
``@register("decomposer", "<name>")``; see ``binding_gate.registry``. Do not import
components here: discovery imports every module of this package automatically.
"""
