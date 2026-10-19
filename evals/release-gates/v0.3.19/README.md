# v0.3.19 paired-model release gate

v0.3.18 (`8d94016442e9456e5875d0c558d26b13ccf21ebe`)과 #375 이후의
v0.3.19 후보 (`4da1c7c012dd37465850dbeb28c99e64682f2243`)를 같은 실행에서 비교했다.
두 kit은 추적 파일의 독립 snapshot이며 평가기·case·threshold는 변경하지 않았다.

Codex CLI `0.159.2`에 `gpt-6.1-sol`을 요청했다. author 3회·review 2회·동시성 8로
4 stack × 3 mode(clean/local/team) × 2 kit의 168개 고유 unit(author 72, review 96)을
측정했다. 실제 native 호출은 610개이며 모두 종료 코드 0이다. 610개 모두 요청 모델 flag를
확인했지만 실제 model identity는 관측하지 못했다. 단일 provider 평가는 workflow의
독립 Claude/Codex 리뷰를 대신하지 않는다.

[gate.md](gate.md)의 모든 기준을 통과했다. author 비캐시 입력·출력 토큰 비율은
`0.975`·`0.940`, review 비율은 `1.009`·`1.014`다. 두 kit에서 모두 유효한 쌍만 비교한
비율이며, 전체 native 호출의 총사용량과 혼동하지 않는다.

후보의 app/clean/defect 첫 review 시행은 모델 호출 전 설치가 600초 한도를 넘어 무효다.
이 시행을 지우거나 별도로 재시도하지 않았다. 이 unit의 리뷰어 6개 호출이 실행되지 않아
예정된 616개 중 610개를 실행했다. 후보의 결함 탐지는 24개 defect unit 중 유효한 23개로
측정했으며, 그 무효 unit과 짝은 review 토큰 비율에서 제외한다. 유효 review 정확도는
before `42/44`, after `43/43`이다. before의 두 오탐은 web clean/local 정상 diff의
두 번째 시행이다. `expected=null` 4개씩은 정확도·오탐 분모에서 제외한다.
author architecture-lint는 kit별 pass 12개·n/a 24개이며 n/a를 pass로 세지 않는다.

[측정 기록](../../results/phase-0.3.19-20261020/)에 compact 결과, 무결성 감사와 package
검증을 보존했다. 610개 command receipt의 prompt/stdout/stderr SHA-256, 종료 코드,
sandbox·모델 요청 flag와 관측 usage를 원자료와 대조했다. compact 행은 원본의 측정값과
판정을 그대로 담으며 로컬 경로와 리뷰 원문은 공개하지 않는다. 원본 native evidence는
비공개 Git runtime archive에 보존했다. 완료된 fixture의 ignored node_modules·Next 빌드
캐시만 디스크 확보를 위해 정리했으며, 소스·Git 기록·모델 증거·측정값은 유지했다.

후보 준비 때 npm tarball·wheel·sdist와 두 installer의 clean/local/pending 신규 설치·재설치를
검증했다. 이번에는 같은 후보·평가기 지문과 세 산출물 SHA-256 일치를 재확인하고 설치 fixture의
parity를 다시 검사했다. 설치 행렬 전체를 다시 실행한 것으로 해석하지 않는다. 실제 host
trust/startup과 Homebrew 업그레이드는 검증하지 않았다.

이 기록은 릴리스 준비 증거다. 이 기록을 포함한 PR을 merge한 뒤 main의 해당 커밋에
`v0.3.19` 태그를 붙이면 release workflow가 기록과 두 kit의 지문을 다시 검증한다.

재판정(저장소 root, `<before>`는 v0.3.18의 clean archive):

```sh
python evals/phase_eval_compare.py evals/release-gates/v0.3.19 \
  --require-kit . --require-before-kit <before> --require-evaluator .
```
