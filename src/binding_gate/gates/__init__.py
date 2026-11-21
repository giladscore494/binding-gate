"""Gates mapping check results to a verdict (gate function E).

Empty in PR 1 by design. Implemented by PR 4.
Add one module per component in this package and decorate its factory with
``@register("gate", "<name>")``; see ``binding_gate.registry``. Do not import
components here: discovery imports every module of this package automatically.
"""
