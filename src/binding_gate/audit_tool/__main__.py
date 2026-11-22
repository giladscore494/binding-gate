"""Command line of the human audit.

``sample RUN_DIR [RUN_DIR ...] --out SAMPLE.json [--html PAGE.html]``
    Draw the pre-registered sample (50 blocked + 50 accepted gate decisions,
    fixed seed) from finished run directories; optionally write the labelling page.
``html --sample SAMPLE.json --out PAGE.html``
    Write the offline labelling page for an existing sample.
``label --sample SAMPLE.json --out LABELS.json [--auditor NAME]``
    Label in the terminal, one case at a time (resumable).
``analyze --sample SAMPLE.json --labels LABELS.json [--labels MORE.json ...] --out AUDIT.json``
    Agreement, disagreements and decomposer share; AUDIT.json is read by
    ``python -m binding_gate.report``.

Exit codes: 0 success; 1 failure; 2 bad command line.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from binding_gate.audit_tool.analysis import analyse_files
from binding_gate.audit_tool.html import render_labelling_page
from binding_gate.audit_tool.sampling import AuditError, load_sample, sample_from_run_dirs, write_sample
from binding_gate.audit_tool.terminal import label_interactively


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m binding_gate.audit_tool", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample", help="draw the pre-registered audit sample from run directories")
    sample.add_argument("run_dirs", nargs="+")
    sample.add_argument("--out", required=True, help="sample JSON file to write")
    sample.add_argument("--html", default=None, help="also write the labelling page here")
    page = commands.add_parser("html", help="write the offline labelling page of a sample")
    page.add_argument("--sample", required=True)
    page.add_argument("--out", required=True)
    label = commands.add_parser("label", help="label in the terminal (resumable)")
    label.add_argument("--sample", required=True)
    label.add_argument("--out", required=True, help="label JSON file (created or continued)")
    label.add_argument("--auditor", default=None)
    analyze = commands.add_parser("analyze", help="agreement, disagreements, decomposer share")
    analyze.add_argument("--sample", required=True)
    analyze.add_argument("--labels", action="append", required=True, help="label file; repeat for more auditors (first = primary)")
    analyze.add_argument("--out", required=True)
    return parser


def _write_page(sample_path: str, out: str) -> None:
    sample, digest = load_sample(sample_path)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(render_labelling_page(sample, digest), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "sample":
            sample = sample_from_run_dirs(args.run_dirs)
            digest = write_sample(sample, args.out)
            print(json.dumps({"sample": args.out, "sha256": digest, "split": sample["split"],
                              "composition": sample["composition"], "population": sample["population"],
                              "shortfall": sample["shortfall"]}, indent=2, sort_keys=True))
            if args.html:
                _write_page(args.out, args.html)
                print(f"labelling page: {args.html}")
        elif args.command == "html":
            _write_page(args.sample, args.out)
            print(f"labelling page: {args.out}")
        elif args.command == "label":
            sample, digest = load_sample(args.sample)
            label_interactively(sample, digest, args.out, auditor=args.auditor)
        elif args.command == "analyze":
            analysis = analyse_files(args.sample, args.labels)
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(analysis, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
            print(json.dumps({"written": args.out, "per_half": analysis["per_half"],
                              "disagreements": len(analysis["disagreements"]),
                              "single_auditor": analysis["single_auditor"]}, indent=2, sort_keys=True))
    except (AuditError, OSError, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
