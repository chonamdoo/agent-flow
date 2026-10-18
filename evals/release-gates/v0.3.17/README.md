# v0.3.17 paired-model release gate

이 기록은 실제 이전 릴리스 v0.3.16 (`89512553cd85a608b02845e1dd1c3d81b4727dca`)과
#368 merge `a7b4734d619e56705d52c016340e5d4b98148554` 기반 0.3.17 후보를 같은 실행에서 비교했다.
before는 tag archive, after는 main archive에 최종 working-tree patch를 적용한 독립 snapshot이다.
kit 지문은 meta.json에 보존했으며 현재 후보와 comparator로 대조했다. 평가기·case·threshold는 변경하지 않았다.

실제 Codex CLI 0.163.0에 `gpt-6.1-sol`을 요청했다. author 3회·review 2회·동시성 8로
4 stack × 3 case mode(clean/local/team) × 2 kit의 168 units(author 72, review 96),
616 native 호출을 완료했다. case의 team은 stack별 설치 architecture 선택을 따른다.
이 단일 provider 평가는 workflow의 독립 Claude/Codex 리뷰와 별개다.
서버 stream은 model identity를 제공하지 않아 관측 model은 unknown이다. 요청 flag 검증과 구분한다.

616개 command receipt와 prompt/stdout/stderr SHA-256, sandbox/model flag, 종료 상태,
terminal usage·집계 및 168개 고유 matrix를 감사했다. 감사 PASS는 모델 판정 정확도 100%라는 뜻이 아니다.
양쪽 kit 모두 backend/clean/clean 2건씩을 잘못 request-changes로 분류했다.
지적은 fixture의 named-test 증거 부족이며 production readiness 변경의 회귀로 관측되지 않았다.
이를 재채점하거나 case·기준값을 변경하지 않았다. `expected=null` 8건은 정확도·오탐 분모에서 제외한다.
Author lint는 kit별 pass 12/n/a 24이며 n/a를 통과 분모에 넣지 않는다.
정확도·읽기·토큰 및 기존 기준의 통과 여부는 [gate.md](gate.md)를 따른다.

원자료 공개본과 package/설치 검증·감사는 [측정 디렉터리](../../results/phase-0.3.17-20261018/)에 있다.
공개 results gzip·meta에서는 개인 home와 로컬 입력/fixture 경로 prefix만 placeholder로 치환했다.
원본 결과·metadata·전체 native evidence는 workflow runtime의 비공개 archive에 보존했다.
원본과 공개본 checksum은 privacy-audit.json에 구분한다. 전체 세션 transcript는 복사하지 않았다.
raw의 판정·usage·fingerprint는 바꾸지 않았고 공개본과 compact record를 각각 재판정해 동일 PASS를 확인했다.

태그·GitHub Release·npm/Homebrew 배포는 수행하지 않았다. 이 기록은 배포 준비 gate이며 배포 완료 증거가 아니다.

재판정(저장소 root, `<before>`는 v0.3.16의 clean archive):

```sh
python evals/phase_eval_compare.py evals/release-gates/v0.3.17 \
  --require-kit . --require-before-kit <before> --require-evaluator .
```
