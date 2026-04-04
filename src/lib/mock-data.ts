import type { AgentNodeData, FlowEdge, AgentEvent, Session, AgentTeam } from './types'
import type { WorkflowTemplate } from './types'
import { templateToSession } from './workflow-templates'

function uid(): string {
  return Math.random().toString(36).slice(2, 10)
}

export function createSessionFromTemplate(
  template: WorkflowTemplate,
  projectName: string,
): Session {
  const { agents, edges, agentTeams } = templateToSession(template, projectName)

  // 오케스트레이터 초기 상태
  const initialAgents = agents.map((a) =>
    a.role === 'orchestrator'
      ? { ...a, state: 'running' as const, eventCount: 218 }
      : a,
  )

  return {
    id: uid(),
    projectName,
    platform: template.platform,
    templateId: template.id,
    agents: initialAgents,
    edges,
    events: [],
    status: 'active',
    startedAt: Date.now(),
    totalCost: 0,
    agentTeams,
    verificationLoop: {
      maxIterations: template.verificationLoop.maxIterations,
      currentIteration: 0,
      status: 'idle',
      history: [],
    },
  }
}

// 시뮬레이션 이벤트 시퀀스를 템플릿 기반으로 생성
export interface ScheduledEvent {
  delayMs: number
  event: Omit<AgentEvent, 'id' | 'timestamp'>
  agentStateUpdate?: { agentId: string; state: AgentNodeData['state']; tokensUsed?: number }
  edgeActivation?: { edgeId: string; active: boolean }
}

export function createSimulationFromTemplate(template: WorkflowTemplate): ScheduledEvent[] {
  const events: ScheduledEvent[] = []
  const orchestrator = template.agents.find((a) => a.role === 'orchestrator')
  if (!orchestrator) return events

  // 오케스트레이터 → 순차적 에이전트 실행
  const nonOrchAgents = template.agents.filter((a) => a.role !== 'orchestrator')
  let delay = 0

  // 오케스트레이터 시작
  events.push({
    delayMs: delay,
    event: {
      type: 'agent_spawn',
      agentId: orchestrator.id,
      agentName: orchestrator.name,
      payload: { message: '워크플로우 시작' },
    },
    agentStateUpdate: { agentId: orchestrator.id, state: 'running' },
  })

  for (let i = 0; i < nonOrchAgents.length; i++) {
    const agent = nonOrchAgents[i]
    const prevAgent = i > 0 ? nonOrchAgents[i - 1] : orchestrator

    // 핸드오프 에지 찾기
    const edge = template.edges.find(
      (e) => e.source === prevAgent.id && e.target === agent.id,
    )

    delay += 1500

    // 핸드오프
    if (edge) {
      events.push({
        delayMs: delay,
        event: {
          type: 'handoff',
          agentId: prevAgent.id,
          agentName: prevAgent.name,
          payload: { message: `${agent.name}에게 전달` },
        },
        edgeActivation: { edgeId: edge.id, active: true },
      })
    }

    delay += 500

    // 에이전트 시작
    events.push({
      delayMs: delay,
      event: {
        type: 'agent_spawn',
        agentId: agent.id,
        agentName: agent.name,
        payload: { message: `${agent.description} 시작` },
      },
      agentStateUpdate: { agentId: agent.id, state: 'running' },
    })

    delay += 1500

    // thinking
    events.push({
      delayMs: delay,
      event: {
        type: 'thinking',
        agentId: agent.id,
        agentName: agent.name,
        payload: { message: `${agent.description} 진행 중...` },
      },
      agentStateUpdate: { agentId: agent.id, state: 'thinking', tokensUsed: 2000 + i * 1500 },
    })

    // developer는 검증 루프 시뮬레이션 (CODE→TEST→FIX→PASS)
    if (agent.role === 'developer') {
      // ── Round 1: CODE ──
      delay += 2000
      events.push({
        delayMs: delay,
        event: {
          type: 'tool_call_start',
          agentId: agent.id,
          agentName: agent.name,
          payload: { tool: 'Edit', message: '[검증 루프 1/3] CODE: 코드 작성 중...' },
        },
        agentStateUpdate: { agentId: agent.id, state: 'tool_calling', tokensUsed: 8000 },
      })
      delay += 2000

      // ── Round 1: TEST → FAIL ──
      events.push({
        delayMs: delay,
        event: {
          type: 'tool_call_start',
          agentId: agent.id,
          agentName: agent.name,
          payload: { tool: 'Bash', message: '[검증 루프 1/3] TEST: 빌드 + 테스트 실행...' },
        },
        agentStateUpdate: { agentId: agent.id, state: 'tool_calling', tokensUsed: 10000 },
      })
      delay += 1500
      events.push({
        delayMs: delay,
        event: {
          type: 'tool_call_end',
          agentId: agent.id,
          agentName: agent.name,
          payload: { tool: 'Bash', message: '[검증 루프 1/3] TEST FAIL: 타입 에러 2건 발견' },
        },
        agentStateUpdate: { agentId: agent.id, state: 'running', tokensUsed: 11000 },
      })

      // ── Round 1: FIX ──
      delay += 1500
      events.push({
        delayMs: delay,
        event: {
          type: 'tool_call_start',
          agentId: agent.id,
          agentName: agent.name,
          payload: { tool: 'Edit', message: '[검증 루프 1/3] FIX: 타입 에러 수정 중...' },
        },
        agentStateUpdate: { agentId: agent.id, state: 'tool_calling', tokensUsed: 13000 },
      })
      delay += 2000

      // ── Round 2: TEST → PASS ──
      events.push({
        delayMs: delay,
        event: {
          type: 'tool_call_start',
          agentId: agent.id,
          agentName: agent.name,
          payload: { tool: 'Bash', message: '[검증 루프 2/3] TEST: 빌드 + 테스트 재실행...' },
        },
        agentStateUpdate: { agentId: agent.id, state: 'tool_calling', tokensUsed: 15000 },
      })
      delay += 1500
      events.push({
        delayMs: delay,
        event: {
          type: 'tool_call_end',
          agentId: agent.id,
          agentName: agent.name,
          payload: { tool: 'Bash', message: '[검증 루프 2/3] TEST PASS: 빌드 성공, 모든 테스트 통과' },
        },
        agentStateUpdate: { agentId: agent.id, state: 'running', tokensUsed: 18000 },
      })

      // ── PASS 완료 ──
      delay += 1000
      events.push({
        delayMs: delay,
        event: {
          type: 'message',
          agentId: agent.id,
          agentName: agent.name,
          payload: { message: '[검증 루프 PASS] 2회 만에 검증 완료 — 품질 2x 향상' },
        },
        agentStateUpdate: { agentId: agent.id, state: 'running', tokensUsed: 20000 },
      })
    }

    // build_test는 빌드 시뮬레이션
    if (agent.role === 'build_test') {
      delay += 1500
      events.push({
        delayMs: delay,
        event: {
          type: 'tool_call_start',
          agentId: agent.id,
          agentName: agent.name,
          payload: { tool: 'Bash', message: '빌드 실행 중...' },
        },
        agentStateUpdate: { agentId: agent.id, state: 'tool_calling', tokensUsed: 3000 },
      })
      delay += 2000
      events.push({
        delayMs: delay,
        event: {
          type: 'tool_call_end',
          agentId: agent.id,
          agentName: agent.name,
          payload: { tool: 'Bash', message: '빌드 성공' },
        },
        agentStateUpdate: { agentId: agent.id, state: 'running', tokensUsed: 5000 },
      })
    }

    delay += 2000

    // 에이전트 완료
    const tokensFinal = agent.role === 'developer' ? 24000 : agent.role === 'build_test' ? 6000 : 5000 + i * 1200
    events.push({
      delayMs: delay,
      event: {
        type: 'agent_complete',
        agentId: agent.id,
        agentName: agent.name,
        payload: { message: `${agent.name} 작업 완료`, tokensUsed: tokensFinal },
      },
      agentStateUpdate: { agentId: agent.id, state: 'completed', tokensUsed: tokensFinal },
      edgeActivation: edge ? { edgeId: edge.id, active: false } : undefined,
    })
  }

  // 마지막 에이전트 → 오케스트레이터 완료
  const lastAgent = nonOrchAgents[nonOrchAgents.length - 1]
  const finalEdge = template.edges.find(
    (e) => e.source === lastAgent.id && e.target === orchestrator.id,
  ) ?? template.edges.find(
    (e) => e.target === orchestrator.id && e.condition === 'on_success',
  )

  delay += 1000
  if (finalEdge) {
    events.push({
      delayMs: delay,
      event: {
        type: 'handoff',
        agentId: lastAgent.id,
        agentName: lastAgent.name,
        payload: { message: 'Orchestrator에게 결과 전달' },
      },
      edgeActivation: { edgeId: finalEdge.id, active: true },
    })
  }

  delay += 1000
  events.push({
    delayMs: delay,
    event: {
      type: 'agent_complete',
      agentId: orchestrator.id,
      agentName: orchestrator.name,
      payload: { message: '워크플로우 완료' },
    },
    agentStateUpdate: { agentId: orchestrator.id, state: 'completed', tokensUsed: 55200 },
    edgeActivation: finalEdge ? { edgeId: finalEdge.id, active: false } : undefined,
  })

  return events
}
