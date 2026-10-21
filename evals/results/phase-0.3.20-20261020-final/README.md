# v0.3.20 release verification

후보 `388c1638c498971dcc1fb99b746068b7adb25186`이 v0.3.19
(`af4205df215e696dfcced9110d783fecf44372f0`)와의 paired release gate를 통과했다. 후보는
#386 계획 결정 대조, #396 Next.js auth 리뷰 라우팅 정밀화, #393 heldout 라우팅 사례,
#401 리뷰어 필수 본문 1회 읽기와 v0.3.20 metadata를 포함한다. 측정 중 평가기·case·threshold와
runtime은 바꾸지 않았다.

Codex CLI 0.163.0에 두 역할 모두 `gpt-6.1-sol`을 요청했고 author 3회·review 2회·동시성 12로
실행했다. 168개 고유 unit을 모두 보존했다. 4 stack × 3 mode × 2 kit snapshot에서 author 72,
review 96이다. 측정은 2026-10-20 23:12 UTC(Asia/Seoul 2026-10-21)에 시작했다. 디렉터리
이름은 같은 릴리스의 기존 기록 날짜(20261020)를 따르며, 원본 timestamp는 UTC다.

모든 threshold를 통과했다. author 필수 읽기와 behavior/plan/norm 통과율은 두 kit 모두
1.000이다. 채점된 review 정확도는 before 42/44, after 44/44이고 결함 탐지는 두 kit 모두
1.000이다. before의 오탐 두 건은 web clean/local 정상 diff의 두 번째 시행에서 났으며,
clean-verdict 오탐률은 before 0.017(2/120), after 0.000(0/120)이다. 무효 unit은 없다.
kit별 expected=null 4개(app/team/clean·backend/team/clean 각 2개)는 정확도 분모 밖에 둔다.
architecture-lint는 kit별 author 결과 pass 12개·n/a 24개이며 n/a는 pass가 아니다.

author 비캐시 입력·출력 비율은 1.005/1.033, review 비율은 0.924/1.087이다. 두 kit에서 모두
유효한 쌍만 기여한다. 누락된 model usage를 추정하지 않았다. 계획된 616개 호출을 모두 실행했고,
모두 종료 코드 0으로 끝나며 usage를 제공했다. provider 오류 event는 없다.

[integrity-audit.json](integrity-audit.json)은 모든 native command receipt, prompt/stdout/stderr
SHA-256과 관측 usage를 원본 행과 대조하고, 공개한 compact 행이 원본 측정값을 그대로 담는지
확인한다. before/after 트리의 kit 지문과 평가기 지문도 export meta와 다시 대조했다. 모델 요청
flag는 확인했지만 관측 model identity는 추론하지 않는다. native evidence는 저장소에 넣지 않고
측정 환경의 로컬 archive SHA-256만 감사에 기록했다. 공개 기록에는 native 원문과 로컬 경로 prefix가 없다
([privacy-audit.json](privacy-audit.json)). 완료된 성공 scratch project에서만 ignored 의존성과
Next 빌드 캐시를 디스크 확보를 위해 지웠으며, project 소스·Git 기록·native evidence는 유지했다.

[package-verification.json](package-verification.json)은 측정한 후보 `388c163`의 `git archive`에서
다시 만든 npm tarball·wheel·sdist의 SHA-256과 진입 skill, sdist에서 다시 만든 wheel의 동일성,
설치한 wheel CLI 버전, node installer의 Next.js clean/local/pending 신규 설치·재설치 종료 코드를
기록한다. local 모드는 web case overlay의 `skills/architecture/SKILL.md`를 지정했으며, 지정하지
않으면 installer가 설계대로 거부한다. 설치된 kit 지문은 gate after kit 지문과 같다. 실제 host
trust/startup과 Homebrew 업그레이드는 검증하지 않았다. 전체 test suite와 phase-budget 검사는
PR CI가 맡는다.

이 통과는 세 번째 전체 실행이다. 첫 실행(main `a87cd08`)은 container에 Dart/Flutter가 없어
app unit 18개가 모델 호출 전에 실패한 채 59개 unit 이후 중단했다. Flutter 설치 후 두 번째
실행은 168/168을 완료하고 품질 지표가 모두 before 이상이었지만 review 비캐시 입력 비율 1.108이
한도 1.05를 넘었다. #396과 #401로 auth 라우팅 오탐과 리뷰어 재읽기를 줄였고, #385는
maintainer 결정으로 유지했다(#398 닫음). 실패한 두 실행은 이 기록에 포함하지 않았으며 재채점이나
선택 재시도도 하지 않았다. 이 기록은 릴리스 준비를 입증하며, 태그 생성과 배포는 별도 작업이다.
