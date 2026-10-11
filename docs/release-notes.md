# Release notes

## v0.3.15 (배포 준비)

v0.3.14 이후의 변경을 정리한다. 버전 변경은 main에 반영됐으며, 실제 모델 평가의
릴리즈 조건을 통과한 뒤 태그와 GitHub Release를 게시한다.

### 주요 변경

- Agent Flow 진입 지침을 host별 skill로 모았다. 새 설치는 루트 `AGENTS.md`와
  `CLAUDE.md`에 Agent Flow 지침을 추가하지 않는다. 재설치는 소유 기록과 내용이
  일치하는 기존 관리 블록만 은퇴시키고, 프로젝트 규칙·사용자 수정·백업은 보존한다.
  설치하거나 세션을 여는 것만으로 workflow를 시작하지 않는다. [#335](https://github.com/chonamdoo/agent-flow/pull/335)
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
