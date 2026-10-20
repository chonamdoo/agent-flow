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
