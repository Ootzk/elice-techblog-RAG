-- 수집 스냅샷
SELECT id, created_at, processor_version
FROM corpus_snapshots
ORDER BY created_at DESC;
