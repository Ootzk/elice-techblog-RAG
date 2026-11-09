from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from test_corpus import URL, article_html

from elice_knowledge.corpus import PROCESSOR_VERSION, CorpusError
from elice_knowledge.ingest import collect, read_manifest


def setup_store(previous=None):
    store = Mock()
    store.documents.return_value = previous or {}
    store.save.return_value = "created"
    store.snapshot.return_value = ("snapshot-hash", [{"url": URL}])
    return store


def test_conditional_request_reuses_validated_version():
    crawler = Mock()
    crawler.discover.return_value = [URL]
    crawler.fetch.return_value = httpx.Response(304, request=httpx.Request("GET", URL))
    store = setup_store(
        {
            URL: {
                "processor_version": PROCESSOR_VERSION,
                "etag": '"previous"',
                "content_hash": "cached",
            }
        }
    )
    result = collect(crawler, store, uuid4(), min_documents=1)
    crawler.fetch.assert_called_once_with(URL, {"If-None-Match": '"previous"'})
    store.save.assert_not_called()
    store.not_modified.assert_called_once()
    assert result["counts"]["not_modified"] == 1
    assert result["status"] == "ok"


def test_bad_language_preserves_previous_document_and_does_not_publish_snapshot():
    crawler = Mock()
    crawler.discover.return_value = [URL]
    crawler.fetch.return_value = httpx.Response(
        200, text=article_html(lang="en"), request=httpx.Request("GET", URL)
    )
    store = setup_store({URL: {"processor_version": PROCESSOR_VERSION}})
    result = collect(crawler, store, uuid4(), min_documents=1)
    assert result["documents"][0]["included"] is True
    assert result["status"] == "partial"
    store.save.assert_not_called()
    store.snapshot.assert_not_called()


def test_discovery_failure_cannot_mark_existing_documents_missing():
    crawler = Mock()
    crawler.discover.side_effect = CorpusError("incomplete listing")
    store = setup_store({URL: {"processor_version": PROCESSOR_VERSION}})
    result = collect(crawler, store, uuid4(), min_documents=1)
    assert result["status"] == "failed"
    crawler.fetch.assert_not_called()
    store.observe.assert_not_called()
    store.snapshot.assert_not_called()


def test_missing_url_is_rechecked_and_preserved_as_candidate():
    crawler = Mock()
    crawler.discover.return_value = [URL + "-new"]
    crawler.fetch.side_effect = lambda url, _: httpx.Response(
        200, text=article_html(canonical=url), request=httpx.Request("GET", url)
    )
    store = setup_store({URL: {"processor_version": PROCESSOR_VERSION}})
    result = collect(crawler, store, uuid4(), min_documents=1)
    assert next(d for d in result["documents"] if d["url"] == URL)["missing_candidate"] is True
    assert crawler.fetch.call_count == 2


def test_force_and_processor_change_bypass_http_validators():
    for force, processor in [(True, PROCESSOR_VERSION), (False, "old-parser")]:
        crawler = Mock()
        crawler.discover.return_value = [URL]
        crawler.fetch.return_value = httpx.Response(
            200, text=article_html(), request=httpx.Request("GET", URL)
        )
        store = setup_store({URL: {"processor_version": processor, "etag": '"cached"'}})
        assert collect(crawler, store, uuid4(), min_documents=1, force=force)["status"] == "ok"
        crawler.fetch.assert_called_once_with(URL, {})


def test_fixed_manifest_does_not_pull_unselected_documents_into_snapshot():
    crawler = Mock()
    crawler.fetch.return_value = httpx.Response(
        200, text=article_html(), request=httpx.Request("GET", URL)
    )
    store = setup_store({URL + "-other": {"processor_version": PROCESSOR_VERSION}})
    collect(crawler, store, uuid4(), urls=[URL], min_documents=1)
    crawler.discover.assert_not_called()
    store.snapshot.assert_called_once_with([URL])


def test_minimum_size_failure_never_publishes_a_snapshot():
    crawler = Mock()
    crawler.discover.return_value = [URL]
    store = setup_store()
    assert collect(crawler, store, uuid4())["status"] == "failed"
    store.snapshot.assert_not_called()


def test_pinned_manifest_detects_source_drift_before_replacing_existing_content():
    crawler = Mock()
    crawler.fetch.return_value = httpx.Response(
        200, text=article_html(), request=httpx.Request("GET", URL)
    )
    store = setup_store({URL: {"processor_version": PROCESSOR_VERSION}})
    result = collect(
        crawler, store, uuid4(), urls=[URL], expected_hashes={URL: "old-hash"}, min_documents=1
    )
    assert result["status"] == "partial"
    assert "pinned manifest" in result["documents"][0]["detail"]
    store.save.assert_not_called()
    store.snapshot.assert_not_called()


def test_different_parser_manifest_requires_explicit_refresh(tmp_path):
    manifest = tmp_path / "old-manifest.json"
    manifest.write_text(
        '{"processor_version":"different-parser","documents":[{"url":"' + URL + '"}]}'
    )
    with pytest.raises(CorpusError, match="processor version"):
        read_manifest(manifest)
