# Vendored skill provenance

여기 있는 skill 중 일부는 [mattpocock/skills](https://github.com/mattpocock/skills)에서 가져와
우리 구조에 맞게 고친 vendored 사본이다. 이 파일은 **어느 시점의 upstream과 맞춰 뒀는지**를 적는
자리다. 이 기록이 없으면 다음 동기화가 매번 전량 diff가 된다 — 기록이 있으면
`git -C <clone> diff <pinned>..HEAD -- skills/` 범위 diff로 줄어든다.

- upstream: `https://github.com/mattpocock/skills`
- pinned commit: `8b36d4fb2635b3c21998dcd8144439c9e5ba7302` (2026-08-05)

## 매핑

upstream은 `skills/engineering|productivity/<name>/`로 묶고, 우리는 `skills/<name>/` 평면이다.
그 한 단계 말고는 경로가 같다.

| 우리 | upstream | 상태 |
| --- | --- | --- |
| `codebase-design` | `engineering/codebase-design` | 바이트 동일 |
| `domain-modeling` | `engineering/domain-modeling` | 바이트 동일 |
| `tdd` | `engineering/tdd` | 동일 + 우리 `requires` |
| `grill-with-docs` | `engineering/grill-with-docs` | 동일 + 우리 `requires` |
| `grilling` | `productivity/grilling` | 동일 + 아래 이모지 예외 |
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

## `agents/openai.yaml`

upstream은 skill마다 이 파일을 둔다(Codex picker의 `interface.display_name`/`short_description`,
user-invoked면 `policy.allow_implicit_invocation: false`). **가져오지 않는다.**

bundled skill 중 host skill 디렉터리(`.claude/skills`, `.Codex/skills`, `.omp/skills`)로 링크되는
것은 `BUNDLED_HOST_SKILL_NAMES` allowlist에 있는 것뿐이고(`bin/agent-flow-kit.mjs`, parity가
두 installer와 대조한다), 나머지는 `.agent-flow/skills/`에만 두고 AGENTS.md 인덱스와 phase
프롬프트로 노출한다. 여기 있는 vendored skill은 전부 allowlist 밖이라 host의 skill picker에
올라가지 않는다 — 그래서 Codex의 `policy`도, Claude만 읽는 `disable-model-invocation`도 지금은
소비자가 없다. 소비자 없는 metadata를 skill마다 늘리면 `SKILL.md` frontmatter와 두 번째 진실
원천이 갈라질 뿐이다.

되돌아볼 조건: vendored skill을 `BUNDLED_HOST_SKILL_NAMES`에 올리는 날. 그때는 user-invoked
skill(`disable-model-invocation: true`인 `grill-with-docs`, `to-prd`)에 `agents/openai.yaml`의
`policy.allow_implicit_invocation: false`를 **쌍으로** 넣어야 한다. 한쪽만 있으면 같은 skill이
Claude에서는 사람만, Codex에서는 모델도 부를 수 있는 상태가 된다.
