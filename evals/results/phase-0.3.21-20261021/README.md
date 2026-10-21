# v0.3.21 release verification

후보 `c1d9dfc9b991eafd975b0860713d9625c32e28f9`가 v0.3.20(`207a54cdb878c4d7352279a750c2a1e14bbc1f12`)과의 paired
release gate를 이번 릴리스에 한한 비캐시 입력 한도 1.06으로 통과했다. 후보는 #394 라우팅 어휘, #387·#403 평가 사례,
#397·#400·#404·#405 실전 UI 과제, #406 runner 결함 수정(F13·F1·F9)을 포함한다. 측정 중 kit·평가기·case·한도와
runtime은 바꾸지 않았다.

Codex CLI 0.163.0에 두 역할 모두 `gpt-6.1-sol`을 요청했고 author 3회·review 2회·동시성 12로 실행했다. 168개 고유
unit(author 72, review 96)과 native 호출 616개를 보존했다. 측정은 2026-10-21 03:52 UTC에 시작해 04:38 UTC에 끝났다.

| 지표 | before | after |
|---|---:|---:|
| author 필수 읽기·behavior/plan/norm 통과율 | 1.000 | 1.000 |
| 리뷰 정답(채점 44개) | 42/44 | 44/44 |
| 결함 탐지 | 1.000 | 1.000 |
| clean-verdict 오탐률 | 0.017 | 0.000 |
| 무효 unit | 0 | 0 |
| author 비캐시 입력·출력 비율 | | 1.052 · 1.017 |
| review 비캐시 입력·출력 비율 | | 0.996 · 0.993 |

author 비캐시 입력 비율 1.052는 기본 한도 1.05를 넘는다. 유지보수자가 이번 릴리스에 한해 한도를 1.06으로 정했다.
근거와 첫 실행(1.069, [run-1-gate.md](run-1-gate.md))은 [게이트 기록](../../release-gates/v0.3.21/README.md)에 있다.
kit별 expected=null 4개(app/team/clean·backend/team/clean 각 2개)는 정확도 분모 밖에 둔다.

## 파일

| 파일 | 내용 |
|---|---|
| [gate.md](gate.md) | 1.06 한도로 다시 계산한 판정(release-gates 기록과 같다) |
| [run-1-gate.md](run-1-gate.md) | 첫 실행의 판정. 기본 한도 1.05에서 실패했고 재채점하지 않았다 |
| `meta.json`, `results.jsonl.gz` | export된 compact 기록 사본 |
| [integrity-audit.json](integrity-audit.json) | 616개 command receipt의 argv·cwd·종료 코드와 prompt/stdout/stderr SHA-256, 관측 usage를 원자료와 대조. compact 행이 원본 측정값·판정을 그대로 담는지 확인. 두 kit 지문과 평가기 지문을 트리에서 다시 계산 |
| [privacy-audit.json](privacy-audit.json) | 공개 파일에 리뷰 원문·로컬 경로·자격 증명·이메일이 없는지 검사 |
| [package-verification.json](package-verification.json) | 릴리스 브랜치 `git archive`로 npm·wheel·sdist 재빌드, sdist→wheel 재현, 설치 CLI 버전 0.3.21, Next.js installer clean/local/pending 신규·재설치, 설치된 kit 지문 = 게이트 after kit 지문 |

원본 native evidence(prompt·리뷰 원문)는 저장소에 넣지 않는다. 로컬 archive sha256은
`c6f696d054bc0cc0c526b4412606c26e31df0f5796db62ec28768f919119457c`다. 실제 model identity는 관측하지 않았고
요청 flag만 확인했다.
