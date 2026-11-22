"""Run configs: validation and a hash that tracks meaning, not formatting."""

from __future__ import annotations

from pathlib import Path

import pytest

import builders as b
from binding_gate.config import ConfigError, load_config
from binding_gate.contracts import Split

REPO_ROOT = Path(__file__).resolve().parents[1]

BASE = """\
config_version = 1
name = "dev-baseline"
split = "dev"
repeats = 3

[dataset]
name = "toy"
[dataset.options]
root = "data/raw/toy"
seed = 7

[[models]]
name = "fake"
label = "model-a"
[models.options]
temperature = 0.0

[[arms]]
name = "no_gate"

[[arms]]
name = "gated"
label = "gated-all-checks"
[arms.options]
checks = ["quote_verbatim", "entity_in_window"]

[[scorers]]
name = "provenance"
"""

# Same meaning: comments, blank lines, key order, table order within a table,
# inline tables and an explicit default label.
REFORMATTED = """\
# a comment
repeats = 3
split = "dev"
name = "dev-baseline"
config_version = 1
description = ""

scorers = [{ name = "provenance", options = {} }]

[dataset]
options = { seed = 7, root = "data/raw/toy" }
name = "toy"

[[models]]
options = { temperature = 0.0 }
label = "model-a"
name = "fake"

[[arms]]
name = "no_gate"
label = "no_gate"

[[arms]]
label = "gated-all-checks"
name = "gated"
options = { checks = ["quote_verbatim", "entity_in_window"] }
"""


def _write(tmp_path: Path, text: str, name: str = "run.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_load_and_normalise(tmp_path):
    config = load_config(_write(tmp_path, BASE))
    assert config.split is Split.DEV and config.repeats == 3 and config.limit is None
    assert [a.label for a in config.arms] == ["no_gate", "gated-all-checks"]
    assert config.dataset.options == {"root": "data/raw/toy", "seed": 7}
    assert config.to_dict()["config_version"] == 1


def test_hash_is_stable_and_ignores_formatting(tmp_path):
    first = load_config(_write(tmp_path, BASE, "a.toml"))
    again = load_config(_write(tmp_path, BASE, "b.toml"))
    reformatted = load_config(_write(tmp_path, REFORMATTED, "c.toml"))
    assert first.config_hash == again.config_hash == reformatted.config_hash
    assert len(first.config_hash) == 64


def test_hash_is_pinned():
    # Guards against accidental changes to canonicalisation or normalisation,
    # which would silently detach old traces from their configs.
    assert b.run_config().config_hash == PINNED_HASH


@pytest.mark.parametrize(
    "old,new",
    [
        ("repeats = 3", "repeats = 4"),
        ('split = "dev"', 'split = "test"'),
        ("seed = 7", "seed = 8"),
        ("temperature = 0.0", "temperature = 0.7"),
        ('checks = ["quote_verbatim", "entity_in_window"]', 'checks = ["entity_in_window", "quote_verbatim"]'),
        ('label = "model-a"', 'label = "model-b"'),
        ('name = "dev-baseline"', 'name = "other"'),
    ],
)
def test_hash_changes_with_any_change_of_meaning(tmp_path, old, new):
    assert old in BASE
    base = load_config(_write(tmp_path, BASE, "a.toml"))
    changed = load_config(_write(tmp_path, BASE.replace(old, new), "b.toml"))
    assert base.config_hash != changed.config_hash


def test_arm_order_is_part_of_the_hash():
    one = b.run_config(arms=[{"name": "a"}, {"name": "b"}])
    two = b.run_config(arms=[{"name": "b"}, {"name": "a"}])
    assert one.config_hash != two.config_hash


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"config_version": 2}, "config_version"),
        ({"name": ""}, "name"),
        ({"split": "train"}, "split"),
        ({"repeats": 0}, "repeats"),
        ({"repeats": True}, "repeats"),
        ({"limit": 0}, "limit"),
        ({"split": "test", "limit": 5}, "not allowed on the test split"),
        ({"models": []}, r"\[\[models\]\]"),
        ({"arms": []}, r"\[\[arms\]\]"),
        ({"arms": [{"name": "gated"}, {"name": "gated"}]}, "labels must be unique"),
        ({"arms": [{"name": "gated", "typo": 1}]}, "unknown key"),
        ({"dataset": {"name": "toy", "options": "x"}}, "options must be a table"),
        ({"environment": "prod"}, "unknown top-level key"),
    ],
)
def test_invalid_configs_are_rejected(overrides, message):
    with pytest.raises(ConfigError, match=message):
        b.run_config(**overrides)


def test_dataset_table_is_required(tmp_path):
    text = BASE.replace('[dataset]\nname = "toy"\n[dataset.options]\nroot = "data/raw/toy"\nseed = 7\n', "")
    with pytest.raises(ConfigError, match=r"\[dataset\]"):
        load_config(_write(tmp_path, text))


def test_non_json_values_and_bad_toml_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="JSON-compatible"):
        load_config(_write(tmp_path, BASE.replace("seed = 7", "seed = 2026-11-22")))
    with pytest.raises(ConfigError, match="JSON-compatible"):
        load_config(_write(tmp_path, BASE.replace("seed = 7", "seed = nan")))
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(_write(tmp_path, "name = "))


def test_two_ablations_of_one_arm_are_told_apart_by_label():
    config = b.run_config(
        arms=[
            {"name": "gated", "label": "gated-full", "options": {"checks": ["a", "b"]}},
            {"name": "gated", "label": "gated-minus-b", "options": {"checks": ["a"]}},
        ]
    )
    assert [a.label for a in config.arms] == ["gated-full", "gated-minus-b"]


def test_every_committed_config_loads():
    # configs/prices.toml is the price table of the cost estimator and configs/budget.toml the
    # budget plan (PR 13), not run configs
    # (it is validated in tests/test_pricing.py). configs/decomposer_eval_*.toml are
    # configs of the separate decomposer measurement (PR 3), validated in
    # tests/test_decomposer_eval.py.
    paths = sorted(
        p for p in (REPO_ROOT / "configs").glob("*.toml")
        if p.name not in ("prices.toml", "budget.toml") and not p.name.startswith("decomposer_eval")
    )
    assert paths, "configs/ must contain at least the example config"
    for path in paths:
        load_config(path)


def test_runner_table_is_optional_hashed_when_present_and_must_be_json_compatible():
    plain = b.run_config()
    assert plain.runner == {} and "runner" not in plain.to_dict()
    with_runner = b.run_config(runner={"concurrency": 8, "max_usd": 5.0})
    assert with_runner.to_dict()["runner"] == {"concurrency": 8, "max_usd": 5.0}
    assert with_runner.config_hash != plain.config_hash
    assert b.run_config(runner={}).config_hash == plain.config_hash
    assert b.run_config(runner={"max_usd": 6.0}).config_hash != b.run_config(runner={"max_usd": 5.0}).config_hash
    with pytest.raises(ConfigError, match="runner"):
        b.run_config(runner=["not", "a", "table"])
    with pytest.raises(ConfigError, match="runner"):
        b.run_config(runner={"budget": float("nan")})


PINNED_HASH = "bfd5a6d5bbda754616928daa79270d890fecb6d11a1b68e8a6fef9172bc55f59"
