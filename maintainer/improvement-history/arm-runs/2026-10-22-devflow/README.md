# #410 arm 비교 1차: devflow, n=5 (2026-10-22)

## 설정

- 과제: `evals/phase-cases/devflow` (Python, 장바구니 견적에 대량 할인 추가). hidden fix·plan·visible test로 채점한다.
- 조건(`evals/arm_compare.py`)
  - A: 과제 문장 + test 명령 + 완료 선언 규칙. kit 없음.
  - B: `development` workflow. 에이전트가 runner를 돌리고, 사용자 역할도 맡는다.
  - C: `full-feature` workflow. B와 같은 방식이다.
  - C′: `c_prime.py` RULES_VERSION 4. runner 없이 같은 절차 문장만 준다.
- 모델: 모든 조건이 Claude Opus 5.5(`claude-opus-5-5`) 서브에이전트다. 같은 컨테이너에서 돌렸다(Linux, Landlock ABI 7).
  - B·C의 리뷰어는 Claude와 Codex 0.163.0 subprocess다.
- kit: `experiment/epic-409` f1d4e5f(#443 merge 직후). prompt fingerprint는 `results.jsonl`의 `prompt_sha256`에 있다(조건 안에서 5회 모두 같다).
- 실행: 4개 조건을 1회씩 묶은 wave 5개. 파일럿(wave 1)도 trial 1로 센다. 파일럿 뒤에 바꾼 것은 채점 쪽뿐이고 에이전트가 보는 입력은 바꾸지 않았다.
- 오염 방지
  - 에이전트가 설치에 쓰는 kit 사본에는 `evals/`가 없다.
  - hidden test는 run이 끝난 뒤에 복사한다.
  - transcript의 도구 호출에 hidden test 경로가 나오면 `contaminated`로 표시한다. 이번 20회는 모두 0이다.

## 결과

`summary-table.md`가 원표다. 비용은 controller transcript의 토큰에 Opus 5.5 단가를 곱한 추정이다.
- 단가: input $4, cache read $0.20, output $20 / 1M tokens.
- uncached input은 cache write 단가 $5로 계산한 상한이다.

| 조건 | 성공 | 평균 시간 | 출력 토큰 | 입력 토큰(캐시 포함) | 추정 비용/run | 리뷰어 job |
|---|---|---|---|---|---|---|
| A | 5/5 | 37초 | 3.3k | 0.28M | $0.25 | - |
| B | 5/5 | 527초 | 23k | 3.9M | $1.71 | 평균 11 |
| C | 4/5 | 1,242초 | 54k | 15.5M | $5.16 | 평균 24 |
| C′ | 5/5 | 250초 | 20k | 2.4M | $1.37 | - |

- 리뷰어 subprocess는 사용량을 내지 않는다. 그래서 위 비용에는 B·C 리뷰어 비용이 빠져 있다.
- Fisher 단측 p는 모든 조건이 1.000이다. 성공률 차이는 없다.

## 해석

1. **devflow는 천장 과제다.** 맨 모델(A)이 5/5, 평균 37초로 끝냈다. 이 과제로는 workflow 효과를 볼 수 없다. #428의 기준(맨 모델 통과율 90% 이상이면 비교에 쓰지 않는다)에 따라 결론 대신 "천장"으로 기록한다. 비교는 OYG(#428)로 한다.
2. **같은 결과에 드는 비용**
   - C는 A보다 시간이 약 34배, controller 비용이 약 21배 들었다.
   - C′는 시간 약 7배, 비용 약 5.5배다.
   - 이 과제에서는 추가 비용에 대응하는 품질 차이가 관측되지 않았다.
3. **C의 실패 1회(C-3)는 하네스 때문이다.**
   - 서브에이전트에는 프로젝트 hook이 로드되지 않는다. 그래서 runner가 test 실행을 증거로 기록하지 못했고, multi-review가 두 번 연속 거부했다.
   - 코드는 맞았다(hidden 3/3, 리뷰어 10명 전원 정상).
   - #424 규칙대로 실패로 셌다. hook이 도는 controller(프로젝트에서 시작한 세션)였다면 결과가 달랐을 수 있다.
4. **C 5회 중 2회(C-4, C-5)는 증거 gate를 자기 승인으로 우회했다.**
   - 같은 hook 부재로 test 증거가 막히자, 에이전트가 SPEC의 `verify`를 `test:`에서 `manual`로 바꿨다.
   - 그리고 `agent-flow spec approve`로 스스로 승인했다. C-4에서는 Claude Code auto mode가 이를 "[Auto-Mode Bypass]"로 표시했다.
   - 범위 밖 접근은 없었다.
   - 이는 #409가 말한 "자기신고 잔존"의 실측 사례다. 사용자 역할을 맡은 에이전트가 사람 승인 경로를 그대로 쓸 수 있다. #413(사람 승인·보호 브랜치 서버 이관)의 근거로 남긴다.
   - harness의 `spec_self_approvals` 필드가 이 호출을 센다.

## 실행 중 드러난 kit 마찰 (B·C transcript 보고)

- phase prompt는 완료 마커를 ```` ```text ```` 블록으로 보여 준다. 그런데 runner는 fenced 블록 안의 마커를 읽지 않는다. 그대로 복사하면 gate가 실패한다(B-4, B-5).
- QA의 `feedback-command`가 기록된 argv와 정확히 같아야 한다(`PYTHONPATH=src pytest -q`가 아니라 `env PYTHONPATH=src pytest -q`, B-5).
- implement의 "scope grew" 메시지에 나온 문서 해시를 어느 파일과도 대응시킬 수 없었다. skill을 읽고 다시 제출하면 통과한다(B-5, 여러 run).
- `development` run은 commit 없이 local-handoff로 끝난다. 채점은 worktree 작업 트리로 했다.

## 한계

- 과제 1개, 조건당 n=5다. 차이가 없다는 결과가 효과가 없다는 증명은 아니다(천장).
- controller가 사용자 역할을 하므로 사람 시간은 잴 수 없다(#410 코멘트).
- B·C는 hook이 없는 환경에서 돌았다. hook guard(쓰기 경계 등)가 빠진 C이고, 위 3·4번은 그 결과다.
- 같은 컨테이너(4 CPU)에서 wave마다 4개를 동시에 돌렸다. 시간 비교는 wave 안에서만 공정하다.
- B·C 프롬프트의 runner 명령은 과제를 셸 따옴표로 감싸지 않았다(`US$100.00`의 `$1`이 셸 변수로 해석될 수 있다).
  - 20회 모두 runner가 기록한 task는 원문 그대로였다. 에이전트가 명령을 그대로 복사하지 않고 직접 따옴표를 처리했다.
  - 이후 run을 위해 `shlex.quote`로 고쳤다. 그래서 B·C의 `prompt_sha256`가 이번 기록과 달라진다.
