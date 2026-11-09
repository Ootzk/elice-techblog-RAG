"""BM25 keyword retrieval over the same snapshot/policy chunks as dense search."""

import re
import threading
from collections import OrderedDict
from importlib.metadata import version

from llama_index.core.schema import MetadataMode

from elice_knowledge.corpus import digest
from elice_knowledge.retrieval import OpenIndex, search_output

BM25_VERSION = "elice-bm25-v1"
BM25_PARAMETERS = {"method": "lucene", "k1": 1.5, "b": 0.75}
# Kiwi tags dropped from the term stream: particles (J), endings (E), affixes
# (XP/XS), copulas (VCP/VCN), punctuation and symbols. Foreign words (SL),
# hanja (SH) and numbers (SN) remain searchable.
_DROPPED_TAGS = ("J", "E", "XP", "XS", "VCP", "VCN", "SF", "SP", "SS", "SE", "SO", "SW", "SB")
# Domain proper nouns Kiwi otherwise splits (for example "리 스클라" + "우드").
# Compounds also emit their parts so "엘리스 클라우드" still matches.
USER_WORDS = {
    "엘리스": (),
    "엘리스클라우드": ("엘리스", "클라우드"),
    "런박스": (),
    "헬피": (),
}
_MIXED_WORD = re.compile(r"[0-9a-z][0-9a-z.+#_-]*[0-9a-z+#]|[0-9a-z]")
_CACHE_LIMIT = 8
_cache: OrderedDict[tuple[str, str], object] = OrderedDict()
_cache_lock = threading.Lock()
_kiwi = None
_kiwi_lock = threading.Lock()


def tokenizer_identity() -> str:
    return (
        f"{BM25_VERSION}:kiwipiepy-{version('kiwipiepy')}"
        f"+model-{version('kiwipiepy_model')}:bm25s-{version('bm25s')}"
    )


def bm25_profile() -> dict:
    return {
        "tokenizer": tokenizer_identity(),
        "user_words": USER_WORDS,
        **BM25_PARAMETERS,
        "document_input": "EMBED",
    }


def _analyzer():
    global _kiwi
    with _kiwi_lock:
        if _kiwi is None:
            from kiwipiepy import Kiwi

            # Model loading takes seconds; one shared analyzer serves all calls.
            kiwi = Kiwi()
            for word in USER_WORDS:
                kiwi.add_user_word(word, "NNP")
            _kiwi = kiwi
        return _kiwi


def tokenize(text: str) -> list[str]:
    """Korean morphemes plus whole mixed alphanumeric words such as b300 or gpt-4o.

    Term frequency is kept: duplicates are meaningful for BM25.
    """
    normalized = text.casefold()
    tokens = []
    for token in _analyzer().tokenize(normalized):
        tag = token.tag.split("-")[0]
        form = token.form.strip()
        if not form or tag.startswith(_DROPPED_TAGS):
            continue
        if len(form) < 2 or " " in form:
            continue
        tokens.append(form)
        tokens.extend(USER_WORDS.get(form, ()))
    for word in normalized.split():
        for match in _MIXED_WORD.findall(word):
            if re.search(r"[a-z]", match) and re.search(r"[0-9]", match):
                tokens.append(match)
    return tokens


def _document_text(node) -> str:
    # Same title/heading enrichment the dense index embeds.
    return node.get_content(metadata_mode=MetadataMode.EMBED)


def _bm25_index(index: OpenIndex):
    content = digest(
        {
            "index_key": index.index_key,
            "nodes": [(node.id_, node.hash) for node in index.nodes],
            "bm25": bm25_profile(),
        }
    )
    key = (index.index_key, content)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    import bm25s

    retriever = bm25s.BM25(
        method=BM25_PARAMETERS["method"], k1=BM25_PARAMETERS["k1"], b=BM25_PARAMETERS["b"]
    )
    retriever.index([tokenize(_document_text(node)) for node in index.nodes], show_progress=False)
    with _cache_lock:
        _cache[key] = retriever
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
    return retriever


def keyword_search(index: OpenIndex, query: str, top_k: int = 5) -> dict:
    if not query.strip() or not 1 <= top_k <= 50:
        raise ValueError("Require a non-empty query and top_k between 1 and 50")
    terms = tokenize(query.strip())
    if not terms or not index.nodes:
        return search_output(index, query, [])
    retriever = _bm25_index(index)
    documents, scores = retriever.retrieve(
        [terms], k=min(top_k, len(index.nodes)), show_progress=False
    )
    matches = [
        (index.nodes[int(position)], float(score))
        for position, score in zip(documents[0], scores[0], strict=True)
        # Zero means no query term occurs in the chunk; it is not evidence.
        if score > 0
    ]
    return search_output(index, query, matches)
