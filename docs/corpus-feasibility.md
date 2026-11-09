# 한국어 엘리스 테크블로그 Corpus 사전 점검

점검일: 2026-11-09 (Asia/Seoul)

## 결론

https://elice.io/ko/resources/blog?filter=tech_blog 의 현재 목록에 포함된 27개 글을 대상으로 한국어 URL에서 HTML과 본문을 모두 수집했다. 과제의 최소 20문서 조건을 충족하며, 텍스트 기반 Citation RAG의 초기 Corpus로 구현 가능하다. 검색·답변 품질은 아직 측정하지 않았다.

## 실제 확인 결과

| 항목 | 관측값 |
| --- | --- |
| 목록 페이지 | 3페이지, 12 + 12 + 3개 |
| 중복 제거한 글 | 27개 |
| 한국어 URL 접근 및 본문 추출 | 27/27 성공 |
| HTML 언어 및 canonical | 27/27 `ko`, 한국어 canonical |
| 게시일 범위 | 2021-08-30 ~ 2026-10-26 |
| 추출 본문 문자 수 합계 | 162,324자 |
| 글별 본문 문자 수 범위 | 1,780 ~ 21,549자 |
| HTML 표 | 15개 글에 총 48개 |
| 코드 블록 (`pre`) | 11개 글 |
| 본문 이미지 | 22개 글에 총 64개 |

문자 수는 `articleBody` 내부의 텍스트 노드를 줄바꿈으로 연결한 진단용 값이다. 홍보 문구 등 본문 내부의 부가 요소가 포함되며, 이미지 내부 텍스트는 포함하지 않는다. 토큰 수나 정제 완료 데이터 규모가 아니다.

## 수집 구조

- `?filter=tech_blog&page=1`부터 `page=3`까지 서버가 반환한 HTML에 목록 링크가 존재한다.
- 개별 글은 JavaScript 실행 없이 HTML에서 본문을 읽을 수 있다.
- `article [itemprop="articleBody"]`로 본문을 구분할 수 있고, 제목·게시일·canonical 및 JSON-LD의 게시/수정 시각도 확인된다.
- 소제목에 ID가 있어 문서 URL과 섹션 앵커로 인용 위치를 안내할 수 있다.
- robots.txt의 현재 내용은 `User-Agent: *`, `Allow: /`이다. 이는 콘텐츠 재배포 라이선스를 의미하지 않는다. 재현성 확보에는 공개 저장소에 원문을 복제하기보다 URL manifest와 다운로드 스크립트를 제공하는 방식을 우선 고려한다.

## 언어 전환 재현

목록의 글 링크는 `/resources/blog/{slug}` 형태이며 `/ko`가 생략되어 있다. B300 네트워크 글에서 다음을 확인했다.

| 요청 | 결과 |
| --- | --- |
| `/resources/blog/eci-b300-gpu-virtualization-2`, Accept-Language 없음 | 308 → `/en/resources/blog/eci-b300-gpu-virtualization-2` |
| 동일 URL, `Accept-Language: ko-KR,ko;q=0.9` | 308 → `/ko/resources/blog/eci-b300-gpu-virtualization-2` |
| `/ko/resources/blog/eci-b300-gpu-virtualization-2`, 영어 Accept-Language | 200, HTML `lang="ko"` 유지 |

따라서 발견한 글 경로에 `/ko`를 명시하고 한국어 Accept-Language를 함께 보낸다. 최종 URL, HTML lang, canonical 및 실제 본문을 검증하고 영어로 바뀐 응답은 인덱싱에서 제외한다. 위 테스트는 브라우저에서 일어나는 모든 언어 전환 경로를 검증한 것은 아니다.

## RAG 설계에 주는 시사점

1. 사이트의 tech_blog 분류를 초기 포함 기준으로 삼고, 글마다 포함/제외 이유를 manifest에 기록한다. 기술 글 안에도 채용·제품 홍보 문단이 있으므로 문서 선택과 본문 정제를 따로 수행한다. 추가 제외 후에도 최소 20문서가 남는지 확인한다.
2. 본문을 제목·소제목 계층으로 나누되 긴 섹션만 추가 분할한다. 표의 헤더와 행 관계, 코드 블록, 벤치마크의 조건과 단위를 보존한다. 페이지 전체를 임베딩하면 메뉴·푸터·중복 목차가 검색에 섞인다.
3. 이미지의 캡션과 주변 설명을 보존한다. 텍스트에 없는 그래프 수치는 초기 텍스트 RAG의 답변 가능 범위에서 제외하고, 이미지 해석은 별도 확장 과제로 둔다.
4. 출처에는 한국어 원문 URL, 섹션 ID, 문서/청크 ID를 사용한다. 수집 시각·게시/수정 시각·원문 해시·데이터 버전을 보관해 실험 간 Corpus를 고정한다. 재다운로드만으로 동일 내용이 보장되지는 않으므로 해시 차이를 감지한다.
5. 과거 글과 특정 실험 환경의 결과를 현재의 일반적 사실로 단정하지 않도록 답변에 글의 날짜와 실험 조건을 반영한다.

## Eval 및 개선 실험 후보

- 사실 확인: B200 한국어 모델 글의 데이터 구성·번역·필터링 방법.
- 요약·추론: B300 글에서 ATS 장애의 관찰 증상, 원인 조사, 수정 검증을 구분해 설명.
- 조건부 비교: 병렬 스토리지 글에서 워크로드에 따라 적합 구성이 달라지는 이유와 PoC의 한계 설명.
- 복수 문서 종합: 한국어 모델 구축의 데이터셋 편과 학습·평가 편을 함께 활용.
- 응답 보류: 문서에 없는 벤치마크 조건의 정확한 수치 또는 현재 가격을 묻는 질문. 실제로 전체 Corpus에 근거가 없는지 확인한 후 Gold Set에 포함.

초기 개선 가설로 고정 길이 청킹과 제목·섹션·표 구조를 보존한 청킹을 비교할 수 있다. 검색 근거 회수율과 인용 근거의 정합성을 측정하고, Gold Set에는 기대 문서/섹션 및 필수 주장과 조건을 기록한다. 동일 데이터로 반복 튜닝하지 않도록 개발용과 최종 평가용 질의를 구분한다.

## 점검 산출물

초기 조사에 사용한 임시 스크립트·응답 HTML·추출 텍스트는 개발 자료로 정리했다.
현재 수집 대상은 `corpus/manifest.json`, 재현 가능한 수집·검증 경로는 README의 Ingest 실행 절차를 따른다.
위 조사 결과는 사전 검토 기록이며 현재 Corpus 품질 평가는 별도로 수행한다.

## 대표 원문

- https://elice.io/ko/resources/blog/eci-b300-gpu-virtualization-2
- https://elice.io/ko/resources/blog/b200-korean-llm-dataset-1
- https://elice.io/ko/resources/blog/b200-korean-llm-dataset-2
- https://elice.io/ko/resources/blog/ai-storage-poc-workload-comparison
- https://elice.io/robots.txt
