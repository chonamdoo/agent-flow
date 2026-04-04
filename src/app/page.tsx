'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import type { Session, VerificationLoop, WorkflowExecution, AgentNodeData } from '@/lib/types'
import { createSessionFromTemplate } from '@/lib/mock-data'
import { useEventStream } from '@/hooks/useEventStream'
import { runWorkflow } from '@/lib/orchestrator'
import { WORKFLOW_PRESETS } from '@/lib/workflow-templates'
import FlowCanvas from '@/components/FlowCanvas'
import Sidebar from '@/components/Sidebar'
import TopBar from '@/components/TopBar'
import AgentDetailCard from '@/components/AgentDetailCard'
import VerificationLoopPanel from '@/components/VerificationLoopPanel'
import AppSidebar from '@/components/AppSidebar'
import KanbanBoard from '@/components/KanbanBoard'
import DashboardMain from '@/components/DashboardMain'
import AddProjectModal from '@/components/AddProjectModal'

interface Project {
  id: string
  name: string
  path: string
  platform: 'web' | 'android' | 'ios'
  icon: string
}

interface KanbanCard {
  id: string
  prompt: string
  status: 'backlog' | 'planning' | 'designing' | 'developing' | 'reviewing' | 'done'
  projectId: string
  startedAt: number
  currentAgent?: string
  completedAt?: number
  qaScore?: string
  tags?: string[]
  completedAgents?: string[]
}

type MainView = 'dashboard' | 'kanban' | 'flow'

export default function Home() {
  const [projects, setProjects] = useState<Project[]>([])
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null)
  const [selectedProjectPath, setSelectedProjectPath] = useState<string>('')
  const [mainView, setMainView] = useState<MainView>('dashboard')
  const [prompt, setPrompt] = useState('')
  const [cards, setCards] = useState<KanbanCard[]>([])
  const [session, setSession] = useState<Session | null>(null)
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
  const [isRunning, setIsRunning] = useState(false)
  const [streamingText, setStreamingText] = useState<Record<string, string>>({})
  const [execution, setExecution] = useState<WorkflowExecution | null>(null)
  const [showAddProject, setShowAddProject] = useState(false)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const template = WORKFLOW_PRESETS[0]
  const { events, updateAgents, updateEdges, pushEvent, clearEvents } = useEventStream(undefined, 'real')

  // 프로젝트 로드
  useEffect(() => {
    fetch('/api/projects')
      .then((res) => res.json())
      .then((data: Project[]) => setProjects(data))
      .catch(() => {})
  }, [])

  // 대시보드로 이동
  const handleSelectDashboard = useCallback(() => {
    if (!isRunning) {
      setMainView('dashboard')
      setSelectedProjectId(null)
    }
  }, [isRunning])

  // 프로젝트 선택 → 칸반 보드
  const handleSelectProject = useCallback((id: string) => {
    setSelectedProjectId(id)
    const proj = projects.find((p) => p.id === id)
    if (proj) setSelectedProjectPath(proj.path)
    if (!isRunning) setMainView('kanban')
  }, [projects, isRunning])

  // 하네스 실행
  const handleSubmit = useCallback(async (_files?: File[]) => {
    if (!prompt.trim() || isRunning || !selectedProjectId) return

    const cardId = `card-${Date.now()}`
    const newCard: KanbanCard = {
      id: cardId,
      prompt: prompt.trim(),
      status: 'planning',
      projectId: selectedProjectId,
      startedAt: Date.now(),
    }
    setCards((prev) => [newCard, ...prev])

    setMainView('flow')
    setIsRunning(true)
    const userPrompt = prompt.trim()
    setPrompt('')
    clearEvents()
    setStreamingText({})
    setExecution(null)

    const newSession = createSessionFromTemplate(template, selectedProjectId)
    setSession(newSession)

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const result = await runWorkflow({
        userPrompt,
        projectId: selectedProjectId,
        projectPath: selectedProjectPath || undefined,
        onEvent: (event) => {
          pushEvent(event)
          if (event.agentStateUpdate) {
            const agentId = event.agentStateUpdate.agentId
            const state = event.agentStateUpdate.state
            const columnMap: Record<string, KanbanCard['status']> = {
              pm_planner: 'planning', designer: 'designing',
              developer: 'developing', security_expert: 'reviewing', reviewer: 'reviewing',
            }
            if (columnMap[agentId]) {
              if (state === 'running' || state === 'thinking') {
                setCards((prev) => prev.map((c) =>
                  c.id === cardId ? { ...c, status: columnMap[agentId], currentAgent: agentId } : c,
                ))
              } else if (state === 'completed') {
                setCards((prev) => prev.map((c) =>
                  c.id === cardId ? {
                    ...c,
                    currentAgent: undefined,
                    completedAgents: [...new Set([...(c.completedAgents ?? []), agentId])],
                  } : c,
                ))
              }
            }
          }
        },
        onStreaming: (agentId, text) => {
          setStreamingText((prev) => ({ ...prev, [agentId]: (prev[agentId] ?? '') + text }))
        },
        onComplete: (exec) => {
          setExecution(exec)
          setIsRunning(false)
          setCards((prev) => prev.map((c) =>
            c.id === cardId ? { ...c, status: 'done', completedAt: Date.now() } : c,
          ))
        },
        onError: () => { setIsRunning(false) },
        costLimit: 5,
        signal: controller.signal,
      })
      setExecution(result)
    } catch { setIsRunning(false) }
  }, [prompt, isRunning, selectedProjectId, selectedProjectPath, clearEvents, pushEvent, template])

  const handleStop = useCallback(() => {
    abortRef.current?.abort()
    setIsRunning(false)
  }, [])

  // 이벤트 → 세션 동기화
  useEffect(() => {
    if (!session) return
    setSession((prev) => {
      if (!prev) return prev
      const newAgents = updateAgents(prev.agents)
      const newEdges = updateEdges(prev.edges)
      if (newAgents === prev.agents && newEdges === prev.edges) return prev
      const allComplete = newAgents.every((a) => a.state === 'completed' || a.state === 'idle')
      const hasError = newAgents.some((a) => a.state === 'error')
      const status = hasError ? 'failed' : allComplete && events.length > 0 ? 'completed' : 'active'
      const totalCost = execution ? execution.totalCost : (newAgents.reduce((sum, a) => sum + a.tokensUsed, 0) / 1000) * 0.003
      const verificationLoop = deriveVerificationLoop(prev.verificationLoop, events)
      const agentsWithOutput = newAgents.map((a) => {
        const streamText = streamingText[a.id]
        const execResult = execution?.results.find((r) => r.role === a.role)
        const output = execResult?.output ?? streamText ?? a.output
        return output !== a.output ? { ...a, output } : a
      })
      return { ...prev, agents: agentsWithOutput, edges: newEdges, events, status, totalCost, verificationLoop }
    })
  }, [events, updateAgents, updateEdges, session !== null, execution, streamingText])

  useEffect(() => {
    timerRef.current = setInterval(() => { setSession((prev) => (prev ? { ...prev } : null)) }, 1000)
    return () => { if (timerRef.current) clearInterval(timerRef.current) }
  }, [])

  const selectedAgent = selectedAgentId && session
    ? session.agents.find((a) => a.id === selectedAgentId) ?? null : null

  const selectedProject = projects.find((p) => p.id === selectedProjectId)
  const sessionAgents: AgentNodeData[] = session?.agents ?? template.agents.map((a) => ({
    id: a.id, name: a.name, role: a.role, state: 'idle' as const,
    color: a.color, position: { x: 0, y: 0 }, tokensUsed: 0, eventCount: 0,
    model: a.model, description: a.description,
  }))
  const projectCards = selectedProjectId ? cards.filter((c) => c.projectId === selectedProjectId) : []

  // ──── 렌더 ────
  return (
    <div className="flex h-screen bg-[#09090b]">
      <AppSidebar
        projects={projects}
        selectedProjectId={selectedProjectId}
        currentView={mainView}
        onSelectDashboard={handleSelectDashboard}
        onSelectProject={handleSelectProject}
        onAddProject={() => setShowAddProject(true)}
        agents={sessionAgents}
        isRunning={isRunning}
      />

      {/* 프로젝트 추가 모달 */}
      {showAddProject && (
        <AddProjectModal
          onClose={() => setShowAddProject(false)}
          onAdd={(p) => {
            setProjects((prev) => [...prev, p])
            setSelectedProjectId(p.id)
            setSelectedProjectPath(p.path)
            setMainView('kanban')
          }}
        />
      )}

      {/* 메인 영역 — 뷰에 따라 교체 */}
      {mainView === 'dashboard' && (
        <DashboardMain
          projects={projects}
          onSelectProject={handleSelectProject}
        />
      )}

      {mainView === 'kanban' && selectedProjectId && (
        <div className="flex flex-1 flex-col overflow-hidden">
          <KanbanBoard
            cards={projectCards}
            agents={sessionAgents}
            projectName={selectedProject?.name ?? selectedProjectId}
            prompt={prompt}
            onPromptChange={setPrompt}
            onSubmit={handleSubmit}
            isRunning={isRunning}
            onStop={handleStop}
          />
        </div>
      )}

      {mainView === 'flow' && session && selectedProjectId && (
        <div className="flex flex-1 overflow-hidden">
          {/* 좌: 칸반 보드 (compact) — 모바일에서 숨김 */}
          <div className="hidden lg:flex w-[420px] shrink-0 flex-col border-r border-zinc-800 overflow-hidden">
            <KanbanBoard
              cards={projectCards}
              agents={sessionAgents}
              projectName={selectedProject?.name ?? selectedProjectId}
              prompt={prompt}
              onPromptChange={setPrompt}
              onSubmit={handleSubmit}
              isRunning={isRunning}
              onStop={handleStop}
              compact
            />
          </div>

          {/* 우: FlowCanvas + 이벤트 로그 */}
          <div className="flex flex-1 flex-col overflow-hidden">
            <TopBar session={session} events={events} />
            <div className="flex flex-1 overflow-hidden">
              <Sidebar
                agents={session.agents}
                events={events}
                agentTeams={session.agentTeams}
                selectedAgentId={selectedAgentId}
                onSelectAgent={setSelectedAgentId}
              />
              <div className="relative flex-1">
                <FlowCanvas
                  agents={session.agents}
                  edges={session.edges}
                  selectedAgentId={selectedAgentId}
                  onSelectAgent={setSelectedAgentId}
                  onUpdateNodeState={null}
                />
                {selectedAgent && (
                  <AgentDetailCard agent={selectedAgent} events={events} onClose={() => setSelectedAgentId(null)} />
                )}
                <div className="absolute bottom-16 right-4 w-56">
                  <VerificationLoopPanel loop={session.verificationLoop} config={template.verificationLoop} />
                </div>
                <div className="absolute top-4 left-4 flex items-center gap-2">
                  <button
                    onClick={() => setMainView(selectedProjectId ? 'kanban' : 'dashboard')}
                    className="rounded-lg bg-zinc-900/80 border border-zinc-800 px-3 py-1.5 text-xs text-zinc-400 hover:text-white hover:border-zinc-700 transition-colors"
                  >
                    ← {selectedProjectId ? '칸반 보드' : '대시보드'}
                  </button>
                  {isRunning && (
                    <button onClick={handleStop} className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-1.5 text-xs font-medium text-red-400 hover:bg-red-500/20 transition-colors">
                      중지
                    </button>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function deriveVerificationLoop(prev: VerificationLoop, events: { payload: Record<string, unknown> }[]): VerificationLoop {
  let loop = { ...prev, history: [...prev.history] }
  for (const event of events) {
    const msg = typeof event.payload.message === 'string' ? event.payload.message : ''
    if (msg.includes('[검증 루프') && msg.includes('CODE:')) {
      const m = msg.match(/(\d+)\/(\d+)/); const r = m ? parseInt(m[1], 10) : loop.currentIteration + 1
      loop = { ...loop, status: 'coding', currentIteration: r }
    } else if (msg.includes('[검증 루프') && msg.includes('TEST:')) { loop = { ...loop, status: 'testing' } }
    else if (msg.includes('TEST FAIL:')) {
      const m = msg.match(/(\d+)\/(\d+)/); const r = m ? parseInt(m[1], 10) : loop.currentIteration
      const errors = msg.split(':').slice(-1)[0]?.trim() ?? ''
      loop.history = [...loop.history.filter((h) => h.round !== r), { round: r, codeAgentId: 'developer', testResult: 'fail', errors: [errors], fixApplied: false, timestamp: Date.now() }]
    } else if (msg.includes('[검증 루프') && msg.includes('FIX:')) {
      loop = { ...loop, status: 'fixing' }; const last = loop.history[loop.history.length - 1]
      if (last) loop.history = [...loop.history.slice(0, -1), { ...last, fixApplied: true }]
    } else if (msg.includes('TEST PASS:')) {
      const m = msg.match(/(\d+)\/(\d+)/); const r = m ? parseInt(m[1], 10) : loop.currentIteration
      loop.history = [...loop.history.filter((h) => h.round !== r), { round: r, codeAgentId: 'developer', testResult: 'pass', errors: [], fixApplied: false, timestamp: Date.now() }]
    } else if (msg.includes('[검증 루프 PASS]')) { loop = { ...loop, status: 'passed' } }
  }
  return loop
}
