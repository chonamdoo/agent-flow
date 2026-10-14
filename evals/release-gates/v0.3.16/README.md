# v0.3.16 paired-model release gate

이 기록은 v0.3.15 tag `075a79a74a330b99059019f6317fa3131f832927`과 PR #365가 머지된 최신 main
`f5bee439bddb85d96545236502e49c1e8d62046e`를 같은 실행에서 측정한 결과다. 이전 PR #364의 gate와
집중 author 평가는 이번 후보를 증명하는 기록으로 재사용하지 않았다.
입력 kit은 두 commit을 각각 `git archive`로 풀었다.

공식 Codex CLI 0.162.1에서 `gpt-6.1-sol`을 요청했고 author 3회·review 2회·동시성 8로
168개 고유 시행(author 72, review 96)과
616개 native 호출을 완료했다. 모든 호출의 prompt/stdout/stderr 체크섬,
command receipt, terminal usage와 집계 및 kit/evaluator/case 지문을 무결성 감사에서 대조했다.
서버 stream이 model identity를 제공하지 않으면 관측 model은 unknown으로 그대로 둔다.
요청 flag 검증을 관측 model 확인으로 보고하지 않는다.

원본 결과와 metadata·무결성 감사는 같은 PR의 `evals/results/phase-0.3.16-20261015/`에
보관한다. 전체 native evidence와 실행 로그·체크섬은 평가 환경의 증거 폴더에도 보존한다.
이 compact record와 원본 gzip은 같은 입력의 comparator로 각각 재판정했다.
기존 threshold는 변경하지 않았다. 정확도·읽기·토큰 비교와 pass/fail은 `gate.md`를 따른다. JSON의 통과 flag를 신뢰하지 않고 기록 행에서 다시 판정한다.

Review의 `expected=null` 8건은 완료·비용에는 포함하고 정확도·오탐
분모에는 넣지 않는다. Author lint는 적용/미적용을 구분하며 `n/a`를 PASS로 세지 않는다.
실제 모델 평가와 버전 준비 CI의 성공은 GitHub release 또는 Homebrew 배포 완료의 증거와 구분한다.

재판정은 checkout root에서 다음과 같이 실행한다. `<before>`는 `git archive v0.3.15`로
저장소 밖에 푼 clean directory다.

```sh
python evals/phase_eval_compare.py evals/release-gates/v0.3.16 \
  --require-kit . --require-before-kit <before> --require-evaluator .
```
