"""Builds a throwaway repository, a synthetic benchmark checkout and a run config for runner tests."""

from __future__ import annotations

from pathlib import Path

import builders as b
import dg_fixture
from binding_gate.config import RunConfig, parse_config

PRICES = """\
prices_version = 1
[assumptions]
chars_per_token = 4.0
visible_output_tokens = 100
reasoning_tokens_low = 0
reasoning_tokens_high = 1000
[[price]]
model_id = "fake-cheap"
input_per_mtok = 1.0
output_per_mtok = 2.0
source = "test"
accessed = "2026-11-22"
[[price]]
model_id = "fake-dear"
input_per_mtok = 100.0
output_per_mtok = 200.0
source = "test"
accessed = "2026-11-22"
"""


def make_repo(tmp_path: Path, *, prereg_status: str = "DRAFT") -> Path:
    repo = b.init_repo(tmp_path / "repo", prereg_status=prereg_status)
    (repo / "configs").mkdir()
    (repo / "configs" / "prices.toml").write_text(PRICES, encoding="utf-8")
    b.git(repo, "add", "-A")
    b.git(repo, "commit", "-q", "-m", "prices")
    return repo


def raw_config(dataset_options: dict, **overrides) -> dict:
    raw = {
        "config_version": 1,
        "name": "runner-test",
        "split": "dev",
        "repeats": 2,
        "dataset": {"name": "deceptive_grounding", "options": dataset_options},
        "models": [
            {"name": "scripted_fake", "label": "good", "options": {"model_id": "fake-cheap", "behaviour": "target"}},
            {"name": "scripted_fake", "label": "bad", "options": {"model_id": "fake-cheap", "behaviour": "competitor"}},
        ],
        "arms": [{"name": "no_gate"}],
        "scorers": [{"name": "provenance"}],
        "runner": {"concurrency": 3, "max_usd": 5.0},
    }
    raw.update(overrides)
    return raw


def setup(tmp_path: Path, **overrides) -> tuple[Path, RunConfig, dict]:
    """Return ``(repo, config, raw config)`` with a 3-dev / 2-test synthetic checkout."""
    repo = make_repo(tmp_path)
    options = dg_fixture.write_checkout(tmp_path / "dg")
    raw = raw_config(options, **overrides)
    return repo, parse_config(raw), raw


def toml_text(raw: dict) -> str:
    """Serialise the small config dict used in tests as TOML (enough for these shapes)."""

    def value(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, str):
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        raise TypeError(v)

    def table(name: str, data: dict) -> list[str]:
        lines = [f"[{name}]"] + [f"{k} = {value(v)}" for k, v in data.items() if not isinstance(v, dict)]
        for k, v in data.items():
            if isinstance(v, dict):
                lines += table(f"{name}.{k}", v)
        return lines

    lines = [f"{k} = {value(v)}" for k, v in raw.items() if not isinstance(v, (dict, list))]
    for key in ("runner", "dataset"):
        if key in raw:
            lines += table(key, raw[key])
    for key in ("models", "arms", "scorers"):
        for entry in raw.get(key, []):
            lines.append(f"[[{key}]]")
            lines += [f"{k} = {value(v)}" for k, v in entry.items() if k != "options"]
            if "options" in entry:
                lines += table(f"{key}.options", entry["options"])
    return "\n".join(lines) + "\n"
