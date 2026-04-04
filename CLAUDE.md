# Agent Flow — AI 소프트웨어 팩토리 오케스트레이터

@AGENTS.md

---

## 기술 스택
- **Next.js 16** App Router + TypeScript + Tailwind v4
- **D3-force** 에이전트 노드 시각화
- **Claude Code CLI** child_process 래핑 (Max 구독, 비용 $0)
- **Chrome DevTools MCP** 브라우저 자동화 테스트
- **파일 기반 저장** data/projects.json, data/executions/

---

## 디자인 시스템

| 요소 | 규칙 |
|------|------|
| 배경 | `bg-[#09090b]`(페이지) / `bg-zinc-950`(사이드바) / `bg-zinc-900`(카드) |
| 액센트 | `emerald`(실행/성공) / `red`(에러/중지) / `amber`(경고) / `blue`(검수) |
| 에이전트 색상 | PM `#8b5cf6` / Designer `#ec4899` / Dev `#34d399` / Security `#f59e0b` / Reviewer `#3b82f6` |
| 카드 | `rounded-lg border border-zinc-800 bg-zinc-900/50` |
| 텍스트 | `text-white`(제목) / `text-zinc-400`(라벨) / `text-zinc-600`(보조) / `font-mono`(숫자) |
| 입력 | `bg-zinc-900 border border-zinc-800 rounded-xl` |
| 반응형 | 사이드바 `w-14 md:w-56`, 대시보드 `grid-cols-2 md:grid-cols-4` |

---

## 구현 현황

**완료**: 좌측 사이드바(프로젝트+에이전트), 3단 뷰(대시보드/칸반/FlowCanvas), 칸반 6열(아이디어→기획→디자인→개발→검수→완료), 인라인 프롬프트+파일 드래그앤드롭, D3-force 에이전트 노드 시각화, SSE 이벤트 스트리밍(mock/real), CLI 래핑 엔진(child_process), Chrome DevTools MCP 설정, 에이전트별 도구 권한 분리, 프로젝트 CRUD API, 실행 기록 저장, 칸반 카드 자동 이동(에이전트 상태 훅), 모바일 반응형
**미구현**: Google OAuth 인증, 프로젝트 추가/삭제 UI 모달, 실행 기록 상세 뷰어, 에이전트 출력 마크다운 렌더, 실행 비용 집계 대시보드, Railway/Render 배포 설정, macOS CLI 검증

---

## 주요 파일 맵

| 영역 | 경로 |
|------|------|
| 메인 페이지 | `src/app/page.tsx` (뷰 라우팅, 오케스트레이션) |
| CLI 엔드포인트 | `src/app/api/agent/run/route.ts` (spawn claude) |
| 프로젝트 API | `src/app/api/projects/route.ts` (GET/POST/DELETE) |
| 실행 기록 API | `src/app/api/executions/route.ts` (GET) |
| Mock SSE | `src/app/api/events/route.ts` + `src/lib/event-store.ts` |
| 오케스트레이터 | `src/lib/orchestrator.ts` (runWorkflow, callAgent, REJECT 루프) |
| 에이전트 프롬프트 | `src/lib/agent-prompts.ts` (시스템 프롬프트, 모델 매핑, 도구 권한) |
| 타입 정의 | `src/lib/types.ts` (AgentRole, Session, TokenUsage, WorkflowExecution) |
| 프로젝트 저장 | `src/lib/project-store.ts` (fs CRUD → data/projects.json) |
| 실행 기록 저장 | `src/lib/execution-store.ts` (fs → data/executions/*.json) |
| 워크플로우 프리셋 | `src/lib/workflow-templates.ts` (Web/Android/iOS) |
| 사이드바 | `src/components/AppSidebar.tsx` (프로젝트 목록 + 에이전트 상태) |
| 칸반 보드 | `src/components/KanbanBoard.tsx` (6열 + 인라인 프롬프트 + 드래그앤드롭) |
| 대시보드 | `src/components/DashboardMain.tsx` (통계 + 프로젝트 + 실행 기록) |
| 에이전트 그래프 | `src/components/FlowCanvas.tsx` (D3-force SVG) |
| 이벤트 로그 | `src/components/Sidebar.tsx` (FlowCanvas 좌측 패널) |
| 에이전트 상세 | `src/components/AgentDetailCard.tsx` (출력 + 토큰 + 이벤트) |
| 검증 루프 | `src/components/VerificationLoopPanel.tsx` (CODE→TEST→FIX→PASS) |
| 이벤트 훅 | `src/hooks/useEventStream.ts` (mock/real 이중 모드) |
| MCP 설정 | `.mcp.json` (Chrome DevTools) |
| 프로젝트 데이터 | `data/projects.json` (Trading Journal, CrestyNode) |

---

## 핵심 코드 패턴

- **뷰 라우팅**: `MainView = 'dashboard' | 'kanban' | 'flow'` — page.tsx에서 조건부 렌더
- **이벤트 스트림**: `useEventStream(mode)` — mock/real 이중 모드, `pushEvent()` 콜백
- **오케스트레이터**: `runWorkflow()` — 프론트엔드가 에이전트 순차 호출
- **칸반 카드 상태**: `agentStateUpdate` → `setCards()` — 에이전트 완료 시 칼럼 자동 이동
- **CLI 호출**: `spawn('cmd.exe', ['/c', 'claude', ...args])` — Windows 대응
- **프로젝트 저장**: `project-store.ts` (fs 기반 CRUD) → `/api/projects`

---

## 파일 구조

```
src/
├── app/
│   ├── page.tsx                    ← 메인 (3단 뷰 라우팅)
│   └── api/
│       ├── agent/run/route.ts      ← Claude CLI 실행 엔드포인트
│       ├── projects/route.ts       ← 프로젝트 CRUD
│       ├── executions/route.ts     ← 실행 기록 조회
│       └── events/route.ts         ← Mock SSE 시뮬레이션
├── components/
│   ├── AppSidebar.tsx              ← 좌측 고정 사이드바
│   ├── KanbanBoard.tsx             ← 칸반 보드 + 인라인 프롬프트
│   ├── DashboardMain.tsx           ← 대시보드 메인 영역
│   ├── FlowCanvas.tsx              ← D3-force 에이전트 시각화
│   ├── Sidebar.tsx                 ← FlowCanvas 이벤트 로그
│   ├── TopBar.tsx                  ← 실행 상태바
│   ├── AgentDetailCard.tsx         ← 에이전트 상세 패널
│   └── VerificationLoopPanel.tsx   ← CODE→TEST→FIX→PASS 패널
├── hooks/
│   └── useEventStream.ts           ← SSE + pushEvent 이중 모드
└── lib/
    ├── types.ts                    ← 전체 타입 정의
    ├── agent-prompts.ts            ← 에이전트 시스템 프롬프트 + 도구 권한
    ├── orchestrator.ts             ← 클라이언트 워크플로우 드라이버
    ├── project-store.ts            ← 파일 기반 프로젝트 저장소
    ├── execution-store.ts          ← 실행 기록 저장소
    └── workflow-templates.ts       ← 플랫폼별 워크플로우 프리셋
```

---

## 하네스 워크플로우

공통 워크플로우/완료보고/주의사항: 루트 `.claude/rules/harness-workflow.md` 참조

### 에이전트 파이프라인
```
Orchestrator → PM/Planner → Designer → Developer → Security Expert → Reviewer
                                         ↑                            ↓ (REJECT)
                                         └────────────────────────────┘ (max 3회)
```

### 에이전트별 도구 권한
| Role | 허용 도구 |
|------|---------|
| pm_planner | Read, Glob, Grep |
| designer | Read, Glob, Grep |
| developer | Read, Write, Edit, Bash, Glob, Grep, Chrome DevTools MCP |
| security_expert | Read, Glob, Grep, Bash |
| reviewer | Read, Glob, Grep, Bash |

---

## 상세 참조
- 하네스 워크플로우/완료보고: 루트 `.claude/rules/harness-workflow.md`
- 품질 평가 기준: 루트 `.claude/rules/evaluation-criteria.md`
- 보안 체크리스트: 루트 `.claude/agents/security_expert.md`
- 코딩 스타일: 루트 `.claude/rules/coding-style.md`
- 토큰 최적화: 루트 `.claude/rules/token-optimization.md`
