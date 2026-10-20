# 릴리스 기준 개선 사이클 런북

릴리스 태그 직후 그 버전을 기준선으로 한 번 돈다(주기 실행은 하지 않는다). 결과는 `v<버전>.md`에 남기고
[index.md](index.md)의 추이 표를 갱신하며, 측정 원자료는 경로·비밀을 지운 `v<버전>-raw.tar.gz`로 옆에 둔다.
이 문서만으로 새 컨테이너에서 같은 측정을 재현할 수 있어야 한다.

## 트리거

- 자동화 여부(2026-10-20 확인): 저장소 workflow에는 Anthropic·OpenAI 자격 증명이 없고 `release.yml`은 `v*` 태그
  push에서 brew formula만 갱신한다. Claude Code 클라우드 세션의 예약 작업은 시간 기반·수동 실행뿐이라 GitHub 릴리스
  이벤트로 세션을 깨우려면 claude.ai 자격 증명을 저장소에 둬야 한다. 그렇게 하지 않는다.
- 현재 방식: 릴리스 직후 유지보수자가 요청한다("v0.3.21 릴리스했어, 개선 사이클 돌려줘").
- 긴 측정: 백그라운드 명령은 2시간 제한이 있다. 세션 스스로 깨어나도록 예약 메시지(`send_later`)를 걸고, 측정은
  `setsid`로 띄워 세션 명령과 분리한다. 회차마다 커밋한다.

## 환경 준비

```bash
# 저장소와 Python 환경(평가기 실행용)
git clone https://github.com/chonamdoo/agent-flow && cd agent-flow
python3 -m venv $S/venv && $S/venv/bin/pip install -e ".[dev]"     # S=<scratch 디렉터리>
export AGENT_FLOW_SKIP_CODEX_TRUST=1 AGENT_FLOW_NO_UPDATE_CHECK=1 PYTHONPATH=src

# Claude CLI: 세션에 이미 있고 인증돼 있다(claude --version).
# Codex CLI(구독 인증):
npm install -g @openai/codex@0.163.0
codex login --device-auth          # 출력된 URL·일회용 코드를 사람이 브라우저에서 ChatGPT 계정으로 입력
codex login status                 # "Logged in using ChatGPT"
codex exec --skip-git-repo-check "Reply with exactly: pong"   # 응답 확인
```

- 클라우드 환경의 Network access에 `auth.openai.com`(로그인), `chatgpt.com`(구독 호출), `api.openai.com`을 허용해야
  한다(환경 설정 → Edit → Allowed domains, "Allow package managers" 유지). 바꾼 정책은 새 세션에만 적용될 수 있다.
- 기본 `codex login`은 브라우저의 localhost 리디렉션을 기다려서 원격 컨테이너에서는 끝나지 않는다. `--device-auth`를 쓴다.
- 컨테이너가 회수되면 설치·로그인이 사라진다. 환경 설정 스크립트에 설치 명령을 넣어 두면 편하다.
- 구독 Codex는 달러 비용과 턴 수를 보고하지 않는다(표에 n/a). 사용 한도(HTTP 429)에 걸리면 기다렸다가 같은 명령으로
  다시 돈다. 릴리스 게이트는 실패 unit만 골라 다시 돌리지 않는다.

## kit 준비

kit은 고정 sha의 git worktree다. 측정 중에는 kit을 바꾸지 않는다.

```bash
git fetch origin --tags
git worktree add --detach $S/kit-base v<버전>          # 기준선(또는 기준 main sha)
git worktree add --detach $S/kit-after <후보 sha>       # 후보
# 아직 커밋하지 않은 후보 문구를 잴 때는 kit-after 작업 트리의 파일만 고친다(평가기는 kit 파일을 그대로 설치한다).
```

평가기(`evals/phase_eval.py`와 사례)는 실행하는 checkout의 것을 쓴다. 릴리스 행렬 밖 사례(fixloop 등)를 쓰려면 그
사례가 있는 checkout에서 실행한다.

## 순서

1. **결정적 검사**(모델 없음): `phase_budget`·`skill_routing`을 base와 후보에서 재고 비교한다.

   ```bash
   python evals/phase_budget.py --kit $S/kit-base --output $S/budget-base.json
   python evals/phase_budget.py --kit $S/kit-after --output $S/budget-after.json
   python evals/phase_budget_compare.py $S/budget-base.json $S/budget-after.json
   python evals/skill_routing.py --kit $S/kit-base --output $S/routing-base.json
   python evals/skill_routing.py --kit $S/kit-after --output $S/routing-after.json
   python evals/skill_routing.py --compare $S/routing-base.json $S/routing-after.json
   ```

2. **기준선 모델 측정**: 릴리스 행렬 밖 어려운 사례를 provider별로 잰다. 모든 시행에서 세 축(behavior·plan·norm)을
   통과하지 못한 사례가 "baseline 실패 사례"다.

   ```bash
   python evals/phase_eval.py --kit before=$S/kit-base --stacks fixloop,fixloop-ts,commerce,backend \
     --modes clean --scenario author --trials 5 --author-provider <claude|codex> --review-providers <claude|codex> \
     --concurrency 4 --output $S/baseline-<provider>
   python tools/autoresearch/cycle_report.py $S/baseline-<provider>
   ```

3. **모델 루프**(baseline 실패 사례만, 회차당 $20 상한, Claude 제안자): 편집 범위는 그 phase가 required로 읽는 skill 본문.

   ```bash
   python tools/autoresearch/loop.py --track model --tag v<버전> --skills <skill> --stacks <실패 사례> \
     --modes clean --trials 3 --budget-usd 20 --iterations 10 --patience 5 --root $S/autoresearch
   ```

   model 트랙은 통과 수가 늘 때만 유지한다. 삭제로 짧게 만드는 편집은 유지하지 않는다. 유지된 첫 개선만 후보다.

4. **후보 검증**(before·after 각각, provider마다):

   | 검사 | 사례 | 시행 | 통과 기준 |
   |---|---|---:|---|
   | 효과 | 루프 대상 사례 | 8+ | after ≥ 90%, before 대비 one-sided Fisher p ≤ 0.05(또는 before도 포화) |
   | 일반화 | held-out(다른 stack·다른 근거 문서, 루프가 보지 않음) | 5 | 떨어지지 않음 |
   | 회귀 | commerce, backend | 5 | 떨어지지 않음 |
   | 위험: 틀린 계획 | fixloop-wrongplan | 5 | 떨어지지 않음(Spec 적용 또는 충돌 보고) |
   | 위험: 계획 없는 workflow | devflow | 5–8 | 통과 유지, 턴·도구 호출 +10% 이내, plan lookups가 늘지 않음 |
   | 리뷰(단독) | review backend·web clean | 2–3 | 결함 검출이 줄지 않음, clean 오탐이 늘지 않음 |
   | 리뷰(병렬) | `--review-providers claude,codex` backend·web clean | 2–3 | seeded 결함 전부 검출, clean 오탐 0, 판정 무효율 < 5% |
   | 공통 | 위 전부 | — | uncached input 토큰 +10% 이내, overclaims(eligible) 증가 없음 |

   ```bash
   python evals/phase_eval.py --kit before=$S/kit-base --kit after=$S/kit-after --stacks <사례> --modes clean \
     --scenario author --trials <n> --author-provider <p> --review-providers <p> --concurrency 4 --output $S/<run>
   python evals/phase_eval.py --kit before=$S/kit-base --kit after=$S/kit-after --stacks backend,web --modes clean \
     --scenario review --trials 2 --author-provider claude --review-providers claude,codex --output $S/review-parallel
   python tools/autoresearch/cycle_report.py $S/<run> ... $S/review-parallel
   ```

   결과가 애매하면 시행을 늘린다. 위험 검사에서 회귀가 하나라도 나오면 skill 변경은 빼고 사례만 PR로 올린다. 회귀
   원인이 문구 범위라면 조건을 좁힌 변형을 한 번 더 잴 수 있다(2026-10-20 devflow 사례).

5. **PR**:
   - skill·리뷰 템플릿 변경: 다음 버전 대상 PR. 바이트가 늘면 `phase-budget-accepted` 라벨, 본문 `회귀 대조` 절.
   - 평가기(`evals/`)·릴리스 정책·workflow 계약 변경: 자동 merge하지 않고 보고만 한다(평가기를 바꾸면 다음 릴리스
     게이트가 재사용 없이 전체를 잰다).
   - 새 실패 사례: draft PR(채택은 유지보수자 검토).

6. **기록**:

   ```bash
   python tools/autoresearch/archive_runs.py --output maintainer/improvement-history/v<버전>-raw.tar.gz \
     <이름>=$S/<run> ... --file loop/results.tsv=$S/autoresearch/v<버전>/results.tsv
   ```

   `v<버전>.md`: 기준 sha, 사례별 점수·시행 수(provider별), 시도한 개선과 유지·폐기 근거, 위험 검사, 토큰·턴·도구 호출·
   overclaims, 비용, PR 링크. [index.md](index.md)에 한 줄 추가. 보관 원자료는 `tar -xzf` 후 `cycle_report.py`에
   그대로 넣으면 같은 표가 나온다(`evidence-stats.json`에 도구 호출·plan lookups가 남는다).

## 실전 UI 과제

phase_eval의 작은 fixture로는 안 드러나는 실패(긴 PRD, 여러 화면, 타이머·상태 저장)를 보려고, 공개 사이트에서
관찰한 구조·흐름·상호작용으로 만든 과제를 Agent Flow `full-feature` workflow로 처음부터 만들게 하고 브라우저 검사로
채점한다. 과제는 `evals/ui-tasks/<task>/`에 있다(`beauty-store`: 2026-10-20 관찰, 과제 v1, 5개 화면, Spec Item 9개,
검사 20개). 형식과 도구는 `evals/README.md`의 "실전 UI 과제"를 본다.

원본 사이트: 다시 관찰하는 것은 원본 구조가 크게 바뀌어 PRD의 화면·흐름이 더는 맞지 않을 때만이다. REA 캡처·스크린샷·
스크립트는 scratchpad 같은 로컬에만 두고 커밋하지 않는다. 다시 관찰하면 PRD의 과제 버전과 관찰 날짜를 올리고,
`acceptance.json`·`reference/`를 같이 고쳐 기준 클론이 다시 100%인지 확인한다. 평소 회차는 원본을 보지 않는다.

```bash
T=evals/ui-tasks/beauty-store
python evals/ui_task.py validate $T
python evals/ui_task.py run $T --static $T/reference --repeat 3        # 기준 클론: 모든 검사 통과여야 한다

# 새 중립 프로젝트에서 full-feature 실행(kit = 측정할 agent-flow 트리)
P=$(mktemp -d)/store && mkdir -p $P && cd $P && git init -q && git commit -q --allow-empty -m init
mkdir -p docs fixtures && cp <repo>/$T/prd.md docs/source-prd.md && cp <repo>/$T/fixtures/catalog.json fixtures/
<kit>/bin/agent-flow install . <installer 옵션>
<kit>/bin/agent-flow run "docs/source-prd.md의 Store를 구현한다(Spec Item·test-id 계약을 그대로 따른다)" --workflow full-feature
# pause(domain-grill·prd·merge-approval)마다 agent-flow continue. push-pr 이후는 원격이 없으면 commit에서 멈춘다.

# 채점: 정적 산출물은 --static, 서버가 필요한 앱은 띄운 뒤 --url
python <repo>/evals/ui_task.py run <repo>/$T --url http://127.0.0.1:3000 --repeat 3 --summary ui.md --json ui.json
```

작은 회차: 전체 PRD를 한 번에 돌리면 수 시간이 걸리고 실패 원인이 섞인다. 회차마다 slice 하나(예: SPEC-6–8 장바구니,
SPEC-4 목록)만 과제로 주고 `--spec`으로 그 Spec Item만 채점한다. kit 후보를 비교할 때는 같은 slice·같은 provider로
before·after를 각각 2회 이상 돌린다. 실행 중 Agent Flow가 실패하거나 헤맨 지점(잘못된 phase 전이, 같은 수정 반복,
검사와 다른 test-id, 타이머·저장을 빠뜨린 구현)은 phase 로그를 직접 확인해 기록하고, 재현되는 실패는 phase_eval 사례로
줄여 draft PR로 올린다.

채택 규칙: 자율 운영 규칙과 같다. 같은 slice에서 after의 통과 검사 수가 before보다 많고, 다른 Spec Item 회귀가 없고,
phase_eval 릴리스 행렬(held-out·위험 사례 포함, Claude·Codex 모두)도 before 이상일 때만 kit 변경을 유지한다. 검사를
지우거나 PRD를 줄여 통과시키지 않는다. 과제 자체(PRD·검사·fixture)를 고치는 변경은 평가기 변경과 같이 draft로 올린다.

플랫폼: managed reviewer subprocess의 write-sandbox는 macOS `sandbox-exec`만 있다(`docs/USAGE.md`). Linux 컨테이너에서는
multi-review가 fail closed로 멈추므로(13/24 phase), 리뷰·gates·commit까지 보려면 macOS host에서 돌린다. Linux에서 돌리면
그 직전까지의 phase와 빌드 점수만 얻는다. 권한 우회 headless(`claude -p --dangerously-skip-permissions`)는 쓰지 않고, 별도
원격 세션(`create_session`)이나 사람이 연 세션에서 controller로 진행한다.

비용·시간(2026-10-20):
- `ui_task.py run`: 검사 20개 기준 1회 약 10초(`--repeat 3` 약 30초), 모델 비용 없음.
- 기준 클론 작성: Claude 서브에이전트 1회(약 6분, 약 11만 토큰).
- full-feature 1회(Linux 원격 세션, Claude Opus controller, 전체 PRD): 프로젝트 준비 약 4분, 실행 18분 17초(domain-grill →
  multi-review에서 정지, 13/24 phase), 채점 약 9분. 세션 비용 약 $9.7(출력 약 10.8만 토큰, 캐시 읽기 약 2,700만 토큰,
  준비·채점 포함). reviewer subprocess는 sandbox 때문에 돌지 않아 그 비용은 0이다. 기록:
  `maintainer/improvement-history/ui-task-runs/2026-10-20-beauty-store/`(브랜치 `eval/beauty-store-fullfeature-run`).

## 비용·시간 참고치 (2026-10-20)

| 단위 | Claude 1회 | 시간(동시 4–6) |
|---|---:|---|
| fixloop·fixloop-wrongplan·devflow author | 약 $0.22 | 16회 약 5분 |
| fixloop-ts author | 약 $0.25 | |
| backend author | 약 $0.35 | |
| commerce author | 약 $0.53 | 10회 약 10분 |
| review backend·web (리뷰어 6관점) | 약 $0.57 | 16단위 약 15분 |
| 루프 1회차(제안 + 3회 채점) | 약 $0.80 | 약 3분 |

Codex(구독)는 달러 대신 사용 한도를 쓴다. 토큰은 Claude와 비슷하다(fixloop author uncached 약 36–37k).
위 표 구성으로 Claude 한 사이클은 약 $35–45, 리뷰 검사가 가장 비싸다.

## 함정 (실제로 겪은 것)

- **사례 하나로 매긴 점수와 "짧아짐" tie-break**: 루프가 측정되지 않는 지침 네 문단을 지웠다. model 트랙에서는 이
  tie-break를 없앴다(#383). 채택 전 held-out·회귀 검사를 생략하지 않는다.
- **기준선 변동**: fixloop Claude before는 3회 중 0회, 8회 중 2회 통과였다. 대상 사례는 8회 이상 재고 Fisher p를 본다.
- **provider마다 포화가 다르다**: 같은 fixloop가 Codex는 before부터 8/8이다(매 run 계획을 읽는다). provider별로 판정한다.
- **문구가 넓은 규칙**: "slice plan을 먼저 읽는다"를 조건 없이 쓰자 계획 없는 development run 5회 중 2회가 없는 계획을
  찾았다. "phase prompt가 slice plan을 가리킬 때"로 좁힌 뒤 0회.
- **phase-only author에는 controller 맥락이 없다**: 직전 phase 산출물 경로를 `author_inputs`로 넘긴다.
- **claim_audit가 green 산출물만 찾았다**: fix-loop·implement run의 claims가 비어 overclaim을 볼 수 없었다(#387에서 수정).
  `overclaims (eligible)`는 overclaim이 생길 수 있었던 행 수다.
- **plan 탐색 집계**: 프로젝트 디렉터리 이름에 사례 이름이 들어가므로(`fixloop-wrongplan`) 경로를 지우고 센다. Claude는
  stream-json의 `tool_use`, Codex는 `exec --json`의 `command_execution`을 읽는다. 같은 디렉터리의 실행끼리만 짝짓는다
  (trial 번호가 실행마다 반복된다).
- **리뷰 판정 무효 약 20%의 원인**: 리뷰 관점 템플릿 17개가 출력 예시 머리에 `verdict: approve | request-changes`를
  두고, 끝에서는 "판정 줄은 마지막에 정확히 한 번"을 요구했다. 모델이 두 곳에 같은 판정을 써서 파서(정확히 한 줄)가
  무효로 처리했다. 머리 줄을 지웠다(템플릿 수정 PR). 파서 완화는 승인 증거 계약 변경이라 하지 않았다.
- **Node 테스트 출력**: oracle은 TAP(`# pass N`)과 spec(`ℹ pass N`)을 모두 센다(#387). 파이프면 TAP, TTY면 spec이다.
- **QA skill 판정기**: `Status: **green**` 강조를 놓쳤고, discovery 요청에 "계획만" 접미사를 붙이자 어떤 skill도 로드되지
  않았다(#384에서 수정).
- **프로세스 정리**: `pkill -f <패턴>`이 자기 셸까지 죽였다. 측정은 `setsid`로 띄우고 PID나 작업 중지로 멈춘다.
- **쌓은 PR이 squash merge된 뒤**: 남은 PR에 main을 merge해서 충돌을 푼다(rebase하지 않는다).
- **평가기 지문**: `evals/`의 평가기 파일을 바꾸면 다음 릴리스 게이트는 재사용 없이 전체를 잰다. 평가기 PR은 게이트
  측정 뒤에 merge한다.
- **로컬 전용 실패**: `tests/test_docs_count_parity.py::test_parity_check_passes_on_the_installed_kit`은 연결된 worktree
  컨테이너에서 설치 소유권 확인으로 실패한다(main에서도 같음). CI에서는 통과한다.
