"""Command line of the public report: ``python -m binding_gate.report build ...``.

``build --analysis PATH [--analysis PATH ...] --out DIR``
    ``PATH`` is an ``analysis.json`` or the directory holding it (figures are
    taken from its ``figures/``). Optional: ``--run-dir`` (repeatable; manifests
    and recorded spend; a run in no analysis is listed as not analysed),
    ``--baseline`` (repeatable; section 10 report JSON), ``--audit`` (audit
    analysis JSON), ``--deviations`` (JSON list of declared deviations),
    ``--preregistration`` (default ``PREREGISTRATION.md``), ``--title``.

Writes ``DIR/report.md``, ``DIR/figures/*`` and ``DIR/report.html``.
Exit codes: 0 success; 1 refused or failed; 2 bad command line.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from binding_gate.report.build import ReportError, load_inputs, write_report
from binding_gate.report.prereg_doc import PreregParseError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m binding_gate.report", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build report.md and report.html")
    build.add_argument("--analysis", action="append", required=True, help="analysis.json or its directory (repeatable)")
    build.add_argument("--run-dir", action="append", default=[], help="run directory (repeatable)")
    build.add_argument("--baseline", action="append", default=[], help="section 10 baseline report JSON (repeatable)")
    build.add_argument("--audit", default=None, help="audit analysis JSON (python -m binding_gate.audit_tool analyze)")
    build.add_argument("--deviations", default=None, help="JSON list of declared deviations")
    build.add_argument("--preregistration", default="PREREGISTRATION.md")
    build.add_argument("--title", default=None)
    build.add_argument("--out", required=True, help="output directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inputs = load_inputs(
            args.analysis,
            prereg_path=args.preregistration,
            run_dirs=args.run_dir,
            baseline_paths=args.baseline,
            audit_path=args.audit,
            deviations_path=args.deviations,
            title=args.title,
        )
        written = write_report(inputs, args.out)
    except (ReportError, PreregParseError, OSError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(written, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
