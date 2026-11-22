"""Snapshot store (hashing, immutability, publication) and the page fetcher (robots, limits, failures)."""

from __future__ import annotations

import gzip
import json
import os

import pytest

import live_fakes as lf
from binding_gate.research.fetch import FETCH_MODEL_ID, FetchResponse, FetchSettings, PageFetcher, USER_AGENT
from binding_gate.research.snapshots import SnapshotError, SnapshotStore, sha256_bytes

# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


def test_blobs_are_content_addressed_written_once_and_read_only(tmp_path):
    store = SnapshotStore(tmp_path / "s")
    digest = store.put_blob(b"page bytes")
    assert digest == sha256_bytes(b"page bytes") and store.get_blob(digest) == b"page bytes"
    path = store.blobs / digest
    mtime = path.stat().st_mtime_ns
    assert store.put_blob(b"page bytes") == digest and path.stat().st_mtime_ns == mtime  # not rewritten
    assert not os.access(path, os.W_OK) or os.geteuid() == 0  # read-only (root ignores the mode bits)
    assert path.stat().st_mode & 0o222 == 0


def test_an_altered_blob_is_detected_on_read_on_verify_and_on_rewrite(tmp_path):
    store = SnapshotStore(tmp_path / "s")
    digest = store.put_text("original text")
    store.append({"kind": "page", "snapshot_id": "snap-1", "url": "u", "raw_sha256": digest, "text_sha256": digest})
    path = store.blobs / digest
    os.chmod(path, 0o644)
    path.write_text("altered text")
    for action in (lambda: store.get_blob(digest), store.verify, lambda: store.put_text("original text")):
        with pytest.raises(SnapshotError, match="altered"):
            action()


def test_index_is_append_only_jsonl_and_unknown_kinds_are_refused(tmp_path):
    store = SnapshotStore(tmp_path / "s")
    store.append({"kind": "robots", "origin": "https://a.test"})
    store.append({"kind": "page", "snapshot_id": "snap-x", "url": "https://a.test/"})
    assert [r["kind"] for r in store.records()] == ["robots", "page"]
    assert store.page("snap-x").url == "https://a.test/"
    with pytest.raises(SnapshotError):
        store.append({"kind": "secret"})


# --------------------------------------------------------------------------- #
# fetcher
# --------------------------------------------------------------------------- #


def fetcher(tmp_path, web, **settings):
    store = SnapshotStore(tmp_path / "snapshots")
    clock = iter(f"2026-11-22T10:00:{i:02d}+00:00" for i in range(60))
    return PageFetcher(store, FetchSettings(per_host_interval_seconds=0.0, **settings), transport=web.transport,
                       clock=lambda: next(clock)), store


def test_a_page_is_stored_with_url_time_status_type_and_both_hashes(tmp_path):
    web = lf.FakeWeb()
    f, store = fetcher(tmp_path, web)
    snap = f.fetch(lf.PAGE_A)
    r = snap.record
    assert snap.ok and r["http_status"] == 200 and r["content_type"].startswith("text/html")
    assert r["raw_sha256"] == sha256_bytes(lf.HTML_A.encode()) and store.get_blob(r["raw_sha256"]) == lf.HTML_A.encode()
    text = store.get_text(r["text_sha256"])
    assert text.startswith("Sample Games 2027\nThe Sample Games 2027 were hosted by Southport") and "var x" not in text
    assert r["fetched_at"] == "2026-11-22T10:00:00+00:00" and r["snapshot_id"].startswith("snap-")
    assert r["user_agent"] == USER_AGENT and web.requests[-1][1]["User-Agent"] == USER_AGENT
    assert [x["kind"] for x in store.records()] == ["robots", "page"]  # robots.txt checked first
    assert store.verify()["blobs_checked"] == 2


def test_robots_disallow_404_allow_and_5xx_or_unreachable_disallow(tmp_path):
    web = lf.FakeWeb()
    web.pages["https://down.test/robots.txt"] = (503, {}, b"")
    web.pages["https://down.test/p"] = (200, {"Content-Type": "text/html"}, b"<p>x</p>")
    web.fail["https://dead.test/robots.txt"] = "timeout"
    f, store = fetcher(tmp_path, web)
    assert f.fetch(lf.PAGE_BLOCKED).record["outcome"] == "robots_disallowed"
    assert f.fetch("https://down.test/p").record["outcome"] == "robots_disallowed"
    assert f.fetch("https://dead.test/p").record["outcome"] == "robots_disallowed"
    assert f.fetch(lf.PAGE_GONE).record["outcome"] == "http_error"  # robots 404 -> allowed, page 404
    assert lf.PAGE_BLOCKED not in web.page_urls_requested() and "https://down.test/p" not in web.page_urls_requested()
    outcomes = {r["origin"]: r["outcome"] for r in store.records("robots")}
    assert outcomes == {
        "https://blocked.test": "parsed",
        "https://down.test": "unreachable_disallow_all",
        "https://dead.test": "unreachable_disallow_all",
        "https://gone.test": "unavailable_allow_all",
    }


def test_robots_is_fetched_once_per_origin(tmp_path):
    web = lf.FakeWeb()
    web.pages["https://sample.test/other"] = (200, {"Content-Type": "text/plain"}, b"other page text")
    f, _store = fetcher(tmp_path, web)
    f.fetch(lf.PAGE_A)
    f.fetch("https://sample.test/other")
    assert [u for u, _h in web.requests].count("https://sample.test/robots.txt") == 1


def test_redirects_are_followed_with_robots_checked_for_each_target_and_limited(tmp_path):
    web = lf.FakeWeb()
    web.pages["https://hop.test/start"] = (301, {"Location": "https://example.test/2031"}, b"")
    web.pages["https://hop.test/to-blocked"] = (302, {"Location": "https://blocked.test/page"}, b"")
    web.pages["https://loop.test/a"] = (302, {"Location": "/a"}, b"")
    f, _store = fetcher(tmp_path, web, max_redirects=3)
    ok = f.fetch("https://hop.test/start").record
    assert ok["outcome"] == "ok" and ok["final_url"] == lf.PAGE_D and ok["redirects"] == [lf.PAGE_D]
    assert f.fetch("https://hop.test/to-blocked").record["outcome"] == "robots_disallowed"
    loop = f.fetch("https://loop.test/a").record
    assert loop["outcome"] == "too_many_redirects" and len(loop["redirects"]) == 3


def test_size_limit_truncates_and_is_recorded(tmp_path):
    web = lf.FakeWeb()
    web.pages["https://big.test/p"] = (200, {"Content-Type": "text/plain"}, b"word " * 1000)
    f, store = fetcher(tmp_path, web, max_bytes=100)
    r = f.fetch("https://big.test/p").record
    assert r["outcome"] == "ok" and r["truncated"] is True and r["raw_bytes"] == 100


def test_gzip_bodies_are_decoded(tmp_path):
    web = lf.FakeWeb()
    web.pages["https://z.test/p"] = (200, {"Content-Type": "text/plain", "Content-Encoding": "gzip"}, gzip.compress(b"zipped text"))
    web.pages["https://z.test/bad"] = (200, {"Content-Type": "text/plain", "Content-Encoding": "gzip"}, b"not gzip")
    f, store = fetcher(tmp_path, web)
    assert store.get_text(f.fetch("https://z.test/p").record["text_sha256"]) == "zipped text"
    assert f.fetch("https://z.test/bad").record["outcome"] == "decode_error"


@pytest.mark.parametrize(
    "url,page,fail,outcome",
    [
        ("ftp://x.test/a", None, None, "invalid_url"),
        ("https://t.test/p", None, "timeout", "timeout"),
        ("https://n.test/p", None, "network_error", "network_error"),
        ("https://pdf.test/p", (200, {"Content-Type": "application/pdf"}, b"%PDF"), None, "unsupported_content_type"),
        ("https://empty.test/p", (200, {"Content-Type": "text/html"}, b"<script>x</script>"), None, "empty_text"),
        ("https://srv.test/p", (500, {"Content-Type": "text/html"}, b"oops"), None, "http_error"),
    ],
)
def test_every_failure_is_one_stored_record_and_is_never_retried(tmp_path, url, page, fail, outcome):
    web = lf.FakeWeb()
    if page is not None:
        web.pages[url] = page
    if fail is not None:
        web.fail[url] = fail
    f, store = fetcher(tmp_path, web)
    snap = f.fetch(url)
    assert snap.record["outcome"] == outcome and not snap.ok and snap.record["error"]
    assert web.page_urls_requested().count(url) <= 1
    assert [r["outcome"] for r in store.records("page")] == [outcome]


def test_per_host_politeness_waits_between_requests_to_the_same_host(tmp_path):
    web = lf.FakeWeb()
    web.pages["https://sample.test/other"] = (200, {"Content-Type": "text/plain"}, b"t")
    waits = []
    now = [100.0]
    f = PageFetcher(SnapshotStore(tmp_path / "s"), FetchSettings(per_host_interval_seconds=2.0), transport=web.transport,
                    sleep=waits.append, monotonic=lambda: now[0])
    f.fetch(lf.PAGE_A)
    assert waits == [2.0]  # robots.txt goes first; page A waits 2 s (the fake clock does not move)
    f.fetch("https://sample.test/other")
    assert waits == [2.0, 4.0]  # robots.txt is cached; the next slot is 2 s after page A's
    assert FETCH_MODEL_ID == "binding-gate/fetch@1"


def test_publish_manifest_has_urls_times_and_hashes_but_no_text(tmp_path):
    web = lf.FakeWeb()
    f, store = fetcher(tmp_path, web)
    f.fetch(lf.PAGE_A)
    path = store.write_publish_manifest()
    text = path.read_text()
    manifest = json.loads(text)
    assert manifest["pages"][0]["url"] == lf.PAGE_A and manifest["pages"][0]["raw_sha256"]
    assert "Southport" not in text and "Sample Games 2027" not in text  # neither page text nor title
    assert manifest["robots"][0]["origin"] == "https://sample.test"


def test_fetch_settings_are_validated():
    with pytest.raises(ValueError, match="unknown"):
        FetchSettings.from_options({"max_size": 3})
    with pytest.raises(ValueError):
        FetchSettings.from_options({"max_bytes": 0})
    assert FetchSettings.from_options(None) == FetchSettings()


def test_transport_response_header_lookup_is_case_insensitive():
    assert FetchResponse(200, {"content-TYPE": "text/plain"}, b"").header("Content-Type") == "text/plain"
