"""The GRAB-RAG adapter, tested on a synthetic checkout and export with the real file shapes."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

import grab_rag_fixture as fx
from binding_gate import registry
from binding_gate.answering import PRESENTATIONS, build_answer_request
from binding_gate.contracts import Split
from binding_gate.datasets import grab_rag as gr
from binding_gate.parsing import ANSWER_FORMAT_INSTRUCTIONS
from binding_gate.split import item_split, select_dev

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CHECKOUT = REPO_ROOT / "data" / "raw" / "grab-rag"


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "grab-rag"
    return root, fx.write_checkout(root)


def _adapter(options):
    return registry.create("dataset", "grab_rag", **options)


def _items(options):
    return list(_adapter(options).iter_items())


# -- mapping ------------------------------------------------------------------- #


def test_items_come_per_question_misleading_then_supportive_in_file_order(checkout):
    _root, options = checkout
    adapter = _adapter(options)
    items = list(adapter.iter_items())
    # 6 questions per source dataset; qi 3 is skipped by the export (passages null),
    # so 5 misleading + 5 paired supportive items each.
    assert len(items) == 2 * (5 + 5)
    assert adapter.coverage == {
        label: {"questions": 6, "misleading": 5, "misleading_skipped": 1, "planted_underivable": 0, "supportive": 5}
        for label in ("natural_questions", "hotpotqa")
    }
    assert [i.id for i in items] == [i.id for i in _items(options)]
    assert len({i.id for i in items}) == len(items)
    first, second = items[0], items[1]
    assert first.gold["condition"] == "misleading" and second.gold["condition"] == "supportive"
    assert first.group_id == second.group_id == f"natural_questions/{first.gold['question_id']}"
    for item in items:
        label, qid, condition = item.id.split("/")
        assert item.dataset == "grab_rag" and item.stratum == f"{label}/{condition}"
        assert item.group_id == gr.group_id_of(label, qid) and item.gold["question_id"] == qid
        assert item.gold["grab_condition"] == gr.GRAB_CONDITION[condition]
    records = fx.pick_records("natural_questions", 3, 3)
    assert first.question == records[0]["question"]
    skipped = records[3]["id"]
    assert not [i for i in items if i.gold["question_id"] == skipped]


def test_misleading_item_marks_the_edited_passage_and_the_planted_answer(checkout):
    _root, options = checkout
    record = fx.pick_records("natural_questions", 3, 3)[0]
    passages = fx.qc_passages(record, 0, fx.pick_records("natural_questions", 3, 3))
    item = next(i for i in _items(options) if i.id == f"natural_questions/{record['id']}/misleading")
    assert [d.id for d in item.documents] == ["doc1", "doc2", "doc3", "doc4", "doc5"]
    assert [d.text for d in item.documents] == [f"{p['title']}: {p['text']}" for p in passages]
    edited_position = next(i for i, p in enumerate(passages) if "pid" not in p)
    misleading_id = f"doc{edited_position + 1}"
    roles = item.gold["document_roles"]
    assert roles[misleading_id] == "competitor" and item.gold["misleading_document_id"] == misleading_id
    assert sorted(r for d, r in roles.items() if d != misleading_id) == ["other"] * 4
    assert item.gold["planted_answer"] == fx.planted_name(record)
    assert item.gold["planted_answer_note"] is None
    assert item.gold["replaced_text"] == [record["answers"][0]]
    assert item.gold["answers"] == record["answers"]
    assert item.gold["edited_gold_passage_index"] == 0 and item.gold["title_edited"] is False
    assert item.gold["edit_similarity"] > 0.8
    assert set(item.gold["hard_negative_pids"]) == {f"doc{i}" for i in range(1, 6)} - {misleading_id}
    assert fx.planted_name(record) in item.documents[edited_position].text
    assert not any(gr.contains_answer(d.text, record["answers"]) for d in item.documents)


def test_hotpotqa_misleading_shows_one_edited_gold_passage_and_drops_the_other(checkout):
    _root, options = checkout
    items = [i for i in _items(options) if i.stratum == "hotpotqa/misleading"]
    for item in items:
        assert item.gold["n_gold_passages"] == 2 and item.gold["edited_gold_passage_index"] == 0
        assert item.gold["planted_answer"].startswith("Zorbland")
        assert list(item.gold["document_roles"].values()).count("competitor") == 1


def test_supportive_item_reproduces_q100(checkout):
    _root, options = checkout
    items = {i.id: i for i in _items(options)}
    nq = fx.pick_records("natural_questions", 3, 3)
    item = items[f"natural_questions/{nq[0]['id']}/supportive"]
    gold_text = f"{nq[0]['gold_passages'][0]['title']}: {nq[0]['gold_passages'][0]['text']}"
    assert [d.text for d in item.documents] == [gold_text] * 5  # NQ: one gold passage in all 5 slots
    assert set(item.gold["document_roles"].values()) == {"target"}
    assert item.gold["planted_answer"] is None
    hp = fx.pick_records("hotpotqa", 3, 3)
    for qi in (0, 1, 2):
        item = items[f"hotpotqa/{hp[qi]['id']}/supportive"]
        slots = [hp[qi]["gold_passages"][i % 2] for i in range(5)]
        random.Random(42 + qi).shuffle(slots)
        assert [d.text for d in item.documents] == [gr.passage_text(p) for p in slots]
        assert list(item.gold["gold_passage_index"].values()) == gr.supportive_passage_order(2, qi)


def test_supportive_shuffle_is_pinned():
    # The permutation of random.shuffle is not guaranteed across Python versions. These
    # vectors are what build_condition("Q100") gives with Random(42 + qi) on CPython 3.11
    # and 3.13; if this fails, supportive items no longer match GRAB-RAG's.
    assert [gr.supportive_passage_order(2, qi) for qi in range(4)] == [
        [1, 1, 0, 0, 0], [1, 0, 1, 0, 0], [0, 1, 0, 0, 1], [0, 0, 1, 1, 0],
    ]  # fmt: skip
    assert gr.supportive_passage_order(1, 7) == [0] * 5


def test_supportive_questions_option(tmp_path):
    options = fx.write_checkout(tmp_path / "g")
    everything = _items({**options, "supportive_questions": "all"})
    assert sum(i.gold["condition"] == "supportive" for i in everything) == 12
    only_misleading = _items({**options, "conditions": ["misleading"]})
    assert {i.gold["condition"] for i in only_misleading} == {"misleading"} and len(only_misleading) == 10
    nq_only = _items({**options, "source_datasets": ["natural_questions"]})
    assert {i.gold["source_dataset"] for i in nq_only} == {"natural_questions"}


def test_supportive_alone_needs_no_export(tmp_path):
    options = fx.write_checkout(tmp_path / "g", export=False)
    adapter = _adapter(options)
    items = list(adapter.iter_items())
    assert len(items) == 12 and {i.gold["condition"] for i in items} == {"supportive"}
    assert adapter.fingerprint() == options["data_sha256"]


def test_gold_never_reaches_the_arm_input(checkout):
    _root, options = checkout
    for item in _items(options):
        arm_input = item.arm_input().to_dict()
        assert set(arm_input) == {"item_id", "question", "documents"}
        assert all(d["metadata"] == {} for d in arm_input["documents"])
        # The item id names the condition, as Deceptive Grounding's ids do; it is never put in a
        # prompt (answering.build_answer_request uses question and documents only) and the gate
        # may use only the question, the constraints and the document text (ARCHITECTURE.md 3).
        assert build_answer_request(item.arm_input(), "grab_rag_context").to_dict() == build_answer_request(
            item.arm_input().__class__(item_id="x", question=item.question, documents=item.documents),
            "grab_rag_context",
        ).to_dict()
        text = json.dumps({k: v for k, v in arm_input.items() if k != "item_id"})
        for secret in ("document_roles", "competitor", "planted", "misleading", "supportive", "target"):
            assert secret not in text
        assert [d["id"] for d in arm_input["documents"]] == [f"doc{n}" for n in range(1, 6)]


# -- split ------------------------------------------------------------------------ #


def test_no_question_is_in_both_splits_and_dev_drops_test(checkout):
    _root, options = checkout
    items = _items(options)
    splits_by_question: dict[tuple[str, str], set[Split]] = {}
    for item in items:
        key = (item.gold["source_dataset"], item.gold["question_id"])
        splits_by_question.setdefault(key, set()).add(item_split(item))
    assert all(len(s) == 1 for s in splits_by_question.values())
    assert {next(iter(s)) for s in splits_by_question.values()} == {Split.DEV, Split.TEST}
    dev = select_dev(items)
    assert dev and all(item_split(i) is Split.DEV for i in dev)
    dev_questions = {i.question for i in dev}
    test_questions = {i.question for i in items if item_split(i) is Split.TEST}
    assert not dev_questions & test_questions
    # Each dev question that has a misleading item has its paired supportive item, too.
    by_group: dict[str, set[str]] = {}
    for item in dev:
        by_group.setdefault(item.group_id, set()).add(item.gold["condition"])
    assert all(conditions == {"misleading", "supportive"} for conditions in by_group.values())


# -- integrity --------------------------------------------------------------------- #


def test_fingerprint_covers_data_and_export(checkout):
    root, options = checkout
    with_export = _adapter(options).fingerprint()
    assert options["data_sha256"] == gr.data_manifest_sha256(root)
    assert with_export != options["data_sha256"] and len(with_export) == 64
    assert _adapter({**options, "conditions": ["supportive"], "contexts": None, "contexts_sha256": None}).fingerprint() == options["data_sha256"]


def test_wrong_commit_is_refused(checkout):
    _root, options = checkout
    with pytest.raises(gr.DatasetError, match="pins"):
        _adapter({**options, "commit": "b" * 40}).fingerprint()


def test_a_copy_without_git_is_accepted_on_the_data_hash_alone(tmp_path):
    options = fx.write_checkout(tmp_path / "g", with_git=False)
    assert len(_items(options)) == 20


def test_modified_eval_data_is_refused(checkout):
    root, options = checkout
    path = root / "data" / "hotpotqa" / "eval.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace("invented", "INVENTED", 1), encoding="utf-8")
    with pytest.raises(gr.DatasetError, match="pinned data hash"):
        _items(options)


def test_an_extra_eval_file_changes_the_manifest(checkout):
    root, options = checkout
    fx.write_jsonl(root / "data" / "extra" / "eval.jsonl", [{"id": "x"}])
    with pytest.raises(gr.DatasetError, match="pinned data hash"):
        _adapter(options).fingerprint()


def test_changed_upstream_system_prompt_is_refused_and_policies_py_is_never_executed(checkout):
    root, options = checkout
    assert gr.read_upstream_system_prompt(root) == gr.SYSTEM_PROMPT  # the file raises SystemExit if run
    (root / "src" / "policies.py").write_text('sys_base = "You are a pirate."\n', encoding="utf-8")
    with pytest.raises(gr.DatasetError, match="sys_base"):
        _adapter(options).fingerprint()


def test_missing_checkout_gives_instructions(tmp_path):
    with pytest.raises(gr.DatasetError, match="Clone"):
        _adapter({"root": (tmp_path / "nowhere").as_posix(), "conditions": ["supportive"]}).fingerprint()


def test_missing_export_explains_how_it_is_produced(checkout):
    _root, options = checkout
    with pytest.raises(gr.DatasetError, match="build_condition"):
        _adapter({**options, "contexts": options["contexts"] + ".missing"}).fingerprint()


def test_unpinned_export_reports_its_hash_and_a_changed_export_is_refused(checkout):
    _root, options = checkout
    with pytest.raises(gr.DatasetError, match=options["contexts_sha256"]):
        _adapter({**options, "contexts_sha256": "UNPINNED"}).fingerprint()
    rows = fx.read_export(options)
    fx.write_jsonl(Path(options["contexts"]), rows[::-1])
    with pytest.raises(gr.DatasetError, match="does not match the pinned hash"):
        _adapter(options).fingerprint()


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"conditions": []}, "non-empty list"),
        ({"conditions": ["degraded"]}, "distinct values"),
        ({"conditions": ["misleading", "misleading"]}, "distinct values"),
        ({"source_datasets": ["squad"]}, "distinct values"),
        ({"contexts": None}, "needs dataset option contexts"),
        ({"contexts_sha256": None}, "contexts_sha256 is required"),
        ({"conditions": ["supportive"]}, "only used by the misleading"),
        ({"conditions": ["supportive"], "contexts": None, "supportive_questions": "with_misleading"}, "needs the misleading"),
        ({"supportive_questions": "some"}, "supportive_questions"),
        ({"seed": "42"}, "seed"),
        ({"root": ""}, "root"),
    ],
)
def test_invalid_options_are_refused(checkout, overrides, message):
    _root, options = checkout
    with pytest.raises(gr.DatasetError, match=message):
        _adapter({**options, **overrides})


# -- malformed inputs ------------------------------------------------------------------ #


def _damaged_export(options, change):
    rows = fx.read_export(options)
    change(rows)
    return fx.rewrite_export(options, rows)


def _first_with_passages(rows):
    return next(r for r in rows if r["passages"] is not None)


def _edited(row):
    return next(p for p in row["passages"] if "pid" not in p)


def _negative(row):
    return next(p for p in row["passages"] if "pid" in p)


def _leak_answer(rows):
    row = _first_with_passages(rows)
    record = next(r for r in fx.pick_records(row["dataset"], 3, 3) if r["id"] == row["question_id"])
    _negative(row)["text"] = f"It mentions {record['answers'][0]} . Also more words."


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda rows: rows.pop(0), "has no line for"),
        (lambda rows: rows.append({**rows[0], "question_id": "nobody"}), "not in eval.jsonl"),
        (lambda rows: rows.append(dict(rows[0])), "duplicate export line"),
        (lambda rows: rows[0].update(condition="Q50"), "condition must be 'QC'"),
        (lambda rows: rows[0].update(seed=43), "seed"),
        (lambda rows: rows[0].update(extra=1), "exactly the keys"),
        (lambda rows: rows[0].update(passages="text"), "list or null"),
        (lambda rows: _first_with_passages(rows)["passages"].pop(), "list of 5 passages"),
        (lambda rows: _negative(_first_with_passages(rows)).pop("pid"), "exactly one passage must lack 'pid'"),
        (lambda rows: _edited(_first_with_passages(rows)).update(pid="x_0", score=1.0), "exactly one passage must lack 'pid'"),
        (lambda rows: _negative(_first_with_passages(rows)).update(score="high"), "numeric score"),
        (lambda rows: _negative(_first_with_passages(rows)).update(colour="red"), "exactly the keys"),
        (_leak_answer, "contains a gold answer"),
        (
            lambda rows: _edited(_first_with_passages(rows)).update(
                text="A completely unrelated invented text about weather patterns over a fictional desert plateau."
            ),
            "not an edit of any gold passage",
        ),
    ],
)
def test_malformed_exports_are_refused(checkout, change, message):
    _root, options = checkout
    with pytest.raises(gr.DatasetError, match=message):
        _items(_damaged_export(options, change))


def test_a_hard_negative_that_is_a_gold_passage_is_refused():
    records = fx.pick_records("hotpotqa", 3, 3)
    record = records[0]
    passages = fx.qc_passages(record, 0, records)
    negative = next(p for p in passages if "pid" in p)
    negative.update(title="x", text=record["gold_passages"][1]["text"].replace(record["answers"][0], "Another river"))
    record = {**record, "gold_passages": [record["gold_passages"][0], {"title": "x", "text": negative["text"]}]}
    with pytest.raises(gr.DatasetError, match="is a gold passage"):
        gr.misleading_item("hotpotqa", record, passages)


@pytest.mark.parametrize(
    "damage",
    ["drop_key", "extra_key", "wrong_dataset", "empty_answers", "no_gold", "slash_in_id", "passage_extra_key"],
)
def test_malformed_eval_records_are_refused(damage):
    record = fx.nq_record(1)
    if damage == "drop_key":
        del record["answers"]
    elif damage == "extra_key":
        record["context"] = []
    elif damage == "wrong_dataset":
        record["dataset"] = "hotpotqa"
    elif damage == "empty_answers":
        record["answers"] = []
    elif damage == "no_gold":
        record["gold_passages"] = []
    elif damage == "slash_in_id":
        record["id"] = "a/b"
    else:
        record["gold_passages"][0]["url"] = "http://example.invalid"
    with pytest.raises(gr.DatasetError):
        gr.validate_record(record, "natural_questions")


# -- planted answer ---------------------------------------------------------------- #


def test_planted_answer_through_article_flip_title_edit_and_repeated_occurrences():
    gold = {"title": "Omar Quill", "text": "Omar Quill was a Belgian painter. Later, Omar Quill moved to a Belgian port."}
    edited = {"title": "Ida Rhee", "text": "Ida Rhee was an Italian painter. Later, Ida Rhee moved to an Italian port."}
    # Two different replacements -> not derivable (GRAB-RAG writes one replacement per edit).
    planted = gr.derive_planted_answer(gold, edited, ["Omar Quill"])
    assert planted.value is None and "different spans" in planted.note
    edited = {"title": "Ida Rhee", "text": "Ida Rhee was a Belgian painter. Later, Ida Rhee moved to a Belgian port."}
    planted = gr.derive_planted_answer(gold, edited, ["Omar Quill"])
    assert (planted.value, planted.title_edited, planted.replaced) == ("Ida Rhee", True, ("Omar Quill",) * 3)
    # An a/an flip in front of the replacement is not part of it.
    gold = {"title": "", "text": "He was born to a Belgian family in 1901."}
    edited = {"title": "", "text": "He was born to an Italian family in 1901."}
    assert gr.derive_planted_answer(gold, edited, ["Belgian"]).value == "Italian"


def test_planted_answer_for_years_and_partially_shared_names():
    gold = {"title": "T", "text": "Founded in 1977 , it closed in 1990 ; the 1977 charter survives ."}
    edited = {"title": "T", "text": "Founded in 1947 , it closed in 1990 ; the 1947 charter survives ."}
    assert gr.derive_planted_answer(gold, edited, ["1977"]).value == "1947"
    # Only the differing tokens are recovered when the replacement shares a token with the answer;
    # value matching then needs only that core ("Delhi"), which is still specific to the plant.
    gold = {"title": "T", "text": "The office moved to New York City in 1950 ."}
    edited = {"title": "T", "text": "The office moved to New Delhi in 1950 ."}
    planted = gr.derive_planted_answer(gold, edited, ["New York City"])
    assert (planted.value, planted.replaced) == ("Delhi", ("York City",))


def test_planted_answer_failures_are_recorded_not_raised():
    gold = {"title": "T", "text": "Alpha beta gamma delta ."}
    assert "does not differ" in gr.derive_planted_answer(gold, dict(gold), ["beta"]).note
    assert "deletes text" in gr.derive_planted_answer(gold, {"title": "T", "text": "Alpha gamma delta ."}, ["beta"]).note
    leaked = gr.derive_planted_answer(gold, {"title": "T", "text": "Alpha beta2 gamma delta ."}, ["Beta2"])
    assert "contains a gold answer" in leaked.note


def test_an_underivable_plant_keeps_the_item(checkout):
    _root, options = checkout

    def change(rows):
        edited = _edited(_first_with_passages(rows))
        edited["text"] = edited["text"].replace("made-up", "invented-ish", 1)  # a second, different change

    adapter = _adapter(_damaged_export(options, change))
    items = list(adapter.iter_items())
    assert len(items) == 20 and adapter.coverage["natural_questions"]["planted_underivable"] == 1
    item = items[0]
    assert item.gold["planted_answer"] is None and "different spans" in item.gold["planted_answer_note"]


def test_answer_normalisation_matches_grab_rag_rules():
    assert gr.answer_normalize("The 1st Battalion, 1,000 men!") == "first battalion 1000 men"
    assert gr.contains_answer("It was the Beatles' album.", ["Beatles"])
    assert not gr.contains_answer("In 19770 BC", ["1977"])
    assert gr.contains_answer("an answer", ["", "answer"]) and not gr.contains_answer("x", [""])


# -- presentation ------------------------------------------------------------------- #


def test_presentation_is_grab_rag_p0_with_our_headers_and_answer_format(checkout):
    _root, options = checkout
    item = _items(options)[0]
    assert "grab_rag_context" in PRESENTATIONS
    request = build_answer_request(item.arm_input(), "grab_rag_context")
    system, user = request.messages
    assert (system.role, system.content) == ("system", gr.SYSTEM_PROMPT)
    blocks = "\n\n".join(f"[Document {n} — source: doc{n}]\n{d.text}" for n, d in enumerate(item.documents, start=1))
    assert user.content == f"Context:\n{blocks}\n\nQuestion: {item.question}\n\n{ANSWER_FORMAT_INSTRUCTIONS}"
    assert gr.context_messages(item.question, item.documents, "")[1].content == f"Context:\n{blocks}\n\nQuestion: {item.question}"


def test_no_gate_accepts_the_presentation():
    arm = registry.create("arm", "no_gate", model=object(), presentation="grab_rag_context")
    assert arm.presentation == "grab_rag_context"


def test_pinned_constants_are_well_formed_and_documented():
    assert len(gr.PINNED_COMMIT) == 40 and len(gr.PINNED_DATA_SHA256) == 64
    prereg = (REPO_ROOT / "PREREGISTRATION.md").read_text(encoding="utf-8")
    assert gr.PINNED_COMMIT in prereg and gr.PINNED_DATA_SHA256 in prereg


# -- the real checkout (only where it has been cloned; never committed) ----------------- #


@pytest.mark.skipif(not REAL_CHECKOUT.is_dir(), reason="GRAB-RAG not cloned into data/raw/ (it is never committed)")
def test_real_checkout_matches_the_pinned_facts():
    adapter = _adapter({"root": REAL_CHECKOUT.as_posix(), "conditions": ["supportive"]})
    assert adapter.fingerprint() == gr.PINNED_DATA_SHA256
    items = list(adapter.iter_items())
    assert adapter.coverage == {
        label: {"questions": 500, "misleading": 0, "misleading_skipped": 0, "planted_underivable": 0, "supportive": 500}
        for label in gr.SOURCE_DATASETS
    }
    assert len({i.group_id for i in items}) == 1000
    dev = select_dev(items)
    assert len({i.group_id for i in dev if i.gold["source_dataset"] == "natural_questions"}) == 191
    assert len({i.group_id for i in dev if i.gold["source_dataset"] == "hotpotqa"}) == 203
