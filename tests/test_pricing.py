"""Price file loading and the committed PR 2 configs."""

from __future__ import annotations

from pathlib import Path

import pytest

from binding_gate import registry, runner
from binding_gate.config import load_config
from binding_gate.contracts import Split
from binding_gate.datasets import deceptive_grounding as dg
from binding_gate.pricing import PricingError, load_prices, parse_prices

REPO_ROOT = Path(__file__).resolve().parents[1]
SLOTS = {
    "openai-low": ("openai_responses", "gpt-6-luna", (0.10, 0.50)),
    "openai-mid": ("openai_responses", "gpt-6.1-sol", (2.00, 10.00)),
    "anthropic-low": ("anthropic_messages", "claude-haiku-5-5", (0.10, 0.50)),
    "anthropic-mid": ("anthropic_messages", "claude-sonnet-5-5", (2.00, 10.00)),
    "google-low": ("google_gemini", "gemini-3.1-flash-lite", (0.25, 1.50)),
    "google-mid": ("google_gemini", "gemini-3.5-flash", (1.50, 9.00)),
    "deepseek-low": ("deepseek_chat", "deepseek-flash", (0.30, 1.20)),  # PR 13, slot G (peak price)
}
PRIMARY = ("openai-low", "anthropic-low", "google-low", "deepseek-low")
"""PR 13: the four primary low-cost models (A, C, E, G); the mid-tier B, D, F run on a test subsample."""
BASE = {
    "prices_version": 1,
    "assumptions": {"chars_per_token": 4.0, "visible_output_tokens": 250, "reasoning_tokens_low": 0, "reasoning_tokens_high": 2000},
    "price": [{"model_id": "m", "input_per_mtok": 2.0, "output_per_mtok": 10.0, "source": "s", "accessed": "2026-11-22"}],
}


def test_cost_arithmetic():
    book = parse_prices(BASE)
    assert book.get("m").cost(1_000_000, 100_000) == pytest.approx(3.0)
    assert book.assumptions.input_tokens(4001) == 1001
    assert book.has("m") and not book.has("other")
    with pytest.raises(PricingError, match="no \\[\\[price\\]\\] entry"):
        book.get("other")


@pytest.mark.parametrize(
    "change",
    [
        {"prices_version": 2},
        {"surprise": 1},
        {"assumptions": {"chars_per_token": 4.0}},
        {"assumptions": {**BASE["assumptions"], "chars_per_token": 0}},
        {"assumptions": {**BASE["assumptions"], "reasoning_tokens_high": 0, "reasoning_tokens_low": 5}},
        {"price": [{**BASE["price"][0], "input_per_mtok": -1}]},
        {"price": [{k: v for k, v in BASE["price"][0].items() if k != "source"}]},
        {"price": [{k: v for k, v in BASE["price"][0].items() if k != "accessed"}]},
        {"price": [BASE["price"][0], BASE["price"][0]]},
        {"price": [{**BASE["price"][0], "discount": 0.5}]},
    ],
)
def test_malformed_price_files_are_refused(change):
    with pytest.raises(PricingError):
        parse_prices({**BASE, **change})


def test_committed_price_file_has_a_sourced_dated_price_for_every_selected_model():
    book = load_prices(REPO_ROOT / "configs" / "prices.toml")
    for _component, model_id, (price_in, price_out) in SLOTS.values():
        price = book.get(model_id)
        assert (price.input_per_mtok, price.output_per_mtok) == (price_in, price_out)
        assert price.source and price.accessed == "2026-11-22"
    judge = book.get("kimi-k3.1-pro")
    assert (judge.input_per_mtok, judge.output_per_mtok) == (2.00, 10.00)


@pytest.mark.parametrize(
    "name,repeats,limit,labels",
    [("dev_baseline.toml", 1, None, PRIMARY), ("dev_smoke.toml", 1, 1, tuple(SLOTS))],
)
def test_committed_dev_configs_encode_the_preregistered_selection(name, repeats, limit, labels):
    config = load_config(REPO_ROOT / "configs" / name)
    assert config.split is Split.DEV and (config.repeats, config.limit) == (repeats, limit)
    assert {m.label: (m.name, m.options["model_id"]) for m in config.models} == {
        label: SLOTS[label][:2] for label in labels
    }
    assert [a.name for a in config.arms] == ["no_gate"] and [s.name for s in config.scorers] == ["provenance"]
    assert config.dataset.options["commit"] == dg.PINNED_COMMIT
    assert config.dataset.options["data_sha256"] == dg.PINNED_DATA_SHA256
    assert not Path(config.dataset.options["root"]).is_absolute()
    settings = runner.RunnerSettings.from_config(config)
    book = load_prices(REPO_ROOT / settings.prices)
    for spec in config.models:
        client = runner.build_model(spec)  # options are valid; no key is needed to construct
        assert client.max_output_tokens == 4096 and client.retry.max_retries == 5
        assert book.has(client.model_id) and settings.cap_for(spec.label) is not None
        assert "preview" not in client.model_id and "latest" not in client.model_id
    # The judge (Moonshot) is from a family different from every model under test and is not run in PR 2.
    assert "moonshot_chat" not in {m.name for m in config.models}
    assert "moonshot_chat" in registry.available("model")


def test_smoke_and_baseline_differ_only_in_size_and_budget():
    baseline = load_config(REPO_ROOT / "configs" / "dev_baseline.toml").to_dict()
    smoke = load_config(REPO_ROOT / "configs" / "dev_smoke.toml").to_dict()
    for key in ("dataset", "arms", "scorers", "split"):
        assert baseline[key] == smoke[key]
    # PR 13: the smoke run adds the mid-tier models; the primary models have the same specs
    smoke_models = {m["label"]: m for m in smoke["models"]}
    assert all(smoke_models[m["label"]] == m for m in baseline["models"])


def test_every_model_under_test_runs_at_its_lowest_reasoning_setting():
    """PR 13 (section 9): identical policy, config only."""
    smoke = load_config(REPO_ROOT / "configs" / "dev_smoke.toml")
    for spec in smoke.models:
        options = spec.options
        if spec.name == "openai_responses":
            assert options["reasoning"] == {"effort": "none"}
        elif spec.name == "google_gemini":
            assert options["thinking_config"] == {"thinkingLevel": "minimal"}
        elif spec.name == "anthropic_messages":
            assert "thinking" not in options  # the Messages API thinks only when enabled
        elif spec.name == "deepseek_chat":
            assert options["thinking"] == {"type": "disabled"}
        else:  # pragma: no cover
            raise AssertionError(spec.name)


def test_per_model_reasoning_assumption_overrides_the_file_wide_one():
    book = parse_prices({**BASE, "price": [{**BASE["price"][0], "reasoning_tokens_high": 9000}, {**BASE["price"][0], "model_id": "n"}]})
    assert book.reasoning_high("m") == 9000 and book.reasoning_high("n") == 2000 and book.reasoning_high(None) == 2000
    with pytest.raises(PricingError):
        parse_prices({**BASE, "price": [{**BASE["price"][0], "reasoning_tokens_high": -1}]})
    committed = load_prices(REPO_ROOT / "configs" / "prices.toml")
    assert committed.assumptions.reasoning_tokens_high == 500  # lowest reasoning setting (PR 13)
    assert committed.reasoning_high("kimi-k3.1-pro") == 2000  # the judge runs at its provider default


def test_no_api_key_or_machine_path_is_committed_in_configs():
    for path in (REPO_ROOT / "configs").glob("*.toml"):
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text and "sk-" not in text and "api_key" not in text.lower()


def test_preregistration_section_9_lists_the_same_ids_and_prices():
    prereg = (REPO_ROOT / "PREREGISTRATION.md").read_text(encoding="utf-8")
    section = prereg.split("## 9. Models", 1)[1].split("## 10.", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ") and "model under test" in line]
    assert len(rows) == 7
    for _component, model_id, (price_in, price_out) in SLOTS.values():
        (row,) = [line for line in rows if f"`{model_id}`" in line]
        assert f"{price_in:.2f} / {price_out:.2f}" in row
    assert "`kimi-k3.1-pro`" in section and "secondary judge" in section
    assert "**Status:** DRAFT" in prereg and "FROZEN" not in prereg.split("\n", 3)[2]
