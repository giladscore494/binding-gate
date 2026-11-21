"""Dataset adapters.

Empty in PR 1 by design. Implemented by PR 2 (Deceptive Grounding), 7 (GRAB-RAG), 8 (SealQA).
Add one module per component in this package and decorate its factory with
``@register("dataset", "<name>")``; see ``binding_gate.registry``. Do not import
components here: discovery imports every module of this package automatically.
"""
