"""Immutable document versions and corpus snapshots in PostgreSQL."""

import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from llama_index.core.schema import Document
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from elice_knowledge.corpus import PROCESSOR_VERSION, Article, digest
from elice_knowledge.database import connect
from elice_knowledge.documents import VersionDocuments
from elice_knowledge.settings import Settings


def snapshot_identity(manifest: list[dict]) -> str:
    # Source HTML can change page chrome while the extracted document version stays
    # identical. Logical corpus identity must also survive a fresh database rebuild.
    return digest(
        sorted(
            [{"url": e["url"], "version_id": e["version_id"]} for e in manifest],
            key=lambda entry: entry["url"],
        )
    )


def initialize_corpus(conn):
    statements = [
        """CREATE TABLE IF NOT EXISTS corpus_runs (
            id uuid PRIMARY KEY, started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz, status text NOT NULL,
            processor_version text NOT NULL, report jsonb
        )""",
        """CREATE TABLE IF NOT EXISTS corpus_documents (
            id uuid PRIMARY KEY, url text UNIQUE NOT NULL,
            current_version_id uuid, etag text, last_modified text,
            checked_at timestamptz, fetched_at timestamptz
        )""",
        """CREATE TABLE IF NOT EXISTS corpus_versions (
            id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES corpus_documents(id),
            title text NOT NULL, published_at text, modified_at text,
            content_hash text NOT NULL, raw_hash text NOT NULL,
            processor_version text NOT NULL, document jsonb NOT NULL, raw_html text NOT NULL,
            collected_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(document_id, content_hash)
        )""",
        """CREATE TABLE IF NOT EXISTS corpus_observations (
            run_id uuid NOT NULL REFERENCES corpus_runs(id), url text NOT NULL,
            checked_at timestamptz NOT NULL DEFAULT now(), status text NOT NULL,
            http_status integer, raw_hash text, detail text,
            PRIMARY KEY(run_id, url)
        )""",
        """CREATE TABLE IF NOT EXISTS corpus_snapshots (
            id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
            processor_version text NOT NULL, manifest jsonb NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS corpus_snapshot_documents (
            snapshot_id text REFERENCES corpus_snapshots(id),
            version_id uuid REFERENCES corpus_versions(id), PRIMARY KEY(snapshot_id, version_id)
        )""",
    ]
    for statement in statements:
        conn.execute(statement)


class CorpusStore:
    def __init__(self, settings: Settings, version_documents: VersionDocuments | None = None):
        self.settings = settings
        self.version_documents = version_documents or VersionDocuments.postgres(settings)

    def close(self):
        self.version_documents.close()

    def start_run(self, run_id):
        with connect(self.settings) as conn:
            conn.execute(
                "INSERT INTO corpus_runs(id, status, processor_version) VALUES (%s, 'running', %s)",
                (run_id, PROCESSOR_VERSION),
            )

    def documents(self) -> dict[str, dict]:
        with connect(self.settings) as conn:
            conn.row_factory = dict_row
            rows = conn.execute("""
                SELECT d.*, v.content_hash, v.processor_version
                FROM corpus_documents d
                JOIN corpus_versions v ON v.id = d.current_version_id ORDER BY d.url
            """).fetchall()
        return {row["url"]: row for row in rows}

    def save(self, article: Article, headers) -> str:
        document_id = uuid5(NAMESPACE_URL, article.url)
        version_id = uuid5(document_id, article.content_hash)
        document = article.to_document()
        with connect(self.settings) as conn:
            previous = conn.execute(
                "SELECT current_version_id FROM corpus_documents WHERE id = %s", (document_id,)
            ).fetchone()
            conn.execute(
                "INSERT INTO corpus_documents(id,url) VALUES (%s,%s) ON CONFLICT(id) DO NOTHING",
                (document_id, article.url),
            )
            conn.execute(
                """
                INSERT INTO corpus_versions
                (id, document_id, title, published_at, modified_at, content_hash,
                 raw_hash, processor_version, document, raw_html)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING
                """,
                (
                    version_id,
                    document_id,
                    article.title,
                    article.published_at,
                    article.modified_at,
                    article.content_hash,
                    article.raw_hash,
                    PROCESSOR_VERSION,
                    Jsonb(document.to_dict()),
                    article.raw_html,
                ),
            )
            # Native parser IDs are allocated on extraction. Reuse the first archived
            # representation of this content version, including its relationships.
            archived = conn.execute(
                "SELECT document FROM corpus_versions WHERE id=%s", (version_id,)
            ).fetchone()[0]
            document = Document.from_dict(archived)
            document.metadata = json.loads(
                json.dumps(document.metadata, ensure_ascii=False, sort_keys=True)
            )
            conn.execute(
                """
                UPDATE corpus_documents SET current_version_id=%s, etag=%s, last_modified=%s,
                    checked_at=now(), fetched_at=now() WHERE id=%s
                """,
                (version_id, headers.get("etag"), headers.get("last-modified"), document_id),
            )
        self.version_documents.register(document)
        if previous is None:
            return "created"
        return "unchanged" if previous[0] == version_id else "updated"

    def not_modified(self, url: str, headers):
        # A 304 must also repair registrations missing after a process/DB failure.
        self.version_documents.register(self.current_document(url))
        with connect(self.settings) as conn:
            conn.execute(
                """
                UPDATE corpus_documents SET checked_at=now(), etag=COALESCE(%s,etag),
                    last_modified=COALESCE(%s,last_modified) WHERE url=%s
                """,
                (headers.get("etag"), headers.get("last-modified"), url),
            )

    def current_document(self, url: str) -> Document:
        with connect(self.settings) as conn:
            conn.row_factory = dict_row
            row = conn.execute(
                """
                SELECT v.*, d.url FROM corpus_versions v
                JOIN corpus_documents d ON d.current_version_id=v.id WHERE d.url=%s
            """,
                (url,),
            ).fetchone()
            if row is None:
                raise ValueError("No archived version for URL")
            stored = Document.from_dict(row["document"])
            stored.metadata = json.loads(
                json.dumps(stored.metadata, ensure_ascii=False, sort_keys=True)
            )
        return stored

    def observe(self, run_id, url, status, http_status=None, raw_hash=None, detail=None):
        with connect(self.settings) as conn:
            conn.execute(
                """
                INSERT INTO corpus_observations
                    (run_id,url,status,http_status,raw_hash,detail) VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (run_id, url, status, http_status, raw_hash, detail),
            )

    def snapshot(self, urls: list[str]) -> tuple[str, list[dict]]:
        # Same transaction fixes the version set and snapshot links together.
        with connect(self.settings) as conn:
            rows = conn.execute(
                """
                SELECT d.url, d.id, v.id, v.content_hash, v.raw_hash, v.processor_version
                FROM corpus_documents d JOIN corpus_versions v ON v.id=d.current_version_id
                WHERE d.url = ANY(%s)
                ORDER BY d.url
            """,
                (urls,),
            ).fetchall()
            manifest = [
                {
                    "url": r[0],
                    "document_id": str(r[1]),
                    "version_id": str(r[2]),
                    "content_hash": r[3],
                    "raw_hash": r[4],
                    "processor_version": r[5],
                    "included": True,
                    "reason": "Korean tech_blog; last validated version",
                }
                for r in rows
            ]
            snapshot_id = snapshot_identity(manifest)
            conn.execute(
                """
                INSERT INTO corpus_snapshots(id,processor_version,manifest) VALUES (%s,%s,%s)
                ON CONFLICT(id) DO NOTHING
                """,
                (snapshot_id, PROCESSOR_VERSION, Jsonb(manifest)),
            )
            for entry in manifest:
                conn.execute(
                    """
                    INSERT INTO corpus_snapshot_documents(snapshot_id,version_id) VALUES (%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (snapshot_id, entry["version_id"]),
                )
        return snapshot_id, manifest

    def finish_run(self, run_id, report):
        with connect(self.settings) as conn:
            conn.execute(
                """
                UPDATE corpus_runs SET status=%s,finished_at=%s,report=%s WHERE id=%s
                """,
                (report["status"], datetime.now(UTC), Jsonb(report), run_id),
            )
