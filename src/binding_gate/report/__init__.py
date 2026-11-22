"""The public report (PREREGISTRATION.md section 15; PR 10).

Modules
-------
``document``    block model rendered to Markdown and to one self-contained HTML file
``prereg_doc``  reads the status, rules, commitments, threats and amendments from PREREGISTRATION.md
``build``       inputs, integrity checks and the report sections

Command line::

    python -m binding_gate.report build --analysis runs/<id>/analysis [--analysis ...] \\
        [--run-dir runs/<id> ...] [--baseline baseline.json] [--audit audit.json] \\
        [--deviations deviations.json] --out report/

writes ``report.md`` (with the figures copied to ``figures/``) and
``report.html`` (figures embedded, no external assets, no script). Nothing here
reads traces or calls a model; see ``binding_gate.report.build``.
"""
