"""Deterministic builder of the layer-2 dataset ``sec_financials`` from SEC EDGAR (PR 17).

``python -m binding_gate.cli dataset-prepare --config configs/dev_sec_baseline.toml``
(free; network access to sec.gov and data.sec.gov; repository secret
``SEC_CONTACT_EMAIL``) runs :func:`prepare`, which writes a versioned dataset
directory. Runs never touch EDGAR: they read that directory through the adapter
``binding_gate.datasets.sec_financials``, which verifies it against the pinned
manifest hash (``data_sha256`` in the run configs).

Every rule below is version :data:`BUILD_VERSION` of the pre-registration
(PREREGISTRATION.md section 3, layer 2; all numbers PROPOSED). Nothing here
calls a model, uses randomness or reads the clock except to record retrieval
times in the (unpinned) retrieval log.

EDGAR facts this module relies on are **unverified** (the PR 17 sandbox could not
reach sec.gov): URL patterns and JSON field names of ``company_tickers.json``,
the XBRL ``frames`` and ``companyfacts`` APIs and the ``submissions`` API, and
the per-filing ``FilingSummary.xml`` and ``R<n>.htm`` statement pages. The code
checks every field it uses and refuses (or excludes a company with a recorded
reason) when the data do not have the expected shape; the first build in GitHub
Actions settles it.

Phase 1, selection (the company universe; :func:`select_universe`)
------------------------------------------------------------------
1. Candidates: every entity of the XBRL frame ``us-gaap/Assets/USD/CY2024Q4I``
   (total assets at the calendar instant 2024-12-31, one fact per filer) whose
   CIK is in ``company_tickers.json`` (listed companies) and whose ``loc`` field,
   when present, starts with ``US``; ranked by total assets, descending (ties:
   CIK ascending).
2. In rank order, the ``submissions`` record of each candidate is read until
   every sector quota of :data:`SECTOR_QUOTAS` (SIC divisions; 300 companies in
   all) is filled or :data:`MAX_CANDIDATES` candidates were examined. A
   candidate is taken when its SIC code lies in a sector whose quota is not yet
   full, is not in :data:`EXCLUDED_SIC` (commodity pools, funds, blank-check
   companies, asset-backed issuers), ``entityType`` (when present) is
   ``operating``, and it filed an original ``10-K`` (not ``10-K/A``) with XBRL
   in the filing window :data:`FILING_WINDOW`; the latest such filing is THE
   filing of the company. Every candidate examined is recorded with the reason
   it was taken or skipped.
3. The result, ``selection.json``, is a snapshot of mutable sources (the ticker
   list and the frame change over time). After the first build it is committed
   (``configs/datasets/sec_financials_selection_v1.json``) and named in the
   builder config (``selection_file``), so every later build reads only
   immutable sources (archived filing documents and facts filtered by
   accession number).

Phase 2, build (documents, gold, conditions; :func:`build_dataset`)
--------------------------------------------------------------------
* **Statements.** From the filing's ``FilingSummary.xml``: the cover report
  (registrant name), the income statement and the balance sheet (rules in
  :func:`classify_reports`), each an EDGAR-rendered ``R<n>.htm`` page of the
  filing's own XBRL financial statements.
* **Document text** (:func:`render_document`, version 1): first line
  ``"<registrant name>: annual report on Form 10-K for the fiscal year ended
  <Month D, YYYY>"`` (the name standing alone at the start as on the cover page);
  then each statement as ``"<registrant name>: <statement title with its stated
  unit scale>"`` (as the printed statements carry the company name above each
  statement) followed by one line per table row, ``"<row label> | <column
  label>: <cell text> | ..."``; a column header shared by every column (e.g.
  "12 Months Ended") is written once on the statement line. Income statement
  first, then the balance sheet. If the text exceeds :data:`DOC_CHAR_BUDGET`
  characters, each statement keeps its leading rows within its share of the
  budget (shares split evenly; a share a statement does not need goes to the
  other) and the cut is marked ``"[<n> more rows not shown]"``.
* **Gold** (:func:`extract_metric`): for each metric of :data:`METRICS`, the
  first concept in its preference list with exactly one value, in the
  company's ``companyfacts``, for the filing's accession number, form ``10-K``,
  period ending on the filing's report date (duration metrics: a period of 350
  to 380 days; balance-sheet metrics: an instant) AND whose value is displayed,
  at the statement's stated scale, in the current-year column of the kept text
  of the matching statement. Values are therefore AS REPORTED IN THAT FILING:
  a later restatement (in the next 10-K or a 10-K/A) never changes gold, and
  prior-year values come from the same filing (as reported there). Net income
  is not used when the filing also reports a different consolidated
  ``ProfitLoss`` for the year (two "net income" lines). The other annual
  periods of the same concept in the same filing are kept as
  ``target_alternatives`` (wrong-period detection).
* **Peers** (:func:`assign_peers`): companies are paired within the same split
  (``binding_gate.split``, unit = CIK) and the same two-digit SIC major group;
  a group with one member is pooled with the other singletons of its sector in
  that split; a company still alone has no peer and is excluded. Within a
  group the members are ordered by SHA-256(``"<seed>\\npeer\\n<cik>"``) and
  each member's peer is the next one in that cycle. So no company appears in
  both splits, as target or as peer.
* **Metric** of a company: among the metrics available for it and (if any)
  for its peer, the one with the smallest SHA-256(``"<seed>\\nmetric\\n<cik>\\n
  <metric>"``).
* **Conditions** (strata ``"<target>/<competitor>"``): ``present/none`` (the
  target filing only: the multi-year table tests the period binding),
  ``present/peer`` (target and same-industry peer filing; order by
  SHA-256(``"<seed>\\norder\\n<item id>"``), first byte even = target first)
  and ``absent/peer`` (the peer filing only: the correct behaviour is to
  abstain). Documents are labelled ``doc1``, ``doc2`` by position, so the
  source label reveals no role. No document is edited; a unit-scale variant
  is not constructed (the scale stated in each statement is kept as filed).
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from binding_gate.canonical import canonical_json, sha256_json, sha256_text, utc_now_iso
from binding_gate.contracts import Document, Item
from binding_gate.datasets import sec_edgar
from binding_gate.datasets.sec_edgar import EdgarClient, EdgarError, NotFound
from binding_gate.split import assign_split
from binding_gate.textnorm import normalize_text

DATASET_NAME = "sec_financials"
BUILD_VERSION = 1
SEED = 20261122
"""Pre-registered seed of every hash-based choice of the builder (fixed 2026-11-22, before any data was fetched)."""

FRAME = ("us-gaap", "Assets", "USD", "CY2024Q4I")
FILING_WINDOW = ("2025-07-01", "2026-06-30")
"""Inclusive filing-date window of THE 10-K of each company (one full filing year before the study)."""
MAX_CANDIDATES = 2500
DOC_CHAR_BUDGET = 10_000
DURATION_DAYS = (350, 380)

SECTOR_QUOTAS: tuple[tuple[str, int, int, int], ...] = (
    # (sector, first SIC, last SIC, companies) - SIC divisions B to I
    ("mining", 1000, 1499, 20),
    ("construction", 1500, 1799, 10),
    ("manufacturing", 2000, 3999, 110),
    ("transportation_communications_utilities", 4000, 4999, 40),
    ("wholesale_trade", 5000, 5199, 15),
    ("retail_trade", 5200, 5999, 30),
    ("finance_insurance_real_estate", 6000, 6799, 40),
    ("services", 7000, 8999, 35),
)
EXCLUDED_SIC = frozenset({6189, 6221, 6722, 6726, 6770})
"""Asset-backed issuers, commodity pools, investment funds and trusts, blank-check companies:
their annual reports have no comparable income statement."""


@dataclass(frozen=True)
class Metric:
    name: str
    statement: str  # "income_statement" or "balance_sheet"
    period: str  # "duration" or "instant"
    concepts: tuple[str, ...]
    question: str


METRICS: dict[str, Metric] = {
    m.name: m
    for m in (
        Metric(
            "revenue", "income_statement", "duration",
            ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
             "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"),
            "What was {name}'s total revenue, in US dollars, for the fiscal year ended {date}, "
            "as reported in its annual report on Form 10-K?",
        ),
        Metric(
            "operating_income", "income_statement", "duration", ("OperatingIncomeLoss",),
            "What was {name}'s operating income (or loss), in US dollars, for the fiscal year ended {date}, "
            "as reported in its annual report on Form 10-K?",
        ),
        Metric(
            "net_income", "income_statement", "duration", ("NetIncomeLoss",),
            "What was {name}'s net income (or loss), in US dollars, for the fiscal year ended {date}, "
            "as reported in its annual report on Form 10-K?",
        ),
        Metric(
            "total_assets", "balance_sheet", "instant", ("Assets",),
            "What were {name}'s total assets, in US dollars, as of {date}, "
            "as reported in its annual report on Form 10-K?",
        ),
        Metric(
            "cash", "balance_sheet", "instant", ("CashAndCashEquivalentsAtCarryingValue",),
            "What were {name}'s cash and cash equivalents, in US dollars, as of {date}, "
            "as reported in its annual report on Form 10-K?",
        ),
    )
}  # fmt: skip
QUESTION_TEMPLATE_VERSION = 1
RENDER_VERSION = 1

CONDITIONS: tuple[tuple[str, str, bool, bool], ...] = (
    # (condition name, stratum, target shown, peer shown)
    ("target_only", "present/none", True, False),
    ("target_peer", "present/peer", True, True),
    ("peer_only", "absent/peer", False, True),
)

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")  # fmt: skip


class BuildError(RuntimeError):
    """Raised when the dataset cannot be built deterministically (nothing is written as final)."""


def parameters() -> dict[str, Any]:
    """Every pre-registered parameter of the build (part of the pinned manifest content)."""
    return {
        "build_version": BUILD_VERSION,
        "seed": SEED,
        "frame": "/".join(FRAME),
        "filing_window": list(FILING_WINDOW),
        "max_candidates": MAX_CANDIDATES,
        "sector_quotas": [list(q) for q in SECTOR_QUOTAS],
        "excluded_sic": sorted(EXCLUDED_SIC),
        "doc_char_budget": DOC_CHAR_BUDGET,
        "duration_days": list(DURATION_DAYS),
        "metrics": {m.name: {"statement": m.statement, "period": m.period, "concepts": list(m.concepts),
                             "question": m.question} for m in METRICS.values()},
        "question_template_version": QUESTION_TEMPLATE_VERSION,
        "render_version": RENDER_VERSION,
        "conditions": [list(c) for c in CONDITIONS],
    }


def key(*parts: object) -> str:
    """SHA-256 hex of ``"<seed>\\n<part>\\n<part>..."`` (every hash-based choice of the builder)."""
    return hashlib.sha256("\n".join([str(SEED), *map(str, parts)]).encode("utf-8")).hexdigest()


def cik10(cik: int) -> str:
    return f"{int(cik):010d}"


def group_id_of(cik: int) -> str:
    return f"sec/{cik10(cik)}"


def split_of(cik: int) -> str:
    return assign_split(DATASET_NAME, group_id_of(cik)).value


def long_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def sector_of(sic: int) -> str | None:
    for name, low, high, _quota in SECTOR_QUOTAS:
        if low <= sic <= high:
            return name
    return None


# =========================================================================== #
# Builder config (fair-access settings; not part of the pinned content)
# =========================================================================== #


@dataclass(frozen=True)
class BuildConfig:
    user_agent_project: str = sec_edgar.DEFAULT_PROJECT_PART
    max_requests_per_second: float = sec_edgar.DEFAULT_RPS
    max_retries: int = sec_edgar.DEFAULT_MAX_RETRIES
    selection_file: str = ""

    @classmethod
    def load(cls, path: str | Path | None) -> BuildConfig:
        if not path:
            return cls()
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        allowed = {"config_version", "user_agent_project", "max_requests_per_second", "max_retries", "selection_file"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise BuildError(f"{path}: unknown key(s) {unknown}")
        if raw.get("config_version") != 1:
            raise BuildError(f"{path}: config_version must be 1")
        config = cls(
            user_agent_project=str(raw.get("user_agent_project", sec_edgar.DEFAULT_PROJECT_PART)),
            max_requests_per_second=raw.get("max_requests_per_second", sec_edgar.DEFAULT_RPS),
            max_retries=raw.get("max_retries", sec_edgar.DEFAULT_MAX_RETRIES),
            selection_file=str(raw.get("selection_file", "")),
        )
        if "@" in config.user_agent_project:
            raise BuildError(f"{path}: user_agent_project must not contain an e-mail address (use the secret SEC_CONTACT_EMAIL)")
        return config


# =========================================================================== #
# Phase 1: selection
# =========================================================================== #


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def ticker_map(raw: Any) -> dict[int, dict[str, Any]]:
    """``company_tickers.json`` -> ``cik -> {"title", "tickers"}`` (tickers in file order)."""
    rows = raw.values() if isinstance(raw, dict) else raw if isinstance(raw, list) else None
    if rows is None:
        raise BuildError("company_tickers.json: expected an object or a list of entries")
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise BuildError("company_tickers.json: every entry must be an object")
        cik = _int(row.get("cik_str", row.get("cik")))
        ticker, title = row.get("ticker"), row.get("title")
        if cik is None or not isinstance(ticker, str) or not isinstance(title, str):
            raise BuildError(f"company_tickers.json: entry without cik_str/ticker/title: {str(row)[:80]}")
        entry = out.setdefault(cik, {"title": title, "tickers": []})
        if ticker not in entry["tickers"]:
            entry["tickers"].append(ticker)
    return out


def frame_candidates(frame: Any, tickers: Mapping[int, Any]) -> list[dict[str, Any]]:
    """The ranked candidates of rule 1 (see the module docstring)."""
    data = frame.get("data") if isinstance(frame, dict) else None
    if not isinstance(data, list):
        raise BuildError("frames response: no 'data' list")
    seen: set[int] = set()
    out = []
    for row in data:
        cik = _int(row.get("cik")) if isinstance(row, dict) else None
        val = row.get("val") if isinstance(row, dict) else None
        if cik is None or not isinstance(val, (int, float)) or isinstance(val, bool):
            raise BuildError(f"frames response: entry without cik/val: {str(row)[:80]}")
        loc = row.get("loc")
        if cik in seen or cik not in tickers or (isinstance(loc, str) and loc and not loc.upper().startswith("US")):
            continue
        seen.add(cik)
        out.append({"cik": cik, "assets": val, "loc": loc})
    out.sort(key=lambda r: (-r["assets"], r["cik"]))
    return out


FILING_FIELDS = ("accessionNumber", "filingDate", "reportDate", "form")


def filing_rows(block: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Column arrays of a submissions ``filings`` block -> one dict per filing."""
    columns = {name: block.get(name) for name in (*FILING_FIELDS, "isXBRL", "primaryDocument")}
    if not all(isinstance(columns[name], list) for name in FILING_FIELDS):
        raise BuildError("submissions: filings block lacks accessionNumber/filingDate/reportDate/form arrays")
    n = len(columns["accessionNumber"])
    if any(isinstance(v, list) and len(v) != n for v in columns.values()):
        raise BuildError("submissions: filings arrays of different lengths")
    return [{name: (v[i] if isinstance(v, list) else None) for name, v in columns.items()} for i in range(n)]


def choose_filing(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """THE 10-K: latest original ``10-K`` with XBRL filed inside :data:`FILING_WINDOW` (ties: accession)."""
    low, high = FILING_WINDOW
    eligible = [
        dict(r) for r in rows
        if r.get("form") == "10-K"
        and isinstance(r.get("filingDate"), str) and low <= r["filingDate"] <= high
        and isinstance(r.get("reportDate"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["reportDate"])
        and r.get("isXBRL") in (None, 1, True, "1")
    ]  # fmt: skip
    if not eligible:
        return None
    return max(eligible, key=lambda r: (r["filingDate"], r["accessionNumber"]))


def submissions_filings(client: EdgarClient, cik: int, sub: Mapping[str, Any]) -> list[dict[str, Any]]:
    filings = sub.get("filings")
    if not isinstance(filings, dict) or not isinstance(filings.get("recent"), dict):
        raise BuildError(f"submissions of CIK {cik}: no filings.recent block")
    rows = filing_rows(filings["recent"])
    low, high = FILING_WINDOW
    for page in filings.get("files") or []:
        if not isinstance(page, dict) or not isinstance(page.get("name"), str):
            continue
        start, end = page.get("filingFrom", ""), page.get("filingTo", "")
        if isinstance(start, str) and isinstance(end, str) and start <= high and end >= low:
            if not re.fullmatch(r"CIK\d{10}-submissions-\d{3}\.json", page["name"]):
                raise BuildError(f"submissions of CIK {cik}: unexpected page name {page['name']!r}")
            extra = client.get_json(sec_edgar.SUBMISSIONS_PAGE_URL.format(name=page["name"]))
            if isinstance(extra, dict):
                rows.extend(filing_rows(extra))
    return rows


def select_universe(client: EdgarClient) -> dict[str, Any]:
    """Phase 1 (module docstring). Returns the selection record written to ``selection.json``."""
    tickers = ticker_map(client.get_json(sec_edgar.COMPANY_TICKERS_URL))
    taxonomy, tag, unit, period = FRAME
    frame_url = sec_edgar.FRAMES_URL.format(taxonomy=taxonomy, tag=tag, unit=unit, period=period)
    candidates = frame_candidates(client.get_json(frame_url), tickers)
    quotas = {name: quota for name, _l, _h, quota in SECTOR_QUOTAS}
    taken: dict[str, int] = {name: 0 for name in quotas}
    companies: list[dict[str, Any]] = []
    examined: list[dict[str, Any]] = []
    for rank, candidate in enumerate(candidates[:MAX_CANDIDATES], start=1):
        if all(taken[s] >= quotas[s] for s in quotas):
            break
        cik = candidate["cik"]
        try:
            sub = client.get_json(sec_edgar.SUBMISSIONS_URL.format(cik=cik))
        except NotFound:
            examined.append({"cik": cik, "rank": rank, "taken": False, "reason": "no_submissions"})
            continue
        if not isinstance(sub, dict):
            raise BuildError(f"submissions of CIK {cik}: not an object")
        sic = _int(sub.get("sic"))
        sector = sector_of(sic) if sic is not None else None
        reason = None
        if sic is None or sector is None:
            reason = "sic_outside_sectors"
        elif sic in EXCLUDED_SIC:
            reason = "sic_excluded"
        elif taken[sector] >= quotas[sector]:
            reason = "sector_quota_full"
        elif sub.get("entityType") not in (None, "", "operating"):
            reason = "not_an_operating_company"
        filing = None
        if reason is None:
            filing = choose_filing(submissions_filings(client, cik, sub))
            if filing is None:
                reason = "no_10k_in_window"
        if reason is not None:
            examined.append({"cik": cik, "rank": rank, "taken": False, "reason": reason, "sic": sic})
            continue
        assert filing is not None and sector is not None
        taken[sector] += 1
        names = [tickers[cik]["title"]] + ([sub["name"]] if isinstance(sub.get("name"), str) else [])
        sub_tickers = [t for t in sub.get("tickers") or [] if isinstance(t, str)]
        companies.append({
            "cik": cik,
            "edgar_name": sub.get("name") if isinstance(sub.get("name"), str) else tickers[cik]["title"],
            "names": list(dict.fromkeys(names)),
            "tickers": list(dict.fromkeys(tickers[cik]["tickers"] + sub_tickers)),
            "sic": sic,
            "sic_description": sub.get("sicDescription") if isinstance(sub.get("sicDescription"), str) else None,
            "sector": sector,
            "assets_rank": rank,
            "frame_assets_usd": candidate["assets"],
            "accession": filing["accessionNumber"],
            "filing_date": filing["filingDate"],
            "report_date": filing["reportDate"],
            "split": split_of(cik),
        })
        examined.append({"cik": cik, "rank": rank, "taken": True, "reason": "taken", "sic": sic})
    return {
        "selection_version": BUILD_VERSION,
        "parameters": {k: parameters()[k] for k in ("seed", "frame", "filing_window", "max_candidates", "sector_quotas", "excluded_sic")},
        "candidates_in_frame": len(candidates),
        "companies": companies,
        "examined": examined,
        "quota_shortfall": {s: quotas[s] - taken[s] for s in quotas if taken[s] < quotas[s]},
    }


# =========================================================================== #
# FilingSummary.xml and R<n>.htm statement pages
# =========================================================================== #


@dataclass(frozen=True)
class ReportRef:
    position: int
    short_name: str
    long_name: str
    html_file: str
    menu_category: str


def parse_filing_summary(xml_bytes: bytes) -> list[ReportRef]:
    if len(xml_bytes) > 5_000_000:
        raise BuildError("FilingSummary.xml larger than 5 MB")
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise BuildError(f"FilingSummary.xml: {exc}") from exc
    reports = []
    for element in root.iter("Report"):
        def text(tag: str) -> str:
            child = element.find(tag)
            return " ".join((child.text or "").split()) if child is not None else ""

        html_file = text("HtmlFileName")
        if not html_file or not re.fullmatch(r"R\d+\.html?", html_file):
            continue
        reports.append(ReportRef(_int(text("Position")) or 0, text("ShortName"), text("LongName"), html_file, text("MenuCategory")))
    return sorted(reports, key=lambda r: (r.position, r.html_file))


_BS = re.compile(r"balance sheets?|financial position|financial condition|statements? of condition", re.I)
_IS = re.compile(r"operations|income|earnings|\bloss\b", re.I)
_NOT_IS = re.compile(r"parenthetical|cash flows?|equity|stockholders|shareholders|partners|members|changes in|"
                     r"balance sheet|financial (position|condition)", re.I)
_COVER = re.compile(r"\bcover\b|document and entity information", re.I)


def classify_reports(reports: Sequence[ReportRef]) -> dict[str, ReportRef]:
    """``{"cover", "income_statement", "balance_sheet"}`` -> report (missing keys: not found).

    Statements are the reports of menu category ``Statements`` in position order. Balance sheet: the first
    whose name matches balance sheet / financial position / financial condition and not "parenthetical".
    Income statement: the first whose name matches operations / income / earnings / loss and none of
    parenthetical, cash flow, equity, stockholders, shareholders, partners, members, changes in, balance
    sheet, financial position or condition; a name without "comprehensive" is preferred. Cover: the first
    report of category ``Cover`` or named cover / document and entity information.
    """
    out: dict[str, ReportRef] = {}
    statements = [r for r in reports if r.menu_category.lower() == "statements"]
    for r in statements:
        name = f"{r.short_name} {r.long_name}"
        if "balance_sheet" not in out and _BS.search(name) and "parenthetical" not in name.lower():
            out["balance_sheet"] = r
    income = [r for r in statements if _IS.search(f"{r.short_name} {r.long_name}") and not _NOT_IS.search(f"{r.short_name} {r.long_name}")]
    plain = [r for r in income if "comprehensive" not in f"{r.short_name} {r.long_name}".lower()]
    if plain or income:
        out["income_statement"] = (plain or income)[0]
    for r in reports:
        if r.menu_category.lower() == "cover" or _COVER.search(f"{r.short_name} {r.long_name}"):
            out["cover"] = r
            break
    return out


@dataclass
class Cell:
    tag: str
    text: str
    colspan: int
    rowspan: int
    classes: str


class _TableParser(HTMLParser):
    """Collects the cells of every ``<table>`` (nested tables are kept separate); ``<br>`` is a space."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict[str, Any]] = []
        self._stack: list[dict[str, Any]] = []
        self._cell: Cell | None = None
        self._buffer: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "table":
            table = {"classes": a.get("class", ""), "rows": []}
            self.tables.append(table)
            self._stack.append(table)
        elif tag == "tr" and self._stack:
            self._stack[-1]["rows"].append([])
        elif tag in ("td", "th") and self._stack and self._stack[-1]["rows"]:
            self._cell = Cell(tag, "", _int(a.get("colspan")) or 1, _int(a.get("rowspan")) or 1, a.get("class", ""))
            self._buffer = []
        elif tag == "br" and self._cell is not None:
            self._buffer.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._cell is not None and self._stack:
            self._cell.text = " ".join("".join(self._buffer).split())
            self._stack[-1]["rows"][-1].append(self._cell)
            self._cell = None
        elif tag == "table" and self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._cell is not None and not self._skip:
            self._buffer.append(data)


@dataclass
class StatementTable:
    title: str
    column_labels: list[str]
    shared_header: str
    rows: list[tuple[str, list[str]]]  # (label, cell texts per data column)
    scale: int  # stated scale of US-dollar amounts (1, 1_000, 1_000_000, 1_000_000_000)


_SCALE = re.compile(r"\$\s*in\s+(thousands|millions|billions)", re.I)
SCALES = {"thousands": 1_000, "millions": 1_000_000, "billions": 1_000_000_000}


def parse_statement(html: bytes | str) -> StatementTable:
    """Flatten the first report table of an ``R<n>.htm`` page (header rows of ``th`` cells, then rows)."""
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    parser = _TableParser()
    parser.feed(text)
    parser.close()
    tables = [t for t in parser.tables if "report" in t["classes"].split()] or [t for t in parser.tables if t["rows"]]
    if not tables:
        raise BuildError("statement page has no table")
    rows = [r for r in tables[0]["rows"] if r]
    header_rows: list[list[Cell]] = []
    while rows and all(c.tag == "th" for c in rows[0]):
        header_rows.append(rows.pop(0))
    if not header_rows:
        raise BuildError("statement page has no header row")
    # Lay the header cells out on a grid (rowspan/colspan); grid column 0 holds the title.
    grid: dict[tuple[int, int], Cell] = {}
    for r, row in enumerate(header_rows):
        c = 0
        for cell in row:
            while (r, c) in grid:
                c += 1
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    grid[(r + dr, c + dc)] = cell
            c += cell.colspan
    title_cell = grid.get((0, 0))
    title = title_cell.text if title_cell else ""
    width = max((len(row) for row in rows), default=1)
    labels: list[list[str]] = []
    for k in range(1, width):
        parts: list[str] = []
        for r in range(len(header_rows)):
            cell = grid.get((r, k))
            if cell is not None and cell is not title_cell and cell.text and (not parts or parts[-1] != cell.text):
                parts.append(cell.text)
        labels.append(parts)
    shared = ""
    if labels and all(len(p) >= 2 for p in labels) and len({p[0] for p in labels}) == 1:
        shared = labels[0][0]
        labels = [p[1:] for p in labels]
    column_labels = [" ".join(p) or f"column {i + 1}" for i, p in enumerate(labels)]
    body: list[tuple[str, list[str]]] = []
    for row in rows:
        if any(c.tag == "th" for c in row) and not any(c.tag == "td" for c in row):
            continue
        label = row[0].text
        values = [c.text for c in row[1:]]
        values += [""] * (len(column_labels) - len(values))
        body.append((label, values[: len(column_labels)]))
    match = _SCALE.search(title)
    return StatementTable(title, column_labels, shared, body, SCALES[match.group(1).lower()] if match else 1)


_CELL_NUMBER = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")


def cell_value(text: str) -> Decimal | None:
    """The number shown in a statement cell (``"$ (1,234)"`` -> -1234), or ``None``."""
    cleaned = re.sub(r"\[\d+\]", "", text).replace("$", "").replace(" ", "").replace(" ", "")
    if not cleaned or not _CELL_NUMBER.match(cleaned):
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")") or cleaned.startswith("-")
    try:
        value = Decimal(cleaned.strip("()").lstrip("-").replace(",", ""))
    except InvalidOperation:
        return None
    return -value if negative else value


def registrant_name(cover: StatementTable | None) -> str | None:
    if cover is None:
        return None
    for label, values in cover.rows:
        if normalize_text(label) == "entity registrant name":
            name = next((v for v in values if v), None)
            return " ".join(name.split()) if name else None
    return None


# =========================================================================== #
# Document rendering
# =========================================================================== #


def row_line(label: str, values: Sequence[str], column_labels: Sequence[str]) -> str:
    cells = [f"{column_labels[i]}: {v}" for i, v in enumerate(values) if v]
    return " | ".join([label, *cells]) if cells else label


def statement_lines(name: str, table: StatementTable) -> tuple[str, list[str]]:
    title = f"{name}: {table.title}" + (f" (all columns: {table.shared_header})" if table.shared_header else "")
    return title, [row_line(label, values, table.column_labels) for label, values in table.rows]


@dataclass
class RenderedDocument:
    text: str
    kept_rows: dict[str, int]  # statement -> number of rows kept
    total_rows: dict[str, int]


def render_document(name: str, report_date: str, statements: Mapping[str, StatementTable]) -> RenderedDocument:
    """The document text of one filing (version :data:`RENDER_VERSION`; see the module docstring)."""
    header = f"{name}: annual report on Form 10-K for the fiscal year ended {long_date(report_date)}"
    order = [s for s in ("income_statement", "balance_sheet") if s in statements]
    blocks = {s: statement_lines(name, statements[s]) for s in order}
    full = {s: len(t) + 1 + sum(len(line) + 1 for line in rows) for s, (t, rows) in blocks.items()}
    available = DOC_CHAR_BUDGET - len(header)
    shares: dict[str, int] = {}
    if sum(full.values()) <= available:
        shares = dict(full)
    else:
        even = available // max(1, len(order))
        small = {s: v for s, v in full.items() if v <= even}
        rest = [s for s in order if s not in small]
        remaining = available - sum(small.values())
        shares = {**small, **{s: remaining // max(1, len(rest)) for s in rest}}
    lines = [header]
    kept: dict[str, int] = {}
    for s in order:
        title, rows = blocks[s]
        used = len(title) + 1
        out = [title]
        n = 0
        for i, line in enumerate(rows):
            marker = len(f"[{len(rows) - i - 1} more rows not shown]") + 1 if i < len(rows) - 1 else 0
            if used + len(line) + 1 + marker > shares[s]:
                break
            out.append(line)
            used += len(line) + 1
            n += 1
        if n < len(rows):
            out.append(f"[{len(rows) - n} more rows not shown]")
        lines.extend(out)
        kept[s] = n
    return RenderedDocument("\n".join(lines), kept, {s: len(blocks[s][1]) for s in order})


# =========================================================================== #
# Gold from companyfacts
# =========================================================================== #


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def concept_facts(facts: Mapping[str, Any], concept: str, accession: str) -> list[dict[str, Any]]:
    """Facts of ``us-gaap:<concept>`` in USD reported by the filing ``accession`` on form 10-K."""
    node = facts.get("facts", {}).get("us-gaap", {}).get(concept) if isinstance(facts, dict) else None
    rows = node.get("units", {}).get("USD") if isinstance(node, dict) else None
    if not isinstance(rows, list):
        return []
    out = []
    for f in rows:
        if not isinstance(f, dict) or f.get("accn") != accession or f.get("form") != "10-K":
            continue
        val, end = f.get("val"), f.get("end")
        if not isinstance(val, (int, float)) or isinstance(val, bool) or not isinstance(end, str):
            continue
        if isinstance(val, float) and not val.is_integer():
            continue
        out.append({"start": f.get("start"), "end": end, "val": int(val), "fy": f.get("fy"), "fp": f.get("fp"), "filed": f.get("filed")})
    return out


def _periods(rows: Sequence[Mapping[str, Any]], period: str) -> dict[tuple[str | None, str], set[int]]:
    out: dict[tuple[str | None, str], set[int]] = {}
    for f in rows:
        start = f.get("start")
        if period == "duration":
            if not isinstance(start, str) or not DURATION_DAYS[0] <= _days(start, f["end"]) <= DURATION_DAYS[1]:
                continue
        elif start is not None:
            continue
        out.setdefault((start if period == "duration" else None, f["end"]), set()).add(f["val"])
    return out


def find_cell(table: StatementTable, value: int, year: int, kept_rows: int) -> dict[str, Any] | None:
    """The first kept row/column whose cell shows ``value`` at the stated scale in a column of ``year``."""
    for index, (label, cells) in enumerate(table.rows[:kept_rows]):
        for col, text in enumerate(cells):
            shown = cell_value(text)
            if shown is None or str(year) not in table.column_labels[col]:
                continue
            if abs(shown * table.scale - value) * 2 < table.scale:
                return {"row_index": index, "row_label": label, "column_label": table.column_labels[col], "cell_text": text}
    return None


def extract_metric(
    metric: Metric,
    facts: Mapping[str, Any],
    accession: str,
    report_date: str,
    table: StatementTable | None,
    kept_rows: int,
) -> dict[str, Any]:
    """Gold for one metric of one filing, or ``{"available": False, "reason": ...}`` (module docstring)."""
    if table is None:
        return {"available": False, "reason": f"no_{metric.statement}"}
    year = date.fromisoformat(report_date).year
    reasons = []
    for concept in metric.concepts:
        periods = _periods(concept_facts(facts, concept, accession), metric.period)
        current = [(k, v) for k, v in periods.items() if k[1] == report_date]
        if not current:
            reasons.append(f"{concept}:no_current_fact")
            continue
        if len(current) > 1 or len(current[0][1]) != 1:
            reasons.append(f"{concept}:ambiguous_current_fact")
            continue
        (start, end), values = current[0]
        value = next(iter(values))
        cell = find_cell(table, value, year, kept_rows)
        if cell is None:
            reasons.append(f"{concept}:not_displayed_in_kept_text")
            continue
        if metric.name == "net_income":
            profit = _periods(concept_facts(facts, "ProfitLoss", accession), "duration")
            other = profit.get((start, end), set())
            if other and other != {value}:
                return {"available": False, "reason": "profit_loss_differs_from_net_income"}
        alternatives = [
            {"start": k[0], "end": k[1], "value": next(iter(v))}
            for k, v in sorted(periods.items(), key=lambda kv: (kv[0][1], kv[0][0] or ""))
            if k[1] != report_date and len(v) == 1
        ]
        return {
            "available": True,
            "metric": metric.name,
            "concept": concept,
            "statement": metric.statement,
            "value": value,
            "period": {"start": start, "end": end},
            "alternatives": alternatives,
            "display": {**cell, "scale": table.scale},
        }
    return {"available": False, "reason": ";".join(reasons) or "no_concept"}


def facts_for_concept(facts: Mapping[str, Any], concept: str, accession: str, period: str) -> list[dict[str, Any]]:
    """All single-valued periods of one concept in one filing (the peer's values for wrong-entity detection)."""
    periods = _periods(concept_facts(facts, concept, accession), period)
    return [{"start": k[0], "end": k[1], "value": next(iter(v))} for k, v in sorted(periods.items(), key=lambda kv: (kv[0][1], kv[0][0] or "")) if len(v) == 1]


# =========================================================================== #
# Phase 2: per-company build, peers, items
# =========================================================================== #


@dataclass
class CompanyBuild:
    company: dict[str, Any]
    name: str
    document: str
    metrics: dict[str, dict[str, Any]]
    sources: list[dict[str, Any]] = field(default_factory=list)
    facts_sha256: str = ""


def build_company(client: EdgarClient, company: Mapping[str, Any]) -> CompanyBuild | dict[str, Any]:
    """Statements, document and gold of one selected company; a dict ``{"cik", "reason"}`` if excluded."""
    cik, accession = int(company["cik"]), str(company["accession"])
    sources: list[dict[str, Any]] = []

    def fetch(filename: str) -> bytes:
        url = sec_edgar.archive_url(cik, accession, filename)
        body = client.get(url)
        sources.append({"url": url, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)})
        return body

    try:
        reports = classify_reports(parse_filing_summary(fetch("FilingSummary.xml")))
    except NotFound:
        return {"cik": cik, "reason": "no_filing_summary"}
    except BuildError as exc:
        return {"cik": cik, "reason": f"filing_summary_unreadable: {exc}"}
    statements: dict[str, StatementTable] = {}
    for kind in ("income_statement", "balance_sheet"):
        if kind in reports:
            try:
                statements[kind] = parse_statement(fetch(reports[kind].html_file))
            except NotFound:
                pass
            except BuildError as exc:
                return {"cik": cik, "reason": f"{kind}_unreadable: {exc}"}
    if not statements:
        return {"cik": cik, "reason": "no_statements"}
    cover = None
    if "cover" in reports:
        try:
            cover = parse_statement(fetch(reports["cover"].html_file))
        except (NotFound, BuildError):
            cover = None
    name = registrant_name(cover) or str(company["edgar_name"])
    rendered = render_document(name, str(company["report_date"]), statements)
    try:
        facts = client.get_json(sec_edgar.COMPANYFACTS_URL.format(cik=cik))
    except NotFound:
        return {"cik": cik, "reason": "no_companyfacts"}
    metrics = {
        m.name: extract_metric(m, facts, accession, str(company["report_date"]), statements.get(m.statement), rendered.kept_rows.get(m.statement, 0))
        for m in METRICS.values()
    }
    peer_values = {}
    for m in METRICS.values():
        info = metrics[m.name]
        concept = info["concept"] if info.get("available") else None
        if concept is None:  # values of the first concept with any annual fact, for wrong-entity detection
            concept = next((c for c in m.concepts if facts_for_concept(facts, c, accession, m.period)), None)
        peer_values[m.name] = {"concept": concept, "values": facts_for_concept(facts, concept, accession, m.period) if concept else []}
    fy = None
    for m in metrics.values():
        if m.get("available"):
            for f in concept_facts(facts, m["concept"], accession):
                if f["end"] == company["report_date"] and f["val"] == m["value"]:
                    fy = f.get("fy")
                    break
            break
    build = CompanyBuild(
        company={**dict(company), "registrant_name": name, "fiscal_year_focus": fy, "values_for_peers": peer_values,
                 "rows_kept": rendered.kept_rows, "rows_total": rendered.total_rows},
        name=name,
        document=rendered.text,
        metrics=metrics,
        sources=sources,
    )
    build.facts_sha256 = sha256_json({"metrics": metrics, "peer_values": peer_values})
    return build


def assign_peers(builds: Sequence[CompanyBuild]) -> tuple[dict[int, int], list[dict[str, Any]]]:
    """``target cik -> peer cik`` by the rule of the module docstring; and the companies left without a peer."""
    usable = [b for b in builds if any(m.get("available") for m in b.metrics.values())]
    by_group: dict[tuple[str, str], list[CompanyBuild]] = {}
    for b in usable:
        by_group.setdefault((b.company["split"], f"sic2:{int(b.company['sic']) // 100:02d}"), []).append(b)
    groups: dict[tuple[str, str], list[CompanyBuild]] = {}
    pooled: dict[tuple[str, str], list[CompanyBuild]] = {}
    for (split, grp), members in sorted(by_group.items()):
        if len(members) >= 2:
            groups[(split, grp)] = members
        else:
            pooled.setdefault((split, f"sector:{members[0].company['sector']}"), []).extend(members)
    alone: list[dict[str, Any]] = []
    for (split, grp), members in sorted(pooled.items()):
        if len(members) >= 2:
            groups[(split, grp)] = members
        else:
            alone.extend({"cik": m.company["cik"], "reason": "no_peer_in_split_and_sector"} for m in members)
    peers: dict[int, int] = {}
    for _group, members in sorted(groups.items()):
        ordered = sorted(members, key=lambda b: (key("peer", cik10(b.company["cik"])), b.company["cik"]))
        for i, b in enumerate(ordered):
            peers[int(b.company["cik"])] = int(ordered[(i + 1) % len(ordered)].company["cik"])
    return peers, alone


def choose_metric(target: CompanyBuild, peer: CompanyBuild) -> str | None:
    mine = [m for m, info in target.metrics.items() if info.get("available")]
    if not mine:
        return None
    both = [m for m in mine if peer.company["values_for_peers"][m]["values"]]
    pool = both or mine
    return min(pool, key=lambda m: (key("metric", cik10(target.company["cik"]), m), m))


def company_summary(b: CompanyBuild) -> dict[str, Any]:
    c = b.company
    return {
        "cik": c["cik"], "name": b.name, "edgar_name": c["edgar_name"], "names": c["names"], "tickers": c["tickers"],
        "sic": c["sic"], "sector": c["sector"], "accession": c["accession"], "report_date": c["report_date"],
        "filing_date": c["filing_date"], "fiscal_year_focus": c.get("fiscal_year_focus"),
    }


def build_items(builds: Sequence[CompanyBuild], peers: Mapping[int, int]) -> tuple[list[Item], list[dict[str, Any]]]:
    by_cik = {int(b.company["cik"]): b for b in builds}
    items: list[Item] = []
    skipped: list[dict[str, Any]] = []
    for cik in sorted(peers):
        target, peer = by_cik[cik], by_cik[peers[cik]]
        metric = choose_metric(target, peer)
        if metric is None:
            skipped.append({"cik": cik, "reason": "no_available_metric"})
            continue
        gold_metric = target.metrics[metric]
        report_date = str(target.company["report_date"])
        question = METRICS[metric].question.format(name=target.name, date=long_date(report_date))
        aliases = list(dict.fromkeys([target.name, *target.company["names"], *target.company["tickers"]]))
        peer_values = peer.company["values_for_peers"][metric]
        for condition, stratum, show_target, show_peer in CONDITIONS:
            item_id = f"sec/{cik10(cik)}/{metric}/{condition}"
            shown: list[tuple[str, str]] = []
            if show_target:
                shown.append(("target", target.document))
            if show_peer:
                shown.append(("competitor", peer.document))
            if len(shown) == 2 and int(key("order", item_id)[:2], 16) % 2 == 1:
                shown.reverse()
            documents = [Document.create(f"doc{i}", text) for i, (_role, text) in enumerate(shown, start=1)]
            gold = {
                "document_roles": {d.id: role for d, (role, _t) in zip(documents, shown)},
                "condition": condition,
                "target_shown": show_target,
                "metric": metric,
                "concept": gold_metric["concept"],
                "statement": gold_metric["statement"],
                "unit": "USD",
                "value": gold_metric["value"],
                "period": gold_metric["period"],
                "report_date": report_date,
                "accession": target.company["accession"],
                "form": "10-K",
                "fiscal_year_focus": target.company.get("fiscal_year_focus"),
                "display": gold_metric["display"],
                "target_alternatives": gold_metric["alternatives"],
                "company": company_summary(target),
                "entity_aliases": aliases,
                "competitor": {
                    **company_summary(peer),
                    "concept": peer_values["concept"],
                    "values": peer_values["values"],
                },
                "build_version": BUILD_VERSION,
                "question_template_version": QUESTION_TEMPLATE_VERSION,
            }
            items.append(Item(id=item_id, group_id=group_id_of(cik), dataset=DATASET_NAME, question=question,
                              documents=tuple(documents), gold=gold, stratum=stratum))
    return items, skipped


# =========================================================================== #
# The whole build, the manifest and its pinned hash
# =========================================================================== #

SELECTION_NAME = "selection.json"
ITEMS_NAME = "items.jsonl"
MANIFEST_NAME = "manifest.json"
RETRIEVAL_LOG_NAME = "retrieval_log.jsonl"
CACHE_DIRNAME = "cache"

REUSE_TERMS = (
    "Source: U.S. Securities and Exchange Commission, EDGAR (company_tickers.json, XBRL frames and companyfacts "
    "APIs, submissions API, and the filings' FilingSummary.xml and R<n>.htm statement pages). Filings are public "
    "records submitted to the SEC by the companies; the reuse terms of sec.gov (and whether any company asserts "
    "copyright in its statements) could NOT be verified in PR 17 (sec.gov was unreachable from the build "
    "sandbox): UNVERIFIED. Until verified, the generated dataset and traces are not committed or published."
)


def content_sha256(content: Mapping[str, Any]) -> str:
    """The pinned hash: SHA-256 of the canonical JSON of the manifest's ``content``."""
    return sha256_json(content)


def items_text(items: Sequence[Item]) -> str:
    return "".join(canonical_json(item.to_dict()) + "\n" for item in items)


def build_dataset(client: EdgarClient, selection: Mapping[str, Any]) -> dict[str, Any]:
    """Phase 2 over a selection. Returns ``{"items", "content", "excluded"}`` (nothing written)."""
    companies = selection.get("companies")
    if not isinstance(companies, list) or not companies:
        raise BuildError("selection.json lists no companies")
    builds: list[CompanyBuild] = []
    excluded: list[dict[str, Any]] = []
    for company in sorted(companies, key=lambda c: int(c["cik"])):
        if company.get("split") != split_of(int(company["cik"])):
            raise BuildError(f"selection.json: the split of CIK {company['cik']} disagrees with binding_gate.split")
        result = build_company(client, company)
        if isinstance(result, dict):
            excluded.append(result)
        else:
            builds.append(result)
    no_metric = [{"cik": b.company["cik"], "reason": "no_available_metric:" + ",".join(
        f"{k}={v.get('reason')}" for k, v in sorted(b.metrics.items()))} for b in builds if not any(m.get("available") for m in b.metrics.values())]
    peers, alone = assign_peers(builds)
    items, skipped = build_items(builds, peers)
    excluded += no_metric + alone + skipped
    text = items_text(items)
    used = sorted(peers)
    by_cik = {int(b.company["cik"]): b for b in builds}
    content = {
        "dataset": DATASET_NAME,
        "parameters": parameters(),
        "selection_sha256": sha256_json(selection),
        "items_sha256": sha256_text(text),
        "n_items": len(items),
        "n_companies": len({i.group_id for i in items}),
        "items_by_split": {s: sum(1 for i in items if split_of(int(i.gold["company"]["cik"])) == s) for s in ("dev", "test")},
        "items_by_stratum": {s: sum(1 for i in items if i.stratum == s) for _c, s, _t, _p in CONDITIONS},
        "items_by_metric": {m: sum(1 for i in items if i.gold["metric"] == m) for m in METRICS},
        "peers": {cik10(t): cik10(p) for t, p in sorted(peers.items())},
        "filings": [
            {"cik": cik, "accession": by_cik[cik].company["accession"], "documents": by_cik[cik].sources,
             "facts_extracted_sha256": by_cik[cik].facts_sha256}
            for cik in used
        ],
        "excluded": sorted(excluded, key=lambda e: (int(e["cik"]), e["reason"])),
    }
    return {"items": items, "items_text": text, "content": content}


def write_dataset(root: Path, selection: Mapping[str, Any], built: Mapping[str, Any], retrieval: Mapping[str, Any]) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    (root / SELECTION_NAME).write_text(canonical_json(selection) + "\n", encoding="utf-8", newline="\n")
    (root / ITEMS_NAME).write_text(built["items_text"], encoding="utf-8", newline="\n")
    manifest = {
        "manifest_version": 1,
        "content": built["content"],
        "content_sha256": content_sha256(built["content"]),
        "reuse_terms": REUSE_TERMS,
        "retrieval": dict(retrieval),  # unpinned: times and request counts; never the contact e-mail
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return manifest


def selection_file_sha256(text: str) -> str:
    """Hash of a selection as stored (canonical JSON, one line)."""
    return sha256_json(json.loads(text))


def prepare(
    root: str | Path,
    *,
    build_config: str | Path | None = None,
    contact_email: str | None = None,
    transport: sec_edgar.Transport | None = None,
    sleep: Any = None,
    repo_root: str | Path = ".",
) -> dict[str, Any]:
    """Phase 1 (or the committed selection) and phase 2; writes the dataset directory; returns the manifest.

    ``contact_email`` defaults to the secret ``SEC_CONTACT_EMAIL``; without it nothing is fetched.
    """
    config = BuildConfig.load(build_config)
    email = contact_email if contact_email is not None else sec_edgar.contact_email_from_secret()
    root = Path(root)
    kwargs: dict[str, Any] = {}
    if transport is not None:
        kwargs["transport"] = transport
    if sleep is not None:
        kwargs["sleep"] = sleep
    client = EdgarClient(
        contact_email=email,
        project_part=config.user_agent_project,
        cache_dir=root / CACHE_DIRNAME,
        log_path=root / RETRIEVAL_LOG_NAME,
        max_requests_per_second=config.max_requests_per_second,
        max_retries=config.max_retries,
        **kwargs,
    )
    started = utc_now_iso()
    if config.selection_file:
        path = Path(repo_root) / config.selection_file
        if not path.is_file():
            raise BuildError(f"the builder config names selection_file {config.selection_file!r}, which does not exist")
        selection = json.loads(path.read_text(encoding="utf-8"))
        selection_source = f"committed file {config.selection_file}"
    else:
        selection = select_universe(client)
        selection_source = "phase 1 (live EDGAR sources)"
    built = build_dataset(client, selection)
    retrieval = {
        "started_at": started,
        "ended_at": utc_now_iso(),
        "selection_source": selection_source,
        "user_agent_project": config.user_agent_project,
        "contact_declared": True,
        "max_requests_per_second": config.max_requests_per_second,
        "network_requests": client.network_requests,
        "cache_hits": client.cache_hits,
    }
    return write_dataset(root, selection, built, retrieval)


__all__ = ["BuildError", "EdgarError", "prepare", "parse_statement", "render_document", "extract_metric"]
