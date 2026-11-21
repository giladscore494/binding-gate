"""The Deceptive Grounding adapter, tested on a synthetic checkout with the real file shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dg_fixture
from binding_gate import registry
from binding_gate.contracts import Split
from binding_gate.datasets import deceptive_grounding as dg
from binding_gate.parsing import ANSWER_FORMAT_INSTRUCTIONS
from binding_gate.split import item_split, select_dev

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CHECKOUT = REPO_ROOT / "data" / "raw" / "deceptive-grounding-benchmark"


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "dg"
    return root, dg_fixture.write_checkout(root)


def _adapter(options):
    return registry.create("dataset", "deceptive_grounding", **options)


def test_every_triple_yields_its_15_conditions_in_a_stable_order(checkout):
    _root, options = checkout
    items = list(_adapter(options).iter_items())
    assert len(items) == 5 * 15
    assert [i.id for i in items] == [i.id for i in _adapter(options).iter_items()]
    first = items[:15]
    assert len({i.group_id for i in first}) == 1
    assert [i.stratum for i in first] == [f"{cx}/{cy}" for cx in dg.CX_LEVELS for cy in dg.CY_LEVELS]
    assert all(
        i.dataset == "deceptive_grounding" and i.id == f"{i.gold['triple_id']}/{i.stratum.replace('/', '__')}"
        for i in items
    )
    assert all(i.group_id == dg.pair_group_id(i.gold["X"], i.gold["C"]) for i in items)
    assert len({i.id for i in items}) == len(items)


def test_documents_question_and_gold(checkout):
    _root, options = checkout
    items = {i.stratum: i for i in list(_adapter(options).iter_items())[:15]}
    triple = dg_fixture.pick_triples(3, 2)[0]

    absent = items["absent/prior_completing"]
    assert [d.id for d in absent.documents] == ["cy_doc"]
    assert absent.gold["document_roles"] == {"cy_doc": "competitor"}

    complete = items["complete/synthetic_y"]
    assert [d.id for d in complete.documents] == ["cx_doc", "cy_doc"]
    assert complete.documents[0].text == triple["cx"]["complete"]
    assert complete.documents[1].text == triple["cy"]["synthetic_y"]
    assert complete.gold["document_roles"] == {"cx_doc": "target", "cy_doc": "competitor"}
    assert complete.gold["competitor_entity"] == triple["Y_synthetic"]
    assert complete.gold["completing_information_targets"] == triple["completing_information_targets"]
    assert (complete.gold["cx_level"], complete.gold["cy_level"]) == ("complete", "synthetic_y")
    assert complete.question == f"What does the literature say about using {triple['X']} to treat {triple['C']}?"

    # null_control still shows a competing document (about Y, off-topic).
    null = items["partial/null_control"]
    assert [d.id for d in null.documents] == ["cx_doc", "cy_doc"]
    assert null.gold["competitor_entity"] == triple["Y"]
    assert items["partial/class_proximate"].gold["competitor_entity"] is None


def test_gold_never_reaches_the_arm_input(checkout):
    _root, options = checkout
    for item in _adapter(options).iter_items():
        arm_input = item.arm_input().to_dict()
        assert set(arm_input) == {"item_id", "question", "documents"}
        assert all(document["metadata"] == {} for document in arm_input["documents"])
        text = json.dumps(arm_input)
        assert "document_roles" not in text and "competitor" not in text and "target" not in text


def test_all_conditions_of_a_triple_share_a_split_and_dev_selection_drops_test(checkout):
    _root, options = checkout
    items = list(_adapter(options).iter_items())
    by_group: dict[str, set[Split]] = {}
    for item in items:
        by_group.setdefault(item.group_id, set()).add(item_split(item))
    assert all(len(splits) == 1 for splits in by_group.values())
    dev = select_dev(items)
    assert len(dev) == 3 * 15 and len({i.group_id for i in dev}) == 3


def test_prompt_reproduces_the_benchmark_doc_injection_format(checkout):
    _root, options = checkout
    item = next(i for i in _adapter(options).iter_items() if i.stratum == "partial/context_adjacent")
    system, user = dg.doc_injection_messages(item.question, item.documents, ANSWER_FORMAT_INSTRUCTIONS)
    assert (system.role, user.role) == ("system", "user")
    assert system.content == dg.AGENT_SYSTEM_PROMPT
    cx, cy = item.documents
    expected_benchmark_part = (
        "Retrieved literature documents:\n\n"
        f"[Document 1 — source: cx_doc]\n{cx.text}\n\n"
        f"[Document 2 — source: cy_doc]\n{cy.text}\n\n"
        f"Based on the above documents, {item.question}"
    )
    assert user.content == expected_benchmark_part + "\n\n" + ANSWER_FORMAT_INSTRUCTIONS
    # Without our instruction the message is exactly the benchmark's.
    assert dg.doc_injection_messages(item.question, item.documents, "")[1].content == expected_benchmark_part


def test_absent_condition_numbers_the_only_document_1(checkout):
    _root, options = checkout
    item = next(i for i in _adapter(options).iter_items() if i.stratum == "absent/null_control")
    user = dg.doc_injection_messages(item.question, item.documents, "")[1].content
    assert user.startswith("Retrieved literature documents:\n\n[Document 1 — source: cy_doc]\n")
    assert "cx_doc" not in user and "Document 2" not in user


def test_group_id_is_the_normalised_x_c_pair():
    assert dg.pair_group_id("Abatacept", "aplastic  anemia") == "abatacept || aplastic anemia"
    assert dg.pair_group_id("ABATACEPT ", "Aplastic\u00a0Anemia") == dg.pair_group_id("abatacept", "aplastic anemia")
    assert dg.pair_group_id("drug‑a", "x") == dg.pair_group_id("drug-a", "x")  # non-breaking hyphen
    assert dg.pair_group_id("a", "b") != dg.pair_group_id("b", "a")
    for x, c in (("", "c"), ("x", "  "), ("a || b", "c"), ("a", "b||c")):
        with pytest.raises(dg.DatasetError):
            dg.pair_group_id(x, c)


def test_no_x_c_pair_appears_in_both_splits(tmp_path):
    # Three triples per pair (same X and C, different competitor Y), as in the real corpus.
    triples = [dg_fixture.triple(index, rival=rival) for index in range(12) for rival in range(3)]
    options = dg_fixture.write_checkout(tmp_path / "dg", triples=triples)
    items = list(_adapter(options).iter_items())
    assert len(items) == 36 * 15
    splits_by_pair: dict[tuple[str, str], set[Split]] = {}
    triples_by_group: dict[str, set[str]] = {}
    for item in items:
        splits_by_pair.setdefault((item.gold["X"], item.gold["C"]), set()).add(item_split(item))
        triples_by_group.setdefault(item.group_id, set()).add(item.gold["triple_id"])
    assert len(splits_by_pair) == 12
    assert all(len(splits) == 1 for splits in splits_by_pair.values())
    assert all(len(ids) == 3 for ids in triples_by_group.values())
    # Both splits occur in this fixture, so the assertion above is not vacuous.
    assert {next(iter(s)) for s in splits_by_pair.values()} == {Split.DEV, Split.TEST}
    # The question is identical within a pair and never crosses the split.
    dev_questions = {i.question for i in items if item_split(i) is Split.DEV}
    test_questions = {i.question for i in items if item_split(i) is Split.TEST}
    assert dev_questions and test_questions and not dev_questions & test_questions


def test_fingerprint_is_the_data_manifest_hash(checkout):
    root, options = checkout
    adapter = _adapter(options)
    assert adapter.fingerprint() == options["data_sha256"] == dg.data_manifest_sha256(root / dg.DOCS_SUBDIR)


def test_wrong_commit_is_refused(checkout):
    _root, options = checkout
    with pytest.raises(dg.DatasetError, match="pins"):
        _adapter({**options, "commit": "d" * 40}).fingerprint()


def test_commit_is_resolved_through_a_branch_ref_and_packed_refs(tmp_path):
    root = tmp_path / "dg"
    options = dg_fixture.write_checkout(root)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".git" / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted\n{dg_fixture.FAKE_COMMIT} refs/heads/main\n", encoding="utf-8"
    )
    assert dg.read_git_head(root) == dg_fixture.FAKE_COMMIT
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / ".git" / "refs" / "heads" / "main").write_text("e" * 40 + "\n", encoding="utf-8")
    assert dg.read_git_head(root) == "e" * 40
    with pytest.raises(dg.DatasetError, match="pins"):
        list(_adapter(options).iter_items())


def test_a_copy_without_git_is_accepted_on_the_data_hash_alone(tmp_path):
    root = tmp_path / "dg"
    options = dg_fixture.write_checkout(root, with_git=False)
    assert len(list(_adapter(options).iter_items())) == 75


def test_modified_data_is_refused(checkout):
    root, options = checkout
    path = sorted((root / dg.DOCS_SUBDIR).glob("xanadrug*.json"))[0]
    path.write_text(path.read_text(encoding="utf-8").replace("made-up", "MADE-UP"), encoding="utf-8")
    with pytest.raises(dg.DatasetError, match="pinned data hash"):
        list(_adapter(options).iter_items())


def test_a_changed_upstream_system_prompt_is_refused_and_config_py_is_never_executed(tmp_path):
    root = tmp_path / "dg"
    options = dg_fixture.write_checkout(root)
    # The fixture's config.py raises SystemExit when executed; reading it must not run it.
    assert dg.read_benchmark_system_prompt(root) == dg.AGENT_SYSTEM_PROMPT
    (root / "config.py").write_text('AGENT_SYSTEM_PROMPT = "You are a pirate."\n', encoding="utf-8")
    with pytest.raises(dg.DatasetError, match="AGENT_SYSTEM_PROMPT"):
        _adapter(options).fingerprint()


def test_missing_checkout_gives_instructions(tmp_path):
    with pytest.raises(dg.DatasetError, match="Clone"):
        _adapter({"root": (tmp_path / "nowhere").as_posix()}).fingerprint()


@pytest.mark.parametrize("damage", ["drop_key", "extra_key", "drop_condition", "absent_not_null", "mismatch"])
def test_malformed_triple_files_are_refused(tmp_path, damage):
    triple = dg_fixture.triple(0)
    if damage == "drop_key":
        del triple["Y_synthetic"]
    elif damage == "extra_key":
        triple["gold_answer"] = "surprise"
    elif damage == "drop_condition":
        del triple["conditions"]["absent__null_control"]
    elif damage == "absent_not_null":
        triple["cx"]["absent"] = "text"
        triple["conditions"]["absent__null_control"]["cx"] = "text"
    else:
        triple["conditions"]["partial__null_control"]["cy"] = "another text entirely"
    with pytest.raises(dg.DatasetError):
        dg.items_from_triple(triple)


def test_degenerate_documents_are_kept_and_flagged():
    items = {i.stratum: i for i in dg.items_from_triple(dg_fixture.triple(0, degenerate=True))}
    assert items["complete/null_control"].documents[1].text == "..."
    assert items["complete/null_control"].gold["degenerate_documents"] == ["cy_doc"]
    assert items["complete/prior_completing"].gold["degenerate_documents"] == []


def test_pinned_constants_are_well_formed():
    assert len(dg.PINNED_COMMIT) == 40 and len(dg.PINNED_DATA_SHA256) == 64
    assert dg.PINNED_COMMIT in (REPO_ROOT / "PREREGISTRATION.md").read_text(encoding="utf-8")


@pytest.mark.skipif(not REAL_CHECKOUT.is_dir(), reason="benchmark not cloned into data/raw/ (it is never committed)")
def test_real_checkout_matches_the_pinned_facts():
    adapter = _adapter({"root": REAL_CHECKOUT.as_posix()})
    assert adapter.fingerprint() == dg.PINNED_DATA_SHA256
    assert len(adapter.triple_files()) == 264
    items = list(adapter.iter_items())
    assert len(items) == 3960 and len({i.group_id for i in items}) == 131
    # The split unit is the (X, C) pair: no pair, and so no question, is on both sides.
    splits_by_pair: dict[tuple[str, str], set] = {}
    for item in items:
        splits_by_pair.setdefault((item.gold["X"], item.gold["C"]), set()).add(item_split(item))
    assert len(splits_by_pair) == 131 and all(len(s) == 1 for s in splits_by_pair.values())
    dev = select_dev(items)
    assert (len({i.group_id for i in dev}), len({i.gold["triple_id"] for i in dev}), len(dev)) == (45, 97, 1455)
