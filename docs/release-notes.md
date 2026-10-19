# Release notes

## v0.3.18 (준비 중)

다음 patch 후보다. 태그 생성과 실제 배포는 아직 하지 않았다.

- 필수 규범의 scope 확장으로 같은 작성 phase에 재진입할 때 기존 RED 실행의
  시간창·코드 기준선을 보존한다. 규범 승인과 artifact는 계속 새로 요구하며,
  실제 phase 전이·수정 self-loop에서는 이전 RED를 재사용하지 않는다.

- managed worktree에 bind된 host 세션이 leader에 설치된 launcher를 정확한 경로로
  불러 자기 run을 `abort`할 수 있다. leader가 바뀐 뒤에도 열리며, launcher가 설치
  기록과 달라졌거나 다른 run·worktree·옵션·복합 셸을 쓰면 계속 막는다.
  [#371](https://github.com/chonamdoo/agent-flow/pull/371)
- bound 세션의 lifecycle 명령이 다른 worktree를 고르지 못하게 했다. 상대
  `--worktree`·`--root`는 명령이 실행되는 자리에서 풀고, CLI가 다르게 읽을 수
  있는 축약 옵션·중복 옵션·`~` 값은 거절한다.
  [#371](https://github.com/chonamdoo/agent-flow/pull/371)
- worktree 안에서 실행한 `abort`는 다른 worktree나 leader의 run을 닫지 않는다.
  그런 run은 leader checkout이나 해당 worktree에서 닫는다.
  [#371](https://github.com/chonamdoo/agent-flow/pull/371)
- bound 세션이 leader에 설치된 launcher를 명령 이름 자리에서 부르면 PATH의
  `agent-flow`와 똑같이 판정한다. 명령별 예외 없이 `pr-watch`·`run-command`·
  `gates`·`spec approve` 같은 명령이 PATH 없는 세션에서도 열린다. 인수·리다이렉션
  대상처럼 셸이 명령 이름으로 읽지 않는 자리의 launcher 경로와 설치 기록과 달라진
  launcher는 계속 막는다. 예약어는 인용·확장 없는 낱말만 인정한다(`!$X <launcher>`의
  launcher는 인수다). 자기 checkout 안의 symlink, 따옴표, `~`로 다른 실행 파일을
  고르는 호출은 lifecycle 면제를 받지 못한다.
  [#372](https://github.com/chonamdoo/agent-flow/pull/372),
  [#373](https://github.com/chonamdoo/agent-flow/pull/373)
- worktree 안에서 실행한 명령은 `--worktree`·`--root`·`--run-dir`·
  `worktree --name/--path`로 이미 있는 다른 checkout이나 run을 고르지 못한다.
  형제 worktree를 `worktree remove --allow-unmerged`로 지우던 경로가 닫힌다.
  새 run을 만드는 `run`·`start`와 아직 없는 checkout을 만드는 `worktree create`는
  그대로다.
  `spec approve`·`spec confirm`은 그 worktree의 active run에만 기록한다(#370의
  정확한 현재 run 규칙을 CLI로 옮겼다).
  [#372](https://github.com/chonamdoo/agent-flow/pull/372)
- RED reference와 GREEN 명령을 대조할 때 regression selector의 대소문자를
  보존한다. enum 마커 값은 계속 정규화한다.
  [#370](https://github.com/chonamdoo/agent-flow/pull/370)
- `phase_eval` review fixture의 실행 증거가 실제 발견한 테스트 전체를 이름으로
  실행한다. case·기대 판정·threshold는 바꾸지 않았다.
  [#370](https://github.com/chonamdoo/agent-flow/pull/370)

## v0.3.17 (2026-10-18)

v0.3.16 이후의 변경이다. [v0.3.17](https://github.com/chonamdoo/agent-flow/releases/tag/v0.3.17)로 배포했다.

- managed project launcher의 `install`이 Python runtime 자산 디렉터리 대신
  설치기를 만든 kit origin으로 위임한다. Homebrew의 검증된 `opt` 링크를
  interpreter와 installer가 공유하며 Python 격리와 launcher 무결성을 유지한다.
  [#368](https://github.com/chonamdoo/agent-flow/pull/368)
- GitHub가 명시적으로 빈/null `reviewDecision`을 반환하는 저장소는 승인 없는
  상태를 무한 대기하지 않는다. 필수 리뷰·변경 요청·누락/미지 관측·CI·게시 HEAD
  검증과 별도의 사용자 병합 승인 단계는 유지한다.

기존 launcher는 active run이 끝난 뒤 leader에서 새 installer로 갱신하고 host
세션을 다시 시작해야 한다. npx kit cache가 제거된 경우의 installer-missing 실패,
`update`와 전역 PATH 탐색 문제를 해결한 것으로 확대하지 않는다.

## v0.3.16

v0.3.15 이후 managed worktree에서 project-local skill 스크립트를 실행하고 사본을
안전하게 정리할 수 있도록 수정했다. [#362](https://github.com/chonamdoo/agent-flow/pull/362)

- 새 worktree 생성과 기존 checkout의 재사용·채택 때 leader의
  `.agent-flow/local-skills/` 파일을 복사한다. checkout 상대경로로 동봉 스크립트를
  실행할 수 있다. 기존 checkout 파일은 덮지 않고 `.env`·`.env.*` 파일과
  디렉터리, symlink는 복사하지 않는다.
- 복사한 바이트의 기록으로 정상 사본을 알아보므로, 이후 leader에서 skill을
  고치거나 지워도 그대로인 사본이 자동 정리를 막지 않는다. 사본 실행으로
  생긴 Python bytecode도 정상 정리를 막지 않는다. checkout에서 수정한 사본과
  사용자가 추가한 파일·캐시는 계속 보존하며, 정리를 막은 파일 경로를 알린다.

작성 phase의 skill 선택이 이미 해석된 경우에는 전달된 profile 값과 required-read plan을 사용하도록 안내한다. inline 본문은 바로 적용하고 계획에 지정된 파일·적용되는 참조만 읽으며, 같은 본문·지문을 가리키는 후속 읽기 지시는 기존 읽음을 재사용한다. 선택을 다시 해석하기 위한 전체 skill index·workflow/run metadata 읽기를 줄인다. full-feature의 red phase와 설치되는 `.agent-flow/rules/workflow-contract.md`도 같은 Before Starting 선택 절차를 가리킨다. 독립 reviewer·미설치 skill 처리와 필수 규범은 유지한다.

기존 프로젝트는 leader checkout에서 kit을 재설치하고 host 세션을 다시 시작한다.
기존 worktree의 local skill 사본은 다음 `run`/`start` 재사용 때 채워진다.

## v0.3.15 (2026-10-12)

v0.3.14 이후의 변경을 정리한다. 실제 모델 평가 168건과 릴리스 게이트를 통과해
[v0.3.15](https://github.com/chonamdoo/agent-flow/releases/tag/v0.3.15)를 배포했다.
macOS ARM에서 Homebrew v0.3.14 → v0.3.15 업그레이드와 새 프로젝트 설치를 확인했다.

### 주요 변경

- Agent Flow 진입 지침을 host별 skill로 모았다. 새 설치는 루트 `AGENTS.md`와
  `CLAUDE.md`에 Agent Flow 지침을 추가하지 않는다. 재설치는 소유 기록과 내용이
  일치하는 기존 관리 블록만 은퇴시키고, 프로젝트 규칙·사용자 수정·백업은 보존한다.
  설치하거나 세션을 여는 것만으로 workflow를 시작하지 않는다. [#335](https://github.com/chonamdoo/agent-flow/pull/335)
- bundled host skill은 Codex의 암묵적 호출 목록에서 제외한다. 명시 호출과 runner의
  phase별 skill 전달은 유지하며 사용자·팀 skill의 자체 정책은 덮지 않는다.
  [#354](https://github.com/chonamdoo/agent-flow/pull/354)
- 사용자 확인 단계는 `approval_command`로 승인한다. `next_command`는 승인을
  대신하지 않는다. `domain-grill`은 결정 출처를 남기고 사용자 확인에서 멈추며,
  `merge-approval`은 병합 직전 별도의 승인을 기다린다. [#291](https://github.com/chonamdoo/agent-flow/pull/291),
  [#292](https://github.com/chonamdoo/agent-flow/pull/292), [#294](https://github.com/chonamdoo/agent-flow/pull/294)
- Claude·Codex·OMP의 host 연결 상태를 표시한다. host가 일치하는 실행 관측만
  해당 host의 연결 증거로 인정한다. Codex `continue`의 종료 코드를 관측할 수 없는
  경우에는 검증된 일회성 receipt가 있어야 다음 단계 안내를 연다. [#295](https://github.com/chonamdoo/agent-flow/pull/295),
  [#327](https://github.com/chonamdoo/agent-flow/pull/327), [#328](https://github.com/chonamdoo/agent-flow/pull/328)
- host가 테스트 종료 코드를 보내지 못하면 `agent-flow run-command -- <argv>`의
  실제 실행 결과로 RED와 수정 후 검증을 기록한다. 자기신고만으로 실패한 테스트를
  증명하지 않는다. [#326](https://github.com/chonamdoo/agent-flow/pull/326)
- worktree 정리는 run 기록을 보관한 뒤 진행한다. 짧은 workflow의 local handoff는
  checkout을 유지하며, 병합 뒤 수동 SPEC 승인도 현재 publication과 GitHub HEAD를
  대조한다. [#293](https://github.com/chonamdoo/agent-flow/pull/293), [#322](https://github.com/chonamdoo/agent-flow/pull/322)
- provider sandbox는 checkout의 `.agent-flow`와 host 설정의 symlink 이름·대상을
  보호한다. Codex reviewer는 checkout의 `AGENTS.md`를 자동 지시로 로딩하지 않는다.
  파일을 리뷰 증거로 읽는 것은 가능하다. [#314](https://github.com/chonamdoo/agent-flow/pull/314),
  [#321](https://github.com/chonamdoo/agent-flow/pull/321), [#344](https://github.com/chonamdoo/agent-flow/pull/344)
- profile YAML의 중복 키와 지원하지 않는 gate 키를 거부하고, 배포 workflow의
  알 수 없는 최상위 키를 CI에서 검사한다. 측정에 성공한 base가 없는 phase budget도
  실패로 판정한다. [#300](https://github.com/chonamdoo/agent-flow/pull/300),
  [#324](https://github.com/chonamdoo/agent-flow/pull/324), [#325](https://github.com/chonamdoo/agent-flow/pull/325),
  [#331](https://github.com/chonamdoo/agent-flow/pull/331)
- architecture-lint gate는 checkout 루트의 동명 `agent_flow` 패키지로 대체되지
  않도록 실행한다. [#333](https://github.com/chonamdoo/agent-flow/pull/333)
- 미사용 `agent-flow memory entities` 명령과 memory 패키지를 제거했다. 새 빈
  memory 디렉터리를 만들지 않으며 기존 사용자 데이터는 보존한다.
  [#342](https://github.com/chonamdoo/agent-flow/pull/342)
- Python 지원 하한을 실제 runtime과 같은 **3.11**로 맞췄다. 현재 host는
  Claude·Codex·OMP이며, ADR에는 Gemini/antigravity 제거 사실과 당시 결정의
  역사적 범위를 구분해 적었다. [#329](https://github.com/chonamdoo/agent-flow/pull/329),
  [#343](https://github.com/chonamdoo/agent-flow/pull/343)
- Python Clean profile은 HTTP handler를 inbound adapter로 인정하고, Next.js의
  App Router presentation에는 빈 feature API 계층을 요구하지 않는다.
  [#356](https://github.com/chonamdoo/agent-flow/pull/356)

### 기존 프로젝트 업데이트

설치본 탐색을 git root, HOME, 시작점 경계로 제한한다. 관측 hooks와 Python/OMP는
linked worktree에서 leader 설치본만 인정하며 worker나 worktree container의 사본을
사용하지 않는다. 경로의 대소문자 표기가 달라도 같은 디렉터리의 경계가 유지된다.
JS CLI는 git common root를 프로젝트 루트로 사용하는 기존 동작을 유지한다.

관리 hook의 내용과 digest가 바뀌므로 kit 업데이트 뒤 기존 프로젝트에도 재설치해야
한다. leader checkout에서 다음 명령을 실행한다.

```bash
node bin/agent-flow-kit.mjs install --hooks
```

재설치는 관리 hook과 기록된 digest, 설치된 Python runtime 및 OMP extension을 함께
갱신한다. linked worktree에서 실행하지 않는다.

재설치 뒤 host 세션을 다시 시작해 갱신된 hook과 skill을 읽게 한다. 수정됐거나
소유 기록이 불명확한 루트 문서는 보존하고 알린다. `--force-managed`로도 소유
검증을 우회하지 않는다. 루트 문서 변환을 다시 요청할 때만
`install --migrate-root-context`를 명시한다.
