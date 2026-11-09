"""LlamaIndex document representation and PostgreSQL registration.

The corpus archive owns immutable history. The LlamaIndex docstore is a derived,
repairable registration of those versioned documents for subsequent RAG stages.
"""

import json
from collections.abc import Callable
from uuid import NAMESPACE_URL, uuid5

from llama_index.core.ingestion import DocstoreStrategy, IngestionPipeline
from llama_index.core.schema import Document
from llama_index.core.storage.docstore.types import BaseDocumentStore
from llama_index.storage.docstore.postgres import PostgresDocumentStore
from llama_index.storage.kvstore.postgres import PostgresKVStore
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import create_engine

from elice_knowledge.database import sqlalchemy_url
from elice_knowledge.settings import Settings


def article_document(
    *,
    url,
    title,
    published_at,
    modified_at,
    nodes,
    source_metadata,
    content_hash,
    processor_version,
) -> Document:
    document_id = uuid5(NAMESPACE_URL, url)
    version_id = uuid5(document_id, content_hash)
    metadata = {
        "url": url,
        "title": title,
        "published_at": published_at,
        "modified_at": modified_at,
        "html_nodes": [node.to_dict() for node in nodes],
        "source": source_metadata,
        "content_hash": content_hash,
        "processor_version": processor_version,
    }
    # Stable nested metadata order also makes native LlamaIndex hashes reproducible
    # when restoring the archive from JSONB, which does not retain object key order.
    metadata = json.loads(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    return Document(
        id_=str(version_id),
        text="\n\n".join(node.text for node in nodes),
        metadata=metadata,
        excluded_embed_metadata_keys=["html_nodes", "source", "content_hash", "processor_version"],
        excluded_llm_metadata_keys=["html_nodes", "source", "content_hash", "processor_version"],
    )


class VersionDocuments:
    def __init__(self, docstore: BaseDocumentStore, close: Callable | None = None):
        self.docstore = docstore
        self._close = close
        # Explicitly empty: default transformations may select models. This stage
        # registers whole documents and performs no chunking or inference.
        self.pipeline = IngestionPipeline(
            transformations=[],
            docstore=docstore,
            docstore_strategy=DocstoreStrategy.DUPLICATES_ONLY,
            disable_cache=True,
        )

    @classmethod
    def postgres(cls, settings: Settings, schema_name="public"):
        if settings.database_url is None:
            raise ValueError("DATABASE_URL is required")
        params = conninfo_to_dict(settings.database_url.get_secret_value())
        url = sqlalchemy_url(settings)
        engine = create_engine(
            url,
            connect_args={
                "connect_timeout": 5,
                "options": params.get("options", "")
                + " -c statement_timeout=5000 -c lock_timeout=3000",
            },
            pool_pre_ping=True,
        )
        # Construct the supported KV backend directly. The installed docstore's
        # from_uri convenience method forwards an unsupported keyword to KVStore.
        kv = PostgresKVStore(
            table_name="elice_documents",
            schema_name=schema_name,
            engine=engine,
            async_connection_string=url.set(drivername="postgresql+asyncpg").render_as_string(
                hide_password=False
            ),
        )
        return cls(PostgresDocumentStore(kv, namespace="elice_corpus_versions"), engine.dispose)

    def register(self, document: Document) -> bool:
        stored_hash = self.docstore.get_document_hash(document.id_)
        exists = self.docstore.document_exists(document.id_)
        # IngestionPipeline writes its dedup hash before it stores document text.
        # Repair an interrupted registration so a cached hash cannot hide missing data.
        if stored_hash is not None and (not exists or stored_hash != document.hash):
            self.docstore.add_documents([document], allow_update=True)
        added = self.pipeline.run(documents=[document])
        if not self.docstore.document_exists(document.id_):
            raise RuntimeError("Document registration did not persist its text")
        return bool(added)

    def close(self):
        if self._close:
            self._close()
