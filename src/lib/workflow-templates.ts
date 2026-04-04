import type {
  WorkflowTemplate,
  AgentDefinition,
  EdgeDefinition,
  AgentTeam,
  VerificationLoopConfig,
} from './types'
import { ANDROID_AGENTS, ANDROID_VERIFICATION } from './agents/android/skills'
import { IOS_AGENTS, IOS_VERIFICATION } from './agents/ios/skills'

// ──────────────────────────────────────────
// 공통 에이전트 (모든 플랫폼에서 공유)
// ──────────────────────────────────────────

const orchestrator = (name = 'Orchestrator'): AgentDefinition => ({
  id: 'orchestrator',
  name,
  role: 'orchestrator',
  color: '#f43f5e',
  description: '전체 워크플로우를 오케스트레이션하고 에이전트 간 핸드오프를 관리',
  systemPrompt: '하네스 오케스트레이터. 기획→디자인→개발→보안→QA 순서로 에이전트를 호출하고 결과를 종합.',
  tools: ['Agent', 'Read', 'Write'],
  model: 'opus',
})

const pmPlanner = (extras = ''): AgentDefinition => ({
  id: 'pm_planner',
  name: 'PM/Planner',
  role: 'pm_planner',
  color: '#8b5cf6',
  description: '기능 기획 및 SPEC.md 생성',
  systemPrompt: `기능 요구사항 분석, 아키텍처 설계, SPEC.md 작성. ${extras}`,
  tools: ['Read', 'Write', 'Glob', 'Grep'],
  model: 'opus',
})

const reviewer = (extras = ''): AgentDefinition => ({
  id: 'reviewer',
  name: 'Reviewer',
  role: 'reviewer',
  color: '#3b82f6',
  description: 'QA 검수 및 합격/불합격 판정',
  systemPrompt: `코드 품질, 디자인, 기능, 보안을 종합 평가하여 QA_REPORT.md 작성. ${extras}`,
  tools: ['Read', 'Glob', 'Grep', 'Bash'],
  model: 'opus',
})

// ──────────────────────────────────────────
// Web (Next.js) 프리셋
// ──────────────────────────────────────────

const webAgents: AgentDefinition[] = [
  orchestrator('조이사(CSO)'),
  pmPlanner('Next.js App Router 기반 SPA/SSR 설계'),
  {
    id: 'designer',
    name: 'Designer',
    role: 'designer',
    color: '#ec4899',
    description: 'UI/UX 설계 및 DESIGN.md 생성',
    systemPrompt: 'Tailwind CSS 4 디자인 토큰 기반 UI 설계. 다크테마, 반응형, 컴포넌트 구조 정의.',
    tools: ['Read', 'Write', 'Glob'],
    model: 'sonnet',
  },
  {
    id: 'developer',
    name: 'Developer',
    role: 'developer',
    color: '#34d399',
    description: 'React/TypeScript 코드 구현',
    systemPrompt: 'SPEC.md + DESIGN.md 기반 Next.js 16 + TypeScript + Supabase 코드 구현. 불변성 패턴, Server/Client 컴포넌트 분리.',
    tools: ['Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep'],
    model: 'sonnet',
  },
  {
    id: 'security_expert',
    name: 'Security Expert',
    role: 'security_expert',
    color: '#f59e0b',
    description: '보안 감사 및 SECURITY_REPORT.md 생성',
    systemPrompt: 'Supabase RLS 정책, XSS/CSRF 방지, getUser() 인증, 환경변수 검증 감사.',
    tools: ['Read', 'Glob', 'Grep'],
    model: 'sonnet',
  },
  reviewer('빌드 성공 필수. Tailwind 디자인 토큰 준수 확인. 가중점수 7.0 이상 PASS.'),
]

const webEdges: EdgeDefinition[] = [
  { id: 'e1', source: 'orchestrator', target: 'pm_planner', label: '기획 지시' },
  { id: 'e2', source: 'pm_planner', target: 'designer', label: 'SPEC.md' },
  { id: 'e3', source: 'designer', target: 'developer', label: 'DESIGN.md' },
  { id: 'e4', source: 'developer', target: 'security_expert', label: '코드 구현' },
  { id: 'e5', source: 'security_expert', target: 'reviewer', label: 'SECURITY_REPORT' },
  { id: 'e6', source: 'reviewer', target: 'developer', label: 'REJECT', condition: 'on_failure' },
  { id: 'e7', source: 'reviewer', target: 'orchestrator', label: 'PASS', condition: 'on_success' },
]

const webTeams: AgentTeam[] = [
  { id: 'content', name: 'Content Growth', agentIds: ['pm_planner', 'designer'], status: 'idle' },
  { id: 'product', name: 'Product Success', agentIds: ['developer', 'reviewer'], status: 'idle' },
  { id: 'infra', name: 'Data Infra', agentIds: ['security_expert'], status: 'idle' },
]

// ──────────────────────────────────────────
// Android (Kotlin) 프리셋 — 스킬 파일에서 임포트
// ──────────────────────────────────────────

const androidAgents = ANDROID_AGENTS

const androidEdges: EdgeDefinition[] = [
  { id: 'e1', source: 'orchestrator', target: 'pm_planner', label: '기획 지시' },
  { id: 'e2', source: 'pm_planner', target: 'designer', label: 'SPEC.md' },
  { id: 'e3', source: 'designer', target: 'developer', label: 'DESIGN.md' },
  { id: 'e4', source: 'developer', target: 'build_test', label: '코드 구현' },
  { id: 'e5', source: 'build_test', target: 'security_expert', label: '빌드 결과' },
  { id: 'e6', source: 'security_expert', target: 'reviewer', label: 'SECURITY_REPORT' },
  { id: 'e7', source: 'reviewer', target: 'developer', label: 'REJECT', condition: 'on_failure' },
  { id: 'e8', source: 'reviewer', target: 'deployer', label: 'PASS', condition: 'on_success' },
  { id: 'e9', source: 'deployer', target: 'orchestrator', label: '배포 완료' },
]

const androidTeams: AgentTeam[] = [
  { id: 'planning', name: 'Planning & Design', agentIds: ['pm_planner', 'designer'], status: 'idle' },
  { id: 'engineering', name: 'Engineering', agentIds: ['developer', 'build_test'], status: 'idle' },
  { id: 'quality', name: 'Quality & Security', agentIds: ['security_expert', 'reviewer'], status: 'idle' },
  { id: 'release', name: 'Release', agentIds: ['deployer'], status: 'standby' },
]

// ──────────────────────────────────────────
// iOS (Swift) 프리셋 — 스킬 파일에서 임포트
// ──────────────────────────────────────────

const iosAgents = IOS_AGENTS

const iosEdges: EdgeDefinition[] = [
  { id: 'e1', source: 'orchestrator', target: 'pm_planner', label: '기획 지시' },
  { id: 'e2', source: 'pm_planner', target: 'designer', label: 'SPEC.md' },
  { id: 'e3', source: 'designer', target: 'developer', label: 'DESIGN.md' },
  { id: 'e4', source: 'developer', target: 'build_test', label: '코드 구현' },
  { id: 'e5', source: 'build_test', target: 'security_expert', label: '빌드 결과' },
  { id: 'e6', source: 'security_expert', target: 'reviewer', label: 'SECURITY_REPORT' },
  { id: 'e7', source: 'reviewer', target: 'developer', label: 'REJECT', condition: 'on_failure' },
  { id: 'e8', source: 'reviewer', target: 'deployer', label: 'PASS', condition: 'on_success' },
  { id: 'e9', source: 'deployer', target: 'orchestrator', label: '배포 완료' },
]

const iosTeams: AgentTeam[] = [
  { id: 'planning', name: 'Planning & Design', agentIds: ['pm_planner', 'designer'], status: 'idle' },
  { id: 'engineering', name: 'Engineering', agentIds: ['developer', 'build_test'], status: 'idle' },
  { id: 'quality', name: 'Quality & Security', agentIds: ['security_expert', 'reviewer'], status: 'idle' },
  { id: 'release', name: 'Release', agentIds: ['deployer'], status: 'standby' },
]

// ──────────────────────────────────────────
// 프리셋 레지스트리
// ──────────────────────────────────────────

// ──────────────────────────────────────────
// 검증 루프 설정 (플랫폼별)
// 모든 워크플로우에 필수 — 커스텀이어도 반드시 포함
// ──────────────────────────────────────────

const webVerification: VerificationLoopConfig = {
  enabled: true,
  maxIterations: 3,
  codeAgentId: 'developer',
  testCommand: 'npm run build && npx tsc --noEmit',
  lintCommand: 'npm run lint',
  buildCommand: 'npm run build',
}

const androidVerification = ANDROID_VERIFICATION
const iosVerification = IOS_VERIFICATION

export const WORKFLOW_PRESETS: WorkflowTemplate[] = [
  {
    id: 'web-nextjs',
    name: 'Web (Next.js + Supabase)',
    platform: 'web',
    description: '6단계 하네스: PM → Designer → Developer → Security → Reviewer. 검증 루프 내장 (CODE→TEST→FIX→PASS, 최대 3회).',
    isPreset: true,
    agents: webAgents,
    edges: webEdges,
    agentTeams: webTeams,
    verificationLoop: webVerification,
  },
  {
    id: 'android-kotlin',
    name: 'Android (Kotlin + Compose)',
    platform: 'android',
    description: '8단계 + 검증 루프: Gradle 빌드 + 테스트 자동 검증 (최대 3회 반복). Material Design 3.',
    isPreset: true,
    agents: androidAgents,
    edges: androidEdges,
    agentTeams: androidTeams,
    verificationLoop: androidVerification,
  },
  {
    id: 'ios-swift',
    name: 'iOS (Swift + SwiftUI)',
    platform: 'ios',
    description: '8단계 + 검증 루프: Xcode 빌드 + 테스트 자동 검증 (최대 3회 반복). HIG, TestFlight.',
    isPreset: true,
    agents: iosAgents,
    edges: iosEdges,
    agentTeams: iosTeams,
    verificationLoop: iosVerification,
  },
]

// 템플릿에서 런타임 세션 데이터로 변환
export function templateToSession(
  template: WorkflowTemplate,
  projectName: string,
): {
  agents: import('./types').AgentNodeData[]
  edges: import('./types').FlowEdge[]
  agentTeams: import('./types').AgentTeam[]
} {
  const agents = template.agents.map((def) => ({
    id: def.id,
    name: def.name,
    role: def.role,
    state: 'idle' as const,
    color: def.color,
    position: { x: 0, y: 0 },
    tokensUsed: 0,
    eventCount: 0,
    model: def.model,
    description: def.description,
  }))

  const edges = template.edges.map((def) => ({
    id: def.id,
    source: def.source,
    target: def.target,
    label: def.label,
    active: false,
    condition: def.condition,
  }))

  return { agents, edges, agentTeams: template.agentTeams }
}
