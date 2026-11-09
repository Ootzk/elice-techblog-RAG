-- 표준 TextNode에 저장된 본문과 출처. 필요하면 WHERE s.policy_key = '정책 키'를 추가하세요.
SELECT s.policy_key, v.title, n.node->>'id_' AS chunk_id,
       n.node->'metadata'->>'heading_path' AS heading_path,
       n.node->'metadata'->>'anchor' AS anchor,
       n.node->'metadata'->>'token_count' AS token_count,
       n.node->'metadata'->>'oversized' AS oversized,
       n.node->>'text' AS body
FROM corpus_chunk_sets s
JOIN corpus_versions v ON v.id = s.version_id
CROSS JOIN LATERAL jsonb_array_elements(s.nodes) WITH ORDINALITY AS n(node, position)
ORDER BY s.policy_key, v.title, n.position
LIMIT 50;
