'use client'

import { useEffect, useRef, useCallback, useState } from 'react'
import type { AgentEvent, AgentNodeData, FlowEdge, AgentState } from '@/lib/types'

interface EventStreamResult {
  events: AgentEvent[]
  updateAgents: (agents: AgentNodeData[]) => AgentNodeData[]
  updateEdges: (edges: FlowEdge[]) => FlowEdge[]
}

export function useEventStream(templateId?: string): EventStreamResult {
  const [events, setEvents] = useState<AgentEvent[]>([])
  const pendingUpdatesRef = useRef<Array<{
    agentStateUpdate?: { agentId: string; state: string; tokensUsed?: number }
    edgeActivation?: { edgeId: string; active: boolean }
  }>>([])
  const eventSourceRef = useRef<EventSource | null>(null)

  useEffect(() => {
    // 이전 연결 정리
    eventSourceRef.current?.close()
    setEvents([])
    pendingUpdatesRef.current = []

    const url = templateId ? `/api/events?templateId=${templateId}` : '/api/events'
    const es = new EventSource(url)
    eventSourceRef.current = es

    es.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data)
        const event: AgentEvent = {
          id: data.id,
          timestamp: data.timestamp,
          type: data.type,
          agentId: data.agentId,
          agentName: data.agentName,
          payload: data.payload,
        }

        setEvents((prev) => [...prev, event])

        if (data.agentStateUpdate || data.edgeActivation) {
          pendingUpdatesRef.current.push({
            agentStateUpdate: data.agentStateUpdate,
            edgeActivation: data.edgeActivation,
          })
        }
      } catch {
        // 파싱 에러 무시
      }
    }

    es.onerror = () => {
      // 재연결은 EventSource가 자동 처리
    }

    return () => {
      es.close()
    }
  }, [templateId])

  const updateAgents = useCallback((agents: AgentNodeData[]): AgentNodeData[] => {
    const updates = pendingUpdatesRef.current.filter((u) => u.agentStateUpdate)
    if (updates.length === 0) return agents

    let result = agents
    for (const update of updates) {
      if (!update.agentStateUpdate) continue
      const { agentId, state, tokensUsed } = update.agentStateUpdate
      result = result.map((a) => {
        if (a.id !== agentId) return a
        const newAgent = { ...a, state: state as AgentState }
        if (tokensUsed !== undefined) newAgent.tokensUsed = tokensUsed
        if (state === 'running' && !a.startedAt) newAgent.startedAt = Date.now()
        if (state === 'completed' || state === 'error') newAgent.completedAt = Date.now()
        newAgent.eventCount = a.eventCount + 1
        return newAgent
      })
    }

    // 처리된 상태 업데이트 제거
    pendingUpdatesRef.current = pendingUpdatesRef.current.filter((u) => !u.agentStateUpdate)
    return result
  }, [])

  const updateEdges = useCallback((edges: FlowEdge[]): FlowEdge[] => {
    const updates = pendingUpdatesRef.current.filter((u) => u.edgeActivation)
    if (updates.length === 0) return edges

    let result = edges
    for (const update of updates) {
      if (!update.edgeActivation) continue
      const { edgeId, active } = update.edgeActivation
      result = result.map((e) =>
        e.id === edgeId ? { ...e, active } : e,
      )
    }

    pendingUpdatesRef.current = pendingUpdatesRef.current.filter((u) => !u.edgeActivation)
    return result
  }, [])

  return { events, updateAgents, updateEdges }
}
