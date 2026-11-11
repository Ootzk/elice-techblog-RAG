# Evaluation Reporting

MLflow analysis·repeatability·comparison run에 저장된 값·rubric·질문·답변·문맥·사유를 Notebook과
hvPlot/Bokeh로 표시하고 Panel 독립 HTML로 export한다. 평가 점수를 재계산하거나 해석을 생성하지 않는다.
Knowledge와 별도 Python 환경으로 사용하며 서버·DB를 배포하지 않는다.

## 시작하기

저장소 루트에서 실행한다. Python 3.12·uv가 필요하다.

```bash
uv sync --project tools/reporting --locked
cp tools/reporting/notebooks/templates/analysis.ipynb tools/reporting/notebooks/reports/my-analysis.ipynb
uv run --project tools/reporting --env-file .env.dev jupyter lab
```

VS Code에서는 `tools/reporting/.venv/bin/python`을 커널로 선택하고 MLflow 접속 환경변수를
커널에 전달한다. 실제 주소·키는 Notebook에 쓰지 않는다.

| 템플릿 | 입력 | 표시 내용 |
| --- | --- | --- |
| analysis.ipynb | 완료된 analysis run | 평가 조건·평가 주체·rubric·가중 κ·문항별 등급·사유·문맥 |
| repeatability.ipynb | 같은 조건의 완료된 repeatability run | analysis와 같은 평가 조건 범주·Gold의 두 rubric 경계를 비교하고 경계별 answers 매트릭스를 2열로 배치 |
| comparison.ipynb | 완료된 comparison run | 조건 차이·공통 관측의 등급/자원 변화·구간·정합성 차이·A/B 답변 |

템플릿을 복사한 뒤 설정 셀을 바꿔 순서대로 실행한다.

```python
SOURCE_MODE = "mlflow"
RUN_ID = "완료된-run-id"
REPORT_TITLE = "실험 제목"
OUTPUT_HTML = ROOT / "artifacts/reports/my-analysis.html"
```

RUN_ID를 비우면 최근 완료 run 목록을 표시한다. 기본은 실제 MLflow이며 `demo`는 명시적으로 선택한다.
연결은 `MLFLOW_TRACKING_URI`와 필요한 username/password 또는 token 환경변수를 사용한다.
`import mlflow`·`mlflow.MlflowClient()`로 추가 조회할 수 있다.

```python
from elice_reporting import from_mlflow, analysis_frames

data = from_mlflow("analysis", RUN_ID, ROOT / "artifacts/reports/cache")
frames = analysis_frames(data)
```

로더는 읽기·다운로드만 한다. run 생성·Judge 호출·평가 재실행·업로드는 하지 않는다.
answers는 evaluation-v1, grading·analysis·repeatability·comparison은 evaluation-v2, 채점은 rubric-v1을 요구한다.
rubric·입력·프로필 hash를 확인하며 이전 이진 계약은 거부한다. 형식 변환·호환 코드는 유지하지 않는다.
실제 새 등급으로 재채점·사람 검수가 끝난 analysis가 있어야 실제 보고서를 만들 수 있다.
`synthetic_grading_run_ids`가 있는 개발용 analysis에는 모의 검수 경고를 표시하고,
해당 grading의 `review.provenance=synthetic` 태그를 읽어 평가자 범례에도 구분한다.

로컬 모드는 `SOURCE_MODE="local"`과 LOCAL_REPORT·LOCAL_INPUTS를 설정한다.
comparison은 comparison-result.json·comparison-inputs.json을 사용한다. analysis는 agreement.json과
`{"bundle": <answers bundle>, "gradings": <grading manifest 목록>}`을 사용하며 profiles에
프로필 hash→원문을 선택적으로 포함한다. 다운로드한 data.report·data.inputs도 저장해 사용할 수 있다.
repeatability는 repeatability-result.json·repeatability-inputs.json을 사용한다.
보고서는 Gold의 두 경계를 별도 구역에 표시하고, 각 구역의 answers 반복 매트릭스를
2열 그리드에 순서대로 배치한다. 홀수 시행이면 마지막 행은 한 칸만 사용한다.
질문 선택에서 전체 문항의 합산 분포 또는 개별 문항의 분포를 볼 수 있다. 개별 문항은
입력 analysis에 고정된 등급 건수를 읽으며, 선택 시 두 경계와 모든 answers 매트릭스를 함께 갱신한다.
평가 주체 아이콘·모델명은 범례와 행렬에 표시한다. 셀 hover에는 원점수·충족 건수/유효 건수와
N/A·오류·미기록을 표시한다. 별도의 건수·시행 간 범위 표는 두지 않는다.
시행 간 평균 변화폭은 저장된 질문 대응 비교값으로 표시한다. 경계 판정을 0/1로 바꾼
Bernoulli 분포의 1차 Wasserstein 거리 `W₁ = |p_i-p_j|`를 닫힌식으로 계산한 값이다. Gold의 두 경계는 각각 `%p` 단위의
전체값과 평가 주체·항목별 값을 보여준다. 유효 등급이 없는 시행 쌍은 비교 가능 건수에서 제외한다.
지표 정의와 판정 기준은 입력 analysis의 rubric snapshot을 그대로 읽고, 경계 충족률·평균 변화폭의
계산 단위와 제외 규칙을 별도 표·설명으로 표시한다. 합격 임계값은 설정하지 않는다.
평가 조건의 주체별 표에는 시행마다 grading run 횟수·채점 가능 문항 수·항목당/전체 판정 건수를
보여준다. 두 시행의 grading 횟수가 달라도 보고서를 만들며 해당 주체에 차이를 표시한다.
문항별 결과는 원본 analysis에서 확인한다. 입력 analysis에 `distributions`가 있어야 한다.

## Analysis 표시 기준

평가 조건 → 평가 주체 범례 → Grading/Analysis 지표 정의 → 항목별 κ → 문항별 평가 순서다.
조건에는 데이터셋·원본 answers 링크·QA 설정·rubric 정보를 표시한다. 원본 grading 링크는
평가 주체의 해당 회차 행에 둔다. 불필요한 아코디언·하단 상세 부록은 두지 않는다.

- Grading: 저장된 공통 rubric의 항목별 0·1·2 anchor를 표로 표시한다. Iron은 0·1이다.
  등급·N/A·오류·미기록을 구분하며 비율 점수로 등급을 재생성하지 않는다.
- Analysis: 선형 가중 Cohen κ를 보고서에 표시한다. 일반/제곱 가중 κ와
  Fleiss κ·Krippendorff α는 원본 artifact의 보조 결과로만 보존한다.
- 항목별 κ: 드롭다운의 전체 항목은 Gold 네 행렬을 2×2로 보여주고, 개별 항목은 행렬 하나만 보여준다.
  우상단만 표시하며, 오른쪽 colorbar와 동일한
  RdBu 팔레트·−1~+1 범위를 사용한다. 미정의 값은 회색이며 hover는 계수·유효/제외 수·사유를 담는다.
- 문항별 등급: 평가 주체·회차 N행 × 항목 4열의 행렬이다. 열 너비는 고정하고 높이는 평가 주체 수에 맞춘다.
  Gold 0은 코럴·1은 앰버·2는 블루, Iron 0은 코럴·1은 블루다. 사람도 숫자 등급을 표시한다.
- 문항 드롭다운으로 질문·답변을 바꾸고, 우측의 평가자/항목 드롭다운으로 저장된 사유를 읽는다.
  항목명은 그룹 제목에 한 번만 표시하고 각 표의 행은 평가자 아이콘·회차 | 사유 | 점수로 구성한다.
  한쪽 또는 양쪽 전체를 지원하며 두 항목 모두 전체면 항목별 표에 모든 평가자·회차 행을 표시한다.
- 참고 자료는 실제 generation_sources 전체와 인용 번호를 연결한다. 인용 자료·미인용 참고 자료·검색 후보를 구분하고
  저장된 본문만 표시한다. 원문 사이트를 다시 가져오거나 누락된 자료를 추정하지 않는다.
  제공된 자료가 0개이거나 기준상 답변 근거 부족·실제 유보 응답이면 안내를 표시한다.
  전달된 자료 개수와 답변 근거 확보 여부를 구분하며, 사실 주장 없는 유보는 근거 충실도 N/A,
  근거 없는 사실 주장은 0점이라는
  기준을 안내하고 유보의 적절성은 질문 적합성에서 보도록 한다. 미기록 문맥을 0개로 표시하지 않는다.

그래프의 이동·확대와 영역 선택은 끈다. 문항별 행렬의 셀을 클릭하면 해당 평가자·항목을,
왼쪽 평가자 아이콘은 해당 평가자·전체 항목을, 위 항목명은 전체 평가자·해당 항목을 선택한다.
좌상단은 양쪽 모두 전체로 바꾼다. 사유 패널의 두 드롭다운과 연동하며 κ 행렬은 hover만 허용한다.
표 검색·정렬·문항·사유·문맥 선택은
브라우저 JavaScript로 동작하며 Python callback이나 연결된 서버를 요구하지 않는다.

조건 표에는 모델·문항 수·검색/자료/대화 예산·온도·프롬프트 버전을 표시한다.
평가 조건 범주 드롭다운에서 데이터셋, 답변·검색, Corpus·청킹, 임베딩·색인,
키워드 검색·Agent, 생성·실행 환경, 평가 주체를 하나씩 선택한다. 독립 HTML에서도 전환된다.
QA 프로필에 저장된 `experiment_conditions`가 있으면 청킹·임베딩·색인·BM25·agent·실행 환경도 표시한다.
저장되지 않은 상세 조건은 미기록으로 표시하며 현재 DB나 실행 환경에서 복원하지 않는다.
Dataset·인덱스·프로필 해시는 화면에 나열하지 않고 원본 실행 링크에서 확인한다.
평가 항목은 표·차트·드롭다운에 같은 한국어 이름·이모지·색을 사용한다.
행렬은 둥근 타일과 내부 제목을 함께 렌더링한다. 문항별 행렬은 4개 평가 항목을 열로,
평가자·회차를 행으로 놓아 열 너비를 고정하고 행 수에 맞춰 높이를 늘린다.
평가 주체는 사람·모의 검수, ChatGPT, Claude, Gemini 순으로 묶고 같은 모델은 설정·회차순으로 둔다.
평가 주체가 늘면 셀 크기를 유지하며 행렬을 확장하고, 넓은 그래프는 가로 스크롤로 본다.

범례는 로고·읽을 수 있는 모델명·회차·원본 채점 링크를 연결한다. A/B 모델 유형 코드는 사용하지 않는다.
Luna·Terra처럼 같은 계열의 모델도 이름과 색으로 구분하고 회차 숫자는 아이콘 오른쪽 아래에 둔다.
사람은 검수자 번호와 색으로 구분한다. 모델명은 채점 프로필 또는 해당 run의 과거 trace 최대 3개에서
읽으며 현재 Gateway 설정으로 추정하지 않는다. 관측되지 않은 모델의 이름을 임의로 보완하지 않는다.
질문·답변은 구분된 틀로 보여주고 답변·참고 자료는 저장된 Markdown을 렌더링한다.
원문 HTML·script는 실행하지 않고 원격 이미지를 자동 로드하지 않는다.
아이콘 라이선스는 [assets](src/elice_reporting/assets/README.md)에 있다.

반복 회차는 별도 행·열을 유지한다. 평균·다수결로 합치지 않으며 κ도 회차 쌍별 저장값을 표시한다.
같은 주체·같은 프로필의 여러 회차가 있으면 반복 일관성 뷰를 추가한다.
다자 패널은 주체당 한 회차만 선택한 analysis를 사용한다. 중복 주체가 있으면 계수를 생략한 원본 사유를 표시한다.

## HTML export와 관리

마지막 셀의 `export_html(report_view, OUTPUT_HTML, ...)`이 선택한 파일을 덮어쓴다.
Notebook 전체 실행 이력·Markdown 셀을 자동으로 모두 export하지 않는다.
Bokeh·Panel 리소스를 HTML에 포함하므로 MLflow·Jupyter·CDN 없이 열 수 있다.
원본 run 링크를 열 때만 MLflow 서버 접속이 필요하다. MLflow 업로드는 템플릿 끝 예제를 명시적으로 실행한다.

- notebooks/templates/: 공통 템플릿.
- notebooks/reports/: 템플릿을 복사해 작성한 실험별 소스. 필요한 것만 커밋한다.
- artifacts/reports/: 다운로드 캐시·HTML·실행 결과. Git에서 제외한다.

Notebook 커밋 전 출력·실행 번호·widget 상태를 지운다. 키·환경 파일·다운로드 원문을 포함하지 않는다.
HTML에는 실제 질문·답변·판정이 들어가므로 공유 대상을 확인한다.

```bash
uv run --project tools/reporting ruff check tools/reporting
uv run --project tools/reporting ruff format --check tools/reporting
uv run --project tools/reporting pytest tools/reporting/tests -q
```

테스트는 합성 ordinal fixture와 새 커널에서 두 Notebook 실행·HTML export·외부 리소스 미참조를
검증한다. 데모를 실제 실험처럼 표시하지 않으며 모델 호출·실제 MLflow 쓰기를 CI에서 하지 않는다.
