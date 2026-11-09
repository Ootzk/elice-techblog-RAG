-- 현재 저장된 글 목록과 본문 미리보기
SELECT d.url, v.title, v.collected_at,
       left(v.document->>'text', 200) AS preview
FROM corpus_documents d
JOIN corpus_versions v ON v.id = d.current_version_id
ORDER BY v.collected_at DESC;
