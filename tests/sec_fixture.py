"""Synthetic stand-in for SEC EDGAR (PR 17): responses shaped like company_tickers.json, the XBRL
frames and companyfacts APIs, the submissions API, FilingSummary.xml and the R<n>.htm statement pages.

Every company, name, number and filing here is INVENTED; nothing is copied from EDGAR. The shapes
follow the project's (unverified) understanding of EDGAR's formats described in
binding_gate.datasets.sec_build. Statements have realistic sizes (about 28 income-statement rows
with three year columns and 38 balance-sheet rows with two), so that dry-run token counts on this
fixture approximate real documents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from binding_gate.datasets import sec_edgar
from binding_gate.datasets.sec_edgar import Response

MONTH_ABBR = ("Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.", "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec.")


def short_date(d: date) -> str:
    return f"{MONTH_ABBR[d.month - 1]} {d.day:02d}, {d.year}"


@dataclass
class FakeCompany:
    cik: int
    name: str
    tickers: list[str]
    sic: int
    assets: int  # frame value (USD)
    report_date: date = date(2025, 12, 31)
    revenue: int = 0
    scale: int = 1_000_000
    revenue_concept: str = "Revenues"
    with_profit_loss: int | None = None  # a different consolidated ProfitLoss (NCI)
    restated_prior_revenue: int | None = None  # prior year as reported in THIS filing (restated)
    no_cash: bool = False
    bank: bool = False
    entity_type: str = "operating"
    filing_date: date | None = None
    extra_filings: list[tuple[str, str, str]] = field(default_factory=list)  # (form, filingDate, reportDate)

    @property
    def accession(self) -> str:
        return f"{self.cik % 10_000_000_000:010d}-26-{self.cik % 1000:06d}"

    @property
    def prior_accession(self) -> str:
        return f"{self.cik % 10_000_000_000:010d}-25-{self.cik % 1000:06d}"

    def years(self) -> list[date]:
        d = self.report_date
        return [d, d.replace(year=d.year - 1), d.replace(year=d.year - 2)]

    def values(self) -> dict[str, list[int]]:
        r = self.revenue or (self.assets // 3 // self.scale) * self.scale
        rev = [r, int(r * 0.93) // self.scale * self.scale, int(r * 0.88) // self.scale * self.scale]
        op = [int(x * 0.18) // self.scale * self.scale for x in rev]
        ni = [int(x * 0.12) // self.scale * self.scale for x in rev]
        assets = [self.assets // self.scale * self.scale, int(self.assets * 0.95) // self.scale * self.scale]
        cash = [int(a * 0.07) // self.scale * self.scale for a in assets]
        return {"revenue": rev, "operating_income": op, "net_income": ni, "total_assets": assets, "cash": cash}


def _fmt(value: int, scale: int, dollar: bool) -> str:
    shown = value // scale
    text = f"{abs(shown):,}"
    text = f"({text})" if shown < 0 else text
    return f"$ {text}" if dollar else text


IS_EXTRA = [
    "Cost of sales", "Gross margin", "Research and development", "Selling, general and administrative",
    "Restructuring charges", "Amortization of intangible assets", "Total operating expenses",
    "Interest income", "Interest expense", "Other income (expense), net", "Income before provision for income taxes",
    "Provision for income taxes", "Equity in earnings of affiliates", "Earnings per share, basic",
    "Earnings per share, diluted", "Shares used in computing earnings per share, basic",
    "Shares used in computing earnings per share, diluted", "Dividends declared per share",
    "Depreciation included above", "Stock-based compensation included above", "Foreign currency effect",
    "Impairment charges", "Gain on sale of business", "Litigation settlements",
]
BS_EXTRA = [
    "Short-term investments", "Accounts receivable, net", "Inventories", "Prepaid expenses", "Other current assets",
    "Total current assets", "Property, plant and equipment, net", "Operating lease right-of-use assets",
    "Goodwill", "Intangible assets, net", "Deferred tax assets", "Other non-current assets",
    "Accounts payable", "Accrued liabilities", "Short-term debt", "Current portion of long-term debt",
    "Deferred revenue", "Total current liabilities", "Long-term debt", "Operating lease liabilities",
    "Deferred tax liabilities", "Other non-current liabilities", "Total liabilities", "Commitments and contingencies",
    "Preferred stock", "Common stock and additional paid-in capital", "Retained earnings",
    "Accumulated other comprehensive loss", "Treasury stock", "Total shareholders' equity",
    "Noncontrolling interests", "Total equity", "Total liabilities and equity",
]


def _row(label: str, cells: list[str], concept: str = "x") -> str:
    tds = "".join(f'<td class="nump">{c}<span></span></td>' for c in cells)
    return (f'<tr class="re"><td class="pl " style="border-bottom: 0px;" valign="top"><a class="a" href="javascript:void(0);" '
            f"onclick=\"top.Show.showAR( this, 'defref_us-gaap_{concept}', window );\">{label}</a></td>{tds}</tr>")


def statement_html(company: FakeCompany, kind: str) -> str:
    v = company.values()
    sc = company.scale
    scale_word = {1_000: "Thousands", 1_000_000: "Millions", 1: ""}[sc]
    unit = f"USD ($)<br> $ in {scale_word}" if scale_word else "USD ($)"
    if kind == "income_statement":
        dates = company.years()
        header = (f'<tr><th class="tl" colspan="1" rowspan="2"><div style="width: 200px;"><strong>CONSOLIDATED STATEMENTS OF '
                  f'OPERATIONS - {unit}</strong></div></th><th class="th" colspan="3">12 Months Ended</th></tr>'
                  "<tr>" + "".join(f'<th class="th"><div>{short_date(d)}</div></th>' for d in dates) + "</tr>")
        rev = list(v["revenue"])
        if company.restated_prior_revenue is not None:
            rev[1] = company.restated_prior_revenue
        rows = [_row("Total revenues" if not company.bank else "Total interest and noninterest income", [_fmt(x, sc, True) for x in rev], company.revenue_concept)]
        rows.append('<tr class="re"><td class="pl "><strong>Operating expenses:</strong></td><td class="text">&#160;<span></span></td>'
                    '<td class="text">&#160;<span></span></td><td class="text">&#160;<span></span></td></tr>')
        for i, label in enumerate(IS_EXTRA[:8]):
            rows.append(_row(label, [_fmt(x // (i + 3) // sc * sc, sc, False) for x in rev]))
        if not company.bank:
            rows.append(_row("Operating income", [_fmt(x, sc, False) for x in v["operating_income"]], "OperatingIncomeLoss"))
        for i, label in enumerate(IS_EXTRA[8:13]):
            rows.append(_row(label, [_fmt(x // (i + 11) // sc * sc, sc, False) for x in rev]))
        if company.with_profit_loss is not None:
            rows.append(_row("Net income", [_fmt(company.with_profit_loss, sc, True), "", ""], "ProfitLoss"))
        rows.append(_row("Net income attributable to the company" if company.with_profit_loss else "Net income",
                         [_fmt(x, sc, True) for x in v["net_income"]], "NetIncomeLoss"))
        for label in IS_EXTRA[13:]:
            rows.append(_row(label, ["$ 1.23", "$ 1.11", "$ 0.98"]))
    else:
        dates = company.years()[:2]
        header = (f'<tr><th class="tl" colspan="1" rowspan="1"><div style="width: 200px;"><strong>CONSOLIDATED BALANCE SHEETS - '
                  f'{unit}</strong></div></th>' + "".join(f'<th class="th"><div>{short_date(d)}</div></th>' for d in dates) + "</tr>")
        rows = []
        if not company.no_cash:
            rows.append(_row("Cash and cash equivalents", [_fmt(x, sc, True) for x in v["cash"]], "CashAndCashEquivalentsAtCarryingValue"))
        for i, label in enumerate(BS_EXTRA[:12]):
            rows.append(_row(label, [_fmt(x // (i + 4) // sc * sc, sc, False) for x in v["total_assets"]]))
        rows.append(_row("Total assets", [_fmt(x, sc, True) for x in v["total_assets"]], "Assets"))
        for i, label in enumerate(BS_EXTRA[12:]):
            rows.append(_row(label, [_fmt(x // (i + 5) // sc * sc, sc, False) for x in v["total_assets"]]))
    footnotes = '<div style="display: none;"><table class="authRefData"><tr><td>Definition hidden</td></tr></table></div>'
    return f'<html><body><span style="display: none;">v3.25</span><table class="report" border="0" cellspacing="2">{header}{"".join(rows)}</table>{footnotes}</body></html>'


def cover_html(company: FakeCompany) -> str:
    return ('<html><body><table class="report"><tr><th class="tl" colspan="1" rowspan="2"><div><strong>Cover - USD ($)</strong></div></th>'
            '<th class="th" colspan="1">12 Months Ended</th></tr><tr><th class="th"><div>' + short_date(company.report_date) + "</div></th></tr>"
            + _row("Document Type", ["10-K"]) + _row("Entity Registrant Name", [company.name])
            + _row("Entity Central Index Key", [f"{company.cik:010d}"]) + "</table></body></html>")


def filing_summary(company: FakeCompany) -> str:
    reports = [
        (1, "Cover", "0000001 - Document - Cover", "R1.htm", "Cover"),
        (2, "CONSOLIDATED STATEMENTS OF OPERATIONS", "0000002 - Statement - CONSOLIDATED STATEMENTS OF OPERATIONS", "R2.htm", "Statements"),
        (3, "CONSOLIDATED STATEMENTS OF COMPREHENSIVE INCOME", "0000003 - Statement - CONSOLIDATED STATEMENTS OF COMPREHENSIVE INCOME", "R3.htm", "Statements"),
        (4, "CONSOLIDATED BALANCE SHEETS", "0000004 - Statement - CONSOLIDATED BALANCE SHEETS", "R4.htm", "Statements"),
        (5, "CONSOLIDATED BALANCE SHEETS (Parenthetical)", "0000005 - Statement - CONSOLIDATED BALANCE SHEETS (Parenthetical)", "R5.htm", "Statements"),
        (6, "CONSOLIDATED STATEMENTS OF CASH FLOWS", "0000006 - Statement - CONSOLIDATED STATEMENTS OF CASH FLOWS", "R6.htm", "Statements"),
    ]
    body = "".join(
        f"<Report instance=\"x.htm\"><IsDefault>false</IsDefault><HtmlFileName>{f}</HtmlFileName><LongName>{ln}</LongName>"
        f"<ShortName>{sn}</ShortName><MenuCategory>{mc}</MenuCategory><Position>{p}</Position></Report>"
        for p, sn, ln, f, mc in reports
    )
    return f'<?xml version="1.0" encoding="utf-8"?><FilingSummary><Version>3.25</Version><MyReports>{body}</MyReports></FilingSummary>'


def companyfacts(company: FakeCompany) -> dict[str, Any]:
    v = company.values()
    acc, prior = company.accession, company.prior_accession
    d0, d1, d2 = company.years()

    def dur(values: list[int], restated: int | None = None) -> list[dict[str, Any]]:
        rows = []
        for i, end in enumerate((d0, d1, d2)):
            val = restated if (restated is not None and i == 1) else values[i]
            rows.append({"start": (end - timedelta(days=364)).isoformat(), "end": end.isoformat(), "val": val, "accn": acc,
                         "fy": d0.year, "fp": "FY", "form": "10-K", "filed": (company.filing_date or date(2026, 2, 20)).isoformat()})
        # the same years as reported in last year's 10-K (prior accession): the original, unrestated values
        for i, end in enumerate((d1, d2)):
            rows.append({"start": (end - timedelta(days=364)).isoformat(), "end": end.isoformat(), "val": values[i + 1],
                         "accn": prior, "fy": d1.year, "fp": "FY", "form": "10-K", "filed": "2025-02-20"})
        return rows

    def inst(values: list[int]) -> list[dict[str, Any]]:
        return ([{"end": end.isoformat(), "val": values[i], "accn": acc, "fy": d0.year, "fp": "FY", "form": "10-K", "filed": "2026-02-20"}
                 for i, end in enumerate((d0, d1))]
                + [{"end": d1.isoformat(), "val": values[1], "accn": prior, "fy": d1.year, "fp": "FY", "form": "10-K", "filed": "2025-02-20"},
                   {"end": (d0 - timedelta(days=92)).isoformat(), "val": values[0] - company.scale, "accn": "q-" + acc, "fy": d0.year, "fp": "Q3", "form": "10-Q", "filed": "2025-11-01"}])

    us_gaap: dict[str, Any] = {
        company.revenue_concept: {"label": "Revenue", "units": {"USD": dur(v["revenue"], company.restated_prior_revenue)}},
        "NetIncomeLoss": {"label": "Net income", "units": {"USD": dur(v["net_income"])}},
        "Assets": {"label": "Assets", "units": {"USD": inst(v["total_assets"])}},
    }
    if not company.bank:
        us_gaap["OperatingIncomeLoss"] = {"label": "Operating income", "units": {"USD": dur(v["operating_income"])}}
    if not company.no_cash:
        us_gaap["CashAndCashEquivalentsAtCarryingValue"] = {"label": "Cash", "units": {"USD": inst(v["cash"])}}
    if company.with_profit_loss is not None:
        rows = dur(v["net_income"])
        rows[0] = {**rows[0], "val": company.with_profit_loss}
        us_gaap["ProfitLoss"] = {"label": "Profit", "units": {"USD": rows}}
    return {"cik": company.cik, "entityName": company.name, "facts": {"dei": {}, "us-gaap": us_gaap}}


def submissions(company: FakeCompany) -> dict[str, Any]:
    filings = [("10-K", (company.filing_date or date(2026, 2, 20)).isoformat(), company.report_date.isoformat(), company.accession),
               ("10-Q", "2025-11-01", "2025-09-30", "q-" + company.accession),
               ("10-K", "2025-02-20", company.report_date.replace(year=company.report_date.year - 1).isoformat(), company.prior_accession)]
    filings += [(f, fd, rd, f"x-{i}-{company.accession}") for i, (f, fd, rd) in enumerate(company.extra_filings)]
    return {
        "cik": f"{company.cik}", "entityType": company.entity_type, "sic": str(company.sic), "sicDescription": "Invented",
        "name": company.name.upper(), "tickers": company.tickers, "exchanges": ["Nasdaq"],
        "filings": {"recent": {
            "accessionNumber": [f[3] for f in filings], "filingDate": [f[1] for f in filings],
            "reportDate": [f[2] for f in filings], "form": [f[0] for f in filings],
            "isXBRL": [1 for _ in filings], "primaryDocument": ["doc.htm" for _ in filings],
        }, "files": []},
    }


class FakeEdgar:
    """A transport for EdgarClient answering from the invented companies; records every request."""

    def __init__(self, companies: list[FakeCompany], *, extra_frame_rows: list[dict] | None = None, fail_once: set[str] | None = None):
        self.companies = {c.cik: c for c in companies}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.extra_frame_rows = extra_frame_rows or []
        self.fail_once = set(fail_once or ())
        self.routes: dict[str, bytes] = {}
        self._build_routes()

    def _build_routes(self) -> None:
        tickers = {}
        for i, c in enumerate(sorted(self.companies.values(), key=lambda c: -c.assets)):
            for t in c.tickers:
                tickers[str(len(tickers))] = {"cik_str": c.cik, "ticker": t, "title": c.name.upper()}
        self.routes[sec_edgar.COMPANY_TICKERS_URL] = json.dumps(tickers).encode()
        frame = {"taxonomy": "us-gaap", "tag": "Assets", "ccp": "CY2024Q4I", "uom": "USD", "label": "Assets",
                 "data": [{"accn": c.prior_accession, "cik": c.cik, "entityName": c.name.upper(), "loc": "US-NY",
                           "end": "2024-12-31", "val": c.assets} for c in self.companies.values()] + self.extra_frame_rows}
        self.routes[sec_edgar.FRAMES_URL.format(taxonomy="us-gaap", tag="Assets", unit="USD", period="CY2024Q4I")] = json.dumps(frame).encode()
        for c in self.companies.values():
            self.routes[sec_edgar.SUBMISSIONS_URL.format(cik=c.cik)] = json.dumps(submissions(c)).encode()
            self.routes[sec_edgar.COMPANYFACTS_URL.format(cik=c.cik)] = json.dumps(companyfacts(c)).encode()
            pages = {"FilingSummary.xml": filing_summary(c), "R1.htm": cover_html(c),
                     "R2.htm": statement_html(c, "income_statement"), "R4.htm": statement_html(c, "balance_sheet")}
            for name, text in pages.items():
                self.routes[sec_edgar.archive_url(c.cik, c.accession, name)] = text.encode()

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> Response:
        self.requests.append((url, dict(headers)))
        if url in self.fail_once:
            self.fail_once.discard(url)
            return Response(503, b"busy", {})
        body = self.routes.get(url)
        return Response(200, body, {}) if body is not None else Response(404, b"", {})


# SIC groups: 3571/3572 (computers, sic2 35), 2834/2836 (pharma, sic2 28), 6021/6022 (banks, sic2 60),
# 5331/5311 (retail, sic2 53), 1311 (oil, sic2 13).
SPEC = [
    (1001, "Zentrix Systems Inc.", ["ZTX"], 3571), (1002, "Quorvan Computing Corp.", ["QVC1"], 3572),
    (1003, "Halbrook Devices Inc.", ["HBD"], 3571), (1004, "Ostrel Machines Corporation", ["OSTM"], 3572),
    (1005, "Pellora Therapeutics Inc.", ["PLRA"], 2834), (1006, "Vantris Pharma Corp.", ["VNTP"], 2836),
    (1007, "Kessel Biologics Inc.", ["KSB"], 2834), (1008, "Draymoor Labs Inc.", ["DRYL"], 2836),
    (1009, "First Calder Bancorp", ["FCB"], 6021), (1010, "Norwin Financial Corp.", ["NWF"], 6022),
    (1011, "Brisk Mart Inc.", ["BRSK"], 5331), (1012, "The Ellery Stores Company", ["ELS"], 5311),
    (1013, "Tamsin Energy Corp.", ["TMSN"], 1311), (1014, "Corvale Resources Inc.", ["CRVR"], 1311),
    (1015, "Lindqvist Retail Group Inc.", ["LQR"], 5331), (1016, "Abernet Bank Holding Corp.", ["ABHC"], 6021),
    (1017, "Graymont Oil Inc.", ["GRMO"], 1311), (1018, "Sundry Robotics Inc.", ["SNDR"], 3571),
    (1019, "Pheliqs Medicines Inc.", ["PHLQ"], 2834), (1020, "Westerbrook Bancshares Inc.", ["WBB"], 6022),
]


def make_companies(n_extra: int = 0) -> list[FakeCompany]:
    """The base fixture (20 companies; special cases on fixed CIKs) plus ``n_extra`` generated ones."""
    out = []
    for i, (cik, name, tickers, sic) in enumerate(SPEC):
        c = FakeCompany(cik=cik, name=name, tickers=tickers, sic=sic, assets=(90 - i) * 1_000_000_000 + cik * 1_000_000)
        if sic in (6021, 6022):
            c.bank = True
        out.append(c)
    by = {c.cik: c for c in out}
    by[1002].revenue_concept = "RevenueFromContractWithCustomerExcludingAssessedTax"  # concept variant
    by[1003].restated_prior_revenue = 41_111_000_000  # prior year restated in this 10-K
    by[1004].with_profit_loss = 1_234_000_000  # NCI: ProfitLoss differs from NetIncomeLoss
    by[1006].report_date = date(2025, 6, 30)
    by[1006].filing_date = date(2025, 8, 15)
    by[1007].scale = 1_000  # $ in Thousands
    by[1013].no_cash = True
    by[1014].report_date = date(2025, 9, 27)
    by[1014].filing_date = date(2025, 11, 14)
    for k in range(n_extra):
        sic = [3571, 2834, 5331, 1311, 4911, 7372][k % 6]
        cik = 2000 + k
        out.append(FakeCompany(cik=cik, name=f"Generated Company {k} Inc.", tickers=[f"G{k}"], sic=sic,
                               assets=(50_000 - k) * 1_000_000))
    return out
