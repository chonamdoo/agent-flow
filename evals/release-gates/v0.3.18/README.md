# v0.3.18 paired-model release gate

이 기록은 v0.3.17 (`1414b2a7858a971f665a0b877bdc899df8cf20ca`)과 #373까지 merge된
v0.3.18 후보 (`02164aa7318d3de7a2bcdafda9418a4d822b2f2b`)를 같은 실행에서 비교한 통과 기록이다.
두 kit은 `git archive`로 만든 독립 snapshot이다. 평가기·case·threshold는 바꾸지 않았다.

Codex CLI `0.163.0`에 `gpt-6.1-sol`을 요청했다. author 3회·review 2회·동시성 8로
4 stack × 3 mode(clean/local/team) × 2 kit의 168개 고유 unit(author 72, review 96),
616개 native 호출을 실행했다. 서버 stream은 관측 model identity를 제공하지 않았다.
요청 flag는 616개 호출 모두 확인했으며, 관측 model은 unknown으로 남긴다.
단일 provider의 릴리스 평가는 workflow의 독립 Claude/Codex 리뷰를 대신하지 않는다.

[gate.md](gate.md)의 모든 기준을 통과했다. author 비캐시 입력·출력 토큰 비율은
`0.983`·`0.982`, review 비율은 `1.006`·`0.995`다. 토큰 비율은 양쪽 kit에서 유효한
쌍만 비교한다. 전체 호출에서 관측한 사용량 합계와 혼동하지 않는다.

리뷰어 13개 호출이 1500초 timeout으로 끝나 before 1개·after 2개 review unit이 무효다.
무효 시행을 지우거나 재시도하지 않았다. 유효 review 정확도는 before `42/43`, after `42/42`다.
timeout 13개 중 12개는 후보 kit의 web/clean·web/local defect 첫 시행에서 났다(각 리뷰어 6명).
나머지 1개는 before web/clean/clean 첫 시행이다. 후보 결함 탐지는 24개 defect unit 중 유효한
22개로 측정했다. 무효 시행과 그 짝은 review 토큰 비율에서 빠지므로 timeout 비용을 포함한
총비용 개선으로 해석할 수 없다. timeout 호출의 미관측 사용량은 추정하지 않는다.
`expected=null` 8개는 정확도·오탐 분모에서 제외한다. author architecture-lint는 kit별
pass 12개·n/a 24개이며 n/a를 pass로 세지 않는다.

[측정 디렉터리](../../results/phase-0.3.18-20261019/final-kit-02164aa/)에 compact 결과,
무결성 감사와 package 검증을 보존했다. 616개 command receipt의 prompt/stdout/stderr SHA-256,
종료 코드·sandbox·모델 요청 flag·관측 usage를 원자료와 대조했다. compact 행은 원본의 판정과
측정값을 그대로 담는다. 공개 기록에는 로컬 경로와 리뷰 원문을 넣지 않았다.
원본 native evidence는 비공개 Git runtime archive에 보존했다.

이전 두 실패 측정은 [기존 기록](../../results/phase-0.3.18-20261019/)에 그대로 남아 있다.
이번 결과는 #371–#373 이후 다른 kit의 새 전체 측정이며, 이전 실패의 재채점이나 무변경 재실행이 아니다.
모든 기준 통과가 이전 kit의 토큰 증가 원인을 규명했다는 뜻은 아니다.

npm tarball·wheel·sdist 빌드와 두 installer의 clean/local/pending 신규 설치·재설치를 검증했다.
사용자 파일 보존, Claude/Codex/OMP hook 등록 파일, 설치 launcher의 workflow export,
wheel CLI 실행을 확인했다. 실제 host trust/startup과 Homebrew 업그레이드는 검증하지 않았다.

태그·GitHub Release·배포는 하지 않았다. 이 기록이 포함된 PR을 merge한 뒤 그 main 커밋에
`v0.3.18` 태그를 붙이면 release workflow가 기록과 두 kit의 지문을 다시 검증한다.

재판정(저장소 root, `<before>`는 v0.3.17의 clean archive):

```sh
python evals/phase_eval_compare.py evals/release-gates/v0.3.18 \
  --require-kit . --require-before-kit <before> --require-evaluator .
```
