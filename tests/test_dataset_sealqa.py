"""SealQA adapter (layer 3) on a synthetic checkout in the documented shape."""

from __future__ import annotations

import json

import pytest

import sealqa_fixture as fx
from binding_gate import registry
from binding_gate.answering import build_answer_request
from binding_gate.contracts import ArmInput, Document, Split
from binding_gate.datasets import sealqa
from binding_gate.split import assign_split, select_dev
from binding_gate.textnorm import normalize_text


@pytest.fixture
def options(tmp_path, monkeypatch):
    fx.DOWNLOADS.clear()
    return fx.write(tmp_path / "sealqa", monkeypatch)


def adapter(options):
    return registry.create("dataset", "sealqa", **options)


def test_pins_are_the_values_read_from_the_hub_on_2026_11_22():
    assert sealqa.PINNED_REVISION == "0e75db526ad93dd039714897b7631afc03510e30"
    assert set(sealqa.FILE_MANIFEST) == {"LICENSE", "README.md", "seal-hard.parquet", "seal-0.parquet"}
    assert "longseal.parquet" not in sealqa.FILE_MANIFEST  # layer 3 does not use LongSeal
    assert sealqa.FILE_MANIFEST["seal-hard.parquet"] == (49966, "b13fbacabac4b055dcabfae161df4dc661e31e915507324790779d107ab53e7e")
    assert sealqa.FILE_MANIFEST["seal-0.parquet"] == (27169, "89c729bd0a13c56847bb30cbdce1e091f7b832bbe9929d3744357cedcf4e33be")
    assert sealqa.resolve_url(sealqa.PINNED_REVISION, "seal-0.parquet") == (
        "https://huggingface.co/datasets/vtllms/sealqa/resolve/0e75db526ad93dd039714897b7631afc03510e30/seal-0.parquet"
    )


def test_prepare_downloads_at_the_pinned_revision_and_records_the_conversion(options, tmp_path):
    root = tmp_path / "sealqa"
    assert all(sealqa.PINNED_REVISION in url for url in fx.DOWNLOADS) and len(fx.DOWNLOADS) == 4
    conversion = json.loads((root / "conversion.json").read_text())
    assert conversion["revision"] == sealqa.PINNED_REVISION and conversion["pyarrow_version"] == "fixture-1.0"
    assert conversion["subsets"]["seal_hard"]["rows"] == 30 and conversion["subsets"]["seal_0"]["rows"] == 12
    assert conversion["subsets"]["seal_hard"]["missing_expected_columns"] == []
    lines = (root / "seal_hard.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["answer"] == "Town 0"


def test_a_corrupted_download_is_refused_and_present_files_are_not_downloaded_again(tmp_path, monkeypatch):
    fx.write(tmp_path / "a", monkeypatch)
    target = tmp_path / "b"
    target.mkdir()
    for name in ("LICENSE", "README.md", "seal-hard.parquet"):
        (target / name).write_bytes((tmp_path / "a" / name).read_bytes())
    requested = []

    def corrupt(url, destination):
        requested.append(url.rsplit("/", 1)[1])
        destination.write_bytes(b"not the pinned bytes")

    with pytest.raises(sealqa.DatasetError, match="seal-0.parquet"):
        sealqa.prepare(target, download=corrupt, read_parquet=lambda p: ([], "x"))
    assert requested == ["seal-0.parquet"] and not (target / "seal-0.parquet").exists()


def test_items_map_one_question_to_one_item_with_gold_out_of_reach_of_arms(options):
    items = list(adapter(options).iter_items())
    assert len(items) == 30  # seal_hard; the 12 seal_0 questions are not duplicated
    first = items[0]
    assert first.dataset == "sealqa" and first.documents == () and first.id == first.group_id
    assert first.group_id == "sealqa/" + __import__("hashlib").sha256(normalize_text(first.question).encode()).hexdigest()[:16]
    assert first.gold["answers"] == ["Town 0"] and first.gold["subsets"] == ["seal_hard", "seal_0"]
    assert first.gold["annotator_urls"] == ["https://annotator.example/0"]
    assert first.stratum == "seal_0/never"
    assert items[20].stratum == "seal_hard/" + items[20].gold["freshness"] and items[20].gold["subsets"] == ["seal_hard"]
    view = first.arm_input().to_dict()
    assert "Town 0" not in json.dumps(view) and "annotator" not in json.dumps(view)


def test_group_id_ignores_case_and_spacing_of_the_question():
    assert sealqa.group_id_of("Who  won?") == sealqa.group_id_of("who won?")


def test_dev_selection_uses_the_preregistered_split(options):
    items = list(adapter(options).iter_items())
    dev = select_dev(items)
    assert dev and all(assign_split("sealqa", i.group_id) is Split.DEV for i in dev)
    assert len(dev) < len(items)


def test_fingerprint_covers_the_converted_rows_and_a_tampered_file_is_refused(options, tmp_path):
    a = adapter(options)
    fingerprint = a.fingerprint()
    assert len(fingerprint) == 64 and adapter(options).fingerprint() == fingerprint
    jsonl = tmp_path / "sealqa" / "seal_hard.jsonl"
    jsonl.write_text(jsonl.read_text().replace("Town 3", "Town 33"))
    with pytest.raises(sealqa.DatasetError, match="does not match the hash"):
        adapter(options).fingerprint()


def test_missing_preparation_explains_what_to_do(tmp_path):
    with pytest.raises(sealqa.DatasetError, match="dataset-prepare"):
        registry.create("dataset", "sealqa", root=str(tmp_path / "nothing")).fingerprint()


def test_inconsistent_answers_and_a_seal_0_question_outside_seal_hard_are_refused(tmp_path, monkeypatch):
    hard, zero = fx.default_rows(5, 2)
    zero[0] = dict(zero[0], answer="Another town")
    opts = fx.write(tmp_path / "x", monkeypatch, hard, zero)
    with pytest.raises(sealqa.DatasetError, match="different answer"):
        list(adapter(opts).iter_items())
    hard, zero = fx.default_rows(5, 2)
    zero.append(fx.row(99))
    opts = fx.write(tmp_path / "y", monkeypatch, hard, zero)
    with pytest.raises(sealqa.DatasetError, match="not found in seal_hard"):
        list(adapter(opts).iter_items())


def test_rows_without_question_or_answer_are_refused(tmp_path, monkeypatch):
    hard, zero = fx.default_rows(3, 1)
    hard[1]["answer"] = "  "
    opts = fx.write(tmp_path / "z", monkeypatch, hard, zero)
    with pytest.raises(sealqa.DatasetError, match="answer"):
        list(adapter(opts).iter_items())


def test_conversion_requires_the_question_and_answer_columns(tmp_path, monkeypatch):
    hard, zero = fx.default_rows(3, 1)
    for r in hard:
        del r["answer"]
    with pytest.raises(sealqa.DatasetError, match="required column"):
        fx.write(tmp_path / "w", monkeypatch, hard, zero)


@pytest.mark.parametrize(
    "bad",
    [{"revision": "main"}, {"subsets": ["seal_0"]}, {"subsets": ["seal_hard", "longseal"]}, {"subsets": []}],
)
def test_options_are_validated(tmp_path, bad):
    with pytest.raises(ValueError):
        registry.create("dataset", "sealqa", root=str(tmp_path), **bad)


def test_live_web_presentation_shows_url_date_question_and_answer_format():
    docs = (
        Document.create("web1", "Example Games 2031\nHosted by Northbridge.", url="https://e.test/a",
                        metadata={"retrieved_on": "2026-11-22"}),
    )
    request = build_answer_request(ArmInput(item_id="i", question="Who hosted?", documents=docs), "sealqa_live_web")
    system, user = request.messages
    assert system.content == sealqa.SYSTEM_PROMPT
    assert user.content.startswith("Web pages returned by a search on 2026-11-22:\n\n[Document 1 — source: web1]\nURL: https://e.test/a\n")
    assert "\n\nQuestion: Who hosted?\n\n" in user.content and '"source_id"' in user.content
    empty = build_answer_request(ArmInput(item_id="i", question="Who hosted?"), "sealqa_live_web")
    assert empty.messages[1].content.startswith(sealqa.NO_DOCUMENTS)
