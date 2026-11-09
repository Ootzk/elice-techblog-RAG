-- 정책 키에 해당하는 설정과 저장된 문서별 결과 수
SELECT p.key AS policy_key, p.config, p.created_at,
       count(s.key) AS document_results,
       coalesce(sum(jsonb_array_length(s.nodes)), 0) AS chunks
FROM corpus_chunk_policies p
LEFT JOIN corpus_chunk_sets s ON s.policy_key = p.key
GROUP BY p.key
ORDER BY p.created_at DESC;
