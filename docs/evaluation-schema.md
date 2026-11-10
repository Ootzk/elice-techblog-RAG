# 평가 저장 스키마

답변 생성과 채점을 분리해 같은 답변을 여러 주체가 여러 번 평가한다. 답변 고정본은
`evaluation-v1`, grading·analysis·comparison은 `evaluation-v2`를 사용한다. 등급 채점 계약은
`rubric-v1`이다. 이전 이진 채점 결과는 조회·분석에서 제외하고 변환·backfill하지 않는다.
기존 answers는 재사용하며 원격 실행·DB의 옛 기록은 자동 삭제하지 않는다.

## Run과 계보

```text
answers A1: 고정된 질문·기대 조건·답변·문맥
├─ grading: model:chatgpt / 프로필 P / 1회차
├─ grading: model:chatgpt / 프로필 P / 2회차
├─ grading: model:claude / 프로필 Q / 1회차
├─ grading: human:<로그인 코드 ID> / 프로필 H / 1회차
└─ grading: human:<로그인 코드 ID> / 프로필 H / 2회차

analysis: 같은 A1에 연결된 지정 grading들의 정합성
comparison: 조건 A/B의 analysis·answers·grading 비교
```

| 종류 | 단위 | 주요 artifact | Metrics |
| --- | --- | --- | --- |
| answers | 데이터셋의 답변 생성 1회 | evaluation-bundle.json, evaluation-results.json | 답변 가능 여부·생성 자원·시간 |
| grading | 한 주체가 answers 전체를 채점한 1회 | grading-result.json, rater-profile.json | rubric 등급 평균·분포·상태별 건수, 선택적 Ragas 진단 |
| analysis | 같은 답변에 대한 지정 grading 집합 | agreement.json, grading-inputs.json | 항목별 일치율·가중 κ·다자 계수 |
| comparison | analysis A/B 한 쌍 | comparison-result.json, comparison-inputs.json | 대응 평균 차이·bootstrap 구간·문항 전이·공통 표본의 계수 차이 |

`evaluation.kind` 태그로 종류를 구분한다. grading은 `mlflow.parentRunId`와 source_run_id로
answers에 연결한다. analysis·comparison은 입력 run과 해시를 artifact로 기록하며 원본을 수정하지 않는다.

## 고정 답변과 rubric

answers bundle에는 Dataset ID·내용 해시·문항 records·실제 생성 cases·QA 프로필을 보존한다.
문항 ID는 중복되지 않고 records와 cases의 집합이 같아야 한다. bundle digest는 digest 필드 외의
전체 내용을 해시한다. 채점 시 source_run_id·bundle digest·문항별 answer_digest를 검증한다.

공통 rubric은 `apps/knowledge/src/elice_knowledge/data/rubrics/`의 버전 있는 JSON이다.
문항별 모범 답안·필수 요점·금지 주장·근거는 Dataset expectations에 둔다.
공통 rubric을 문항마다 복제하지 않으며 answers에 새 rubric을 고정하지 않는다.
과거 answers의 부가 rubric 필드는 채점 기준으로 사용하지 않는다.

| 데이터셋 | 기본 rubric | 항목 | 척도 |
| --- | --- | --- | --- |
| Gold | gold-ordinal-v1 | correctness, completeness, groundedness, relevance | 0 부족·1 부분 충족·2 충분 |
| Iron | iron-binary-v1 | behavior | 0 미충족·1 충족 |

사람과 모델 모두 같은 항목별 anchor로 직접 채점한다. 비율 점수의 반올림·ceil·pass/fail 임계값을
사용하지 않는다. N/A·오류·미제출은 등급 0과 다르다. applicability로 정해진 N/A는 강제하며
relevance·behavior에는 N/A를 허용하지 않는다. 허용 자료는 항목별로 분리한다.

- 정확성: 질문·답변·기대 답안·금지 주장.
- 완전성: 질문·답변·필수 요점. 선택 사항의 누락은 감점 기준이 아니다.
- 근거 충실도: 답변과 실제 `generation_sources`. Gold 답안·검색 후보·외부 지식으로 보충하지 않는다.
- 질문 적합성: 질문·고정 이력·답변. 기대 답안 없이 요청 대응을 본다.
- 행동 준수: 질문·이력·실제 답변·agent 단계와 기대 행동·필수 요점·실패 조건.

## 프로필과 회차

사람 프로필에는 rubric 원문·rubric_digest·contract를, 모델 프로필에는 여기에 등록 scorer 버전·
모델 URI·instructions·Ragas/SDK 설정·구현 해시·diagnostics 여부를 포함한다.
모델 instructions가 해당 rubric과 일치하지 않으면 실행을 거부한다. 같은 답변을 다른 rubric으로
채점할 수 있지만 서로 다른 rubric hash를 섞은 analysis·comparison은 거부한다.

식별 단위는 source_run_id + rater_id + rater_profile_digest + round다. round는 PostgreSQL 공통
레지스트리에서 원자적으로 할당한다. 서로 다른 프로필은 각각 1회차부터 시작할 수 있으므로 검수
API의 식별자는 회차 숫자가 아닌 유일한 grading_id(UUID)다. MLflow run ID와 grading_id를 연결해
동기화 재시도가 새 회차를 만들지 않게 한다. 같은 주체·같은 프로필의 반복과 프로필 변경을 구분한다.

모델 요청은 공급자 기본 샘플링을 사용한다. ChatGPT는 스트리밍·8192 completion tokens·
reasoning_effort=none, 나머지는 비스트리밍·2000 completion tokens를 사용하며 프로필에 기록한다.
부분 JSON·길이 제한 종료·허용 범위 밖 점수는 정상 등급으로 채택하지 않는다.
모델 별칭이나 기록된 조건이 가중치 고정·완전한 결정성을 보장하지 않는다.

## 질문별 등급과 Metrics

manifest는 schema·contract·source_run_id·source_bundle_digest·grading_id·평가자·프로필 hash·
회차·문항 해시와 ratings를 포함한다. rating의 기본 형식은 다음과 같다.

```json
{
  "case_id": "gold-001",
  "dimension": "correctness",
  "score": 1,
  "status": "scored",
  "rationale": "핵심 비교는 맞지만 부가 수치의 적용 조건이 부정확하다."
}
```

`score`는 rubric에 정의된 정수만 허용한다. Boolean·소수·숫자 문자열을 점수로 받지 않는다.
`not_applicable`·`error`는 score=null이다. 사람 초안의 pending은 최종 ratings에 제출하지 않는다.
모델 오류는 error와 오류 사유를 보존하며 해당 grading을 FAILED로 종료한다. 성공한 항목은 남긴다.

| Namespace | 의미 |
| --- | --- |
| rubric.<항목>.mean | scored 등급의 평균. Gold 0~2, Iron 0~1 |
| rubric.<항목>.score_N_count | 각 등급의 건수 |
| rubric.<항목>.*_count | scored·not_applicable·error·pending 상태별 건수 |
| ragas.<진단>.mean | 별도로 요청한 유효 진단 비율의 평균. 등급이 아님 |

적용 가능한 등급이 없으면 평균을 만들지 않는다. 항목들을 단일 총점으로 합치지 않는다.
선택적 diagnostics는 `--diagnostics`로 켜며 프로필의 일부다. 기본값은 꺼짐이다.
누적/최종 문맥의 ContextRecall·ContextPrecision, FactualCorrectness precision,
필수 요점 coverage(ContextRecall의 reference=필수 요점, contexts=답변), Faithfulness를
`measurements`에 저장한다. 필수 요점 coverage에는 FactualCorrectness recall을 쓰지 않는다.
답변 주장→기준과 기준→답변 검증에서 얻는 TP/FN을 섞은 비율은 필수 요점의 직접 포함률과 다르기 때문이다.
계약은 `ragas-diagnostics-v1`이며 primary ratings와 섞지 않는다.

## 실행과 사람 검수

한 모델 run에 그 모델의 모든 문항·항목을 넣는다. MLflow SDK가 질문·항목을 병렬 실행하고 결과를
모아 grading을 게시한다. 기본 상한은 프로세스당 질문 2개 × 항목 4개이며 모델·회차는 순차 실행한다.
모든 4항목이 별도의 고정 graph span인 것은 아니다. Judge 내부 모델 호출은 tracing 대상이다.

Human Review는 현재 로그인한 코드로 채점하며 타인·모델의 점수와 사유를 UI/API에서 제외한다.
rubric snapshot은 회차 시작 시 고정한다. 초안은 revision 충돌을 검사해 저장하고, 최종 제출은
질문 단위로 원자적·불변이다. DB에 먼저 저장하고 본인 grading run에 artifact·metric을 동기화한다.
부분 제출은 RUNNING, 전부 제출하고 동기화하면 FINISHED다. 연결 실패 시 같은 run에 재시도한다.
자세한 API는 [Human Review](human-review.md)를 따른다.

## Analysis

같은 source bundle·rubric의 ratings를 분석한다. 주요 지표는 고정된 전체 척도를 사용하는
**선형 가중 Cohen κ**다. 일반 κ·제곱 가중 κ·정확 일치율·평균 절대 등급 차이·혼동행렬·유효 쌍 수·
N/A 적용 여부 일치도도 보존한다. κ는 scikit-learn, Fleiss κ는 statsmodels,
순서형 Krippendorff α는 krippendorff로 계산한다. 라이브러리 버전과 제외 정책을 analysis_protocol에 기록한다.

사람–모델을 주요 정합성 비교로 두고 사람–사람·모델–모델·동일 주체 반복을 구분한다.
여러 회차를 평균내거나 다수결로 합치지 않는다. 다자 패널은 한 주체당 한 프로필·한 회차만 선택한다.
같은 주체의 grading이 여러 개면 쌍별 분석은 하되 패널 계수는 계산하지 않고 사유를 남긴다.

Fleiss κ는 명목형 정확 일치의 보조 지표로 모든 선택 주체가 채점한 문항을 사용한다.
ordinal α는 문항마다 최소 두 유효 등급이 있는 자료를 사용하되, 패널에 기여하는 주체는 최소 세 명이어야 한다.
N/A·error·미제출을 제외하고 건수를 별도 보고한다. 유효 표본이 너무 적거나 변동이 없으면
계수는 null과 사유로 남기며 metric에 임의의 0을 기록하지 않는다.

## Comparison과 보고서

system 비교는 평가자·rubric을, judge 비교는 동일 답변·사람 판정을 고정한다.
공통 관측의 등급·선택적 진단·QA 자원 사용량을 대응 비교하며 SciPy paired bootstrap 구간을 저장한다.
κ·α는 공통 문항·평가자 관측에서 재계산한다. 다른 표본의 계수를 단순 차감하지 않는다.
입력·설정 차이·제외 사유·계보를 보존하고 모델을 재호출하지 않는다.

보고서는 저장된 값·사유·원문 문맥·rubric anchor만 표현한다. 점수를 재계산하거나 해석을 자동 생성하지 않는다.
새 계약의 실제 사람 정합성과 반복 일관성은 새 등급으로 검수한 자료로 별도 측정해야 한다.
