# 릴리스와 배포 준비

현재 릴리스 대상은 `v1.0.0`이며, 마지막으로 발행된 릴리스는 `v0.1.0`이다. 개발 환경의 수집·검색·QA·평가 기능 구현과,
이미지 발행·클라우드 배포·전수 품질 검증의 완료 상태는 구분한다.

## 패키지와 배포 단위

| 구성 | 버전 원본 | 잠금 파일 | 배포 단위 |
| --- | --- | --- | --- |
| Knowledge API·RAG·Ingest·Eval worker | `apps/knowledge/pyproject.toml` | `apps/knowledge/uv.lock`의 자체 패키지 항목 | Knowledge 이미지 하나 |
| Web | `apps/web/package.json` | `apps/web/package-lock.json`의 최상위·루트 패키지 항목 | 정적 빌드가 Knowledge 이미지에 포함 |
| worker | Knowledge와 동일 | Knowledge와 동일 | 같은 이미지를 다른 command로 실행 |
| 평가 CLI | 별도 패키지 버전 없음 | Knowledge 실행 환경 사용 | 저장소의 `tools/evaluation`에서 실행 |
| Notebook 보고서 | `tools/reporting/pyproject.toml` | `tools/reporting/uv.lock` | 로컬 Jupyter 환경, 이미지 발행 없음 |
| PostgreSQL·Ollama·MLflow | 각 외부 제품 버전 | 해당 인프라 관리 범위 | 이 저장소의 제품 버전과 별도 관리 |

자체 패키지는 통합 릴리스 버전을 사용한다. 현재 Knowledge·Web·Reporting 및 lockfile의 자체 항목은 모두 `1.0.0`이다.
v1.0.0은 RAG 서비스와 평가·사람 검수·실험 보고서의 통합 릴리스다. Open WebUI 원본 커밋·버전은
`apps/web/UPSTREAM.md`의 출처 정보이며, 자체 Web 패키지 버전과 구분한다.
API OpenAPI 버전·연결 검증 MLflow 태그·MCP 버전은 설치된 Knowledge 패키지 메타데이터를 사용한다.

다음 릴리스에서는 대상 `release/vX.Y.Z`에서 분기한 feature 브랜치에서 모든 자체 패키지를 맞춘다.
예를 들어 다음 대상 버전이 `1.0.0`일 때 저장소 루트에서 실행한다.

```bash
uv version --project apps/knowledge --no-sync 1.0.0
uv version --project tools/reporting --no-sync 1.0.0
npm version 1.0.0 --prefix apps/web --no-git-tag-version --allow-same-version --ignore-scripts
uv sync --project apps/knowledge --locked
uv sync --project tools/reporting --locked
uv run --project apps/knowledge python .github/scripts/release.py check --release-version 1.0.0
```

`uv version --no-sync`는 pyproject와 lock을 갱신하며, 이어지는 sync가 설치된 패키지 메타데이터도 갱신한다.
`npm version --no-git-tag-version`은 package·lock만 갱신하고 Git 커밋·태그를 만들지 않는다.
이 명령들은 의존성 업그레이드와 구분하며 diff에서 자체 패키지 버전 외의 변경도 확인한다.
uv/npm 명령의 동시 갱신은 원본에 영향을 주지 않는 임시 복사본에서 `0.1.0 → 0.1.1`로 확인했다.

현재 상태를 외부 API 호출이나 파일 수정 없이 검사하려면 다음을 사용한다.

```bash
uv run --project apps/knowledge python .github/scripts/release.py check --release-version 1.0.0
uv run --project apps/knowledge python -m unittest discover -s .github/tests -v
uv lock --check --project apps/knowledge
```

## CI와 발행 순서

1. feature → release PR: 브랜치·버전·lock 자체 항목, CI·릴리스 계약 테스트, 공백 검사를 항상 실행한다.
   나머지는 merge-base부터 PR head까지 바뀐 경로에 따라 선택한다. 기본 검사는 Python 표준
   라이브러리만 사용하며 npm/uv 의존성을 설치하거나 DB를 기동하지 않는다.
2. release → main PR: 제목은 `Release v1.0.0`. 변경 경로와 관계없이 Web·Knowledge·Reporting 전체 검사와
   `linux/amd64` 이미지 빌드를 실행한다.
   이미지가 있어야 하는 릴리스 PR은 이미지 빌드가 생략되어도 `Ready to merge`를 통과하지 못한다.
3. 사용자가 release PR을 **merge commit**으로 병합한다. feature PR의 squash merge와 구분한다.
4. main push의 Release workflow가 정확히 해당 SHA의 병합 PR·부모 commit·패키지 버전·미사용 태그를 확인한다.
5. GHCR에 `:v1.0.0`, `:sha-<전체 merge SHA>`, `:latest` 이미지를 게시한다.
6. 필요한 모든 이미지 게시가 성공한 뒤 같은 merge SHA에 태그·GitHub Release를 생성한다.

| feature PR 변경 범위 | 추가 검사 |
| --- | --- |
| 문서(`*.md`, `docs/`), Corpus manifest, Gold·Iron 데이터, 조회 SQL, 에디터 설정 | 없음 |
| `apps/web/` 코드·의존성·설정 | Web 포맷·타입 검사·정적 빌드 |
| `apps/knowledge/`, `tools/evaluation/` 코드 | Python lint·format·lock 검사·전체 앱 테스트, 테스트 전용 PostgreSQL |
| `tools/reporting/` 코드·의존성·Notebook | 독립 환경 lint·format·lock 검사, 데모 Notebook 실행·HTML export 테스트 |
| Compose·환경 예시·CI 등 공통 설정 또는 미분류 경로 | Web·Knowledge·Reporting 모두 |

여러 범위가 바뀌면 필요한 검사를 합친다. 문서 경로는 앱 디렉터리 내부에서도 기본 검사만 실행한다.
삭제·이동도 변경 경로에 포함하며, 이동은 이전·새 경로를 모두 확인한다. 선택하지 않은 job은
`skipped`로 표시되고, 선택한 job은 성공해야 `Ready to merge`를 통과한다.
feature PR에서는 Docker 빌드·이미지 게시·릴리스 발행을 하지 않는다.
Gold·Iron 전수 평가와 유료 Judge 호출은 두 PR CI 모두에서 실행하지 않는다.

Knowledge Dockerfile만 있으므로 현재 발행되는 이미지는
`ghcr.io/ootzk/elice-techblog-rag-knowledge` 하나다. Web·worker를 별도 이미지로 발행하지 않는다.
릴리스 스크립트의 prepare/publish를 로컬에서 검증 대신 실행하지 않는다. 해당 명령은
GitHub 인증정보와 실제 main merge를 요구하며 publish는 태그·Release를 생성한다.

현재 자동화는 이미지·GitHub Release 발행까지다. 서버에 컨테이너를 반영하는 작업은 수동이며,
태그가 이미 발행된 릴리스를 덮어쓰거나 다른 commit으로 재발행하지 않는다.
부분 실패가 나면 동일 workflow 실행의 로그·게시 결과를 확인한 뒤 재실행한다.

### 원격 설정 확인 결과

2026-11-10 점검 기준 저장소는 private이고 squash·merge commit을 모두 허용하며,
병합 후 feature 원격 브랜치 자동 삭제가 활성화되어 있다. `v0.1.0` 태그·GitHub Release가 발행되었으며
Release workflow가 main에 반영되어 있다.

브랜치 보호·ruleset API는 현재 저장소 요금제에서 403(상위 요금제 또는 public 저장소 필요)을 반환했다.
따라서 `Ready to merge` workflow의 구현과 서버 측 필수 검사·PR 병합 강제 설정은 동일한 상태가 아니다.
현재는 PR에서 검사 결과와 release merge 방식을 직접 확인해야 한다. 저장소 공개나 요금제 변경은 수행하지 않았다.

## 환경별 이미지 사용

prod `compose.yaml`에는 build가 없고 `KNOWLEDGE_IMAGE`가 필수다.
`.env.example`은 개발 목표인 `:v1.0.0`을 지정하며 실제 태그 발행 후에 pull한다. 버전 태그나 digest로
고정하고 변경되는 `latest`를 배포 기준으로 사용하지 않는다.

dev `compose.dev.yaml`은 로컬 Knowledge build와 PostgreSQL을 추가한다.
`.env.dev.example`은 로컬 이미지 이름을 지정한다. 기존 `.env.dev`에도 아래 항목이 필요하다.

```dotenv
KNOWLEDGE_IMAGE=elice-techblog-rag-knowledge:latest
```

prod는 외부 DB·임베딩·MLflow endpoint를 명시하며 홈서버나 dev DB로 자동 전환하지 않는다.
`compose config`를 파일·채팅에 그대로 출력하면 환경변수 값이 포함될 수 있으므로,
실제 설정 공유 대신 예시 파일로 구조를 확인한다.

```bash
# 실제 서비스 접속이나 모델 호출 없이 prod 구성 확인. .env 파일도 필요하지 않다.
docker compose --env-file .env.example -f compose.yaml config --no-env-resolution --quiet
```

## 최초 배포 순서

1. 배포할 버전의 GitHub Release와 Knowledge 이미지 게시 성공을 확인한다.
2. 배포 호스트에서 `.env.example`을 `.env`로 복사하고 전용 DB·추론·MLflow·로그인 코드를 설정한다.
   private GHCR 이미지이면 Docker에 `read:packages` 권한이 있는 계정으로 로그인한다.
   이미지 pull 인증정보를 애플리케이션 환경 파일에 넣지 않는다.
3. HTTPS ingress/reverse proxy와 DB·MLflow artifact 영속 저장소를 준비한다.
   `FORWARDED_ALLOW_IPS`는 실제 신뢰할 proxy IP로 지정한다. 기본 localhost 바인딩과
   prod Secure 쿠키를 고려해 브라우저가 HTTPS 주소로 접속하도록 한다.
4. 아래 순서로 이미지를 받고 DB를 초기화한 뒤 API와 worker를 시작한다.

```bash
cp .env.example .env
# 위에서 설명한 실제 배포 설정을 채운다.
docker compose --env-file .env -f compose.yaml pull knowledge worker
docker compose --env-file .env -f compose.yaml run --rm --no-deps knowledge python -m elice_knowledge init-db
docker compose --env-file .env -f compose.yaml up -d --no-build knowledge worker
```

5. 대상 MLflow 서버에 QA Prompt를 `production` alias로 등록하고, Corpus 수집·청킹·색인을 준비한다.
   재현 명령과 순서는 [README](../README.md)의 수집·색인·Prompt 절차를 따른다.
6. 호스트의 저장소 checkout에서 Dataset과 Judge 등록 CLI를 실행한다. `tools/evaluation`은
   런타임 이미지에 포함하지 않으므로 운영자 실행 환경은 Python 3.12·uv와 해당 MLflow/API에 접근할 수 있어야 한다.
   Gateway의 세 모델 연결은 대상 MLflow에 별도로 구성한다. 홈서버 secret을 export하지 않는다.
7. 대상 서버의 임베딩과 Corpus·청킹 정책에 일치하는 완료 인덱스를 확인하고, 일반 질문·출처·로그인·worker 동작을 확인한다.
   의미적 Dataset 평가가 필요하면 API와 worker 모두 `EVALUATION_JUDGES_ENABLED=true`로 설정한다.
   prod 대화 자동 평가는 dev 필터와 별도 정책이므로 자동으로 켜지지 않는다.
8. `/health/live`, `/health/ready`와 소규모 QA·평가를 확인한다.
   readiness의 생성 항목은 설정 확인이고 유료 모델 호출 성공을 증명하지 않는다.

업데이트 전 DB·MLflow metadata·artifact와 이전 이미지 digest를 보존한다.
재기동은 기존 볼륨을 유지하며, 운영 데이터가 있는 상태에서 `down -v`를 배포 절차로 사용하지 않는다.
코드의 테이블 초기화는 임의의 이전 릴리스로 DB를 되돌리는 마이그레이션 수단이 아니므로,
문제가 생기면 스키마 호환성과 백업을 확인한 뒤 이미지·데이터 복원 범위를 결정한다.

## 남은 배포 검증

- 엘리스클라우드 DB·MLflow·HTTPS ingress·영속 저장소의 실제 접속 설정 확정.
- BGE-M3의 제출용 서빙 방식 결정. 현재 어댑터는 Ollama 프로토콜이며 다른 ML API는 호환성 확인·구현이 필요.
- 대상 서버에서 Corpus·인덱스·Prompt·Dataset·Judge 연결과 소규모 QA/Eval 확인.
- Gold·Iron 전수 기준 평가·실제 사람 평가 정합성·Before/After 실험은 개발 MVP 기능 구현과 별도로 수행.
- macOS/ARM의 amd64 교차 실행은 Node/libuv assertion으로 중단했다. 네이티브 ARM 이미지 검사와
  릴리스 PR의 네이티브 amd64 빌드를 구분하며, amd64 검증 완료 전 실제 배포가 확인된 것으로 기록하지 않는다.


## v0.1.0 배포 준비 점검 기록

- 패키지·lockfile의 통합 `0.1.0` 정합성 검사 통과.
- 격리 PostgreSQL을 포함한 애플리케이션 테스트 227개와 릴리스 계약 테스트 9개 통과.
- Ruff lint·format, lockfile 검사와 공식 actionlint v1.7.12의 workflow 문법 검사 통과.
- prod는 image 전용, dev는 build·PostgreSQL을 포함하는 Compose 구성 검사 통과.
- ARM 이미지 빌드와 컨테이너 안의 패키지/API 버전·live 응답·정적 UI·라이선스 파일 검사 통과.
- amd64 로컬 교차 실행은 위 Node/libuv 오류로 미검증. 변경한 workflow의 원격 CI,
  당시 최초 GHCR 게시·태그·GitHub Release 생성 및 클라우드 실배포는 미실행.
  이후 main 병합 자동화로 v0.1.0 이미지·태그·GitHub Release 발행을 확인했으며 실제 클라우드 배포와 구분함.
