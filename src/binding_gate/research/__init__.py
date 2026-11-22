"""Layer 3 (live web): search client, page fetcher, snapshot store and re-search query (PR 8).

PREREGISTRATION.md sections 3 (layer 3), 5, 7 and 14. The modules:

``context``    the run context the runner hands to live-web arms (run directory, dry run)
``search``     the web search client (SerpApi, Google engine) and its dry-run stand-in
``robots``     a robots.txt parser and matcher following RFC 9309
``fetch``      the page fetcher: fixed user agent, robots.txt respected, size and time limits
``extract``    HTML / plain text -> text, with the standard library's ``html.parser``
``snapshots``  the snapshot store under ``runs/<run_id>/snapshots/`` and its publish manifest
``query``      the first query and the deterministic re-search query template
``session``    one research session per run: caching of searches and fetches, documents for an attempt

Nothing in this package decides whether an answer is acceptable; that is the
gate's job (``binding_gate.gates.binding``), which sees only documents built
from stored snapshots.
"""
