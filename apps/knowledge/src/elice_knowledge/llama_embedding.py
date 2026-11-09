"""LlamaIndex adapter for the shared, validated embedding backend."""

import asyncio

from llama_index.core.base.embeddings.base import BaseEmbedding
from pydantic import PrivateAttr

from elice_knowledge.embedding import Embedder


class KnowledgeEmbedding(BaseEmbedding):
    _backend: Embedder = PrivateAttr()

    def __init__(self, backend: Embedder, *, batch_size=16):
        super().__init__(model_name="knowledge-embedding", embed_batch_size=batch_size)
        self._backend = backend

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._backend.embed([query])[0]

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._backend.embed([text])[0]

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return self._backend.embed(texts)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return await asyncio.to_thread(self._get_query_embedding, query)
