# results-log

Machine-written branch of binding-gate (PR 17; `src/binding_gate/results_log.py`). It never merges into `main`.

- `runs/<workflow run id>-<stage>/`: the non-sensitive reports of one finished workflow run
  (`run.json` lists what was kept and what was left out), and `ledger-after-run/ledger.json`.
- `frozen/<layer>/<workflow run id>/`: the published results of a frozen test run.

Never here: traces, dataset items, judge scores, web-page snapshots, encrypted files, secrets.
