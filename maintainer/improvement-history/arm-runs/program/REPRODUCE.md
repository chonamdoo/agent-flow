# 재현 절차 (#409 arm 비교)

2026-10-22~23에 쓴 환경과 명령이다. 다른 계정에서 같은 결과를 다시 내거나 실험을 이어가려면 이 순서를 따른다.

## 1. 저장소

```bash
git clone https://github.com/chonamdoo/agent-flow
git -C agent-flow fetch origin experiment/epic-409 fix/epic-409-arm-scoring-instrumentation \
  feat/epic-409-external-case feat/epic-409-fix-loop-reapproval results/epic-409-oyg-cycle1
# 비공개 평가기(접근 권한이 있어야 한다)
git clone https://github.com/chonamdoo/agent-flow-evals-private
git -C agent-flow-evals-private fetch origin feat/oyg-evaluator-v1 feat/mobile-cart-holdout-v0 \
  feat/mobile-cart-v1 feat/mobile-checkout-v0
```

- harness는 `evals/arm_compare.py`다. 기능에 따라 브랜치가 다르다.

  | 브랜치 | 내용 |
  |---|---|
  | `fix/epic-409-arm-scoring-instrumentation`(#452) | 사전등록 성공 규칙, subagent·토큰 계측, checkout 선택 |
  | `feat/epic-409-external-case`(#454) | 비공개 과제 `--case-dir`, seed patch |
  | `feat/epic-409-fix-loop-reapproval`(#455) | fix-loop 상한 재승인 |

- 모바일 과제를 C 조건과 재승인으로 돌리려면 #454와 #455를 합친 로컬 브랜치를 쓴다.
- 비공개 평가기
  - `feat/mobile-checkout-v0` 브랜치에는 `mobile-cart/`, `mobile-cart-v1/`, `mobile-checkout/`이 모두 있다. 뒤 과제의 seed가 앞 과제의 `reference/reference.patch`를 쓴다.
  - OYG 평가기는 `feat/oyg-evaluator-v1` 브랜치의 `oyg/score.py`다.

## 2. 환경 (측정 당시)

| 도구 | 버전 |
|---|---|
| Claude Code | 2.1.308(run 당시), 2.1.309(기록 시점) |
| Codex CLI | 0.163.0(리뷰어와 mutant 작성) |
| 모델 | 모든 controller와 서브에이전트가 `claude-opus-5-5` |
| Python | 3.13. venv에 pytest 9.1, pytest-xdist, pytest-split, PyYAML |
| Node | 22 |
| JDK | 21(Gradle 실행) + 17(Android Gradle toolchain, `apt install openjdk-17-jdk`) |
| Android SDK | `/opt/android-sdk`: platforms;android-36, build-tools;36.0.0, platform-tools. `dl.google.com` 네트워크 허용이 필요하다 |
| 브라우저 | Chromium(Playwright, OYG 평가기) |

**Maven Central 우회.** 공용 egress에서 Maven Central이 429를 주면 `~/.gradle/init.d/central-mirror.gradle.kts`를 둔다. 평가 환경에도 똑같이 있어야 한다.

```kotlin
val centralMirror = "https://maven-central.storage-download.googleapis.com/maven2/"
beforeSettings {
    pluginManagement.repositories.maven(centralMirror)
    pluginManagement.repositories.gradlePluginPortal()
    pluginManagement.repositories.google()
    dependencyResolutionManagement.repositories.maven(centralMirror)
}
allprojects {
    tasks.withType<Test>().configureEach {
        systemProperty("robolectric.dependency.repo.url", centralMirror)
    }
}
```

**권한.**
- controller는 `claude -p --permission-mode auto`로 돈다. 우회 플래그(`--dangerously-skip-permissions`)는 쓰지 않는다.
- runner, hook, 승인 경로, 평가기는 금지 구역이다. 구현 PR만 내고 merge하지 않는다. 자동 모드 분류기가 runner 편집을 막은 적이 있다(README).

## 3. 한 run의 수명

```bash
cd agent-flow            # harness 브랜치 checkout
export ANDROID_HOME=/opt/android-sdk
PY=python3               # venv 권장

# 준비: 프로젝트 생성, kit 설치(B·C), 프롬프트 작성. arm은 A | B | C | Cp
$PY evals/arm_compare.py prepare --kit . --arm A --case oyg --trial 1 --output runs/oyg/A-1
$PY evals/arm_compare.py prepare --kit . --arm C --case x \
    --case-dir ../agent-flow-evals-private/mobile-checkout --trial 1 --output runs/mchk/C-1

# 실행: 같은 claude -p controller, 2시간·$30 상한. phase 승인과 fix-loop 재승인은 harness가 한다
$PY evals/arm_compare.py drive runs/oyg/A-1 --timeout 7200 --budget 30

# 채점: 비공개 평가기
$PY evals/arm_compare.py score runs/oyg/A-1 --evaluator ../agent-flow-evals-private/oyg/score.py
$PY evals/arm_compare.py score runs/mchk/C-1 --evaluator ../agent-flow-evals-private/mobile-checkout/score.py
# harness 결함으로 끊긴 시도는 분모 밖에 둔다
$PY evals/arm_compare.py score runs/oyg/C-1-try2 --evaluator ... --harness-error "<사유>"

# 요약(사전등록 규칙)
$PY evals/arm_compare.py summarize runs/oyg/*/result.json
```

- 한 사이클의 run은 하나씩 차례로, seed를 정해 섞은 순서로 돌린다. 예: `oyg-cycle1/order.txt`, seed 20261022.
- 결과 기록은 `maintainer/improvement-history/arm-runs/<날짜>-<과제>/`에 둔다. 넣을 것은 `results.jsonl`(result.json 한 줄씩), `summary-table.md`, `README.md`다.

## 4. 비공개 과제 만들기와 검증 (mobile-cart, mobile-checkout과 같은 절차)

1. 과제 폴더에 `task/{prd,goal,contract}.md`, `seed.json`, `case.json`, `seed-tests/`, `checks/`(검사 id `MCnnn`), `CANARY`, `score.py`, `validity.py`, `scorer/hidden-test-deps.gradle`을 둔다.
2. seed 기준선: `python3 -I <task>/score.py --build <seed checkout>` → H 0, R·B 참이어야 한다.
3. 독립 기준 구현
   - 검사를 본 적 없는 별도 에이전트가, harness로 준비한 checkout에서 PRD와 계약만 보고 만든다. 비공개 경로를 읽지 말라고 지시한다(격리는 instructed).
   - 채점한 뒤 실패마다 PRD 원문으로 판정한다. 검사가 틀렸으면 검사를, 구현이 틀렸으면 구현을 고친다. PRD가 바꾸게 하는 seed 테스트는 `seed.json` `r_exempt`에 넣고 근거를 적는다.
   - S=1이 되면 `git format-patch`로 `reference/reference.patch`를 저장한다.
4. mutant
   - Codex가 PRD와 기준 구현만 보고 20개를 쓴다. 테스트는 지운 사본을 준다. 요청문은 각 과제 `mutants/BRIEF.md`.
   - 평가기를 동결(`evaluator_sha256` 기록)한 뒤 `python3 -I <task>/validity.py mutants --reference <기준 구현 checkout> --output report.json`을 **한 번만** 돌린다.
5. 맨 모델 파일럿: A 3회. 3/3이면 과제를 어렵게 한다(사전등록 기준).
6. 기록은 과제 `README.md`와 `validity-reports/<날짜>/`에 남긴다.

## 5. 적대적 리뷰 재현

- Codex: 리뷰 대상과 `reviews/BRIEF.md`를 빈 폴더에 넣고 `codex exec --skip-git-repo-check -C <폴더> -s workspace-write "Read BRIEF.md ... do exactly what it says."`를 돌린다.
- Claude: 같은 요청을 새 서브에이전트에게 준다. 대상 파일만 읽게 한다.
- 두 리뷰어는 서로의 결과를 보지 않는다.

## 6. 알려진 함정

- Maven Central 429 → 위의 미러.
- Robolectric은 화면 밖 `LazyColumn` 줄을 그리지 않는다 → 검사는 긴 화면 qualifier(`w411dp-h1800dp`)로 돈다.
- 옮겨 둔 run 디렉터리는 #452 이후 자기 project로 채점된다. 그래도 run 디렉터리는 옮기지 않는 편이 안전하다.
- 동시에 여러 Gradle·브라우저 작업을 돌리면 시간이 늘어난다. 평가기 오류율 E로 흔들림을 확인한다.
