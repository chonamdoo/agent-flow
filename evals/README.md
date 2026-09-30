# agent-flow evals

`tests/`는 하네스가 계약대로 동작하는지 본다. 여기는 **프롬프트가 모델 행동을
바꾸는지**를 본다. 둘은 다른 질문이고, 앞의 것이 전부 초록이어도 뒤의 것은
0점일 수 있다.

## 왜 필요한가

이 저장소의 검증 장치는 대부분 "게이트가 통과했다"를 증거로 쓴다. 그건 게이트가
옳다는 증거가 아니다 — 판정자와 주장자가 같아지는 자리다. eval은 그 고리를
끊는다. 판정은 **기계 oracle**이 하고, 그 oracle은 평가 대상 agent가 쓰지 못한다.

기준선은 Vercel이 Next.js 16 API로 측정한 결과다: 문서 없음 53%, on-demand skill
53%(56%의 경우 아예 발동 안 함), 명시적 지시 79%, AGENTS.md 인덱스 100%.
숫자 자체는 프레임워크 API 과제의 것이라 그대로 옮길 수 없다. 옮기는 것은
**메커니즘**이고, 그게 우리 환경에서도 성립하는지를 여기서 잰다.

## 구성

```text
evals/
  cases/<id>/task.md    agent에게 주는 과제 문장
  cases/<id>/seed/      과제 시작 상태 (그대로 복사된다)
  cases/<id>/check.py   기계 oracle. behavior와 case별 norm을 반환한다
  configs.py            컨텍스트 전달 방식 4종
  run.py                러너. (case × config × trial)마다 격리 프로젝트를 만들고 채점한다
```

## oracle

두 축을 따로 잰다.

| 축 | 무엇을 보는가 | 누가 판정하는가 |
|---|---|---|
| `behavior` | seed의 테스트가 통과하는가 | `pytest` |
| `norm` | case별 주석 규범 또는 계층 경계를 지켰는가 | comment-checker 또는 Python AST/import graph |

주석 case에서는 일반 통념으로 쓴 저가치 주석을 comment-checker가 잡는다.
`layer_boundary`에서는 candidate 밖의 canonical test로 behavior를 실행하고,
AST/import graph가 target use case의 sibling use case 참조·주입과 domain의
data/HTTP/framework import를 거부한다. agent의 자기신고는 어느 축에도 쓰지 않는다.

## 실행

```bash
python3 evals/run.py --host claude --trials 5
python3 evals/run.py --host claude --trials 5 --config agents-index --case comment_norm
```

host CLI(`claude` / `codex`)가 필요하고 네트워크를 쓴다. 그래서 pytest 스위트에
넣지 않는다 — 느리고 비결정적이다. 대신 채점 코드는 `tests/test_eval_scoring.py`가
결정적으로 검사한다.

결과는 `evals/results/<timestamp>.json`에 host 상태, norm 사유, source diff와 함께 남는다.

## 지금까지 잰 것 (claude, trials=5)

| case | baseline | skill-ondemand | agents-index | agents-index-noisy |
|---|---|---|---|---|
| `comment_norm` | 100% | 100% | 100% | 100% |
| `no_narration` | 100% | 100% | 100% | 100% |

원자료: `results/20260726T113935.json`, `results/20260726T115348.json`.

### 결론: 이 두 case에서 계측기는 해상도가 없다

**baseline이 이미 만점이다.** 컨텍스트를 하나도 주지 않아도 모델이 우리 규범을
지킨다. 천장에 붙은 점수는 전달 방식으로 올릴 수 없으므로, 이 결과는
"AGENTS.md 인덱스가 낫다"도 "차이가 없다"도 증명하지 않는다. **아무것도**
증명하지 않는다.

세 번째 관측이 이를 굳혔다. `key=value` 한 줄 파서를 baseline으로 4회 돌린 결과,
네 번 모두 글자까지 같은 최소 구현이 나왔다 — 요청하지 않은 검증도, 서술 주석도,
불필요한 추상화도 없었다. 작고 잘 명세되고 테스트가 붙은 단일 파일 과제에서는
모델의 기본 행동이 이미 `code-generation-discipline`과 일치한다.

### 그래서 무엇이 참인가

- G1~G4·G7(프롬프트 문구, skill summary, retrieval-led 지시, AGENTS.md 인덱스)은
  **이 저장소의 측정으로 정당화되지 않았다.** 근거는 Vercel이 *다른 과제군*
  (훈련 데이터에 없는 프레임워크 API)에서 측정한 메커니즘이다. 해롭다는 증거도
  없고 이롭다는 증거도 없다.
- G6(안 쓰는 skill의 잡음 비용)도 마찬가지다. 가짜 이름 200개를 섞어도 점수가
  안 떨어졌지만, 천장에서는 하락을 볼 수 없다. **부재의 증거가 아니다.**

### 해상도를 얻으려면

baseline이 실패하는 과제가 필요하다. 모델이 추론으로 맞힐 수 없는 것, 즉
**이 프로젝트에만 있는 임의 규약**이어야 한다. 위 두 case는 그 조건을 못 채웠다 —
"왜를 적어라", "쓸데없는 주석을 달지 마라"는 이미 모델의 기본값이다.

다음 case가 노려야 할 곳:

- 여러 파일에 걸친 계층 경계 (`clean-architecture-core`의 domain→infra 금지)
- profile이 정하는 DI 경계와 presentation state 모델링
- completion gate 마커의 정확한 이름과 허용값

셋 다 단일 파일 과제로는 재현되지 않는다. seed가 커지고 루프가 느려지는 대신,
baseline이 실제로 틀릴 수 있는 영역이다.

## `layer_boundary` 실측 (claude, trials=5)

| config | valid/attempted | behavior | norm | both |
|---|---:|---:|---:|---:|
| baseline | 5/5 | 100% | 0% | 0% |
| skill-ondemand | 5/5 | 100% | 0% | 0% |
| agents-index | 5/5 | 100% | 20% | 20% |
| agents-index-noisy | 5/5 | 100% | 20% | 20% |

원자료: `results/20260726T233416.json` (`resolution: true`).

20회 모두 `host_ok=true`였고 baseline이 behavior를 통과하면서 5회 모두 norm을
위반했으므로 case 자체의 해상도는 확인했다. 전달 방식 차이는 이 표본에서
agents-index 계열 각각 5회 중 1회 norm 통과로, 구성당 1 trial 차이다. n=5에서
이 차이로 개선을 주장하지 않는다.

같은 harness로 앞서 돌린 `results/20260726T224200.json`은 `agents-index` 1회가
300s timeout으로 `host_ok=false`가 되어 `resolution: false`
(`invalid-host-trials`)로 남았다. 그 실행의 유효 19회는 네 구성 모두
behavior 100% / norm 0%였다. 위 표는 `--timeout 600`으로 20/20을 채운 실행이다.

## 신규 스킬의 실제 리뷰 과제

`skill_tasks.py`는 신규 7개 스킬에 대해 결함·정상 코드·비대상·별도 과제
28개를 Codex CLI로 검토한다. `baseline`은 프로젝트 스킬 인덱스 없이,
`skill-index`는 참조 파일을 포함한 전체 kit 인덱스와 함께 실행한다.
개별 스킬 한 줄의 효과를 분리하는 실험은 아니다.

```bash
python3 evals/skill_tasks.py --model gpt-6-astra --trials 1 \
  --concurrency 2 --output /tmp/agent-flow-skill-eval
```

Codex CLI와 인증, Python의 PyYAML이 필요하다. 모델 API를 사용하므로
수동 실행하며 CI 필수 검증으로 삼지 않는다. 출력 경로는 새 디렉터리여야 한다.
`--case <id>`로 과제를 선택할 수 있다.

- 임시 프로젝트는 읽기 전용으로 검토한다. 출력 디렉터리에 과제 원본,
  CLI 명령·버전, kit/evaluator 파일 해시, JSON 도구 기록과 응답을 보존한다.
- 판정과 지적 위치를 사전에 정의한 결함 범위와 대조한다. 같은 결함의
  대체 지적 위치를 허용하지만, 다른 결함을 발견한 것으로 대신 세지 않는다.
- 조건부 문서 사용은 성공한 읽기 명령과 문서 내용이 출력에 관측됐을 때만
  센다. 단순 자기신고는 인정하지 않는다. 합친 출력·루프·도구 출력 잘림은
  관측을 누락시킬 수 있어 문서별 직접 읽기를 요청한다.
- 시간초과·CLI 실패·출력 잘림·fixture 변경은 실패로 남긴다.
  종료 코드 0은 모든 host 실행이 유효했다는 뜻이지 채점 만점을 뜻하지 않는다.

이 검사는 리뷰 응답과 관측 가능한 참조 사용을 측정한다. 지적 위치 일치는
의미상 정확성, UI·DB 실행, 내부 모델 식별을 증명하지 않는다. 호스트 전역
스킬까지 격리하지 않으므로 baseline을 “스킬이 전혀 없는 모델”로 해석하지 않는다.
잘못된 정상 fixture나 누락한 정답을 수정했다면 이전 결과를 보존하고
사후 채점 수정과 새 모델 실행을 구분한다. 반복하지 않은 단일 시행이나
baseline 만점만으로 스킬의 개선 효과를 주장하지 않는다.

## phase skill 전달 평가

workflow의 각 phase가 필요한 skill을 정확히 받는지, 쓰지 않는 텍스트에 토큰을
쓰지 않는지를 잰다. 두 도구 모두 비교할 kit를 인자로 받는다. 비교 기준 kit는
`git archive <ref> | tar -x -C <dir>`로 만든다.

### `phase_budget.py` — 결정적, 모델 없음

profile × architecture mode × 변경 조건(변경 없음, profile 기본 경로, 계층 경로,
커밋 후 깨끗한 작업 트리)마다 임시 프로젝트에 kit를 설치하고, 그 kit의 runner로
full-feature 모든 phase의 envelope와 multi-review 리뷰어 job을 렌더해 바이트,
필수 skill, 리뷰어 수를 기록한다.

```bash
PYTHONDONTWRITEBYTECODE=1 python evals/phase_budget.py --kit <kit> --output budget.json
```

모델이 목록의 파일을 실제로 다 여는지는 이 측정으로 알 수 없다. 토큰은
bytes/4 추정이다.

두 결과를 비교하려면 `python evals/phase_budget_compare.py before.json after.json`을 쓴다.
어느 변경 조건에서든 필수 skill이 빠졌거나, Clean이 아닌 mode의 envelope에 `roles`가
들어갔거나, 합계 바이트가 늘었거나, 전에 재던 phase를 못 재면 실패한다. PR에서는
`.github/workflows/tests.yml`의 `phase-budget` job이 base와 head를 병렬로 재고 이 비교를 돌린다.

### `phase_eval.py` — 실제 모델, 수동·비차단

`phase-cases/{web,rn,app,backend}` × `clean`/`local`/`team`(설치 기본 mode + 팀
skill)마다 두 kit를 설치하고 각 kit의 prompt를 그대로 쓴다.

- author: full-feature `green` envelope로 Claude 세션 하나가 구현한다.
  `oracle.py`가 behavior(red 테스트를 원본으로 되돌린 뒤 실행), plan(slice-plan에만
  있는 요구를 검사하는 hidden 테스트), norm(mode 규칙, 정적 검사)을 채점한다.
  필수 SKILL.md 읽음률은 성공한 `Read` 경로만 센다. 전체 본문·reference 읽음이나
  실제 규칙 준수의 증거로 해석하지 않는다. norm은 case의 fixture 계약 검사이며
  일반적인 Clean Architecture 인증이 아니다.
- review: 실제 multi-review 리뷰어 job(모든 관점 × claude/codex)을 결함 diff와
  정상 diff에 돌린다. run 폴더에는 author와 같은 prd·ddd-design·slice-plan과
  design-spec을 두고, 리뷰 대상 트리의 테스트 실행 기록(`test-evidence.md`, 전체 출력)을
  남긴다. 테스트는 프로젝트 복사본에서 돌려 실행 부산물이 리뷰 diff에 섞이지 않게 한다
  (`review_test_command`가 있으면 그것을 쓴다). 런타임과 같은 판정 계약을 쓰고, 무효
  리뷰어가 있으면 그 시행은 채점하지 않는다. 결함은 request-changes 판정의
  Must-fix/blocking 지적 안에서만 찾는다. Should-fix·Notes 같은 비차단 구간의 언급은
  탐지로 세지 않는다.

Claude 리뷰어는 `plan` 대신 `default` permission mode에서 `Read,Glob,Grep`만
사용한다. Write/Edit/Bash/Agent는 제공하지 않는다. 이는 평가 CLI의 도구 제한이지
host 전체의 파일시스템 sandbox가 아니다. author의 쓰기 권한은 바꾸지 않았다.
성공한 Claude Read 경로와 문서별 SHA·inline 본문 포함 여부를 별도로 남긴다.
Codex의 shell 읽기는 이 지표로 정규화하지 않으므로 미관측은 `null`이다.

```bash
PYTHONDONTWRITEBYTECODE=1 python evals/phase_eval.py --kit before=<dir> --kit after=<dir> \
  --scenario author,review --trials 1 --concurrency 10 \
  --claude-model <model> --codex-model <model> --output <new dir>
```

결과는 `results/phase-<kit 버전>-<YYYYMMDD>/`에 둔다. 원자료는 크기 때문에 gzip으로
저장한다. `phase_budget.py` 출력은 `budget-<kit>.json.gz`, `phase_eval.py`가 출력 폴더에
쓰는 `results.jsonl`은 `eval-results.jsonl.gz`로 둔다.

실행할 때는 출력 폴더의 `evidence/`에 호출별 prompt·argv·stdout·stderr를 보존한다.
호출의 SHA, 요청 모델, Claude가 보고한 모델, evaluator·case 파일 해시도 남긴다.
raw에는 로컬 경로와 입력 내용이 있으므로 공개 전 별도 검토한다.
토큰은 cache 포함/제외·cache·output으로 나눈다. 누락은 `null`이며, 일부 합계는
`usage_observed`와 `usage_missing_reviewers`로 구분한다. 형식 실패·provider 오류와
코드 판정은 분리하고, 그런 실패 호출의 관측 사용량도 남긴다. evidence 저장 자체가
실패하면 출력을 해석하기 전에 멈춘다. 이때 리뷰어 행의 사용량은 `null`이고, author
행에는 `usage` 필드가 없다.

### `phase_eval_compare.py` — 릴리스 차단 검사

릴리스 전에 이전 릴리스 kit(`before`)과 새 kit(`after`)를 같은 `phase_eval.py` 실행에서
조합당 3회 이상 돌리고, 그 출력 폴더를 이 검사에 넘긴다. 통과하면 `--export`가 릴리스
기록을 `evals/release-gates/v<버전>/`에 쓴다. 이 기록을 릴리스 PR에 커밋한다. 실패하면 exit 1이고
기록을 쓰지 않는다.

```bash
PYTHONDONTWRITEBYTECODE=1 python evals/phase_eval.py --kit before=<이전 릴리스 트리> --kit after=<새 kit> \
  --scenario author,review --trials 3 --claude-model <model> --codex-model <model> --output <new dir>
python evals/phase_eval_compare.py <new dir> --summary <new dir>/gate.md \
  --export evals/release-gates/v<버전>
```

각 출력 폴더는 실제 행의 모든 kit 지문과 Claude·Codex의 비어 있지 않은 모델 이름을 기록해야
한다. 폴더 사이에서 같은 kit의 지문이 다르면 실패한다. 두 kit의 조합별 trial ID 집합도 같아야
한다. 리뷰 행의 `expected`와 결함 ID는 `case_files`의 해시가 일치하는 `case.json`과 대조한다.
행에서 기대 판정이나 결함을 지워 분모를 줄일 수 없다. case가 명시한 `expect: null`은 그대로
허용하며, 무효 시행의 제외·상한 정책은 바뀌지 않는다.

`release.yml`은 태그를 배포하기 전에 태그 트리의 `evals/release-gates/<태그>/`를 태그 트리의
이 스크립트와 기준으로 다시 판정한다. 기록이 없거나 판정이 실패하면 태그를 거부한다. 다음과
같은 경우에도 거부한다.

- 기록의 `kit_digests`가 태그 트리와 바로 이전 태그 트리의 kit 지문(`kit_source_digest`)과
  다르다.
- 기록의 `evaluator_sha256`·`case_files`가 태그 트리의 `evals/phase_eval.py`·`evals/phase-cases`
  와 다르다.

측정 뒤에 kit·평가기·case를 고치면 다시 측정해야 한다. 버전 파일·`Formula/`는 kit 지문에
들어가지 않으므로 릴리스 버전을 올려도 기록을 그대로 쓸 수 있다. 모델 호출이 필요한 측정은
CI에서 돌리지 않는다.

kit 지문은 디렉터리 안의 모든 파일로 계산하고, 태그 쪽은 git이 추적하는 파일만 들어간다.
그래서 측정할 kit은 `git archive`로 풀거나 추적 파일만 복사한 트리를 쓴다. 작업 트리에 남은
`.DS_Store` 같은 파일 하나만 있어도 지문이 달라져 태그가 거부된다.

기록에는 판정에 쓰는 필드만 남긴다. 로컬 경로, argv, 리뷰어 출력 원문은 빠진다. 원자료는
측정한 곳에 따로 보관한다. 이 검사는 측정 없이 태그하는 실수를 막는다. 커밋 권한이 있는
사람이 행을 꾸며 넣는 것까지 막지는 못한다.

기준은 `phase-eval-thresholds.json`에 있다.

- 하한: author 필수 SKILL.md 읽음률 0.95, 리뷰 판정 정확도 0.95, 심은 결함 탐지 1.0.
- 상한: 무효 시행 비율 0.10.
- 이전 kit보다 낮아지면 실패: author 읽음률·세 축 통과율, 리뷰 판정 정확도, 결함 탐지,
  Claude 리뷰어 필수 문서 읽음률. 정상 diff에서 리뷰어가 변경을 요청한 비율은 높아지면 실패다.
- 토큰: 두 kit에서 모두 유효한 unit끼리 합친다. cache 제외 입력이 1.05배, 출력이
  1.10배를 넘으면 실패다. 유효한 unit에 사용량이 없으면 검증할 수 없으므로 실패다.
- 결과가 완전하지 않으면 실패다.
  - `results.jsonl` 행 수가 `meta.json`의 `units`보다 적거나, 읽을 수 없는 줄이나 중복 unit이 있다.
  - 기준 파일의 `matrix`(stack × mode × variant)에 있는 조합이 한 kit에라도 없거나, 조합당
    시행이 3회보다 적다. 두 kit이 같이 좁게 돌아도 통과하지 못한다.
  - 지표의 분모가 없거나, 채점 필드가 빠졌거나 형식이 틀린 행이 있다. `NaN`, 음수 토큰,
    빠진 `correct`·`defects`·읽음 목록이 여기에 해당한다. 이런 행은 0이나 제외로 처리하지 않고 실패로 본다.
- 측정 조건을 확인할 수 없으면 실패다. 각 폴더의 `evaluator_sha256`·요청 모델·case 파일
  해시가 없거나, 폴더끼리 값이 다르면 해당한다. 기준 파일에 모르는 지표 이름이 있어도 실패다.

읽음률은 성공한 Read 경로만 센다. prompt에 본문이 들어간 문서와 Codex의 미관측(`null`)은
분모에서 뺀다. 기준값을 바꿀 때는 PR에 이유와 근거가 된 측정 결과를 적는다.

리뷰어 형식 오류와 provider 오류(예: 모델 용량 부족)도 무효 시행으로 센다. 이전 kit 대비
하락은 허용 폭 없이 실패로 본다. 실패하면 먼저 원인이 kit 변화인지 측정 잡음인지 원자료로
확인한다. 잡음이면 같은 조건으로 전체를 다시 측정한다. 실패한 unit만 골라 다시 돌리거나,
근거 없이 기준값을 낮춰 통과시키지 않는다.

### 해석할 때 주의

- 시행 1회 결과는 방향만 보여 준다. 정확도 개선을 주장하려면 같은 case를
  반복해 차이가 표본 변동보다 큰지 확인한다.
- flutter와 python은 설치 기본 mode가 `pending`이라 team case도 pending이다.
  pending 규칙상 리뷰어가 구조 결정을 이유로 변경을 요청할 수 있어 정상 diff의 정답
  판정이 정해지지 않는다. 그래서 두 case의 정상 diff는 `expect: null`로 두고 판정을
  채점하지 않는다. 결함 diff는 그대로 채점한다.
- author 평가는 한 phase만 새 세션에서 돌린다. 실제 run처럼 대화 맥락이 이어지는
  경우의 효과는 재지 않는다.
- host 전역 skill과 설정은 격리하지 않는다.
- 런타임 Claude 리뷰어는 provider sandbox 안에서 `plan` mode와 기본 도구로 돈다
  (`src/agent_flow/multi_review.py`). 평가 리뷰어의 도구 조건은 이와 다르고, 이 조건
  변경 전에 모은 리뷰 결과와도 다르다. 리뷰 정확도를 런타임 동작으로 옮기거나 조건이
  다른 결과끼리 비교하지 않는다. `meta.json`에 `evaluator_sha256`이 없거나 값이 다르면
  조건이 다른 결과로 본다.

### 0.3.7 기준 정적 측정 (`results/phase-0.3.7-20260929/`)

37개 profile × mode 조합, 계층 경로 변경 조건, full-feature 한 번 통과 기준이다.
필수 읽기는 envelope에 read plan이 있는 phase만 센다. 리뷰어는 subprocess마다
prompt 전체와 그 provider의 필수 읽기를 한 번씩 센다.

| 구간 | 변경 전 | 변경 후 | 차이 |
|---|---:|---:|---:|
| author phase envelope + 필수 읽기 | 5,579k | 5,244k | −6.0% |
| multi-review·architecture-review 조율 세션 | 1,216k | 146k | −88.0% |
| 리뷰어 subprocess | 13,845k | 12,481k | −9.9% |
| 합계 | 20,640k | 17,872k | −13.4% |

조합별 차이는 −10.2%에서 −17.6%다. 필수 skill이 빠진 경우는 red/green의
`code-review` 하나뿐이다(의도한 변경, 네 조건 합계 148개 phase 항목씩). 커밋 후
조건에서는 경로로 선택되는 가이드가 492개 phase 항목에 새로 붙었다. Clean이 아닌
mode의 계층 경로 조건에서 `roles`가 들어간 phase envelope는 480/624에서 0/624가 됐다.
모델 평가 결과와 채점·사례 보정 기록은 같은 폴더의 `summary.md`에 있다.
