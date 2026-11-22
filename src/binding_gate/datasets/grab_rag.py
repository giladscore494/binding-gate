"""Dataset adapter for GRAB-RAG (layer 2: the misleading-context condition).

Source: https://github.com/andre44s/grab-rag (code for arXiv 2608.22228,
"Prompt-Based Abstention Fails Under Misleading Context: A Controlled Study of
Small Frozen RAG Models", AACL-IJCNLP 2026), pinned at commit
:data:`PINNED_COMMIT`. Code licence: MIT (``LICENSE`` of the checkout). Data
licences, as stated in the checkout's ``README.md`` and on the datasets' cards:
Natural Questions-derived records CC BY-SA 3.0, HotpotQA-derived records
CC BY-SA 4.0. Nothing from GRAB-RAG is redistributed with this repository; the
user clones it into ``data/raw/`` (git-ignored) and the run config points there.

The checkout is treated as untrusted data: this module only reads files. It
never imports or executes anything from the checkout and never runs ``git`` in
it (the commit is read from ``.git`` files; ``src/policies.py`` is parsed with
:mod:`ast` to compare the system prompt, never executed).

What the checkout ships (inspected at the pinned commit)
--------------------------------------------------------
``data/natural_questions/eval.jsonl`` and ``data/hotpotqa/eval.jsonl``, 500
records each, one JSON object per line, sorted by ``id``, with exactly the keys
``id``, ``question``, ``answers`` (list of gold answer strings; 1-10 aliases for
NQ, exactly 1 for HotpotQA), ``gold_passages`` (list of ``{"title", "text"}``;
exactly 1 for NQ, exactly 2 for HotpotQA) and ``dataset``.

What it does NOT ship: the context conditions. They are built at run time by
``src/context_quality.build_condition`` inside ``src/runner.py``:

=========  ===========  ==========================================================
paper      code         passages shown (5 by default)
=========  ===========  ==========================================================
supportive ``Q100``     the gold passages repeated to fill 5 slots, shuffled
degraded   ``Q50``      3 gold slots + 2 hard negatives, shuffled
missing    ``Q0``       5 hard negatives
misleading ``QC``       1 edited gold passage + 4 hard negatives, shuffled
(reference) ``Qclosed`` no passages
=========  ===========  ==========================================================

Hard negatives come from a hybrid BM25 + BGE dense retriever over the pool of
all gold passages of the same file (``src/retrieval.py``); they are the
top-ranked passages that are not gold and contain no answer alias. The edited
passage of ``QC`` comes from ``ContradictoryGenerator``: the first gold passage
in which the gold answer span can be swapped. The swap replaces the answer span
(every whole-word, case-insensitive occurrence, plus overlapping aliases; also
in the title) by a year shifted by +-10/20/30, a number shifted by 10-30 %, or a
different named entity of the same GLiNER type taken from a pool of entities
found in the gold passages; the result must no longer contain any gold answer.
Questions where no swap succeeds are skipped for ``QC`` only (the paper reports
202 of 500 NQ and 330 of 500 HotpotQA questions as QC-eligible; not checked by
us). Building ``QC`` therefore needs the GLiNER and BGE models and a GPU, and
GRAB-RAG's runner does not write the passages it showed anywhere (its result
rows hold only the model output). Hence:

* ``supportive`` is built here, deterministically and without any model, from
  ``eval.jsonl``: exactly what ``build_condition(..., "Q100", rng)`` returns,
  with ``rng = random.Random(seed + qi)`` as in ``src/runner.py`` (``qi`` = index
  of the record in its file, ``seed`` = 42).
* ``misleading`` is read from a *context export* (option ``contexts``): a JSON
  Lines file with one line per question of the selected source datasets,
  produced OUTSIDE this repository by running GRAB-RAG's own code at the pinned
  commit (see :data:`EXPORT_PROCEDURE`). It does not exist yet; until it does,
  only ``supportive`` items can be built. Its SHA-256 is pinned in the run
  config (``contexts_sha256``) and checked on every load.

What "misleading" is, and why it matters for this study
--------------------------------------------------------
The edit substitutes the ANSWER, not the entity the question is about. The
question's subject stays in the passage and the passage now asserts a wrong
value (or, when the answer is the passage's own subject, a wrong name) for it.
This is a counterfactual knowledge conflict (the paper adapts the entity-swap
method of Longpre et al. 2021), not an entity-attribution mismatch of the kind
the gate targets: a quote from the edited passage is textually *about the right
entity*. See PREREGISTRATION.md section 3, layer 2, for the consequences.

How the edited passage is identified
------------------------------------
GRAB-RAG does not label it. In ``build_condition``'s output the edited passage
is a plain ``{"title", "text"}`` dict, whereas hard negatives come from the
retriever as ``{"pid", "title", "text", "score"}``. The adapter requires exactly
one passage without ``pid`` and checks it: it must contain no gold answer
(GRAB-RAG's own ``contains_answer`` rule, re-implemented in
:func:`contains_answer`) and must be an edit of one of the record's gold
passages (:func:`match_edited_gold_passage`). The planted wrong answer is not
recorded by GRAB-RAG either; it is recovered by a token diff between the gold
passage and its edit (:func:`derive_planted_answer`). When the diff does not
yield one consistent replacement, ``gold["planted_answer"]`` is ``None`` and the
reason is recorded; the item is kept, never dropped.

Mapping onto :class:`~binding_gate.contracts.Item`
--------------------------------------------------
* ``id`` = ``"<source dataset>/<question id>/<condition>"``;
  ``stratum`` = ``"<source dataset>/<condition>"``.
* ``group_id`` = ``"<source dataset>/<question id>"``: the question, so all
  conditions of one question share a split and a bootstrap cluster
  (PREREGISTRATION.md section 4). The source dataset prefix keeps the two id
  namespaces apart.
* ``question`` = the record's question, verbatim (NQ questions are lower-case
  without a question mark; they are not edited).
* ``documents`` = the passages in the order GRAB-RAG shows them, with neutral ids
  ``doc1`` ... ``doc5`` (position only) and text ``"<title>: <text>"`` exactly as
  GRAB-RAG's ``build_context_string`` renders one passage.
* ``gold["document_roles"]``: ``misleading`` - the edited passage is
  ``competitor``, hard negatives are ``other``; ``supportive`` - every passage
  is ``target``. Gold answers, the planted answer, which passage was edited and
  the retriever ids of the hard negatives are also in ``gold`` and never reach an
  arm.

Presentation to the model
-------------------------
:func:`context_messages`: GRAB-RAG's system message (``sys_base`` of
``src/policies.py``, verbatim) and its ``P0`` user layout ("Context:", the
passages, "Question: ..."), with each passage under our usual
``[Document n — source: docN]`` header so that the answer format's ``source_id``
rule can be followed, and our structured-answer instruction (PREREGISTRATION.md
section 6.2) in place of GRAB-RAG's own JSON instruction. GRAB-RAG truncates the
context to 1,500 tokens of the model's tokenizer; we do not truncate.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import random
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from binding_gate.canonical import sha256_json
from binding_gate.contracts import Document, Item, Message
from binding_gate.datasets.deceptive_grounding import format_document_block, read_git_head
from binding_gate.registry import register
from binding_gate.textnorm import normalize_text

DATASET_NAME = "grab_rag"
REPOSITORY_URL = "https://github.com/andre44s/grab-rag"
PINNED_COMMIT = "2006561476b39a6cd1ebc0ab5df1b09af01dbcab"
PINNED_DATA_SHA256 = "d11d48acb7d30cd7033db39b909d8d2d5c0af1c925191ae9e7bc25c7549d14b0"
"""Manifest hash of ``data/*/eval.jsonl`` at :data:`PINNED_COMMIT` (see
:func:`data_manifest_sha256`)."""

SOURCE_DATASETS = ("natural_questions", "hotpotqa")
"""GRAB-RAG's dataset labels; each is ``data/<label>/eval.jsonl``."""

MISLEADING = "misleading"
SUPPORTIVE = "supportive"
CONDITIONS = (MISLEADING, SUPPORTIVE)
GRAB_CONDITION = {MISLEADING: "QC", SUPPORTIVE: "Q100"}

N_PASSAGES = 5
"""``n_passages`` of ``build_condition`` (GRAB-RAG's default, used in all its runs)."""
DEFAULT_SEED = 42
"""``seed`` of GRAB-RAG's ``src/runner.py``; the per-question generator is ``Random(seed + qi)``."""

RECORD_KEYS = frozenset({"id", "question", "answers", "gold_passages", "dataset"})
PASSAGE_KEYS = frozenset({"title", "text"})
NEGATIVE_KEYS = frozenset({"pid", "title", "text", "score"})
EXPORT_KEYS = frozenset({"dataset", "question_id", "condition", "seed", "passages"})

WITH_MISLEADING = "with_misleading"
ALL_QUESTIONS = "all"

MIN_EDIT_SIMILARITY = 0.5
"""An edited passage must have a token-level :class:`difflib.SequenceMatcher`
ratio of at least this with the gold passage it was made from. A swap changes
only the answer span, so real edits score far higher; a lower value means the
export does not come from GRAB-RAG's ``QC`` construction."""

EXPORT_PROCEDURE = """\
Context export for the misleading condition (not part of this repository; run in
GRAB-RAG's own environment at the pinned commit, with its models downloaded):
for each source dataset (nq -> natural_questions, hpqa -> hotpotqa):
  records   = src.runner.load_records(<dataset>)
  retriever = src.retrieval.Retriever.from_records(records)
  generator = src.context_quality.ContradictoryGenerator.from_records(records)
  for qi, rec in enumerate(records):
    cands     = retriever.retrieve(rec["question"], top_k=80)
    negatives = retriever.hard_negatives(rec["question"], rec["gold_passages"], rec["answers"], candidates=cands)
    rng       = random.Random(42 + qi)
    passages  = src.context_quality.build_condition(rec["gold_passages"], negatives, rec["answers"], "QC", rng, generator=generator)
    write one JSON line: {"dataset": <label>, "question_id": rec["id"], "condition": "QC",
                          "seed": 42, "passages": passages}   # passages is null when GRAB-RAG skips the question
These are the calls src/runner.py makes for condition QC (lines 227-252 at the
pinned commit), with the passages written out instead of sent to a model."""

# --- Text copied from GRAB-RAG (MIT, see NOTICE) ------------------------------ #

SYSTEM_PROMPT = (
    "You are a helpful question-answering assistant. "
    "Answer the question using ONLY the provided context. "
    "Always respond with valid JSON."
)
"""``sys_base`` of GRAB-RAG's ``src/policies.py`` at the pinned commit, verbatim."""


class DatasetError(RuntimeError):
    """Raised when the checkout or the context export is missing, altered or malformed."""


# --- GRAB-RAG's answer normalisation (re-implemented; MIT, see NOTICE) -------- #

_ORDINALS = {
    "1st": "first", "2nd": "second", "3rd": "third", "4th": "fourth", "5th": "fifth",
    "6th": "sixth", "7th": "seventh", "8th": "eighth", "9th": "ninth", "10th": "tenth",
    "11th": "eleventh", "12th": "twelfth", "13th": "thirteenth", "14th": "fourteenth",
    "15th": "fifteenth", "16th": "sixteenth", "17th": "seventeenth", "18th": "eighteenth",
    "19th": "nineteenth", "20th": "twentieth",
}  # fmt: skip
_ORDINAL_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in _ORDINALS) + r")\b", re.IGNORECASE)
_COMMA_NUMBER_RE = re.compile(r"(\d),(\d)")
_ARTICLE_RE = re.compile(r"\b(a|an|the)\b")
_PUNCTUATION = str.maketrans("", "", string.punctuation)


def answer_normalize(text: str) -> str:
    """GRAB-RAG's ``src.utils.normalize``: lower case, ``1,000`` -> ``1000``,
    ``1st``..``20th`` spelled out, ASCII punctuation removed, articles removed,
    whitespace collapsed. Used only to compare answers, never for quotes."""
    if not text:
        return ""
    text = text.lower()
    text = _COMMA_NUMBER_RE.sub(r"\1\2", text)
    text = _ORDINAL_RE.sub(lambda m: _ORDINALS[m.group().lower()], text)
    text = text.translate(_PUNCTUATION)
    text = _ARTICLE_RE.sub(" ", text)
    return " ".join(text.split())


def contains_answer(context: str, answers: Sequence[str]) -> bool:
    """GRAB-RAG's ``src.utils.contains_answer``: some normalised answer occurs in the
    normalised context as a whole-word sequence."""
    norm_context = answer_normalize(context)
    for answer in answers:
        norm_answer = answer_normalize(answer or "")
        if norm_answer and re.search(r"\b" + re.escape(norm_answer) + r"\b", norm_context):
            return True
    return False


# --- Presentation ---------------------------------------------------------------- #


def passage_text(passage: dict[str, Any]) -> str:
    """One passage as GRAB-RAG's ``build_context_string`` renders it, without the ``[i]`` number."""
    title = str(passage.get("title", "")).strip()
    text = str(passage.get("text", "")).strip()
    return f"{title}: {text}" if title else text


def context_messages(
    question: str, documents: Sequence[Document], answer_instructions: str
) -> tuple[Message, ...]:
    """GRAB-RAG's system message and ``P0`` layout, our document headers and answer format."""
    user = f"Context:\n{format_document_block(documents)}\n\nQuestion: {question}"
    if answer_instructions:
        user = f"{user}\n\n{answer_instructions}"
    return (Message("system", SYSTEM_PROMPT), Message("user", user))


# --- Integrity checks -------------------------------------------------------------- #


def data_manifest_sha256(root: Path) -> str:
    """SHA-256 over ``<path relative to root> NUL <sha256 of file bytes> LF`` for every
    ``data/*/eval.jsonl`` of the checkout, sorted by path."""
    digest = hashlib.sha256()
    paths = sorted((root / "data").glob("*/eval.jsonl"), key=lambda p: p.relative_to(root).as_posix())
    for path in paths:
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0" + file_hash.encode("ascii") + b"\n")
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_upstream_system_prompt(root: Path) -> str | None:
    """``sys_base`` from the checkout's ``src/policies.py``, parsed but never executed."""
    try:
        tree = ast.parse((root / "src" / "policies.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "sys_base" for target in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# --- Reading the edited passage ------------------------------------------------------- #

_TOKEN_RE = re.compile(r"\w+|[^\w\s]")
_ARTICLES = frozenset({"a", "an"})


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(
        None, [t[0] for t in _tokens(a)], [t[0] for t in _tokens(b)], autojunk=False
    ).ratio()


def match_edited_gold_passage(edited: dict[str, Any], gold_passages: Sequence[dict[str, Any]]) -> tuple[int, float]:
    """``(index, similarity)`` of the gold passage the edited passage was made from.

    Similarity is the token-level ``SequenceMatcher`` ratio of the passage texts;
    the highest wins, ties go to the lower index (GRAB-RAG edits the first gold
    passage it can).
    """
    scores = [_similarity(str(gold.get("text", "")), str(edited.get("text", ""))) for gold in gold_passages]
    best = max(range(len(scores)), key=lambda i: (scores[i], -i))
    return best, scores[best]


def _field_edits(original: str, edited: str) -> list[tuple[str, str]]:
    """``(deleted, inserted)`` spans between two strings, from a token-level diff.

    Tokens are words and single punctuation characters. A leading ``a``/``an``
    flip inside a change is dropped: GRAB-RAG's ``fix_article`` adjusts the
    article in front of the replacement, which is not part of it.
    """
    a, b = _tokens(original), _tokens(edited)
    matcher = difflib.SequenceMatcher(None, [t[0] for t in a], [t[0] for t in b], autojunk=False)
    edits: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if (
            i1 < i2
            and j1 < j2
            and a[i1][0].lower() in _ARTICLES
            and b[j1][0].lower() in _ARTICLES
            and a[i1][0].lower() != b[j1][0].lower()
        ):
            i1, j1 = i1 + 1, j1 + 1
        if i1 == i2 and j1 == j2:
            continue
        deleted = original[a[i1][1] : a[i2 - 1][2]] if i1 < i2 else ""
        inserted = edited[b[j1][1] : b[j2 - 1][2]] if j1 < j2 else ""
        edits.append((deleted, inserted))
    return edits


@dataclass(frozen=True)
class PlantedAnswer:
    """What the token diff between a gold passage and its edit yields."""

    value: str | None
    """The inserted replacement (``None`` when it could not be derived)."""
    replaced: tuple[str, ...]
    """The deleted spans of the gold passage (title and text), in order."""
    title_edited: bool
    note: str | None
    """Why ``value`` is ``None``; ``None`` when it was derived."""


def derive_planted_answer(gold: dict[str, Any], edited: dict[str, Any], answers: Sequence[str]) -> PlantedAnswer:
    """Recover the wrong answer GRAB-RAG planted in ``edited`` (an edit of ``gold``).

    Rule: diff title and text (:func:`_field_edits`). Every change must insert a
    non-empty span, and all inserted spans must be equal after
    :func:`answer_normalize` (GRAB-RAG writes one replacement everywhere); that
    span is the planted answer. It must not itself contain a gold answer.
    Otherwise the planted answer is ``None`` and ``note`` says why.
    """
    title_edits = _field_edits(str(gold.get("title", "")), str(edited.get("title", "")))
    text_edits = _field_edits(str(gold.get("text", "")), str(edited.get("text", "")))
    edits = text_edits + title_edits
    replaced = tuple(deleted for deleted, _ in edits if deleted)
    title_edited = bool(title_edits)

    def fail(note: str) -> PlantedAnswer:
        return PlantedAnswer(None, replaced, title_edited, note)

    if not edits:
        return fail("the edited passage does not differ from its gold passage")
    if any(not answer_normalize(inserted) for _, inserted in edits):
        return fail("a change deletes text without inserting a replacement")
    forms = {answer_normalize(inserted) for _, inserted in edits}
    if len(forms) != 1:
        return fail(f"the changes insert {len(forms)} different spans")
    value = edits[0][1]
    if contains_answer(value, answers):
        return fail("the inserted span contains a gold answer")
    return PlantedAnswer(value, replaced, title_edited, None)


# --- The adapter --------------------------------------------------------------- #


def _string_list(value: Any, option: str, allowed: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value or not all(isinstance(v, str) for v in value):
        raise DatasetError(f"dataset option {option} must be a non-empty list of strings")
    unknown = sorted(set(value) - set(allowed))
    if unknown or len(set(value)) != len(value):
        raise DatasetError(f"dataset option {option} must name distinct values from {list(allowed)}; got {list(value)}")
    return tuple(v for v in allowed if v in value)


@register("dataset", DATASET_NAME)
class GrabRagAdapter:
    """Yields, per question in file order, its ``misleading`` then ``supportive`` item.

    Options (``[dataset.options]``):

    ``root``                 path of the GRAB-RAG checkout (required).
    ``commit``               expected commit; checked when ``root`` is a git checkout.
    ``data_sha256``          expected :func:`data_manifest_sha256`; always checked.
    ``repository``           where the checkout comes from (used by the workflow to clone it).
    ``conditions``           any of ``["misleading", "supportive"]`` (default: both).
    ``source_datasets``      any of ``["natural_questions", "hotpotqa"]`` (default: both).
    ``contexts``             path of the context export (required for ``misleading``).
    ``contexts_sha256``      its expected SHA-256 (required with ``contexts``).
    ``supportive_questions`` ``"with_misleading"`` (default when ``misleading`` is
                             selected): supportive items only for questions that
                             have a misleading context, so the two conditions are
                             paired; or ``"all"`` (the only value without
                             ``misleading``).
    ``seed``                 GRAB-RAG's run seed (default 42); the export must carry the same.
    """

    name = DATASET_NAME

    def __init__(
        self,
        *,
        root: str,
        commit: str = PINNED_COMMIT,
        data_sha256: str = PINNED_DATA_SHA256,
        repository: str = REPOSITORY_URL,
        conditions: Sequence[str] = CONDITIONS,
        source_datasets: Sequence[str] = SOURCE_DATASETS,
        contexts: str | None = None,
        contexts_sha256: str | None = None,
        supportive_questions: str | None = None,
        seed: int = DEFAULT_SEED,
    ):
        for option, value in (("root", root), ("commit", commit), ("data_sha256", data_sha256), ("repository", repository)):
            if not isinstance(value, str) or not value:
                raise DatasetError(f"dataset option {option} must be a non-empty string")
        self.conditions = _string_list(conditions, "conditions", CONDITIONS)
        self.source_datasets = _string_list(source_datasets, "source_datasets", SOURCE_DATASETS)
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise DatasetError("dataset option seed must be an integer")
        wants_misleading = MISLEADING in self.conditions
        if supportive_questions is None:
            supportive_questions = WITH_MISLEADING if wants_misleading else ALL_QUESTIONS
        if supportive_questions not in (WITH_MISLEADING, ALL_QUESTIONS):
            raise DatasetError(f"dataset option supportive_questions must be {WITH_MISLEADING!r} or {ALL_QUESTIONS!r}")
        if supportive_questions == WITH_MISLEADING and not wants_misleading:
            raise DatasetError("supportive_questions = 'with_misleading' needs the misleading condition")
        if wants_misleading and (not isinstance(contexts, str) or not contexts):
            raise DatasetError(
                "the misleading condition needs dataset option contexts: the path of the context "
                "export, which GRAB-RAG does not ship (see binding_gate.datasets.grab_rag.EXPORT_PROCEDURE)"
            )
        if contexts is not None and not wants_misleading:
            raise DatasetError("dataset option contexts is only used by the misleading condition")
        if contexts is not None and (not isinstance(contexts_sha256, str) or not contexts_sha256):
            raise DatasetError("dataset option contexts_sha256 is required with contexts")
        self.root = Path(root)
        self.commit = commit
        self.data_sha256 = data_sha256
        self.repository = repository
        self.contexts = None if contexts is None else Path(contexts)
        self.contexts_sha256 = contexts_sha256
        self.supportive_questions = supportive_questions
        self.seed = seed
        self._verified_fingerprint: str | None = None
        self.coverage: dict[str, dict[str, int]] = {}
        """Filled by :meth:`iter_items`: per source dataset, the number of questions,
        of misleading items, of questions GRAB-RAG skipped for ``QC``, of misleading
        items without a derivable planted answer, and of supportive items."""

    # -- integrity ----------------------------------------------------------------- #

    def verify(self) -> str:
        """Check the checkout and the export against their pins; return :meth:`fingerprint`."""
        if self._verified_fingerprint is not None:
            return self._verified_fingerprint
        missing = [label for label in SOURCE_DATASETS if not (self.root / "data" / label / "eval.jsonl").is_file()]
        if missing:
            raise DatasetError(
                f"{self.root} has no data/{missing[0]}/eval.jsonl. Clone {self.repository} at commit "
                f"{self.commit} into {self.root} (see docs/RUNNING_BASELINE.md)."
            )
        if (self.root / ".git").exists():
            head = read_git_head(self.root)
            if head != self.commit:
                raise DatasetError(
                    f"{self.root} is checked out at {head or 'an unreadable commit'}, "
                    f"but the run config pins {self.commit}"
                )
        actual = data_manifest_sha256(self.root)
        if actual != self.data_sha256:
            raise DatasetError(
                f"the eval files under {self.root / 'data'} do not match the pinned data hash "
                f"(expected {self.data_sha256}, found {actual})"
            )
        upstream_prompt = read_upstream_system_prompt(self.root)
        if upstream_prompt is not None and upstream_prompt != SYSTEM_PROMPT:
            raise DatasetError(
                "sys_base in the checkout's src/policies.py differs from the copy in binding_gate.datasets.grab_rag"
            )
        fingerprint = actual
        if self.contexts is not None:
            if not self.contexts.is_file():
                raise DatasetError(
                    f"the context export {self.contexts} does not exist. GRAB-RAG does not ship the "
                    f"misleading contexts; they must be exported once with GRAB-RAG's own code:\n{EXPORT_PROCEDURE}"
                )
            contexts_actual = file_sha256(self.contexts)
            if not _HEX64.match(self.contexts_sha256 or ""):
                raise DatasetError(
                    f"contexts_sha256 is not pinned (found {self.contexts_sha256!r}). The file "
                    f"{self.contexts} has SHA-256 {contexts_actual}; pin it in the run config after "
                    "checking where the file came from."
                )
            if contexts_actual != self.contexts_sha256:
                raise DatasetError(
                    f"the context export {self.contexts} does not match the pinned hash "
                    f"(expected {self.contexts_sha256}, found {contexts_actual})"
                )
            fingerprint = sha256_json({"data_sha256": actual, "contexts_sha256": contexts_actual})
        self._verified_fingerprint = fingerprint
        return fingerprint

    def fingerprint(self) -> str:
        """The data manifest hash; with a context export, the hash of both pins together."""
        return self.verify()

    # -- reading --------------------------------------------------------------------- #

    def records(self, label: str) -> list[dict[str, Any]]:
        """The validated records of ``data/<label>/eval.jsonl`` in file order."""
        path = self.root / "data" / label / "eval.jsonl"
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            where = f"{label}/eval.jsonl:{line_number}"
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise DatasetError(f"{where}: not JSON: {exc}") from exc
            validate_record(record, label, where)
            if record["id"] in seen:
                raise DatasetError(f"{where}: duplicate id {record['id']!r}")
            seen.add(record["id"])
            records.append(record)
        if not records:
            raise DatasetError(f"{path} holds no records")
        return records

    def exported_contexts(self) -> dict[tuple[str, str], list[dict[str, Any]] | None]:
        """``(source dataset, question id) -> passages`` (``None`` = skipped by GRAB-RAG)."""
        assert self.contexts is not None
        exported: dict[tuple[str, str], list[dict[str, Any]] | None] = {}
        for line_number, line in enumerate(self.contexts.read_text(encoding="utf-8").splitlines(), start=1):
            where = f"{self.contexts.name}:{line_number}"
            try:
                entry = json.loads(line)
            except ValueError as exc:
                raise DatasetError(f"{where}: not JSON: {exc}") from exc
            if not isinstance(entry, dict) or set(entry) != EXPORT_KEYS:
                raise DatasetError(f"{where}: an export line has exactly the keys {sorted(EXPORT_KEYS)}")
            if entry["dataset"] not in SOURCE_DATASETS or not isinstance(entry["question_id"], str):
                raise DatasetError(f"{where}: unknown dataset or non-string question_id")
            if entry["condition"] != GRAB_CONDITION[MISLEADING]:
                raise DatasetError(f"{where}: condition must be 'QC', found {entry['condition']!r}")
            if entry["seed"] != self.seed or isinstance(entry["seed"], bool):
                raise DatasetError(f"{where}: exported with seed {entry['seed']!r}, the run config says {self.seed}")
            key = (entry["dataset"], entry["question_id"])
            if key in exported:
                raise DatasetError(f"{where}: duplicate export line for {key}")
            passages = entry["passages"]
            if passages is not None and not isinstance(passages, list):
                raise DatasetError(f"{where}: passages must be a list or null")
            exported[key] = passages
        return exported

    def iter_items(self) -> Iterator[Item]:
        self.verify()
        exported = self.exported_contexts() if MISLEADING in self.conditions else {}
        if exported:
            known = {(label, r["id"]) for label in SOURCE_DATASETS for r in self.records(label)}
            strangers = sorted(set(exported) - known)
            if strangers:
                raise DatasetError(f"the context export names questions that are not in eval.jsonl: {strangers[:3]}")
        self.coverage = {}
        for label in self.source_datasets:
            counts = {"questions": 0, "misleading": 0, "misleading_skipped": 0, "planted_underivable": 0, "supportive": 0}
            self.coverage[label] = counts
            for qi, record in enumerate(self.records(label)):
                counts["questions"] += 1
                has_misleading = False
                if MISLEADING in self.conditions:
                    key = (label, record["id"])
                    if key not in exported:
                        raise DatasetError(
                            f"the context export has no line for {label}/{record['id']}; it must cover every "
                            "question of the selected source datasets (passages null when skipped)"
                        )
                    passages = exported[key]
                    if passages is None:
                        counts["misleading_skipped"] += 1
                    else:
                        item = misleading_item(label, record, passages, self.seed)
                        counts["misleading"] += 1
                        counts["planted_underivable"] += item.gold["planted_answer"] is None
                        has_misleading = True
                        yield item
                if SUPPORTIVE in self.conditions and (self.supportive_questions == ALL_QUESTIONS or has_misleading):
                    counts["supportive"] += 1
                    yield supportive_item(label, record, qi, self.seed)


def validate_record(record: Any, label: str, where: str = "record") -> None:
    """Raise :class:`DatasetError` unless ``record`` has the shape of a GRAB-RAG eval record."""
    if not isinstance(record, dict) or set(record) != RECORD_KEYS:
        found = sorted(record) if isinstance(record, dict) else type(record).__name__
        raise DatasetError(f"{where}: expected exactly the keys {sorted(RECORD_KEYS)}, found {found}")
    if record["dataset"] != label:
        raise DatasetError(f"{where}: dataset is {record['dataset']!r}, expected {label!r}")
    for key in ("id", "question"):
        if not isinstance(record[key], str) or not record[key].strip():
            raise DatasetError(f"{where}: {key} must be a non-empty string")
    if "/" in record["id"]:
        raise DatasetError(f"{where}: id must not contain '/'")
    answers = record["answers"]
    if not isinstance(answers, list) or not answers or not all(isinstance(a, str) and a.strip() for a in answers):
        raise DatasetError(f"{where}: answers must be a non-empty list of non-empty strings")
    gold = record["gold_passages"]
    if not isinstance(gold, list) or not gold:
        raise DatasetError(f"{where}: gold_passages must be a non-empty list")
    for passage in gold:
        _check_passage(passage, PASSAGE_KEYS, where)


def _check_passage(passage: Any, keys: frozenset[str], where: str) -> None:
    if not isinstance(passage, dict) or set(passage) != keys:
        found = sorted(passage) if isinstance(passage, dict) else type(passage).__name__
        raise DatasetError(f"{where}: a passage must have exactly the keys {sorted(keys)}, found {found}")
    if not isinstance(passage["title"], str) or not isinstance(passage["text"], str) or not passage["text"].strip():
        raise DatasetError(f"{where}: passage title must be a string and text a non-empty string")


def group_id_of(label: str, question_id: str) -> str:
    """The split/bootstrap unit: one question of one source dataset."""
    return f"{label}/{question_id}"


def _documents(passages: Sequence[dict[str, Any]]) -> tuple[Document, ...]:
    return tuple(Document.create(f"doc{n}", passage_text(p)) for n, p in enumerate(passages, start=1))


def _base_gold(label: str, record: dict[str, Any], condition: str, seed: int) -> dict[str, Any]:
    return {
        "condition": condition,
        "grab_condition": GRAB_CONDITION[condition],
        "source_dataset": label,
        "question_id": record["id"],
        "answers": list(record["answers"]),
        "n_gold_passages": len(record["gold_passages"]),
        "seed": seed,
    }


def _item(label: str, record: dict[str, Any], condition: str, documents: tuple[Document, ...], gold: dict) -> Item:
    return Item(
        id=f"{label}/{record['id']}/{condition}",
        group_id=group_id_of(label, record["id"]),
        dataset=DATASET_NAME,
        question=record["question"],
        documents=documents,
        gold=gold,
        stratum=f"{label}/{condition}",
    )


def supportive_passage_order(n_gold: int, qi: int, seed: int = DEFAULT_SEED) -> list[int]:
    """Gold-passage index shown in each of the 5 slots of ``Q100``.

    ``build_condition`` fills slot ``i`` with gold passage ``i % n_gold`` and
    shuffles the slots with ``random.Random(seed + qi)``; the permutation of
    ``random.shuffle`` depends only on the list length and the generator, so
    shuffling the indices gives the same order as shuffling the passages.
    """
    order = [i % n_gold for i in range(N_PASSAGES)]
    random.Random(seed + qi).shuffle(order)
    return order


def supportive_item(label: str, record: dict[str, Any], qi: int, seed: int = DEFAULT_SEED) -> Item:
    """GRAB-RAG's ``Q100`` (supportive) context of one record; ``qi`` = its index in the file."""
    validate_record(record, label)
    order = supportive_passage_order(len(record["gold_passages"]), qi, seed)
    documents = _documents([record["gold_passages"][i] for i in order])
    gold = {
        **_base_gold(label, record, SUPPORTIVE, seed),
        "document_roles": {d.id: "target" for d in documents},
        "gold_passage_index": {d.id: i for d, i in zip(documents, order)},
        "planted_answer": None,
    }
    return _item(label, record, SUPPORTIVE, documents, gold)


def misleading_item(label: str, record: dict[str, Any], passages: Any, seed: int = DEFAULT_SEED) -> Item:
    """GRAB-RAG's ``QC`` (misleading) context of one record, as exported (see the module docstring)."""
    validate_record(record, label)
    where = f"context export {label}/{record['id']}"
    if not isinstance(passages, list) or len(passages) != N_PASSAGES:
        raise DatasetError(f"{where}: expected a list of {N_PASSAGES} passages")
    edited_positions = [i for i, p in enumerate(passages) if isinstance(p, dict) and "pid" not in p]
    if len(edited_positions) != 1:
        raise DatasetError(f"{where}: exactly one passage must lack 'pid' (the edited one), found {len(edited_positions)}")
    position = edited_positions[0]
    answers = record["answers"]
    gold_texts = {answer_normalize(p["text"]) for p in record["gold_passages"]}
    for i, passage in enumerate(passages):
        _check_passage(passage, PASSAGE_KEYS if i == position else NEGATIVE_KEYS, where)
        if contains_answer(f"{passage['title']} {passage['text']}", answers):
            raise DatasetError(f"{where}: passage {i + 1} contains a gold answer, which GRAB-RAG's QC excludes")
        if i != position:
            if not isinstance(passage["pid"], str) or isinstance(passage["score"], bool) or not isinstance(passage["score"], (int, float)):
                raise DatasetError(f"{where}: hard negative {i + 1} needs a string pid and a numeric score")
            if answer_normalize(passage["text"]) in gold_texts:
                raise DatasetError(f"{where}: hard negative {i + 1} is a gold passage")
    edited = passages[position]
    gold_index, similarity = match_edited_gold_passage(edited, record["gold_passages"])
    if similarity < MIN_EDIT_SIMILARITY:
        raise DatasetError(
            f"{where}: the edited passage is not an edit of any gold passage (best similarity {similarity:.2f})"
        )
    planted = derive_planted_answer(record["gold_passages"][gold_index], edited, answers)
    documents = _documents(passages)
    misleading_id = documents[position].id
    gold = {
        **_base_gold(label, record, MISLEADING, seed),
        "document_roles": {d.id: "competitor" if d.id == misleading_id else "other" for d in documents},
        "misleading_document_id": misleading_id,
        "edited_gold_passage_index": gold_index,
        "edit_similarity": round(similarity, 4),
        "planted_answer": planted.value,
        "planted_answer_note": planted.note,
        "replaced_text": list(planted.replaced),
        "title_edited": planted.title_edited,
        "hard_negative_pids": {d.id: p["pid"] for d, p in zip(documents, passages) if d.id != misleading_id},
    }
    return _item(label, record, MISLEADING, documents, gold)
