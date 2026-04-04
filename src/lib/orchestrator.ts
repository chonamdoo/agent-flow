import type { AgentRole, AgentEvent, AgentResult, TokenUsage, WorkflowExecution } from './types'
import { WORKFLOW_SEQUENCE } from './agent-prompts'

type EventCallback = (event: AgentEvent & {
  agentStateUpdate?: { agentId: string; state: string; tokensUsed?: number }
  edgeActivation?: { edgeId: string; active: boolean }
}) => void

type StreamingCallback = (agentId: string, text: string) => void

interface OrchestratorOptions {
  userPrompt: string
  projectId: string
  projectPath?: string
  onEvent: EventCallback
  onStreaming?: StreamingCallback
  onComplete?: (execution: WorkflowExecution) => void
  onError?: (error: string) => void
  costLimit?: number
  signal?: AbortSignal
}

function uid(): string {
  return Math.random().toString(36).slice(2, 10)
}

function findEdgeId(source: string, target: string): string {
  return `e-${source}-${target}`
}

export async function runWorkflow(opts: OrchestratorOptions): Promise<WorkflowExecution> {
  const {
    userPrompt,
    projectId,
    projectPath,
    onEvent,
    onStreaming,
    onComplete,
    onError,
    costLimit = 5,
    signal,
  } = opts

  const execution: WorkflowExecution = {
    id: uid(),
    userPrompt,
    projectId,
    results: [],
    status: 'running',
    currentAgent: null,
    totalCost: 0,
    startedAt: Date.now(),
  }

  const previousOutputs: Array<{ role: AgentRole; output: string }> = []

  // Orchestrator 시작 이벤트
  emitEvent(onEvent, {
    type: 'agent_spawn',
    agentId: 'orchestrator',
    agentName: 'Orchestrator',
    payload: { message: '하네스 워크플로우 시작' },
    agentStateUpdate: { agentId: 'orchestrator', state: 'running' },
  })

  for (const role of WORKFLOW_SEQUENCE) {
    if (signal?.aborted) {
      execution.status = 'stopped'
      break
    }

    // 비용 가드
    if (execution.totalCost >= costLimit) {
      const msg = `비용 한도 초과 ($${execution.totalCost.toFixed(2)} / $${costLimit}). 파이프라인 중단.`
      onError?.(msg)
      execution.status = 'failed'
      break
    }

    execution.currentAgent = role
    const agentId = role
    const agentName = getAgentDisplayName(role)

    // 핸드오프 이벤트
    const prevAgent = previousOutputs.length > 0
      ? previousOutputs[previousOutputs.length - 1].role
      : 'orchestrator'

    emitEvent(onEvent, {
      type: 'handoff',
      agentId: prevAgent,
      agentName: getAgentDisplayName(prevAgent as AgentRole),
      payload: { message: `${agentName}에게 전달` },
      edgeActivation: { edgeId: findEdgeId(prevAgent, agentId), active: true },
    })

    // 에이전트 시작
    emitEvent(onEvent, {
      type: 'agent_spawn',
      agentId,
      agentName,
      payload: { message: `${agentName} 작업 시작` },
      agentStateUpdate: { agentId, state: 'running' },
    })

    // thinking 이벤트
    await delay(300)
    emitEvent(onEvent, {
      type: 'thinking',
      agentId,
      agentName,
      payload: { message: `${agentName} 분석 중...` },
      agentStateUpdate: { agentId, state: 'thinking' },
    })

    // CLI 호출
    const result = await callAgent(role, userPrompt, projectId, previousOutputs, signal, (text) => {
      onStreaming?.(agentId, text)
    }, projectPath)

    if (result.status === 'error') {
      emitEvent(onEvent, {
        type: 'agent_error',
        agentId,
        agentName,
        payload: { message: `에러: ${result.errorMessage}` },
        agentStateUpdate: { agentId, state: 'error' },
        edgeActivation: { edgeId: findEdgeId(prevAgent, agentId), active: false },
      })

      // Reviewer가 아닌 에이전트 에러 → 파이프라인 중단
      if (role !== 'reviewer') {
        execution.status = 'failed'
        execution.results.push(result)
        onError?.(result.errorMessage ?? 'Unknown error')
        break
      }
    }

    execution.results.push(result)
    execution.totalCost += result.usage.costUsd
    previousOutputs.push({ role, output: result.output })

    // 에이전트 완료
    emitEvent(onEvent, {
      type: 'agent_complete',
      agentId,
      agentName,
      payload: {
        message: `${agentName} 완료`,
        tokensUsed: result.usage.inputTokens + result.usage.outputTokens,
        output: result.output,
      },
      agentStateUpdate: {
        agentId,
        state: 'completed',
        tokensUsed: result.usage.inputTokens + result.usage.outputTokens,
      },
      edgeActivation: { edgeId: findEdgeId(prevAgent, agentId), active: false },
    })

    // Reviewer REJECT 처리 — developer 재실행 (최대 2회 추가)
    if (role === 'reviewer' && isReject(result.output)) {
      const retries = execution.results.filter((r) => r.role === 'developer').length
      if (retries < 3) {
        // developer와 security_expert를 재실행하기 위해 시퀀스에 추가
        emitEvent(onEvent, {
          type: 'message',
          agentId: 'reviewer',
          agentName: 'Reviewer',
          payload: { message: `REJECT — Developer에게 피드백 전달 (재시도 ${retries + 1}/3)` },
        })

        // developer부터 다시 실행 — WORKFLOW_SEQUENCE 끝에서 루프
        const rerunSequence: AgentRole[] = ['developer', 'security_expert', 'reviewer']
        for (const rerunRole of rerunSequence) {
          if (signal?.aborted) break
          if (execution.totalCost >= costLimit) break

          execution.currentAgent = rerunRole
          const reId = rerunRole
          const reName = getAgentDisplayName(rerunRole)

          emitEvent(onEvent, {
            type: 'agent_spawn',
            agentId: reId,
            agentName: reName,
            payload: { message: `${reName} 재실행 (피드백 반영)` },
            agentStateUpdate: { agentId: reId, state: 'running' },
          })

          emitEvent(onEvent, {
            type: 'thinking',
            agentId: reId,
            agentName: reName,
            payload: { message: `${reName} 피드백 분석 중...` },
            agentStateUpdate: { agentId: reId, state: 'thinking' },
          })

          const reResult = await callAgent(
            rerunRole, userPrompt, projectId, previousOutputs, signal,
            (text) => onStreaming?.(reId, text), projectPath,
          )

          execution.results.push(reResult)
          execution.totalCost += reResult.usage.costUsd
          // 이전 같은 role 출력 대체
          const existingIdx = previousOutputs.findIndex((o) => o.role === rerunRole)
          if (existingIdx >= 0) {
            previousOutputs[existingIdx] = { role: rerunRole, output: reResult.output }
          } else {
            previousOutputs.push({ role: rerunRole, output: reResult.output })
          }

          emitEvent(onEvent, {
            type: 'agent_complete',
            agentId: reId,
            agentName: reName,
            payload: {
              message: `${reName} 완료`,
              tokensUsed: reResult.usage.inputTokens + reResult.usage.outputTokens,
              output: reResult.output,
            },
            agentStateUpdate: {
              agentId: reId,
              state: 'completed',
              tokensUsed: reResult.usage.inputTokens + reResult.usage.outputTokens,
            },
          })
        }
      }
    }
  }

  // Orchestrator 완료
  if (execution.status === 'running') {
    execution.status = 'completed'
  }
  execution.currentAgent = null
  execution.completedAt = Date.now()

  const totalTokens = execution.results.reduce(
    (sum, r) => sum + r.usage.inputTokens + r.usage.outputTokens, 0,
  )

  emitEvent(onEvent, {
    type: 'agent_complete',
    agentId: 'orchestrator',
    agentName: 'Orchestrator',
    payload: { message: `워크플로우 ${execution.status === 'completed' ? '완료' : '중단'}` },
    agentStateUpdate: {
      agentId: 'orchestrator',
      state: execution.status === 'completed' ? 'completed' : 'error',
      tokensUsed: totalTokens,
    },
  })

  onComplete?.(execution)
  return execution
}

// ──────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────

async function callAgent(
  role: AgentRole,
  userPrompt: string,
  projectId: string,
  previousOutputs: Array<{ role: AgentRole; output: string }>,
  signal?: AbortSignal,
  onText?: (text: string) => void,
  projectPath?: string,
): Promise<AgentResult> {
  const startTime = Date.now()

  try {
    const response = await fetch('/api/agent/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role, userPrompt, projectId, projectPath, previousOutputs }),
      signal,
    })

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({ error: 'Request failed' }))
      return {
        role,
        output: '',
        usage: { inputTokens: 0, outputTokens: 0, model: '', costUsd: 0 },
        durationMs: Date.now() - startTime,
        status: 'error',
        errorMessage: errorData.error ?? `HTTP ${response.status}`,
      }
    }

    const reader = response.body?.getReader()
    if (!reader) {
      return {
        role,
        output: '',
        usage: { inputTokens: 0, outputTokens: 0, model: '', costUsd: 0 },
        durationMs: Date.now() - startTime,
        status: 'error',
        errorMessage: 'No response body',
      }
    }

    const decoder = new TextDecoder()
    let buffer = ''
    let fullOutput = ''
    let usage: TokenUsage = { inputTokens: 0, outputTokens: 0, model: '', costUsd: 0 }
    let finalDurationMs = 0

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })

      const lines = buffer.split('\n\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const jsonStr = line.slice(6)

        try {
          const data = JSON.parse(jsonStr)

          if (data.type === 'text_delta') {
            onText?.(data.text)
          } else if (data.type === 'complete') {
            fullOutput = data.output
            usage = data.usage
            finalDurationMs = data.durationMs
          } else if (data.type === 'error') {
            return {
              role,
              output: '',
              usage,
              durationMs: Date.now() - startTime,
              status: 'error',
              errorMessage: data.error,
            }
          }
        } catch {
          // JSON 파싱 에러 무시
        }
      }
    }

    return {
      role,
      output: fullOutput,
      usage,
      durationMs: finalDurationMs || (Date.now() - startTime),
      status: 'success',
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Unknown error'
    if (message.includes('abort')) {
      return {
        role,
        output: '',
        usage: { inputTokens: 0, outputTokens: 0, model: '', costUsd: 0 },
        durationMs: Date.now() - startTime,
        status: 'error',
        errorMessage: '사용자가 중단했습니다.',
      }
    }
    return {
      role,
      output: '',
      usage: { inputTokens: 0, outputTokens: 0, model: '', costUsd: 0 },
      durationMs: Date.now() - startTime,
      status: 'error',
      errorMessage: message,
    }
  }
}

function emitEvent(
  callback: EventCallback,
  event: Omit<AgentEvent, 'id' | 'timestamp'> & {
    agentStateUpdate?: { agentId: string; state: string; tokensUsed?: number }
    edgeActivation?: { edgeId: string; active: boolean }
  },
) {
  callback({
    id: uid(),
    timestamp: Date.now(),
    ...event,
  })
}

function getAgentDisplayName(role: AgentRole | string): string {
  const names: Record<string, string> = {
    orchestrator: 'Orchestrator',
    pm_planner: 'PM/Planner',
    designer: 'Designer',
    developer: 'Developer',
    security_expert: 'Security Expert',
    reviewer: 'Reviewer',
  }
  return names[role] ?? role
}

function isReject(output: string): boolean {
  const lowerOutput = output.toLowerCase()
  return lowerOutput.includes('reject') || lowerOutput.includes('반려')
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
