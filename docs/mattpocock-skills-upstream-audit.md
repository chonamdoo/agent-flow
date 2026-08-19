# mattpocock/skills upstream 대조

`skills/UPSTREAM.md`의 pinned commit 이후 upstream에 들어간 것, upstream에만 있는 skill,
그리고 그중 우리 사본에 반영한 것(4절)을 적는다. pin 이전 기준선은 `skills/UPSTREAM.md`가 정본이다.

- upstream: `https://github.com/mattpocock/skills`
- pinned (대조 당시 우리 기준): `8b36d4fb2635b3c21998dcd8144439c9e5ba7302` (2026-08-05)
- upstream HEAD (대조 시점): `9c9f36ccd3995266cd675468af71639c8dde1ec5` (2026-08-17)
- `skills/` 를 건드린 commit 18개, 그중 우리가 vendored한 8개 skill에 닿는 것 7개 skill / 7개 파일
- pin 이후 upstream에서 **새로 생기거나 사라진 skill은 없다** — `SKILL.md`를 가진 디렉터리가
  양쪽 모두 35개(vendored 8 + upstream 전용 27)로 동일하다. 카테고리 루트의 `README.md` 5개는
  skill이 아니라 세지 않는다

## 1. vendored skill에 들어간 upstream 변경

| 우리 | upstream | 바뀐 파일 | upstream 변경 내용 | 우리 사본 상태 (대조 당시) |
| --- | --- | --- | --- | --- |
| `codebase-design` | `engineering/codebase-design` | `DESIGN-IT-TWICE.md` | "using the Agent tool" 삭제 — subagent 호출을 harness 중립으로 (`14bfbbd`, `c0d6901`) | pin과 바이트 동일 |
| `domain-modeling` | `engineering/domain-modeling` | `SKILL.md` | frontmatter `description` 재작성: trigger를 "discussing codebase terminology / writing or editing a CONTEXT.md / recording or editing an ADR"로 바꾸고 "another skill needs to maintain" 절 삭제 (`bd8e81b`, `e12e7ec`, `54bc6b6`) | pin과 바이트 동일 |
| `tdd` | `engineering/tdd` | `SKILL.md` | 본문의 `` `/codebase-design` skill `` 호출 표현을 `call the Skill tool with "codebase-design"` 로 교체 (`d28dfdc`, `fcf0071`) | 이미 분기 (우리 `requires` + `tests.md` 편집) |
| `grill-with-docs` | `engineering/grill-with-docs` | `SKILL.md` | 본문 한 줄을 `Call the Skill tool twice, for "grilling" and "domain-modeling".` 로 교체 (`d28dfdc`, `fcf0071`, `447ca70`) | 이미 분기 (우리 `requires`) |
| `grilling` | `productivity/grilling` | `SKILL.md` | em-dash 3곳을 콜론/세미콜론으로 (`86cba45`). 알고리즘 변경 없음 | 이미 분기 (이모지 제거) |
| `code-review` | `engineering/code-review` | `SKILL.md` | (a) `run /setup-matt-pocock-skills` 지시를 "tell the user to run" 안내로 완화, (b) `Send a single message with two Agent tool calls. Use the general-purpose subagent for both.` 두 줄 삭제 (`1dab982`, `6a34259`, `14bfbbd`) | 이미 분기 (`## Quick start` 유지 등) |
| `to-prd` | `engineering/to-spec` | `SKILL.md` | `setup-matt-pocock-skills` 안내 문장만 완화 (`1dab982`, `6a34259`) | 이미 분기 (PRD 용어로 재작성) |
| `resolving-merge-conflicts` | `engineering/resolving-merge-conflicts` | — | pin 이후 변경 없음 | 이미 분기 (description 인용부호) |

### 우리 분기 기준 적용 가능성

판정 방법: upstream이 지운 pin 쪽 줄이 우리 사본에 **리터럴로 남아 있는지** 본다. 남아 있으면
그 변경은 우리에게도 닿는 것이고, 없으면 우리가 이미 그 자리를 다시 써서 변경이 무효다.

| 우리 | 판정 | 근거 |
| --- | --- | --- |
| `codebase-design/DESIGN-IT-TWICE.md` | **적용 대상 (1건)** | pin 줄이 우리 사본에 그대로 있고, 바뀐 내용("using the Agent tool" 삭제)은 harness 중립이라 우리 구조와 충돌하지 않는다 |
| `domain-modeling/SKILL.md` | **부분 적용** | pin 줄이 그대로 있어 교체는 되지만, upstream이 지운 `or when another skill needs to maintain the domain model` 절이 우리 구조에서 **살아 있는 trigger**다 — `grill-with-docs`의 `requires`, `workflows/default.yaml:18`, `workflows/full-feature.yaml:18,27-29`가 다른 skill/phase에서 이 skill을 부른다. 전량 교체는 후퇴이고, 새로 붙은 CONTEXT.md/ADR trigger만 취하는 게 맞다 |
| `grilling/SKILL.md` | 표기만 (2/3줄) | em-dash → 콜론 2줄은 적용 가능. 세 번째 줄(sub-agent dispatch)은 우리가 이미 `read-only exploration sub-agent`로 다시 써서 무효 |
| `tdd/SKILL.md` | 적용 안 함 | pin 줄은 남아 있지만 새 문장이 `call the Skill tool with "codebase-design"`이다. 우리 vendored skill은 `BUNDLED_HOST_SKILL_NAMES` 밖이라 host skill picker에 없고, `full-feature.yaml:26-29`는 `.agent-flow/skills/<name>/SKILL.md` **경로**로 resolve하라고 지시한다 — 우리 install에서 새 문장은 거짓이 된다 |
| `grill-with-docs/SKILL.md` | 적용 안 함 | 위와 같은 `Skill tool` 표현 교체 |
| `code-review/SKILL.md` | **무효** | upstream이 고친 두 줄이 우리 사본에 아예 없다 (`setup-matt-pocock`·`issue-tracker.md`·`general-purpose` 문자열 모두 부재). 즉 우리는 이미 그 지시를 빼고 multi-review로 대체했다 |
| `to-prd/SKILL.md` | **무효** | `setup-matt-pocock` 문자열 부재 — 우리 PRD 재작성이 그 줄을 이미 없앴다 |
| `resolving-merge-conflicts` | — | pin 이후 upstream 변경 없음 |

정리: pin 이후 upstream 변경 7건 중 우리 기준으로 반영 대상은 **`codebase-design` 1건 + `domain-modeling` 부분 1건**이고,
`grilling` 2줄은 표기, 나머지 4건은 우리 분기 때문에 이미 무효거나 우리 install에서 틀린 문장이 된다.

## 2. upstream에만 있고 우리가 안 가져온 skill (27개)

카테고리 루트의 `README.md` 5개(`deprecated`, `engineering`, `in-progress`, `misc`,
`productivity`)는 skill이 아니라 제외했다. `skills/deprecated/`에는 README 말고 아무것도 없다.

### engineering (11)

| upstream | description 요지 |
| --- | --- |
| `ask-matt` | repo 내 skill 라우터 — 상황에 맞는 skill/flow를 고른다 |
| `diagnosing-bugs` | 어려운 버그·성능 회귀 진단 루프 ("diagnose"/"debug this" trigger). pin 이후 secret redaction 추가 (`efce423`, `bda79a3`) + `scripts/hitl-loop.template.sh` |
| `implement` | spec 또는 ticket 묶음 기준 구현 |
| `improve-codebase-architecture` | deepening 기회 스캔 → HTML 리포트 → 고른 것 grilling |
| `prototype` | 설계 질문 검증용 throwaway prototype |
| `research` | 1차 출처 조사 후 결과를 repo 안 Markdown으로 남김 |
| `setup-matt-pocock-skills` | issue tracker·triage label·domain doc 배치 초기 설정 (다른 engineering skill의 전제) |
| `to-tickets` | plan/spec을 blocking edge 선언이 있는 tracer-bullet ticket으로 분해 |
| `triage` | issue/외부 PR을 triage 상태 기계로 통과시키고 agent-ready brief 작성 |
| `wayfinder` | 세션 하나에 안 들어가는 큰 작업을 decision ticket 지도로 계획 |
| `wizard` | 사람만 할 수 있는 단계를 안내하는 interactive bash wizard 생성 |

### productivity (6)

| upstream | description 요지 |
| --- | --- |
| `grill-me` | plan/design을 벼리는 인터뷰 (우리 `grilling`과 별개 entrypoint) |
| `handoff` | 현재 대화를 다른 agent가 받을 handoff 문서로 압축 |
| `teach` | workspace 안에서 개념·기술 교육 |
| `to-questionnaire` | 못 정한 결정을 남이 채울 questionnaire로 전환 |
| `wait-what` | 직전 메시지가 전달 실패했을 때 다시 피치 |
| `writing-for-agents` | agent용 문서 작성 — skill 작성/편집, AGENTS.md·CLAUDE.md 수정 |

### in-progress (6)

`claude-handoff`, `loop-me`, `setup-ts-deep-modules`, `writing-beats`, `writing-fragments`, `writing-shape`

### misc (4)

`git-guardrails-claude-code`, `migrate-to-shoehorn`, `scaffold-exercises`, `setup-pre-commit`

## 3. `UPSTREAM.md`에 안 적힌 local 분기

- `tdd/tests.md` — upstream은 pin 이후 변경 없는데 우리 사본이 다르다.
  implementation-detail 예제를 "결함 하나만 보이는" 형태로 다시 씀 (upstream `27,30-34` 대 우리 `27,30-33`).
  `UPSTREAM.md`의 매핑은 `tdd`를 "동일 + 우리 `requires`"로 적고 있어 이 편집이 기록에 없다.
- `grilling/SKILL.md`의 sub-agent dispatch 문장 — 우리는 "find it yourself — read the repo, run the
  tool, or dispatch a read-only exploration sub-agent"로 다시 썼다. `UPSTREAM.md`는 `grilling`
  분기를 "이모지 예외"로만 적고 있어 이 편집이 기록에 없다.
- `agents/openai.yaml` — upstream은 vendored 8개 전부에 두고 우리는 없다. 이건 `UPSTREAM.md`가
  의도적 제외로 기록한 항목이다.

## 4. 이번에 반영한 것

| 대상 | 결정 | 내용 |
| --- | --- | --- |
| `skills/codebase-design/DESIGN-IT-TWICE.md` | 채택 | "using the Agent tool" 삭제 — subagent 호출을 harness 중립으로 |
| `skills/domain-modeling/SKILL.md` | 부분 채택 | upstream의 CONTEXT.md/ADR trigger를 더하고, upstream이 지운 `or when another skill needs to maintain the domain model` 절은 남김 |
| `skills/grilling/SKILL.md` | 미채택 | em-dash → 콜론. 이 저장소는 em-dash를 쓴다 |
| `skills/tdd/SKILL.md`, `skills/grill-with-docs/SKILL.md` | 미채택 | `call the Skill tool with "<name>"` — 우리 install에 없는 호출 경로 |
| `skills/code-review/SKILL.md`, `skills/to-prd/SKILL.md` | 해당 없음 | upstream이 고친 줄이 우리 사본에 없음 |
| `skills/UPSTREAM.md` | 갱신 | pin `8b36d4fb` → `9c9f36cc`, 미기록 분기 2건(`tdd/tests.md`, `grilling` sub-agent 문장) 기록, 위 미채택 근거 기록 |

2절의 upstream 전용 skill 27개는 이번에 가져오지 않았다. 판단 근거가 각기 다르므로 필요할 때
개별로 다룬다.
