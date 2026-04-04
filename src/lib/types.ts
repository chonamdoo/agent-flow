// 에이전트 역할 — 플랫폼 공통 + 플랫폼별 확장
export type AgentRole =
  | 'orchestrator'
  | 'pm_planner'
  | 'designer'
  | 'developer'
  | 'security_expert'
  | 'reviewer'
  | 'build_test'
  | 'deployer'
  | 'custom'

export const AGENT_ROLE_META: Record<AgentRole, { label: string; color: string; icon: string }> = {
  orchestrator: { label: 'Orchestrator', color: '#f43f5e', icon: '●' },
  pm_planner: { label: 'PM/Planner', color: '#8b5cf6', icon: '📋' },
  designer: { label: 'Designer', color: '#ec4899', icon: '🎨' },
  developer: { label: 'Developer', color: '#34d399', icon: '⚙️' },
  security_expert: { label: 'Security', color: '#f59e0b', icon: '🔒' },
  reviewer: { label: 'Reviewer', color: '#3b82f6', icon: '📝' },
  build_test: { label: 'Build & Test', color: '#06b6d4', icon: '🔨' },
  deployer: { label: 'Deployer', color: '#10b981', icon: '🚀' },
  custom: { label: 'Custom', color: '#a855f7', icon: '⚡' },
}

// 에이전트 상태
export type AgentState =
  | 'idle'
  | 'running'
  | 'thinking'
  | 'tool_calling'
  | 'completed'
  | 'error'
  | 'waiting'

export const AGENT_STATE_COLORS: Record<AgentState, string> = {
  idle: '#52525b',
  running: '#34d399',
  thinking: '#a78bfa',
  tool_calling: '#fbbf24',
  completed: '#22c55e',
  error: '#ef4444',
  waiting: '#94a3b8',
}

// 플랫폼 타입
export type Platform = 'web' | 'android' | 'ios'

export const PLATFORM_META: Record<Platform, { label: string; icon: string; color: string }> = {
  web: { label: 'Web (Next.js)', icon: '🌐', color: '#34d399' },
  android: { label: 'Android (Kotlin)', icon: '🤖', color: '#a4c639' },
  ios: { label: 'iOS (Swift)', icon: '🍎', color: '#007aff' },
}

// 에이전트 정의 (템플릿용)
export interface AgentDefinition {
  id: string
  name: string
  role: AgentRole
  color: string
  description: string
  systemPrompt: string
  tools: string[]
  model: string
}

// 에지 정의 (템플릿용)
export interface EdgeDefinition {
  id: string
  source: string
  target: string
  label?: string
  condition?: 'on_success' | 'on_failure' | 'always'
}

// ──────────────────────────────────────────
// 검증 루프 (CODE→TEST→FIX→PASS)
// 모든 워크플로우에 필수 내장
// ──────────────────────────────────────────

export interface VerificationLoop {
  maxIterations: number             // 최대 반복 횟수 (기본 3)
  currentIteration: number          // 현재 반복
  status: 'idle' | 'coding' | 'testing' | 'fixing' | 'passed' | 'failed'
  history: VerificationRound[]
}

export interface VerificationRound {
  round: number
  codeAgentId: string               // 코드 작성 에이전트
  testResult: 'pass' | 'fail' | 'pending'
  errors: string[]
  fixApplied: boolean
  timestamp: number
}

// 워크플로우 템플릿
export interface WorkflowTemplate {
  id: string
  name: string
  platform: Platform
  description: string
  isPreset: boolean
  agents: AgentDefinition[]
  edges: EdgeDefinition[]
  agentTeams: AgentTeam[]
  verificationLoop: VerificationLoopConfig
  // 영속성
  projectPath?: string              // 저장된 프로젝트 경로
  savedAt?: number                  // 저장 시각
}

export interface VerificationLoopConfig {
  enabled: boolean                  // 항상 true (필수)
  maxIterations: number             // 기본 3
  codeAgentId: string               // CODE 단계 에이전트
  testCommand: string               // TEST 명령어
  lintCommand?: string              // LINT 명령어
  buildCommand?: string             // BUILD 명령어
}

// 에이전트 팀
export interface AgentTeam {
  id: string
  name: string
  agentIds: string[]
  status: 'active' | 'standby' | 'idle'
}

// 에이전트 노드 (런타임)
export interface AgentNodeData {
  id: string
  name: string
  role: AgentRole
  state: AgentState
  parentId?: string
  color: string
  position: { x: number; y: number }
  tokensUsed: number
  eventCount: number
  startedAt?: number
  completedAt?: number
  output?: string
  model?: string
  description?: string
}

// 에이전트 간 연결 (런타임)
export interface FlowEdge {
  id: string
  source: string
  target: string
  label?: string
  active: boolean
  condition?: 'on_success' | 'on_failure' | 'always'
}

// 이벤트 타입
export type AgentEventType =
  | 'agent_spawn'
  | 'agent_complete'
  | 'agent_error'
  | 'tool_call_start'
  | 'tool_call_end'
  | 'message'
  | 'thinking'
  | 'handoff'

// 에이전트 이벤트
export interface AgentEvent {
  id: string
  timestamp: number
  type: AgentEventType
  agentId: string
  agentName: string
  payload: Record<string, unknown>
}

// 세션
export interface Session {
  id: string
  projectName: string
  platform: Platform
  templateId: string
  agents: AgentNodeData[]
  edges: FlowEdge[]
  events: AgentEvent[]
  status: 'active' | 'completed' | 'failed'
  startedAt: number
  totalCost: number
  agentTeams: AgentTeam[]
  verificationLoop: VerificationLoop
}

// ──────────────────────────────────────────
// SDK 실행 관련 타입
// ──────────────────────────────────────────

export interface TokenUsage {
  inputTokens: number
  outputTokens: number
  model: string
  costUsd: number
}

export interface AgentResult {
  role: AgentRole
  output: string
  usage: TokenUsage
  durationMs: number
  status: 'success' | 'error'
  errorMessage?: string
}

export interface WorkflowExecution {
  id: string
  userPrompt: string
  projectId: string
  results: AgentResult[]
  status: 'idle' | 'running' | 'completed' | 'failed' | 'stopped'
  currentAgent: AgentRole | null
  totalCost: number
  startedAt: number
  completedAt?: number
}

// 모델별 토큰 단가 (per million tokens)
export const MODEL_PRICING: Record<string, { input: number; output: number }> = {
  'claude-opus-4-6-20250514': { input: 15, output: 75 },
  'claude-sonnet-4-6-20250514': { input: 3, output: 15 },
  'claude-haiku-4-5-20251001': { input: 0.8, output: 4 },
}

export function calculateCost(
  inputTokens: number,
  outputTokens: number,
  model: string,
): number {
  const pricing = MODEL_PRICING[model] ?? MODEL_PRICING['claude-sonnet-4-6-20250514']
  return (inputTokens * pricing.input + outputTokens * pricing.output) / 1_000_000
}

// ──────────────────────────────────────────

// D3-force 시뮬레이션용 노드
export interface SimNode extends AgentNodeData {
  x: number
  y: number
  fx?: number | null
  fy?: number | null
  vx?: number
  vy?: number
  index?: number
}

// D3-force 시뮬레이션용 링크
export interface SimLink {
  source: string | SimNode
  target: string | SimNode
  label?: string
  active: boolean
}
