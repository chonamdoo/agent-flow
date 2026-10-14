# Resolved author context: focused before/after measurement

작성 phase에 이미 해석된 profile과 required-read plan이 있는데도 전체 skill index와
workflow/run metadata에서 선택을 다시 찾도록 안내하던 중복 절차를 정리했다.
필수 본문·적용되는 참조·scope 변경 시 선택 갱신 의무는 유지한다.

이 기록은 **집중 author 평가이며 릴리스 게이트가 아니다**. 세 시도 모두
`backend/clean`, `app/clean`, `rn/local`을 같은 fixture·task·oracle로 수정 전/후 각 3회,
총 18회 실행했다. 모든 kit은 0.3.16이다. v0.3.15 대비 릴리스 평가는 별도다.
첫 시도의 app 증가를 보존하고 지침을 변경한 뒤 두 번째 시도를 실행했다.
세 번째 시도는 PR 리뷰 지적에 따라 red phase와 설치되는 workflow contract 문구까지
맞춘 최종 kit을 측정했다. 잘 나온 시행만 골라 합치거나 서로 다른 시도의 before/after를
섞지 않았다. 시도마다 실행 환경이 달라 시도 사이의 절대값은 비교하지 않는다.

## 최종 결과 — attempt-3

아래 값은 동일 사례 3회 합계다. 비캐시 입력은 native terminal의
`input_tokens - cached_input_tokens`이며 USD 청구액은 관측하지 않았다.

| 사례 | 비캐시 입력 before | after | 변화 | 전체 입력 변화 | 코드 수정 전 command 출력 바이트 변화 |
| --- | ---: | ---: | ---: | ---: | ---: |
| backend/clean | 184,884 | 155,865 | -15.7% | -21.3% | -13.0% |
| app/clean | 228,509 | 211,371 | -7.5% | -25.8% | -14.3% |
| rn/local | 198,287 | 187,272 | -5.6% | -11.0% | -31.6% |
| 합계 | 611,680 | 554,508 | -9.3% | -20.1% | -20.8% |

- 실제 전체 catalog 출력: before 2회(rn/local) → after 0회. 이 실행의 before는
  attempt-2보다 catalog를 덜 출력했다. Escaped workflow `source_text` 출력은 두 쪽 모두
  0회이므로 workflow 출력 절감을 주장하지 않는다.
- command 수: 423 → 319. 코드 수정 전 command 출력 바이트는 -20.8%다.
- behavior/plan/norm: 18/18 통과. 필수 skill 읽음률: 18/18에서 1.0.
  읽음률은 성공한 SKILL.md 경로의 관측이며 전체 본문·참조 이해를 증명하지 않는다.
- Clean architecture-lint: kit마다 6 pass, RN/local 3 n/a. n/a는 pass로 세지 않는다.
- 출력 토큰 합계: 56,905 → 54,528(-4.2%).
- app trial 1 비캐시 입력: 67,443 → 89,466(+32.7%). 나머지 8쌍은 줄었다.
  모든 개별 시행이 줄었다고 보고하지 않는다. 3회 합계의 감소를 장기적인 보장이나
  통계적 확정으로 보지 않는다.
  - 이 시행에서 줄어든 것: 전체 입력 1,340,147 → 977,658(-27.0%), command 53 → 29개,
    command 출력 222,017 → 195,333B. catalog·workflow 출력은 0회다.
  - 비캐시가 늘어난 이유: cached 입력이 1,272,704 → 888,192로 전체 입력보다 더 크게 줄었다.
    prompt·command 출력 바이트/4와 출력 token으로 잡은 새 내용 추정치는 약 66.6k → 59.2k로
    오히려 줄었고, 추정치를 넘는 비캐시는 +0.8k → +30.2k다. 이 크기는 당시 맥락 전체를 한 번
    cache 없이 다시 보낸 양과 비슷하다. before kit의 backend trial 3에도 +27.5k가 있다.
    terminal 합계만으로는 원인을 확정할 수 없다. kit 변경이 추가 내용을 보낸 증거는 없으므로
    서버 prompt cache 변동으로 추정한다. 비캐시 값 자체는 `input_tokens - cached_input_tokens`
    그대로이며 `verify.py`가 다시 계산한다.
  - 이 시행은 이미 읽은 SKILL.md 6개를 곧바로 한 번 더 읽었다. 아래의 남은 재읽기 한계에 해당한다.
- 이미 읽은 SKILL.md 재읽기는 합계 47 → 43회 남았다. 실제 모델의 중복 읽기 제거는
  확인하지 못했다. 이 PR은 자동 실행 필터나 문서 읽기 캐시를 추가하지 않는다.
- 감소폭은 attempt-2(-27.3%)보다 작다. 실행 환경과 before의 catalog 출력 횟수가 달라
  두 시도의 차이를 지침 변경 효과로 귀속하지 않는다.

## 이전 결과 — attempt-2

리뷰 수정 전 kit(`4e4908a`)의 결과다. 같은 방식의 3회 합계다.

| 사례 | 비캐시 입력 before | after | 변화 | 전체 입력 변화 | 코드 수정 전 command 출력 바이트 변화 |
| --- | ---: | ---: | ---: | ---: | ---: |
| backend/clean | 159,419 | 101,080 | -36.6% | -7.9% | -38.7% |
| app/clean | 168,440 | 161,938 | -3.9% | -17.4% | -21.3% |
| rn/local | 174,782 | 102,412 | -41.4% | -4.8% | -40.7% |
| 합계 | 502,641 | 365,430 | -27.3% | -11.0% | -32.6% |

- 실제 전체 catalog 출력: before 8회 → after 0회. Escaped workflow `source_text`
  출력은 before 1회(app/clean trial 1) → after 0회다. 한 번의 관측이므로 workflow 출력
  절감을 주장하지 않는다.
- behavior/plan/norm 18/18 통과, 필수 skill 읽음률 18/18에서 1.0.
- 출력 토큰 합계: 47,044 → 45,565(-3.1%). backend/clean만 보면 +11.9%다.
- app trial 3 비캐시 입력: 66,745 → 68,328(+2.4%).
- 이미 읽은 SKILL.md 재읽기는 합계 36 → 41회 남았다.

## 첫 수정 결과 — attempt-1

전체 catalog 출력은 8 → 0회, 비캐시 입력 합계는 421,254 → 374,761(-11.0%)였다.
그러나 app/clean은 142,608 → 163,792(+14.9%)로 증가했다. 전체 입력은
1,709,200 → 1,466,704(-242,496)인데 cached 입력이 1,566,592 →
1,302,912(-263,680)로 더 크게 줄어 uncached가 +21,184 증가했다.
캐시가 줄어든 원인을 native terminal 한 개의 합계만으로 확정할 수 없다.
재읽기 추가 횟수도 39 → 40회 남았다. 이 결과를 app 비용 개선으로 판정하지 않았고,
동일 본문·지문을 가리키는 후속 read 지시가 기존 읽음을 재사용하도록 지침을 보완했다.
동일한 kit을 성공할 때까지 반복 실행한 것이 아니다. 각 시도의 변경은 `source.patch.gz`에 있다.

## 앞선 릴리스 평가에서 무엇을 확인했나

처음 보고한 작성 +2.7%는 v0.3.15/첫 v0.3.16 후보의 전체 author 36쌍 결과다.
이 PR의 app 3쌍 결과와 분모가 다르다. 원본 168-unit 기록은 보류 중인 PR #364에 있다.

- 작성 uncached: 1,782,736 → 1,830,093(+47,357, +2.656%).
- 36쌍 모두 경로를 정규화한 native prompt가 같고 prompt byte 합계는 감소했다.
- RN/local trial 3의 index는 before/after 모두 56,535B이고 SHA-256도
  `272bf393ce98b17aa4720155662821945f4e0dc50f8bc231be0db479a5cc1f3d`로 동일했다.
  index 비대화나 #354/#355/#356의 신규 회귀가 증가 원인이라는 근거는 없다.
  세 PR은 baseline v0.3.15에도 이미 들어 있다.
- backend/clean trial 1은 추가 profile+index 출력 57,580B,
  app/clean trial 2는 grep 실패(exit 2)에도 전체 workflow source_text 42,229B,
  RN/local trial 3은 전체 index 56,535B가 model에 전달됐다.
- 전체 command 출력은 6,415,632 → 6,924,094B(+7.9%)였다.
  추가 출력과 캐시 변화는 관측했지만 각 출력에 +47,357 token을 나눠 귀속할 수는 없다.

`input-token-attribution.json`, `author-skill-index-volume.json`,
`token-baseline-refactor-proof.json`, `version-refactor-token-correlation.json`에 근거를 보관했다.

## 조건과 보관

- base: `4ff46ad18baa50348016112016aadf4aa98f1648` (origin/main).
- attempt-1 source: `f9014b7cba85f6093c091f1242aa0e50317941e3`.
- attempt-2 source: `4e4908a675c92ff9f9662fe0fb83c479a4e578a3`.
- attempt-3 source: `96eb5b1c07f7fa6eedb6c92453b69663c1b671dd`. kit 지문은 before
  `91644dcc…5fa4`(attempt-2 before와 같음), after `3adac1cf…cd2d9`다.
  evaluator 지문은 세 시도 모두 `c90abd23…4d69`다. 측정 뒤 PR head(`00e0526`)
  위로 rebase해 같은 변경이 `a12cf99`가 됐다. rebase로 바뀐 파일은 `evals/`와 `tests/`뿐이라
  kit 지문은 측정한 after와 같은 `3adac1cf…cd2d9`다.
- 입력은 저장소 밖에 각각 `git archive`로 추출했다. attempt-1/2의 원본 tar, 실제
  wheel/sdist/npm 패키지와 설치된 0.3.16 wheel CLI는 클라우드 증거 폴더에 보존한다.
  attempt-3은 패키지를 만들지 않고 archive 트리를 kit으로 직접 설치했다.
- 공식 Codex CLI 0.162.1, 요청 model `gpt-6.1-sol`, 동시성 8,
  author sandbox `workspace-write`, 기존 phase-only green 과제와 oracle 사용.
- attempt-1/2: 클라우드 Linux, Python 3.12.14, Node 24.19.0, Flutter 3.47.6 / Dart 3.13.5.
  attempt-3: 로컬 macOS arm64, Python 3.12.13, Node 24.14.1, Flutter 3.47.2 / Dart 3.13.1.
  app oracle는 기존 pure Dart fixture다. 실제 Flutter UI 앱 평가로 보고하지 않는다.
- 54개 호출의 prompt/stdout/stderr checksum, command receipt, terminal usage를
  검증했다. provider error 0. 서버 stream은 model identity를 제공하지 않아 관측값은
  unknown이며, 요청 model 확인과 관측 model 확인을 구분한다.
- `native-evidence.tar.gz`는 각 unit의 원본 prompt/stdout/stderr/receipt와 실제 설치
  catalog를 포함한다. 로그에 나온 절대 경로는 attempt-1/2는 클라우드 경로,
  attempt-3은 로컬 macOS 경로다. attempt-3 원본에서 credential 형태의 문자열은
  발견하지 못했다.
- `context-read-analysis.json.gz`는 첫 구현 파일 변경 이전의 command 목록과 바이트를
  별도 저장한다. fixture에서 관측한 file_change와 literal source redirect가 경계다.
  세 시도 모두 미확인 경계는 없다. shell 일반 동작을 완전히 해석하는 검사기가 아니다.
  attempt-1/2의 executor는 저장소에 없다. attempt-3은 `focused_run.py`로 실행하고
  `package_attempt.py`로 묶었다. 이 경계 규칙은 attempt-2의 18개 경계를 native stream에서
  그대로 재현한다. 재읽기 횟수는 evaluator의 `_codex_read_paths`가 성공으로 본 SKILL.md
  읽기를 skill별로 센 값이다.
- CLI는 per-turn uncached 사용량을 제공하지 않는다. 해당 읽기 구간의 token/청구액은
  추정하지 않는다. raw 바이트와 terminal token을 같은 단위로 비교하지 않는다.

## 회귀 확인과 재현

선택한 guard 40개를 명시적 node id로 직렬 실행해 모두 통과했다.
`guard-command.json`, `guard-output.log`, `guard-result.json`에 실행을 기록했다.
첫 실행의 20개 실패는 `/var/tmp/agent-flow-onboarding/.git` 조상에 의해 non-Git fixture가
추적 상태를 판단하지 못한 문제였다. 독립 임시 경로로 옮겨 같은 코드·단정으로 통과했다.
당시 실패 로그도 `initial-*.gz`로 보관했다. protected .git이나 tracking guard는 변경하지 않았다.

원장 전수 조사에서 245개 항목을 확인했고 관련 후보 105개 중 이번 변경의 계약
#224/#225/#258/#275/#317/#340/#354/#355/#356/#362/#363(11개)를 선택했다.
함수 계보와 선택 근거는 `function-ancestry.log.gz`, `ledger-scope-scan.json`, `design.json`에 있다.
필수 문서·전이 의존성·missing coverage·Clean/local/pending·reviewer/controller 전달 경계를
기존 guard로 대조했다. 새 진단 guard는 혼합 출력·실패 grep·wildcard·filtered catalog·
필수 문서 정상 출력·손상/미완료 native stream을 구분한다. 문구가 같은지만 검사하지 않는다.

선택 profile 3개 × mode 3개 × change condition 5개를 정적으로 측정했다.
필수 skill 누락, 비-Clean roles 누출, head 측정 오류는 없었다. 명시적인 선택/읽기 지침이
추가돼 layer의 전체 전달 바이트는 +0.6%, author +0.8%, reviewer +0.5% 증가했다.
controller는 같았다. `budget-compare.md`에 다른 condition의 증가도 보존했다.
이 결정적 측정은 실제 index 재탐색의 tool 출력이나 서버 캐시를 포함하지 않는다.
CI는 전체 profile/mode를 다시 검사한다. 의도한 지침 증가의 근거는 이 실측이다.
`budget-compare.md`는 attempt-2 kit의 측정이다. red phase 문구를 바꾼 최종 kit의
정적 예산은 PR CI의 `phase_budget`이 다시 측정한다.

리뷰 수정 뒤 같은 40개 guard에 진단기의 unfiltered catalog·누락 파일/비객체 catalog
검사와, `workflowContract()` 파일 계보의 #266/#201 guard를 더해 실행해 모두 통과했다.
명령과 출력은 `review-fix-guard-output.log`에 있다.

저장된 원본 확인은 모델 호출 없이 실행한다:

```sh
python evals/results/phase-author-context-0.3.16-20261014/verify.py
```

앞으로 phase-scoped author 결과에서 catalog/workflow source 출력 회귀를 확인할 때:

```sh
python evals/phase_eval_context_reads.py <native-unit>/stdout.raw \
  --catalog <fixture>/.agent-flow/skills/index.json
```

전체 catalog/escaped workflow source가 나오면 exit 1이다. 전체 catalog는 `--catalog`로 준
설치 index와 내용이 같은 JSON 출력만 센다. 그래서 필터 없는 설치(`selected_skills: "all"`)도
탐지하고, 필터링한 일부 출력은 세지 않는다. `--catalog`는 필수다.
손상된 JSON 줄, type이 없는 event·item, 문자열이 아닌 command/output, 유효하지 않은 Unicode,
decoder 한도를 넘는 중첩이나 정수 자릿수가 있는 event·catalog, 빈/미완료 stream, 읽을 수 없는
파일은 exit 2로 끝난다. event는 `\n`으로만 나누므로 문자열 안의 U+2028·U+0085는 그대로 읽는다.
command 출력 안에서 decode할 수 없는 JSON은 catalog가 아닌 것으로 건너뛴다.
필요한 한 항목을 추출하거나 필수 SKILL.md를 읽는 동작은 실패시키지 않는다.
partial/truncated catalog, 다른 JSON 객체 안에 감싼 catalog, 모든 raw YAML 읽기를 탐지하는
도구는 아니며 runtime gate가 아니다.

실제 모델 재현은 attempt-3과 같은 executor로 18개 unit을 한 번에 실행한다:

```sh
# 저장소 밖의 새 빈 <before>/<after> 디렉터리를 먼저 만든다.
git archive 4ff46ad18baa50348016112016aadf4aa98f1648 | tar -x -C <before>
git archive HEAD | tar -x -C <after>
python evals/results/phase-author-context-0.3.16-20261014/focused_run.py \
  <after> <before> <after> <run-out>
python evals/results/phase-author-context-0.3.16-20261014/package_attempt.py \
  <run-out> <attempt-dir> . 4ff46ad18baa50348016112016aadf4aa98f1648 <after-commit>
```

`focused_run.py`는 Python 3.12 환경(pyyaml, click)과 Codex CLI 로그인을 사용한다.
같은 executor라도 scheduling·서버 캐시 조건까지 같아지는 것은 아니다. 출력 폴더는
저장소 밖의 새 디렉터리를 사용하고, 로컬 SDK/CLI/model 조건 차이도 보고에 남긴다.

전체 pytest suite와 Parity는 PR CI에 맡긴다. 새 kit의 전체 168-unit 평가·릴리스 게이트·
태그·Homebrew 배포는 이번 집중 변경에서 실행하지 않았다. #364의 이전 gate는 변경된
kit을 증명하지 못하므로 배포 전 새 전체 측정 기록이 필요하다.

## PR 리뷰 수정과 테스트 설치 정리

PR #365 리뷰에서 기준 catalog 없이 filtered JSON을 전체 catalog로 세는 오탐을 확인했다.
API와 CLI 모두 catalog를 필수로 받도록 바꾸고, 기준 없이 실행하면 exit 2, 실제 전체 기준과
대조한 filtered output은 exit 0인 재현을 추가했다. `review-catalog-before.json`과
`review-catalog-after.json`에 이전 exit 1 오탐과 수정 결과를 기록했다. 같은 기준 catalog로
원본 36 native 호출을 재판독해 결과가 같음을 확인했다. 이 수정은 shipped kit/evaluator와
실제 모델 입력을 바꾸지 않아 추가 모델 측정은 하지 않았다.

이후 로컬 병렬 리뷰(Claude·Codex reviewer 각 3)에서 확인한 문제도 고쳤다.

- 필터 없는 설치의 index(`selected_skills: "all"`)는 list shape 검사 때문에 전체 catalog로
  세지 못했다. 이제 설치 index와 내용이 같은지만으로 판정한다.
- 손상된 JSON, 객체가 아니거나 type이 없는 event·item, 유효하지 않은 Unicode, 과도한 중첩,
  정수 자릿수 한도(Python 3.11+)가 traceback(exit 1, "넓은 출력 있음"과 같은 코드)이나
  0회 읽음으로 끝났다. 모두 exit 2로 바꿨다. 문자열 안의 U+2028·U+0085 때문에 정상
  evidence를 손상으로 판정하던 줄 분리도 `\n` 기준으로 고쳤다.
- `verify.py`의 무결성 검사가 `assert`라 `python -O`에서 빠진 채 PASS를 출력할 수 있었다.
  명시적 검사로 바꾸고 UTF-8을 지정했다.
- full-feature red phase와 설치되는 workflow contract에 전체 index 재탐색 문구가 남아 있었다.
  같은 Before Starting 절차를 가리키도록 맞추고 최종 kit으로 attempt-3을 측정했다.
- attempt-2의 workflow 출력 횟수를 before 1 → after 0으로 정정했다.

진단기 변경 뒤에도 attempt-1/2/3의 기록된 `context_outputs`는 `verify.py`에서 그대로 재현된다.

사용자 요청에 따라 이전 168-unit과 이번 두 집중 실행의 테스트 설치 중복물을 정리했다.
생성 source/test/phase/readiness 파일은 각각 압축하고 모든 파일 checksum을 원본과 대조한
뒤 제거했다. raw 결과·native 로그·SHA-256·kit archive·패키지·배포용 SDK/CLI/인증·공유
의존 환경은 유지했다. cleanup report 세 개에 결과 hash의 동일성과 source archive hash를
남겼으며 클라우드의 source archive manifest에는 파일별 checksum이 있다.
