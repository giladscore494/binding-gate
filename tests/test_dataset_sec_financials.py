"""Layer 2 (PR 17): the EDGAR client, the deterministic builder and the sec_financials adapter.

Everything runs on tests/sec_fixture.py (invented companies and EDGAR-shaped responses) with an injected
transport: no network, no key; the contact address is invented.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import sec_fixture as fx
import sec_helpers
from binding_gate import cli, registry
from binding_gate.answering import PRESENTATIONS, build_answer_request
from binding_gate.datasets import sec_build, sec_edgar
from binding_gate.datasets.sec_build import BuildError, parse_statement, render_document
from binding_gate.datasets.sec_edgar import EdgarClient, EdgarError, MissingContact, NotFound, Response
from binding_gate.datasets.sec_financials import DatasetError, SecFinancialsAdapter
from binding_gate.split import assign_split

EMAIL = sec_helpers.FAKE_EMAIL
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("sec") / "data"
    fake = fx.FakeEdgar(fx.make_companies())
    manifest = sec_build.prepare(root, contact_email=EMAIL, transport=fake, sleep=lambda s: None)
    adapter = SecFinancialsAdapter(root=str(root), data_sha256=manifest["content_sha256"])
    return root, manifest, adapter, list(adapter.iter_items()), fake


# -- fair access ------------------------------------------------------------------------ #


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_user_agent_is_project_part_plus_the_secret_email_and_is_never_stored(built):
    root, manifest, _adapter, _items, fake = built
    ua = fake.requests[0][1]["User-Agent"]
    assert ua == f"{sec_edgar.DEFAULT_PROJECT_PART} {EMAIL}"
    assert ua == f"binding-gate research project {EMAIL}" and "http" not in ua
    for path in root.rglob("*"):
        if path.is_file():
            assert EMAIL.encode() not in path.read_bytes(), path  # cache, log, manifest, items, selection
    assert manifest["retrieval"]["contact_declared"] is True and "@" not in json.dumps(manifest["retrieval"])


def test_missing_secret_refuses_before_any_request(tmp_path, monkeypatch):
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    calls = []
    with pytest.raises(MissingContact, match="SEC_CONTACT_EMAIL is not set"):
        sec_build.prepare(tmp_path / "d", transport=lambda *a: calls.append(a))
    assert calls == [] and not (tmp_path / "d").exists()


def test_secret_is_read_from_the_environment_and_validated(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "not-an-address")
    with pytest.raises(MissingContact, match="does not look like an e-mail address") as info:
        sec_build.prepare(tmp_path / "d", transport=lambda *a: None)
    assert "not-an-address" not in str(info.value)
    assert sec_edgar.CONTACT_SECRET in __import__("binding_gate.secrets").secrets.KNOWN_SECRETS


def test_cli_dataset_prepare_refuses_without_the_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    config = (REPO_ROOT / "configs" / "dev_sec_smoke.toml").read_text(encoding="utf-8")
    config = config.replace('root = "data/raw/sec-financials"', f'root = "{(tmp_path / "d").as_posix()}"')
    config = config.replace('build_config = "configs/datasets/sec_financials_v1.toml"',
                            f'build_config = "{(REPO_ROOT / "configs/datasets/sec_financials_v1.toml").as_posix()}"')
    path = tmp_path / "c.toml"
    path.write_text(config, encoding="utf-8")
    assert cli.main(["dataset-prepare", "--config", str(path)]) == cli.EXIT_FAILURE
    err = capsys.readouterr().err
    assert "SEC_CONTACT_EMAIL is not set" in err and "No address is substituted" in err
    assert not (tmp_path / "d").exists()


def test_project_part_must_not_carry_an_address_and_rate_is_capped(tmp_path):
    with pytest.raises(EdgarError, match="no e-mail address"):
        EdgarClient(contact_email=EMAIL, project_part="x me@example.org", cache_dir=tmp_path)
    with pytest.raises(EdgarError, match="fair-access limit"):
        EdgarClient(contact_email=EMAIL, cache_dir=tmp_path, max_requests_per_second=10)
    bad = tmp_path / "b.toml"
    bad.write_text('config_version = 1\nuser_agent_project = "p a@b.org"\n', encoding="utf-8")
    with pytest.raises(BuildError, match="must not contain an e-mail"):
        sec_build.BuildConfig.load(bad)


def test_rate_limit_retries_logging_cache_and_404(tmp_path):
    clock = Clock()
    url_ok, url_busy, url_missing = "https://data.sec.gov/a.json", "https://data.sec.gov/b.json", "https://data.sec.gov/c.json"
    state = {"busy": 2}

    def transport(url, headers, timeout):
        if url == url_busy and state["busy"]:
            state["busy"] -= 1
            return Response(503, b"", {})
        if url == url_missing:
            return Response(404, b"", {})
        return Response(200, b'{"ok": 1}', {})

    log = tmp_path / "log.jsonl"
    client = EdgarClient(contact_email=EMAIL, cache_dir=tmp_path / "cache", log_path=log, max_requests_per_second=5,
                         transport=transport, sleep=clock.sleep, clock=clock.time)
    assert client.get_json(url_ok) == {"ok": 1}
    assert client.get_json(url_busy) == {"ok": 1}
    with pytest.raises(NotFound):
        client.get(url_missing)
    with pytest.raises(NotFound):
        client.get(url_missing)  # the 404 is cached, not requested again
    assert client.get_json(url_ok) == {"ok": 1} and client.cache_hits == 1  # (the cached 404 raises at once)
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["status"] for e in entries] == [200, 503, 503, 200, 404]  # every attempt logged
    assert client.network_requests == 5
    assert 2.0 in clock.sleeps and 4.0 in clock.sleeps  # exponential backoff
    assert all(s >= 0 for s in clock.sleeps) and any(abs(s - 0.2) < 1e-9 for s in clock.sleeps)  # 5 per second
    assert all("User-Agent" not in e and EMAIL not in json.dumps(e) for e in entries)


def test_non_retryable_status_and_exhausted_retries(tmp_path):
    client = EdgarClient(contact_email=EMAIL, cache_dir=tmp_path, transport=lambda *a: Response(403, b"", {}), sleep=lambda s: None)
    with pytest.raises(EdgarError, match="HTTP 403"):
        client.get("https://www.sec.gov/x")
    client = EdgarClient(contact_email=EMAIL, cache_dir=tmp_path / "2", transport=lambda *a: Response(503, b"", {}),
                         sleep=lambda s: None, max_retries=2)
    with pytest.raises(EdgarError, match="3 attempt"):
        client.get("https://www.sec.gov/y")


# -- selection --------------------------------------------------------------------------- #


def test_selection_rules_and_reasons(tmp_path):
    companies = fx.make_companies()
    by = {c.cik: c for c in companies}
    by[1017].entity_type = "other"  # not an operating company
    by[1018].filing_date = date(2026, 8, 1)  # its 10-K lies outside the filing window
    by[1018].extra_filings = [("10-K/A", "2026-01-15", "2025-12-31")]  # an amendment never counts
    fake = fx.FakeEdgar(companies, extra_frame_rows=[
        {"accn": "x", "cik": 9999, "entityName": "FOREIGN", "loc": "GB", "end": "2024-12-31", "val": 10**15},
    ])
    client = EdgarClient(contact_email=EMAIL, cache_dir=tmp_path, transport=fake, sleep=lambda s: None)
    selection = sec_build.select_universe(client)
    reasons = {e["cik"]: e["reason"] for e in selection["examined"]}
    assert reasons[1017] == "not_an_operating_company" and reasons[1018] == "no_10k_in_window"
    assert 9999 not in reasons  # not in company_tickers.json (and not a US location)
    taken = [c["cik"] for c in selection["companies"]]
    ranks = [c["assets_rank"] for c in selection["companies"]]
    assert ranks == sorted(ranks) and len(taken) == len(companies) - 2
    for c in selection["companies"]:
        assert c["split"] == assign_split("sec_financials", f"sec/{c['cik']:010d}").value
        assert c["accession"] == by[c["cik"]].accession  # the original 10-K, not the 10-Q or last year's
    assert selection["quota_shortfall"]["manufacturing"] == 110 - sum(1 for c in selection["companies"] if c["sector"] == "manufacturing")


def test_choose_filing_takes_the_latest_original_10k_with_xbrl_in_the_window():
    rows = [
        {"form": "10-K", "filingDate": "2025-03-01", "reportDate": "2024-12-31", "accessionNumber": "a", "isXBRL": 1},
        {"form": "10-K", "filingDate": "2026-02-01", "reportDate": "2025-12-31", "accessionNumber": "b", "isXBRL": 1},
        {"form": "10-K/A", "filingDate": "2026-03-01", "reportDate": "2025-12-31", "accessionNumber": "c", "isXBRL": 1},
        {"form": "10-K", "filingDate": "2026-05-01", "reportDate": "2025-12-31", "accessionNumber": "d", "isXBRL": 0},
        {"form": "10-K", "filingDate": "2026-07-01", "reportDate": "2026-03-31", "accessionNumber": "e", "isXBRL": 1},
    ]
    assert sec_build.choose_filing(rows)["accessionNumber"] == "b"
    assert sec_build.choose_filing(rows[:1]) is None


def test_generation_is_deterministic_and_a_committed_selection_skips_phase_1(built, tmp_path):
    root, manifest, _adapter, _items, _fake = built
    again = sec_build.prepare(tmp_path / "again", contact_email=EMAIL, transport=fx.FakeEdgar(fx.make_companies()), sleep=lambda s: None)
    assert again["content_sha256"] == manifest["content_sha256"]
    assert (tmp_path / "again" / "items.jsonl").read_bytes() == (root / "items.jsonl").read_bytes()
    committed = tmp_path / "configs" / "selection.json"
    committed.parent.mkdir()
    committed.write_text((root / "selection.json").read_text(), encoding="utf-8")
    config = tmp_path / "builder.toml"
    config.write_text('config_version = 1\nselection_file = "configs/selection.json"\n', encoding="utf-8")
    fake = fx.FakeEdgar(fx.make_companies())
    third = sec_build.prepare(tmp_path / "third", build_config=config, contact_email=EMAIL, transport=fake,
                              sleep=lambda s: None, repo_root=tmp_path)
    assert third["content_sha256"] == manifest["content_sha256"]
    urls = [u for u, _h in fake.requests]
    assert sec_edgar.COMPANY_TICKERS_URL not in urls and not any("frames" in u or "submissions" in u for u in urls)
    assert third["retrieval"]["selection_source"].startswith("committed file")


def test_the_committed_builder_config_loads_and_holds_no_address():
    config = sec_build.BuildConfig.load(REPO_ROOT / "configs" / "datasets" / "sec_financials_v1.toml")
    assert "@" not in (REPO_ROOT / "configs" / "datasets" / "sec_financials_v1.toml").read_text()
    assert config.max_requests_per_second <= 8 and config.selection_file == "configs/datasets/sec_financials_selection_v1.json"
    assert config.user_agent_project == "binding-gate research project"


# -- statements, flattening and rendering ------------------------------------------------- #


def test_statement_flattening_keeps_row_and_column_labels_and_the_stated_scale():
    company = fx.make_companies()[0]
    table = parse_statement(fx.statement_html(company, "income_statement"))
    assert table.title == "CONSOLIDATED STATEMENTS OF OPERATIONS - USD ($) $ in Millions"
    assert table.shared_header == "12 Months Ended" and table.scale == 1_000_000
    assert table.column_labels == ["Dec. 31, 2025", "Dec. 31, 2024", "Dec. 31, 2023"]
    assert table.rows[0][0] == "Total revenues" and table.rows[0][1][0].startswith("$ ")
    assert ("Operating expenses:", ["", "", ""]) in table.rows  # a section line without values
    assert "Definition hidden" not in json.dumps(table.rows)  # the hidden definitions table is ignored
    bs = parse_statement(fx.statement_html(company, "balance_sheet"))
    assert bs.shared_header == "" and bs.column_labels == ["Dec. 31, 2025", "Dec. 31, 2024"]
    thousands = fx.make_companies()[6]
    assert parse_statement(fx.statement_html(thousands, "income_statement")).scale == 1_000


def test_header_grid_with_rowspan_and_colspan():
    html = ('<table class="report"><tr><th rowspan="2">TITLE - USD ($) $ in Thousands</th><th colspan="2">3 Months Ended</th>'
            '<th colspan="1">12 Months Ended</th></tr><tr><th>Mar. 31, 2025</th><th>Mar. 31, 2024</th><th>Dec. 31, 2024</th></tr>'
            '<tr><td>Revenue</td><td>$ 1</td><td>2</td><td>(3)</td></tr></table>')
    table = parse_statement(html)
    assert table.column_labels == ["3 Months Ended Mar. 31, 2025", "3 Months Ended Mar. 31, 2024", "12 Months Ended Dec. 31, 2024"]
    assert table.scale == 1_000 and table.rows == [("Revenue", ["$ 1", "2", "(3)"])]
    with pytest.raises(BuildError):
        parse_statement("<p>no table</p>")


@pytest.mark.parametrize(("text", "value"), [("$ 391,035", 391035), ("(1,234)", -1234), ("$ (12)", -12), ("1.23", "1.23"),
                                             ("", None), ("—", None), ("12 [1]", 12), ("abc", None)])
def test_cell_values(text, value):
    shown = sec_build.cell_value(text)
    assert (shown is None and value is None) or str(shown) == str(value)


def test_registrant_name_comes_from_the_cover_page():
    company = fx.make_companies()[11]
    assert sec_build.registrant_name(parse_statement(fx.cover_html(company))) == "The Ellery Stores Company"


def test_classify_reports_picks_operations_over_comprehensive_and_skips_parentheticals():
    reports = sec_build.parse_filing_summary(fx.filing_summary(fx.make_companies()[0]).encode())
    chosen = sec_build.classify_reports(reports)
    assert {k: v.html_file for k, v in chosen.items()} == {"cover": "R1.htm", "income_statement": "R2.htm", "balance_sheet": "R4.htm"}


def test_render_document_layout_and_size_budget_with_marker():
    company = fx.make_companies()[0]
    statements = {"income_statement": parse_statement(fx.statement_html(company, "income_statement")),
                  "balance_sheet": parse_statement(fx.statement_html(company, "balance_sheet"))}
    rendered = render_document("Zentrix Systems Inc.", "2025-12-31", statements)
    lines = rendered.text.splitlines()
    assert lines[0] == "Zentrix Systems Inc.: annual report on Form 10-K for the fiscal year ended December 31, 2025"
    assert lines[1] == "Zentrix Systems Inc.: CONSOLIDATED STATEMENTS OF OPERATIONS - USD ($) $ in Millions (all columns: 12 Months Ended)"
    assert lines[2].startswith("Total revenues | Dec. 31, 2025: $ ") and " | Dec. 31, 2024: $ " in lines[2]
    assert len(rendered.text) <= sec_build.DOC_CHAR_BUDGET and rendered.kept_rows == rendered.total_rows
    big = statements["balance_sheet"]
    big.rows = big.rows * 8  # far over the budget
    trimmed = render_document("Zentrix Systems Inc.", "2025-12-31", statements)
    assert len(trimmed.text) <= sec_build.DOC_CHAR_BUDGET
    assert trimmed.kept_rows["income_statement"] == trimmed.total_rows["income_statement"]  # the short one is whole
    assert trimmed.kept_rows["balance_sheet"] < trimmed.total_rows["balance_sheet"]
    assert trimmed.text.splitlines()[-1] == f"[{trimmed.total_rows['balance_sheet'] - trimmed.kept_rows['balance_sheet']} more rows not shown]"


# -- gold extraction ------------------------------------------------------------------------ #


def _company(cik: int) -> fx.FakeCompany:
    return next(c for c in fx.make_companies() if c.cik == cik)


def _gold(cik: int, metric: str, kept: int | None = None):
    company = _company(cik)
    m = sec_build.METRICS[metric]
    table = parse_statement(fx.statement_html(company, m.statement))
    return sec_build.extract_metric(m, fx.companyfacts(company), company.accession, company.report_date.isoformat(),
                                    table, len(table.rows) if kept is None else kept), company


def test_gold_is_as_reported_in_the_filing_including_a_restated_prior_year():
    gold, company = _gold(1003, "revenue")
    assert gold["available"] and gold["concept"] == "Revenues" and gold["value"] == company.values()["revenue"][0]
    prior = [a for a in gold["alternatives"] if a["end"] == "2024-12-31"]
    assert prior == [{"start": "2024-01-02", "end": "2024-12-31", "value": 41_111_000_000}]  # restated, from THIS 10-K
    assert gold["display"]["row_label"] == "Total revenues" and gold["display"]["column_label"] == "Dec. 31, 2025"


def test_concept_variant_and_instant_metrics():
    gold, _ = _gold(1002, "revenue")
    assert gold["concept"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assets, company = _gold(1002, "total_assets")
    assert assets["period"] == {"start": None, "end": "2025-12-31"} and assets["value"] == company.values()["total_assets"][0]
    assert [a["end"] for a in assets["alternatives"]] == ["2024-12-31"]  # the 10-Q instant is not from this filing


def test_unavailable_metrics_are_recorded_with_their_reason():
    nci, _ = _gold(1004, "net_income")
    assert nci == {"available": False, "reason": "profit_loss_differs_from_net_income"}
    cash, _ = _gold(1013, "cash")
    assert not cash["available"] and "no_current_fact" in cash["reason"]
    cut, _ = _gold(1001, "total_assets", kept=3)  # the row lies beyond the kept text
    assert not cut["available"] and "not_displayed_in_kept_text" in cut["reason"]
    thousands, company = _gold(1007, "revenue")
    assert thousands["available"] and thousands["display"]["scale"] == 1_000


# -- items, conditions, split ------------------------------------------------------------------ #


def test_three_conditions_per_company_with_neutral_ids_and_roles(built):
    _root, manifest, _adapter, items, _fake = built
    by_group: dict[str, list] = {}
    for item in items:
        by_group.setdefault(item.group_id, []).append(item)
    assert manifest["content"]["n_items"] == len(items) == 3 * len(by_group)
    for group, members in by_group.items():
        assert [i.stratum for i in members] == ["present/none", "present/peer", "absent/peer"]
        assert len({i.question for i in members}) == 1 and len({i.gold["metric"] for i in members}) == 1
        only, both, peer = members
        assert [d.id for d in only.documents] == ["doc1"] and only.gold["document_roles"] == {"doc1": "target"}
        assert sorted(both.gold["document_roles"].values()) == ["competitor", "target"] and [d.id for d in both.documents] == ["doc1", "doc2"]
        assert peer.gold["document_roles"] == {"doc1": "competitor"} and peer.gold["target_shown"] is False
        assert only.documents[0].text == next(d.text for d in both.documents if both.gold["document_roles"][d.id] == "target")
        assert peer.documents[0].text.startswith(peer.gold["competitor"]["name"] + ": annual report on Form 10-K")
        assert only.documents[0].text.startswith(only.gold["company"]["name"] + ": annual report on Form 10-K")
        assert only.gold["company"]["cik"] != only.gold["competitor"]["cik"]
        assert only.question.startswith(f"What w") and only.gold["company"]["name"] in only.question
        assert "in US dollars" in only.question and "as reported in its annual report on Form 10-K" in only.question
        for item in members:
            assert not any(k in json.dumps([d.metadata for d in item.documents]) for k in ("target", "competitor"))
    orders = {i.gold["document_roles"]["doc1"] for i in items if i.stratum == "present/peer"}
    assert orders == {"target", "competitor"}  # the hash order puts either first


def test_no_company_appears_in_both_splits_as_target_or_peer(built):
    _root, manifest, _adapter, items, _fake = built
    for item in items:
        target = assign_split("sec_financials", item.group_id)
        peer = assign_split("sec_financials", f"sec/{item.gold['competitor']['cik']:010d}")
        assert target == peer, item.id
    assert set(manifest["content"]["items_by_split"]) == {"dev", "test"}
    excluded = {e["cik"]: e["reason"] for e in manifest["content"]["excluded"]}
    assert set(excluded.values()) <= {"no_peer_in_split_and_sector"}


def test_peers_share_the_sic_major_group_or_sector(built):
    _root, _manifest, _adapter, items, _fake = built
    sectors = {c[0]: c for c in sec_build.SECTOR_QUOTAS}
    for item in items:
        a, b = item.gold["company"]["sic"], item.gold["competitor"]["sic"]
        assert a // 100 == b // 100 or sec_build.sector_of(a) == sec_build.sector_of(b)
        assert sec_build.sector_of(a) in sectors


def test_metric_choice_is_the_smallest_hash_among_metrics_available_for_target_and_peer():
    def stub(cik, available, peer_values):
        return sec_build.CompanyBuild(
            company={"cik": cik, "values_for_peers": {m: {"values": [1] if m in peer_values else []} for m in sec_build.METRICS}},
            name="x", document="x", metrics={m: {"available": m in available} for m in sec_build.METRICS})

    everything = set(sec_build.METRICS)
    target = stub(1, everything, everything)
    expected = min(everything, key=lambda m: sec_build.key("metric", "0000000001", m))
    assert sec_build.choose_metric(target, stub(2, everything, everything)) == expected
    peer_without = stub(2, everything, everything - {expected})
    assert sec_build.choose_metric(target, peer_without) == min(everything - {expected}, key=lambda m: sec_build.key("metric", "0000000001", m))
    assert sec_build.choose_metric(stub(1, {"cash"}, everything), stub(2, set(), set())) == "cash"  # fallback: target only
    assert sec_build.choose_metric(stub(1, set(), everything), target) is None


# -- the adapter --------------------------------------------------------------------------- #


def test_adapter_refuses_unpinned_wrong_pin_and_tampering(built, tmp_path):
    root, manifest, _adapter, _items, _fake = built
    with pytest.raises(DatasetError, match="not pinned yet"):
        SecFinancialsAdapter(root=str(root)).fingerprint()
    with pytest.raises(DatasetError, match="but the config pins"):
        SecFinancialsAdapter(root=str(root), data_sha256="0" * 64).fingerprint()
    with pytest.raises(ValueError):
        SecFinancialsAdapter(root=str(root), data_sha256="xyz")
    copy = tmp_path / "copy"
    copy.mkdir()
    for name in ("manifest.json", "items.jsonl", "selection.json"):
        (copy / name).write_bytes((root / name).read_bytes())
    pin = manifest["content_sha256"]
    assert SecFinancialsAdapter(root=str(copy), data_sha256=pin).fingerprint() == pin
    (copy / "items.jsonl").write_text((root / "items.jsonl").read_text().replace("Total revenues", "Total revenue"), encoding="utf-8")
    with pytest.raises(DatasetError, match="items_sha256"):
        list(SecFinancialsAdapter(root=str(copy), data_sha256=pin).iter_items())
    (copy / "items.jsonl").write_bytes((root / "items.jsonl").read_bytes())
    edited = json.loads((root / "manifest.json").read_text())
    edited["content"]["n_items"] += 1
    (copy / "manifest.json").write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(DatasetError, match="was edited"):
        SecFinancialsAdapter(root=str(copy), data_sha256=pin).fingerprint()
    with pytest.raises(DatasetError, match="dataset-prepare"):
        SecFinancialsAdapter(root=str(tmp_path / "missing"), data_sha256=pin).fingerprint()


def test_registration_pin_method_and_dataset_pin_output(built, capsys, tmp_path):
    assert "sec_financials" in registry.available("dataset")
    adapter = registry.create("dataset", "sec_financials")
    assert adapter.pin_method == "prepare" and adapter.commit == "build-v1:UNPINNED"
    assert cli.main(["dataset-pin", "--config", str(REPO_ROOT / "configs" / "dev_sec_baseline.toml")]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "method=prepare" in out and "root=data/raw/sec-financials" in out


def test_presentation_names_no_company_and_shows_neutral_labels(built):
    _root, _manifest, _adapter, items, _fake = built
    item = next(i for i in items if i.stratum == "present/peer")
    request = build_answer_request(item.arm_input(), "sec_filings")
    system, user = request.messages
    assert "sec_filings" in PRESENTATIONS
    assert item.gold["company"]["name"] not in system.content and "peer" not in system.content.lower()
    assert "[Document 1 — source: doc1]" in user.content and "[Document 2 — source: doc2]" in user.content
    assert user.content.index("Question: ") > user.content.index("[Document 2")
    assert "target" not in user.content.lower().replace("total", "")


def test_manifest_records_sources_parameters_and_unverified_reuse_terms(built):
    _root, manifest, _adapter, _items, _fake = built
    content = manifest["content"]
    assert content["parameters"]["seed"] == 20261122 and content["parameters"]["doc_char_budget"] == 10_000
    for filing in content["filings"]:
        assert {d["url"].rsplit("/", 1)[1] for d in filing["documents"]} >= {"FilingSummary.xml", "R2.htm", "R4.htm"}
        assert all(len(d["sha256"]) == 64 for d in filing["documents"])
    assert "UNVERIFIED" in manifest["reuse_terms"]
    assert set(content["items_by_stratum"]) == {"present/none", "present/peer", "absent/peer"}


def test_refusal_excerpt_strips_tags_and_hides_addresses():
    body = b"<html><body><h1>Your Request Originates from an Undeclared Automated Tool</h1><p>email webmaster@sec.gov or someone@example.org</p></body></html>"
    text = sec_edgar.refusal_excerpt(body, {"server": "AkamaiGHost"})
    assert "Undeclared Automated Tool" in text and "AkamaiGHost" in text
    assert "@" not in text and "<" not in text
    assert len(text) < 400
