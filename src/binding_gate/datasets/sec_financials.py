"""Dataset adapter ``sec_financials``: layer 2, financial facts from US public-company annual reports (PR 17).

The dataset is GENERATED from SEC EDGAR by ``binding_gate.datasets.sec_build``
(``python -m binding_gate.cli dataset-prepare --config <config>``; free; needs
the repository secret ``SEC_CONTACT_EMAIL`` for the User-Agent that SEC's
fair-access rules require). Generation is deterministic and pre-registered
(PREREGISTRATION.md section 3, layer 2; rules in the ``sec_build`` docstring).

This adapter only READS the generated directory (``root``) and verifies it:

* ``manifest.json``: SHA-256 of the canonical JSON of its ``content`` must equal
  ``content_sha256`` and the pin ``data_sha256`` of the run config (the
  orchestrator commits the hash printed by the first ``dataset-prepare`` in
  GitHub Actions). While the pin is ``"UNPINNED"`` every load refuses.
* ``items.jsonl`` and ``selection.json``: their hashes must equal
  ``content.items_sha256`` and ``content.selection_sha256``.

Items (one per company and condition; the unit of the split and of the
bootstrap is the company, ``group_id = "sec/<10-digit CIK>"``):

* ``question``: "What was <registrant>'s total revenue, in US dollars, for the
  fiscal year ended <date>, as reported in its annual report on Form 10-K?"
  (template per metric, ``sec_build.METRICS``);
* ``documents``: ``doc1`` (and ``doc2``): the rendered financial statements of
  the target's 10-K and/or of a same-industry peer's 10-K;
* ``stratum``: ``present/none``, ``present/peer`` or ``absent/peer`` (target
  document shown / competitor document shown), so the analysis reports the two
  factors like layer 1;
* ``gold`` (never shown to an arm): ``document_roles`` (``target`` /
  ``competitor``), the gold ``value`` (US dollars, as reported in the filing),
  ``unit``, ``concept``, ``period``, ``accession``, ``display`` (row, column,
  cell text and stated scale), ``target_alternatives`` (the same concept for the
  other years of the same filing), ``competitor`` (the peer company and its
  values of the same metric), ``company`` and ``entity_aliases`` (names and
  tickers).

Presentation ``sec_filings`` (``answering.PRESENTATIONS``): a system message
written for this study (it names no company and gives no hint about peer
documents), the documents as ``[Document n — source: docN]`` blocks, the
question, and the structured-answer instruction of PREREGISTRATION.md 6.2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterator, Sequence

from binding_gate.canonical import sha256_json
from binding_gate.contracts import Document, Item, Message
from binding_gate.datasets import sec_build
from binding_gate.registry import register

DATASET_NAME = sec_build.DATASET_NAME
REPOSITORY_URL = "https://www.sec.gov/edgar"
UNPINNED = "UNPINNED"
DEFAULT_ROOT = "data/raw/sec-financials"
DEFAULT_BUILD_CONFIG = "configs/datasets/sec_financials_v1.toml"


class DatasetError(RuntimeError):
    """Raised when the generated dataset is missing, unpinned, altered or malformed."""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@register("dataset", DATASET_NAME) if __name__ != "__main__" else (lambda component: component)
class SecFinancialsAdapter:
    """Options (``[dataset.options]``):

    ``root``          the generated dataset directory (default ``data/raw/sec-financials``; git-ignored)
    ``data_sha256``   the pinned manifest content hash, or ``"UNPINNED"`` before the first generation
    ``build_config``  the committed builder config (fair-access settings, committed selection file)
    ``repository``    informational (``cli dataset-pin``)
    """

    name = DATASET_NAME
    pin_method = "prepare"
    """Tells the workflow to run ``cli dataset-prepare`` (free; secret SEC_CONTACT_EMAIL) instead of a git clone."""

    def __init__(
        self,
        *,
        root: str = DEFAULT_ROOT,
        data_sha256: str = UNPINNED,
        build_config: str = DEFAULT_BUILD_CONFIG,
        repository: str = REPOSITORY_URL,
    ):
        for option, value in (("root", root), ("data_sha256", data_sha256), ("build_config", build_config), ("repository", repository)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"dataset option {option} must be a non-empty string")
        if data_sha256 != UNPINNED and (len(data_sha256) != 64 or any(c not in "0123456789abcdef" for c in data_sha256)):
            raise ValueError('data_sha256 must be a 64-digit lower-case hex SHA-256 or "UNPINNED"')
        self.root = Path(root)
        self.data_sha256 = data_sha256
        self.build_config = build_config
        self.repository = repository
        self.commit = f"build-v{sec_build.BUILD_VERSION}:{data_sha256}"
        self._manifest: dict[str, Any] | None = None

    def prepare(self, **kwargs: Any) -> dict[str, Any]:
        """Generate the dataset (``cli dataset-prepare``); prints/returns the manifest with ``content_sha256``."""
        manifest = sec_build.prepare(self.root, build_config=self.build_config, **kwargs)
        pinned = manifest["content_sha256"]
        return {
            "content_sha256": pinned,
            "pinned_in_config": self.data_sha256,
            "matches_pin": pinned == self.data_sha256,
            "n_items": manifest["content"]["n_items"],
            "n_companies": manifest["content"]["n_companies"],
            "items_by_split": manifest["content"]["items_by_split"],
            "items_by_stratum": manifest["content"]["items_by_stratum"],
            "items_by_metric": manifest["content"]["items_by_metric"],
            "excluded": len(manifest["content"]["excluded"]),
            "retrieval": manifest["retrieval"],
            "next_step": (
                "pin this content_sha256 as data_sha256 in every sec_financials config and commit "
                f"{self.root.as_posix()}/{sec_build.SELECTION_NAME} as the selection_file of the builder config"
                if pinned != self.data_sha256 else "the generated dataset matches the pinned hash"
            ),
        }

    def verify(self) -> dict[str, Any]:
        if self._manifest is not None:
            return self._manifest
        if self.data_sha256 == UNPINNED:
            raise DatasetError(
                "the sec_financials dataset is not pinned yet (data_sha256 = \"UNPINNED\"): generate it once with "
                "`python -m binding_gate.cli dataset-prepare --config <config>` (free; secret SEC_CONTACT_EMAIL) and "
                "commit the printed content_sha256 as data_sha256 in the sec configs (docs/RUNNING.md)"
            )
        path = self.root / sec_build.MANIFEST_NAME
        if not path.is_file():
            raise DatasetError(f"{path} is missing: run `python -m binding_gate.cli dataset-prepare --config <config>` first")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            content = manifest["content"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DatasetError(f"{path}: unreadable manifest: {exc}") from exc
        actual = sha256_json(content)
        if actual != manifest.get("content_sha256"):
            raise DatasetError(f"{path}: content_sha256 does not match its content (the manifest was edited)")
        if actual != self.data_sha256:
            raise DatasetError(
                f"the generated dataset in {self.root} has content hash {actual}, but the config pins {self.data_sha256}"
            )
        items = self.root / sec_build.ITEMS_NAME
        if not items.is_file() or file_sha256(items) != content.get("items_sha256"):
            raise DatasetError(f"{items} does not match the items_sha256 of the manifest")
        selection = self.root / sec_build.SELECTION_NAME
        if not selection.is_file() or sha256_json(json.loads(selection.read_text(encoding="utf-8"))) != content.get("selection_sha256"):
            raise DatasetError(f"{selection} does not match the selection_sha256 of the manifest")
        self._manifest = manifest
        return manifest

    def fingerprint(self) -> str:
        self.verify()
        return self.data_sha256

    def iter_items(self) -> Iterator[Item]:
        self.verify()
        seen: set[str] = set()
        path = self.root / sec_build.ITEMS_NAME
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = Item.from_dict(json.loads(line))
            except (ValueError, KeyError, TypeError) as exc:
                raise DatasetError(f"{path}:{number}: not a valid item: {exc}") from exc
            if item.dataset != DATASET_NAME or item.id in seen:
                raise DatasetError(f"{path}:{number}: wrong dataset or duplicate item id {item.id!r}")
            roles = item.gold.get("document_roles")
            if not isinstance(roles, dict) or set(roles) != {d.id for d in item.documents}:
                raise DatasetError(f"{path}:{number}: every document needs a role")
            seen.add(item.id)
            yield item


# --------------------------------------------------------------------------- #
# Presentation (answering.PRESENTATIONS["sec_filings"])
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = (
    "You are a financial research assistant. Answer the question using only the documents provided below, "
    "which are financial statements from annual reports filed with the U.S. Securities and Exchange Commission."
)
"""Written for this study. It names no company and says nothing about peer documents or missing
documents, so that the gate, not the prompt, is what differs between arms."""


def filing_messages(question: str, documents: Sequence[Document], answer_instructions: str) -> tuple[Message, ...]:
    blocks = "\n\n".join(f"[Document {n} — source: {d.id}]\n{d.text}" for n, d in enumerate(documents, start=1))
    user = f"Documents:\n\n{blocks}\n\nQuestion: {question}"
    if answer_instructions:
        user = f"{user}\n\n{answer_instructions}"
    return (Message("system", SYSTEM_PROMPT), Message("user", user))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m binding_gate.datasets.sec_financials")
    parser.add_argument("command", choices=["prepare"])
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--build-config", default=DEFAULT_BUILD_CONFIG)
    args = parser.parse_args(argv)
    try:
        adapter = SecFinancialsAdapter(root=args.root, build_config=args.build_config)
        print(json.dumps(adapter.prepare(), indent=2, sort_keys=True))
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
