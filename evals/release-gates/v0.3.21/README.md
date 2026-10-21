# v0.3.21 paired-model release gate

v0.3.20 (`207a54cdb878c4d7352279a750c2a1e14bbc1f12`)과 #406까지 merge된 v0.3.21 후보 main
(`c1d9dfc9b991eafd975b0860713d9625c32e28f9`)을 같은 실행에서 비교했다. 두 kit은 `git archive`로 만든 추적 파일의
독립 snapshot이다. 평가기와 case는 후보 트리의 것을 썼다(#387·#403으로 평가기와 릴리스 행렬 밖 사례가 바뀌어
v0.3.20 기록을 재사용하지 않고 전체를 측정했다).

Codex CLI `0.163.0`에 `gpt-6.1-sol`을 요청했다. author 3회·review 2회·동시성 12로 4 stack × 3 mode(clean/local/team)
× 2 kit의 168개 고유 unit(author 72, review 96)을 측정했다. 실제 native 호출은 616개다. 단일 provider 평가는
workflow의 독립 Claude/Codex 리뷰를 대신하지 않는다.

## 판정과 이번 릴리스의 한도 예외

[gate.md](gate.md)는 이 기록이 **이번 릴리스에 한해 author·review 비캐시 입력 토큰 비율 한도를 1.05에서 1.06으로
올린 기준**을 모두 통과한다는 판정이다. 유지보수자가 결정했다. 출력 비율 한도(1.10)와 모든 품질 기준은 그대로다.
`evals/phase-eval-thresholds.json`은 이 태그 뒤에 1.05로 되돌린다.

| 지표 | before | after |
|---|---:|---:|
| 리뷰 정답(채점 44개) | 42/44 | 44/44 |
| clean-verdict 오탐률 | 0.017 | 0.000 |
| 결함 탐지 | 1.000 | 1.000 |
| author 필수 읽기·통과율 | 1.000 | 1.000 |
| author 비캐시 입력·출력 비율 | | 1.052 · 1.017 |
| review 비캐시 입력·출력 비율 | | 0.996 · 0.993 |

예외의 근거는 같은 kit 쌍을 두 번 전체 측정한 결과다. 두 번째가 이 기록이다.

| 실행 | author 비캐시 입력 | 전체 입력 | 품질 |
|---|---:|---:|---|
| 1차 (03:00–03:50 UTC, [기록](../../results/phase-0.3.21-20261021/run-1-gate.md)) | 1.069 | 1.015 | 리뷰 정답 43/44 → 44/44, 오탐 0.008 → 0 |
| 2차 (03:52–04:38 UTC, 이 기록) | 1.052 | 1.004 | 리뷰 정답 42/44 → 44/44, 오탐 0.017 → 0 |

stack별 author 비캐시 입력 비율은 1차·2차에 backend 1.088·1.090, app 1.222·1.035, web 1.030·1.130,
rn 0.957·0.963이었다. 두 번 모두 일정한 증가는 backend뿐이다. #394가 backend fixture("주문 목록 endpoint … 400
error body")에 `backend-api-contract` skill을 붙여 author 프롬프트가 285 bytes 늘고 그 skill을 한 번 더 읽는다.
endpoint를 만드는 과제라 맞는 라우팅이며, #394 PR이 이 변화를 미리 적었다. app·web의 증가는 실행마다 다른 stack에서
나타났고 그 stack의 author 프롬프트는 두 kit에서 같거나 작으며(app 14,625 → 14,611 bytes) 실행 항목 수도 늘지 않았다.
이는 비캐시 입력이 캐시 적중에 따라 흔들리는 측정 변동으로 판단했다. backend를 뺀 2차 비율은 1.042다.

1차 실행은 재채점하거나 선택 재시도하지 않았고 이 기록에 섞지 않았다. 1차와 2차 사이에 kit·평가기·case·한도는
바꾸지 않았다.

## 무결성과 배포 검증

[측정 기록](../../results/phase-0.3.21-20261021/)에 compact 결과, 무결성·개인정보 감사, package 검증을 보존했다.
원본 native evidence(prompt·리뷰 원문)는 저장소에 넣지 않는다.

후보에 버전·릴리스 노트·회귀 장부를 더한 릴리스 브랜치 커밋의 `git archive`로 npm tarball·wheel·sdist를 다시
빌드했다. sdist에서 만든 wheel의 진입 skill, 설치한 wheel CLI 버전(`agent-flow 0.3.21`), node installer의 Next.js
clean/local/pending 신규 설치·재설치를 검증했다. 버전 파일·문서는 kit 지문 범위 밖이라, 설치된 kit 지문은 게이트의
after kit(c1d9dfc) 지문과 같다. 실제 host trust/startup과 Homebrew 업그레이드는 검증하지 않았다.

이 기록을 포함한 PR을 merge한 뒤 main의 해당 커밋에 `v0.3.21` 태그를 붙이면 release workflow가 기록과 두 kit의
지문을 다시 검증한다.

재판정(저장소 root, `<before>`는 v0.3.20의 clean archive):

```sh
python evals/phase_eval_compare.py evals/release-gates/v0.3.21 \
  --require-kit . --require-before-kit <before> --require-evaluator .
```
