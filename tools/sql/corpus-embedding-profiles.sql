-- 모델·입력 형식별 프로필과 LlamaIndex가 벡터를 저장하는 실제 테이블
SELECT key AS profile_key, table_name, profile, created_at
FROM corpus_embedding_profiles
ORDER BY created_at DESC;
