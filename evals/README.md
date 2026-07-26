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
