from llama_index.core.schema import Document, TextNode
from llama_index.core.storage.docstore import SimpleDocumentStore
from test_corpus import PROSE, URL, article_html

from elice_knowledge.corpus import extract_article
from elice_knowledge.documents import VersionDocuments


def test_standard_document_keeps_structure_and_stable_version_identity():
    article = extract_article(article_html(), URL)
    document = article.to_document()
    assert isinstance(document, Document)
    assert document.metadata["source"] == article.source_metadata
    assert len(document.metadata["html_nodes"]) == len(article.nodes)
    assert document.metadata["url"] == URL
    assert document.metadata["content_hash"] == article.content_hash
    assert "raw_html" not in document.metadata
    assert "html_nodes" in document.excluded_embed_metadata_keys
    second = extract_article(article_html().replace("메뉴 오염", "변경된 메뉴"), URL).to_document()
    assert document.id_ == second.id_
    assert document.metadata["content_hash"] == second.metadata["content_hash"]


def test_pipeline_deduplicates_immutable_versions_without_models():
    registry = VersionDocuments(SimpleDocumentStore())
    assert registry.pipeline.transformations == []
    assert registry.pipeline.vector_store is None
    first = extract_article(article_html(), URL).to_document()
    assert registry.register(first) is True
    assert registry.register(first) is False
    second = extract_article(article_html(prose=PROSE + "새 결과입니다."), URL).to_document()
    assert registry.register(second) is True
    assert (
        registry.docstore.get_document(first.id_).metadata["html_nodes"]
        == first.metadata["html_nodes"]
    )
    assert registry.docstore.get_document(second.id_).text == second.text


def test_interrupted_registration_hash_cannot_hide_a_missing_document():
    docstore = SimpleDocumentStore()
    document = extract_article(article_html(), URL).to_document()
    # This is the state left if the native pipeline fails after hash registration.
    docstore.set_document_hash(document.id_, document.hash)
    assert not docstore.document_exists(document.id_)
    VersionDocuments(docstore).register(document)
    assert docstore.get_document(document.id_).text == document.text
    assert docstore.get_document_hash(document.id_) == document.hash


def test_native_nodes_have_unique_ids_and_roundtrip_standard_serialization():
    first = extract_article(article_html(), URL)
    second = extract_article(article_html(), URL)
    assert len({node.id_ for node in first.nodes}) == len(first.nodes)
    assert first.content_hash == second.content_hash
    serialized = first.to_document().metadata["html_nodes"]
    restored = [TextNode.from_dict(data) for data in serialized]
    assert [node.text for node in restored] == [node.text for node in first.nodes]
    assert [node.to_dict() for node in restored] == [node.to_dict() for node in first.nodes]
