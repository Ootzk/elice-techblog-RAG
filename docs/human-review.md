# Human Review

Human Review는 MLflow evaluation run에서 생성된 답변에 사람이 평가를 추가하는 기능이다.
질의응답·Judge를 다시 호출하지 않고 저장된 질문·답변·근거를 검수한다.
화면은 `/review`이며 기존 대화 화면과 로그인 코드 관리 화면에서 이동할 수 있다.
Open WebUI에서 추출한 답변·인용 렌더러를 재사용하며 검수 UI·API는 자체 구현이다.

## 사용 순서

1. 새 버전의 `init-db`를 실행해 검수 테이블을 추가하고 새 API·프런트를 기동한다.
   기존 테이블과 평가 결과를 지우지 않는다. dev 예시는 저장소 루트 기준이다.

   ```bash
   docker compose --env-file .env.dev -f compose.yaml -f compose.dev.yaml build knowledge
   docker compose --env-file .env.dev -f compose.yaml -f compose.dev.yaml run --rm --no-deps knowledge python -m elice_knowledge init-db
   docker compose --env-file .env.dev -f compose.yaml -f compose.dev.yaml up -d --no-build knowledge worker
   ```

2. 관리자 코드로 로그인하고 **사람 검수 → MLflow 평가 실행 불러오기**를 선택한다.
   `QA_MLFLOW_EXPERIMENT_NAME`에 해당하는 실험의 `activity=evaluation` run을 최신순으로 조회한다.
   목록은 25개씩 더 불러올 수 있다. 별도의 MLflow 사용자 비밀번호나 브라우저용 API 키는 필요하지 않다.
   데이터셋 이름·run 이름·실행 시각으로 구분하며 **MLflow 원본 run 열기**로 바로 이동할 수 있다.
3. 종료된 run을 검수 목록에 추가한다. 원본 run의 `evaluation-bundle.json`을 가져와 해시·문항 연결을
   검증하고 DB에 고정한다. 실패한 run도 유효한 bundle이 있으면 가져올 수 있지만 생성 실패 문항은 제외한다.
   실행 중인 run, 다른 실험, bundle이 없거나 손상된 run은 가져오지 않는다.
4. 관리자는 본인에게 자동 배정된 run을 검수하거나 기존 로그인 코드에 배정한다.
   다른 검수자는 자신의 코드로 로그인하면 배정된 run만 본다. 코드 발급·연장·폐기는 기존 관리자 화면에서 한다.
5. 질문·고정된 이전 대화·답변·인용을 읽고 항목별 탭에서 기준과 허용된 참고 자료를 확인한다.
   모든 항목의 판정과 이유를 입력한 뒤 **저장하고 다음 질문**을 누른다.
6. 중단 후 같은 코드로 돌아오면 저장된 결과를 유지하며 첫 미제출 질문부터 이어서 검수한다.
   제출한 질문은 읽기 전용이다. 제출 전 입력은 질문 이동·새로고침 시 보존되지 않는다.
7. **MLflow 동기화**로 저장한 판정을 원본 run에 반영한다. 연결이 실패해도 DB의 판정은 유지된다.
   재시도는 아직 동기화되지 않은 문항만 처리하며 동일 artifact 경로를 사용한다.
   관리자는 검수 중인 run 상단의 **MLflow 원본 run 열기 → Artifacts → human-review**에서 결과를 확인한다.
   이 링크는 일반 검수자에게 표시하지 않으며 API도 관리자 인증을 요구한다.

## 판정과 블라인드 범위

| 데이터셋 | 항목 | 척도 |
| --- | --- | --- |
| Gold | 정확성·완전성·근거 충실도·응답 적절성 | 충족 / 미충족 / 해당 없음 |
| Iron | 행동 준수 | 충족 / 미충족 |

기존 Judge의 rubric과 `evaluation_row` 입력 구성을 재사용한다. Gold의 충족·미충족은 Judge의 yes/no,
Iron의 충족·미충족은 Boolean true/false와 대응한다. 분석 파일에서는 pass/fail/not_applicable로 저장한다.
정확성·완전성의 사전 적용 조건도 Judge와 공유한다. 적용되지 않는 항목은 해당 없음으로 고정한다.
근거 충실도에서는 누적 검색 후보나 모범 답안 대신 **최종 생성에 실제 제공한 generation_sources**만 사용한다.
Iron은 실행 근거가 있으면 도구 실행 내역도 확인한다. 기준 원문은 탭에서 펼쳐 볼 수 있다.

검수 API는 허용한 질문·답변·항목별 참고 자료와 **현재 코드의 제출 결과만** 반환한다.
Judge 점수·의견, 다른 검수자 판정, 생성 모델 설정과 전체 bundle은 브라우저에 전달하지 않는다.
이는 검수 화면의 블라인드 처리이며 MLflow 자체의 열람 권한을 제한하는 기능은 아니다.
MLflow 관리자 권한을 가진 검수자는 별도로 점수를 열람할 수 있다. 답변 본문이 모델명을 직접 말하는 경우도
자동으로 지우지 않는다. 신뢰도 실험 시 검수 전에 다른 평가를 보지 않는 절차를 함께 지켜야 한다.

run을 가져온 시점의 rubric·적용 조건·답변 해시를 저장하며 재가져오기로 바꾸지 않는다.
원본 run의 Judge 버전과 현행 저장소 rubric이 달라진 경우 동일 기준이라고 간주하지 않는다.
반복·버전 비교의 rubric 대조와 실험 단위 관리는 후속 고도화 범위다.

## 저장과 MLflow 연결

- `human_review_sources`: 원본 MLflow run ID, 고정 bundle과 검수용 packet. 새 평가 run을 만들지 않는다.
- `human_review_assignments`: run별 검수자 코드 ID. 코드가 만료·폐기되면 기존 인증 규칙에 따라 접근이 차단된다.
- `human_reviews`: run·검수자·문항별 labels와 동기화 상태. 질문 전체를 한 트랜잭션으로 제출한다.

같은 요청 재전송은 성공으로 처리하지만, 이미 제출한 판정을 다른 내용으로 덮어쓰면 409를 반환한다.
브라우저가 보낸 답변 해시가 서버와 다르면 저장하지 않는다. 이 정책으로 탭 중복 제출과 대상 혼동을 방지한다.

동기화 경로는 원본 run의 `human-review/<검수자 코드 ID>/<문항 ID 해시>.json`이다.
원본 run·bundle 해시와 분석용 labels를 기록한다. 현재 동기화는 **run artifact**이며 MLflow의 native
Trace Feedback를 새로 생성하거나 Judge 점수를 수정하지 않는다. DB 저장 성공과 MLflow 동기화 성공은 별도 표시한다.

**내 평가 JSONL 내려받기**는 기존 `tools/evaluation/review.py analyze --human-labels`와 호환된다.
기존 재채점 run ID들을 함께 지정하면 같은 답변의 Judge–사람 비교를 수행할 수 있다.
한 코드를 공유하면 한 검수자로 취급되므로 독립 평가자는 각자 다른 코드를 사용한다.
사람의 재검수 라운드·제출 수정·실험 비교 대시보드는 현재 제공하지 않는다.

## API

모든 웹 API는 기존 HttpOnly 세션을 사용하며 상태 변경은 CSRF와 origin을 검사한다.
평가 실행용 Bearer 토큰만으로 검수자 신원을 대신하지 않는다.

| 경로 | 권한·역할 |
| --- | --- |
| `GET /api/admin/review-candidates` | 관리자: MLflow 평가 run 목록, page_token 페이지 조회 |
| `POST /api/admin/reviews` | 관리자: run_id로 고정 bundle 가져오기 |
| `GET /api/admin/reviews/{run}/origin` | 관리자: 원본 run 이름·데이터셋 이름·시각·MLflow 링크 |
| `POST /api/admin/reviews/{run}/assignments` | 관리자: reviewer_id에 배정 |
| `GET /api/reviews` | 본인에게 배정된 run과 진행 상황 |
| `GET /api/reviews/{run}/cases/{position}` | 배정된 run의 0부터 시작하는 질문과 본인 판정 |
| `POST /api/reviews/{run}/cases/{position}` | answer_digest·scores 제출 |
| `GET /api/reviews/{run}/labels` | 본인의 분석용 labels |
| `POST /api/reviews/{run}/sync` | 본인의 미동기화 판정을 원본 MLflow run에 기록 |

## 검증 범위

PostgreSQL 통합 테스트에서 코드별 배정·CSRF·만료·블라인드 응답·적용 조건·원자적 제출·중복 제출·재로그인 복원·
동기화 실패 후 재시도·artifact 연결·분석 파일 호환성을 검증한다. 모델 호출과 MLflow 쓰기는 mock으로 대체한다.
실제 전수 평가·사람 검수는 이 테스트에 포함하지 않는다.

이번 구현에서는 격리 PostgreSQL을 포함한 앱 테스트와 Python lint·format·lock 검사,
Web 포맷·타입 검사·정적 빌드를 통과했다. 실제 홈서버 MLflow에서 평가 run 1개와 고정 답변 1개·4차원을
임시 로컬 DB로 가져오는 읽기 연동도 확인했다. 원격에 테스트 사람 판정을 기록하지 않았다.
연결 가능한 브라우저가 없어 화면의 직접 클릭·시각 검증은 미수행이다.
