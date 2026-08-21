[English](../GETTING-STARTED.md)

# 시작하기

이 문서는 빈 터미널에서 변경 하나를 끝내기까지의 경로다. 모든 명령과 플래그는
[USAGE.md](../USAGE.md)에 정리되어 있으므로 여기서 되풀이하지 않고 링크한다. 이 도구가 왜
이런 모양인지는 [README.md](../../README.md)에 있다.

## 무엇에 동의하는 것인가

이 도구를 설치하면 라우팅 결정이 agent에서 runner로 넘어간다. 다음 phase는 runner가 정하고,
agent는 출력된 `next_command`를 따르며, 끝을 빨리 보려고 phase를 건너뛰는 일은 사용자에게도
agent에게도 허용되지 않는다. phase가 실제로 자기 일을 했는지는 agent의 보고가 아니라 hook이
기록한 명령 실행 로그로 판정한다 — 해당 phase에 test 명령이 없는 수정은 agent가 무엇을 적어
두었든 거부된다. 그래서 run은 필요 없다고 생각하는 phase까지 포함해 끝까지 걸어야 하는 phase의
연속이다. 그 마찰은 결함이 아니라 결과물이고, 이 문서의 나머지는 그 마찰이 어디서 비용이 되는지
말한다.

## 준비물

- **Homebrew.** 아래 설치 경로가 tap이므로 Homebrew가 먼저 있어야 한다.
- **git, 그리고 git 저장소.** `agent-flow run`은 저장소 밖에서 시작을 거부한다. 모든 run이
  격리된 worktree에서 작업하고, git이 없으면 격리할 대상 자체가 없다.
- **호스트 CLI 최소 하나 — Claude Code 또는 Codex CLI.** agent-flow는 모델을 직접 호출하지
  않는다. phase prompt를 출력하고 호스트 CLI가 쓴 결과를 읽으므로, 호스트 CLI가 없으면 phase를
  진행시킬 주체가 없다.
- **`node`.** formula가 런타임 의존성으로 함께 설치하므로 직접 할 일은 없다. 프로젝트 설치와
  관리되는 모든 hook이 `bin/agent-flow-kit.mjs`를 실행한다.
- **`gh`, PR phase에만.** `agent-flow pr-watch`는 `gh` CLI를 직접 호출하고 이미 가진 인증을
  물려받는다. agent-flow는 자체 토큰을 관리하지 않는다.

review phase가 의미를 갖기를 원한다면 Claude Code와 Codex CLI를 **둘 다** 설치한다.
`multi_review`로 표시된 phase — 작은 workflow의 `review`, 큰 workflow의 `multi-review`,
`architecture-review`, `final-review` — 는 설치된 Claude와 Codex CLI에서만 review angle을 격리된
subprocess로 실행한다. angle을 provider에 어떻게 나누는지는 phase마다 다르다. `final-review`는
모든 angle을 양쪽 provider에 배분하고, 나머지 `multi_review` phase는 모든 angle을 하나의 주
provider에서 실행하며 추가로 선택된 provider가 있으면 그것을 더한다.

- **둘 다 설치** — `final-review`의 판정이 서로 독립인 두 프로세스와 두 provider에서 나오고,
  나머지 `multi_review` phase는 주 provider 위에 두 번째 provider를 더 얻는다. 이 phase를 두는
  이유가 그것이다. 코드를 쓴 세션은 자기 코드를 통과시키므로 그 세션에게 review를 맡기지 않는다.
- **하나만 설치** — 모든 angle이 여전히 독립 subprocess로 실행되지만 한 provider가 전부를
  담당한다. 프로세스 독립성은 남고 provider 독립성은 사라진다.
- **둘 다 없음** — phase가 실패로 닫힌다. controller 세션이 review 판정을 대신 기록할 수 없으므로
  run은 거기서 멈춘다.

## 설치

```bash
brew tap chonamdoo/agent-flow https://github.com/chonamdoo/agent-flow
brew install chonamdoo/agent-flow/agent-flow
```

저장소 이름이 `homebrew-*`가 아니므로 tap은 URL을 명시한 두 인수 형태를 쓴다. 설치는 완전한
이름으로 한다. Homebrew 6.0부터 서드파티 tap은 명시적 신뢰가 필요하고, 완전한 이름이 이 formula
하나에 대해 그 신뢰를 준다.

그다음 프로젝트에 workflow를 설치한다.

```bash
agent-flow .
```

경로만 준 형태가 설치 명령이다. `agent-flow <dir>`가 해당 디렉터리에 프로젝트 자산을 설치하므로
먼저 `cd`할 필요가 없다. 설치 플래그는 그대로 전달된다.

```bash
agent-flow . --profile android --skills tdd,code-review
```

이 명령에는 제약이 둘 있다. **프로젝트당 한 번** 실행한다. 새 세션이 시작된 것은 다시 실행할
이유가 아니다. 그리고 **leader checkout에서만** 실행한다. 연결된 worktree 안에서 실행하면
차단된다. 설치되는 자산은 run 하나가 아니라 저장소에 속하기 때문이다.

## 첫 run

`bugfix`를 쓴다. phase가 5개 — `reproduce`, `implement-fix`, `review`, `qa`, `handoff` — 이고,
실제 review와 실제 검증 단계를 모두 가진 workflow 중 가장 저렴하다.

이 명령들은 빈 셸이 아니라 Claude Code 또는 Codex 세션 안에서 실행한다. agent-flow는 phase
prompt를 출력하고 기다리며, 그 prompt를 읽고 phase의 일을 하고 `required_artifact`를 쓰는 주체는
그 세션의 호스트 CLI다. 세션 안에서는 같은 단계에 슬래시 명령 형태가 있다 — 시작은
`/agent-flow <task>`, 계속은 `/agent-flow`. 터미널에서 바이너리만 직접 실행하면
`status: awaiting_host`에 멈춘 채 artifact를 쓸 주체가 없다. USAGE.md의
[Running](../USAGE.md#running)을 본다.

```bash
agent-flow run "fix the login timeout on slow networks" --workflow bugfix --worktree fix-login-timeout
```

출력의 첫 줄이 작업 공간이다.

```text
worktree: fix-login-timeout /Users/you/.agent-flow/worktrees/<repo-id>/fix-login-timeout
```

`--worktree`를 생략하면 이름을 task 문장에서 만든다. 기본 위치는
`~/.agent-flow/worktrees/<repo-id>/<name>`이고, 프로젝트 폴더 밖인 것은 의도다. worktree를
프로젝트 안에 두면 leader를 열어 둔 IDE가 worktree 활동에 반응해 leader의 캐시를 건드리고,
leader tripwire가 그것을 오염으로 보고한다 — 그러면 남은 phase가 막힌다. **모든 작업은 그
worktree에서 하고, leader checkout에서는 하지 않는다.** tripwire가 감시하는 대상이 leader이므로,
거기서 편집하는 것은 자기 run을 자기가 멈추는 일이다.

이어서 run이 첫 phase prompt를 출력하고 멈춘다.

```text
═══ phase 'reproduce' awaits host AI. Write artifact → `agent-flow continue --root /path/to/project --worktree fix-login-timeout`. ═══
status: awaiting_host
run: bugfix/<run-id>
current_phase: reproduce
reason: missing_phase_artifact
required_artifact: /.../reproduce.md
next_command: agent-flow continue --root /path/to/project --worktree fix-login-timeout
```

사실 네 개로 읽는다. `current_phase`는 run이 있는 자리다. `reason`은 멈춘 이유다.
`required_artifact`는 이 phase가 내놓아야 할 정확한 파일이다. `next_command`는 run을 움직이는
유일한 명령이다. 제안이 아니라 runner의 결정이고, 기억으로 다시 타이핑하면 안 되는 `--root`와
`--worktree`를 담고 있다.

그래서 반복은 이렇다. 호스트 CLI가 그 phase의 일을 하고 `required_artifact`를 쓴 뒤, 출력된
명령을 실행한다.

```bash
agent-flow continue --root /path/to/project --worktree fix-login-timeout
```

아무것도 진행시키지 않고 현재 위치만 확인할 때는 이렇게 한다.

```bash
agent-flow status --root /path/to/project --worktree fix-login-timeout
```

이 반복을 다섯 번 돌면 `bugfix`가 끝난다. 마지막 출력은 이렇다.

```text
status: complete
reason: workflow_complete
report: /.../RUN_REPORT.md
next_command: none
```

`next_command: none`이 run의 끝이다. `RUN_REPORT.md`는 phase artifact를 모은 문서이고,
`agent-flow report --run-dir <run-dir>`가 나중에 다시 쓴다. 더 진행하지 않을 run을 멈출 때는
이렇게 한다.

```bash
agent-flow abort --root /path/to/project --worktree fix-login-timeout --yes
```

## workflow 고르기

일의 크기로 고른다. 유일한 출처는 `src/agent_flow/workflows/<name>.yaml`이다.

| workflow | phase | 맞는 크기 |
|---|---|---|
| `review` | 3 | 기존 변경을 읽기만 한다. 코드는 쓰지 않는다 |
| `bugfix` | 5 | 재현되는 버그 하나. 먼저 실패한 회귀 테스트를 동반한다 |
| `diagnosing-bugs` | 9 | 원인이 불분명하거나 간헐적이거나 성능이 떨어진 어려운 버그 하나 |
| `development` | 6 | 관심사 하나. 구현 경로를 이미 알고 있다 |
| `default` | 15 | PR과 merge까지 가는 변경 |
| `full-feature` | 24 | 요구사항, PRD, DDD 모델링에서 시작하는 기능 |

분명히 말한다. 작은 변경에 `default`를 쓰면 phase를 기다리는 비용이 변경 자체보다 크다. 15개
phase가 각각 artifact를 요구하는데, 두 줄 수정에는 15개 phase 몫의 결정이 없다. `bugfix`나
`development`를 먼저 잡고, 변경 안에 PR과 review와 merge가 실제로 들어 있을 때만 올린다.

작은 workflow가 주지 않는 것이 하나 있다. `review`, `bugfix`, `diagnosing-bugs`,
`development`에는 `design`이나 `prd` phase가 없어서 SPEC ledger가 없다. 사용자가 준 지시가
압축된 대화를 넘어 살아남는다는 보장은 `default`와 `full-feature`에만 있다.

## phase가 막았을 때

run을 실제로 멈추는 것은 세 가지다. 세 경우 모두 어느 쪽인지 말해 주는 명령은
`agent-flow status`이고, `reason`을 먼저 읽는다.

**artifact가 없다.** `reason: missing_phase_artifact`이고 `required_artifact`가 파일을 지목한다.
phase가 문서를 만들지 않았으니 판정할 대상이 없다. 그 정확한 경로에 파일을 쓰고 `next_command`를
실행한다. 다른 파일명은 인정되지 않는다. runner는 workflow가 지정한 자리만 본다.

**`## Completion Gate` marker가 없다.** `reason: missing_completion_markers`이고 status가 목록을
출력한다.

```text
missing_completion_markers: ["regression-test:", "red-observed:"]
```

artifact는 있지만 phase가 요구한 것에 답하지 않았다. 그 marker 줄들을 artifact **끝**의
`## Completion Gate` 블록에 그대로 추가하고 `next_command`를 실행한다. marker 값은 본문과
교차 검증되므로, 문서 내용과 어긋나는 값은 값이 없는 것과 같이 거부된다 — gate를 통과하려고
그럴듯한 값을 채우는 방법은 통하지 않는다.

**reviewer 판정이 `request-changes`다.** run은 멈추지 않고 뒤로 라우팅된다. `bugfix`에서는
`review` phase가 `request-changes`를 `implement-fix`로 되돌리므로 `current_phase`가 다시
`implement-fix`가 되고, implement와 review 한 쌍이 다시 실행된다. reviewer 한 명의
`request-changes` 하나로 전체 판정이 `request-changes`가 된다. 몇 명이 approve했는지는 상관없다.
무엇이 요구되었는지는 run 디렉터리 — `required_artifact`가 들어 있는 그 디렉터리 — 의
`review.md`를 읽거나, 검색해서 확인한다.

```bash
agent-flow query "request-changes" --run-dir <run-dir>
```

빠져나오는 길은 지적을 고치고 review를 다시 돌리는 것이다. 우회는 없다. 판정을 approve로 편집할
수 없다. 편집 대상이 될 그 artifact가 바로 증거이기 때문이다.

## 도구를 최신으로 유지하기

`run`, `start`, `status`, `continue`는 하루 최대 한 번 새 release를 확인하고, 있으면 stderr에 한
줄을 출력한다. 확인은 1.5초로 제한되고 실패까지 포함해 결과를 캐시하므로, 막힌 네트워크의 비용은
명령마다 한 번이 아니라 하루에 한 번이다.

```bash
agent-flow update
```

이 명령은 캐시를 건너뛰고 즉시 물어보며, 설치된 버전, 최신 release, 그리고 이 kit이 설치된
방식에 맞는 업그레이드 명령을 출력한다. Homebrew 설치라면
`brew upgrade chonamdoo/agent-flow/agent-flow`다. 스스로 업그레이드하지는 않으므로, 열려 있는
run 아래에서 무언가 바뀌는 일은 없다.

```bash
AGENT_FLOW_NO_UPDATE_CHECK=1
```

자동 확인을 끈다. `agent-flow update`는 이 스위치를 무시한다. 직접 묻는 것은 물어봐 주는 것과
다르기 때문이다.

kit을 업그레이드한 뒤에는 각 프로젝트에서 `agent-flow .`을 다시 실행한다. 프로젝트에 복사된
자산은 사본이고, kit 업그레이드는 그 사본을 건드리지 않는다. 이것은 서로 다른 두 경고이고 해법도
둘이다. kit이 낡았다는 것은 업그레이드로 풀고, 프로젝트의 사본이 kit과 더 이상 맞지 않는다는
것은 다시 설치해서 푼다.

## 이것이 아닌 것

**속도 향상이 아니다.** 시간이 줄어든다는 약속은 어디에도 없다. 약속하는 것은 phase가 테스트가
실행되었다고 말하면 실제로 실행되었다는 것이다. phase가 많아지면 기다림이 늘고, 작은 변경에서
그 교환은 손해다 — 그래서 작은 workflow가 있다.

**reviewer는 비용이 든다.** review phase는 reviewer sub-agent를 별도 프로세스로 병렬 실행한다.
reviewer 둘을 돌리면 그 phase의 토큰 비용이 두 배가 되고, 큰 workflow에는 그런 phase가 여럿
있다. 독립성을 비용 없이 얻는 방법은 없다. 코드를 쓴 세션을 공유하는 reviewer는 reviewer가
아니다.

**gate 통과가 올바른 변경을 뜻하지는 않는다.** gate는 선언된 검사가 실행되었고 artifact가
주장하는 결과를 보고했음을 증명한다. 그 변경이 옳은 변경이라는 것은 증명하지 않는다. diff를
읽어라.

**아직 개인 도구다.** 검증 기준은 한 사람의 기준을 한 사람의 프로젝트에 적용한 것이다. 팀이
신뢰하기 위해 필요한 네 가지가 풀리지 않았고 [Open problems](../../README.md#open-problems)에
적혀 있다. 다른 사람에게 배포하기 전에 그것을 읽고, 지금까지 정리된 경로는
[TEAM-ADOPTION.md](TEAM-ADOPTION.md)를 본다.
