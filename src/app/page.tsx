'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import type { Session, WorkflowTemplate, VerificationLoop, WorkflowExecution } from '@/lib/types'
import { createSessionFromTemplate } from '@/lib/mock-data'
import { useEventStream } from '@/hooks/useEventStream'
import { runWorkflow } from '@/lib/orchestrator'
import { WORKFLOW_PRESETS } from '@/lib/workflow-templates'
import FlowCanvas from '@/components/FlowCanvas'
import Sidebar from '@/components/Sidebar'
import TopBar from '@/components/TopBar'
import AgentDetailCard from '@/components/AgentDetailCard'
import VerificationLoopPanel from '@/components/VerificationLoopPanel'
import PromptInput from '@/components/PromptInput'
import Dashboard from '@/components/Dashboard'

type AppView = 'dashboard' | 'prompt' | 'running'

export default function Home() {
  const [view, setView] = useState<AppView>('dashboard')
  const [currentProject, setCurrentProject] = useState<string>('trading-journal')
  const [currentProjectPath, setCurrentProjectPath] = useState<string>('')
  const [session, setSession] = useState<Session | null>(null)
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
  const [isRunning, setIsRunning] = useState(false)
  const [streamingText, setStreamingText] = useState<Record<string, string>>({})
  const [execution, setExecution] = useState<WorkflowExecution | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  // Web 프리셋 기본 사용
  const template = WORKFLOW_PRESETS[0]

  // 이벤트 스트림 (real 모드 전용)
  const { events, updateAgents, updateEdges, pushEvent, clearEvents } = useEventStream(
    undefined, 'real',
  )

  // 대시보드에서 "하네스 실행" 클릭
  const handleRunHarness = useCallback((projectId: string, projectPath: string) => {
    setCurrentProject(projectId)
    setCurrentProjectPath(projectPath)
    setView('prompt')
  }, [])

  // 프롬프트 제출 → 하네스 실행
  const handlePromptSubmit = useCallback(async (
    userPrompt: string,
    projectId: string,
    projectPath: string,
  ) => {
    if (isRunning) return

    setCurrentProject(projectId)
    setCurrentProjectPath(projectPath)
    setView('running')
    setIsRunning(true)
    clearEvents()
    setStreamingText({})
    setExecution(null)

    const newSession = createSessionFromTemplate(template, projectId)
    setSession(newSession)

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const result = await runWorkflow({
        userPrompt,
        projectId,
        projectPath: projectPath || undefined,
        onEvent: pushEvent,
        onStreaming: (agentId, text) => {
          setStreamingText((prev) => ({
            ...prev,
            [agentId]: (prev[agentId] ?? '') + text,
          }))
        },
        onComplete: (exec) => {
          setExecution(exec)
          setIsRunning(false)
        },
        onError: () => {
          setIsRunning(false)
        },
        costLimit: 5,
        signal: controller.signal,
      })

      setExecution(result)
    } catch {
      setIsRunning(false)
    }
  }, [isRunning, clearEvents, pushEvent, template])

  // 중지
  const handleStop = useCallback(() => {
    abortRef.current?.abort()
    setIsRunning(false)
  }, [])

  // 이벤트 → 에이전트 상태 동기화
  useEffect(() => {
    if (!session) return

    setSession((prev) => {
      if (!prev) return prev
      const newAgents = updateAgents(prev.agents)
      const newEdges = updateEdges(prev.edges)
      if (newAgents === prev.agents && newEdges === prev.edges) return prev

      const allComplete = newAgents.every(
        (a) => a.state === 'completed' || a.state === 'idle',
      )
      const hasError = newAgents.some((a) => a.state === 'error')
      const status = hasError
        ? 'failed'
        : allComplete && events.length > 0
          ? 'completed'
          : 'active'

      const totalCost = execution
        ? execution.totalCost
        : (newAgents.reduce((sum, a) => sum + a.tokensUsed, 0) / 1000) * 0.003

      const verificationLoop = deriveVerificationLoop(prev.verificationLoop, events)

      const agentsWithOutput = newAgents.map((a) => {
        const streamText = streamingText[a.id]
        const execResult = execution?.results.find((r) => r.role === a.role)
        const output = execResult?.output ?? streamText ?? a.output
        return output !== a.output ? { ...a, output } : a
      })

      return {
        ...prev,
        agents: agentsWithOutput,
        edges: newEdges,
        events,
        status,
        totalCost,
        verificationLoop,
      }
    })
  }, [events, updateAgents, updateEdges, session !== null, execution, streamingText])

  // 경과 시간 타이머
  useEffect(() => {
    timerRef.current = setInterval(() => {
      setSession((prev) => (prev ? { ...prev } : null))
    }, 1000)
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [])

  const selectedAgent = selectedAgentId && session
    ? session.agents.find((a) => a.id === selectedAgentId) ?? null
    : null

  const handleSelectAgent = useCallback((id: string | null) => {
    setSelectedAgentId(id)
  }, [])

  // ──────── 뷰 라우팅 ────────

  // 1. 대시보드
  if (view === 'dashboard') {
    return <Dashboard onRunHarness={handleRunHarness} />
  }

  // 2. 프롬프트 입력
  if (view === 'prompt') {
    return (
      <PromptInput
        projectId={currentProject}
        projectPath={currentProjectPath}
        onSubmit={handlePromptSubmit}
        onBack={() => setView('dashboard')}
        isRunning={isRunning}
      />
    )
  }

  // 3. 실행 화면 (FlowCanvas)
  if (!session) {
    return <Dashboard onRunHarness={handleRunHarness} />
  }

  return (
    <div className="flex h-screen flex-col bg-[#09090b]">
      <TopBar session={session} events={events} />

      <div className="flex flex-1 overflow-hidden">
        <Sidebar
          agents={session.agents}
          events={events}
          agentTeams={session.agentTeams}
          selectedAgentId={selectedAgentId}
          onSelectAgent={handleSelectAgent}
        />

        <div className="relative flex-1">
          <FlowCanvas
            agents={session.agents}
            edges={session.edges}
            selectedAgentId={selectedAgentId}
            onSelectAgent={handleSelectAgent}
            onUpdateNodeState={null}
          />

          {selectedAgent && (
            <AgentDetailCard
              agent={selectedAgent}
              events={events}
              onClose={() => setSelectedAgentId(null)}
            />
          )}

          <div className="absolute bottom-16 right-4 w-56">
            <VerificationLoopPanel
              loop={session.verificationLoop}
              config={template.verificationLoop}
            />
          </div>

          {/* 좌측 상단: 컨트롤 */}
          <div className="absolute top-4 left-4 flex items-center gap-2">
            <button
              onClick={() => setView('dashboard')}
              className="flex items-center gap-1.5 rounded-lg bg-zinc-900/80 border border-zinc-800 px-3 py-1.5 text-xs text-zinc-400 hover:text-white hover:border-zinc-700 transition-colors"
            >
              ← 대시보드
            </button>

            {isRunning && (
              <button
                onClick={handleStop}
                className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-1.5 text-xs font-medium text-red-400 hover:bg-red-500/20 transition-colors"
              >
                중지
              </button>
            )}

            {!isRunning && session.status !== 'active' && (
              <button
                onClick={() => setView('prompt')}
                className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-1.5 text-xs font-medium text-emerald-400 hover:bg-emerald-500/20 transition-colors"
              >
                + 새 실행
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function deriveVerificationLoop(
  prev: VerificationLoop,
  events: { payload: Record<string, unknown> }[],
): VerificationLoop {
  let loop = { ...prev, history: [...prev.history] }

  for (const event of events) {
    const msg = typeof event.payload.message === 'string' ? event.payload.message : ''

    if (msg.includes('[검증 루프') && msg.includes('CODE:')) {
      const roundMatch = msg.match(/(\d+)\/(\d+)/)
      const round = roundMatch ? parseInt(roundMatch[1], 10) : loop.currentIteration + 1
      loop = { ...loop, status: 'coding', currentIteration: round }
    } else if (msg.includes('[검증 루프') && msg.includes('TEST:')) {
      loop = { ...loop, status: 'testing' }
    } else if (msg.includes('TEST FAIL:')) {
      const roundMatch = msg.match(/(\d+)\/(\d+)/)
      const round = roundMatch ? parseInt(roundMatch[1], 10) : loop.currentIteration
      const errors = msg.split(':').slice(-1)[0]?.trim() ?? ''
      loop.history = [
        ...loop.history.filter((h) => h.round !== round),
        { round, codeAgentId: 'developer', testResult: 'fail', errors: [errors], fixApplied: false, timestamp: Date.now() },
      ]
    } else if (msg.includes('[검증 루프') && msg.includes('FIX:')) {
      loop = { ...loop, status: 'fixing' }
      const lastRound = loop.history[loop.history.length - 1]
      if (lastRound) {
        loop.history = [...loop.history.slice(0, -1), { ...lastRound, fixApplied: true }]
      }
    } else if (msg.includes('TEST PASS:')) {
      const roundMatch = msg.match(/(\d+)\/(\d+)/)
      const round = roundMatch ? parseInt(roundMatch[1], 10) : loop.currentIteration
      loop.history = [
        ...loop.history.filter((h) => h.round !== round),
        { round, codeAgentId: 'developer', testResult: 'pass', errors: [], fixApplied: false, timestamp: Date.now() },
      ]
    } else if (msg.includes('[검증 루프 PASS]')) {
      loop = { ...loop, status: 'passed' }
    }
  }

  return loop
}
