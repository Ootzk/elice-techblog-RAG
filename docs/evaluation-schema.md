# 평가 저장 스키마

이 문서는 v0.2.0 평가 저장 계약 `evaluation-v1`의 기준이다. 구현은 이 계약을 따른다.
같은 답변을 여러 평가자가 여러 번 채점할 수 있으며, 사람과 모델에 같은 저장 단위를 적용한다.
이전 형식은 변환하거나 혼합하지 않는다. schema 태그나 artifact의 schema가 맞지 않는 실행은 목록·분석에서 제외한다.
기존 원격 실행·로컬 데이터는 자동 삭제하지 않는다.

## 네 가지 run

```text
answers A1: 한 번 생성한 데이터셋 전체의 질문·답변·근거
├─ grading: model:chatgpt / 1회차
├─ grading: model:chatgpt / 2회차
├─ grading: model:claude / 1회차
├─ grading: model:gemini / 1회차
├─ grading: human:<코드 ID> / 1회차
└─ grading: human:<코드 ID> / 2회차

analysis: 같은 A1에 연결된 grading run들을 비교한 통계
comparison: 조건 A/B의 analysis와 연결된 answers·grading을 비교한 결과
```

**grading run 1개 = 한 평가자가 한 답변 생성 run 전체를 한 번 채점한 단위**다.
질문·차원별 판정은 run 내부의 레코드다. 데이터셋·프롬프트·검색 정책을 바꿔 생성하면 별도 answers run이 된다.

| run 종류 | 책임 | 주요 artifact | Metric |
| --- | --- | --- | --- |
| answers | Dataset·QA 설정을 고정하고 RAG 답변 생성 | evaluation-bundle.json, evaluation-results.json | 결정적 답변 가능 여부 지표, 생성 자원·시간 |
| grading | 한 사람 또는 한 모델의 한 번 채점 | grading-result.json, rater-profile.json | 공통 차원별 통과율·채점/N/A/오류/미채점 건수, 모델의 Ragas 원점수 |
| analysis | 지정한 grading run들의 반복·평가자 간 비교 | agreement.json, 비교한 run 목록 | 차원별 일치율·κ 등 유효한 수치 |
| comparison | 조건 A/B의 품질·자원·정합성 비교 | comparison-result.json, comparison-inputs.json | 대응 평균 차이·신뢰구간·문항별 변화·동일 대상의 계수 차이 |

grading의 `mlflow.parentRunId`는 answers run ID다. analysis는 동일한 원본 답변을 비교하며 입력 run ID들을 보존한다.
원본 답변 run에 모델·사람 점수를 덧붙이거나 새 채점으로 이전 metric을 덮어쓰지 않는다.

## 식별과 MLflow 태그

모든 run은 `evaluation.schema=evaluation-v1`, `evaluation.kind=answers|grading|analysis|comparison`을 갖는다.
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
모델 프로필에는 Ragas 버전·구현 해시·모델 요청 조건·pass threshold를 추가한다. 등록된 MLflow scorer의
모델 URI와 rubric을 고정하지만, 오프라인 품질 점수 계산은 기존 make_judge 대신 Ragas가 수행한다.
일반 대화의 서버 측 online 자동 Judge와 오프라인 Eval Harness는 구분한다.
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
사람 검수는 동결 rubric을 사용하고 모델은 동일한 품질 차원을 Ragas 알고리즘으로 계산한다.
차원별 허용 입력 범위와 사전 N/A 조건은 공유하지만, 주장 분해와 분수 점수는 사람 판정과 동일하지 않다.
이진 threshold를 통한 비교의 정합성은 실제 사람 자료로 검증해야 한다.
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
모델 호출 실패는 error로 구분한다.
모델 manifest는 `measurement_contract=ragas-v1`과 `measurements`를 포함한다. 각 측정은
record_id·metric·score(0–1 또는 null)·status(scored/not_applicable/error)·rationale를 갖는다.
Gold 문항당 네 품질 점수와 누적·최종 문맥의 Recall·Precision 네 점수, Iron은 behavior 한 점수를 저장한다.
complete 모델 manifest는 생성 성공 문항의 모든 측정을 포함해야 하며 중복·범위 밖 수치·NaN을 거부한다.
원점수 1.0만 pass로 변환한 네 품질 label이 측정과 일치하는지 검증한다. 사람 manifest의 measurements는 비어 있다.
이 계약이 없는 이전 모델 manifest는 새 분석에서 받지 않는다. 사람의 미제출 초안은 label이 아니며 grading artifact에 포함하지 않는다.

## 공통 metric

각 grading run에는 평가자 이름이 붙지 않은 동일 metric 이름을 사용한다.

- quality.<차원>.pass_rate: pass / (pass + fail). 분모가 0이면 기록하지 않는다.
- quality.<차원>.scored_cases: pass + fail
- quality.<차원>.not_applicable_cases: N/A 수
- quality.<차원>.error_cases: 채점 오류 수
- quality.<차원>.pending_cases: 아직 판정되지 않은 수
- source_failed_cases: 원본 생성 실패 문항 수

모델 run에는 `ragas.<metric>.mean`·scored_cases·not_applicable_cases·error_cases를 함께 기록한다.
이는 0–1 원점수 집계이며 사람과 공통인 이진 통과율과 구분한다. 문맥 지표에는 통과 임계값을 적용하지 않는다.
MLflow SDK가 추가하는 native 지표와 위 공통 지표를 구분하며 비교·보고서는 위 공통 이름을 사용한다.
N/A·오류·미제출을 실패나 0점으로 섞지 않는다. 부분 사람 검수는 pending_cases를 남기고 RUNNING을 유지한다.
analysis의 κ는 같은 답변의 대응되는 정상 이진 판정만 사용하며, 정의되지 않으면 숫자를 만들지 않고 이유를 artifact에 남긴다.

## 상태와 저장 흐름

모델: 원본 검증 → 평가자별 프로필 고정 → 각 모델·반복별 grading 할당 → MLflow run 생성 →
그 모델의 차원별 scorer 실행 → 공통 labels·metric 저장 → FINISHED 또는 FAILED.
최초 Dataset 평가도 이 경로로 모델별 자식 run을 만든다. Judge만 반복하는 CLI도 같은 경로를 사용한다.

한 모델의 모든 차원을 한 번의 `mlflow.genai.evaluate()`에 전달한다. SDK가 질문과 Ragas 지표를 병렬 실행하고
결과를 모두 모은 뒤 공통 grading을 저장한다. 등록 scorer에서 고정한 모델 URI로 Ragas를 실행하는 어댑터가
차원별 입력을 분리하며, 사전 N/A는 모델을 호출하지 않는다. 한 항목의 호출 오류는 해당 판정에만
error로 남기고 다른 항목의 성공 결과는 보존한다. 오류가 있으면 grading은 FAILED다.
모델 간·반복 회차 간 실행은 순차로 유지한다.

동시성은 프로세스별 `MLFLOW_GENAI_EVAL_MAX_WORKERS`(질문, 기본 2, 범위 1–32)와
`MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS`(질문당 차원, 기본 4, 범위 1–16)로 제한한다.
Gold 기본값은 최대 8개, Iron은 질문당 한 차원이므로 최대 2개 동시 모델 요청이다.
Ragas 한 지표는 여러 요청을 할 수 있으며 지표 내부 LLM 요청을 직렬화해 SDK 상한이 유지되게 한다.
동시에 여러 worker 작업·CLI를 실행하면 호출 수는 합산되며, Gateway 전체의 전역 제한은 아니다.
실행에 사용한 두 값을 `judge-results.json`의 execution에 기록한다.
평가의 실행 단위인 scorer와 관측 단위인 trace는 다르다. SDK의 scorer tracing은 별도 옵션이며
병렬 실행 자체가 네 개의 독립 grading run이나 trace를 의미하지 않는다.

사람: 회차 시작 → 공통 grading 할당·MLflow run 생성(RUNNING) → 초안/최종 제출을 DB에 저장 →
명시적 동기화로 해당 grading run의 artifact·metric 갱신 → 모든 문항 제출 시 FINISHED.
진행 중인 같은 사람 회차는 이어서 사용하며 완료 후 새 회차를 만든다. 초안과 이전 점수를 새 회차에 복사하지 않는다.
MLflow 장애가 최종 제출을 잃게 하지 않으며, 같은 grading_id로 생성·동기화를 재시도한다.
완료한 사람 판정은 불변이고 새 회차에서만 다시 평가한다.

로컬 테이블은 evaluation_sources(고정 원본), grading_runs(공통 평가자·회차·원격 ID),
grading_cases(질문별 제출 labels), grading_measurements(Ragas 원점수), grading_drafts(사람 초안)로 구분한다.
기존 human_review_*와 혼합하지 않고 새 계약의 테이블·schema만 사용한다. 호환 마이그레이션·옛 metric backfill은 하지 않는다.

## 검수 UI와 분석

UI는 answers run만 평가 실행으로 조회한다. 본인의 사람 grading 회차·완료 횟수만 보여주며 모델/다른 사람의 점수는 숨긴다.
원본 answers run 링크와 별도로 현재 채점 run 링크를 관리자에게 제공해 metric과 artifact를 바로 찾을 수 있게 한다.
같은 원본의 grading run들을 선택해 analysis를 수행한다. 한 사람/모델의 같은 프로필 반복은 repeat_consistency,
같은 주체의 다른 프로필은 judge_revision, 다른 주체끼리는 inter_rater로 구분한다.
분석은 원본 답변/프로필/입력 run 목록을 확인하고, schema가 다른 예전 결과는 받지 않는다.

analysis의 `comparisons`는 사람–모델을 `purpose=human_alignment`로 우선 표시한다.
다른 목적은 human_reliability·model_agreement·repeat_consistency·judge_revision으로 구분한다.
Cohen’s κ는 Ragas DiscreteMetric을 통해 scikit-learn으로 계산한다.

`panels`는 차원별 전체 선택 집단(all), 혼합 집단 안에 각 유형이 세 주체 이상인 경우
humans_only·models_only를 포함한다. 집단마다 members에 rater·profile_digest·round를 기록한다.
한 주체의 여러 프로필/회차를 별개의 독립 평가자로 세지 않는다. 중복 주체가 있는 집단은
multiple_gradings_per_subject 사유와 주체 목록을 남기고 계수는 null이다. 유효한 다른 하위 집단의 계산은 계속한다.

패널은 최소 세 주체를 요구하며 α에도 실제 기여하는 주체가 최소 세 명이어야 한다. `fleiss_kappa`는 statsmodels의 Fleiss 방식이며 모든 선택 주체가
pass/fail을 준 문항만 사용한다. `krippendorff_alpha`는 krippendorff의 nominal 방식으로 두 주체 이상이
pass/fail을 준 문항을 사용한다. 최소 유효 문항 두 개는 Harness의 표본 정책이다.
complete_binary_cases·pairable_cases·각 방법의 제외 문항 수·N/A/오류/누락 판정 수를 함께 기록한다.
rater_coverage와 alpha_effective_subjects로 선택만 됐고 실제 유효 판정은 없는 주체를 구분한다.
계수는 음수도 허용하며, 상수 판정·표본 부족·비정상 수치는 null과 개별 사유로 남긴다.
N/A 적용 여부의 일치도는 쌍별 지표로 별도 기록하며 N/A를 pass나 fail 범주에 넣지 않는다.

`analysis_protocol`은 방법·명목 척도·표본/제외 정책·라이브러리 버전을 고정한다.
MLflow에는 유한한 값만 `agreement.<차원>.<쌍 식별자>.*`와
`agreement.panel.<차원>.<집단>.<구성 식별자>.*`로 기록하고 미정 계수를 0으로 채우지 않는다.
집단 계수는 보조 기술통계이며 사람–모델 κ를 대체하지 않는다.

## 조건 간 comparison

`comparison-v1`은 analysis A/B 한 쌍과 연결된 원본·채점 입력을 비교한다. `evaluation.kind=comparison`인
독립 run으로 만들며 단일 parent를 지정하지 않는다. baseline_analysis_run_id·candidate_analysis_run_id와
comparison_digest 태그로 양쪽 계보를 연결하고 baseline과 같은 experiment에 기록한다.
원본 answers·grading·analysis를 수정하거나 생성·Judge를 재호출하지 않는다.

입력 analysis는 FINISHED와 evaluation-v1 태그를 요구한다. 원본 종류·종료 상태·source 해시,
grading의 완결성과 답변 binding을 검사한다. grading-inputs로 analysis를 재계산해 agreement.json과 일치하는지
확인하므로 분석 정책·라이브러리 버전이 달라졌다면 먼저 analyze를 다시 실행해야 한다.
한 평가자당 한 grading만 허용하며 여러 답변 생성/채점 반복 묶음은 첫 버전 범위에 포함하지 않는다.

| purpose | 고정 조건 | 변경 허용 |
| --- | --- | --- |
| system | Dataset 해시·공통 문항 내용·rubric·평가자 집단과 프로필 | 서로 다른 answers run의 QA 설정·생성 결과 |
| judge | 동일한 answers bundle·rubric·평가자 ID 집단·사람 프로필과 판정 | 같은 모델 평가자 ID의 채점 프로필·회차 |

공통 문항 ID를 대응시키고 baseline_only·candidate_only를 기록한다. 같은 ID의 질문·이력·expectations가 다르면
거부한다. 서버 간 재등록으로 ID·Dataset 해시가 달라진 자료를 자동 의미 매칭하지 않는다.
QA 설정과 평가자 프로필의 차이·입력 해시를 저장한다. grading run의 프로필 원문을 조회할 수 있으면 해시를 검증하고
상세 변경도 보존한다. 파일 grading만 있는 경우 프로필은 해시로만 식별할 수 있다.
설정 변경이 하나의 인과 요인인지 자동으로 보장하지 않는다.

평균은 양쪽에 유효한 값이 있는 같은 문항에서 계산하며, 차이는 candidate−baseline이다.
N/A·오류·생성 실패·미측정을 0으로 바꾸지 않고 제외 문항과 이유를 보존한다. 생성 실패율은 공통 문항 전체에서
별도로 비교한다. system 품질 점수의 변화는 개선/악화, judge 점수와 캐시 입력 토큰 수의 변화는 단순 상승/하락으로
표시한다. Judge의 점수 상승을 답변 개선으로 해석하지 않는다. 자원 지표는 QA 생성의 시간·토큰·호출 수이며
Judge 호출 자원 사용량은 아니다.

평균 차이의 구간은 SciPy paired percentile bootstrap을 사용한다. 기본 seed=42·2,000회·95%이며
문항 ID가 재표집 단위다. 유효 문항 두 개 미만이면 구간을 만들지 않는다. 일정한 차이의 구간은 양 끝값이 같을 수 있다.
작은 공개 Gold의 문항 재표집 민감도를 보여주며 실제 사용자 모집단의 보장이나 답변/Judge 반복 변동을 포함하지 않는다.
κ·α의 신뢰구간·유의성 검정·자동 승자 선정은 제공하지 않는다.

일치도는 양쪽의 동일한 문항×평가자 이진 판정 교집합에서 다시 계산한다. analysis에 표시된 서로 다른 표본의 κ를
그대로 빼지 않는다. support에 원래 N/A·오류·누락 상태와 포함 여부를 저장하고 패널의 건수는 교집합 mask 적용 후의
표본을 뜻한다. 계산 불가 계수의 delta는 null이며 양쪽 사유를 보존한다.
comparison-result.json에는 요약·문항별 변화·대응 일치도·계산 조건을, comparison-inputs.json에는 재현할 입력을 담는다.
유한한 숫자만 comparison.* metric으로 기록한다.

이 문서는 구현 계약이며 실제 유료 모델 평가나 사람 정합성 실험의 완료를 의미하지 않는다.
