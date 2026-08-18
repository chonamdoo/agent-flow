# 설치와 사용법

이 도구가 무엇이고 왜 이런 구조인지는 [README.md](../README.md)에 있습니다. 여기에는
실제로 돌리는 방법만 적습니다.

## 설치

프로젝트당 한 번만 합니다. 새 세션이 시작됐다는 이유로 다시 실행하지 않습니다.

```bash
pip install -e <path-to-this-kit>
```

```bash
npx <path-to-this-kit> install
```

순서가 중요합니다. bootstrap이 생성하는 문서가 `agent-flow` 실행 파일을 참조하는데,
그 실행 파일은 첫 단계가 만들어 줍니다.

다른 프로젝트에 설치할 때는 `--root`를 씁니다. 그 디렉터리로 `cd` 할 필요가 없습니다.

```bash
npx <path-to-this-kit> install --root <project-path>
```

설치는 언제나 leader checkout에서 합니다. linked worktree 안에서 실행하면 차단됩니다.

## 실행

Claude나 Codex 세션 안에서 씁니다.

```text
/agent-flow 유저 프로필 페이지 추가          # 시작
/agent-flow                                  # 선택한 worktree에서 이어가기
/agent-flow status                           # 진행 상황
/agent-flow abort                            # 취소
```

같은 일을 CLI로 직접 하려면 이렇게 씁니다.

```bash
agent-flow run "유저 프로필 페이지 추가"
```

```bash
agent-flow status --worktree "feat-user-profile"
```

```bash
agent-flow continue --worktree "feat-user-profile"
```

워크플로를 지정하려면 `--workflow`를 붙입니다. 생략하면 `default`입니다.

```bash
agent-flow run "<task>" --workflow bugfix
```

### worktree

```bash
agent-flow worktree create --name feat-user-profile
```

```bash
agent-flow worktree list
```

기본 자리는 `~/.agent-flow/worktrees/<repo-id>/<name>`입니다. 프로젝트 폴더 안에 두면
leader를 열어 둔 IDE가 worktree 작업에 반응해 leader의 캐시를 건드리고, leader
tripwire가 그것을 오염으로 보고해 남은 단계가 막힙니다.

손으로 `git worktree add`를 하지 않습니다. 생성 잠금, base 선택, 채택 기록을 건너뛰게
됩니다. 그 밖의 linked worktree는 채택해야 인식됩니다.

```bash
agent-flow worktree adopt --path <checkout>
```

### SPEC 원장

최초 목록은 자동으로 baseline이 됩니다. 이후의 추가·변경·삭제만 델타로 보여 줍니다.

```bash
agent-flow spec changes --run-dir <run-dir>
```

```bash
agent-flow spec confirm --run-dir <run-dir>
```

수동 검증 항목도 같은 흐름을 따릅니다. 사용자가 대화에서 확인해 주면 에이전트가
대신 실행합니다.

```bash
agent-flow spec approve <spec-id> --run-dir <run-dir>
```

### 게이트

`gates` 단계는 `--phase all`로 실행해야 합니다. CLI 기본값은 `pre-commit`이고 빌드와
테스트는 `pre-push`로 선언되어 있어, 기본 실행으로는 목록에 오르지 않습니다. 런너는
결과 파일의 `produced_by.gate_phase`를 확인하고 `pre-commit` 실행을 QA 증거로 인정하지
않습니다.

```bash
agent-flow gates --phase all
```

```bash
agent-flow gates
```

두 번째 형태는 기본값인 `pre-commit`만 돌리는 국소 점검입니다.

### 스킬

`skills sync`는 프로파일이 선언한 외부 `skill_sources`만 가져옵니다. 프로파일과
워크플로 자체는 설치 프로그램을 다시 돌려야 갱신됩니다.

```bash
agent-flow skills sync
```

### PR 감시

```bash
agent-flow pr-watch <number>
```

조치가 필요한 상태가 될 때까지 폴링합니다. `--once`를 붙이면 한 번만 조회합니다.

`gh` CLI를 그대로 호출하며 사용자가 이미 가진 인증을 물려받습니다. agent-flow 쪽에서
토큰을 따로 관리하지 않습니다. `gh`가 없거나 인증되지 않았으면 그 사실을 그대로
알립니다.

## 저장소 구조

```text
agent-workflow/
├── bin/
│   ├── agent-flow-kit.mjs        # 주 진입점. install과 설치 자산 동기화
│   └── agent-flow-install.mjs    # 설치 전용 진입점
├── lib/                          # installer가 공유하는 JS 모듈
├── src/agent_flow/               # Python 오케스트레이터
│   ├── cli.py                    # run / continue / status / abort
│   ├── runner.py                 # phase 루프. 라우팅 권한이 여기 있다
│   ├── artifact.py               # 단계 산출물 기록
│   ├── multi_review.py           # 리뷰 관점을 CLI들에 분배
│   ├── subprocess_pool.py        # 타임아웃과 드레인을 갖춘 병렬 서브프로세스
│   ├── core/                     # 경계·격리·원장·게이트 판정
│   ├── adapters/                 # base / auto / hosted / generic
│   ├── workflows/                # 워크플로 YAML 정본 (한 벌뿐)
│   └── profiles/                 # 스택별 프로파일
├── skills/                       # 설치 시 .agent-flow/skills/로 복사
├── templates/_shared/review/     # 리뷰 관점 프롬프트
├── bootstrap/                    # AGENTS.md / CLAUDE.md 템플릿
├── scripts/hooks/                # PreToolUse / PostToolUse / Stop 훅
└── tests/
```

`bin/agent-flow-kit.mjs`는 `start`/`status`/`next`/`advance`를 Python CLI로 넘기고 run
lifecycle을 스스로 전진시키지 않습니다. 자기 상태를 쓰는 곳은 `push-watch` 하나뿐입니다.

## 프로파일

`src/agent_flow/profiles/<stack>.yaml`이 `branching`, `gates`, `review_angles`,
`artifacts`, `vocabulary`, `commit_convention`, `pr`을 선언합니다. 필드 스키마는
`src/agent_flow/profiles/_schema.yaml`에 있습니다.

런너는 활성 프로파일을 파싱해 모든 단계 프롬프트에 주입합니다. 호스트 AI는 "어딘가에서
찾아보라"가 아니라 실제 값을 받습니다. 활성 프로파일은 `.agent-flow/kit.json`의
`profile` 또는 환경변수 `AGENT_FLOW_PROFILE`로 정해집니다.

Android 프로파일 예시입니다.

```yaml
branching:
  strategy: trunk
  worktree: required        # 브랜치만으로는 불가
  naming: { prefix: "feat/", slug_style: kebab-case }

gates:
  - architecture-lint  (pre-commit, 필수)
  - test               (pre-commit, 필수)
  - lint               (pre-commit, 선택)
  - build              (pre-push,   필수)

review_angles:
  - architecture-design
  - android-skills
  - compose-stability
  - test-edge
  - sdui
  - udf
```

build와 test와 lint 명령은 활성 프로파일의 `gates`에서만 가져옵니다. 게이트에 없는 검증
명령을 임의로 반복하지 않습니다.

## 리뷰어 분배

`multi_review: true`로 표시된 단계는 설치된 **Claude와 Codex** CLI에서만 리뷰 관점을
돌립니다. OMP는 host나 controller로 쓸 수 있지만 리뷰어 제공자로는 쓰지 않습니다.

`final-review`는 모든 관점을 두 제공자 모두에 분배합니다. 그 밖의 `multi_review` 단계는
모든 관점을 주 제공자 하나에 돌리고, 선택된 추가 제공자가 있으면 함께 돌립니다.

- **둘 다 설치됨** — `final-review`가 모든 관점을 양쪽에서 돌리고, 프로브가 실패한
  제공자는 남은 관점에서 제외됩니다
- **하나만 설치됨** — 모든 관점이 그 제공자에서 돌되 여전히 독립 서브프로세스입니다
- **둘 다 없음** — 단계가 실패로 닫힙니다. 컨트롤러 세션이 리뷰 판정을 대신 기록하지
  못합니다

`AGENT_FLOW_REVIEWERS="codex"`로 좁힐 수 있습니다. Claude와 Codex 밖의 이름은 무시됩니다.
관점별 산출물(`final-review-<angle>-<provider>.md`)은 일부 타임아웃에도 남습니다. 느린
CLI 하나가 나머지를 막지 않습니다.

## 검증

```bash
npm run parity:check
```

설치 자산과 소스가 어긋났는지, 그리고 [README.md](../README.md)가 선언한 워크플로 단계
수와 프로파일 이름과 스킬 수가 정본 파일과 일치하는지 검사합니다.

```bash
npm test
```

Python 테스트와 위 검사를 함께 돌립니다.

## 알려진 성격

이 kit 자신은 자기가 처방하는 아키텍처를 그대로 따르지 않습니다. `ddd-architecture`
스킬이 사용자 코드에 DDD와 Clean Architecture를 요구하지만, kit의 Python 소스는 추상
하나(`Adapter`)를 둔 절차적 코드입니다. 작은 CLI 도구에는 그게 맞는 선택이지만, 처방과
실물이 다르다는 사실은 적어 둡니다.
