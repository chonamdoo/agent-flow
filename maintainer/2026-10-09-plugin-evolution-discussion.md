# 설치 형태·도메인 그릴링 결정과 작업 계획 (2026-10-09)

이 문서는 2026-10-09 대화의 결정, 최종 제안, 실행 계획, 근거를 정리한 것이다. 구현이나 workflow 승인, 독립 리뷰의 증거가 아니다. 이 문서와 원자료 폴더 외에 저장소 코드는 바꾸지 않았다.

**위치:** `docs/`는 npm 패키지와 플러그인 payload에 포함된다(`package.json` `files`, `lib/plugin-bundle.mjs`가 같은 목록을 복사). 그래서 개발 전용인 `maintainer/`에 둔다. 원자료는 `maintainer/2026-10-09-plugin-evolution-raw/`에 있다.

**표기:**
- **[확인]** 이 세션에서 코드·명령 실행·PR·이슈 원문으로 직접 확인한 사실이다.
- **[결정]** 사용자가 대화에서 확정한 내용이다.
- **[출처: X]** 외부 모델이나 다른 패인의 주장이다. 별도로 확인하지 않았다면 확인했다고 적지 않는다.
- 경로가 `runner.py`, `artifact.py`, `cli.py`, `core/`, `adapters/`, `workflows/`로 시작하면 `src/agent_flow/` 아래 경로다. workflow YAML(`full-feature.yaml`, `default.yaml`)은 `src/agent_flow/workflows/` 아래에 있다.
- **[확인 필요]** 확인하지 못한 항목이다. 판단을 덧붙이지 않는다.

---

## 1. 확정된 결정

| # | 결정 | 근거 대화 |
|---|---|---|
| D1 | **Claude, Codex, OMP 등 다양한 에이전트에서 동작하는 것이 최우선이다** | 사용자: “제일 중요한건 claude codex omp 등 다양한 에이전트에서 동작되야해” |
| D2 | AI 에이전트에게 워크플로를 강제한다는 강점은 약해지면 안 된다 | 사용자: “강점은 전혀 사라지지 않는 형태로” |
| D3 | 설치된 런타임·YAML의 digest를 run 시작 때 대조하는 제안은 하지 않는다 | 사용자: “수정 언제든지 할수있으니 빼고” |
| D4 | 훅 실행을 증명할 수 없는 에이전트는 **보호 수준을 표시하고 허용**한다 | 사용자: 권고안 채택 |
| D5 | 사람 승인을 신뢰 경계로 만드는 것은 **지금은 하지 않는다.** #162와 #222에서 내린 결정을 유지한다 | 사용자: 권고안 채택 |
| D6 | full-feature에서 승인 지점이 2번(`domain-grill` 뒤, `prd` 뒤)으로 늘어나는 것을 받아들인다 | 사용자: “잘못 개발되는거 보다 한번더 확인 받는게 맞지않아?” → 권고 유지 |
| D7 | 진행 순서는 권고안대로 한다(4절) | 사용자: “진행순서는 너의 권고안대로” |
| D8 | 작업은 **단계마다 PR을 따로 내고, PR 생성까지만** 한다. merge는 사용자가 PR마다 따로 승인한다 | 사용자: 권고안 채택 |
| D9 | YAML 중복 키는 **처음부터 에러**로 막고, 메시지에 파일·줄·키를 표시한다 | 사용자: 권고안 채택 |
| D10 | 이 문서를 정리한 뒤 새 패인을 열어 Agent Flow 스킬을 호출하고 작업을 진행한다 | 사용자 요청 |
| D11 | 마켓에 배포하지 않으므로 **플러그인 형태로 가지 않고 지금 형태(프로젝트 설치)를 유지한다.** 플러그인 전용 작업(A1, A2)은 계획에서 뺀다. 기존 packer 코드는 건드리지 않는다. 제거하려면 따로 결정해야 한다 | 사용자: “마켓 배포 하지않을꺼라서 플로그인 형태가 이점이 없다면 현재 에이전트 플로우 형태로 가고 싶어” |
| D12 | 플러그인 형태를 빼더라도 **워크플로 강제력은 절대 약해지면 안 된다.** 지금과 같거나 더 강하고 단단해야 한다. 모든 PR은 4절의 “강제력 비약화 조건”을 지킨다 | 사용자: “기존에 에이전트 플로우가 해야하는 워크플로우 강제력은 절대 약해지면 안돼 기존과 혹은 그이상으로 강제되고 단단해져야해” |
| D13 | 이슈로 기록한 H1(#290)과 H2(#195, #196, #197)를 계획에 넣는다. 순서는 P1 → P2 → H1 → A3·A4·A5 + H2이고, A6은 언제든 한다(4절) | 사용자: “추천안대로 너의 추천대로 계획에 넣자” |

---

## 2. 최종 제안: 설치 형태와 호스트 연결

**구조 (D11):** 지금 형태를 유지한다. 판정은 Python runner와 workflow YAML이 하고, 실행본과 상태는 프로젝트에 설치한다. Claude와 Codex는 프로젝트 훅으로, OMP는 기존 extension으로 연결한다. 훅 세트(실행 전 차단 2규칙, 실행 후 tripwire, 세션 binding, 실행 기록)는 그대로 유지한다.

**플러그인 형태와 비교 [확인]:**
- 강제 수단은 두 형태가 같다. 플러그인을 켜도 installer나 훅은 실행되지 않는다(`lib/plugin-bundle.mjs:77`). 프로젝트 준비는 플러그인에 들어 있는 같은 installer로 `install`을 실행하는 것이다(`:84`).
- packer가 만드는 플러그인은 Claude·Codex용이다. 진입 문서에는 “OMP uses its existing project adapter”라고 적혀 있다(`:86`). OMP의 플러그인 지원 여부는 확인하지 않았다(`docs/issues/0009-plugin-optin-design.md:344`에도 미확정으로 적혀 있다).
- 실행본은 프로젝트 준비 때 설치되므로 플러그인을 업데이트해도 프로젝트마다 다시 설치해야 한다(`:84`, `docs/issues/0009-plugin-optin-design.md:325`).
- 플러그인 형태로 가면 설치 경로가 installer와 packer 2개가 된다.

| # | 제안 | 영향 범위 | 근거 [확인] | 회귀 방지 조건 |
|---|---|---|---|---|
| ~~A1~~ | 제외(D11). 기존 플러그인 제품화(Claude·Codex 실제 설치 검증, 진입 스킬 하나 노출) | - | pack 결과에 진입 스킬은 `agent-flow` 하나뿐이고 `kit/`에 runner·workflow YAML·OMP extension이 들어 있다. manifest는 `.claude-plugin/plugin.json`과 `plugin.json`(버전 0.3.14)이다 | - |
| ~~A2~~ | 제외(D11). pack symlink 경로 버그. **고치지 않은 알려진 버그로 남는다** | `lib/plugin-bundle.mjs:225`, `:250`, `:158-175` | `/tmp/...`로 출력하면 `EEXIST: file already exists, mkdir '/tmp'`로 실패하고 `/private/tmp/...`로는 성공했다. `:225`는 정규화된 상위 경로로 출력 경로를 만드는데, `:250`은 정규화 전 경로를 `ensureParentDirectories`에 넘긴다. 그래서 상대 경로가 `../../private/...`가 되고 symlink인 `/tmp`에서 `:167-169` 조건으로 예외가 난다 | - |
| A3 | OMP 설치 위치 탐색 차이를 재현해 보고, 우회가 재현될 때만 Python 규칙에 맞춘다 | `lib/omp-hooks-extension.mjs:85-112` | Python은 leader를 먼저 찾는다(`core/hook_integrity.py:159-176`). OMP extension은 시작 위치에서 상위 폴더 순으로 찾고(`:85-104`), 세션 시작 때 한 번 정한다(`:112`) | #151이 넣은 “git common root와 HOME에서 탐색을 멈추는” 경계를 유지한다 |
| A4 | 상태 진단에서 설치, 등록, 신뢰, 실제 실행을 구분해 보여 주고 호스트 지원 등급(D4)을 표시한다 | status 출력, `core/hook_integrity.py` | 지금 run 시작 검사는 등록만 확인한다(`hook_integrity.py:179-212`). 문서에도 “Hook registration is not proof that a host executes hooks”라고 적혀 있다(`docs/USAGE.md:545`) | run을 막는 새 gate는 추가하지 않는다(D4) |
| A5 | Claude·Codex·OMP 공통 검증을 한다. 실행 전 차단, tripwire, binding, 명시적 재개를 확인한다 | 테스트, 실제 호스트 smoke | 세 호스트 모두 같은 훅 스크립트를 등록한다(6절 표) | 5절 leader 탐지 회귀 금지 목록을 함께 검증한다 |
| A6 | YAML 중복 키를 거부한다. 배포 YAML은 CI 테스트로, `.agent-flow/profiles/<id>.local.yaml`은 로딩할 때 검사한다(D9) | 새 CI 테스트, `core/profiles.py:547`, `:626`, `:679` | PyYAML `safe_load`는 같은 키가 두 번 나오면 뒤엣것으로 덮어쓴다. 배포 workflow·profile YAML 20개를 중복 키 검사 로더로 읽어 보니 중복이 없었다 | 고정된 run을 재개하는 경로(`core/workflow_pin.py` `_load_pin`)에는 적용하지 않는다 |

**하지 않는 것:**
- 플러그인 형태 전환(D11), 전역 최신 런타임, 플러그인 cache에서 직접 실행, 스킬만 배포.
- tripwire 제거, 실행 전 쓰기 대상 추측, 경로 예외 추가(5절 이력).
- 보호 브랜치 차단을 git hook으로 옮기기(철회).
- run 시작 때 런타임·YAML digest 대조(D3).
- 종료 단계 도달 검사를 로더에 추가. `eda851a`(#287)의 테스트가 내장 workflow 6개 전부에 대해 이미 검사한다(`tests/test_phase_workflow.py`).

---

## 3. 최종 제안: 도메인 그릴링

**관찰된 문제** [출처: GRILL 패인 보고, 원본 run 로그는 대조하지 않음]
- `domain-grill`이 질문 0개로 통과된 run이 있었다: web_qa_skill(Codex, 10-02), chatbot(08-27), kroute(09-04).
- web_qa_skill run에서 에이전트가 PRD pause를 `--approve <hash>`로 스스로 통과했다.

**코드에서 확인한 원인** [확인]
- 질문·답 없이 marker만 있는 artifact로 `domain-grill`에서 `product-brief`로 넘어가는 것이 테스트로 고정돼 있다(`tests/test_cli.py:5117-5147`).
- `domain-grill`에는 pause가 없다. full-feature에서 pause가 있는 단계는 `prd`뿐이다(`full-feature.yaml:55`). default는 `slice-plan`이다(`default.yaml:163`).
- 프롬프트가 값까지 채운 완료 marker 블록을 보여 준다(`full-feature.yaml:33-37`).
- `domain-grill`에는 스킬 읽기 gate가 없다(`core/skill_resolver.py:76`, `:1089`, `core/local_skills.py:247`).
- 승인이 필요하면 `next_command`에 `--approve <token>`이 붙는다(`runner.py:2722-2726`, `artifact.py:293-294`). `[approval]` 줄에도 출력된다(`runner.py:976`).
- 토큰은 run id, phase, 진입 시각, artifact sha256만으로 계산된다(`artifact.py:541-549`). 승인 함수 주석은 “Acknowledge these artifact bytes, not the caller's identity”다(`artifact.py:562`).
- 진입 스킬에 “artifact를 쓰면 `next_command`를 실행하라”(`skills/agent-flow/SKILL.md:67`)와 “design or slice review로 멈추면 사용자 승인을 기다려라”(`:68`)가 함께 있다. `:68`은 full-feature의 PRD pause를 언급하지 않는다.
- 생성되는 workflow 계약 문구도 “must follow the runner's printed `next_command` exactly”라고 쓴다(`bin/agent-flow-kit.mjs:2543`).

### P1. pause 승인 명령을 `next_command`에서 분리 (1단계)
- **변경:**
  - 승인 대기 중 `next_command`에서 `--approve <token>`만 뺀다.
  - 승인 명령은 별도 `approval_command:` 줄과 `status_json` 필드로 내보낸다. SPEC 확인의 `spec_confirm_command`(`cli.py:3805-3811`)와 같은 모양이다.
  - `SKILL.md:68`을 모든 pause를 대상으로 고친다. 내용은 “artifact를 요약해 채팅으로 묻고, 그 요약에 사용자가 답한 뒤에만 `approval_command`를 실행한다. pause 이전의 승인은 세지 않는다”이다.
  - `bin/agent-flow-kit.mjs:2543` 계약 문구와 `docs/USAGE.md`의 승인 설명도 같은 기준으로 고친다.
- **영향 범위:**
  - 코드: `runner.py:961-983`, `:2722-2726`, `artifact.py:290-294`, `core/workflow_status.py`
  - 테스트: `tests/test_approval_status.py:12-54`(토큰이 `next_command`에 있다고 단언하는 계약 테스트). `tests/test_runner_smoke.py:191`, `:421`과 `tests/test_t6_run_pin.py`는 CLI에 `--approve`를 직접 넘기는 테스트라 CLI 인자가 그대로면 바뀌지 않는다.
- **보존할 것 [확인]:**
  - `continue --approve TOKEN` CLI 인자(`cli.py:329-333`).
  - 표시된 artifact만 승인하고 내용이 바뀌면 거부하는 동작(`tests/test_approval_status.py:49-54`, #216).
  - **`next_command`의 `--worktree <name>` 선택자.** 세션 binding이 status 출력의 `next_command`를 파싱해 worktree를 찾는다(`core/host_write_boundary.py:267-279`).
  - Stop 안내는 `status_command`만 출력하고 `next_command`와 토큰은 출력하지 않는다(`host_write_boundary.py:2221-2228`). 이 동작을 바꾸지 않는다.
  - 사용자에게 정확한 문구나 터미널 입력을 요구하지 않는다(`SKILL.md:49`).
  - lifecycle 예외 범위(`{continue, run, start, status}`, `host_write_boundary.py:106`).
- **증명 장치가 아니다:** 토큰은 계산할 수 있고 승인 함수는 호출자를 확인하지 않는다(위 근거). 의도적인 자가 승인은 여전히 가능하다.
- **확인 필요:** Codex를 포함한 실제 호스트에서 이 변경이 자가 승인을 줄이는지.

### P2. domain-grill 개선 (2단계)
- **변경 (workflow YAML만):**
  - 태스크나 저장소가 가리키는 미리 한 인터뷰 결과, `GLOSSARY.md`, `docs/adr/`의 결정은 재사용한다.
  - 빈 결정만 한 라운드씩 묻고 턴을 끝낸다.
  - 결정마다 출처를 `## Decision Sources`에 기록하고, 이 제목을 필수 marker로 넣는다.
  - 질문이 0개여도 요약을 보여 주고 확인을 받는다. 문서는 사용자 동의가 아니다.
  - `domain-grill`에 `pause_after: true`를 붙인다. 새 상태는 만들지 않는다. 기존 pause 처리(`runner.py:961-983`)와 전이 검사(`runner.py:1382-1386`)를 그대로 쓴다.
  - 완료 marker 예시를 `<true|not_needed>` 같은 자리표시로 바꾼다.
  - `default.yaml`의 design 단계에는 재사용 규칙만 적용한다.
- **영향 범위:** `full-feature.yaml` `domain-grill`, `default.yaml` design 1절, `tests/test_cli.py:5117-5147`(marker만 있으면 `product-brief`로 넘어가는 테스트), `evals/phase_budget.py` 비교, `evals/phase_eval.py`.
- **보존·회귀 방지:**
  - 첫 단계에 pause가 생기므로 #237의 첫 단계 진입 전 status와 cursor 테스트를 함께 검증한다. #237이 지정한 테스트는 `tests/test_run_cursor_and_transitions.py::test_a_progressed_cursor_without_a_current_phase_stops`, `test_an_empty_current_phase_is_corruption_not_an_absent_name`, `test_phase_index_past_the_workflow_stops_instead_of_completing`, `test_phase_index_that_disagrees_with_current_phase_stops`다.
  - #224의 workflow 고정 때문에 새 run부터 적용된다.
  - 원본에서 가져온 `grilling`, `grill-with-docs` 스킬 본문은 고치지 않는다. 고치면 `skills/UPSTREAM.md`에 분기로 기록해야 한다(`docs/mattpocock-skills-upstream-audit.md`).
  - marker 파서가 자리표시를 그대로 복사한 값을 거부하는지 검증한다(`033310d` 이후 파서).
  - P1이 먼저 들어가야 한다. P2만 넣으면 승인 명령이 `next_command`에 섞인 상태라 PRD와 같은 경로로 자가 승인될 수 있다.

### 지금 하지 않는 것: 사람 승인 증거 검사 (D5)
- **이력 [확인]:**
  - #134: SPEC 항목 검증 도입.
  - #151: 채팅 `승인`을 `UserPromptSubmit` 훅으로 runner에 전달. PR 본문에 따르면 4개 층의 원인으로 모든 호스트에서 기록이 안 되던 것을 고쳤다.
  - #160: OMP `before_agent_start`에서 정확한 승인 turn을 기록.
  - #162: “Replace persistent provenance approvals with run-local agent confirmation”. 승인 훅 3개(`prepare-spec-user-prompt.py`, `confirm-spec-user-prompt.py`, `guard-spec-approval.sh`)를 삭제했다.
  - 이 훅들은 지금 은퇴 목록에 있다(`lib/managed-hooks.mjs:15-21`). 테스트가 OMP extension에 다시 등록되지 않는지 검사한다(`tests/test_cli.py:2031-2038`).
  - #222 결론: “승인 이벤트를 다시 신뢰 경계로 만들려면 누가 기록할 수 있는지부터 별도 결정해야 한다. 전역 hook 복원이나 정확한 승인 문구 강제는 이번 권고에 포함하지 않는다.”
- **다시 하려면 필요한 것:** (1) D5를 뒤집는 사용자 결정, (2) Claude·Codex·OMP·generic에서 사람 입력과 자동 입력을 구분할 수 있는지 실측, (3) 교착을 피하는 설계.
- 두 리뷰가 모두 지적했듯 이걸 하게 되면 첫 대상은 `merge-approval`이다.

**하지 않는 것:** 하위 상태(`questions_open` → `awaiting_confirmation`), fail-closed와 `start --no-grill`, 질문 개수 하한이나 `## Rounds`를 증거로 쓰기, `domain-grill`에 스킬 읽기 gate 추가, 단계 건너뛰기 route, 인터뷰 전용 workflow, 채팅 원문 전체를 저장하는 원장.

**남는 사항 [확인]:**
- `merge-approval`에는 pause도 토큰도 없다. 프롬프트는 사용자에게 merge 승인을 받은 뒤에만 에이전트가 `verdict: approve`를 쓰라고 지시한다(`full-feature.yaml:791-800`).
- 의도적인 자가 승인은 P1과 P2 뒤에도 가능하다(`artifact.py:541-549`, `:562`).

---

## 4. 실행 계획 (D7, D8, D13)

| 단계 | 내용 | PR | 순서 이유 |
|---|---|---|---|
| 1 | P1(승인 명령 분리). 진행 중 | PR 1개. merge 안 함 | P2와 H1이 P1의 `approval_command`를 쓴다 |
| 2 | P2(domain-grill 개선) | PR 1개. merge 안 함 | D7에서 정한 순서 |
| 3 | H1(#290, merge 전 승인을 runner가 강제) | PR 1개. merge 안 함 | 반영되면 D8을 runner가 강제한다 |
| 4 | A3, A4, A5 (호스트 연결 검증) + H2(#195, #196, #197) | PR 1개. merge 안 함 | 모두 호스트 연결과 훅 쪽 변경이다. #195 댓글에 따르면 #195-#197을 한 릴리스로 묶을 때 훅 matcher·digest 변경에 따른 재설치를 한 번으로 끝낼 수 있다 |
| 언제든 | A6(YAML 중복 키 거부) | 별도 PR. merge 안 함 | 작고, 강제를 더하기만 하는 변경 |

**공통 조건:**
- 작업 전에 `maintainer/verification-policy.md`를 읽고 변경 영향에 맞는 검증을 고른다(`AGENTS.md`).
- 이 저장소 profile은 `python`이다(`.agent-flow/kit.json`). CI-only gate는 로컬에서 실행하지 않는다(`maintainer/verification-policy.md:48`).
- 버그 수정에는 수정 전에 실패하고 수정 후에 통과하는 회귀 테스트를 둔다.
- worktree는 `agent-flow worktree create --name feat-<slug>`로 만든다. leader 브랜치를 바꾸지 않는다(`AGENTS.md`).
- **leader 상태:** 이 문서와 원자료 폴더가 leader에 untracked로 있어서 leader가 dirty다. 진입 게이트가 `--allow-dirty` 없이 막을 수 있다(`core/worktrees.py:212-213`, `:366-371`, `:3590-3614`). `--allow-dirty` 사용 여부는 사용자가 정한다.

**회귀 대조 (모든 작업, 2026-10-10 사용자 지시):**
- 회귀 금지 기준은 5절 표와 5.1 원장(`maintainer/2026-10-09-plugin-evolution-raw/regression-ledger/ledger.md`)이다. 원장에는 과거 버그 PR·이슈가 전부 들어 있다. 5절 표만으로는 부족하다. 5절은 이 계획이 건드리는 강제 장치만 담는다.
- run task 문구에 "회귀 금지는 5절과 5.1 원장 전수 대조"를 넣는다.
- design
  - 원장에서 바꿀 영역의 topic, 바꿀 파일·함수를 다룬 항목을 모두 고른다.
  - 바꿀 함수마다 `git log -L`로 그 함수를 고친 PR을 찾는다. 원장에 없는 PR이면 더한다.
  - 고른 항목과 각 회귀 금지 조건을 design에 적는다.
- PR 전
  - 고른 항목의 guard 테스트를 node id로 돌린다.
  - guard가 없거나 조건을 다 덮지 못하면 base와 head를 같은 배치로 실행해 대조한다.
- PR 본문에 "회귀 대조" 절을 적는다. 담을 것: 고른 항목 수, guard 결과, guard 없는 항목의 판단, 이전 PR이 다루지 않은 동작 변화.
- merge 뒤 그 PR을 원장 7.14 형식(회귀 금지 조건, guard)으로 더한다.

**강제력 비약화 조건 (D12, 모든 PR):**
- 플러그인을 빼도 강제 수단은 줄지 않는다. 플러그인은 원래 installer나 훅을 실행하지 않았다(`lib/plugin-bundle.mjs:77`).
- 기존 강제 장치는 제거하거나 완화하지 않는다. 대상: 실행 전 차단 2규칙, tripwire, 세션 binding, 실행 기록, 필수 marker, pause와 승인 토큰 결속, 2명 이상의 독립 reviewer, runner가 실행하는 gates, route 그래프 불변식.
- 이 장치를 지키는 테스트는 지우지 않고, 단정을 약하게 바꾸지 않는다. 예: `test_pre_block_surface_stays_two_rules`, `test_lifecycle_exemption_stays_narrow`(`tests/test_host_write_boundary.py:2145`, `:2175`), `test_every_packaged_workflow_reaches_completion_and_bounds_its_rework_cycles`(`tests/test_phase_workflow.py:456`), `test_node_workflow_run_blocks_phase_skip_until_artifact_exists`(`tests/test_cli.py:4929`), `tests/test_worktree_tripwire.py`, #237 지정 테스트.
- 계약이 바뀌어 테스트를 고쳐야 하면 같은 불변식을 더 엄격하게 단정한다. P1의 `test_next_command_can_approve_only_the_displayed_artifact`(`tests/test_approval_status.py:13`)를 고칠 때 단정할 것은 세 가지다. `next_command`에 토큰이 없다. `--approve` 없이 `continue`하면 계속 막힌다. `approval_command`의 토큰은 표시된 artifact만 승인하고 내용이 바뀌면 거부한다.
- 새 예외, 우회 플래그, 단계 건너뛰기 route, 경로 예외를 추가하지 않는다.
- 표시 기능(A4)은 기존 run 시작 검사(`core/hook_integrity.py:179-212`)를 완화하지 않는다. D4의 “표시하고 허용”은 지금 동작이다. 훅이 없는 호스트는 generic 어댑터로 이미 실행된다(`adapters/auto.py:28-37`).
- PR 본문에 “강제력 비약화 확인”을 적는다: 바뀐 강제 장치, 그대로 통과한 강제 테스트, 실행한 검증.

**강화 항목 (D13으로 계획에 포함) [확인]:**
- **H1. merge 전 사람 승인을 runner가 막게 한다.** 이슈 #290. 3단계.
  - 현재: default workflow는 `pr-watch`가 `green`이면 바로 `merge`로 간다(`default.yaml:602-604`). default에는 `merge-approval` 단계가 없다.
  - runner의 `merge` 진입 검사는 SPEC 증거만 본다(`runner.py:619-631`). `guard-protected-branch.sh`에는 merge 규칙이 없다.
  - full-feature의 `merge-approval`에는 pause가 없다(`full-feature.yaml:791-800`).
  - 그래서 D8의 “merge는 PR마다 사용자 승인”은 지금은 에이전트가 지시를 따르는지에 달려 있다.
  - 제안: P1의 `approval_command`와 기존 pause 처리로 merge 직전에 멈추게 한다. 새 상태는 만들지 않는다.
  - 영향: `default.yaml`, `full-feature.yaml`, route 테스트, `phase_budget`.
  - 장점: D8을 runner가 강제한다. 단점: merge하는 run마다 멈추는 지점이 하나 늘어난다. 또 P1 규칙상 pause 이전의 승인은 세지 않으므로, 미리 merge를 허락해 둘 수 없다.
- **H2. 열린 이슈 #195, #196, #197: Devin에서 강제가 반쯤만 켜지는 문제.** 4단계에서 A3·A4·A5와 함께 한다.
  - #195: `exec` 도구 이름이 어떤 matcher에도 없다(`core/host_write_boundary.py:34`, `scripts/hooks/worktree-tripwire.py:9-11`). 그래서 Devin에서는 보호 브랜치 차단, 쓰기 경계, tripwire, 실행 기록이 동작하지 않는다.
  - #196: exit code가 없는 호스트에서는 `red-observed` 검사가 통과한다(`core/command_evidence.py:452`, `:462`).
  - #197: `.devin`이 sandbox 쓰기 금지 목록에 없다(`providers/subprocess.py:35`).
  - 세 문제 모두 현재 HEAD에 그대로 있다. 이슈 본문은 claude, codex, omp에 회귀 위험이 없다고 적고, 승인 조건도 적어 두었다.
  - 장점: D1 범위가 넓어진다. 단점: 훅 matcher와 digest가 바뀌어서, 설치된 프로젝트마다 한 번씩 다시 설치해야 한다(#195 본문).

---

## 5. 회귀 방지 근거: 관련 PR·커밋·이슈

| 대상 | 내용 [확인] | 이번 제안이 지킬 조건 |
|---|---|---|
| #88 (`3f91fbb`) | 실행 전 쓰기 차단 훅 `guard-worktree-write.py` 등록 해제. leader 쓰기·부분 수정·bash 변형을 막다가 버그가 잦았고 launcher-trust 데드락이 있었다 | 실행 전 추측 차단을 되살리지 않는다 |
| #89 (`c0ac756`) | runner `mutation_boundary` 제거. 외부 세션의 leader 변경만으로 예외가 났고, 기준선 정리 전 예외로 데드락이 생겼다 | leader 변경 때문에 복구 경로를 막지 않는다 |
| `ba90a6f` | tripwire를 탐지 전용으로. `reset --hard`가 사용자 미커밋 작업을 지웠다 | tripwire가 leader를 변경하지 않는다 |
| `1d2408a` | 셸 쓰기 범위 추측 제거, PostToolUse tripwire 도입 | 추측 표를 되살리지 않는다 |
| `67eb776` | 다른 worktree의 진행 중 변경으로 tripwire가 차단하지 않게 함 | 유지 |
| `5067004` | 실행 전 차단을 2규칙으로 축소. 되돌릴 수 있는 동적·symlink 쓰기는 PostToolUse tripwire가 명령마다 잡는다는 전제 | tripwire 훅 유지. `test_pre_block_surface_stays_two_rules`(`tests/test_host_write_boundary.py:2145`) |
| `786794c`, `e184ae9` | lifecycle 명령을 tripwire 앞에 둠. fast-forward는 같은 브랜치·조상·leader reflog 세 조건일 때만 정상. lifecycle 예외 확대안 철회 | `test_lifecycle_exemption_stays_narrow`(`tests/test_host_write_boundary.py:2175`) |
| `b00bedc` | 스냅샷 형식 차이는 재캡처하고 오염으로 보고하지 않음 | 유지 |
| `88eafbf`, `ada6624`, `a077b8b` | git이 침묵하는 경로도 봄. 경로 예외 대신 보고된 상태만 `--accept-leader-drift`로 승인하고 HEAD 축은 승인 불가. 범위는 `all \| tracked-only` 두 값 | 경로 예외를 추가하지 않는다 |
| 현재 tripwire 훅 | `scripts/hooks/worktree-tripwire.py:45-84`: bash류 명령만, binding된 세션만, leader만, `include_ignored=False`. 필수 관리 훅(`core/hook_integrity.py:54`, `:93-96`) | 유지 |
| #134 → #151 → #160 → #162 | 채팅 승인 훅을 구현했다가 run-local agent confirmation으로 교체 | D5. 훅 기반 승인을 되살리지 않는다 |
| #216 (`f47da81`) | 승인·리뷰 증거를 run·phase·산출물에 결속. `test_next_command_can_approve_only_the_displayed_artifact` | P1에서 토큰 위치만 바꾸고 결속은 유지한다 |
| #222 | 승인 범위 제어 조사. 훅 복원과 정확한 승인 문구 강제를 하지 않는다. 기존 수정과의 충돌 금지선 표 | D5 |
| #224 | workflow 고정 | YAML 변경은 새 run부터 적용 |
| #237 | 첫 단계 진입 전 status 오분류 수정과 필수 회귀 테스트 목록 | P2에서 함께 검증 |
| #242 | 쓰이지 않는 `.agent-flow/workflows` 사본 설치 중단 | 프로젝트 workflow 덮어쓰기 경로가 없다 |
| #258 | phase skill 전달·토큰 측정. `phase_budget` CI 비교 | 프롬프트 변경 PR은 `phase_budget` 비교가 필요하다 |
| #265, `docs/mattpocock-skills-upstream-audit.md` | upstream pin 4588b32. 원본 스킬 분기는 `skills/UPSTREAM.md`에 기록 | P2는 workflow YAML만 고친다 |
| #287 (`eda851a`) | 내장 workflow route 그래프 불변식 테스트 | 로더에 중복 구현하지 않는다 |
| #102 | 코드 0줄로 push-pr 통과, 단계 간 수치 전달 부재 → 설계 원장과 실행 관측 도입 | `## Decision Sources`는 에이전트가 쓰는 기록이고 증명이 아니다 |
| `033310d` | 존재하는 marker를 완료 gate가 놓치던 문제 수정 | P2의 자리표시 예시와 파서를 함께 검증 |
| #178 | fix-loop 상한 | 변경 없음 |
| 열린 이슈 #195, #196, #197 | exec 도구 이름이 matcher에 없음, exit code 없는 host에서 red gate 통과, `.devin` 상태 경로 | 이번 제안 범위 밖. 훅 기반 기능을 추가할 때 고려 대상 |

### 5.1 과거 버그 원장 (전수)

- 위치: `maintainer/2026-10-09-plugin-evolution-raw/regression-ledger/ledger.md`. 증상·원인·수정은 같은 폴더의 `bug-ledger.json`에 있다.
- 범위
  - PR #1–#291과 이슈 전체에서 버그로 분류한 173건(PR 138, 이슈 35)
  - 그 뒤 merge된 #292–#295, #300, #314(7.14절)
  - 항목마다 회귀 금지 조건과 guard 테스트(pytest node id)를 적었다.
- 경위
  - 10-09 03:25 사용자 지시("기존 버그 PR 및 이슈 전부 확인해서 회귀 금지 포함")로 적대적 리뷰 입력 `/tmp/af-adv-review/brief.md` §7을 만들었다.
  - 그 목록을 이 문서에 옮기지 않았다. 그래서 "회귀 금지는 5절"만 가리킨 이후 run(P2부터 #314까지)에는 전수 대조가 빠졌다. #314는 merge 뒤에 대조했다(5.2).
  - 2026-10-10 지금 위치로 옮기고 4절에 "회귀 대조" 절차를 넣었다.

### 5.2 #314 merge 뒤 회귀 대조 (2026-10-10)

**한 것**
- 원장에서 관련 항목 57건을 골랐다(install 탐색, sandbox, hook, worktree, leader). 모든 guard 테스트가 #314 CI 전체 pytest에서 통과했다.
- sandbox 계보 조사(#140, 4d01ff7, #103·#128·#129, #27, #142, #152·#153·#161, #165): 회귀 0건.
- install-root 계보 조사(#100 P0, #29, #19, #15, #151, #152, #153, #155, #158, #161, #164, #295, #6)
  - Python과 OMP를 24개 배치에서 base·head로 나란히 실행했다.
  - 실제 CLI(`start`/`continue`/`status`)도 실행했다.
  - guard 71개를 직렬로 돌렸다: base 140 passed, head 138 passed. head 실패 2건은 node install timeout이다. 따로 다시 돌리면 통과하고, base도 같은 timeout을 냈다.

**발견**

| 구분 | 내용 | 영향 | 근거 |
|---|---|---|---|
| head 전용 실패 | HOME이 없고 uid에 passwd 항목도 없으면 `find_install_root`가 `RuntimeError: Could not determine home directory.`를 낸다. 시작점에 설치본이 있어도 그렇다. base는 시작점을 돌려줬다 | 그런 환경에서 run 시작 검사와 status가 예외로 멈춘다. 드문 환경이다 | `/tmp/a3h_home_unset.py` base·head 대조 |
| 이전 guard 변경 | #100 P0 guard `test_install_root_resolves_from_a_nested_worktree`의 fixture를 git 저장소로 바꿨다. 원래 단정(git 밖·HOME 밖에서 조상 설치본을 찾음)은 head에서 실패한다 | 사용자 영향은 거의 없다(run/start는 git 아닌 root를 받지 않음). slice-plan에서 알리고 승인받았지만(D-3) 원장 절차상 명시 결정으로 남겨야 할 guard 변경이다 | base 테스트를 head 소스로 실행: 1 failed |
| 동작 변화 | 설치된 프로젝트 안의 독립 git clone(중첩 저장소)에서 `run`/`start`/`continue`가 `installation could not be found`로 멈춘다(HOME 안팎 모두) | base는 부모 프로젝트 설치본으로 검사를 통과시켰지만 그 checkout에는 hook이 등록되지 않았다. 막는 쪽이다. base에서 시작한 그런 run은 업그레이드 뒤 `continue`가 막힌다 | 실제 CLI: head rc=2, base rc=0 |
| 동작 변화 | HOME이 dotfiles 저장소이고 거기 설치한 경우, 관리형 worktree의 status 카드가 install `not_found`, claude `registered=missing`, `runner_only`로 보인다 | 표시만 바뀐다. run 시작 검사, CLI root, OMP는 같다. Claude/Codex hook은 절대경로 등록이라 실제로 돈다. 즉 카드가 보호를 낮게 보고한다 | 실제 `agent-flow status` base·head |
| 동작 변화 | `.agent-flow`를 git이 추적하는 저장소에서 team worker가 merge하면 `.agent-flow/project.yaml` unlink가 막혀 중간에 실패하고 dirty 상태로 남는다 | 설치 기본값(`.agent-flow/` ignore, #270)에서는 생기지 않는다 | `sandbox-exec` 재현 base rc=0, head rc=1 |
| 동작 변화 | superproject가 HOME 밖인 submodule에서 시작하면 설치본을 찾지 못한다 | CLI는 base·head 모두 뒤에서 다른 이유로 rc=2다(기존 버그). head는 더 일찍, worktree를 남기지 않고 멈춘다 | 실제 CLI |
| 위험(기존과 같은 유형) | checkout `.agent-flow`가 symlink면 실경로 deny가 workspace나 `/`까지 막는다 | 막히는 쪽이다. `.claude` 등에서 #140부터 같은 유형이 있었다 | `sandbox-exec` 재현 |
| 위험(이전부터 있음) | HOME=/에서 Python과 OMP가 다르다 | base와 같다. #314가 만든 것이 아니다 | base·head 같음 |
| 위험 | `find_install_root`와 evidence hook의 `find_project_root`가 다른 규칙을 쓴다 | 위 dotfiles 카드 차이의 원인이다. #302 범위다 | — |

**처리(2026-10-10, #315 merge `5a4497c`):**
- 사용자 결정 (a): 후속 PR 하나로 처리하고 나머지 동작 변화는 의도한 강화로 둔다.
- head 전용 실패: 처음에는 HOME 없이 탐색을 계속하게 고쳤다(`8dcaf3d`). Codex P1이 그 환경에서 OMP extension이 `os.homedir()` 예외로 로드되지 않는다는 것을 짚었고 재현으로 확인했다. 사용자 결정 (A)로 run 시작 검사가 "home directory could not be determined" `HookIntegrityError`로 멈추게 바꿨다(`cdc4b35`). base(`74707c9`)는 그 환경에서 OMP hook 없이 run을 시작시켰으므로, 이 결과는 base보다 엄격하다.
- guard 변경: #100 P0 guard가 두 배치(git 저장소 안 → 저장소 설치본, git 밖·HOME 밖 → `None`)를 단정하고 docstring에 결정을 적는다.
- 원장 7.14에 #315를 더했다. 남은 빈틈(다른 `Path.home()` 호출, `XDG_STATE_HOME`이 없을 때 CLI traceback, `HOME=""` 차이)은 그 항목에 적었다.
- 사용자가 pre-merge SPEC-5·6 승인 기록 전에 merge해 #301 상태가 다시 났다. run `20261010-102051`을 abort하고 worktree를 정리했다.

---

## 6. 근거: 확인된 코드 사실

| 주제 | 위치 [확인] |
|---|---|
| runner가 단계 판정, run 시작 전 훅 무결성 검사 | `runner.py:437-487`, `:480`, `core/hook_integrity.py:179-212` |
| 상태 전이: route 계산 → `transitions.jsonl` → `meta.json` → `events.jsonl` | `runner.py:273-378`, `:1303-1440`, `:1715-1757`, `core/observation.py:25-28` |
| 다음 단계 전달: 이전 artifact 본문은 프롬프트에 넣지 않음. 설계 원장(`design-spec.md`)만 주입 | `adapters/base.py:180-182`, `:219-234`, `core/design_ledger.py:1-12` |
| workflow 고정·재개 검증 | `core/workflow_pin.py:28-56`, `:59-97`, `:100-138` |
| 새 run의 workflow 위치(설치 런타임 사본) | `core/phase_workflow.py:251-292`, `:327-356` |
| 설치 런타임은 staleness 검사에서 제외 | `core/kit_digest.py:79-116` |
| YAML 구조 검증 | `phase_workflow.py:383`, `:438-450`, `:524-528`, `:558-567` |
| launcher가 쓰는 인터프리터 절대경로와 digest 검사 | `lib/installer-shared.mjs:2945-2982`, `core/hook_integrity.py:452-527` |
| 런타임이 없을 때 JS 진입의 kit `src` fallback | `bin/agent-flow-kit.mjs:2580-2591`, `:2640-2643` |
| 호스트 어댑터 (claude/codex/omp, 나머지는 generic) | `adapters/auto.py:25-49`, `adapters/generic.py:1-16` |
| 훅 등록 (Claude / Codex / OMP) | 실행 전 차단 `guard-host-worktree.sh`: `.claude/settings.json:18` / `.Codex/hooks.json:18` / `.omp/extensions/agent-flow-hooks.ts:143-144`. tripwire: `:59` / `:59` / `:189`. binding `bind-host-worktree.py`: `:55` / `:55` / `:177`. 실행 기록 `record-command-run.py`: `:51` / `:51` / `:176` |
| 훅 스크립트가 호출하는 Python | `guard-host-worktree.sh:24`, `worktree-tripwire.py:26-27`, `bind-host-worktree.py:21`, `record-command-run.py:118` |
| 플러그인 pack | `bin/agent-flow-plugin.mjs:1-22`, `lib/plugin-bundle.mjs:71-92`, `:218-305`, `package.json:6`, `:13` |
| reviewer 격리 범위 | `docs/USAGE.md:547-548` (macOS `sandbox-exec`만 검증, 다른 OS는 fail closed) |

---

## 7. 대화 경과

1. **Dryforge 설치 형태:** 각 에이전트 CLI의 플러그인 marketplace로 설치한다. 프로젝트에는 `.dryforge/` 작업 기록, `docs/`, `AGENTS.md`/`CLAUDE.md`가 남는다. Claude Code 플러그인 설치 범위는 user/project/local이다. 출처: [README_ko](https://github.com/prekuter/dryforge/blob/main/README_ko.md), [Claude Code 문서](https://code.claude.com/docs/en/discover-plugins).
2. **`.dryforge/`와 `.agent-flow/` 비교:** 이때 Agent Flow에 플러그인 형태가 없는 것처럼 답했는데 틀렸다. `agent-flow-plugin`과 `plugin:pack`이 이미 있다.
3. **Agent Flow 형태:** 처음에는 “독립 CLI(전역 공유 런타임) 본체”를 권했다가 철회했다.
4. **두 모델의 플러그인 전환 제안:** Claude Opus 5.5 xhigh와 Codex gpt-6-astra xhigh(8절). 진행 중에 사용자가 D1을 지정했다. 두 모델 모두 core + 호스트 연결 + 선택형 플러그인 + 프로젝트 고정 런타임을 권고했다. 다만 입력 자료가 그 방향을 유도했다.
5. **종합안 적대적 자기 검토:** 철회·정정 항목은 9절에 있다.
6. **훅 필요성:** 단계 판정에는 훅이 필요 없다. 훅으로만 할 수 있는 건 실행 전 차단, 명령 직후 탐지, 세션 정보다.
7. **leader 오염 탐지 롤백 이력:** 5절 표. 롤백된 건 실행 전 추측 차단과 runner hard-raise다. 실행 후 tripwire 훅은 지금도 세 호스트에 등록돼 있다.
8. **최종 형태와 지원 등급:** 2절과 D4.
9. **YAML 수정 우려:** 진행 중 run은 고정본으로 보호된다. 새 run은 설치본 YAML을 그대로 쓰고, 설치 런타임 내용을 검증하는 경로는 없다(6절). 사용자가 digest 대조를 제외했다(D3).
10. **워크플로 형식 조사:** Claude Opus 5.5 high(8절). 조사 결과 [출처: 모델]: 강제하면서 사람이 편집하는 정의는 YAML과 엔진, 개발자용은 코드 그래프, Markdown 스킬은 안내용이다. 직접 원문을 확인한 건 Spec Kit 워크플로 문서와 Archon Zod 스키마다. 권고를 대조한 결과 `safe_load`, phase·skills 모르는 키 거부, 실행마다 digest 고정은 이미 있다. 중복 YAML 키 거부는 없다(A6).
11. **상태 전이와 전달 방식:** 6절의 상태 전이·다음 단계 전달 행.
12. **domain-grill 필요성:** 인터뷰 결과는 매번 필요하지만 인터뷰 행위는 매번 필요하지 않다(P2).
13. **GRILL 패인 조사(OMP, Opus 5.5):** 3절 관찰된 문제. 그 밖의 주장 [출처: GRILL 패인]: 원본 mattpocock grilling 대비 비교, OSS 비교(AI-DLC, oh-my-codex, Archon, gentle-ai, GSD-2), Codex Default 모드 템플릿 문구. 원본 grilling 문서 인용 두 개(“The skill is finished when you say the understanding is shared”, “another skill runs grilling … no async mode”)는 직접 확인했다.
14. **패인 결과 대조 리뷰와 두 번째 적대적 리뷰:** Claude Opus 5.5 xhigh와 Codex gpt-6.1-sol xhigh(8절). 두 리뷰가 공통으로 지적한 것은 토큰 제거를 분리로 바꾸기, 하위 상태·fail-closed·`--no-grill` 제외, B안은 고쳐서 유지, `merge-approval` 누락이다. Opus는 승인 훅 폐기 이력을 찾았고, 이 세션에서 #151·#162로 확인했다.
15. **PR·이슈 회귀 대조:** 5절.
16. **결정 확정:** 1절 D4-D10. 이때 1단계는 A2 + P1이었다.
17. **지금 형태 vs 플러그인 형태:** 사용자가 “오픈소스이고 마켓에 배포하지 않아도 플러그인 형태가 좋은가”를 물었다. 강제 수단은 같고, packer 플러그인은 Claude·Codex용이라 OMP는 기존 extension을 쓰며, 설치 경로만 2개가 된다고 답했다(2절 비교). 답변 중 “OMP에는 플러그인 경로가 없다”고 말했는데, OMP의 플러그인 지원 여부는 확인하지 않은 것이라 정정한다. 사용자가 지금 형태를 유지하기로 했고(D11), 1단계에는 P1만 남았다.
18. **강제력 비약화 (D12):** 사용자가 “플러그인을 빼도 강제력은 절대 약해지면 안 된다”고 지시했다. 4절에 모든 PR이 지킬 조건을 적었고, 진행 중인 P1 run(패인 `w15:pK`)에도 전달했다. 확인 과정에서 강제 빈틈 두 가지(H1, H2)를 찾았다. 계획에 넣을지 물었을 때 사용자는 선택하지 않았다. 이후 “이슈에 기록만 해놔”라고 지시해서, H1은 새 이슈 #290으로 기록했다. H2는 이미 #195, #196, #197로 열려 있어 #290 본문에 링크만 걸었다.
19. **H1·H2 계획 포함 (D13):** 순서를 그대로 두고 H1과 H2를 계획에 넣자고 권했고, 사용자가 그대로 채택했다. 최종 순서는 P1 → P2 → H1(#290) → A3·A4·A5 + H2(#195-#197)이고, A6은 언제든 한다.

---

## 8. 외부 모델·패인 실행 기록

| 용도 | 요청 | 확인된 출처 정보 [확인] | 원자료 |
|---|---|---|---|
| 플러그인 전환 제안 | Claude `claude-opus-5-5`, `--effort xhigh`, `--tools ''` | 반환 `canonicalModel=claude-opus-5-5`, `is_error=false`, `num_turns=11`. effort 필드 없음. 본문에 저장소를 읽었다고 적혀 있다 | `raw/opus-proposal.md`, `raw/opus-response.json` |
| 플러그인 전환 제안 | Codex `gpt-6-astra`, `model_reasoning_effort="xhigh"`, read-only | exit 0, `turn.completed`, 오류 이벤트 없음. 모델·effort 필드 없음. 도구 이벤트 0 | `raw/astra-*.json*`, `raw/astra-proposal.md` |
| 워크플로 형식 조사 | Claude `claude-opus-5-5`, `--effort high`, WebSearch/WebFetch | `canonicalModel=claude-opus-5-5`, `num_turns=69`. 웹 검색 8회는 `claude-haiku-5-5`로 기록됨 | `raw/format-research*.md`, `raw/format-research-response.json` |
| GRILL 패인 조사 | OMP 패인 (상태줄 표시 Opus 5.5) | 최종 답변과 사용자 메시지는 세션 기록에서 그대로 추출했다 | `raw/grill/pane-*.md`, `raw/grill/pane-reports/` |
| 그릴링 적대적 리뷰 | Claude `claude-opus-5-5`, `--effort xhigh`, Read/Grep/Glob/WebFetch/WebSearch, 저장소 읽기 전용 | `canonicalModel=claude-opus-5-5`, `is_error=false`, `num_turns=69`. effort 필드 없음 | `raw/grill/opus-review*` |
| 그릴링 적대적 리뷰 | Codex `gpt-6.1-sol`, `model_reasoning_effort="xhigh"`, read-only | exit 0, `turn.completed`, 오류 이벤트 없음. 명령 이벤트 33. 모델·effort 필드 없음. stderr에 서브에이전트 생성 실패 1줄 | `raw/grill/sol-review*` |

---

## 9. 철회·정정한 발언

| 당시 발언 | 정정 |
|---|---|
| Agent Flow는 플러그인 형태가 아니다 | packer와 bin이 이미 있다 |
| 독립 CLI(전역 공유 런타임)를 본체로 | 철회. 프로젝트 고정 실행본 |
| 두 모델이 같은 결론 = 근거 | 입력 자료가 결론을 유도했고 조건도 달라 근거에서 제외 |
| 훅이 없으면 보호 없는 실행을 허용하지 않는다 | 새 정책이었다. D4로 정리 |
| 새 에이전트는 기존 어댑터에 연결만 하면 된다 | `_HOSTED`가 고정 집합이라(`adapters/auto.py:25`) 여러 곳을 바꿔야 한다 |
| 호스트 → 어댑터 → 런타임, 어댑터가 이벤트를 변환 | 어댑터는 runner 내부 구성요소다. 이벤트 변환은 훅과 OMP extension(`lib/omp-hooks-extension.mjs:303-342`)이 한다 |
| OMP 탐색 차이는 P0 | 재현 후 수정(A3) |
| OMP subagent는 비참여 | 확인되지 않아 제외 |
| 프로젝트 고정 실행본 | 인터프리터는 전역 절대경로이고 digest로 검증한다 |
| 에이전트별 훅은 사전 차단만 남기자 | 철회. tripwire를 포함한 훅 세트 유지 |
| 보호 브랜치 차단을 git hook으로 옮기자 | 철회 |
| tripwire를 빼면 롤백 방식으로 돌아간다 | 과장이었다. 빼면 단계 경계에서만 발견한다 |
| 설치 런타임 digest 대조 | D3로 제외 |
| 토큰을 제거하고 사람 턴 원장과 묶자 | 토큰은 분리(P1), 사람 턴 원장은 D5로 하지 않음 |
| marker 예시는 키만 남기자 | 자리표시로 바꾼다(P2) |
| 진행 순서 1번은 훅 없는 에이전트 정책 결정 | D4로 결정했고, 순서는 4절 |
| 플러그인 제품화(A1)와 pack 버그 수정(A2)을 계획에 넣음 | 플러그인 전환을 전제로 한 제안이었다. 마켓 배포를 하지 않으므로 D11로 제외 |

---

## 10. 확인 필요 항목

- web_qa_skill, chatbot, kroute 원본 run 로그.
- Claude·Codex에서 실제 native 플러그인 설치(D11로 범위 밖).
- 세 호스트에서 훅이 실제로 실행돼 차단하는지.
- OMP 설치 위치 탐색 차이로 실제 우회가 가능한지.
- Codex의 사람 입력 이벤트, OMP `input` 이벤트 payload, 사람 입력과 자동 입력의 구분 가능성.
- #162가 승인 훅을 걷어낸 직접적인 이유(PR 본문에 명시되지 않음).
- P1이 실제 호스트(특히 Codex)에서 자가 승인을 줄이는지.
- 런타임이 없을 때 lifecycle 외 명령이 kit `src`로 넘어가는 경로의 영향.
- 인터프리터 업그레이드와 active run 중 재설치 금지가 겹칠 때의 복구 경로.
- `/var` 아래 출력 경로에서도 pack이 실패하는지(D11로 범위 밖).

---

## 11. 원자료

`maintainer/2026-10-09-plugin-evolution-raw/`:
- `brief.md`, `priority.md`: 플러그인 전환 제안 입력.
- `opus-proposal.md`, `opus-response.json`: Claude 제안 원문과 반환 JSON.
- `astra-proposal.md`, `astra-events.jsonl`, `astra-execution.json`, `astra-provenance.json`: Codex 제안 원문과 실행 기록.
- `format-research-brief.md`, `format-research.md`, `format-research-response.json`: 워크플로 형식 조사.
- `grill/review-brief.md`, `grill/controller-proposals.md`: 두 번째 리뷰 입력.
- `grill/pane-first-answer.md`, `grill/pane-final-answer.md`, `grill/pane-user-messages.md`: GRILL 패인 답변과 질문.
- `grill/pane-reports/`: GRILL 패인 조사 보고서 6개.
- `grill/opus-review.md`, `grill/opus-review-response.json`: Claude 리뷰 원문과 반환 JSON.
- `grill/sol-review.md`, `grill/sol-review-events.jsonl`, `grill/sol-review-execution.json`, `grill/sol-review-stderr.txt`: Codex 리뷰 원문과 실행 기록.
- `regression-ledger/ledger.md`, `regression-ledger/bug-ledger.json`: 과거 버그 PR·이슈 원장(5.1). `/tmp/af-adv-review/`에서 옮겼다.

원문에는 확인되지 않은 주장이 섞여 있다. 이 문서에서 [확인]이 붙은 내용만 이 세션에서 확인한 사실이다. pack 결과(`plugin-smoke/`)는 `/private/tmp/agent-flow-plugin-proposals-jgp5QY/`에만 있다. 같은 명령(`node bin/agent-flow-plugin.mjs pack --output <정규화된 경로>`)으로 다시 만들 수 있다.
