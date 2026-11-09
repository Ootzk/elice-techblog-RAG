# 대화 서비스 실행과 Dataset 평가

## 실행 구성

`apps/web`는 Open WebUI 프론트엔드에서 추출·수정한 Svelte 화면이다.
원본 커밋·파일별 해시·변경 범위는 `apps/web/UPSTREAM.md`와 `upstream.json`에 있다.
Node 22(22.17 이상)에서 정적 빌드하고 Knowledge 이미지에 포함한다.
API와 worker는 동일 이미지의 서로 다른 프로세스이며 Open WebUI 백엔드는 실행하지 않는다.

```mermaid
flowchart LR
  Browser[브라우저 · 추출한 Open WebUI] --> API[Knowledge · FastAPI]
  API --> PG[(PostgreSQL · 세션/대화/작업/QA 기록)]
  Worker[Knowledge worker] <--> PG
  Worker --> Vector[기존 pgvector 검색]
  Worker --> Elice[엘리스 Serverless 생성]
  Admin[평가 CLI · 관리자 Bearer] --> API
  API --> Dataset[MLflow Dataset]
  Worker --> Runs[MLflow 평가 Run]
```

`.env.dev`에는 기존 DB·임베딩·MLflow·생성 설정과 아래 값을 넣는다.
배포는 `.env`와 `compose.yaml`을 사용하고 HTTPS reverse proxy 뒤에 배치한다.

```dotenv
ADMIN_ACCESS_CODE=<최초 관리자 로그인 코드, 최소 20자>
AUTH_SESSION_SECRET=<세션 CSRF 서명용 비밀값, 최소 32자>
EVALUATION_API_TOKEN=<평가 API 전용 관리자 토큰>
QA_ACTIVE_INDEX_KEY=<corpus-vector-indexes.sql에서 확인한 인덱스 키>
CHAT_SESSION_HOURS=168
QA_WORKER_CONCURRENCY=1
```

값은 예를 들어 `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`로 각각 생성한다.
로그인 코드는 로그인 화면에만 입력한다. 평가 API 토큰과 세션 비밀값은 프론트엔드에 넣지 않는다.
`init-db`가 최초 관리자 코드를 DB에 등록한다. 동일 코드 재실행으로 폐기 상태를 되돌리지 않는다.
`AUTH_SESSION_SECRET`은 모든 API 인스턴스에서 동일하게 유지한다. 변경하면 기존 쿠키는 무효가 되지만,
로그인 코드와 코드별 대화는 남아 있어 다시 로그인할 수 있다.

```bash
export KNOWLEDGE_ENV_FILE=.env.dev
# 기존 DB를 유지하고 테이블만 추가한다. 먼저 새 이미지를 빌드한다.
docker compose --env-file .env.dev -f compose.yaml -f compose.dev.yaml build knowledge
docker compose --env-file .env.dev -f compose.yaml -f compose.dev.yaml run --rm --no-deps knowledge python -m elice_knowledge init-db
docker compose --env-file .env.dev -f compose.yaml -f compose.dev.yaml up -d --no-deps knowledge worker
```

브라우저에서 http://127.0.0.1:8080 에 접속한다. 별도 UI 개발 시 Node 22에서
`cd apps/web && npm ci && npm run dev`를 사용한다. Vite는 `/api`를 8080으로 proxy한다.
API와 Vite 접속 주소의 hostname을 통일한다. `localhost`와 `127.0.0.1` 쿠키는 공유되지 않는다.

prod 세션 쿠키는 Secure다. HTTPS proxy가 원래 Host·scheme을 전달하도록 구성하고,
Uvicorn의 forwarded-header 신뢰 대상을 해당 proxy 주소로 제한한다.
로그인 한도는 연결 IP 기준이므로 reverse proxy 환경에서는 신뢰할 수 있는 client IP 전달도 구성한다.
Compose 기본 바인딩은 localhost이므로 외부 접속은 배포 ingress에서 연결한다.

## 관리자와 방문자 코드

관리자 코드로 로그인하면 대화 화면 상단에 **관리자 ↗** 버튼이 표시된다.
`/admin/authentication`에서 다음 작업을 할 수 있다.

- 방문자별 이름/용도, 유효기간(1~3660일), 시간당 질문 한도를 지정해 코드 발급.
- 정상·만료 임박(3일 이내)·만료·폐기 상태 필터, 최근 로그인 시간/IP와 최근 1시간 질문 수 확인.
- 활성 코드는 기존 만료일에서, 만료된 코드는 현재 시각에서 유효기간 연장.
- 코드 폐기. 연결된 모든 세션은 다음 요청부터 차단하며 폐기는 되돌릴 수 없다.

방문자 코드는 암호학적 난수로 생성하고 DB에는 SHA-256 해시만 보관한다.
홈페이지의 역할·발급/연장/폐기 흐름을 참고했으며, 원문 코드를 라벨에 보관하는 방식은 가져오지 않았다.
발급 직후에만 원문을 표시하므로 닫기 전에 복사해 전달한다. 분실 시 새 코드를 발급하고 기존 코드를 폐기한다.
코드 자체가 대화 소유자이므로 여러 사람에게 하나의 코드를 배포하면 서로의 대화가 보인다.
새 코드는 별도의 소유자이며 다른 코드의 대화를 자동으로 가져오지 않는다.

관리자 코드는 웹에서 생성·승격·폐기하지 않는다. 새 `ADMIN_ACCESS_CODE`를 환경 파일에 지정하고
새 환경으로 `init-db`를 실행하면 추가 관리자 코드가 등록된다. 이전 관리자 코드 폐기는 서버 CLI를 사용한다.
마지막 관리자 코드는 폐기할 수 없다.

```bash
docker compose --env-file .env.dev -f compose.yaml -f compose.dev.yaml exec knowledge python -m elice_knowledge revoke-admin '<관리자 코드 ID>'
```

`ADMIN_ACCESS_CODE`는 초기 등록용이며 DB에 등록된 후 환경 파일 변경만으로 이전 코드를 폐기하지 않는다.
관리자 브라우저 로그인과 `EVALUATION_API_TOKEN`은 서로 다른 권한 경로다.
평가 Bearer만으로 코드 관리 화면/API를 사용할 수 없으며, 로그인 코드는 평가 API 토큰을 대신하지 않는다.
관리자 HTML은 서버에서, 관리 API는 세션 역할/CSRF에서 각각 접근을 제한한다.

| API | 역할 |
| --- | --- |
| `GET /api/admin/access-codes?status=all&page=1` | 관리자 코드 목록·사용량, 페이지당 30개 |
| `POST /api/admin/access-codes` | 방문자 코드 발급, 원문 일회 반환 |
| `POST /api/admin/access-codes/{id}/extend` | `days`만큼 유효기간 연장 |
| `DELETE /api/admin/access-codes/{id}` | 방문자 코드 영구 폐기 |

등록·연장·폐기는 `access_code_events`에 작업자와 UTC 시각을 기록하며 원문 코드를 남기지 않는다.
서버에서 표시할 시간은 UTC로 저장하고 화면에서는 브라우저 현지 시간대로 표시한다.

## 대화와 실패 처리

- opaque 쿠키는 HttpOnly/SameSite=Lax이며 DB에는 해시만 저장한다. CSRF 토큰은 메모리에 둔다.
- 같은 로그인 코드라면 로그아웃·쿠키 만료·다른 기기에서도 대화를 불러온다. 다른 코드의 대화는 404다.
- 대화에 고정된 QA 프로필은 인덱스·모델·생성 매개변수·문맥 예산·코드/의존성 해시를 포함한다.
  접속 endpoint는 해시로만 식별한다. 프로필과 실행 환경이 달라지면 새 대화를 시작해야 한다.
- 기본 최근 3쌍/2048토큰 이력을 planner에 지시 대상 해석용으로만 주고 매 턴 문서를 다시 검색한다.
  한 질문의 유료 호출은 planner 턴(최대 `QA_AGENT_MAX_TOOL_CALLS`회)과 답변 생성 1회다.
  도구 호출 한도에 도달하면 planner를 더 호출하지 않고 답변 단계로 넘어가므로 기본값 4에서 최대 5회다.
  근거가 없거나 되묻기로 끝나면 답변 생성 호출은 생략한다.
- 요청 UUID는 같은 전송에 재사용한다. 네트워크 오류 시 브라우저 sessionStorage에 질문과 UUID를
  남겨 같은 요청으로 확인한다. 자격증명은 저장하지 않는다.
- queued/running 작업은 polling한다. 출처 검증을 마친 최종 답변만 표시한다.
- 원문 링크는 인용문을 Text Fragments(`#:~:text=`)로 지정해 지원 브라우저에서 이동·강조한다.
  Markdown 표기를 보이는 텍스트로 정리하고 여러 문단은 시작/끝 범위로 연결한다.
  기존 섹션 앵커는 유지한다. 원문 변경·동일 문구 중복·브라우저 지원/정책에 따라 강조가 실패할 수 있으며,
  실제 브라우저에서의 원문 강조는 자동 검증하지 않았다. 명세: https://wicg.github.io/scroll-to-text-fragment/
- worker lease 기본 300초, 동시 실행 기본 1이다. DB에서 모든 worker의 claim을 직렬화하고
  running 작업 수를 검사한다. lease 만료 작업은 interrupted이며 자동 재호출하지 않는다.
- 쿠키 세션 기본 168시간이며 로그아웃은 해당 세션만 종료한다. 코드별 대화는 세션 정리로 삭제하지 않는다.
  명시적으로 삭제한 대화와 관련 실행 기록은 24시간 후 worker가 정리한다. 평가 기록은 별도다.
- 질문 한도는 모든 기기·세션을 합쳐 코드별로 계산한다. 실패/중단을 포함한 접수 건수 기준이며
  재로그인으로 초기화하지 않는다. 관리자 코드는 이 시간당 한도만 우회하고 전역 worker 동시 실행 제한은 적용한다.
- 코드 만료/폐기 시 queued 작업은 중단한다. 이미 실행 중인 외부 모델 요청을 취소하거나 과금을 되돌리지는 않는다.
- 방문자 질문 한도는 코드 발급 시 설정한다.
- 총 예산과 대규모 공개 서비스의 ingress 제한은 후속 과제다.

## 평가 API와 CLI

`POST /qa`, QA/scorer 프로필과 Dataset/평가 API는 `Authorization: Bearer <EVALUATION_API_TOKEN>`이 필요하다.
`POST /qa`는 관리자용 즉시 실행 경로다. 일반 대화는 반드시 `/api/conversations/.../turns` 큐를 사용한다.
HTTP 계약은 `/docs`에서 확인한다.

### 저장소 Dataset 등록

질문·기대 답안은 저장소의 [Gold Set](../tools/evaluation/gold_set/README.md)과
[Iron Set](../tools/evaluation/iron_set/README.md) JSONL에서 수정한다.
저장소 루트에서 Knowledge의 Python 환경으로 등록 CLI를 실행한다.

```bash
# 로컬 파일만 검증한다. MLflow 접속·생성 호출은 하지 않는다.
uv run --project apps/knowledge python tools/evaluation/register.py --dry-run
# .env.dev의 MLflow 접속 설정과 QA_MLFLOW_EXPERIMENT_NAME을 사용해 두 세트를 등록한다.
uv run --project apps/knowledge --env-file .env.dev python tools/evaluation/register.py
# 한 세트만 등록할 수도 있다.
uv run --project apps/knowledge --env-file .env.dev python tools/evaluation/register.py --dataset gold
```

`elice-techblog-gold`·`elice-techblog-iron` Dataset을 각각 하나씩 생성하고 출력으로 ID를 반환한다.
동일 이름이 이미 있으면 같은 Dataset을 갱신한다. 변경된 레코드는 교체하고 삭제된 질문은 제거하며,
내용이 같은 레코드는 유지한다. MLflow UI의 별도 편집 내용도 재등록할 때 저장소 내용을 기준으로 맞춘다.
등록은 모델 호출이나 평가 실행을 포함하지 않는다. 등록 도중 오류가 나면 원인을 해결한 뒤 재실행한다.

원본 파일 경로·SHA-256은 Dataset 태그, `gold-001` 같은 문항 ID와 메타데이터는 레코드 태그에 저장한다.
등록 파일 해시는 출처 추적용이며, 아래 평가 접수 해시는 MLflow 레코드를 읽어 계산하는 별도 값이다.
서버 접속은 `MLFLOW_TRACKING_URI`와 MLflow 인증 환경 변수를 사용한다.
홈서버와 제출 서버에는 같은 명령으로 각각 등록하며 Dataset ID는 각 서버의 출력을 사용한다.

Gold·Iron 모두 같은 평가 API/CLI로 실행한다. Iron은 `expected_behavior`·`required_points`·
`failure_conditions`를 요구하며 Judge가 꺼져 있으면 접수를 거부한다. 서로 다른 계약을 한 Dataset에 섞지 않는다.

### Judge 등록과 dev 자동 평가

MLflow Gateway에 구성된 세 endpoint의 인증정보를 재사용한다. 앱에 모델별 API 키를 추가하지 않는다.

```bash
uv run --project apps/knowledge --env-file .env.dev python tools/evaluation/judges.py --enable-dev
```

등록 명령은 저장소 rubric을 게시하며 변경 시 새 Judge 버전을 등록한다. 같은 내용은 재사용한다.
Gateway endpoint는 `elice-techblog-rag-judge-{chatgpt,claude,gemini}`가 미리 구성되어 있어야 한다.

- `qa-gold-*`: 모범 답안·필수 요점·금지 주장과 비교한다.
- `qa-iron-*`: 기대 행동·실패 조건과 비교한다.
- `qa-online-*`: 모범 답안 없이 질문 적합성·검색 근거 충실도·인용을 판단한다.

`compose.dev.yaml`은 API와 worker의 `EVALUATION_JUDGES_ENABLED=true`를 설정한다.
호스트에서 worker를 실행하면 환경 변수도 직접 설정한다. prod 기본값은 false다.
Gold/Iron 접수 시 해당 세 Judge의 등록 버전을 고정하고, QA 완료 후
`mlflow.genai.evaluate()`로 채점한다. Judge 오류·누락 판정은 평가 실패로 처리한다.
Run의 `judge-versions.json`, `judge-results.json`에 버전·문항 ID·판정·이유를 남기고
Evaluation runs에서 상세 결과를 확인한다. 사례 API의 scores에도 Boolean 판정을 0/1로 제공한다.

online Judge는 `environment=dev`이면서 `evaluation_mode=online`인 Trace를 100% 자동 평가한다.
일반 QA에는 질문·고정 이력·답변·검색 근거·agent 단계가 담긴 `techblog-qa` 루트 Trace를 기록한다.
LangGraph 노드와 분기는 `mlflow.langchain.autolog(run_tracer_inline=True)`로 자동 추적한다.
LlamaIndex autologging은 중복 방지를 위해 끄고, 직접 구현한 경계만 다음 span으로 보완한다.

| Span | 유형 | 기록 내용 |
| --- | --- | --- |
| `plan`·`act`·`answer`·`clarify`, 조건 분기 | 자동 그래프 추적 | 노드 입출력·시간·오류, planner의 선택과 사용한 도구 수 |
| `generation.chat` | LLM | 실제 메시지·응답·모델·토큰 사용량. 부모 노드로 planner/최종 생성을 구분 |
| `keyword_search`·`meaning_search` | TOOL | 도구 인자·반환값·실패 상태 |
| `retrieval.keyword_search`·`retrieval.meaning_search` | RETRIEVER | 실제 검색어·top_k·청크 본문·출처·점수 |
| `retrieval.final_context` | RETRIEVER | 토큰 예산을 적용한 뒤 최종 생성에 전달한 근거 |
| `planner.parse` | PARSER | planner JSON과 파싱한 결정 |
| `answer.parse_and_validate` | PARSER | 생성 결과 파싱·인용문 검증, 응답 상태와 검증 결과 |

설정 객체·HTTP 인증 헤더·endpoint 주소는 내보내지 않는다. 검색 내부 오류는 오류 코드만 기록한다.
Trace 저장 실패는 대화를 중단하지 않는다. MLflow 추적을 끄면 Trace도 기록하지 않는다.
데이터셋 QA는 `evaluation_mode=offline`으로 기록해 online Judge와 중복 채점하지 않는다.
MLflow 서버는 비동기로 채점하며 기본적으로 Trace 시작 후 약 5분 뒤 대상이 되므로 즉시 점수가 보이지 않을 수 있다.
[자동 평가 문서](https://mlflow.org/docs/latest/genai/eval-monitor/automatic-evaluations/)를 참고한다.
인간 평가와 Cohen’s kappa, Judge 반복 판정의 일관성 검증은 별도 후속 작업이다.

### Gold / Iron Set 평가

MLflow Gold Dataset의 각 레코드는 다음 형식을 사용한다.

```json
{
  "inputs": {"question": "GPU 가상화의 목적은?", "history": []},
  "expectations": {
    "answerable": true,
    "evidence": [{"url": "https://elice.io/ko/resources/blog/<article>", "quote": "실제 원문 인용 구간"}],
    "answer": "사람이 검토한 수용 기준 또는 기대 답변"
  }
}
```

history가 있으면 user/assistant 완전한 쌍으로 구성한다. expectations는 QA로 전달하지 않는다.
answerable=true 문항은 evidence가 필수다. answerable=false는 evidence를 생략할 수 있다.
데이터셋을 읽을 때 inputs를 엄격히 검증하고 전체 레코드 내용으로 해시를 만든다.
실행 접수 시 그 해시를 다시 확인하고 선택 레코드를 PostgreSQL에 고정한다. 한 번에 최대 200개다.
현재 provided-history만 허용하며 rollout 요청은 422로 거부한다.

```bash
# EVALUATION_API_TOKEN은 현재 터미널 환경에 설정한다. 명령 인자나 Git에 넣지 않는다.
python3 tools/evaluation/evaluate.py --dataset-id '<dataset-id>'
# 위 출력의 대상·건수·프로필을 검토한 뒤 유료 작업을 명시적으로 접수한다.
python3 tools/evaluation/evaluate.py --dataset-id '<dataset-id>' --submit
```

개별 record는 `--record-id`를 반복해 선택한다. QA 프로필은 `--qa-profile-key`로 지정할 수 있다.
`--prompt NAME=VERSION|ALIAS`를 반복하면 해당 Registry 프롬프트로 고정한 실험용 프로필을 만들어 평가한다(미리보기에서도 프로필은 생성된다).
평가는 모든 프롬프트가 Registry에서 확정된 프로필만 접수하고, 저장소 템플릿으로 대체된 프로필은 409로 거부한다.
CLI 기본 동작은 metadata만 읽으며 생성하지 않는다. 접수 후 다음 API에서 조회한다.

| API | 용도 |
| --- | --- |
| `GET /api/qa-profiles` | 활성/저장된 QA 프로필 |
| `POST /api/qa-profiles` | `index_key`·`top_k`·`prompts`(이름→버전/alias)로 실험용 프로필 생성, HTTP 201 |
| `GET /api/scorer-profiles` | 고정 scorer 키와 정의 |
| `GET /api/evaluation-datasets/{id}` | Dataset 해시·레코드 검토 |
| `POST /api/evaluations` | 고정 평가 접수, HTTP 202 |
| `GET /api/evaluations/{id}` | 상태·집계·MLflow Run ID |
| `GET /api/evaluations/{id}/cases?limit=50&offset=0` | 사례별 실행·검색 결과·점수·오류 |

일반 대화는 코드·생성 endpoint 변경으로 기존 QA 프로필이 만료되면 다음 질문 처리 시 현재 활성 프로필로 갱신한다.
기존 대화 이력과 이전 실행의 프로필은 보존하며 실패한 질문을 자동 재실행하지 않는다.
Dataset 평가는 프로필을 갱신하지 않고 실행 조건 불일치를 오류로 처리한다.

대화·평가·ask CLI는 `execution.execute`를 공유하고 `qa_runs`에 입력·출력·지연을 저장한다.
MLflow에는 Dataset ID/해시·QA/scorer 프로필·사례별 출력·집계 JSON과 지표를 남긴다.
MLflow 기록 실패 또는 사례 실패가 있으면 평가 전체를 성공으로 표시하지 않는다.

첫 scorer `evidence-v1`은 다음 두 지표만 계산한다.

- `evidence_recall`: URL과 정규화한 원문 인용문이 검색된 청크에 함께 존재하는 비율.
- `answerability_correct`: answered 여부가 기대 answerable과 일치하는지.

agentic QA의 `evidence_recall`는 각 검색 호출의 top_k가 아니라 실행 전체의 누적 검색 결과를 본다.
기대 근거가 없는 문항은 이 회수율의 채점 대상에서 제외한다. `answerability_correct`는 답변 내용이
틀려도 answered 상태만 맞으면 통과할 수 있으므로 의미적 정확성과 혼동하지 않는다.
실행 실패는 0점 답변으로 섞지 않고 실패율과 유효 채점 건수를 별도로 집계한다.
이 지표는 의미적 답변 정확성·충실도를 검증하지 않는다.
Gold 22문항·Iron 8문항은 작성·검수 후 홈서버 MLflow에 등록했다.
Gold·Iron 전수 기준 평가와 사람 판단과의 정합성 검증은 아직 수행하지 않았다.
등록 CLI는 생성이나 평가를 호출하지 않는다.

### 자원·시간 지표

QA 응답의 `llm_call_count`·`tool_call_count`와 `usage`는 한 질문의 planner 및 최종 생성을 합산한다.
Judge 사용량은 이 집계에 포함하지 않는다. `usage`는 기존 Chat Completions 이름
`prompt_tokens`·`completion_tokens`·`total_tokens`와 캐시 입력인 `cached_tokens`를 사용한다.
`prompt_tokens_details.cached_tokens`가 유효한 정수일 때만 추출하며, input/total에 더하지 않는다.

필드별 `usage_reported_calls`로 제공한 호출 수를 기록하고, 모든 호출이 보고한 항목만 질문 총량에 넣는다.
누락·null·잘못된 값은 0으로 채우지 않는다. 호출별 LLM span에는 제공된 값만 기록하고,
캐시 입력은 MLflow 표준 `cache_read_input_tokens`로 전달한다. Trace의 자동 토큰 집계는 보고된 호출을
기준으로 하므로, 질문 전체의 완전성은 QA 응답의 coverage를 함께 확인한다.

평가 Run에는 성공한 QA 사례를 대상으로 다음 지표를 기록한다.

| 지표 접두사 | 값과 집계 |
| --- | --- |
| `qa_duration_ms` | QA 실행 시간의 `_mean`·`_median`·`_max`·`_measured_cases` |
| `failed_qa_duration_ms` | 실패한 QA의 시간을 위와 같은 방식으로 별도 집계 |
| `input_tokens`·`cached_input_tokens`·`output_tokens`·`total_tokens` | 위 집계와 `_total`. 완전히 보고한 질문만 대상 |
| `llm_call_count`·`tool_call_count` | 질문당 호출 수의 위 집계와 `_total` |
| `cache_hit_ratio` | 캐시와 입력이 모두 보고된 같은 질문 집합에서 캐시 입력 합 / 전체 입력 합 |
| `cache_hit_ratio_measured_cases` | 위 비율의 계산 대상 질문 수 |

측정값이 없는 항목은 건수만 0으로 기록하고 평균·합계·비율을 만들지 않는다.
`qa_duration_ms`는 프로필을 확인한 뒤 공통 QA 실행에 들어가서 완료할 때까지이며,
추적 설정·종료를 포함하지만 큐 대기와 QA 결과 DB 저장은 제외한다.
질문별 원시 값은 `evaluation-results.json`과 사례 API에서 확인한다.

일반 대화 작업의 DB 결과에는 `timings.queue_wait_ms`(접수부터 worker claim까지),
`worker_duration_ms`(claim 후 처리 시작부터 답변 준비까지), 두 값의 합인 `server_duration_ms`를 남긴다.
worker 시간에는 프로필 갱신·이력 조회·QA 실행·QA 결과 저장이 포함된다.
이 값은 작업 완료 DB 갱신과 브라우저 polling/렌더링 시간을 포함하지 않으므로 사용자 체감 시간과 구분한다.
큐 대기 시간은 QA 루트 span의 `elice.queue_wait_ms`에도 기록한다.

아직 원화/달러 비용·첫 토큰 시간(TTFT)·브라우저 기준 응답 시간은 산출하지 않는다.
비교 실험에서는 질문·모델·자원 한도·캐시 상태와 측정 건수를 같이 확인한다.
소규모 세트의 기본 집계는 평균·중앙값·최댓값이며 p95는 산출하지 않는다.

MLflow 지표 이름은 key다. 코드에서 새 이름을 기록해도 이전 Run의 key가 자동으로 변경되지 않는다.
이후 실행에는 `evidence_recall`을 사용하고, 과거 `evidence_recall_at_k` 기록은 보존한다.

## 검증 한계

Judge 연동은 Gold·Iron의 고정 모범 답안 샘플 각 1개에 모델 3개가 판정·이유를 남기는지 확인했다.
실제 QA 샘플로 질문·답변·검색 근거의 Trace 저장과 online Judge 3개의 서버 자동 판정을 확인했다.
span 보강 후 별도 실제 질문에서는 답변 생성과 36개 span의 서버 저장·부모 자식 연결·LLM 토큰 집계를 확인했다.
자원 계측 보강 후 실제 QA 샘플에서 토큰·캐시 사용량·호출 수·지연을 확인하고, 성능 요약 Run의 저장·재조회를 검증했다.
이는 연결·rubric 실행 검증이며 전수 기준 평가나 Judge 신뢰도 측정 결과가 아니다.
격리된 임시 PostgreSQL DB를 포함한 Python 테스트 221개, lint와 개발 이미지 빌드·기동을 확인했다.

브라우저 연결이 없어 실제 화면 클릭·모바일 배치는 확인하지 못했다.
대화 UI의 정적 빌드·Docker HTTP 검증과 Dataset 등록의 파일 검증·원격 내용 대조·재등록을 확인했다.
등록 CLI의 단위 테스트는 신규 등록·변경·삭제·중복 방지와 잘못된 입력 거부를 다룬다.
PostgreSQL 통합 테스트는 전용 테스트 DB가 설정된 환경에서 별도로 실행해야 한다.
기존 Corpus 27개·스냅샷 1개·청킹 결과 54개·벡터 인덱스 1개를 유지했다.
런타임 npm 의존성 보안 검사는 별도로 확인하며, 개발 도구에 low severity advisory 4개가 남아 있다. `npm audit --omit=dev`는 0개다.
정적 배포 이미지에는 Node/npm 개발 도구를 포함하지 않는다.
