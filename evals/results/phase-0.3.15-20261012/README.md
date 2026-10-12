# v0.3.15 실제 모델 릴리스 평가

2026-10-12(Asia/Seoul)에 이전 v0.3.14와 새 후보 kit을 같은 실행에서 측정했다.
배포에 사용한 기록은 [#358](https://github.com/chonamdoo/agent-flow/pull/358)로
머지됐으며, [gate.md](gate.md)의 기존 threshold 판정은 PASS다.
[v0.3.15](https://github.com/chonamdoo/agent-flow/releases/tag/v0.3.15)는 배포 완료됐다.

## 측정 입력과 실행

- before: `v0.3.14`가 가리키는 커밋 `4221038fa84318f661747b3634960885169f8215`의 clean Git archive.
- after: `0ffba881a0c3f1c20f41359f205ef4e517ed5482`의 clean Git archive.
- 평가기와 437개 case 파일은 후보의 동일한 버전을 사용했다. kit·evaluator·case 지문은 [meta.json](meta.json)에 있다.
- Codex CLI `0.162.1`, 요청 모델 `gpt-6.1-sol`, author/review 모두 codex-only.
- web/rn/app/backend × clean/local/team × before/after, author 조합당 3회, review clean/defect 조합당 2회, concurrency 8, `--keep`: 총 168행(author 72, review 96).
- 측정은 2026-10-12 07:46:54~08:55:05 KST에 실행했다. 실제 native CLI 616호출의 요청 모델 인자와 원본 usage를 검증했다. 스트림에 모델 식별자가 보고되지 않아 관측 모델을 요청 모델로 채우지 않았다.

아래는 실제 실행 경로만 일반화한 명령이다. `before-kit`·`after-kit`은 clean Git
archive이며 원본 checkout과 결과 `out`은 별도 경로를 사용한다. 각 kit에는 해당
버전 wheel의 검증용 Python 환경을 준비했다. 후보 archive 루트에서 실행하며,
toolchain과 CLI 인증 준비는 [평가 안내](../../README.md)를 따른다.

```sh
python evals/phase_eval.py \
  --kit before=/path/to/before-kit --kit after=/path/to/after-kit \
  --scenario author,review --trials 3 --review-trials 2 --concurrency 8 \
  --author-provider codex --review-providers codex --codex-model gpt-6.1-sol \
  --output /path/to/out --keep
python evals/phase_eval_compare.py /path/to/out --summary /path/to/out/gate.md \
  --require-kit /path/to/after-kit --require-before-kit /path/to/before-kit --require-evaluator . \
  --export evals/release-gates/v0.3.15
```

## 결과와 판정 범위

- 두 kit 모두 84/84행 유효하며 provider 오류와 무효 행은 없다. author는 각 kit 36/36행에서 behavior/plan/norm과 필수 읽기를 통과했다.
- review는 각 kit 48행 완료·유효, 정확도 채점 44행이다. before 정답은 43/44, after는 44/44이며 결함 탐지율은 모두 1.000이다. before의 `web/clean/clean/trial1` 오탐 1건은 결과에 보존했다.
- 각 kit의 `app/team/clean` 2행과 `backend/team/clean` 2행은 pending 정책상 `expected=null`이다. 완료·토큰·읽기 측정에는 포함하고 정확도와 clean 오탐 분모에서는 제외한다.
- architecture-lint는 각 kit에서 적용된 12행 PASS, 24행 n/a다. n/a를 PASS로 세지 않는다.
- after/before 토큰 비율은 author 비캐시 입력 0.954·출력 0.955, review 비캐시 입력 0.957·출력 0.987이다. 상세 지표와 분모는 [gate.md](gate.md)를 따른다.

## 증거와 배포 확인

- 통과 export는 [evals/release-gates/v0.3.15](../../release-gates/v0.3.15/)다. [integrity-audit.json](integrity-audit.json)에 168개 고유 행, 616호출, 원본 SHA256·usage 대조와 관측 모델의 한계를 기록했다.
- 원본 168행·CLI 로그·체크섬은 클라우드 작업 공간에 보존했다. 정리한 설치 fixture의 소스·결과는 별도 체크섬을 가진 압축 archive로 보관하고, 배포 테스트 환경과 공유 의존성 cache는 유지했다.
- #357의 별도 측정 결과나 과거 #346의 실패를 이 기록과 섞지 않았다. 이 README는 #358의 배포 기록을 설명하며 meta/results/gate/audit를 수정하지 않는다.
- 태그 커밋 `075a79a74a330b99059019f6317fa3131f832927`의 clean archive에서 지문과 게이트를 다시 판정했다. [Release workflow](https://github.com/chonamdoo/agent-flow/actions/runs/38187536598)가 이를 확인한 뒤 Release와 formula를 게시했다.
- [macOS ARM Homebrew 검증](https://github.com/chonamdoo/agent-flow/actions/runs/38187699156)에서 v0.3.14 → v0.3.15 업그레이드·`brew test`·새 프로젝트 설치가 통과했다. launcher 버전은 `agent-flow 0.3.15`, 인터프리터는 `/opt/homebrew/opt/agent-flow/libexec/.venv/bin/python`이다. hook은 같은 인터프리터인지 `samefile`로 확인했다. 이 검증은 GitHub runner에서 실행했으며 사용자의 개인 Mac을 변경한 것은 아니다.
