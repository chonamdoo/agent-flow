# Resolved author context: focused before/after measurement

작성 phase에 이미 해석된 profile과 required-read plan이 있는데도 전체 skill index와
workflow/run metadata에서 선택을 다시 찾도록 안내하던 중복 절차를 정리했다.
필수 본문·적용되는 참조·scope 변경 시 선택 갱신 의무는 유지한다.

이 기록은 **집중 author 평가이며 릴리스 게이트가 아니다**. 두 시도 모두
`backend/clean`, `app/clean`, `rn/local`을 같은 fixture·task·oracle로 수정 전/후 각 3회,
총 18회 실행했다. 두 kit은 모두 0.3.16이다. v0.3.15 대비 릴리스 평가는 별도다.
첫 시도의 app 증가를 보존하고 지침을 변경한 뒤 두 번째 시도를 실행했다.
잘 나온 시행만 골라 합치거나 서로 다른 시도의 before/after를 섞지 않았다.

## 최종 결과 — attempt-2

아래 값은 동일 사례 3회 합계다. 비캐시 입력은 native terminal의
`input_tokens - cached_input_tokens`이며 USD 청구액은 관측하지 않았다.

| 사례 | 비캐시 입력 before | after | 변화 | 전체 입력 변화 | 코드 수정 전 command 출력 바이트 변화 |
| --- | ---: | ---: | ---: | ---: | ---: |
| backend/clean | 159,419 | 101,080 | -36.6% | -7.9% | -38.7% |
| app/clean | 168,440 | 161,938 | -3.9% | -17.4% | -21.3% |
| rn/local | 174,782 | 102,412 | -41.4% | -4.8% | -40.7% |
| 합계 | 502,641 | 365,430 | -27.3% | -11.0% | -32.6% |

- 실제 전체 catalog 출력: before 8회 → after 0회. Escaped workflow `source_text`
  출력은 두 쪽 모두 0회이므로 이 시행에서 workflow 출력 절감을 주장하지 않는다.
- behavior/plan/norm: 18/18 통과. 필수 skill 읽음률: 18/18에서 1.0.
  읽음률은 성공한 SKILL.md 경로의 관측이며 전체 본문·참조 이해를 증명하지 않는다.
- Clean architecture-lint: kit마다 6 pass, RN/local 3 n/a. n/a는 pass로 세지 않는다.
- 출력 토큰 합계: 47,044 → 45,565(-3.1%). backend/clean만 보면 +11.9%다.
- app trial 3 비캐시 입력: 66,745 → 68,328(+2.4%). 모든 개별 시행이 줄었다고
  보고하지 않는다. 3회 합계의 감소를 장기적인 보장이나 통계적 확정으로 보지 않는다.
- 이미 읽은 SKILL.md 재읽기는 합계 36 → 41회 남았다. 읽음 재사용 지침을 명확히
  했지만 실제 모델의 중복 읽기 제거는 확인하지 못했다. 이 PR은 자동 실행 필터나
  문서 읽기 캐시를 추가하지 않는다.

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
- 입력은 저장소 밖에 각각 `git archive`로 추출했다. 원본 tar, 실제 wheel/sdist/npm
  패키지와 설치된 0.3.16 wheel CLI도 클라우드 증거 폴더에 보존한다.
- 공식 Codex CLI 0.162.1, 요청 model `gpt-6.1-sol`, 동시성 8,
  author sandbox `workspace-write`, 기존 phase-only green 과제와 oracle 사용.
- Python 3.12.14, Node 24.19.0, Flutter 3.47.6 / Dart 3.13.5.
  app oracle는 기존 pure Dart fixture다. 실제 Flutter UI 앱 평가로 보고하지 않는다.
- 36개 호출의 prompt/stdout/stderr checksum, command receipt, terminal usage를
  검증했다. provider error 0. 서버 stream은 model identity를 제공하지 않아 관측값은
  unknown이며, 요청 model 확인과 관측 model 확인을 구분한다.
- `native-evidence.tar.gz`는 각 unit의 원본 prompt/stdout/stderr/receipt와 실제 설치
  catalog를 포함한다. 로그에 나온 절대 경로는 당시 클라우드 경로다.
- `context-read-analysis.json.gz`는 첫 구현 파일 변경 이전의 command 목록과 바이트를
  별도 저장한다. fixture에서 관측한 file_change와 literal source redirect가 경계다.
  두 시도 모두 미확인 경계는 없다. shell 일반 동작을 완전히 해석하는 검사기가 아니다.
- CLI는 per-turn uncached 사용량을 제공하지 않는다. 해당 읽기 구간의 token/청구액은
  추정하지 않는다. raw 바이트와 terminal token을 같은 단위로 비교하지 않는다.

## 회귀 확인과 재현

선택한 guard 39개를 명시적 node id로 직렬 실행해 모두 통과했다.
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

저장된 원본 확인은 모델 호출 없이 실행한다:

```sh
python evals/results/phase-author-context-0.3.16-20261014/verify.py
```

앞으로 phase-scoped author 결과에서 catalog/workflow source 출력 회귀를 확인할 때:

```sh
python evals/phase_eval_context_reads.py <native-unit>/stdout.raw \
  --catalog <fixture>/.agent-flow/skills/index.json
```

전체 catalog/escaped workflow source가 나오면 exit 1이다. malformed stream과 빈/미완료 stream은 오류로 끝난다.
필요한 한 항목을 추출하거나 필수 SKILL.md를 읽는 동작은 실패시키지 않는다.
partial/truncated catalog나 모든 raw YAML 읽기를 탐지하는 도구는 아니며 runtime gate가 아니다.

실제 모델 재현은 동일 before/after archive를 사용해 각 사례를 3회씩 실행한다:

```sh
git archive 4ff46ad18baa50348016112016aadf4aa98f1648 | tar -x -C <before>
git archive HEAD | tar -x -C <after>
python evals/phase_eval.py --kit before=<before> --kit after=<after> \
  --stacks backend --modes clean --scenario author --trials 3 --concurrency 8 \
  --author-provider codex --review-providers codex --codex-model gpt-6.1-sol \
  --output <backend-out> --keep
# 같은 명령에서 --stacks app --modes clean, --stacks rn --modes local을 각각 실행한다.
```

이 기록의 원 실행은 세 조합을 한 executor에서 동시에 배치했다. 위 공개 CLI로 사례별
실행하면 scheduling/캐시 조건까지 같아지는 것은 아니다. 상대 경로와 출력 폴더는 반드시
저장소 밖의 새 디렉터리를 사용한다. 로컬 SDK/CLI/model 조건 차이도 보고에 남긴다.

전체 pytest suite와 Parity는 PR CI에 맡긴다. 새 kit의 전체 168-unit 평가·릴리스 게이트·
태그·Homebrew 배포는 이번 집중 변경에서 실행하지 않았다. #364의 이전 gate는 변경된
kit을 증명하지 못하므로 배포 전 새 전체 측정 기록이 필요하다.
