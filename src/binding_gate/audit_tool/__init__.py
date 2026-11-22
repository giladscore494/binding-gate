"""The human audit of gate decisions (PREREGISTRATION.md section 13; PR 10).

Modules
-------
``prereg``    the pre-registered audit parameters (seed, sample sizes, interval level)
``sampling``  deterministic 50 blocked + 50 accepted sample from frozen-run traces
``views``     what the auditor sees before the label, and what is revealed after it
``html``      the offline single-file labelling page (no server, no network)
``terminal``  the terminal fallback for labelling one case at a time
``labels``    the label file format shared by both interfaces
``analysis``  agreement with Clopper-Pearson intervals, disagreements, decomposer share

Command line: ``python -m binding_gate.audit_tool sample|html|label|analyze ...``
(see ``python -m binding_gate.audit_tool --help``). Nothing here calls a model.
"""
