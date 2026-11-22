"""The layer-3 correctness rule, its mapping for the statistics, and pricing of auxiliary calls."""

from __future__ import annotations

import pytest

from binding_gate.contracts import AnswerCandidate, Item, Message, ModelCall, ModelRequest, ModelResponse, TokenUsage
from binding_gate.pricing import PricingError, parse_prices
from binding_gate.runner import CostModel, priced_id
from binding_gate.scorers.sealqa_answer import (
    LAYER3_OUTCOME_BY_LABEL,
    SealQAAnswerScorer,
    SealQAScoringError,
    layer3_outcome,
)

QUOTE = "the games were hosted by the city"


def item(answer="San Francisco, California", **gold):
    return Item(id="sealqa/1", group_id="sealqa/1", dataset="sealqa", question="Where?",
                gold={"answer": answer, "answers": [answer], "freshness": "fast", "search_results": "conflict",
                      "subsets": ["seal_hard"], **gold})


def answered(value, quote=QUOTE):
    return AnswerCandidate.answered(value=value, quote=quote, source_id="web1", raw="{}")


@pytest.mark.parametrize(
    "value,gold,label",
    [
        ("The answer is San Francisco, California.", "San Francisco, California", "correct"),
        ("san francisco california", "San Francisco, California", "correct"),  # case and punctuation
        ("It was 1,000 people.", "1000", "correct"),  # 1,000 -> 1000
        ("The 1st season.", "first season", "correct"),
        ("San Francisco", "San Francisco, California", "incorrect"),  # partial: the judge's job, not ours
        ("Northbridge", "Southport", "incorrect"),
        ("Southporter", "Southport", "incorrect"),  # whole words only
    ],
)
def test_value_containment_rule(value, gold, label):
    result = SealQAAnswerScorer().score(item(gold), answered(value))
    assert result.label == label and result.details["outcome"] == label
    assert result.details["rule_version"] == 1 and result.details["freshness"] == "fast"


def test_exact_match_is_recorded_and_short_quotes_abstentions_and_garbage_are_labelled():
    scorer = SealQAAnswerScorer()
    assert scorer.score(item("Southport"), answered("Southport.")).details["value_exact"] is True
    assert scorer.score(item("Southport"), answered("Southport", quote="by Southport")).label == "unparseable"
    assert scorer.score(item(), AnswerCandidate.abstained(raw="", reason="gate")).label == "abstained"
    assert scorer.score(item(), AnswerCandidate.unparseable(raw="x", reason="no json")).label == "unparseable"
    with pytest.raises(SealQAScoringError):
        scorer.score(Item(id="a", group_id="a", dataset="sealqa", question="q"), answered("x"))
    with pytest.raises(ValueError):
        SealQAAnswerScorer(min_quote_tokens=0)


def test_layer3_outcomes_map_onto_the_statistics_categories():
    from binding_gate.baseline_report import OUTCOMES

    assert set(LAYER3_OUTCOME_BY_LABEL.values()) <= set(OUTCOMES)
    assert LAYER3_OUTCOME_BY_LABEL["correct"] == "target_grounded" and LAYER3_OUTCOME_BY_LABEL["incorrect"] == "wrong_entity"

    class R:  # the attributes layer3_outcome reads
        def __init__(self, error=None, scores=()):
            self.error, self.scores, self.item_id, self.arm, self.model = error, scores, "i", "a", "m"

    from binding_gate.contracts import ScoreResult

    assert layer3_outcome(R(error="boom")) == "error"
    assert layer3_outcome(R(scores=(ScoreResult("sealqa_answer", "correct"),))) == "target_grounded"
    assert layer3_outcome(R(scores=(ScoreResult("sealqa_answer", "scorer_error"),))) == "error"
    with pytest.raises(SealQAScoringError):
        layer3_outcome(R(scores=(ScoreResult("provenance", "both"),)))


# --------------------------------------------------------------------------- #
# pricing
# --------------------------------------------------------------------------- #

BASE = {
    "prices_version": 1,
    "assumptions": {"chars_per_token": 4.0, "visible_output_tokens": 250, "reasoning_tokens_low": 0, "reasoning_tokens_high": 2000},
    "price": [
        {"model_id": "gpt-x", "input_per_mtok": 2.0, "output_per_mtok": 10.0, "source": "s", "accessed": "d"},
        {"model_id": "serpapi/google", "input_per_mtok": 0.0, "output_per_mtok": 0.0, "per_call_usd": 0.025, "source": "s", "accessed": "d"},
    ],
}


def call(client, model, requested=None, usage=None, cache_hit=False):
    raw = {"requested_model": requested} if requested else {}
    if cache_hit:
        raw["cache_hit"] = True
    return ModelCall(purpose="x", client=client, request=ModelRequest((Message("user", "q" * 400),)),
                     started_at="2026-11-22T10:00:00+00:00", ended_at="2026-11-22T10:00:01+00:00",
                     response=ModelResponse("t", model, usage, raw))


def test_per_call_price_is_optional_validated_and_added_to_token_cost():
    book = parse_prices(BASE)
    assert book.get("gpt-x").per_call_usd == 0.0 and book.get("serpapi/google").call_cost(0, 0) == 0.025
    assert book.get("serpapi/google").calls_cost(4) == pytest.approx(0.1)
    with pytest.raises(PricingError):
        parse_prices({**BASE, "price": [{**BASE["price"][1], "per_call_usd": -1}]})


def test_calls_are_priced_by_the_id_in_the_response_before_the_label():
    book = parse_prices(BASE)
    assert priced_id(call("decomposer", "gpt-x-2026-10-01", requested="gpt-x"), book, {}) == "gpt-x"  # dated id
    assert priced_id(call("judge", "gpt-x"), book, {}) == "gpt-x"
    assert priced_id(call("model-a", "unknown-id"), book, {"model-a": "gpt-x"}) == "gpt-x"  # label fallback
    assert priced_id(call("model-a", "unknown-id"), book, {}) is None


def test_cost_model_charges_auxiliary_calls_and_lists_unpriceable_ones():
    cost = CostModel(prices=parse_prices(BASE), model_ids={}, max_output_tokens={"decomposer": 1000})
    assert cost.call_cost(call("search", "serpapi/google")) == pytest.approx(0.025)
    assert cost.call_cost(call("search", "serpapi/google", cache_hit=True)) == 0.0
    assert cost.call_cost(call("decomposer", "gpt-x", usage=TokenUsage(1000, 100))) == pytest.approx(0.003)
    # no usage reported: prompt characters / 4 input tokens + max_output_tokens output tokens
    assert cost.call_cost(call("decomposer", "gpt-x")) == pytest.approx((100 * 2 + 1000 * 10) / 1e6)
    assert cost.call_cost(call("mystery", "nobody")) == 0.0 and cost.unpriced_clients == {"mystery"}
