# Vendored skill provenance

여기 있는 skill 중 일부는 [mattpocock/skills](https://github.com/mattpocock/skills)에서 가져와
우리 구조에 맞게 고친 vendored 사본이다. 이 파일은 **어느 시점의 upstream과 맞춰 뒀는지**를 적는
자리다. 이 기록이 없으면 다음 동기화가 매번 전량 diff가 된다 — 기록이 있으면
`git -C <clone> diff <pinned>..HEAD -- skills/` 범위 diff로 줄어든다.

- upstream: `https://github.com/mattpocock/skills`
- pinned commit: `9c9f36ccd3995266cd675468af71639c8dde1ec5` (2026-08-17)
- `diagnosing-bugs` adoption source: `0ab1b63a410a03d3627979a109c8695de27af954` (2026-08-20)

pin을 올릴 때는 채택한 것과 미채택한 것을 아래 표/목록에 같이 적는다. pin만 올리면 다음
동기화가 "이미 본 변경"과 "안 본 변경"을 구별하지 못한다. 대조 근거는
`docs/mattpocock-skills-upstream-audit.md`에 있다.

## 매핑

upstream은 `skills/engineering|productivity/<name>/`로 묶고, 우리는 `skills/<name>/` 평면이다.
그 한 단계 말고는 경로가 같다.

| 우리 | upstream | 상태 |
| --- | --- | --- |
| `codebase-design` | `engineering/codebase-design` | 바이트 동일 |
| `agent-flow-diagnosing-bugs` + `workflows/diagnosing-bugs.yaml` | `engineering/diagnosing-bugs` | lifecycle wrapper + 9-phase workflow + marker-driven command evidence |
| `domain-modeling` | `engineering/domain-modeling` | description 병합 (아래) |
| `tdd` | `engineering/tdd` | 우리 `requires` + `tests.md` 예제 재작성 + `Skill` tool 표현 미채택 (아래) |
| `grill-with-docs` | `engineering/grill-with-docs` | 우리 `requires` + `Skill` tool 표현 미채택 (아래) |
| `grilling` | `productivity/grilling` | 이모지 예외 + sub-agent dispatch 문장 + em-dash 미채택 (아래) |
| `code-review` | `engineering/code-review` | 의도적 분기 (아래) |
| `resolving-merge-conflicts` | `engineering/resolving-merge-conflicts` | description 인용부호만 다름 |
| `to-prd` | `engineering/to-spec` | 우리 PRD 용어로 재작성 |

## 의도적 분기 — 동기화 때 되돌리지 않는다

- **`requires:` / `delivery:`** — upstream에 없는 우리 frontmatter다. `requires`는 installer의
  `validateSkillDependencies`가 검증하고, `delivery: passive`는 AGENTS.md skill index의 `always:`
  줄을 만든다. upstream 본문을 통째로 덮어쓸 때 이 두 줄을 같이 지우지 않는다.
- **PRD 용어** — upstream은 PRD를 spec으로 바꿨다. 우리는 `to-prd` skill, full-feature의
  `product-brief` phase, profile의 `artifacts.prd`/`vocabulary.prd`가 전부 PRD로 묶여 있어
  `code-review` 한 곳만 바꾸면 용어가 갈라진다.
- **`code-review`의 `## Quick start`** — upstream은 `/setup-matt-pocock-skills`가 진입 절차를
  대신하게 되면서 지웠다. 우리에게 그 skill이 없고, 대응물인 profile의 gates/branching은 skill
  **밖**에 있어 본문의 진입 절차를 대신하지 못한다.
- **`/setup-matt-pocock-skills`, `docs/agents/issue-tracker.md` 안내** — 우리에게 없는 명령과
  경로다. upstream이 새로 넣는 이 안내 줄들은 가져오지 않는다.
- **`grilling`의 이모지** — upstream은 질문 포맷에 `❓`/`➡️`를 쓴다. 이 저장소는 이모지를 쓰지
  않으므로 `**Q1** — ...` / `→ ...`로 바꿨다. 알고리즘(design tree, frontier, round 단위 질문,
  종료 조건)은 upstream 그대로다.
- **`resolving-merge-conflicts`의 description 인용부호** — 파싱 결과가 같아 따라가지 않는다.
- **`Skill` tool 호출 표현** — upstream은 skill 간 참조를 `call the Skill tool with "<name>"`로
  통일했다(`tdd`, `grill-with-docs`). 가져오지 않는다. 여기 vendored skill은
  `BUNDLED_HOST_SKILL_NAMES` 밖이라 host skill picker에 올라가지 않고, phase 프롬프트는
  `.agent-flow/skills/<name>/SKILL.md` **경로**로 읽으라고 지시한다
  (`workflows/full-feature.yaml`의 domain-grill phase). 그 문장을 따라가면 우리 install에서
  존재하지 않는 호출 경로를 지시하게 된다.
- **`domain-modeling`의 description** — upstream은 trigger를 파일 기준(CONTEXT.md/ADR)으로
  바꾸면서 `or when another skill needs to maintain the domain model` 절을 지웠다. 그 절은
  우리에게 살아 있는 trigger다 — `grill-with-docs`의 `requires`, `workflows/default.yaml`과
  `workflows/full-feature.yaml`의 design/domain-grill phase가 다른 skill/phase에서 이 skill을
  부른다. 그래서 파일 기준 trigger만 더하고 그 절은 남긴다.
- **`tdd/tests.md`의 implementation-detail 예제** — upstream 예제는 mock 남용과 내부 호출 단정을
  한 블록에 섞어 둔다. 결함 하나만 보이도록 다시 썼다. upstream 본문을 덮어쓸 때 되돌리지 않는다.
- **`grilling`의 sub-agent dispatch 문장** — upstream은 "dispatch a sub-agent to find it"이다.
  우리는 "find it yourself — read the repo, run the tool, or dispatch a read-only exploration
  sub-agent"로 바꿨다. 사실 조회를 subagent 전용으로 읽으면 도구로 바로 확인할 수 있는 것도
  위임하게 된다.
- **`grilling`의 em-dash 정리** — upstream은 em-dash를 콜론으로 바꿨다(`86cba45`). 이 저장소는
  본문에서 em-dash를 쓰므로 따라가지 않는다. 의미 차이가 없다.

## `agents/openai.yaml`

upstream은 skill마다 이 파일을 둔다(Codex picker의 `interface.display_name`/`short_description`,
user-invoked면 `policy.allow_implicit_invocation: false`). Host picker에 올라가지 않는 기존
vendored content skill에는 가져오지 않는다.

`agent-flow-diagnosing-bugs`는 예외다. 이 skill은 `BUNDLED_HOST_SKILL_NAMES` allowlist에 있는
user-invoked lifecycle wrapper라 `.claude/skills`, `.Codex/skills`, `.omp/skills`에 링크된다.
Claude의 `disable-model-invocation: true`와 Codex의
`policy.allow_implicit_invocation: false`를 함께 둬 host마다 호출 정책이 갈리지 않게 한다.

나머지 bundled skill은 `.agent-flow/skills/`에만 두고 AGENTS.md 인덱스와 phase prompt로
노출한다. 소비자가 없는 `agents/openai.yaml`을 늘리면 `SKILL.md` frontmatter와 두 번째
진실 원천이 갈라질 뿐이다.
