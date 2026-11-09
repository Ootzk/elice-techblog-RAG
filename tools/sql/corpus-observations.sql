-- URL별 수집 결과: 같은 URL도 수집 실행마다 이력이 남습니다.
SELECT url, status, http_status, checked_at, detail
FROM corpus_observations
ORDER BY checked_at DESC;
