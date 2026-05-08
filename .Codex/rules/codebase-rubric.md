# Codebase Rubric

코드 개발/리뷰 중 빠르게 참조하는 AI-readiness 단일 기준이다. 점수는 자동 채점과 수동 보강을 합산한다. 태그 의미: `Auto`는 스크립트 신뢰 가능, `Heuristic`은 규칙 기반 추정, `Manual`은 사람이 판정.

## Score

| Cat | Name | Points | Pass Signal |
| --- | --- | ---: | --- |
| A | AI Navigation & Coverage | 15 | AI가 핵심 module/workflow를 1-2 hops 안에 찾음 |
| B | Context Document Quality | 20 | context가 짧고, 정확하고, 바로 작업에 도움 됨 |
| C | Tribal Knowledge Externalization | 20 | 숨은 규칙과 실패 패턴이 문서화됨 |
| D | Dependency & Data Flow Mapping | 15 | "X 변경 영향은?"에 답 가능 |
| E | Verification & Quality Gates | 15 | path/command/test가 검증 가능 |
| F | Freshness & Self-Maintenance | 10 | stale context를 자동/주기적으로 잡음 |
| G | Agent Performance Outcomes | 5 | AI task 성과를 측정함 |

## A. AI Navigation & Coverage /15

핵심 module과 workflow에 navigation guide가 있는지 본다. `CODEX_SCOPE`, `.codex-scope`, `.agentignore`, `AGENTS.md` scope가 있으면 그 범위를 우선한다. 기본 후보는 `src/*`, `app/*`, `lib/*`, `features/*`, `apps/*`, `packages/*`, `services/*`, `workflows/*`, `docs/workflows/*`, `bin/*`, `scripts/*`, package scripts다. `.git`, `.venv`, `node_modules`, `dist`, `build`, `.next`, `coverage`, cache, generated output은 제외한다. `Auto`

`module_coverage = README 섹션 또는 별도 md navigation guide가 있는 scoped 핵심 module 수 / scoped 핵심 module 총수`. `workflow_coverage`도 같은 방식으로 계산한다. Score = `round(((module_coverage * 0.7) + (workflow_coverage * 0.3)) * 15)`.

## B. Context Document Quality /20

Context는 encyclopedia가 아니라 compass여야 한다.

| Item | Points | Criteria |
| --- | ---: | --- |
| Conciseness `Auto` | 4 | entry context 최대 50 lines 또는 약 1,000 tokens. 긴 설명은 summary/reference로 분리 |
| Quick Commands `Heuristic` | 4 | copy-paste 명령과 사용 시점이 있음 |
| Key Files `Heuristic` | 4 | 실제 수정에 필요한 핵심 파일 3-5개가 있음 |
| Hidden Rules `Heuristic` | 4 | `Why:`, `Note:`, `Gotcha`, `Warning`, `Don't` 등으로 실패 규칙을 설명 |
| Cross References `Auto` | 4 | 관련 module/context/dependency map relative link가 있음 |

기본은 핵심 module equal weight 평균으로 계산한다. `CODEX_SCOPE` 등이 critical module을 지정하면 critical은 2x, 나머지는 1x로 계산한다. module coverage <70%면 B 최대 12점, <50%면 최대 8점. Conciseness는 Reference Accuracy 확인 후 점수를 준다.

## C. Tribal Knowledge Externalization /20

각 핵심 module이 아래 5개 질문에 답하면 질문당 4점이다. `Heuristic + Manual`

1. 이 module이 무엇을 own/configure 하는가?
2. 흔한 변경 패턴은 무엇인가?
3. 실패를 부르는 non-obvious rule은 무엇인가?
4. cross-module dependency는 무엇인가?
5. comments/history/human memory에 숨은 지식이 ADR, MEMORY, checklist, playbook에 반영됐는가?

Formula: `module_score = passed_questions / 5`, `C = round(avg(module_score) * 20)`. 빈 헤더, boilerplate, 의미 없는 AI 생성 문단은 0점 처리한다. keyword가 아니라 구체적 파일, 명령, 결정, failure mode를 봐야 한다.

## D. Dependency & Data Flow Mapping /15

변경 영향 범위를 추적할 수 있어야 한다.

| Item | Points | Criteria |
| --- | ---: | --- |
| Graph Source `Auto` | 5 | `ARCHITECTURE.md`, `docs/architecture.md`, `docs/dependency-graph*`, Mermaid/Graphviz, workspace/import graph 존재 |
| Relation Coverage `Heuristic` | 5 | owner, depends-on, used-by, data-in/out 관계가 주요 module에 있음 |
| Impact Answerability `Manual` | 5 | "What depends on X?", "What tests/workflows are affected?"에 1-2 hops 안에 답 가능 |

파일 존재만으로 만점 금지. dependency map은 context에서 링크되어야 하고, 관련 module의 중요 변경보다 오래됐거나 `Last Validated Commit`이 뒤처지면 stale 후보로 표시한다.

## E. Verification & Quality Gates /15

| Item | Points | Criteria |
| --- | ---: | --- |
| Reference Accuracy `Auto + Heuristic` | 5 | markdown links, backtick paths, command args, imports, alias paths가 실제 존재. API/command는 실행 또는 reviewer로 보강 |
| Independent Critic `Manual` | 4 | reviewer, CODEOWNERS, review template, agent critic 등 독립 검토가 있음 |
| Task Validation `Auto` | 4 | 변경 유형별 build/test/lint/typecheck/e2e 명령이 있고 실행 가능 |
| Prompt / Workflow Tests `Heuristic` | 2 | `evals/`, `benchmarks/`, agent test, 대표 AI task query가 있음 |

Zero hallucinated paths가 5점 조건이다. URL, package name, glob, placeholder, generated output, fixture는 ignore pattern으로 분리한다.

## F. Freshness & Self-Maintenance /10

| Score | Criteria |
| ---: | --- |
| 0 | 수동 관리 + stale 여부 불명 |
| 3 | owner 있음 + 최근 릴리즈 또는 90일 안에 update |
| 6 | CI/script/hook으로 broken path 또는 reference 일부 검출 |
| 10 | path validation, coverage gap detection, critic review, stale repair가 주기적으로 실행 |

mtime만으로 stale 확정 금지. broken path check, dependency map freshness, `Last Validated Commit`, reviewer note를 함께 본다.

## G. Agent Performance Outcomes /5

실제 AI task pass rate, tool calls, tokens, completion time, clarification count, rework rate, hallucinated path count 중 하나 이상을 측정하면 3점, before/after와 correctness까지 보면 5점. `evals/`, `benchmarks/`, `agent-metrics/`, `.skill-eval.json`, `agent-results.json`을 우선 확인한다.

## Grade

| Score | Level |
| ---: | --- |
| 90-100 | AI-Native / Agentic-Ready |
| 75-89 | AI-Ready |
| 60-74 | AI-Assisted |
| 40-59 | AI-Fragile |
| <40 | AI-Hostile |

## Recommendation Rule

상위 5개 gap만 제시한다. 형식은 `Action / Effort(S,M,L) / Impact / Priority`. 기본 추천은 context, index, validation, eval 보강을 우선한다. god file 분할, naming refactor 같은 리팩터성 액션은 AI task 실패, 반복 hallucination, cascade bug, dependency 추적 실패의 직접 원인일 때만 제안한다.
