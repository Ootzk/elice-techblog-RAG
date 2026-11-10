# Notebook 실험 보고서

현재는 저장된 평가 결과를 확인하는 **reporting 초안**입니다. 공통 rubric에 따른 등급 채점·
Human Review·가중 κ 개편은 별도 작업으로 진행하며, 그에 따라 템플릿과 표시 구조도 바뀔 수 있습니다.
현재 모델의 `pass/fail`은 원점수 1.0을 기준으로 한 기존 규칙입니다. 사람의 rubric 판정과 같은
척도로 보정된 결과가 아니며, `FactualCorrectness(mode="recall")` 결과도 단순 필수 항목 포함률로
해석하지 않습니다. 이번 도구는 저장된 판정과 점수를 재계산하거나 재분류하지 않습니다.

MLflow의 `analysis`·`comparison` Run에 저장된 조건·지표·질문·답변·근거·판정을
hvPlot/Bokeh 그래프와 표로 표현합니다. 보고 단계에서 해석·결론을 새로 쓰거나 AI로 생성하지 않습니다.
템플릿을 복사해 입력 Run과 표시 범위를 설정하고 Panel의 `report_view`를 독립 HTML로 export합니다.
Knowledge와 별도의 Python 환경이며 서버·DB를 띄우지 않습니다.

## 시작하기

저장소 루트에서 실행합니다. Python 3.12와 uv가 필요합니다.

```bash
uv sync --project tools/reporting --locked
cp tools/reporting/notebooks/templates/analysis.ipynb tools/reporting/notebooks/reports/my-analysis.ipynb
uv run --project tools/reporting --env-file .env.dev jupyter lab
```

VS Code에서는 `tools/reporting/.venv/bin/python`을 Notebook 커널로 선택합니다.
Jupyter 실행 명령의 `--env-file`은 해당 프로세스에만 적용되므로 VS Code 커널에는
`MLFLOW_TRACKING_URI`와 필요한 인증 환경변수를 별도로 전달해야 합니다. Notebook에 실제 주소·키를 쓰지 않습니다.

| 템플릿 | 입력 | 기본 구성 |
| --- | --- | --- |
| `analysis.ipynb` | 완료된 analysis Run | 차원별 사람–모델 Cohen κ, 모델 간 비교, Fleiss κ·Krippendorff α, 라벨·질문·답변 |
| `comparison.ipynb` | 완료된 comparison Run | 조건 변경, 평가자별 품질 변화·신뢰구간, 자원 사용량, κ 변화, 문항별 전이·A/B 답변 |

템플릿은 `SOURCE_MODE = "mlflow"`로 시작하며 실제 완료된 Run을 선택합니다.
CI는 입력 모드를 명시적으로 `demo`로 바꿔 합성 fixture로 실행합니다. 모델 호출은 하지 않습니다.

## 실제 Run 불러오기

복사한 Notebook의 설정 셀을 수정하고 순서대로 실행합니다.

```python
SOURCE_MODE = "mlflow"
RUN_ID = "완료된-analysis-또는-comparison-run-id"
REPORT_TITLE = "실험 제목"
OUTPUT_HTML = ROOT / "artifacts/reports/my-analysis.html"
```

`RUN_ID`를 비워 실행하면 해당 유형의 최근 완료 Run 목록을 보여줍니다. 연결에는 MLflow SDK의
`MLFLOW_TRACKING_URI`, 선택적 `MLFLOW_TRACKING_USERNAME`·`MLFLOW_TRACKING_PASSWORD` 또는
`MLFLOW_TRACKING_TOKEN`을 사용합니다. `.env.dev`의 설정을 재사용할 수 있습니다.

Notebook 안에서 `import mlflow`와 `mlflow.MlflowClient()`로 추가 자료를 직접 조회할 수 있습니다.
공통 로더는 다음처럼 사용합니다.

```python
from elice_reporting import from_mlflow, analysis_frames

data = from_mlflow("analysis", RUN_ID, ROOT / "artifacts/reports/cache")
frames = analysis_frames(data)
```

로더는 Run·artifact를 읽고 캐시하며 스키마·입력 digest 연결을 확인합니다. Run 생성·평가 재실행·
Judge 호출·artifact 업로드는 하지 않습니다. `evaluation-v1`과 `comparison-v1` 계약을 사용하며,
원본 artifact의 집계 결과를 표로 펼칩니다. 점수나 신뢰도를 다시 계산하지 않고, 미정의 값도 0으로 바꾸지 않습니다.

MLflow 없이 파일로 읽으려면 `SOURCE_MODE = "local"`과 `LOCAL_REPORT`·`LOCAL_INPUTS`를 설정합니다.
comparison은 `comparison-result.json`·`comparison-inputs.json`을 그대로 사용합니다.
analysis는 `agreement.json`과 `{"bundle": <evaluation-bundle 내용>, "gradings": <grading-inputs의 results>}`
형태의 입력 JSON이 필요합니다. `profiles`에는 선택적으로 채점 프로필 해시→원문을 넣을 수 있습니다. `data.report`·`data.inputs`를 로컬 JSON으로 저장해 재사용할 수도 있습니다.

## 보고서의 표시 기준

Analysis는 평가 조건·범례 → 지표 정의 → 항목별 κ → 문항 선택·질문·답변·판정 순서입니다.
Comparison은 저장된 조건 → 요약 지표 → 주요 그래프 → 문항별 저장 결과 순서입니다.
Analysis의 평가 주체·지표 정의·판정 기준은 표로 표시합니다. 아코디언·하단 상세 데이터 부록은 두지
않습니다. 원본 answers 링크는 평가 조건에, 원본 grading 링크는 해당 평가자·회차·프로필의 행에 둡니다.
Comparison의 전체 표·개별 라벨·재현 프로토콜은 마지막 부록에 있습니다.
표는 HTML로 렌더링하고 브라우저에서 검색·정렬 기준·내림차순 컨트롤을 제공합니다.
접힌 영역에서 DataTable의 초기화 전 크기 조정이 전체 문서를 중단하는 문제를 피하기 위한 구성입니다.

- Analysis: 같은 답변 고정본의 일치율·Cohen κ·유효/제외 수와 항목별 평가 주체×평가 주체 κ heatmap을 표시합니다.
- Comparison: 시스템 변경은 같은 평가자의 저장된 A/B 점수·차이·구간·시간을 표시합니다.
  Judge 변경은 저장된 사람–모델 κ·차이를 우선 표시합니다.

Analysis의 평가 조건에는 데이터셋 유형·ID·내용 해시·문항 수, 원본 Run 링크와 응답 모델,
검색·생성·프롬프트 설정을 표시하고 그 하위에 평가 주체 범례를 둡니다. 특정 평가자 쌍을 주 비교로
고르지 않습니다. Comparison은 `FOCUS_RATER`·`FOCUS_METRIC`으로 요약 대상을 지정하며 미지정 평가자는
원본 순서의 첫 대상입니다. 여러 평가자의 κ를 평균 내거나 최고 점수를 고르지 않습니다.

Analysis의 문항 드롭다운에는 전체 문항이 저장 순서대로 들어갑니다. `CASE_IDS`로 목록을 제한할 수
있으며 없는 ID는 오류로 처리합니다. 화면에는 내부 ID 배열을 노출하지 않습니다. 선택한 문항의 질문·
답변·판정 격자를 기본 표시하며 오른쪽 패널에서 판정 사유를 읽습니다. 모든 문항을 HTML에 미리 포함하고
JavaScript로 표시를 전환하므로 Jupyter나 서버 연결 없이 문항을 바꿀 수 있습니다.
Comparison은 저장된 공통 문항의 A/B 결과를 카드로 확인합니다.

요약 카드는 원본 집계값을 형식화하며, 답변 수·표시 수 등은 저장된 레코드의 개수입니다.
점수·일치도·차이·구간을 다시 계산하지 않습니다. 미정의 값을 0으로 바꾸지 않으며
원본의 유효 표본·제외 수·계산 불가 사유를 확인할 수 있습니다.

저장된 비교 가설·Judge 판정 사유에 서술이 있으면 원문 그대로 표시합니다. 이는 보고서가 새로 작성한
해석이 아닙니다. 데모도 동일한 표시 경로를 사용하며 합성 데이터라는 표시만 추가합니다.
해석·결론·채택 판단을 입력하는 셀이나 자동 생성 기능은 두지 않습니다.

## Analysis heatmap

평가 조건의 범례에서 로고·사람 아이콘과 저장된 평가자 ID·회차·모델 정보를 연결합니다.
MLflow 로더는 원본 grading Run의 `rater-profile.json`도 읽고 해시·출처를 검증합니다.
모델명은 채점 프로필의 구체 모델 ID 또는 해당 grading Run에 속한 과거 trace 최대 3개의
`mlflow.llm.model` 관측값을 표시합니다. trace ID와 관측 모델은 로더의 grading 링크 정보에 보존합니다.
표본 관측이므로 Gateway에 여러 모델이 연결된 경우 모든 라우팅 대상을 열거한다는 뜻은 아닙니다.
Gateway 별칭만 있고 실행 관측도 없으면 구체 ID를 추정하지 않습니다. 현재 Gateway 설정은 조회하지 않습니다.
평가 주체 표에는 아이콘·평가자 ID·회차·관측 모델과 출처·원래 Gateway 별칭·프로필 해시·원본 Run 링크를 표시합니다.
같은 모델의 여러 회차는 별도 행으로 유지합니다. 아이콘은 로고로 계열을, 왼쪽 위 문자로 구체 모델
유형을 구분하고, 오른쪽 아래 숫자로 평가 회차를 표시합니다. 사람 ID마다 다른 색을 사용합니다.
Claude·OpenAI·Gemini 아이콘의 출처·라이선스는 [assets](src/elice_reporting/assets/README.md)에 있습니다.

- 항목별 κ: 각 평가 항목마다 평가 주체×평가 주체 행렬이며 저장된 계수를 사용합니다.
  보고서에서는 계수를 새로 계산하지 않습니다. κ의 대칭성을 이용해 대각선·하삼각을 생략하고 우상단만 표시합니다.
  Gold 4개 항목은 2×2로 배치하며 미정의·미기록 값은 회색입니다.
  heatmap과 오른쪽 공통 세로 colorbar는 같은 `RdBu` 팔레트·구간을 사용합니다.
  −1은 코럴, 0은 중립색, +1은 블루이며, 범례의 범위는 −1~+1로 고정합니다.
  hover에서 원본 κ·평가자·유효 표본·제외 수·사유를 확인할 수 있습니다.
- 문항별 판정: Gold는 correctness·completeness·groundedness·relevance의 고정 4행,
  평가 주체·회차·프로필별 N열입니다. 한 문항의 행렬은 κ 그래프 한 칸과 같은 크기입니다. Iron은 저장 계약에 따라 behavior 행을 사용합니다.
  pass·fail·N/A·오류·미기록을 각각 다른 색·기호로 구분합니다. 원점수로 판정을 다시 만들지 않고
  저장된 이진 판정을 표시하며, hover에는 판정·원점수·평가자·회차·항목을 표시합니다.

오른쪽 사유 패널은 `평가자 · 회차`와 `Judge 항목` 드롭다운을 제공합니다. 한 평가자와 전체 항목을
선택하면 그 회차의 네 사유를, 전체 평가자와 한 항목을 선택하면 모든 회차의 해당 항목 사유를 표시합니다.
둘 다 전체이면 평가자·회차별로 묶어 전체 사유를 표시합니다. 긴 사유는 패널 안에서 스크롤해 읽으며
원문 줄바꿈을 보존하고 HTML은 escape합니다. 두 필터도 독립 HTML에서 동작합니다.
문항별 `문맥 자료`에서 최종 생성 문맥의 제목·섹션·원문 링크·저장된 본문을 선택해 읽습니다.
답변의 인용 번호를 연결하지만 인용되지 않은 생성 문맥도 생략하지 않습니다. 검색됐어도 동일 본문이
생성 문맥에 전달되지 않은 자료는 `검색 전용`으로 구분합니다. 같은 chunk의 본문이 다르면 합치지
않으며, 생성 문맥의 빈 목록과 문맥 필드 미기록도 구분합니다. 본문은 원문 사이트를 다시 가져오거나
재구성하지 않고 저장된 스냅샷을 그대로 보여줍니다. 문맥 선택 역시 독립 HTML에서 동작합니다.
기본 선택은 첫 모델과 첫 항목입니다. Ragas의 저장된 NLI 결과는 주장·확인 여부·판정 사유 표로
표시하고, 완전성의 두 검증 방향은 구분합니다. 추출 문장과 후속 검증 문장이 순서까지 같을 때만
중복 추출 표시를 생략하며, 검증에 없는 추출 문장은 남깁니다. 이유를 새로 요약·번역·판정하지 않습니다.
검증 방향에는 `답변 내용 검증`·`필수 내용 누락 검사`라는 목적과 예시를 붙입니다. `확인 못함`은
대조 자료로 뒷받침하지 못했다는 모델 판정이며 사실 자체가 거짓이라는 의미로 확장하지 않습니다.
저장된 모델 통과 임계값이 1.0이면 문항·항목별 점수가 0.99여도 fail이라는 엄격한 프로젝트 규칙을
명시합니다. 지표의 반올림이 포함된 점수 기준이며 모든 내부 판정의 일치와 동일한 뜻은 아닙니다.
사람의 rubric 판정과 모델의 수치 임계값은 다른 규칙이므로 κ에 통과 기준 차이가 반영될 수 있음을
설명합니다. 보고서가 임계값을 바꾸거나 원본 결과를 재분류하지는 않습니다.
같은 OpenAI 계열의 Luna·Terra도 구체 모델 ID가 다르면 모델 유형 문자가 다르고, 같은 모델의
여러 회차는 유형 문자를 공유하며 회차 숫자만 달라집니다.

여러 회차를 평균내거나 다수결로 한 판정으로 만들지 않습니다. 문항별 원점수와 범주 판정은 회차별로
남기며, Cohen κ도 회차 쌍별 저장값을 표시합니다. 같은 평가자·같은 프로필의 여러 회차가 있으면
`반복 일관성` 뷰가 자동으로 추가되고 해당 회차들의 κ, 일치율, 유효/제외 수를 보여줍니다.
다른 프로필은 조건 변경이며 반복 일관성으로 묶지 않습니다. Fleiss κ·Krippendorff α의 주체 간 패널은
한 평가자당 한 회차를 선택한 별도 analysis를 사용합니다. 기존 분석기는 같은 주체의 여러 회차가
섞인 패널을 `multiple_gradings_per_subject`로 표시하며, 회차가 많은 평가자에게 가중치를 주지 않습니다.

선택한 문항의 질문·답변·격자·사유 패널은 기본 화면에 표시합니다. 별도의
답변·근거·판정 사유 접이식 카드는 두지 않습니다. 지표 정의는 Grading(개별 답변 채점)과 Analysis(평가자 간 일치도)로 나눕니다.
Grading 표 위에 원점수·통과 판정·N/A·오류/미기록을 bullet로 설명하고, 표에는 네 품질 항목을 둡니다.
Analysis 표에는 Cohen κ·Fleiss κ·명목형 α를 설명합니다. κ hover에는 계수·유효/제외 표본·사유만,
문항 hover에는 판정·원점수·평가자·회차·항목을 표시합니다. 사람 검수는
NaN 대신 수치 점수 없음으로 표시합니다. 모델 결과는 `ragas-v1` 계약만 허용하며 구형 결과는 거부합니다.

실제 MLflow 입력에서는 원본 Run의 experiment ID와 Run ID로 UI 링크를 만듭니다.
링크에는 인증정보·쿼리 토큰을 넣지 않습니다. 데모나 Run 연결 정보가 없는 로컬 입력은
가짜 링크를 생성하지 않고 원본 Run 없음 또는 링크 미기록으로 표시합니다.
링크로 MLflow 원본을 확인할 때에는 해당 서버에 접속할 수 있어야 합니다.

## Notebook 설정과 HTML export

Notebook마다 입력 Run·표시 범위·그래프 구성을 설정합니다. Analysis의 모든 행렬은 이동·확대·선택
도구를 제거하고 hover만 허용합니다. 표 검색·정렬·문항 선택도 HTML에서 브라우저 JavaScript로 동작합니다. Python callback이나 서버를 요구하는
위젯을 추가하면 정적 export에서 동작하지 않으므로 기본 템플릿은 해당 기능을 사용하지 않습니다.

마지막 셀은 `export_html(report_view, OUTPUT_HTML, ...)`을 실행합니다. Notebook의 임의 Markdown 셀이나
실행 이력을 자동으로 전부 export하는 방식은 아닙니다.

Bokeh·Panel 리소스를 HTML에 포함하므로 MLflow·Jupyter·CDN 연결 없이 열 수 있습니다.
템플릿의 export 셀은 지정한 HTML을 덮어쓰므로 실험별 파일명을 지정합니다.
MLflow에 결과를 올리고 싶을 때만 템플릿 끝의 `client.log_artifact(...)` 예제를 명시적으로 실행합니다.

## 파일 관리와 검증

- `notebooks/templates/`: 공통 시작 템플릿.
- `notebooks/reports/`: 실험별로 작성한 Notebook 소스. 필요한 보고서만 Git으로 관리합니다.
- `artifacts/reports/`: 다운로드 캐시·HTML·실행 결과. Git에서 제외합니다.

Notebook을 커밋하기 전 실행 출력, 실행 번호, widget 상태를 지웁니다. 출력에는 실제 답변과
비공개 실험 정보가 들어갈 수 있으며 HTML 공유 전에도 내용을 확인합니다. 환경 파일·API 키·
다운로드 원문을 Notebook에 포함하지 않습니다.

```bash
uv run --project tools/reporting ruff check tools/reporting
uv run --project tools/reporting ruff format --check tools/reporting
uv run --project tools/reporting pytest tools/reporting/tests -q
```

테스트는 두 템플릿을 새 커널에서 데모 데이터로 실행하고 HTML 생성·외부 리소스 미참조를 확인합니다.
모델 호출이나 실제 MLflow 쓰기는 CI에서 수행하지 않습니다.
