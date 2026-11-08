# 버전 및 Git 운영 규칙

사용자가 지정한 main-release-feature 3단 흐름을 적용한다.
[Homepage의 Git Workflow](https://github.com/Ootzk/Homepage/blob/main/AGENTS.md)의
버전 표기, milestone 및 커밋 전 검토 규칙을 참고했다.

## 버전

- Semantic Versioning을 사용한다. 패키지 구성 시 `pyproject.toml`의 프로젝트 버전을 기준으로 관리한다.
- 패키지에는 `X.Y.Z`, 브랜치·milestone·태그·릴리스에는 `vX.Y.Z`를 사용한다.
- `0.x`에서는 기능 묶음과 호환되지 않는 변경에 MINOR를, 호환 가능한 수정에 PATCH를 올린다.
- `1.0.0`부터 호환되지 않는 공개 계약 변경은 MAJOR, 호환 가능한 기능 추가는 MINOR,
  호환 가능한 버그 수정은 PATCH를 올린다.
- 공개 계약은 QA API의 request/response와 Ingest/Eval CLI 및 출력 형식이며, 구현 시 문서화한다.
- Corpus·Gold Set·모델·프롬프트 버전은 앱 버전과 별도로 실험에 기록한다.
- 첫 목표 버전과 milestone 범위는 아직 확정하지 않았다.

## 브랜치와 병합

1. milestone `vX.Y.Z`에 목표·포함 이슈·완료 기준을 정한다.
2. 최신 main에서 `release/vX.Y.Z`를 생성한다.
3. 각 이슈는 대상 release에서 `feature/<slug>`를 분기해 구현한다.
4. 기능 PR의 base는 대상 release로 하고, 검증 후 squash merge를 권장한다.
5. milestone 완료 후 `release/vX.Y.Z → main` PR을 연다. 제목은 `Release vX.Y.Z`로 한다.
6. 패키지 버전과 release 이름의 일치, 실행·평가 결과 및 알려진 한계를 확인하고 merge commit으로 병합한다.
7. main의 병합 커밋에 태그 `vX.Y.Z`를 만들고 같은 버전으로 GitHub Release를 작성한다.

`main`은 최신 릴리스 상태를 유지한다. 이슈 제목은 기능이나 문제를 설명하고 목표 버전은 milestone로 관리한다.
커밋과 squash 제목은 `feat: ...`, `fix: ...`, `docs: ...`, `chore: ...`처럼 변경 목적을 드러낸다.
기존 릴리스 태그는 다시 쓰지 않고 수정은 다음 PATCH release로 진행한다.

## 초기 설정과 에이전트 작업

첫 커밋은 운영 파일과 사전 점검 문서를 main에 기록한다. 비공개 PDF와 `tmp/`는 포함하지 않는다.
초기 커밋에는 릴리스 태그를 붙이지 않으며, 이후 첫 milestone를 정하고 위 흐름을 시작한다.

에이전트는 커밋 전 변경과 검증 결과를 제시하고 사용자 승인을 받는다.
이미 승인된 범위는 다시 승인받지 않는다.
push·PR 병합·태그·GitHub Release 생성은 사용자가 승인한 범위에서 수행한다.
CI와 브랜치 보호는 구현에 필요한 검사가 정해질 때 구성한다.
