"""Command line of the public report: ``python -m binding_gate.report build ...``.

``build --analysis PATH [--analysis PATH ...] --out DIR``
    ``PATH`` is an ``analysis.json`` or the directory holding it (figures are
    taken from its ``figures/``). Optional: ``--run-dir`` (repeatable; manifests
    and recorded spend; a run in no analysis is listed as not analysed),
    ``--baseline`` (repeatable; section 10 report JSON), ``--audit`` (audit
    analysis JSON), ``--deviations`` (JSON list of declared deviations),
    ``--preregistration`` (default ``PREREGISTRATION.md``), ``--title``.

Writes ``DIR/report.md``, ``DIR/figures/*`` and ``DIR/report.html``.

``publish RUN_DIR [--results-dir results] [--file PATH ...] [--include-traces]``
    (PR 16) Copies the non-sensitive results of a finished, analysed run to
    ``results/layer<N>/<run_id>/`` (the committed location, section 17 item
    37): manifest, invocations, frozen-run record, analysis JSON, report and
    figures, and for layer 3 the snapshot publish manifest. Refuses page text,
    layer-2/3 traces, re-scoring outputs and any file not on the allow-list
    (``binding_gate.report.publish``). Writes ``results_manifest.json``.

Exit codes: 0 success; 1 refused or failed; 2 bad command line.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from binding_gate.report.build import ReportError, load_inputs, write_report
from binding_gate.report.prereg_doc import PreregParseError
from binding_gate.report.publish import RESULTS_DIRNAME, PublishError, publish_run


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
    publish = commands.add_parser("publish", help="copy a run's non-sensitive results to results/layer<N>/<run_id>/")
    publish.add_argument("run_dir", help="a finished run directory with its analysis in RUN_DIR/analysis/")
    publish.add_argument("--results-dir", default=RESULTS_DIRNAME, help="default: results")
    publish.add_argument("--file", action="append", default=None,
                         help="publish only these files (paths relative to RUN_DIR; repeatable); a file that is "
                         "not allowed refuses the whole publication")
    publish.add_argument("--include-traces", action="store_true",
                         help="also publish traces.jsonl (layer 1 only: Apache-2.0 documents)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "publish":
        try:
            result = publish_run(args.run_dir, results_dir=args.results_dir, files=args.file,
                                 include_traces=args.include_traces)
        except (PublishError, OSError) as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"published": result.target.as_posix(), "layer": result.layer, "files": result.files,
                          "not_published": result.not_published}, indent=2, sort_keys=True))
        return 0
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
