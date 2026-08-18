# Xirp(Chirp) 해부와 agent-flow 격차 분석

조사일 2026-08-18. 대상 `Xirp-0.14.0-arm64-external`(빌드 `84824ff`).

## 1. 조사 방법과 검증

- `https://xirp.spotify.com/install.sh` 원문만 먼저 읽음. 실행하지 않음.
- 배포물은 `.zip`으로 받음. `latest-mac.yml`의 sha512와 **일치 확인**.
- `.dmg`는 사용하지 않음. 매니페스트 값(192,345,458 / sha512)과 실제 CDN 객체
  (content-length 192,347,559, `x-goog-component-count: 4`)가 **불일치**했다.
  `.zip`만 바이트·해시가 일치한다. 즉 `install.sh`가 받는 dmg는 공개 매니페스트로
  **검증이 불가능**하며, `install.sh` 자체에도 체크섬 검증 코드가 없다.
- `app.asar`를 직접 파싱해 코드를 읽음(설치·실행 안 함). 조사 후 임시 파일 전량 삭제.
  잔여 확인: `/Applications/Xirp.app` 없음, `~/.chirp` 없음, 마운트된 볼륨 없음.

재현이 필요하면 zip을 다시 받아야 한다. 원본 아티팩트는 남기지 않았다.

## 2. 실제 정체: Chirp의 external 에디션

`package.json` → `name: @chirp/electron`, `productName: Xirp`, `chirpEdition: external`.

`config.js`가 에디션 3개를 하드코딩한다.

| edition | 데이터 브랜드 | 기본 WS 포트 |
|---|---|---|
| internal | chirp | 3848 |
| external | **xirp** | 3849 |
| core | chirp | 3850 |

내부/외부가 같은 코드베이스이고 `modules/editions/external.js`가 로드할 모듈 목록을
결정한다. `outbound-network.js`, `secure-storage.js`, `git-host.js`(기본값 `github.com`),
`plugin-marketplace.js`, `portal-url-resolver.js`는 모두 setter 하나뿐인 seam으로,
내부 에디션에서 런타임 주입된다.

### 3층 구조

- `@chirp/electron` — `dist/main.js` 22KB. 창 + 업데이터 + daemon 감시만.
- `@chirp/daemon` — 204 파일. WebSocket API, PGlite, git/tmux/github 서비스.
- `@chirp/squab` — 63 파일. `node-pty` 위에서 에이전트 실행 + 교체.
- UI는 `Resources/dashboard/`(Vite React SPA 408 파일)가 `ws://localhost:3849`로만 붙는다.

`@anthropic-ai/claude-agent-sdk`를 동봉한다(cli.js, sdk.mjs, ripgrep arm64,
tree-sitter wasm). `oneshot/anthropic.js`가 제목 생성 등 배경 작업에 사용한다.

### 상태 저장

`@electric-sql/pglite` — WASM Postgres를 daemon에 임베드. drizzle 마이그레이션 113개.
테이블 16개: `projects sessions worktrees messages settings snapshots todos epics
epic_chunks plan_reviews decisions digests monitored_prs pr_events pr_reviews cost_records`.
`epic_chunks`에 `vector(1536)` 커스텀 타입(pgvector).

단일 daemon + 단일 writer이므로 **Xirp의 확장 한계는 git이 아니라 daemon+PGlite**다.

## 3. "worktree가 50+ 병렬의 핵심 비결"에 대한 판정

거짓이다. 코드 기준으로 역할은 이렇게 갈린다.

- 병렬 실행 primitive = **tmux** (세션 1개 = tmux 세션 1개)
- 충돌 방지 primitive = **단일 daemon**(직렬화 지점)
- 스케줄링 primitive = **`chirp session new --depends-on`** DAG
- worktree = 위 셋 중 아무것도 아님. `-c <cwd>`로 넘길 디렉터리를 만든다.

worktree는 **working tree만** 격리한다. `refs`/`objects`/`config`/`hooks`/`packed-refs`는
한 repo에 공유된다. 즉 "충돌 없이"는 working-tree 수준에서만 참이다.

worktree가 실제로 load-bearing인 곳은 하나다. vendor CLI가 세션 저장소를 **cwd로
키잉**하기 때문이다(adapter 인터페이스가 `sessionRoot(cwd)`, `locateLatest(cwd)`).
같은 cwd를 두 세션이 쓰면 `locateLatest`가 남의 transcript를 집고, handoff 성공 후
원본을 삭제한다. 즉 worktree의 가치는 git 충돌 방지가 아니라 **vendor state의
네임스페이스 분리**다.

## 4. 검증 항목 6개 결과

### 4.1 `git worktree prune` 상호배제 — 없음 (확인)

daemon 200 파일 전수 검색에서 `Mutex`/`withLock`/`Semaphore` 0건. `inflight` 중복
억제는 `portal/service.js`, `auth0/service.js`, `files/service.js`, `query-cache.js`에만
있고 `worktrees/`에는 없다.

prune 호출 2곳 모두 요청 핸들러에서 무보호로 `cwd: project.path` 전역 실행.
1) 생성 경로에서 기존 worktree 강제 정리 직후, 2) 삭제 경로에서 `worktree remove` 실패 폴백.

추가로 reconcile이 **fail-open**이다.

```js
catch(t){ c.debug(`Failed to list git worktrees for ${n.path}: ${t}`), s=[] }
```

`git worktree list` 실패를 "worktree 없음"으로 해석한다. 그 상태로 DB와 대조하면
살아있는 worktree를 고아로 판정한다.

### 4.2 worktree 경로 충돌 처리 — 없음 (확인)

```js
function A(t,e,r){ const n = r.replace(/[^a-zA-Z0-9-]/g, "-"); ... }
```

비단사 변환이고 유일성 토큰이 없다. `feat/auth`와 `feat-auth`가 같은 경로로 접힌다.
`find-by-branch.js`는 git에 정확한 ref로 묻는 별개 조회라 이 문제와 무관하다.
`worktree add` 실패 복구는 `a branch named .+ already exists` 한 가지뿐이고
경로 중복은 그대로 throw한다.

경로 preset: `sibling {parentDir}/{project}-worktree-{branch}`(기본),
`claudeStyle {parentDir}/{project}/.claude/worktrees/{branch}`,
`custom {customPath}/{project}-{branch}`, bare repo `{projectRoot}/{branch}`.

부가 설정 2개가 격리를 뚫는다.
- `worktreeSparseDirectories` → `--no-checkout` 후 `sparse-checkout init --cone`.
- `worktreeIgnoredFilesMode: "symlink" | "copy"` → gitignored 파일을 통째로 운반.
  **symlink 모드면 모든 worktree가 같은 `.env`/`node_modules`를 공유한다.**

내부 에디션 전용 훅도 있다: `worktree:sptCreate` / `worktree:sptRemove` capability와
`worktreeUseSpt` 설정.

### 4.3 권한 우회 플래그 기본값 — 전부 false (이전 의심 철회)

```js
EMPTY_AGENT_DEFAULTS = {
  claude: { dangerously_skip_permissions: false, ... },
  codex:  { dangerously_bypass_approvals_and_sandbox: false, sandbox_mode: null, ... },
  gemini: { yolo: false, sandbox: null, ... },
  snipe:  { yolo: false, auto_route: false, ... }
}
```

단, 전역 opt-in 토글이 있다: `o?.defaultSkipPermissions && (s.dangerously_skip_permissions = true)`.
켜면 claude 세션 전체의 기본값이 뒤집힌다.

따라서 "PR 코멘트 → 임의 실행" 경로는 **사용자가 스위치를 켠 뒤에만** 성립한다.
위험 등급을 "설계 결함"에서 "위험한 조합을 코드가 막지 않음"으로 내린다.
그래도 조합 자체는 막혀 있지 않다. 근거:
- `channels/injector.js` `EVENT_TEMPLATES`의 `{detail}` = 임의 GitHub 사용자의 코멘트 본문
- `channels/instructions.js` = "read the feedback, address it, push a fix"
- `agent/command.js`가 위 우회 플래그들을 같은 경로에서 조립

비공개 4번째 에이전트 `snipe` 발견: `provider`, `auto_route`, `classifier_model`,
`classification_method`. `visibility:"public"`은 claude/codex/gemini 3개뿐이다.

### 4.4 `squab/dist/state-machine.js` — agent-flow phase와 축이 다름

프로세스 수명 FSM이다. 불법 전이는 throw하고 전이는 `recordTransition`으로 기록한다.

```
Idle → Spawning → Running → Stopping → Translating → Spawning
                                    ↘ Exited
        (어디서든) → Failed
```

`Translating`이 transcript 변환 단계이고 `Stopping → Translating → Spawning`이
에이전트 교체 루프다.

agent-flow의 phase는 작업 단계(design→implement→review→merge)다. 직교한다.
**Xirp에는 agent-flow의 workflow phase 머신에 대응하는 것이 없다.** Xirp의 작업
흐름 지시는 `--append-system-prompt` 산문뿐이다.

### 4.5 tmux 스크롤백과 죽은 pane — 자동 회수됨 (이전 의심 철회)

`remain-on-exit`가 3상태로 관리된다: 생성 시 `on`(즉사 진단용) → launch 검증 통과 후
`failed`(정상 종료는 pane 안 남김) → shell-only 세션은 `off`.

추가로 `pane-died` hook이 죽는 순간 `capture-pane -p -J -S -100`과
`#{pane_dead_status}`를 파일로 떨어뜨리고 `kill-session`한다. 좀비 누적은 없다.

실제 비용은 `history-limit 50000`. 세션당 5만 줄 스크롤백이다.
[미확인] 실제 메모리 사용량은 측정하지 않았다.

### 4.6 tmux 세션 부트스트랩 (참고)

```
tmux new-session -d -s <name>
  -e CHIRP_SESSION_ID / CHIRP_DAEMON_PORT / CHIRP_WS_URL / CHIRP_PARENT_SESSION_ID
  -e DISABLE_UPDATE_PROMPT / DISABLE_AUTO_UPDATE / FORCE_COLOR=3 / COLORTERM=truecolor
  -c <worktree path> -x <cols> -y <rows>
  '<squab launch command>' ; set-option remain-on-exit on
```

`allow-passthrough on`을 켜서 에이전트의 터미널 이스케이프를 통과시킨다.
입력은 `send-keys -t <s> -l <text>` + 별도 `send-keys Enter`. 인터럽트는 `send-keys C-c`.

## 5. Xirp의 벤더 중립 방식

`@chirp/squab` 자기 설명: "Launches a coding agent under a PTY supervisor and can swap
one agent for another mid-session without losing conversation context."

`performHandoff` 흐름:

```
sourceAdapter.findBySessionId(cwd, id)   실패 시 locateLatest(cwd) + 신선도 검사
  → readNative()            vendor 파일 → canonical
  → hasConversationalContent()
  → withHandoffMarker(from, to, reason, ts)
  → targetAdapter.sanitize()
  → targetAdapter.writeNative()
  → targetAdapter.resumeArgs()
  → recordHandoff() + notifyMovedOut/In
```

resume 인자는 벤더별로 다르다.

| 에이전트 | resumeArgs |
|---|---|
| claude | `--resume <sessionRoot>/<id>.jsonl` |
| codex | `-c check_for_update_on_startup=false resume <uuid>` |
| gemini | `--resume <id>` (없으면 `--resume`) |

손실 지점 3개를 그대로 갖고 있다.

1. `sanitize` 실패 시 stderr 한 줄만 쓰고 **오염된 데이터로 진행**한다.
2. handoff 성공 후 **원본 transcript를 삭제**한다(`sourceDeleted`).
3. gemini adapter에 `} else s.type;` — 처리 못 하는 이벤트 타입을 평가만 하고 버린다.
   타입 누락이 카운트되지 않고 사라진다.

## 6. MCP 사용 방식 2가지

**Portal(Backstage) 컨텍스트 주입** — `modules/portal/agent-config.js`

```js
{ mcpServers: { portal: { type:"http", url:`${origin}/api/mcp-actions/v1`,
    headersHelper: `node -e "...readFileSync('~/.chirp/portal-auth.json')...Bearer "` } } }
```

`headersHelper`는 표준 MCP 필드가 아니다. 토큰을 config에 굽지 않고 호출 시점에
셸로 뽑는다. origin은 `http://localhost` 또는 Portal origin 정확 일치만 허용.
동반 system prompt가 강제형이다: "you MUST ALWAYS call `workspace.get-project-context`
... even if you do not think the current task requires this tool."

확인된 Backstage 연동: `https://<sub>.spotifyportal.com/.backstage/health/v1/liveness`,
`backstage.io/source-location` annotation으로 repo 위치 역추적,
텔레메트리 `https://backstage-api.spotify.com/portal/v1/xirp/telemetry`.

**실행 중 세션에 이벤트 푸시** — `channels/channel-server.js`

stdio MCP 서버(`chirp-channel`)가 daemon WS에 붙어 `channel:push`를 받아
`notifications/claude/channel`로 에이전트에 주입한다. capability는
`experimental["claude/channel"]`. 포트는 `~/.chirp/daemon-<edition>.port`로 발견,
끊기면 지수 백오프(최대 30s) 재연결.

## 7. agent-flow와의 실제 격차

`src/agent_flow/core/worktree_isolation.py`(2,980행), `core/worktrees.py`(4,860행) 기준.
**격리 정확성은 agent-flow가 압도적으로 앞선다.**

| 축 | agent-flow | Xirp |
|---|---|---|
| worktree 생성 직렬화 | `worktree_creation_lock` flock, 재시도 8회 | 없음 |
| 경로 유일성 | `plan_worktree(unique=...)` worker 토큰 | 비단사 slug |
| git 조회 실패 | `list_registered_worktrees` fail-closed raise | fail-open `s=[]` |
| leader 오염 탐지 | tripwire: HEAD+branch+status+tracked-content digest | 없음 |
| git env 유출 차단 | `LEAKY_GIT_ENV_VARS` 제거, `assert_cwd_bound` | 없음 |
| lock 경합 재시도 | `is_git_lock_contention` + `with_git_lock_retry` | 없음 |
| 로케일 고정 | `_GIT_STABLE_LOCALE = LC_ALL=C` | 없음 |
| 삭제 안전성 | 병합 증명 + ref CAS + 저널(v3) + 크래시 재개 | `remove --force` / `rm -rf` + ENOTEMPTY 재시도 |
| hook 격리 | checkout별 등록 파일 `renameat` 원자 배치 | `.git/hooks` 공유 |
| worktree 부가 파일 | 이름 allowlist(`link_node_modules`만) | `symlink`/`copy` 통째로 |
| 동시성 상한 | `provider_lease` slot 레지스트리, `DEFAULT_MAX_WORKERS=8` | 명시적 상한 없음 |
| 쓰기 범위 충돌 | `assert_scopes_isolated`, glob은 충돌로 간주 | 없음 |
| symlink/TOCTOU | `O_NOFOLLOW`, `dir_fd`, sticky bit 확인 | 대응물 없음 |
| **작업 phase 머신** | **YAML + CLI 상태기계(결정적)** | **없음(프롬프트 산문)** |
| 세션 영속성 | 없음(`asyncio` subprocess) | tmux + `pane-died` hook |
| 에이전트 교체 | 없음 | canonical + adapter 3종 |
| 세션 DAG 팬아웃 | 리뷰어 팬아웃만(`multi_review.py`) | `--depends-on` 부모/자식 큐 |
| 라이브 이벤트 주입 | 없음(phase를 사람이 진행) | MCP notification 푸시 |
| 상태 저장 | 파일 + run artifact | PGlite + pgvector |

agent-flow는 Xirp의 비단사 slug 버그를 이미 겪고 고쳤다. `_status_for_registered`
주석이 `feat-issue#110` → `feat-issue-110` 뭉개짐을 명시한다. Xirp의 `A()`는 지금도
그 버그를 갖고 있다.

`WORKTREE_SETUP_ACTIONS` 주석이 철학 차이를 요약한다: "profile은 이 이름들만 고른다.
명령 문자열이 들어올 자리가 없다. npm 설치는 뺐다 — 그 함수가 저장소의 package.json에서
preinstall/postinstall을 끌어다 실행하므로."

### 조사 중 철회한 주장

- "권한 우회 플래그가 기본 켜짐일 것" → 전부 false. 철회.
- "죽은 tmux pane이 누적될 것" → `pane-died` hook이 회수. 철회.
- "git 변경 단일 직렬화를 도입하라" / "경로를 단사로" / "hook 격리" 제안 →
  agent-flow에 **이미 구현돼 있었다**. 소스를 읽지 않고 제안한 것이다. 철회.

## 8. 도출된 액션

- `docs/issues/0007-persist-failed-worker-output.md` — 확인 완료. 이미 저장되고 있었다.
- `docs/issues/0008-slice-level-parallel-implement.md`
- `docs/adr/0008-reject-live-external-event-injection.md`

## 9. 미확인 항목

- Xirp `history-limit 50000`의 실제 메모리 사용량.
- Xirp 내부 에디션의 `worktree:sptCreate`/`sptRemove` 구현체(external 빌드에 없음).
