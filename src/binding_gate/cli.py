"""Command line: ``python -m binding_gate.cli <command> ...``.

Every run is selected by the path of a config file; no experiment setting is
read from the environment (API keys are, through ``binding_gate.secrets``).

Commands
--------
``dry-run --config C [--model L]``
    Load the items, build every request, count calls and estimate cost. Calls no
    API, needs no key, writes no trace.
``preflight --config C [--model L]``
    Free check that each configured model id exists at its provider.
``run --config C --confirm-spend [--model L] [--runs-dir runs] [--resume DIR]``
    The paid run. Refuses without ``--confirm-spend``; runs the preflight
    itself; enforces the budget cap of the config.
``report RUN_DIR [RUN_DIR ...] [--json FILE]``
    The PR 2 baseline report and stop-rule outcome, from trace files only.
``analyze RUN_DIR [RUN_DIR ...] [--out DIR] [--no-figures]``
    The PR 6 statistics (metrics, cluster-bootstrap intervals, H1-H4) from
    trace files only; writes ``analysis.json``, ``report.md`` and figures.
``frozen-test-run --config C --tag prereg-freeze-N --purpose TEXT --confirm-spend``
    The one-command frozen test run: refuses unless every test-lock condition
    holds (the refusal is logged), runs the config on the test split, then
    writes the analysis under ``runs/<run_id>/analysis/``
    (``binding_gate.frozen_run``).
``models --config C``
    The model labels of a config as a JSON array (used by the workflow matrix).
``dataset-pin --config C``
    ``repository=``, ``commit=`` and ``root=`` of the config's dataset (used by
    the workflow to clone the benchmark at the pinned commit).

Exit codes: 0 success; 1 failure; 2 bad command line; 3 budget cap reached (run
left unfinished and resumable); 4 a required API key is not set.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from binding_gate import baseline_report, frozen_run, registry, runner, secrets
from binding_gate.config import ConfigError, load_config
from binding_gate.pricing import PricingError
from binding_gate.registry import RegistryError
from binding_gate.secrets import SecretError
from binding_gate.testlock import TestSplitLocked

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_BUDGET = 3
EXIT_NO_KEY = 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m binding_gate.cli", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def with_config(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--config", required=True, help="path of the run config (TOML)")
        sub.add_argument("--repo-root", default=".", help="top level of the binding-gate checkout (default: .)")
        return sub

    def with_model(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--model", default="all", help='model label from the config, or "all" (default)')

    def with_test_lock(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--test-unlock-tag", default=None, help="freeze tag; only for configs with split = test")
        sub.add_argument("--purpose", default=None, help="stated purpose of a test-split access (logged)")

    dry = with_config("dry-run", "count calls and estimate cost without calling any API")
    with_model(dry)
    with_test_lock(dry)
    dry.add_argument("--json", default=None, help="also write the estimate to this JSON file")

    pre = with_config("preflight", "free check that each model id exists")
    with_model(pre)

    run = with_config("run", "the paid run (requires --confirm-spend)")
    with_model(run)
    with_test_lock(run)
    run.add_argument("--confirm-spend", action="store_true", help="confirm that paid API calls may be made")
    run.add_argument("--runs-dir", default="runs", help="where run directories are created (default: runs)")
    run.add_argument("--resume", default=None, help="run directory of an unfinished run to continue")

    report = commands.add_parser("report", help="baseline report and stop-rule outcome from traces")
    report.add_argument("run_dirs", nargs="+", help="one or more run directories")
    report.add_argument("--arm", default="no_gate", help="arm label to report (default: no_gate)")
    report.add_argument("--json", default=None, help="also write the report to this JSON file")

    analyze = commands.add_parser("analyze", help="statistics, intervals and H1-H4 from traces (PR 6)")
    analyze.add_argument("run_dirs", nargs="+", help="one or more run directories")
    analyze.add_argument("--out", default=None, help="output directory (default: <first run dir>/analysis)")
    analyze.add_argument("--no-figures", action="store_true", help="do not draw figures")

    frozen = with_config("frozen-test-run", "the one-command frozen test run (test split, then analysis)")
    frozen.add_argument("--tag", required=True, help="the freeze tag prereg-freeze-N that HEAD must be")
    frozen.add_argument("--purpose", required=True, help="stated purpose of this test-split access (logged)")
    frozen.add_argument("--confirm-spend", action="store_true", help="confirm that paid API calls may be made")
    frozen.add_argument("--runs-dir", default="runs", help="where the run directory is created (default: runs)")
    frozen.add_argument("--resume", default=None, help="run directory of an unfinished frozen run to continue")
    with_model(frozen)
    frozen.add_argument("--no-figures", action="store_true", help="do not draw figures")

    with_config("models", "print the model labels of a config as a JSON array")
    with_config("dataset-pin", "print repository, commit and root of the config's dataset")
    return parser


def _missing_keys(config, selection: str | None) -> list[tuple[str, str]]:
    """``(label, secret name)`` of every selected model whose API key is not set."""
    missing = []
    for spec in runner.select_models(config, selection):
        secret = getattr(runner.build_model(spec), "secret_name", None)
        if secret and not secrets.has_secret(secret):
            missing.append((spec.label, secret))
    return missing


def _cmd_dry_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    report = runner.dry_run(
        config,
        repo_root=args.repo_root,
        selection=args.model,
        test_unlock_tag=args.test_unlock_tag,
        purpose=args.purpose,
    )
    print(report.format())
    if args.json:
        Path(args.json).write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return EXIT_OK


def _cmd_preflight(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    failed = skipped = 0
    for spec in runner.select_models(config, args.model):
        client = runner.build_model(spec)
        secret = getattr(client, "secret_name", None)
        check = getattr(client, "preflight", None)
        if check is None:
            print(f"preflight {spec.label}: no preflight available for component {spec.name!r}")
            continue
        if secret and not secrets.has_secret(secret):
            print(f"preflight {spec.label}: SKIPPED - repository secret / environment variable {secret} is not set")
            skipped += 1
            continue
        result = check()
        print(f"preflight {spec.label}: {'OK' if result.ok else 'FAILED'} - {result.detail}")
        failed += not result.ok
    if failed:
        return EXIT_FAILURE
    return EXIT_NO_KEY if skipped else EXIT_OK


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not args.confirm_spend:
        print(
            "REFUSED: a paid run needs --confirm-spend. Use the dry-run command to see the "
            "number of calls and the cost estimate first.",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    missing = _missing_keys(config, args.model)
    if missing:
        for label, secret in missing:
            print(f"model {label}: API key {secret} is not set; nothing was run", file=sys.stderr)
        return EXIT_NO_KEY
    summary = runner.execute_run(
        config,
        repo_root=args.repo_root,
        runs_dir=args.runs_dir,
        confirm_spend=True,
        config_path=args.config,
        selection=args.model,
        resume_dir=args.resume,
        test_unlock_tag=args.test_unlock_tag,
        purpose=args.purpose,
    )
    print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    if summary.budget_exceeded:
        print(
            f"ABORTED: budget cap reached for {summary.aborted_models}. The run directory "
            f"{summary.run_dir} is unfinished and can be inspected or resumed.",
            file=sys.stderr,
        )
        return EXIT_BUDGET
    return EXIT_OK


def _cmd_report(args: argparse.Namespace) -> int:
    report = baseline_report.report_from_run_dirs(args.run_dirs, arm=args.arm)
    print(report.format())
    if args.json:
        baseline_report.write_json(report, args.json)
    return EXIT_OK


def _cmd_analyze(args: argparse.Namespace) -> int:
    from binding_gate.stats.analysis import analyse_run_dirs, write_outputs

    analysis = analyse_run_dirs(args.run_dirs)
    out = Path(args.out) if args.out else Path(args.run_dirs[0]) / frozen_run.ANALYSIS_DIRNAME
    written = write_outputs(analysis, out, figures=not args.no_figures)
    print(json.dumps({"written": written, "summary": analysis["summary"]}, indent=2, sort_keys=True))
    return EXIT_OK


def _cmd_frozen_test_run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.repo_root) / args.config if not Path(args.config).is_absolute() else args.config)
    missing = _missing_keys(config, args.model)
    if missing:
        for label, secret in missing:
            print(f"model {label}: API key {secret} is not set; nothing was run", file=sys.stderr)
        return EXIT_NO_KEY
    result = frozen_run.frozen_test_run(
        args.config,
        repo_root=args.repo_root,
        tag=args.tag,
        purpose=args.purpose,
        confirm_spend=args.confirm_spend,
        runs_dir=args.runs_dir,
        selection=args.model,
        resume_dir=args.resume,
        figures=not args.no_figures,
    )
    print(json.dumps({"run": result.summary, "written": result.written}, indent=2, sort_keys=True))
    if result.aborted:
        print(
            f"ABORTED: budget cap reached. {result.run_dir} is unfinished; no analysis was written. "
            "Resume with --resume (a further logged test-split access).",
            file=sys.stderr,
        )
        return EXIT_BUDGET
    print(
        f"Frozen test run finished. Analysis: {result.analysis_dir}. Now commit "
        "audit/test_access_log.jsonl (ARCHITECTURE.md section 6).",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_models(args: argparse.Namespace) -> int:
    print(json.dumps([spec.label for spec in load_config(args.config).models]))
    return EXIT_OK


def _cmd_dataset_pin(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    adapter = registry.create("dataset", config.dataset.name, **config.dataset.options)
    for name in ("repository", "commit", "root"):
        value = getattr(adapter, name, None)
        if value is None:
            raise runner.RunnerError(f"dataset {config.dataset.name!r} does not declare {name!r}")
        print(f"{name}={Path(value).as_posix() if name == 'root' else value}")
    return EXIT_OK


_COMMANDS = {
    "dry-run": _cmd_dry_run,
    "preflight": _cmd_preflight,
    "run": _cmd_run,
    "report": _cmd_report,
    "analyze": _cmd_analyze,
    "frozen-test-run": _cmd_frozen_test_run,
    "models": _cmd_models,
    "dataset-pin": _cmd_dataset_pin,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except SecretError as exc:
        print(f"ERROR (API key): {exc}", file=sys.stderr)
        return EXIT_NO_KEY
    except TestSplitLocked as exc:
        print(f"ERROR (test split locked): {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except (ConfigError, PricingError, RegistryError, RuntimeError, ValueError, TypeError, OSError) as exc:
        # RuntimeError covers RunnerError, TraceError, DatasetError, GitError and ReportError.
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
