[English](../TEAM-ADOPTION.md)

# 팀 도입

이 문서는 "한 사람의 도구"에서 "팀의 도구"로 가는 의도된 경로를 적는다. 지금 저장소가 할 수 있는
것과 아직 없는 것을 나눠 적는다. 로드맵이 아니다 — 날짜도 없고 추정치도 없다. 구현되지 않은 것은
"구현되지 않았다"로 표시한다.

---

## 목표

목표는 팀이 자기 문서·규약·규칙을 넣으면 모든 구성원의 에이전트가 그에 따라 동작하는 것이다.
사람이 채팅에서 같은 규칙을 다시 말하지 않아도 되어야 한다. 한 사람이 공유 규칙을 바꾸면 그 변경이
들어가기 전에 리뷰되고, 다음 run부터 모든 머신이 바뀐 규칙을 따른다.

이것은 목표이고 이미 제공되는 기능이 아니다. 지금은 아래 조각들이 그 일부를 담고 있고, 담지 못하는
부분은 "팀에 없는 것"에 적었다.

---

## 지금 팀 규칙을 담고 있는 것

### 활성 profile

`src/agent_flow/profiles/_schema.yaml`가 profile의 모양을 정의하고, 각
`src/agent_flow/profiles/<id>.yaml`가 그것을 채운다. workflow를 고치지 않고 스택 규칙을 선언할 수
있는 자리는 profile뿐이다. workflow phase는 `profile.gates` 같은 값을 읽기만 하고 스택 중립으로
남기 때문이다.

팀이 선언할 수 있는 것:

- `gates` — 순서 있는 검증 명령. 각 항목이 `id`, `command`, `required`,
  `phase`(`pre-commit | pre-push | post-merge`), `timeout_s`를 갖는다. 분 단위로 도는 gate는
  여기서 자기 상한을 선언한다. timeout은 실패가 아니라 판정 불가로 기록되기 때문이다.
- `review_angles` — 최종 리뷰 phase에 붙는 전문 reviewer. 각 항목이 `id`와 kit root 기준
  `prompt` 경로를 갖는다. `src/agent_flow/profiles/android.yaml`는 `architecture-design`과
  `android-skills`를 선언한다. 기본 run에는 항상 generalist reviewer 하나가 있고 angle은 추가분이다.
- `branching` — `strategy`, `base`, `integration`, `worktree`, `leader_tripwire`, 에이전트가
  만드는 브랜치의 `naming.prefix`·`naming.slug_style`, 그리고 새 worktree에 필요한 gitignored
  머신 설정을 위한 `worktree_setup.copy`.
- `pr` — `target_branch`와 `merge_strategy`.
- `commit_convention` — `style`(`conventional | tagged | freeform`)과
  `co_author`(`include | skip`). `android.yaml`은 `style: tagged`를 쓴다.
- `vocabulary` — 스택 고유 이름으로 바꾸기. workflow YAML은 정본 id(`prd`, `adr`)를 유지하고,
  에이전트가 사용자에게 말할 때만 선언된 단어로 치환한다. `android.yaml`은 `prd: PRD`,
  `flutter.yaml`은 `prd: spec`과 `adr: decision-log`를 매핑한다.
- `execution.reviewers` — reviewer subprocess가 어떤 model/effort로 도는가. `phase`와 `angle`로
  일치시킨다. 이미 배정된 provider를 장식하기만 하고 provider를 고르지는 않는다.

팀이 저장소 안에서 위 목록 전부를 선언할 수는 없다. 저장소는
`.agent-flow/profiles/<profile-id>.local.yaml`로 자기 값을 얹고,
`src/agent_flow/core/profiles.py`의 `PROJECT_OVERRIDE_KEYS`는 정확히 다섯 키만 받는다:
`architecture`, `branching`, `execution`, `gates`, `pr`. 그 밖의 키는 조용히 무시되지 않고
에러로 거부된다 — 반영되지 않는 선언을 삼키지 않기 위해서다. 그래서 `review_angles`,
`commit_convention`, `vocabulary`, `skills`는 저장소별로 설정할 수 없고, 바꾸려면 바뀐 profile을
배포해야 한다. 설치된 `.agent-flow/profiles/<id>.yaml`를 직접 고치는 것은 남지 않는다. install이
새 필드를 기존 설치본에 닿게 하려고 배포 profile을 덮어쓰기 때문이다.

### profile 해석 순서와 `AGENT_FLOW_PROFILE`

`src/agent_flow/core/profile_resolution.py`의 `resolve_profile`이 순서를 고정한다.

1. 환경 변수 `AGENT_FLOW_PROFILE` — 항상 이긴다.
2. `.agent-flow/kit.json:profiles` — filtered installer가 쓴 다중 profile union.
3. `.agent-flow/kit.json:profile` — installer가 쓴 단일 profile.
4. `generic`.

`kit.json`이 가리키는 profile이 디스크에 없으면 성능 저하 모드가 아니라 하드 에러다. 오타 하나가
전체 workflow를 잘못된 스택으로 돌리기 때문이다 — 잘못된 `branching`, 잘못된 `gates`, 잘못된 PR
대상. 조용한 fallback을 원하면 `AGENT_FLOW_FALLBACK_GENERIC=1`로 명시해야 한다.

팀이 얻는 것: profile 집합이 `kit.json`에 커밋되므로 모든 클론이 같은 id로 해석된다. 얻지 못하는
것: 환경 변수 override가 커밋된 값 위에 있고 의도적으로 관대하다. 그래서 `AGENT_FLOW_PROFILE`을
export한 구성원은 다른 규칙 집합으로 돌고, 그 사실은 팀에 보고되지 않는다.

### 설치된 `AGENTS.md` 계약 블록

install은 `bootstrap/AGENTS.md.template`에서 계약을 가져와 프로젝트 루트 `AGENTS.md`의
`<!-- agent-flow:start -->`와 `<!-- agent-flow:end -->` 사이에 쓴다. `CLAUDE.md`는 대신 포인터
블록을 받는다. Claude CLI가 루트 `CLAUDE.md`만 자동 로드하기 때문이다. 그 `@AGENTS.md` import
한 줄이 블록 **밖**의 프로젝트 산문이 Claude와 Claude reviewer에 닿는 유일한 경로다.

블록 안의 두 하위 블록은 installer가 채운다.

- `<!-- agent-flow:skills:start -->` / `<!-- agent-flow:skills:end -->` — skill 인덱스.
- `<!-- agent-flow:docs:start -->` / `<!-- agent-flow:docs:end -->` — 문서 인덱스.
  `lib/installer-shared.mjs`의 `docsIndexBlock`이 `DOCS_INDEX_ROOT`(값은 `docs`)를 훑고 경로만
  싣는다. 크기 상한이 있고 잘라낸 개수를 함께 알린다. 조용히 자르면 목록에 없는 파일이 "없는
  파일"이 되기 때문이다.

그래서 팀이 `docs/`에 자기 마크다운을 두면 모든 에이전트가 로드하는 계약에 그 경로가 이름으로
올라간다. 얻지 못하는 것: 인덱스는 본문이 아니라 경로만 담고, 파일이 추가될 때가 아니라 installer가
돌 때 갱신된다. 팀 산문은 관리 블록 밖에 있어야 한다 — 블록 안은 install이 다시 쓴다. 소유 판정은
`.agent-flow/bootstrap/blocks.json`에 기록된 해시로 하고, installer가 쓰지 않은 블록은 덮지 않고
사용자 편집으로 지켜 보고한다.

### 프로젝트 로컬 skill

`src/agent_flow/core/skill_resolver.py`의 `_DEFAULT_PROJECT_TEMPLATES`가 저장소 쪽 skill root를
순서대로 나열한다.

- `.agent-flow/local-skills/<skill>/SKILL.md` — 개인 drop-box. 여기 둔 문서는 frontmatter 선언이
  없어도 코드 생성·리뷰 phase에 붙는다. 여기 둔 것 자체가 선언이기 때문이다.
- `skills/<skill>/SKILL.md` — 저장소가 소유하고 그래서 이름으로 부를 수 있는 skill.
- `.agent-flow/skills/<skill>/SKILL.md` — 번들 집합.
- `.claude/skills/<skill>/SKILL.md`와 `.agents/skills/<skill>/SKILL.md` — 벤더 설치.
  `skills/`와 구분하는 이유는 이름 소유권이다. 그 이름은 남의 것이다.

profile의 `skills.required_review`는 저장소가 소유한 이름을 차단 요구로 바꾼다. 그룹은 `skills`,
사람이 읽는 `when`, 활성 selector(`task_terms`, `path_globs`, `concerns`), 그리고 run이 출력하는
`missing:` 메시지를 선언한다. selector가 없는 그룹은 활성 근거가 없어 그대로 잠들어 있다.

여기서 팀이 할 수 없는 것: `required_review`는 저장소가 소유한 skill만 이름으로 부를 수 있다.
설치된 외부 skill은 절대 열거하지 않는다 — upstream 이름 변경이 어떤 이름 목록도 낡게 만들기
때문이다. `skills`는 `.local.yaml` override에서 거부된다. 설치 대상을 정하는 쪽은 Python 런타임이
아니라 installer이고, 열면 "선언한 목록"과 "실제 설치된 목록"이 갈려 라우팅이 빈 skill을 가리킬
때까지 보이지 않는다. 그리고 `.agent-flow/local-skills/`는 작업 사본별이다. 팀이 커밋해야 공유된다.

### 외부 `skill_sources`

profile이 `skill_sources`를 선언하고, `src/agent_flow/core/skill_sync.py`의
`parse_skill_sources`가 읽고, `agent-flow skills sync`가 실행한다. 두 종류가 있다.

- `kind: host-managed` — 이미 설치 관리자가 있는 소스. fetch하지 않는다. 경로만 해석하고, 없으면
  선언된 `install_hint`를 install 시점에 1회 출력한다.
  `src/agent_flow/profiles/android.yaml`가 `android-official`과 `chrisbanes`를 이렇게 선언한다.
- `kind: fetch` — 설치 관리자가 없는 순수 git repo. 고정된 `ref`로 머신 공유 캐시
  `~/.agent-flow/skill-sources/<id>/<ref>`에 1회 clone한다(`XDG_STATE_HOME` 또는
  `AGENT_FLOW_SKILL_CACHE`로 위치를 옮긴다). 프로젝트마다도 run마다도 아니다. 저장소 안에는
  아무것도 넣지 않는다. `android.yaml`은 `skydoves-compose-performance`를
  `layout: "*/{skill}/SKILL.md"`로 이렇게 선언한다.

`agent-flow skills sync --refresh`는 캐시를 버리고 다시 받는다. `main` 같은 움직이는 `ref`는
그러지 않으면 머신이 최초로 받은 커밋에 영구히 굳는다 — 보이지 않는 머신별 핀이 된다.
`agent-flow skills sync`는 외부 `skill_sources`만 가져온다. profile과 workflow 자체는 installer를
다시 돌려 갱신한다.

그래서 팀은 선언 하나로 모든 구성원을 같은 외부 문서 집합에 붙일 수 있다. 할 수 없는 것: 그 선언이
배포 profile 안에 있으므로, 자기 소스 목록을 원하는 팀은 고친 profile을 들고 다녀야 한다. 그리고
`ref: main`이면 두 구성원이 서로 다른 커밋에 앉아 있어도 에러도 보고도 없다.

---

## 팀에 없는 것

아래 항목은 모두 `README.md`의 네 가지 미해결 문제에서 나왔다. 어느 것도 구현되지 않았다.

**marker 목록을 누가 소유하고 어떻게 바꾸는가.** phase의 완료 계약은
`src/agent_flow/workflows/<name>.yaml`의 `required_markers`이고,
`src/agent_flow/runner.py`의 `_missing_required_markers`가 artifact에 그 값이 없으면 전진을
막는다. 그 목록에 대한 팀별 소유권이 없고, workflow용 override 파일도 없다.
`PROJECT_OVERRIDE_KEYS`는 profile만 덮고, 설치된 `.agent-flow/workflows/<name>.yaml`은 installer를
다시 돌리면 갱신되므로 손으로 고친 것은 남지 않는다. marker 변경을 제안하고 수용하는 절차는
구현되지 않았다.

**저장소마다 복사되지 않고 버전이 붙어 리뷰되는 공유 규약 팩.** 지금 규약 문서는 저장소마다
`.agent-flow/local-skills/`나 `skills/` 아래로 커밋되거나, 배포 profile에 선언된 `skill_sources`
항목으로 닿는다. kit과 소비하는 저장소 양쪽에서 독립적으로 팀이 소유·버전·리뷰·핀하는 팩은
구현되지 않았다.

**팀이 리뷰 기준에 합의하는 방법.** 기준은 kit root 기준 prompt 경로를 가리키는 `review_angles`
항목이다(예: `templates/_shared/review/architecture-design.md`). `review_angles`는 `.local.yaml`
override가 받는 다섯 키에 없으므로, 팀은 profile을 고쳐 배포하지 않고 angle을 추가하거나 뺄 수 없고,
누가 그것에 합의했는지에 대한 기록도 없다. 합의 절차는 구현되지 않았다.

**구성원별 skill 차이.** `agent-flow skills doctor`와 `agent-flow skills scan`은 실행된 그 머신에서
무엇이 해석되는지 보고한다. 두 머신을 비교하는 것은 없다. `_schema.yaml`이 적은 대로 후보 집합은 이
머신에 마침 설치돼 있는 것이고, 같은 변경을 다른 언어로 서술하면 required 집합이 달라졌다(실측:
영어 6개 / 26,241 B, 한국어 4개) — 그래서 task 문구는 skill을 required로 승격하지 않는다. 두
구성원이 같은 required 집합을 해석하는지 확인하는 팀 단위 점검은 구현되지 않았다.

**병렬 reviewer의 토큰 비용에 팀 규모를 곱한 값.** `execution.reviewers`가 phase·angle별 model과
effort를 고르고, `review_angles` 항목이 하나 늘 때마다 같은 변경에 reviewer subprocess가 하나 더
붙는다. 그 비용을 run 단위로도 팀 단위로도 계산하는 것이 없고, 팀의 언어로 표현된 예산이나 상한도
없다. 비용 계산은 구현되지 않았다.

README의 네 문제 중 앞의 셋은 기술 문제가 아니다. 자동화를 더하는 것은 신뢰를 얻는 것과 같지 않고,
아래 단계 어느 것도 그 사실을 바꾸지 않는다.

---

## 단계 계획

세 단계다. 각 단계는 확장할 기존 메커니즘, 그것을 증명할 가장 작은 관측 결과, 코드가 아니라 사람이
정해야 하는 결정, 그리고 만들지 말고 버려야 하는 조건을 함께 적는다.

### 단계 A — 저장소 하나 밖에 사는 팀 profile

확장 대상: `src/agent_flow/core/profile_resolution.py`의 `resolve_profile`(이미 고정된 순서와
선언된 profile이 없을 때의 하드 에러를 갖고 있다), `.agent-flow/profiles/<id>.local.yaml`
override, 그리고 `src/agent_flow/core/skill_sync.py`의 고정 ref fetch 캐시(이미 `ref`를 머신 공유
캐시로 clone하고 굳은 커밋 sha를 기록한다).

가장 작은 관측 결과: 두 머신의 두 클론이 저장소 밖에 있는 profile 하나를 같은 핀으로 해석해 같은
`gates`와 `pr` 값을 출력하고, 핀을 옮기면 어떤 run이 쓰기 전에 diff로 보인다.

사람이 정할 것: 누가 핀을 옮길 수 있는가, 그리고 옮기는 시점에 이미 돌고 있는 run은 어떻게 되는가.

버릴 조건: 팀이 저장소마다 자기 값을 갖는 편이 맞다고 결론 내릴 때. `.local.yaml` override가 이미
`architecture`, `branching`, `execution`, `gates`, `pr`를 저장소별로 담으므로, 만들지 않아도 잃는
것이 없다.

### 단계 B — 리뷰 담당자가 있는 버전 붙은 skill source로서의 팀 규약 팩

확장 대상: `kind: fetch`·`ref`·`layout`을 쓰는 `skill_sources`
(`src/agent_flow/core/skill_sync.py`), 그리고 선언된 `missing:` 메시지로 이미 phase를 막는
`skills.required_review` 그룹.

가장 작은 관측 결과: 팩이 없으면 리뷰 phase가 그룹의 `missing:` 메시지로 막히고, run 기록에 팩의
커밋 sha가 이름으로 남아 두 run이 어느 팩 버전을 봤는지 비교된다.

사람이 정할 것: 팩 변경을 누가 리뷰하는가, 그리고 팩이 기준을 offered가 아니라 required로 만들 수
있는가. 스키마는 승격을 task 문구 밖에 두기로 정해 뒀으므로, 승격은 어느 쪽이든 사람의 결정이다.

버릴 조건: 팩이 결국 저장소마다 하나로 드러날 때. `.agent-flow/local-skills/`가 이미 그것을 담고,
아무도 소비하지 않는 팩에 버전을 붙이는 것보다 그 디렉터리를 커밋하는 것이 싸다.

### 단계 C — 합의된 검증 계약

확장 대상: `src/agent_flow/workflows/<name>.yaml`의 `required_markers`와
`src/agent_flow/runner.py`의 차단 판정.

가장 작은 관측 결과: 실효 marker 목록이 합의된 목록과 다른데 수용 기록이 없으면 run이 시작을
거부하고, 수용된 변경은 이름이 붙은 diff로 읽힌다.

사람이 정할 것: 누가 어떤 정족수로 `required_markers` 목록을 바꿀 수 있는가, 그리고 구성원이 단일
run에 대해 marker를 느슨하게 할 수 있는가 — 할 수 있다면 그 사실이 나중에 어디에 보이는가.

버릴 조건: 대화로 목록이 바뀔 만큼 팀이 작을 때. 한 사람이 이미 목록을 소유하고 있다면 시작 거부
메커니즘은 없애는 마찰보다 더 많은 마찰을 만든다.

---

## 열린 질문

둘 다 답을 주장하지 않는다.

**프로세스가 만드는 마찰과 그것이 사는 신뢰의 손익분기점은 어디인가?** gate 개수는 혼자면 견딜 수
있다. 팀에서는 얼마의 대기가 얼마의 확신만큼 값하는가, 그리고 프로세스가 돌려주는 것보다 더 많이
비용을 쓰는 지점을 넘었다는 것을 팀은 무엇을 보고 알 수 있는가? 새 구성원에게, 또는 여섯 달 뒤 같은
팀에게 답이 달라졌다는 신호는 무엇인가?

**팀 규모에서 병렬 reviewer 서브에이전트의 경제성은 어떤가?** reviewer를 병렬로 돌리면 토큰 비용이
angle 수만큼 곱해지고, 그것을 돌리는 사람 수만큼 다시 곱해진다. 독립된 두 리뷰 중 실제로 값을 하는
것은 어느 쪽인가? 단위는 run인가 사람인가 변경인가, 그리고 답이 "너무 비싸다"일 때 그 숫자를 보는
사람은 누구인가?
