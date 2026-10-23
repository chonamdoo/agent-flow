# Codex 인계 — 2026-10-23 (KST)

실험 run은 **0회**다. `evals/arm_compare.py drive`, 실제 피실험 모델 호출, 권한 우회 옵션은 사용하지 않았다. main·태그·원본 기록 브랜치에는 push하지 않았고 PR을 merge하지 않았다. 비공개 과제의 명세·검사·구현·mutant·규칙 상세는 공개하지 않는다.

## 작업 브랜치·커밋·PR

| 저장소 | 새 브랜치 | 작업 커밋 | PR / 기반 |
|---|---|---|---|
| 비공개 evals | `feat/holdout-2` | `e2f04f8` 후보 작성, `9c29ddb` bytecode 제외, `46feb48` lint 검토 문서, `9e45921` 검사 보강, `1eb326e` 지문·보고 갱신 | [private #3](https://github.com/chonamdoo/agent-flow-evals-private/pull/3), draft; 기반 `feat/mobile-tabs-v0` `b8b338c` |
| 공개 agent-flow | `codex/neutral-completion-409` | `eed6367` | [#464](https://github.com/chonamdoo/agent-flow/pull/464); 기반 `feat/epic-409-arm-ps` `5534c53` (#463) |
| 공개 agent-flow | `codex/handoff-2026-10-23` | `327a369` 최초 기록; 최종 metadata 커밋은 이 PR의 head를 따른다 | 인계 PR 링크는 아래 기록; 기반 `docs/epic-409-program-record` `12bc814` |

## 1. 두 번째 holdout

후보를 비공개 PR에 작성했다. 명세·계약, 기준 구현 patch, 숨은 검사 **38개**, mutant **20개**, validity 정적 보고서, 후보 fingerprint가 있다. install은 `[]`다. patch 적용 검사 **20/20**, 검사 id 중복 **0**, seed-test 원본과 차이 **0**. JSON/Python 문법, reference patch 적용 가능성, whitespace, fingerprint 일관성을 정적으로 확인했다.

**동결 완료가 아니다.** JDK 21은 있으나 Android SDK가 없어 Gradle을 실행하지 않았다. 기준 구현 S와 mutant 실측 검출률은 미측정이다. 기준 구현·검사를 같은 세션이 작성했으므로 별도 작성자의 블라인드 독립 정상 구현 증거도 남아 있다. 독립성이나 통과를 사실로 기록하지 않았다. 비공개 README의 정확한 후속 문구는 **“A 3회로 난이도 확인 필요”**다.

남은 일: SDK 환경에서 기준 구현 S=1(사전등록 repeat 3), 독립 정상 구현, 유효 mutant 20개 중 숨은 검사 검출 **18개 이상**을 확인한다. 실행 중 검사/채점기 결함은 동결 전에 수정하고 비공개 README에 남긴다. 최종 fingerprint는 그 뒤 고정한다. 현재 후보 지문을 동결 승인으로 사용하면 안 된다. 난이도 확인 A 3회는 이후 Claude 환경에 남긴다.

## 2. 사이클 2a

**판정하지 않았다.** 확인한 최신 공개 원격 `12bc814`의 HANDOFF 끝에 “사이클 2a 결과(자동 기록)”가 없고, 비공개 결과에도 cycle2a-runs.json이 없다. 조건이 충족되지 않아 AP/PS/B/D의 수치나 판정을 만들어 내지 않았다. A/A 6건은 기존 runs.json에서 확인만 했다.

자동 기록 이후 별도 새 브랜치에서 판정한다. 기준 A n=6, 비용 잡음 바닥 14%, 시간 10%. 천장 과제에서는 비열등과 비용만 본다. B-1/D-1의 pending 차단 여부와 리뷰어 비용 하한/전체 비용 20% 여부는 결과가 없어 미측정이다. 리뷰어 계측 PR 착수 여부도 결정하지 않는다. 재실험이나 runner·승인 변경은 이번 작업에 없다.

## 3. 중립 완료 선언 PR

[#464](https://github.com/chonamdoo/agent-flow/pull/464)는 `TASK-STATUS`로 새 완료 선언을 통일했다. arm_compare, completion parser, C′ producer 및 관련 fixture·설명을 바꾸고 C′ rules_version은 5로 올렸다. focused 테스트 **24 passed**, 접두어 대응 후 base/head 분류 **10/10 일치**. 설치 asset 변경 **0**. 전체 CI 상태는 아래 최종 확인에 기록한다.

현재 실험에 적용하거나 merge하지 않는다. 구형 transcript는 기존 harness로 읽어야 한다. 기존 case는 수정하지 말라는 지시에 따라 구형 선언 문구가 남은 계약을 그대로 두었다. 다음 판에 그 case를 사용하려면 별도 범위에서 계약 판·지문을 정리해야 한다. 자세한 검증 범위는 같은 PR의 `NEUTRAL-COMPLETION-VERIFICATION.md`에 있다.

## 4. lint 전환 검토

비공개 PR #3에 검토 문서만 추가했다. **8/8 규칙은 정적 표현 가능**하다는 설계 판단이며, 구현·정밀도·성능은 미검증이다. 도구 코드·기존 평가 규칙은 변경하지 않았다. 현재 실험 도입은 보류한다. 공개 문서에 규칙별 상세나 과제 내용은 싣지 않는다.

## 최종 확인

- 인계 PR: 생성 뒤 이 줄을 갱신한다.
- #464 전체 CI: 진행 중. 로컬 focused 24건 통과와 구별한다.
- private #3: draft / OPEN. 실행 검증과 독립성 조건이 남아 있다.
- 원본 공개 `docs/epic-409-program-record`와 비공개 `feat/mobile-tabs-v0`는 각각 `12bc814`, `b8b338c`를 기준으로 읽었으며 이 작업의 push 대상이 아니다.
- 인계 문서는 문서 변경이다. 함수 계보 조사·실행 테스트는 미적용, 공개 diff와 비공개 정보 유출 여부를 확인했다. 기존 HANDOFF 및 프로그램 계획은 수정하지 않았다.
