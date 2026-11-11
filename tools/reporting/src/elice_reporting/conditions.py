"""Readable projections of recorded experiment conditions; never consult infrastructure."""


def condition_sections(snapshot):
    if not snapshot:
        return []
    if snapshot.get("schema") != "experiment-conditions-v1":
        raise ValueError("Unsupported experiment conditions schema")
    missing = "미기록"
    policy = snapshot.get("chunking", {}).get("policy", {})
    structure = snapshot.get("chunking", {}).get("implementation", {})
    corpus = snapshot.get("corpus", {})
    embedding = snapshot.get("embedding", {}).get("profile", {})
    model = embedding.get("embedding", {})
    vector = snapshot.get("vector_index", {})
    keyword = snapshot.get("keyword_search", {})
    agent = snapshot.get("agent", {})
    generation = snapshot.get("generation", {})
    runtime = snapshot.get("execution", {})
    libraries = runtime.get("libraries", {})

    def number(value, unit=""):
        return f"{value:,} {unit}".strip() if isinstance(value, (int, float)) else missing

    def flag(value):
        return "보존" if value is True else "미보존" if value is False else missing

    indexes = vector.get("sql_indexes")
    ann = [
        name
        for word, name in (("hnsw", "HNSW"), ("ivfflat", "IVFFlat"))
        if any(word in index.get("indexdef", "").lower() for index in indexes or [])
    ]
    layout = missing if indexes is None else " / ".join(ann) or "정확 검색 · ANN 인덱스 없음"
    return [
        (
            "Corpus·청킹",
            [
                (
                    "문서 / 청크 수",
                    f"{number(corpus.get('documents'), '개')} / "
                    f"{number(corpus.get('chunks'), '개')}",
                ),
                (
                    "섹션 기준",
                    {
                        "h2": "H2 · 하위 제목 포함",
                        "every_heading": "각 제목 단계",
                        "page": "문서 전체",
                    }.get(policy.get("profile"), missing),
                ),
                (
                    "긴 섹션 처리",
                    {"sentence": "문장 기반 추가 분할", "none": "추가 분할 없음"}.get(
                        policy.get("overflow"), missing
                    ),
                ),
                (
                    "청크 상한 / overlap",
                    f"{number(policy.get('chunk_size'), '토큰')} / "
                    f"{number(policy.get('chunk_overlap'), '토큰')}",
                ),
                ("토크나이저", policy.get("tokenizer", missing)),
                (
                    "표 / 코드",
                    f"{flag(structure.get('preserve_tables'))} / "
                    f"{flag(structure.get('preserve_code'))}",
                ),
                ("청커 버전", policy.get("chunker_version", missing)),
            ],
        ),
        (
            "임베딩·검색 인덱스",
            [
                ("임베딩 모델", model.get("model", missing)),
                ("임베딩 차원", number(model.get("dimensions"))),
                ("임베딩 제공 방식", model.get("backend", missing)),
                ("정규화", model.get("normalization", missing)),
                ("벡터 저장소 / 검색 방식", f"{vector.get('backend', missing)} · {layout}"),
                ("거리 함수", embedding.get("distance", missing)),
                (
                    "PostgreSQL / pgvector",
                    f"{vector.get('postgres_version', missing)} / "
                    f"{vector.get('pgvector_version') or missing}",
                ),
            ],
        ),
        (
            "키워드 검색·Agent",
            [
                ("키워드 검색", f"BM25 · {keyword.get('method', missing)}"),
                ("BM25 k1 / b", f"{keyword.get('k1', missing)} / {keyword.get('b', missing)}"),
                ("한국어 분석기", f"Kiwi {libraries.get('kiwipiepy', missing)}"),
                (
                    "사용자 사전",
                    number(len(keyword["user_words"]), "개")
                    if "user_words" in keyword
                    else missing,
                ),
                (
                    "Agent / 계획 방식",
                    f"{agent.get('framework', missing)} · {agent.get('planner_protocol', missing)}",
                ),
                (
                    "검색 도구",
                    " / ".join(
                        {"keyword_search": "키워드 검색", "meaning_search": "의미 검색"}.get(t, t)
                        for t in agent.get("tools", [])
                    )
                    or missing,
                ),
                ("검색 미리보기", number(agent.get("observation_preview_chars"), "자")),
            ],
        ),
        (
            "생성·실행 환경",
            [
                (
                    "생성 API",
                    f"{generation.get('provider', missing)} · {generation.get('api', missing)}",
                ),
                (
                    "Seed",
                    "미지정"
                    if "seed" in generation and generation["seed"] is None
                    else generation.get("seed", missing),
                ),
                (
                    "top_p",
                    "공급자 기본값"
                    if "top_p" in generation and generation["top_p"] is None
                    else generation.get("top_p", missing),
                ),
                (
                    "앱 실행 환경",
                    f"{runtime.get('platform', missing)} · "
                    f"{runtime.get('architecture', missing)} · "
                    f"Python {runtime.get('python', missing)}",
                ),
                ("Worker 동시성", number(runtime.get("worker_concurrency"))),
                (
                    "LlamaIndex / LangGraph",
                    f"{libraries.get('llama-index-core', missing)} / "
                    f"{libraries.get('langgraph', missing)}",
                ),
            ],
        ),
    ]
