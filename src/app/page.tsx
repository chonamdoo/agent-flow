'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import type { Session, WorkflowTemplate, VerificationLoop } from '@/lib/types'
import { createSessionFromTemplate } from '@/lib/mock-data'
import { useEventStream } from '@/hooks/useEventStream'
import { saveWorkflow, setProjectWorkflow, getProjectWorkflow, loadWorkflow } from '@/lib/workflow-storage'
import FlowCanvas from '@/components/FlowCanvas'
import Sidebar from '@/components/Sidebar'
import TopBar from '@/components/TopBar'
import AgentDetailCard from '@/components/AgentDetailCard'
import TemplateSelector from '@/components/TemplateSelector'
import VerificationLoopPanel from '@/components/VerificationLoopPanel'

const PROJECT_PATH = 'trading-journal' // 현재 프로젝트

export default function Home() {
  const [selectedTemplate, setSelectedTemplate] = useState<WorkflowTemplate | null>(null)
  const [session, setSession] = useState<Session | null>(null)
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
  const [showSelector, setShowSelector] = useState(true)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // 초기화: 저장된 워크플로우 복원 (클리어해도 유지)
  useEffect(() => {
    const savedTemplateId = getProjectWorkflow(PROJECT_PATH)
    if (savedTemplateId) {
      const template = loadWorkflow(savedTemplateId)
      if (template) {
        setSelectedTemplate(template)
        setSession(createSessionFromTemplate(template, PROJECT_PATH))
        setShowSelector(false)
        return
      }
    }
    setShowSelector(true)
  }, [])

  const { events, updateAgents, updateEdges } = useEventStream(
    selectedTemplate?.id,
  )

  // 템플릿 선택 → 세션 생성 + 영속 저장
  const handleSelectTemplate = useCallback((template: WorkflowTemplate) => {
    setSelectedTemplate(template)
    const newSession = createSessionFromTemplate(template, PROJECT_PATH)
    setSession(newSession)
    setSelectedAgentId(null)
    setShowSelector(false)

    // 영속 저장: 프로젝트 ↔ 워크플로우 매핑
    setProjectWorkflow(PROJECT_PATH, template.id)
    if (!template.isPreset) {
      saveWorkflow(template)
    }
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

      const totalTokens = newAgents.reduce((sum, a) => sum + a.tokensUsed, 0)
      const totalCost = (totalTokens / 1000) * 0.003

      // 검증 루프 상태 추론 (이벤트 메시지 기반)
      const verificationLoop = deriveVerificationLoop(prev.verificationLoop, events)

      return {
        ...prev,
        agents: newAgents,
        edges: newEdges,
        events,
        status,
        totalCost,
        verificationLoop,
      }
    })
  }, [events, updateAgents, updateEdges, session !== null])

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

  // 템플릿 미선택 → 선택 화면
  if (showSelector || !selectedTemplate || !session) {
    return <TemplateSelector onSelect={handleSelectTemplate} />
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

          {/* 검증 루프 패널 (우측 하단) */}
          <div className="absolute bottom-16 right-4 w-56">
            <VerificationLoopPanel
              loop={session.verificationLoop}
              config={selectedTemplate.verificationLoop}
            />
          </div>

          {/* 워크플로우 변경 버튼 */}
          <button
            onClick={() => setShowSelector(true)}
            className="absolute top-4 left-4 flex items-center gap-1.5 rounded-lg bg-zinc-900/80 border border-zinc-800 px-3 py-1.5 text-xs text-zinc-400 hover:text-white hover:border-zinc-700 transition-colors"
          >
            ← 워크플로우 변경
          </button>
        </div>
      </div>
    </div>
  )
}

/**
 * 이벤트 메시지에서 검증 루프 상태를 추론
 * "[검증 루프 X/Y] STATUS:" 형식의 메시지를 파싱
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
