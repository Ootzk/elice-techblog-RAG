-- 문서 버전 × 정책별 청킹 결과. 정책을 바꿔도 기존 행은 유지됩니다.
SELECT s.key AS chunk_set_key, s.policy_key, s.version_id,
       v.title, jsonb_array_length(s.nodes) AS chunks, s.created_at
FROM corpus_chunk_sets s
JOIN corpus_versions v ON v.id = s.version_id
ORDER BY v.title, s.created_at DESC;
