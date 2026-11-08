# Agent Guide

## Scope

- 한국어 엘리스 테크블로그 기반 RAG QA와 Eval Harness를 구현한다.
- 현재 결정은 `docs/`와 사용자 지시를 기준으로 한다. 기술 스택을 추정해 확정하지 않는다.
- Corpus URL에는 `/ko`를 명시하고 최종 언어를 검증한다.
- API 키, `.env`, 수집 원문, 생성 인덱스, 비공개 과제 PDF를 커밋하지 않는다.

## Git workflow

- 자세한 규칙은 [docs/git-workflow.md](docs/git-workflow.md)를 따른다.
- `main`은 릴리스 상태, `release/vX.Y.Z`는 milestone 통합 브랜치다.
- 구현은 대상 release에서 분기한 `feature/<slug>`에서 진행하고 해당 release로 PR을 연다.
- feature → release는 squash merge 권장, release → main은 merge commit을 사용한다.
- 패키지 구성 시 `pyproject.toml`의 프로젝트 버전을 기준으로 관리한다.
- 이슈 제목은 문제/기능을 설명하고 버전 접두사를 붙이지 않는다. 목표 버전은 milestone로 관리한다.
- 커밋 전 변경과 검증 결과를 제시하고 사용자 승인을 받는다. 이미 승인된 커밋 범위는 재승인받지 않는다.
- PR 병합, push, 릴리스 태그 및 GitHub Release 생성은 해당 작업을 사용자가 승인한 범위에서 수행한다.

## Working style

- 과제의 평가·실험·재현성에 필요한 범위부터 구현하고 설계 판단과 한계를 기록한다.
- 의미 있는 단위로 변경하며 관련 없는 사용자 변경은 보존한다.
- 테스트는 변경의 실제 동작과 위험에 맞춰 수행한다. 문서 변경에 불필요한 테스트를 만들지 않는다.
