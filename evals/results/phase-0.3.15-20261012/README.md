# v0.3.15 실제 모델 릴리즈 평가

2026-10-12(Asia/Seoul)에 최신 `origin/main`의 kit과 이전 릴리즈를 같은 실행에서 측정했다. 기존 threshold로 판정한 결과는 [gate.md](gate.md)이고, 통과 기록은 `evals/release-gates/v0.3.15/`다.

## 측정 입력

- 이전 릴리즈: `v0.3.14`, `3ed2c6b559a814692e6f9dd17e665312e61e4848`의 clean Git archive.
- 후보 runtime·skill 입력: `origin/main` `0ffba881a0c3f1c20f41359f205ef4e517ed5482`의 clean Git archive. 릴리즈 준비 PR의 평가 시 HEAD는 `8ae0b2c541adc08cfd1f40ad5a2d00fca20d3c1a`이며 릴리즈 노트와 canonical MD 보충은 kit/evaluator 지문 밖에 있다.
- 두 archive에서 npm 패키지와 정확한 버전별 wheel을 준비했다. 각 kit의 검증용 `.venv`에는 해당 wheel을 설치했고 모델 평가 fixture는 각 kit의 설치 진입점으로 생성했다. `.venv`와 이 결과 디렉터리는 kit 지문에 포함되지 않는다.
- Codex CLI `0.159.0-alpha.3`, 요청 모델 `gpt-6.1-sol`, author/review 모두 codex-only. Python `3.12.14`, Flutter `3.47.7`, Dart `3.13.5`를 사용했다. 사용자 device login 뒤 실제 요청으로 인증을 확인했다.
- 같은 최신 evaluator와 case 437파일: web/rn/app/backend × clean/local/team × before/after. author 조합당 3회, review clean/defect 조합당 2회, concurrency 10, 총 168행.
- kit·evaluator·case 지문은 `meta.json`에 기록했다. 평가 종료 후 다시 fetch한 `origin/main`도 `0ffba88`이었다. 이전 #346의 실패와 다른 소스의 결과를 합치거나 다시 표기하지 않았다.

실제 실행의 경로만 일반화한 명령은 다음과 같다. `before-kit`과 `after-kit`은 위 archive와 버전별 wheel 환경으로 준비한 별도 입력이다.

```sh
python after-kit/evals/phase_eval.py \
  --kit before=before-kit --kit after=after-kit \
  --scenario author,review --author-provider codex --review-providers codex \
  --codex-model gpt-6.1-sol --trials 3 --review-trials 2 --concurrency 10 --keep \
  --output work/phase-0.3.15-20261012
python evals/phase_eval_compare.py work/phase-0.3.15-20261012 \
  --require-kit after-kit --require-before-kit before-kit --require-evaluator after-kit \
  --summary work/phase-0.3.15-20261012/gate.md --export evals/release-gates/v0.3.15
```

## 유효 범위와 보존

- author: 두 kit 각각 36행 모두 유효하고 behavior/plan/norm·필수 읽기 검사를 통과했다. 각 kit의 architecture-lint는 적용된 12행 모두 pass이며 나머지 24행은 n/a다.
- review: before 48행 중 46행, after 48행 모두 유효하다. 정확도 분모는 before 42행(41개 정답), after 44행(44개 정답)이다. 각 kit의 app/team/clean 2행과 backend/team/clean 2행은 pending 정책상 정답을 고정할 수 없어 expected=null이며, 완료·토큰·읽기 측정에는 포함하고 정확도·clean 오탐 분모에서는 제외한다.
- before의 `review/backend/local/clean/trial1`과 `review/app/team/defect/trial2`는 fixture installer가 600초를 초과했다. 두 무효 행을 삭제하거나 재실행하지 않았고 export에도 error=true로 남겼다. before 무효율 2/84, after 0/84를 기존 ceiling 0.10으로 판정했다. 후보에서 새 모델 실패 사례는 관측되지 않았다.
- 디스크 공간 확보를 위해 결과가 이미 기록된 리뷰 fixture 48개의 재생성 가능한 node_modules만 정리했다. 소스 변경·결과·모델 evidence·공유 의존성 cache·진행 중 fixture는 보존했고 측정 입력이나 evaluator는 변경하지 않았다.
- 공개한 meta/results는 comparator의 통과 export와 byte가 같다. comparator가 경로·argv·리뷰 본문을 제외한 측정 필드만 내보냈으며, 원본 168행과 모델 실행 증거는 클라우드 작업 공간에 보존했다.
- 이 평가는 릴리즈 게이트 증거다. 독립 Claude/Codex 두 reviewer의 공식 workflow 승인이나 macOS Homebrew·native trust 실측을 대신하지 않는다. 태그와 GitHub Release는 게시하지 않았다.
