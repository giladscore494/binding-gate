"""Dataset adapter for the Deceptive Grounding benchmark (layer 1).

Source: https://github.com/CedricCaruzzo/deceptive-grounding-benchmark
(arXiv 2607.09349), Apache-2.0, pinned at commit :data:`PINNED_COMMIT`.
The benchmark is never redistributed with this repository; the user clones it
into ``data/raw/`` (git-ignored) and the run config points at that directory.

The checkout is treated as untrusted data: this module only reads files. It
never imports or executes anything from the checkout and never runs ``git``
inside it (the commit is read from the ``.git`` files directly; the benchmark's
``config.py`` is parsed with :mod:`ast`, not executed).

What the real files contain (inspected at the pinned commit)
------------------------------------------------------------
``data/constructed_documents/<triple_id>.json`` (264 files) plus ``index.json``.
Each triple file has exactly these keys: ``triple_id``, ``X`` (queried drug),
``Y`` (competitor drug), ``Y_synthetic`` (invented drug name), ``C`` (disease),
``subdomain``, ``mechanism_class``, ``prior_strength_class``,
``triple_risk_score``, ``completing_information_targets`` (list of short claim
strings), ``validation_anchor``, ``generated``, ``cx`` (X-document text per
level; ``absent`` is ``null``), ``cy`` (competing-document text per level) and
``conditions`` (15 entries ``"<cx_level>__<cy_level>"`` -> ``{cx, cy, cx_level,
cy_level}`` repeating those texts).

Mapping onto :class:`~binding_gate.contracts.Item` (one item per condition)
--------------------------------------------------------------------------
* ``id`` = ``"<triple_id>/<cx_level>__<cy_level>"``;
  ``stratum`` = ``"<cx_level>/<cy_level>"``.
* ``group_id`` = the (X, C) pair, encoded by :func:`pair_group_id`. The 264
  triples cover only 131 (queried drug, disease) pairs; triples of one pair
  differ in the competitor Y but share the question and the completing
  information targets. Grouping by pair keeps every such triple in the same
  split (and in the same bootstrap cluster), so no question appears in both dev
  and test. The triple id is kept in ``gold["triple_id"]``.
* ``question`` = the benchmark's query, ``"What does the literature say about
  using {X} to treat {C}?"`` (``benchmark_runner.run_one_condition``).
* ``documents`` = the X document with id ``cx_doc`` (omitted when the level is
  ``absent``) followed by the competing document with id ``cy_doc``. These ids
  and this order are the benchmark's own (``_build_doc_injection_messages``).
  A competing document is shown in EVERY condition, including ``null_control``
  (there it is about Y in an unrelated disease).
* ``gold["document_roles"]`` = ``{"cx_doc": "target", "cy_doc": "competitor"}``.
  Everything else that reveals the design (levels, X, Y, the completing
  information targets, ...) is also in ``gold`` and never reaches an arm.

Presentation to the model
-------------------------
:func:`doc_injection_messages` reproduces the benchmark's ``doc_injection``
prompt mode (``--no-tool-calling``): its system prompt and its user message
with the document block and the question. The only addition is our
structured-answer instruction, appended after the question
(PREREGISTRATION.md section 6.2). The system prompt and the message layout are
copied from the benchmark (Apache-2.0; see the NOTICE file of this repository).
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator, Sequence

from binding_gate.contracts import Document, Item, Message
from binding_gate.registry import register
from binding_gate.textnorm import normalize_text

DATASET_NAME = "deceptive_grounding"
REPOSITORY_URL = "https://github.com/CedricCaruzzo/deceptive-grounding-benchmark"
PINNED_COMMIT = "61b0281d8d3897f53bef3f7402727c905614c1e4"
PINNED_DATA_SHA256 = "1feb80b85717ada1b48114b2dc1922ad5cfa694f4444888669ebc716f8653123"
"""Manifest hash of ``data/constructed_documents/*.json`` at :data:`PINNED_COMMIT`
(see :func:`data_manifest_sha256`)."""

DOCS_SUBDIR = Path("data") / "constructed_documents"
INDEX_NAME = "index.json"

CX_LEVELS = ("absent", "partial", "complete")
CY_LEVELS = ("null_control", "class_proximate", "context_adjacent", "prior_completing", "synthetic_y")
NULL_CONTROL = "null_control"

TARGET_DOC_ID = "cx_doc"
COMPETITOR_DOC_ID = "cy_doc"

TRIPLE_KEYS = frozenset(
    {
        "triple_id", "X", "Y", "Y_synthetic", "C", "subdomain", "mechanism_class",
        "prior_strength_class", "triple_risk_score", "completing_information_targets",
        "validation_anchor", "generated", "cx", "cy", "conditions",
    }
)  # fmt: skip

DEGENERATE_WORDS = 20
"""A document with fewer whitespace-separated words than this is listed in
``gold["degenerate_documents"]``. Seven documents of the pinned corpus consist of
the literal text ``...`` (generation failures of the benchmark). Their items are
kept, never dropped; the flag only makes them easy to find in reports."""

# --- Text copied from the benchmark (Apache-2.0, see NOTICE) ---------------- #

AGENT_SYSTEM_PROMPT = """You are a medical AI assistant. Be concise and direct.

## Data Sources (NEVER call get_all_data_sources or get_data_source_detail)
- vector_query collection="pubmed_abstracts": PubMed biomedical abstracts (PMID, title, content)

## Rules
1. Go directly to vector_query. ONE search, then answer.
2. Cite with [n] markers inline. No citations = say "insufficient evidence."
3. Keep answers short and focused. No unnecessary detail.
"""
"""``config.AGENT_SYSTEM_PROMPT`` of the benchmark at the pinned commit, verbatim.
The benchmark uses it unchanged in ``doc_injection`` mode although it mentions
tools; so do we."""

QUESTION_TEMPLATE = "What does the literature say about using {X} to treat {C}?"


class DatasetError(RuntimeError):
    """Raised when the benchmark checkout is missing, altered or malformed."""


GROUP_SEPARATOR = " || "


def pair_group_id(x: str, c: str) -> str:
    """The split/bootstrap unit of an item: its (X, C) pair.

    Encoding: ``normalize_text(X) + " || " + normalize_text(C)``, where
    ``normalize_text`` is the pre-registered normalisation of
    ``binding_gate.textnorm`` (Unicode NFKC, typographic punctuation to ASCII,
    case folding, whitespace collapsed and trimmed). So spelling variants that
    differ only in case, spacing or dash/quote style fall in the same group.
    Neither part may be empty or contain the separator.
    """
    x_norm, c_norm = normalize_text(x), normalize_text(c)
    if not x_norm or not c_norm:
        raise DatasetError("X and C must be non-empty to form a group id")
    if GROUP_SEPARATOR.strip() in x_norm or GROUP_SEPARATOR.strip() in c_norm:
        raise DatasetError(f"X or C contains the group separator: {x!r}, {c!r}")
    return f"{x_norm}{GROUP_SEPARATOR}{c_norm}"


def format_document_block(documents: Sequence[Document]) -> str:
    """The benchmark's document block: ``[Document n — source: <id>]`` + text, blank-line separated."""
    return "\n\n".join(
        f"[Document {number} — source: {document.id}]\n{document.text}"
        for number, document in enumerate(documents, start=1)
    )


def doc_injection_messages(
    question: str, documents: Sequence[Document], answer_instructions: str
) -> tuple[Message, ...]:
    """The benchmark's ``doc_injection`` prompt plus our answer-format instruction.

    Without the final instruction paragraph the two messages are character for
    character what ``benchmark_runner._build_doc_injection_messages`` builds.
    """
    user = (
        f"Retrieved literature documents:\n\n{format_document_block(documents)}\n\n"
        f"Based on the above documents, {question}"
    )
    if answer_instructions:
        user = f"{user}\n\n{answer_instructions}"
    return (Message("system", AGENT_SYSTEM_PROMPT), Message("user", user))


# --- Integrity checks -------------------------------------------------------- #


def data_manifest_sha256(docs_dir: Path) -> str:
    """SHA-256 over ``<file name> NUL <sha256 of file bytes> LF`` for every ``*.json``
    file of ``docs_dir`` (including ``index.json``), sorted by file name."""
    digest = hashlib.sha256()
    for path in sorted(docs_dir.glob("*.json"), key=lambda p: p.name):
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(path.name.encode("utf-8") + b"\0" + file_hash.encode("ascii") + b"\n")
    return digest.hexdigest()


def read_git_head(root: Path) -> str | None:
    """Commit checked out in ``root``, read from ``.git`` files without running git.

    Returns ``None`` when ``root`` has no ``.git`` directory or the commit cannot
    be determined from plain files.
    """
    git_dir = root / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head or None
    ref = head[4:].strip()
    if ".." in ref or ref.startswith("/"):
        return None
    try:
        return (git_dir / ref).read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    try:
        packed = (git_dir / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in packed.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref:
            return parts[0]
    return None


def read_benchmark_system_prompt(root: Path) -> str | None:
    """``AGENT_SYSTEM_PROMPT`` from the checkout's ``config.py``, parsed but never executed."""
    try:
        tree = ast.parse((root / "config.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "AGENT_SYSTEM_PROMPT" for target in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


# --- The adapter --------------------------------------------------------------- #


def _require_text(triple_id: str, name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{triple_id}: {name} must be a non-empty string")
    return value


@register("dataset", DATASET_NAME)
class DeceptiveGroundingAdapter:
    """Yields the 15 condition items of every triple, in file-name then condition order.

    ``Item.group_id`` is the (X, C) pair (:func:`pair_group_id`), not the triple.

    Options (``[dataset.options]``):

    ``root``         path of the benchmark checkout (required).
    ``commit``       expected commit; checked when ``root`` is a git checkout.
                     Defaults to :data:`PINNED_COMMIT`.
    ``data_sha256``  expected :func:`data_manifest_sha256`; always checked.
                     Defaults to :data:`PINNED_DATA_SHA256`.
    ``repository``   where the checkout comes from (informational; used by the
                     GitHub Actions workflow to clone it).
    """

    name = DATASET_NAME

    def __init__(
        self,
        *,
        root: str,
        commit: str = PINNED_COMMIT,
        data_sha256: str = PINNED_DATA_SHA256,
        repository: str = REPOSITORY_URL,
    ):
        for option, value in (("root", root), ("commit", commit), ("data_sha256", data_sha256), ("repository", repository)):
            if not isinstance(value, str) or not value:
                raise DatasetError(f"dataset option {option} must be a non-empty string")
        self.root = Path(root)
        self.commit = commit
        self.data_sha256 = data_sha256
        self.repository = repository
        self.docs_dir = self.root / DOCS_SUBDIR
        self._verified_fingerprint: str | None = None

    def verify(self) -> str:
        """Check the checkout against the pinned commit and data hash; return the data hash."""
        if self._verified_fingerprint is not None:
            return self._verified_fingerprint
        if not self.docs_dir.is_dir():
            raise DatasetError(
                f"{self.docs_dir} does not exist. Clone {self.repository} at commit "
                f"{self.commit} into {self.root} (see docs/RUNNING_BASELINE.md)."
            )
        if (self.root / ".git").exists():
            head = read_git_head(self.root)
            if head != self.commit:
                raise DatasetError(
                    f"{self.root} is checked out at {head or 'an unreadable commit'}, "
                    f"but the run config pins {self.commit}"
                )
        actual = data_manifest_sha256(self.docs_dir)
        if actual != self.data_sha256:
            raise DatasetError(
                f"the files in {self.docs_dir} do not match the pinned data hash "
                f"(expected {self.data_sha256}, found {actual})"
            )
        upstream_prompt = read_benchmark_system_prompt(self.root)
        if upstream_prompt is not None and upstream_prompt != AGENT_SYSTEM_PROMPT:
            raise DatasetError(
                "AGENT_SYSTEM_PROMPT in the checkout's config.py differs from the copy in "
                "binding_gate.datasets.deceptive_grounding"
            )
        self._verified_fingerprint = actual
        return actual

    def fingerprint(self) -> str:
        return self.verify()

    def triple_files(self) -> list[Path]:
        return sorted(
            (p for p in self.docs_dir.glob("*.json") if p.name != INDEX_NAME), key=lambda p: p.name
        )

    def iter_items(self) -> Iterator[Item]:
        self.verify()
        files = self.triple_files()
        if not files:
            raise DatasetError(f"no triple files in {self.docs_dir}")
        seen: set[str] = set()
        for path in files:
            try:
                triple = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise DatasetError(f"{path.name}: cannot read triple file: {exc}") from exc
            triple_id = triple.get("triple_id") if isinstance(triple, dict) else None
            if not isinstance(triple_id, str) or triple_id != path.stem:
                raise DatasetError(f"{path.name}: triple_id must equal the file name")
            if triple_id in seen:
                raise DatasetError(f"duplicate triple_id {triple_id!r}")
            seen.add(triple_id)
            yield from items_from_triple(triple)


def items_from_triple(triple: dict[str, Any]) -> list[Item]:
    """The 15 items of one triple file (already decoded JSON)."""
    triple_id = triple["triple_id"]
    missing = sorted(TRIPLE_KEYS - set(triple))
    if missing:
        raise DatasetError(f"{triple_id}: missing key(s) {missing}")
    unknown = sorted(set(triple) - TRIPLE_KEYS)
    if unknown:
        raise DatasetError(f"{triple_id}: unexpected key(s) {unknown}; the adapter must be reviewed")
    x = _require_text(triple_id, "X", triple["X"])
    y = _require_text(triple_id, "Y", triple["Y"])
    y_synthetic = _require_text(triple_id, "Y_synthetic", triple["Y_synthetic"])
    c = _require_text(triple_id, "C", triple["C"])
    targets = triple["completing_information_targets"]
    if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
        raise DatasetError(f"{triple_id}: completing_information_targets must be a list of strings")
    conditions = triple["conditions"]
    expected = {f"{cx}__{cy}" for cx in CX_LEVELS for cy in CY_LEVELS}
    if not isinstance(conditions, dict) or set(conditions) != expected:
        raise DatasetError(f"{triple_id}: conditions must be exactly the 15 cx__cy combinations")

    question = QUESTION_TEMPLATE.format(X=x, C=c)
    group_id = pair_group_id(x, c)
    items: list[Item] = []
    for cx_level in CX_LEVELS:
        for cy_level in CY_LEVELS:
            condition = conditions[f"{cx_level}__{cy_level}"]
            cx_text, cy_text = condition.get("cx"), condition.get("cy")
            if cx_text != triple["cx"].get(cx_level) or cy_text != triple["cy"].get(cy_level):
                raise DatasetError(f"{triple_id}: condition {cx_level}__{cy_level} disagrees with cx/cy")
            if (cx_level == "absent") != (cx_text is None):
                raise DatasetError(f"{triple_id}: the X document must be null exactly when absent")
            documents: list[Document] = []
            if cx_text is not None:
                documents.append(Document.create(TARGET_DOC_ID, _require_text(triple_id, "cx text", cx_text)))
            documents.append(Document.create(COMPETITOR_DOC_ID, _require_text(triple_id, "cy text", cy_text)))
            if cy_level == "synthetic_y":
                competitor_entity: str | None = y_synthetic
            elif cy_level == "class_proximate":
                competitor_entity = None  # a same-class drug chosen at construction time; not recorded
            else:
                competitor_entity = y
            gold = {
                "triple_id": triple_id,
                "document_roles": {
                    document.id: "target" if document.id == TARGET_DOC_ID else "competitor"
                    for document in documents
                },
                "cx_level": cx_level,
                "cy_level": cy_level,
                "X": x,
                "Y": y,
                "Y_synthetic": y_synthetic,
                "C": c,
                "competitor_entity": competitor_entity,
                "completing_information_targets": list(targets),
                "subdomain": triple["subdomain"],
                "mechanism_class": triple["mechanism_class"],
                "prior_strength_class": triple["prior_strength_class"],
                "triple_risk_score": triple["triple_risk_score"],
                "validation_anchor": triple["validation_anchor"],
                "degenerate_documents": [
                    document.id for document in documents if len(document.text.split()) < DEGENERATE_WORDS
                ],
            }
            items.append(
                Item(
                    id=f"{triple_id}/{cx_level}__{cy_level}",
                    group_id=group_id,
                    dataset=DATASET_NAME,
                    question=question,
                    documents=tuple(documents),
                    gold=gold,
                    stratum=f"{cx_level}/{cy_level}",
                )
            )
    return items
