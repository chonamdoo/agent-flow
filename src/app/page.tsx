'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import type { Session, WorkflowTemplate, VerificationLoop, WorkflowExecution } from '@/lib/types'
import { createSessionFromTemplate } from '@/lib/mock-data'
import { useEventStream } from '@/hooks/useEventStream'
import { runWorkflow } from '@/lib/orchestrator'
import { saveWorkflow, setProjectWorkflow, getProjectWorkflow, loadWorkflow } from '@/lib/workflow-storage'
import FlowCanvas from '@/components/FlowCanvas'
import Sidebar from '@/components/Sidebar'
import TopBar from '@/components/TopBar'
import AgentDetailCard from '@/components/AgentDetailCard'
import TemplateSelector from '@/components/TemplateSelector'
import VerificationLoopPanel from '@/components/VerificationLoopPanel'
import PromptInput from '@/components/PromptInput'

type AppMode = 'mock' | 'real'

export default function Home() {
  const [selectedTemplate, setSelectedTemplate] = useState<WorkflowTemplate | null>(null)
  const [currentProject, setCurrentProject] = useState<string>('trading-journal')
  const [currentProjectPath, setCurrentProjectPath] = useState<string>('')
  const [session, setSession] = useState<Session | null>(null)
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
  const [showSelector, setShowSelector] = useState(true)
  const [showPromptInput, setShowPromptInput] = useState(false)
  const [mode, setMode] = useState<AppMode>('real')
  const [isRunning, setIsRunning] = useState(false)
  const [streamingText, setStreamingText] = useState<Record<string, string>>({})
  const [execution, setExecution] = useState<WorkflowExecution | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  // 이벤트 스트림 (mock/real 이중 모드)
  const { events, updateAgents, updateEdges, pushEvent, clearEvents } = useEventStream(
    mode === 'mock' ? selectedTemplate?.id : undefined,
    mode,
  )

  // 초기화: 저장된 워크플로우 복원
  useEffect(() => {
    const savedTemplateId = getProjectWorkflow(currentProject)
    if (savedTemplateId) {
      const template = loadWorkflow(savedTemplateId)
      if (template) {
        setSelectedTemplate(template)
        if (mode === 'mock') {
          setSession(createSessionFromTemplate(template, currentProject))
          setShowSelector(false)
        }
        return
      }
    }
    setShowSelector(true)
  }, [])

  // 템플릿 선택 → 세션 생성
  const handleSelectTemplate = useCallback(async (template: WorkflowTemplate, projectId: string) => {
    setSelectedTemplate(template)
    setCurrentProject(projectId)
    setSelectedAgentId(null)
    setShowSelector(false)

    // 프로젝트 경로 가져오기
    try {
      const res = await fetch('/api/projects')
      const projects = await res.json()
      const project = projects.find((p: { id: string }) => p.id === projectId)
      if (project?.path) {
        setCurrentProjectPath(project.path)
      }
    } catch {
      // 프로젝트 API 실패 시 무시
    }

    setProjectWorkflow(projectId, template.id)
    if (!template.isPreset) {
      saveWorkflow(template)
    }

    if (mode === 'real') {
      // Real 모드: 프롬프트 입력 화면으로
      setShowPromptInput(true)
    } else {
      // Mock 모드: 바로 시뮬레이션
      const newSession = createSessionFromTemplate(template, projectId)
      setSession(newSession)
    }
  }, [mode])

  // Real 모드: 프롬프트 제출 → 하네스 실행
  const handlePromptSubmit = useCallback(async (userPrompt: string) => {
    if (!selectedTemplate || isRunning) return

    setShowPromptInput(false)
    setIsRunning(true)
    clearEvents()
    setStreamingText({})

    // 세션 생성 (모든 에이전트 idle 상태)
    const newSession = createSessionFromTemplate(selectedTemplate, currentProject)
    setSession(newSession)

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const result = await runWorkflow({
        userPrompt,
        projectId: currentProject,
        projectPath: currentProjectPath || undefined,
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
        onError: (error) => {
          setIsRunning(false)
        },
        costLimit: 5,
        signal: controller.signal,
      })

      setExecution(result)
    } catch {
      setIsRunning(false)
    }
  }, [selectedTemplate, currentProject, isRunning, clearEvents, pushEvent])

  // 중지 버튼
  const handleStop = useCallback(() => {
    abortRef.current?.abort()
    setIsRunning(false)
  }, [])

  // 이벤트 스트림에서 에이전트 상태 + 검증 루프 동기화
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

      // Real 모드: execution에서 실비용, Mock 모드: 추정 비용
      const totalCost = execution
        ? execution.totalCost
        : (newAgents.reduce((sum, a) => sum + a.tokensUsed, 0) / 1000) * 0.003

      const verificationLoop = deriveVerificationLoop(prev.verificationLoop, events)

      // 에이전트 output 업데이트 (스트리밍 텍스트 또는 최종 결과)
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

  // 경과 시간 업데이트
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

  // 1. 템플릿 선택 화면
  if (showSelector || !selectedTemplate) {
    return (
      <TemplateSelector
        onSelect={handleSelectTemplate}
        onClose={selectedTemplate ? () => setShowSelector(false) : undefined}
        currentProjectId={currentProject}
      />
    )
  }

  // 2. Real 모드: 프롬프트 입력 화면
  if (showPromptInput && mode === 'real') {
    return (
      <PromptInput
        projectId={currentProject}
        onSubmit={handlePromptSubmit}
        onBack={() => setShowSelector(true)}
        isRunning={isRunning}
      />
    )
  }

  // 3. 세션 없음
  if (!session) {
    return (
      <TemplateSelector
        onSelect={handleSelectTemplate}
        currentProjectId={currentProject}
      />
    )
  }

  // 4. 메인 화면 (FlowCanvas)
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

          {/* 검증 루프 패널 (우측 하단) */}
          <div className="absolute bottom-16 right-4 w-56">
            <VerificationLoopPanel
              loop={session.verificationLoop}
              config={selectedTemplate.verificationLoop}
            />
          </div>

          {/* 좌측 상단: 컨트롤 버튼 */}
          <div className="absolute top-4 left-4 flex items-center gap-2">
            <button
              onClick={() => setShowSelector(true)}
              className="flex items-center gap-1.5 rounded-lg bg-zinc-900/80 border border-zinc-800 px-3 py-1.5 text-xs text-zinc-400 hover:text-white hover:border-zinc-700 transition-colors"
            >
              ← 워크플로우 변경
            </button>

            {/* 모드 토글 */}
            <button
              onClick={() => {
                const next = mode === 'mock' ? 'real' : 'mock'
                setMode(next)
                if (next === 'real') {
                  setShowPromptInput(true)
                }
              }}
              className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
                mode === 'real'
                  ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
                  : 'border-zinc-800 bg-zinc-900/80 text-zinc-500'
              }`}
            >
              {mode === 'real' ? 'SDK 모드' : 'Mock 모드'}
            </button>

            {/* 중지 버튼 */}
            {isRunning && (
              <button
                onClick={handleStop}
                className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-1.5 text-xs font-medium text-red-400 hover:bg-red-500/20 transition-colors"
              >
                중지
              </button>
            )}

            {/* Real 모드에서 새 실행 */}
            {mode === 'real' && !isRunning && session.status !== 'active' && (
              <button
                onClick={() => setShowPromptInput(true)}
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

/**
 * 이벤트 메시지에서 검증 루프 상태를 추론
 */
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
        {
          round,
          codeAgentId: 'developer',
          testResult: 'fail',
          errors: [errors],
          fixApplied: false,
          timestamp: Date.now(),
        },
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
        {
          round,
          codeAgentId: 'developer',
          testResult: 'pass',
          errors: [],
          fixApplied: false,
          timestamp: Date.now(),
        },
      ]
    } else if (msg.includes('[검증 루프 PASS]')) {
      loop = { ...loop, status: 'passed' }
    }
  }

  return loop
}
