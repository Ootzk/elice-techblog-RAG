-- 수집 실행 이력
SELECT id, started_at, finished_at, status
FROM corpus_runs
ORDER BY started_at DESC;
