# Agent Flow — AI 소프트웨어 팩토리 오케스트레이터

공용 규칙(harness/verification/evaluation/coding/security/token/qmd)은 부모 디렉토리 `/Users/namdoo/Downloads/claude/CLAUDE.md` 에서 자동 로드된다. 이 파일은 **프로젝트 특화**만.

## 기술 스택
- Next.js 16 App Router + TypeScript + Tailwind v4
- D3-force 에이전트 노드 시각화
- Claude Code CLI child_process 래핑 (Max 구독, 비용 $0)
- Chrome DevTools MCP 브라우저 자동화 테스트
- 파일 기반 저장: `data/projects.json`, `data/executions/`

## 주요 파일 맵
| 영역 | 경로 |
|------|------|
| 메인 페이지 | `src/app/page.tsx` |
| CLI 엔드포인트 | `src/app/api/agent/run/route.ts` (spawn claude) |
| 프로젝트 API | `src/app/api/projects/route.ts` |
| 실행 기록 API | `src/app/api/executions/route.ts` |
| Mock SSE | `src/app/api/events/route.ts` + `src/lib/event-store.ts` |
| 오케스트레이터 | `src/lib/orchestrator.ts` (runWorkflow, callAgent, REJECT 루프) |
| 에이전트 프롬프트 | `src/lib/agent-prompts.ts` (시스템 프롬프트, 모델 매핑, 도구 권한) |
| 타입 | `src/lib/types.ts` |
| 프로젝트 저장소 | `src/lib/project-store.ts` |
| 실행 기록 저장소 | `src/lib/execution-store.ts` |
| 워크플로우 프리셋 | `src/lib/workflow-templates.ts` |
| 사이드바 | `src/components/AppSidebar.tsx` |
| 칸반 보드 | `src/components/KanbanBoard.tsx` |
| 대시보드 | `src/components/DashboardMain.tsx` |
| FlowCanvas | `src/components/FlowCanvas.tsx` |
| MCP 설정 | `.mcp.json` |

## 핵심 패턴
- 뷰 라우팅: `MainView = 'dashboard' | 'kanban' | 'flow'` (page.tsx 조건부 렌더)
- 이벤트 스트림: `useEventStream(mode)` mock/real 이중 모드
- 오케스트레이터: 프론트엔드가 에이전트 순차 호출, REJECT 시 Developer로 복귀(최대 3회)
- CLI 호출: `spawn('cmd.exe', ['/c', 'claude', ...args])` (Windows 대응)

## 런타임 오케스트레이터 — 에이전트별 도구 권한
| Role | 허용 도구 |
|------|---------|
| pm_planner | Read, Glob, Grep |
| designer | Read, Glob, Grep |
| developer | Read, Write, Edit, Bash, Glob, Grep, Chrome DevTools MCP |
| security_expert | Read, Glob, Grep, Bash |
| code-reviewer | Read, Glob, Grep |
| design-reviewer | Read, Glob, Grep |

## QA 명령 (Step 2 Verification Loop에서 사용)
- **BUILD**: `npm run build`
- **TYPECHECK**: `npx tsc --noEmit`
- **LINT**: `npm run lint`

## 구현 현황
**완료**: 사이드바, 3단 뷰(대시보드/칸반/FlowCanvas), 칸반 6열, 인라인 프롬프트+드래그앤드롭, D3-force 시각화, SSE 스트리밍, CLI 래핑, MCP 설정, 도구 권한 분리, 프로젝트 CRUD, 실행 기록, 카드 자동 이동, 모바일 반응형
**미구현**: Google OAuth, 프로젝트 모달, 실행 기록 상세 뷰어, 마크다운 렌더, 비용 집계 대시보드, Railway/Render 배포, macOS CLI 검증

## 프로젝트 특화 규칙
@.claude/rules/design-tokens.md
@.claude/rules/security-scenarios.md
