-- 특정 글의 본문·메타데이터: 제목 검색어를 바꿔 실행하세요.
SELECT title, jsonb_pretty(document) AS document
FROM corpus_versions
WHERE title ILIKE '%b300%'
LIMIT 1;
