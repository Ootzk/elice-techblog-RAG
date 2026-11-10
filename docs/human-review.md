# Human Review

Human Review는 MLflow evaluation run의 고정 답변에 사람이 평가를 추가하는 기능이다.
`/review`에서 현재 로그인한 코드로만 검수한다. 검수자를 드롭다운으로 고르거나 다른 사람에게 배정하지 않는다.
여러 사람이 참여하려면 각자 다른 코드로 로그인한다. 같은 코드를 공유하면 같은 사람으로 집계된다.

## 평가 실행과 내 검수 회차

- **평가 실행**: RAG가 한 번 생성한 질문별 답변·근거 묶음이다. 여러 사람·회차가 같은 묶음을 검수할 수 있다.
- **내 검수 회차**: 현재 로그인한 사람이 그 묶음 전체를 한 번 채점하는 작업이다.
- **내 검수 N회 완료**: 생성 실패 문항을 제외한 모든 질문을 제출한 본인의 회차 수다. 일부만 제출한 회차는 진행 중으로 표시한다.

화면의 평가 실행 목록에 데이터셋 이름·실행 시각·본인의 완료 횟수를 함께 표시한다.
평가 실행을 선택하면 본인의 회차만 표시한다. 별도의 중복된 ‘내 검수 목록’은 두지 않는다.
진행 중인 회차가 있으면 이어서 검수하며, 완료한 뒤 **새 회차로 다시 검수**로 반복 평가한다.
중복 클릭으로 진행 중인 회차를 여러 개 만들지 않는다.
새 회차에는 이전 점수·의견·초안을 채워 넣지 않는다. 이전 회차의 판정은 그 회차를 직접 열면 읽기 전용으로 확인할 수 있다.

## 실행과 검수 흐름

새 버전의 이미지와 검수 테이블을 준비한다. 기존 수집·평가 결과를 삭제하지 않는다.
저장 기준은 [평가 저장 스키마](evaluation-schema.md)의 `evaluation-v1`이다.
이전 형식의 답변·검수 기록은 목록에서 제외하고 변환하지 않는다. 원격·로컬의 옛 데이터는 자동 삭제하지 않는다.

```bash
docker compose --env-file .env.dev -f compose.yaml -f compose.dev.yaml build knowledge
docker compose --env-file .env.dev -f compose.yaml -f compose.dev.yaml run --rm --no-deps knowledge python -m elice_knowledge init-db
docker compose --env-file .env.dev -f compose.yaml -f compose.dev.yaml up -d --no-build knowledge worker
```

1. 개인 코드로 로그인하고 **사람 검수**에 들어간다. MLflow의 `QA_MLFLOW_EXPERIMENT_NAME`에서
   `activity=evaluation` 실행을 자동 조회한다. **새로고침**·검색·추가 페이지 조회를 지원한다.
2. 실행을 선택하고 **1회차 검수 시작**을 누른다. 본인만의 grading run을 원본 answers run의 자식으로 만든다. 종료된 run의 `evaluation-bundle.json`을 검증해 DB에 고정한다.
   실패·중단된 run도 유효한 bundle이 있으면 검수할 수 있으나 생성 실패 문항은 제외한다.
3. 질문·고정된 이전 대화·답변·인용을 읽고 항목별 기준·참고 자료로 판정과 이유를 작성한다.
   넓은 화면은 답변과 채점 영역을 나란히 보여주며 좁은 화면은 세로로 배치한다.
4. 입력은 서버에 초안으로 자동 저장한다. 이동 전에도 저장을 마치며, 저장에 실패하면 이동을 멈추고 오류·재시도 상태를 표시한다.
   다른 탭의 새 초안을 오래된 내용으로 덮어쓰지 않는다. 브라우저를 닫는 순간 아직 저장 중이라면 이탈 확인을 표시한다.
5. 모든 항목을 작성한 뒤 **제출하고 다음 질문**을 누른다. 초안은 수정할 수 있지만 제출한 질문은 읽기 전용이다.
   질문 번호로 자유롭게 이동하고, 재접속 시 첫 미제출 질문부터 이어서 검수할 수 있다.
6. 모든 질문을 제출하면 해당 회차가 검수 완료로 집계된다. 동기화 완료 여부와는 구분한다.
7. **MLflow 동기화**로 그 회차의 판정·통과율·N/A·오류·미채점 건수를 본인의 grading run에 기록한다.
   부분 제출은 RUNNING, 모든 질문을 제출·동기화하면 FINISHED다. 연결 실패로 run 생성이 지연돼도 로컬 회차는 보존하며 동기화 시 같은 작업을 다시 연결한다. 실패 시 DB의 판정은 보존되고 다시 시도할 수 있다.
8. 반복 평가하려면 **새 회차로 다시 검수**를 누른다. 답변이나 Judge를 다시 호출하지 않는다.

MLflow 연결이 끊겨 목록을 갱신하지 못해도 이미 시작한 본인 회차는 DB의 고정 답변으로 검수할 수 있다.
관리자는 **내 채점 run 열기 → Metrics / Artifacts → grading-result.json**에서 판정·집계를 확인한다.
**원본 답변 run 열기**는 생성 결과를 보는 별도 링크다.
외부 MLflow 화면으로 연결하는 URL과 run 상세 이름은 일반 검수자에게 반환하지 않는다.

## 판정과 블라인드 범위

| 데이터셋 | 항목 | 척도 |
| --- | --- | --- |
| Gold | 정확성·완전성·근거 충실도·응답 적절성 | 충족 / 미충족 / 해당 없음 |
| Iron | 행동 준수 | 충족 / 미충족 |

Judge의 rubric·`evaluation_row` 입력·사전 적용 조건을 공유한다.
분석 파일에는 pass/fail/not_applicable과 판정 근거를 기록하며, 적용 불가 항목은 해당 없음으로 고정한다.
사실 주장이 없는 응답의 groundedness 등 동적 N/A는 rubric에 따라 판단한다.

근거 충실도에는 최종 생성에 실제 제공한 `generation_sources` 전체를 보여주고, 답변에서 인용한 근거를 표시한다.
일부 근거만으로 모든 중요한 사실 주장이 지지되어도 충족이다. 근거를 전부 사용할 필요는 없다.
특정 번호를 인용했다면 그 번호의 근거가 해당 주장을 지지하는지도 확인한다.
모범 답안·누적 검색 후보·외부 지식으로 부족한 생성 근거를 보충하지 않는다.
필수 항목은 목록, 검증된 인용문은 원문 링크·인용 카드로 표시하며 내부 JSON은 기본 화면에 노출하지 않는다.

API에는 현재 코드·선택 회차의 판정과 초안만 포함한다. Judge의 점수·의견과 다른 사람의 판정·회차·완료 횟수는 노출하지 않는다.
실행을 가져온 시점의 답변·rubric·적용 조건·해시를 고정한다.
원본 Judge 버전과 현행 rubric이 달라졌다면 동일 기준으로 간주하지 않는다.

블라인드는 검수 UI/API의 처리 범위다. MLflow 관리자 권한으로 외부 화면을 열거나 이전 본인 회차를 직접 읽는 행동까지 막지 않는다.
반복 일관성 실험에서는 이전 평가를 보지 않고 독립적으로 채점하는 절차를 함께 지켜야 한다.
답변 본문에 포함된 모델명 등은 자동 삭제하지 않는다.

## 저장과 분석

사람과 모델이 공통으로 `evaluation_sources`, `grading_runs`, `grading_cases`, `grading_drafts`를 사용한다.
회차 할당·원격 ID 연결·manifest 검증·metric 집계는 같은 모듈을 통하며 상세 필드는 [스키마](evaluation-schema.md)를 따른다.
초안은 집계·원격 판정에 포함하지 않는다. 같은 최종 제출의 재전송은 성공 처리하고 다른 내용의 덮어쓰기는 409다.
답변 해시나 초안 revision이 맞지 않는 요청도 저장하지 않는다.

동기화 대상은 원본 run이 아니라 본인 grading run의 grading-result.json과 quality.<차원>.* metric이다.
회차가 달라지면 별도 run이므로 이전 판정·metric을 덮어쓰지 않는다. native Trace Feedback나 원본 Judge 표를 수정하지 않는다.
한 회차의 artifact는 진행 중에는 최신 제출을 반영하며 완료 후 판정은 불변이다.

**내 판정 내려받기**는 schema·원본 해시·평가자·회차·labels를 포함하는 grading-result.json이다.
`review.py analyze --grading-run-id` 또는 `--grading-file`로 사람·모델의 완료한 채점들을 함께 분석한다.
원시 labels JSONL이나 이전 형식의 채점은 입력받지 않는다. 실제 사람의 반복 정합성 결과가 확보되었다는 뜻은 아니다.

## API

모든 검수 API는 HttpOnly 세션을 사용하고 상태 변경은 CSRF와 origin을 검사한다.
평가 실행용 Bearer 토큰만으로 검수자를 대신하지 않는다.

| 경로 | 역할 |
| --- | --- |
| `GET /api/review-candidates` | 로그인 사용자: 평가 실행·본인 완료 횟수·회차 목록, 페이지 조회 |
| `POST /api/reviews` | 로그인 사용자: run_id의 본인 새 회차 시작 또는 진행 회차 반환 |
| `GET /api/reviews` | 본인 회차 목록과 문항별 집계 상태 |
| `GET /api/admin/reviews/{run}/origin` | 관리자: 원본 run 이름·시각·외부 URL |
| `GET /api/reviews/{run}/rounds/{round}/progress` | 본인 회차의 문항별 제출·초안·동기화 여부 |
| `GET /api/reviews/{run}/rounds/{round}/cases/{position}` | 고정 문항과 본인 회차의 판정·초안 |
| `PUT /api/reviews/{run}/rounds/{round}/cases/{position}/draft` | 답변 해시·revision·미완성 scores로 초안 저장 |
| `POST /api/reviews/{run}/rounds/{round}/cases/{position}` | 답변 해시·scores·draft_revision으로 최종 제출 |
| `GET /api/reviews/{run}/rounds/{round}/labels` | 본인 해당 회차의 개별 labels |
| `GET /api/reviews/{run}/rounds/{round}/result` | 본인 회차의 공통 grading manifest |
| `GET /api/admin/reviews/{run}/rounds/{round}/origin` | 관리자: 본인 채점 run 링크 |
| `POST /api/reviews/{run}/rounds/{round}/sync` | 본인 grading run의 artifact·metric·상태 동기화 |

## 검증 범위

격리 PostgreSQL에서 로그인 신원·회차별 격리·개인별 횟수·중복 시작·블라인드 응답·CSRF·만료·적용 조건·불변 제출·
초안 복원/충돌·동기화 재시도·회차별 artifact·분석 manifest 검증·이전 schema 거부을 검증한다.
테스트의 모델 호출·MLflow 쓰기는 mock이다. 실제 홈서버의 run/bundle 읽기 연동과 개발 컨테이너 API 기동도 구분해서 확인한다.
연결 가능한 브라우저가 없어 직접 클릭·시각 검증은 미수행이다.

## Ragas 점수와 사람 판정

오프라인 모델 채점은 Ragas 원점수를 별도 measurements에 저장한다. 사람은 기존 네 품질 항목을
pass/fail/N/A로 검수하며 모델의 점수·의견·측정 내역은 검수 API에 노출하지 않는다.
모델 원점수 1.0만 pass로 변환해 이진 일치율·κ를 계산한다. 이는 동일한 수용 여부를 비교하기 위한
명시적인 기준이며 사람의 이진 판정이 Ragas의 주장 분해·분수 점수와 동일하다는 뜻은 아니다.
새 지표·임계값의 사람 정합성은 실제 검수 자료로 별도 확인해야 한다.

## 평가자 일치도

사람–모델의 항목별 Cohen’s κ를 주요 정합성 지표로 보고 사람–사람·모델–모델·동일 주체 반복을 구분한다.
세 주체 이상의 종합 Fleiss’ κ와 Krippendorff’s α는 보조 결과다. 혼합 집단의 합의가 사람 판단과의
정합성을 대체하지 않으므로 사람–모델 쌍별 결과를 함께 본다.
종합 분석은 평가자당 한 회차·프로필만 선택한다. 미제출/N/A/오류를 구분하고, 반복 회차를 별개 사람으로 세지 않는다.
정확한 적용 조건과 CLI는 [평가 저장 스키마](evaluation-schema.md)와 [운영 문서](chat-service-operations.md)를 따른다.
