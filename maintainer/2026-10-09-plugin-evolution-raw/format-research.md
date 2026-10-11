# 워크플로를 강제하는 AI 에이전트 시스템 조사 (2026-10-09 기준)

## 결론

- 단계를 실제로 막고 넘기는(강제) 시스템은 대부분 **선언형 YAML + 런타임 엔진**을 씁니다. Spec Kit `specify workflow`, Archon, MS Agent Framework, Goose recipe, gh-aw가 여기에 속해요. 다른 방식은 **코드 그래프**예요. LangGraph, CrewAI Flows, Temporal이 그렇습니다.
- Markdown(skill/command)은 거의 다 **안내용**이에요. 에이전트가 스스로 지켜야 합니다. Superpowers, BMAD, Agent OS, SuperClaude, OpenHands, Dryforge가 이 쪽이에요.
- 눈에 띄는 변화가 하나 있어요. Markdown 명령으로 시작한 Spec Kit이 별도로 YAML 워크플로 엔진(게이트, 루프, 재개)을 추가했어요.
- 판단: Agent Flow는 **YAML을 유지**하는 게 맞습니다. 다만 세 가지를 더하길 권해요.
  - 스키마 검증
  - 그래프 의미 검증 (경로가 끝까지 이어지는지, 루프마다 상한이 있는지)
  - 정규화한 내용의 digest(지문 같은 해시값)
- 위조를 막는 힘은 형식에서 나오지 않아요. 고정(pinning)과 hook이 막아 줍니다.

## 비교표

| project | 형식 | 정의 위치 | 강제 주체 | 근거 URL |
|---|---|---|---|---|
| Spec Kit (commands) | Markdown + YAML frontmatter | 패키지 템플릿 → 설치 시 저장소에 복사 | 프롬프트(에이전트가 스스로 지킴) | [plan.md](https://raw.githubusercontent.com/github/spec-kit/main/templates/commands/plan.md) |
| Spec Kit (`specify workflow`) | YAML (`steps`, `gate`, `while`/`max_iterations`, `{{ }}` 식) | `workflows/*/workflow.yml` 또는 로컬 파일 | Python 엔진. 단계마다 `state.json`을 저장하고 `resume`로 재개 | [README](https://raw.githubusercontent.com/github/spec-kit/main/workflows/README.md), [ARCHITECTURE](https://raw.githubusercontent.com/github/spec-kit/main/workflows/ARCHITECTURE.md) |
| Kiro specs | Markdown 3종 (requirements/design/tasks) | `.kiro/specs` | IDE (게이트가 어떻게 작동하는지는 공개 안 됨) | [docs/specs](https://kiro.dev/docs/specs/) |
| Kiro hooks / steering | hooks는 JSON, steering은 Markdown + frontmatter | `.kiro/hooks/*.json`, `.kiro/steering/` | `PreToolUse` hook으로 실행 차단 가능 | [hooks](https://kiro.dev/docs/hooks/), [steering](https://kiro.dev/docs/steering/) |
| BMAD-METHOD | `SKILL.md` + `workflow.md` + `step-NN-*.md` + `customize.toml` | `skills/<name>/` | 대부분 프롬프트("NEVER skip steps"). 템플릿 `halt()`로 설정값만 검사 | [bmad-build](https://github.com/bmad-code-org/BMAD-METHOD/tree/main/skills/bmad-build), [workflow.md](https://raw.githubusercontent.com/bmad-code-org/BMAD-METHOD/main/skills/bmad-build/workflow.md) |
| obra/superpowers | `SKILL.md` (frontmatter `name`/`description`) | 플러그인 `skills/` | `SessionStart` hook은 안내문을 넣어 주기만 함. 따르는 건 프롬프트 | [hooks.json](https://raw.githubusercontent.com/obra/superpowers/main/hooks/hooks.json), [SKILL.md](https://raw.githubusercontent.com/obra/superpowers/main/skills/brainstorming/SKILL.md) |
| Task Master | 작업 상태는 `tasks.json`, MCP 설정은 JSON | `.taskmaster/` | CLI/MCP. `next`는 의존성이 끝난 작업만 보여 줌 | [task-structure](https://docs.task-master.dev/capabilities/task-structure) |
| Archon | YAML DAG (`nodes`, `depends_on`, `loop.until`, `interactive`) | `.archon/workflows/` (저장소에 커밋) | TS 실행기 (`dag-executor.ts`), Zod 스키마, 실행 기록은 SQLite/Postgres | [README](https://github.com/coleam00/Archon), [schemas](https://github.com/coleam00/Archon/tree/main/packages/workflows/src/schemas), [workflow.ts](https://raw.githubusercontent.com/coleam00/Archon/main/packages/workflows/src/schemas/workflow.ts) |
| Agent OS | Markdown commands | `commands/agent-os/`, 루트 `config.yml` | 프롬프트("must be run in plan mode") | [shape-spec.md](https://raw.githubusercontent.com/buildermethods/agent-os/main/commands/agent-os/shape-spec.md) |
| cc-sdd | Markdown skills/templates + 상태 파일 `spec.json` (approvals) | `.kiro/specs/<f>/`, `{{KIRO_DIR}}/settings/` | 명령 프롬프트가 `spec.json`의 승인 여부를 확인. `-y`로 건너뛸 수 있음 | [command-reference](https://github.com/gotalab/cc-sdd/blob/main/docs/guides/command-reference.md), [spec-driven](https://github.com/gotalab/cc-sdd/blob/main/docs/guides/spec-driven.md) |
| SuperClaude | Markdown commands (Python 패키지로 설치) | `src/superclaude/commands/*.md` | 프롬프트("behavioral instruction injection") | [commands](https://github.com/SuperClaude-Org/SuperClaude_Framework/tree/master/src/superclaude/commands), [README](https://github.com/SuperClaude-Org/SuperClaude_Framework) |
| Roo Code modes | YAML (JSON도 가능) | `.roomodes`, `custom_modes.yaml` | 런타임이 권한을 강제(`fileRegex` 위반 시 `FileRestrictionError`). 단계 순서는 강제 안 함 | [custom-modes](https://roocodeinc.github.io/Roo-Code/features/custom-modes) |
| Goose recipes | YAML(권장) 또는 JSON | recipe 파일 | 런타임: `retry.checks`(shell 명령 exit 0이어야 통과), `response.json_schema`, `goose recipe validate` | [recipe-reference](https://goose-docs.ai/docs/guides/recipes/recipe-reference) |
| OpenHands skills | `SKILL.md` + YAML frontmatter | `.agents/skills/`, 예전 위치 `.openhands/microagents/` | 프롬프트 주입만 함("do not grant permissions") | [skills](https://docs.openhands.dev/overview/skills) |
| CrewAI | Crew는 `agents.yaml`/`tasks.yaml`, Flow는 Python 데코레이터 | 저장소 코드 | Flow 런타임 (`@router`, Pydantic state) | [flows](https://docs.crewai.com/en/concepts/flows) |
| LangGraph | Python `StateGraph` | 코드 | 런타임: compile 시 구조 검증, checkpointer, `interrupt()`, `recursion_limit` | [graph-api](https://docs.langchain.com/oss/python/langgraph/graph-api) |
| Google ADK Agent Config | YAML (`root_agent.yaml`), JSON Schema 포함 | 저장소 | ADK 런타임. 이 기능은 아직 **실험 단계** | [config](https://adk.dev/agents/config/), [AgentConfig.json](https://github.com/google/adk-python/blob/main/src/google/adk/agents/config_schemas/AgentConfig.json) |
| MS Agent Framework (declarative) | YAML → Workflow 그래프 | 저장소 파일 | 코드로 만든 워크플로와 같은 `Workflow` 런타임 | [declarative](https://learn.microsoft.com/en-us/agent-framework/workflows/declarative) |
| Semantic Kernel Process | 코드 (이벤트 기반 step) | 코드 | 런타임. **experimental** | [process-framework](https://learn.microsoft.com/en-us/semantic-kernel/frameworks/process/process-framework) |
| gh-aw | Markdown + YAML frontmatter → `.lock.yml`로 컴파일 | `.github/workflows/*.md` | 컴파일 시 스키마 검증 + Actions 런타임 + safe-outputs 별도 job | [gh-aw](https://github.github.com/gh-aw/) |
| Dryforge | 일반 Markdown skills | `src/skills`, 작업 기록은 `.dryforge/` | 대부분 프롬프트. git이 clean이어야 한다는 전제조건만 명시. 구현 방식은 미확인 | [repo](https://github.com/prekuter/dryforge), [README](https://raw.githubusercontent.com/prekuter/dryforge/main/README.md) |
| Temporal | 코드 (Go/Java/TS/Python) | 코드 | Event History를 다시 재생(replay). 결정적(deterministic) 코드여야 함 | [workflows](https://docs.temporal.io/workflows) |
| n8n | JSON | DB (기본 SQLite) | 서버 런타임 | [export-import](https://docs.n8n.io/workflows/export-import/), [DB](https://docs.n8n.io/hosting/configuration/supported-databases-settings/) |
| Claude Code hooks (참고) | JSON settings | `.claude/settings.json`, 플러그인 `hooks/hooks.json` | `PreToolUse`에서 deny로 막기, `Stop`에서 block으로 막기 | [hooks](https://code.claude.com/docs/en/hooks) |

## 패턴 정리

근거가 있는 관찰:

- **강제 + 사람이 편집하는 정의**는 YAML이 가장 많아요(Spec Kit workflow, Archon, Goose, MAF, ADK config, Roo modes). 모두 별도 엔진이나 런타임이 실행합니다.
- **강제 + 프로그래머용** 시스템은 코드 그래프를 써요(LangGraph, CrewAI Flows, Temporal, SK Process).
- **JSON**은 주로 기계가 쓰는 곳에 있어요. 상태 파일(`tasks.json`, `spec.json`), GUI가 저장하는 정의(n8n), 호스트 hook 설정(Kiro, Claude Code)이에요.
- **Markdown**은 안내용이에요. 강제처럼 보여도 실제로는 "MUST"라고 쓴 문장일 뿐입니다. hook이 있어도 안내문을 넣어 주는 데까지만 해요(superpowers).
- 검증 방법:
  - Archon: Zod
  - ADK: JSON Schema
  - gh-aw: 컴파일할 때 스키마 검증
  - Goose: `goose recipe validate`
  - Spec Kit: Python `validate_workflow()`
  - LangGraph: `compile()`
- 정의를 실행마다 고정(pinning)한다는 내용은 Spec Kit·Archon 문서에서 **찾지 못했어요**. gh-aw의 "pinned"는 action을 SHA로 고정한다는 뜻이고, 워크플로 내용을 고정한다는 뜻이 아니에요.

판단: Agent Flow처럼 "YAML + Python 단일 권한 runner + 정의 고정 + 쓰기 hook" 조합은 확인한 범위에서 드문 편이에요. Spec Kit workflow와 Archon이 가장 비슷합니다.

## YAML 장단점

### 근거가 있는 것
- MS 공식 가이드는 이렇게 나눠요.
  - YAML: 표준 패턴, 자주 바뀌는 워크플로, 개발자가 아닌 사람이 수정할 때
  - 코드: 복잡한 커스텀 로직, 최대한의 제어가 필요할 때 ([Learn](https://learn.microsoft.com/en-us/agent-framework/workflows/declarative))
- YAML 엔진은 조건과 루프를 위해 결국 자체 식 언어를 들여와요. Spec Kit은 `{{ }}`와 `while`, Archon은 `until`을 씁니다([Spec Kit README](https://raw.githubusercontent.com/github/spec-kit/main/workflows/README.md), [Archon](https://github.com/coleam00/Archon)).
- YAML 1.2는 JSON의 상위집합이에요. 문제가 많던 암묵적 타입 규칙도 지웠어요([yaml.org](https://yaml.org/spec/1.2.2/)).
- PyYAML은 YAML 1.1 기준으로 보여요(문서가 1.1 스펙을 링크함). 또 `yaml.load`는 신뢰할 수 없는 입력에 안전하지 않아서 `safe_load`를 권장해요([PyYAML](https://pyyaml.org/wiki/PyYAMLDocumentation)).
- 코드로 정의하는 엔진은 결정적이어야 한다는 제약이 커요(Temporal).

### 판단
**(a) JSON/TOML과 비교**
- YAML이 사람이 읽고 리뷰하기 쉬워요. 주석도 쓸 수 있어요.
- JSON은 타입이 애매할 일이 없어요. 대신 손으로 쓰기 불편해요.
- TOML은 깊게 중첩된 phase/route 목록을 쓰기 어색해요.
- diff는 셋 다 비슷해요.

**(b) Markdown/frontmatter와 비교**
- 본문이 자유 형식이라 엔진이 해석할 계약(약속된 구조)이 될 수 없어요. 강제용으로는 맞지 않아요.

**(c) Python 코드와 비교**
- 코드는 표현력과 타입 검사가 가장 좋아요.
- 대신 리뷰하는 사람이 흐름을 한눈에 보기 어려워요.
- 내용 고정(digest)도 "데이터"보다 어렵고, 실행 중 동작이 바뀔 위험이 있어요.
- 위조 면에서도 이득이 없어요. 에이전트는 `.py`도 고칠 수 있으니까요. 그래서 막는 일은 hook과 pinning이 맡아야 해요.

**(d) 타입 스키마/DSL과 비교**
- DSL은 도구(에디터, 린터)를 새로 만들어야 해서 비용이 커요.
- "YAML + JSON Schema 또는 Pydantic"이면 DSL의 이점을 대부분 얻어요. ADK가 `yaml-language-server`로 스키마를 연결하는 것과 같은 방식이에요.

**표현력**
- 조건을 verdict enum(정해진 값 목록) → route 표로만 제한하세요. 그러면 식 언어가 필요 없어요. 루프 상한도 필드 하나로 충분해요.

## Agent Flow 권고 (판단)

**YAML을 유지하세요.** 형식을 바꿔도 얻는 게 적어요. 대신 아래를 추가하길 권합니다.

1. **스키마 검증**
   - Pydantic 모델(또는 JSON Schema)에 `extra="forbid"`를 걸고, 공개 스키마 파일을 생성해 에디터와 CI에서 쓰세요.
2. **로더 강화**
   - `safe_load`를 쓰세요.
   - 중복 키는 거부하세요.
   - 불리언/문자열이 헷갈릴 수 있는 값(`on`, `yes`)은 enum으로 막으세요.
3. **그래프 의미 검증** (LangGraph `compile()`과 같은 역할)
   - 모든 route가 실제 있는 phase를 가리키는지
   - 모든 경로가 종료 phase에 닿는지
   - 루프(fix-loop)마다 상한이 있는지
   - 필요한 artifact/marker 이름에 오타가 없는지
4. **digest**
   - 파싱한 결과를 정규화한 뒤 `sha256`을 run record에 저장하세요. resume할 때 비교합니다.
   - 기본 제공 워크플로는 패키지 안의 digest 목록과 대조해서 "수정됨" 경고를 내세요.
5. **편집 금지 경로**
   - 워크플로 디렉터리를 쓰기 경계 hook의 금지 경로에 넣으세요(이미 했다면 그대로 두면 돼요).

## 확인 못 한 항목

- Kiro: spec 단계 게이트의 실제 구현 방식(소스가 닫혀 있음)
- Spec Kit·Archon: 실행마다 정의를 고정하는지 (문서에 언급 없음)
- ADK Agent Config: `LoopAgent`/`max_iterations` 지원 여부
- MS Agent Framework YAML: 식 언어 종류, 액션 전체 목록 (페이지가 너무 커서 일부만 읽음)
- Dryforge: hook이나 스크립트로 강제하는지
- Task Master: `set-status`가 의존성 때문에 막히는지
- cc-sdd: `spec.json`의 정확한 스키마 (GitHub 문서를 검색 요약으로만 봄)
- Prefect: 조사 안 함
- "prekuter/dryforge"는 검색에는 안 나왔지만 직접 접속해서 존재를 확인했어요.

다음에 할 일: 위 권고 1~3번(Pydantic 스키마 + 그래프 검증)부터 계획을 짜 볼까요?