# elice-techblog-RAG

엘리스그룹 AI Engineer (Platform) 미니 프로젝트: 한국어 테크블로그 기반
Citation RAG QA 서비스와 자체 Eval Harness.

현재는 프로젝트 준비 단계이며, 서비스 구현과 기술 스택은 아직 확정하지 않았다.
버전은 패키지 구성 시 `pyproject.toml`에서 관리한다.

- [Corpus 사전 점검](docs/corpus-feasibility.md)
- [버전 및 Git 운영 규칙](docs/git-workflow.md)
- [에이전트 지침](AGENTS.md)

개발은 `main → release/vX.Y.Z → feature/<slug>` 순서로 분기한다.
기능 PR은 release로 squash merge하고, 완료된 release는 main으로 merge commit한다.
버전과 milestone 범위를 먼저 정한 다음 해당 release의 구현을 시작한다.

서비스 설치·Ingest·API·Eval 실행 방법은 해당 기능 구현 시 추가한다.
