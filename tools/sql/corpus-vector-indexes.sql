-- 전체 청크의 임베딩이 완료되어 검색 가능한 인덱스만 등록됩니다.
SELECT i.key AS index_key, i.snapshot_id, i.policy_key, i.profile_key,
       p.profile->'embedding'->>'model' AS model,
       p.profile->'embedding'->>'dimensions' AS dimensions,
       i.chunk_count, i.created_at
FROM corpus_vector_indexes i
JOIN corpus_embedding_profiles p ON p.key = i.profile_key
ORDER BY i.created_at DESC;
