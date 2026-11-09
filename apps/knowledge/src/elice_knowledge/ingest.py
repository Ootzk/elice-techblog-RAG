import json
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from elice_knowledge.corpus import PROCESSOR_VERSION, CorpusError, article_url, extract_article
from elice_knowledge.corpus_store import CorpusStore
from elice_knowledge.crawler import Crawler
from elice_knowledge.database import connect
from elice_knowledge.settings import Settings


def read_manifest(path: Path) -> dict[str, str | None]:
    data = json.loads(path.read_text())
    entries = data.get("documents", [])
    if not entries:
        raise CorpusError("Source manifest has no documents")
    processor = data.get("processor_version")
    if processor is not None and processor != PROCESSOR_VERSION:
        raise CorpusError("Manifest processor version differs from this parser")
    return {
        article_url(entry["url"]): entry.get("content_hash")
        for entry in entries
        if entry.get("included", True)
    }


def collect(
    crawler: Crawler,
    store: CorpusStore,
    run_id,
    *,
    urls=None,
    expected_hashes=None,
    force=False,
    max_pages=100,
    min_documents=20,
    progress=None,
) -> dict:
    report = {
        "run_id": str(run_id),
        "processor_version": PROCESSOR_VERSION,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "failed",
        "documents": [],
        "snapshot_id": None,
    }
    old = store.documents()
    try:
        crawler.load_robots()
        discovered = crawler.discover(max_pages=max_pages) if urls is None else urls
        if len(discovered) < min_documents:
            raise CorpusError("Source list is below min_documents; refusing partial publication")
    except Exception as exc:
        report["error"] = str(exc) if isinstance(exc, CorpusError) else type(exc).__name__
        return report

    missing = set(old) - set(discovered) if urls is None else set()
    targets = sorted(set(discovered) | missing)
    # Recheck known URLs even if absent from the listing. Absence is only a candidate,
    # never a deletion instruction. No versions or active content are removed here.
    for url in targets:
        response = None
        entry = {
            "url": url,
            "in_listing": url in discovered if urls is None else None,
            "missing_candidate": url in missing,
            "included": url in old,
        }
        try:
            previous = old.get(url)
            headers = {}
            if previous and not force and previous["processor_version"] == PROCESSOR_VERSION:
                if previous.get("etag"):
                    headers["If-None-Match"] = previous["etag"]
                if previous.get("last_modified"):
                    headers["If-Modified-Since"] = previous["last_modified"]
            response = crawler.fetch(url, headers)
            if response.status_code == 304:
                if not headers or previous is None:
                    raise CorpusError("Received 304 without a compatible cached version")
                if (expected_hashes or {}).get(url) not in {None, previous["content_hash"]}:
                    raise CorpusError("Cached content differs from the pinned manifest")
                store.not_modified(url, response.headers)
                status, raw_hash = "not_modified", None
            else:
                article = extract_article(response.text, str(response.url))
                # A source redirect must not silently replace another document identity.
                if article.url != url:
                    raise CorpusError("Article redirected to a different document")
                if (expected_hashes or {}).get(url) not in {None, article.content_hash}:
                    raise CorpusError("Source content differs from the pinned manifest")
                status, raw_hash = store.save(article, response.headers), article.raw_hash
            entry.update(status=status, included=True, http_status=response.status_code)
            store.observe(run_id, url, status, response.status_code, raw_hash)
        except Exception as exc:
            status_code = response.status_code if response is not None else None
            if isinstance(exc, httpx.HTTPStatusError):
                status_code = exc.response.status_code
            detail = str(exc) if isinstance(exc, CorpusError) else type(exc).__name__
            entry.update(status="failed", http_status=status_code, detail=detail)
            store.observe(run_id, url, "failed", status_code, detail=detail)
        report["documents"].append(entry)
        if progress:
            progress(entry)

    report["counts"] = {
        status: sum(item["status"] == status for item in report["documents"])
        for status in ("created", "updated", "unchanged", "not_modified", "failed")
    }
    if report["counts"]["failed"] == 0:
        report["snapshot_id"], report["manifest"] = store.snapshot(targets)
        report["status"] = "ok"
    else:
        report["status"] = "partial"
    return report


def run_ingest(settings: Settings, *, manifest=None, report_path=None, force=False) -> dict:
    run_id = uuid4()
    store = CorpusStore(settings)
    # A session lock serializes collectors, but no DB transaction spans HTTP calls.
    with closing(store), connect(settings) as lock:
        lock.autocommit = True
        if not lock.execute("SELECT pg_try_advisory_lock(761912183)").fetchone()[0]:
            raise CorpusError("Another ingest is already running")
        expected_hashes = read_manifest(manifest) if manifest else None
        urls = sorted(expected_hashes) if expected_hashes is not None else None
        path = report_path or settings.ingest_report_dir / f"{run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        store.start_run(run_id)
        try:
            with httpx.Client(timeout=settings.crawl_timeout_seconds) as client:
                report = collect(
                    Crawler(client, settings.crawl_interval_seconds, settings.crawl_retries),
                    store,
                    run_id,
                    urls=urls,
                    expected_hashes=expected_hashes,
                    force=force,
                    max_pages=settings.crawl_max_pages,
                    min_documents=settings.corpus_min_documents,
                    progress=lambda entry: print(
                        json.dumps(entry, ensure_ascii=False), file=sys.stderr
                    ),
                )
        except Exception as exc:
            report = {"run_id": str(run_id), "status": "failed", "error": type(exc).__name__}
        report["finished_at"] = datetime.now(UTC).isoformat()
        store.finish_run(run_id, report)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)
    return {
        "status": report["status"],
        "run_id": str(run_id),
        "report_path": str(path),
        "snapshot_id": report.get("snapshot_id"),
        "counts": report.get("counts", {}),
    }
