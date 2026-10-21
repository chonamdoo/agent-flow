# v0.3.20 paired-model release gate

v0.3.19 (`af4205df215e696dfcced9110d783fecf44372f0`)와 #401까지 merge된
v0.3.20 후보 main (`388c1638c498971dcc1fb99b746068b7adb25186`)를 같은 실행에서 비교했다.
두 kit은 `git archive`로 만든 추적 파일의 독립 snapshot이며 평가기·case·threshold는 변경하지 않았다.

Codex CLI `0.163.0`에 `gpt-6.1-sol`을 요청했다. author 3회·review 2회·동시성 12로
4 stack × 3 mode(clean/local/team) × 2 kit의 168개 고유 unit(author 72, review 96)을
측정했다. 실제 native 호출은 616개(author 72, 리뷰어 544)이며 모두 종료 코드 0이다.
616개 모두 요청 모델 flag를 확인했지만 실제 model identity는 관측하지 못했다.
provider 오류 event와 무효 unit은 없다. 단일 provider 평가는 workflow의 독립 Claude/Codex
리뷰를 대신하지 않는다.

[gate.md](gate.md)의 모든 기준을 통과했다. author 비캐시 입력·출력 토큰 비율은
`1.005`·`1.033`, review 비율은 `0.924`·`1.087`이다(한도 `1.05`·`1.10`). 두 kit에서 모두
유효한 쌍만 비교한 비율이며, 전체 native 호출의 총사용량과 혼동하지 않는다.

유효 review 정확도는 before `42/44`(0.955), after `44/44`(1.000)이다. before의 두 오탐은
web clean/local 정상 diff의 두 번째 시행이며, clean-verdict 오탐률은 before `2/120`(0.017),
after `0/120`(0.000)이다. 결함 탐지는 두 kit 모두 24개 defect unit에서 1.000이다.
`expected=null` 4개씩(app/team/clean·backend/team/clean 각 2개)은 pending 기본 설치라
approve가 고정 정답이 아니므로 정확도·오탐 분모에서 제외하고, 완료·토큰·읽기 측정에는 남긴다.
author 필수 읽기·behavior/plan/norm 통과율은 두 kit 모두 1.000이다. author architecture-lint는
kit별 pass 12개·n/a 24개이며 n/a를 pass로 세지 않는다.

이번 통과 기록은 세 번째 전체 실행이다. 첫 실행(main `a87cd08`)은 container에 Dart/Flutter가
없어 app unit 18개가 모델 호출 전 `evaluation tool unavailable: dart`로 실패한 상태에서
59개 unit 이후 중단했다. Flutter를 설치한 뒤 같은 kit로 다시 실행한 두 번째 실행은 168/168을
완료하고 모든 품질 지표가 before 이상(review 정확도 0.977→1.000)이었지만 review 비캐시 입력
비율 `1.108`이 한도 `1.05`를 넘어 실패했다. 증가분은 #385가 리뷰어 필수 읽기에 더한 본문,
Next.js auth 리뷰 skill이 "signed-in" 표현만으로 web 리뷰어에 라우팅된 오탐, 그리고 리뷰어가
같은 필수 본문을 여러 번 다시 읽은 중복에서 왔다. #396이 auth 라우팅을 보호 표현으로 좁혔고,
#401이 리뷰어가 필수 본문을 한 번만 읽고 중복 절대경로를 빼게 했다. #385는 maintainer 결정으로
유지했다(되돌림 PR #398은 닫음). 두 실패 실행은 재채점하거나 선택 재시도하지 않았고 이 기록에 섞지 않았다.
threshold 예외나 평가기 변경도 없었다. 이보다 앞선 후보 `f7fc39b`의 HTTP 429 중단 기록은
[기존 기록](../../results/phase-0.3.20-20261020/)에 그대로 남아 있다.

[측정 기록](../../results/phase-0.3.20-20261020-final/)에 compact 결과, 무결성 감사와 package
검증을 보존했다. 616개 command receipt의 prompt/stdout/stderr SHA-256, 종료 코드,
sandbox·모델 요청 flag와 관측 usage를 원자료와 대조했다. compact 행은 원본의 측정값과
판정을 그대로 담으며 로컬 경로와 리뷰 원문은 공개하지 않는다. 원본 native evidence(prompt·리뷰 원문)는
저장소에 넣지 않는다. 측정 환경의 로컬 archive sha256은 `1c35b68bb9ff144ed12cd91e8765bc440d73bac84ac55e66504fc1ab525b4971`이다. 완료된 fixture의 ignored node_modules·Next 빌드 캐시만 디스크
확보를 위해 정리했으며, 소스·Git 기록·모델 증거·측정값은 유지했다.

측정한 후보 `388c163`의 `git archive`로 npm tarball·wheel·sdist를 다시 빌드하고, sdist에서
만든 wheel의 진입 skill, 설치한 wheel CLI 버전, node installer의 Next.js clean/local/pending
신규 설치·재설치를 검증했다. 설치된 kit 지문은 gate의 after kit 지문과 같다. 실제 host
trust/startup과 Homebrew 업그레이드는 검증하지 않았다.

이 기록은 릴리스 준비 증거다. 이 기록을 포함한 PR을 merge한 뒤 main의 해당 커밋에
`v0.3.20` 태그를 붙이면 release workflow가 기록과 두 kit의 지문을 다시 검증한다.

재판정(저장소 root, `<before>`는 v0.3.19의 clean archive):

```sh
python evals/phase_eval_compare.py evals/release-gates/v0.3.20 \
  --require-kit . --require-before-kit <before> --require-evaluator .
```
