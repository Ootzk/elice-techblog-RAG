# 평가 저장 스키마

이 문서는 v0.2.0 평가 저장 계약 `evaluation-v1`의 기준이다. 구현은 이 계약을 따른다.
같은 답변을 여러 평가자가 여러 번 채점할 수 있으며, 사람과 모델에 같은 저장 단위를 적용한다.
이전 형식은 변환하거나 혼합하지 않는다. schema 태그나 artifact의 schema가 맞지 않는 실행은 목록·분석에서 제외한다.
기존 원격 실행·로컬 데이터는 자동 삭제하지 않는다.

## 세 가지 run

```text
answers A1: 한 번 생성한 데이터셋 전체의 질문·답변·근거
├─ grading: model:chatgpt / 1회차
├─ grading: model:chatgpt / 2회차
├─ grading: model:claude / 1회차
├─ grading: model:gemini / 1회차
├─ grading: human:<코드 ID> / 1회차
└─ grading: human:<코드 ID> / 2회차

analysis: 같은 A1에 연결된 grading run들을 비교한 통계
```

**grading run 1개 = 한 평가자가 한 답변 생성 run 전체를 한 번 채점한 단위**다.
질문·차원별 판정은 run 내부의 레코드다. 데이터셋·프롬프트·검색 정책을 바꿔 생성하면 별도 answers run이 된다.

| run 종류 | 책임 | 주요 artifact | Metric |
| --- | --- | --- | --- |
| answers | Dataset·QA 설정을 고정하고 RAG 답변 생성 | evaluation-bundle.json, evaluation-results.json | 결정적 검색·유보 지표, 생성 자원·시간 |
| grading | 한 사람 또는 한 모델의 한 번 채점 | grading-result.json, rater-profile.json | 같은 이름의 차원별 통과율·채점/N/A/오류/미채점 건수 |
| analysis | 지정한 grading run들의 반복·평가자 간 비교 | agreement.json, 비교한 run 목록 | 차원별 일치율·κ 등 유효한 수치 |

grading의 `mlflow.parentRunId`는 answers run ID다. analysis는 동일한 원본 답변을 비교하며 입력 run ID들을 보존한다.
원본 답변 run에 모델·사람 점수를 덧붙이거나 새 채점으로 이전 metric을 덮어쓰지 않는다.

## 식별과 MLflow 태그

모든 run은 `evaluation.schema=evaluation-v1`, `evaluation.kind=answers|grading|analysis`를 갖는다.
grading run에는 다음 태그를 함께 기록한다.

| 태그 | 의미 |
| --- | --- |
| source_run_id | 원본 answers run ID |
| source_bundle_digest | 고정 답변 bundle 해시 |
| rater_type | model 또는 human |
| rater_id | model:chatgpt / model:claude / model:gemini 또는 human:<로그인 코드 ID> |
| rater_profile_digest | 평가자 조건의 내용 해시 |
| round | 같은 원본·평가자·프로필 안의 1부터 시작하는 반복 번호 |
| grading_id | 로컬 채점 작업 UUID, 원격 생성 재시도 때 동일 작업을 찾는 키 |

평가자 ID는 논리적인 주체다. 실제 모델·MLflow scorer 이름/버전·평가 기준은 rater-profile.json에 고정한다.
모델/평가 기준이 바뀌면 프로필 해시가 달라지며 동일 조건의 반복으로 취급하지 않는다.
사람 프로필은 원본에 고정한 rubric과 적용 계약으로 결정한다. 로그인 코드가 사람 신원이며 대리 선택하지 않는다.
한 모델 run에는 그 모델의 모든 차원이 들어간다. 세 모델을 하나의 grading run에 넣지 않는다.

round는 PostgreSQL의 공통 레지스트리에서 원자적으로 할당한다. 사람·모델 모두 같은 레지스트리를 사용한다.
원격 run ID와 로컬 grading_id를 연결해 동기화 재시도가 새 회차를 만들지 않도록 한다.

## 고정 답변과 평가 기준

evaluation-bundle.json은 schema, source_run_id, Dataset ID·해시, QA 프로필, kind(gold/iron),
문항 records, 생성 cases, 차원별 rubric 원문을 포함한다. digest는 digest 필드를 제외한 전체 내용의 해시다.
문항 ID는 중복되지 않고 records와 cases의 문항 집합은 같아야 한다.
이전 schema bundle이나 원본 run ID가 다른 bundle은 거부한다.

Gold는 correctness·completeness·groundedness·relevance, Iron은 behavior를 사용한다.
사람 검수 화면과 모델 채점은 같은 rubric·차원별 허용 입력·사전 N/A 조건을 사용한다.
등록된 모델 Judge의 instructions가 원본 rubric과 맞지 않으면 다른 기준으로 조용히 채점하지 않는다.
Gateway URI·등록 scorer 버전은 식별하지만 외부 모델 별칭 뒤의 가중치 변경이나 완전한 결정성까지 보장하지 않는다.

## 공통 질문별 판정

grading-result.json은 다음 정보를 포함한다.

```json
{
  "schema": "evaluation-v1",
  "source_run_id": "<answers run>",
  "source_bundle_digest": "<hash>",
  "rater_type": "human",
  "rater_id": "human:<code UUID>",
  "rater_profile_digest": "<hash>",
  "round": 1,
  "complete": true,
  "labels": [
    {
      "record_id": "<dataset record ID>",
      "answer_digest": "<question/history/answer/context hash>",
      "dimension": "groundedness",
      "rater": "human:<code UUID>",
      "round": "1",
      "protocol": "<rater profile hash>",
      "label": "pass",
      "rationale": "결정적인 근거"
    }
  ]
}
```

label은 pass/fail/not_applicable/error다. 최종 결과에 같은 문항·차원 판정이 중복되면 안 된다.
answer_digest·평가자·회차·프로필이 manifest와 다르거나 허용하지 않은 문항/차원이 있으면 거부한다.
complete=true는 생성에 성공한 모든 문항의 모든 차원이 판정된 상태다.
생성 실패 문항은 두 평가자 유형 모두 채점에서 제외하고 source_failed_cases에 따로 기록한다.
모델 호출 실패는 error로 구분한다. 사람의 미제출 초안은 label이 아니며 grading artifact에 포함하지 않는다.

## 공통 metric

각 grading run에는 평가자 이름이 붙지 않은 동일 metric 이름을 사용한다.

- quality.<차원>.pass_rate: pass / (pass + fail). 분모가 0이면 기록하지 않는다.
- quality.<차원>.scored_cases: pass + fail
- quality.<차원>.not_applicable_cases: N/A 수
- quality.<차원>.error_cases: 채점 오류 수
- quality.<차원>.pending_cases: 아직 판정되지 않은 수
- source_failed_cases: 원본 생성 실패 문항 수

MLflow SDK가 추가하는 native 지표와 위 공통 지표를 구분하며 비교·보고서는 위 공통 이름을 사용한다.
N/A·오류·미제출을 실패나 0점으로 섞지 않는다. 부분 사람 검수는 pending_cases를 남기고 RUNNING을 유지한다.
analysis의 κ는 같은 답변의 대응되는 정상 이진 판정만 사용하며, 정의되지 않으면 숫자를 만들지 않고 이유를 artifact에 남긴다.

## 상태와 저장 흐름

모델: 원본 검증 → 평가자별 프로필 고정 → 각 모델·반복별 grading 할당 → MLflow run 생성 →
그 모델의 차원별 scorer 실행 → 공통 labels·metric 저장 → FINISHED 또는 FAILED.
최초 Dataset 평가도 이 경로로 모델별 자식 run을 만든다. Judge만 반복하는 CLI도 같은 경로를 사용한다.

사람: 회차 시작 → 공통 grading 할당·MLflow run 생성(RUNNING) → 초안/최종 제출을 DB에 저장 →
명시적 동기화로 해당 grading run의 artifact·metric 갱신 → 모든 문항 제출 시 FINISHED.
진행 중인 같은 사람 회차는 이어서 사용하며 완료 후 새 회차를 만든다. 초안과 이전 점수를 새 회차에 복사하지 않는다.
MLflow 장애가 최종 제출을 잃게 하지 않으며, 같은 grading_id로 생성·동기화를 재시도한다.
완료한 사람 판정은 불변이고 새 회차에서만 다시 평가한다.

로컬 테이블은 evaluation_sources(고정 원본), grading_runs(공통 평가자·회차·원격 ID),
grading_cases(질문별 제출 labels), grading_drafts(사람 초안)로 구분한다.
기존 human_review_*와 혼합하지 않고 새 계약의 테이블·schema만 사용한다. 호환 마이그레이션·옛 metric backfill은 하지 않는다.

## 검수 UI와 분석

UI는 answers run만 평가 실행으로 조회한다. 본인의 사람 grading 회차·완료 횟수만 보여주며 모델/다른 사람의 점수는 숨긴다.
원본 answers run 링크와 별도로 현재 채점 run 링크를 관리자에게 제공해 metric과 artifact를 바로 찾을 수 있게 한다.
같은 원본의 grading run들을 선택해 analysis를 수행한다. 한 사람/모델의 같은 프로필 반복은 repeat_consistency,
같은 주체의 다른 프로필은 judge_revision, 다른 주체끼리는 inter_rater로 구분한다.
분석은 원본 답변/프로필/입력 run 목록을 확인하고, schema가 다른 예전 결과는 받지 않는다.

이 문서는 구현 계약이며 실제 유료 모델 평가나 사람 정합성 실험의 완료를 의미하지 않는다.
