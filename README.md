# elice-techblog-RAG

> 엘리스 - AI Engineer (Platform) 포지션 미니 프로젝트: 엘리스 테크블로그 컨텐츠(한국어)를 이용한 RAG 시스템 구축 및 자체 Eval Harness 설계

## 1. 실행 방법과 환경 변수

### 설치

### 환경 변수 설정

### 문서 수집과 색인

### 서버 실행과 질문 예시

### 평가 실행

---

## 2. 데이터셋과 기술 스택 선정 이유

### Corpus: [엘리스 테크블로그 컨텐츠(한국어)](https://elice.io/ko/resources/blog?filter=tech_blog)

제시된 Corpus 유형 중 무엇으로 할 지 고민하다가, 엘리스 미니 프로젝트인만큼 엘리스 블로그에 공개된 컨텐츠들을 사용하면 참가자 입장에서는 기술 면접 대비 공부도 자연스럽게 되면서 면접관의 관심을 끌기 좋고, 실제 면접관 입장에서는 익숙한 도메인인 만큼 RAG 시스템 퀄리티를 평가하기도 좋을 것 같아 서로에게 재미있는 주제라고 생각이 되어 골랐습니다.

블로그 컨텐츠는 엘리스 소식/블로그/테크블로그 세 가지 카테고리로 나뉘어져 있음을 확인했고, 그 중 엘리스 소식 부분은 엘리스 구성원들의 이야기가 주로 담겨있는 만큼 개인정보 부분에서 문제가 될 것 같아 제외했고 (비록 public으로 공개되어 있지만), 블로그는 일반적인 IT 기술 트렌드나 용어 설명들을 담고 있지만 그만큼 AI가 미리 학습한 지식을 이용하여 대답할 가능성이 있어 제외했습니다.
테크블로그 컨텐츠의 경우 실제로 엘리스에서 독자적으로 수행한 실험/구축/개발에 관한 내용이기 때문에, 검색 시스템이 제대로 동작하지 않으면 AI Hallucination이 일어나기 쉬운 구조라 시스템의 퀄리티를 측정하기에 좋다고 생각하여 테크블로그 카테고리만 사용하기로 결정했습니다.

블로그 컨텐츠는 또한 세 가지 언어(한국어, 영어, 일본어)를 지원하는 것을 확인했는데, 한국어가 최신 글들을 많이 포함하고 있고 아직 외국어로는 번역되지 않은 컨텐츠가 있음을 확인했기 때문에 한국어로 작성된 글들만 대상으로 하기로 결정했습니다. (무엇보다 우리 모두 한국인이기 때문에...)
:warning: 블로그 페이지에서 글들을 오갈 때 언어설정이 영어로 불규칙하게 바뀌는 현상이 있습니다.

현실적으로는 블로그 내용들이 모두 인터넷에 공개되어 있어 일반적인 웹 검색으로 블로그 컨텐츠를 쉽게 검색할 수 있지만, 이번 미니 프로젝트에서는 이 블로그의 내용들이 공개되어 있지 않다고 가정하고 문서 Ingest, Chunking, Embedding 등 RAG에 필요한 구성 요소들을 구현하도록 하겠습니다.

### 모델과 프레임워크

현재 결정한 구성은 아래와 같습니다.

| 역할 | 선택한 기술 |
| --- | --- |
| 지식 데이터와 벡터 저장 | PostgreSQL + pgvector |
| Embedding 모델 | BGE-M3 |
| 개발용 Embedding 추론 | 홈서버 Ollama |
| 실험·평가 관리 | MLflow Dataset, Evaluation Run, Prompt와 AI endpoint 설정 |

## 3. 시스템 아키텍처

### 서비스와 데이터 흐름

아래는 지금까지 결정한 내용을 바탕으로 그린 설계안입니다. 아직 서비스 구현 전이며,
Knowledge와 RAG를 하나의 프로세스로 실행할지 나눌지는 정하지 않았습니다.

```mermaid
flowchart TB
    User(["👤 사용자 · 면접관"])
    Blog[/"🌐 엘리스 한국어 테크블로그"/]
    Frontend("🖥️ 프런트엔드 앱")

    subgraph Knowledge["⚙️ Knowledge / RAG — 논리적 구성"]
        Ingest[["🔄 지식 갱신<br/>원본 감시 · 수집 · 정제 · 청킹"]]
        RAG("💬 RAG QA<br/>검색 · 답변 생성 · Citation")
        Adapter[["🔌 Embedding 어댑터"]]
    end

    subgraph PostgreSQL["🐘 PostgreSQL — DB와 계정 분리"]
        KnowledgeDB[("Knowledge DB<br/>문서 · 버전 · 청크 · pgvector")]
        MLflowDB[("MLflow 메타데이터 DB")]
    end

    subgraph Inference["🧠 모델 추론"]
        Embedding("BGE-M3<br/>Embedding 추론 서버")
        Models("생성 · Judge 모델<br/>추론 endpoint")
    end

    subgraph Evaluation["📊 평가와 실험 관리"]
        Eval[["🧪 Eval Harness<br/>평가 실행 · 지표 수집 · 보고서 생성"]]
        MLflow("MLflow<br/>Dataset · Prompt · AI endpoint 설정<br/>Evaluation Run · Trace")
    end

    Artifacts[("📦 영속 artifact 저장소")]
    Report[/"📄 실험 보고서<br/>Before / After · 사례 분석"/]

    User --> Frontend
    Frontend -->|질문| RAG
    RAG -->|답변과 출처| Frontend
    Blog --> Ingest
    Ingest -->|문서와 벡터 저장 · 갱신| KnowledgeDB
    Ingest -->|문서 임베딩| Adapter
    RAG -->|질의 임베딩| Adapter
    Adapter --> Embedding
    RAG -->|근거 검색| KnowledgeDB
    RAG -->|답변 생성| Models
    Eval -->|평가 질의 실행| RAG
    Eval -->|Judge 사용 시| Models
    Eval <-->|평가 설정 · 실행 결과| MLflow
    RAG -.->|Prompt 조회 · 선택적 Trace 기록| MLflow
    MLflow --> MLflowDB
    MLflow --> Artifacts
    User -->|평가 설정과 결과 확인| MLflow
    Eval --> Report

    classDef person fill:#f1f5f9,stroke:#475569,color:#0f172a,stroke-width:2px
    classDef service fill:#eff6ff,stroke:#2563eb,color:#1e3a8a
    classDef storage fill:#f0fdf4,stroke:#16a34a,color:#14532d
    classDef model fill:#faf5ff,stroke:#9333ea,color:#581c87
    classDef evaluation fill:#fff7ed,stroke:#ea580c,color:#7c2d12
    classDef document fill:#fffbeb,stroke:#ca8a04,color:#713f12

    class User person
    class Frontend,Ingest,RAG,Adapter service
    class KnowledgeDB,MLflowDB,Artifacts storage
    class Embedding,Models model
    class Eval,MLflow evaluation
    class Blog,Report document
```

👤는 사용자, 둥근 상자는 서비스와 추론 endpoint, 이중 테두리 상자는 처리 모듈,
원통은 저장소, 기울어진 상자는 외부 입력과 보고서를 나타냅니다.
실선은 주요 호출·데이터 흐름이고, 점선은 Prompt 조회·선택적 Trace 기록 연결입니다.

Knowledge는 문서와 수집 상태를 관계형 데이터로 관리하고, 청크 벡터도 같은 PostgreSQL의
pgvector에 저장합니다. 문서를 색인할 때와 질문을 검색할 때는 같은 Embedding 프로필을 사용합니다.
생성 모델과 Judge 모델의 구체적인 선택, 프런트엔드와 RAG의 프레임워크는 아직 정하지 않았습니다.

MLflow에는 평가 Dataset과 Prompt, AI endpoint 설정을 준비합니다.
Eval Harness는 이 설정을 이용해 평가를 실행하고, 질문별 결과와 주요 지표를 기록해 보고서로 정리합니다.
지표와 판정 기준은 직접 설계하며, MLflow 설정만으로 RAG나 평가 코드가 대신 구현되는 것은 아닙니다.

### 개발 환경과 제출 환경

| 구분 | 개발·주요 실험 | 제출·면접관 데모 |
| --- | --- | --- |
| 사용하는 인프라 | 홈서버 인프라 중심 | 엘리스클라우드와 엘리스 모델 서비스 |
| BGE-M3 추론 | 홈서버 Ollama | 기관 모델 등록 또는 런박스 GPU 직접 서빙 |
| MLflow | 홈서버 MLflow | 별도 배포한 MLflow와 Dataset·AI endpoint·Prompt 설정 |
| 주요 목적 | 구현, Before/After 실험, 보고서 작성 | QA 직접 사용, 설정·결과 확인, 평가 실행 |

Embedding과 MLflow는 환경별 접속 설정으로 연결합니다. Embedding은 백엔드별 API 차이를
어댑터에서 처리하고, 같은 BGE-M3라도 서빙 방식이 달라졌다면 출력과 검색 결과의 호환성을 확인합니다.
필요한 경우 제출 환경에서 다시 임베딩합니다.

주요 실험과 보고서는 홈서버에서 만들고, 제출 환경에서는 면접관이 홈서버에 접속하지 않아도
서비스를 사용해볼 수 있도록 준비합니다. 모든 개발 이력을 옮기거나 주요 실험 전체를
클라우드에서 다시 수행하는 방식은 아니며, 제출 환경의 실제 동작은 별도로 확인합니다.

## 4. 평가 체계

### Gold Set

### 품질 지표와 판정 기준

### 재현성과 회귀 검증

## 5. 핵심 설계 결정과 Trade-off

## 6. 한계점, 알려진 이슈와 향후 개선 과제

## 7. 개선 실험과 결과

### Hypothesis — 무엇을 왜 바꿨나요?

### Result — 결과가 어떻게 달라졌나요?

### Analysis — 결과를 어떻게 해석했나요?

### Next Steps — 다음에는 무엇을 해볼까요?
