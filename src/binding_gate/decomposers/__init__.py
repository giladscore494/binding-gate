"""Question decomposers (gate function A).

Add one module per component in this package and decorate its factory with
``@register("decomposer", "<name>")``; see ``binding_gate.registry``. Do not import
components here: discovery imports every module of this package automatically.

Contents (PR 3)
---------------
``model_decomposer``               the registered decomposer: a model client chosen
                                   by config, a fixed prompt, a strict parser, every
                                   call logged, one decomposition per question per run.
``decomposition_format``           the fixed prompt and the deterministic parser.
``cache``                          the per-run decomposition cache.
``deceptive_grounding_reference``  REFERENCE ONLY (reads gold); used solely by
                                   ``binding_gate.evaluation.decomposer``. Not
                                   registered, not a decomposer, never used in the gate.

How an arm obtains decompositions
---------------------------------
Only through the registry and the ``Decomposer`` protocol, never by importing a
module of this package::

    spec = options["decomposer"]   # {name = "model_decomposer", options = {model = {...}}}
    decomposer = registry.create("decomposer", spec["name"], **spec.get("options", {}))
    decomposition = decomposer.decompose(arm_input.question)
    # decomposition.constraints -> QuestionConstraints
    # decomposition.model_calls -> append to ArmOutput.model_calls

``decompose`` raises on failure; every exception it raises on purpose carries
``model_calls`` (so the runner can log them). See ``model_decomposer`` for details.
"""
