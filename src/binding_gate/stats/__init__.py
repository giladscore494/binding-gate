"""Statistics for binding-gate (PR 6): metrics, cluster bootstrap, hypothesis decisions.

Modules
-------
``prereg``      the pre-registered parameters (seed, resamples, thresholds); mirrors
                PREREGISTRATION.md sections 6.4, 8 and 11, guarded by a test.
``bootstrap``   statistic expressions and the deterministic pure-Python cluster bootstrap.
``metrics``     trace records -> per-item column totals; the metric definitions.
``hypotheses``  H1-H4 decision rules.
``analysis``    loading run directories, the full analysis, JSON output.
``report``      Markdown rendering of the analysis.

Statistics read trace files only; they never call a model. Command line:
``python -m binding_gate.cli analyze RUN_DIR [RUN_DIR ...] --out DIR``.
"""
