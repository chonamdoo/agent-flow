import type { AgentEvent, WorkflowTemplate } from './types'
import { createSimulationFromTemplate } from './mock-data'
import { WORKFLOW_PRESETS } from './workflow-templates'

type EventListener = (event: AgentEvent & {
  agentStateUpdate?: { agentId: string; state: string; tokensUsed?: number }
  edgeActivation?: { edgeId: string; active: boolean }
}) => void

class EventStore {
  private listeners: Set<EventListener> = new Set()
  private events: AgentEvent[] = []
  private simulationTimers: ReturnType<typeof setTimeout>[] = []
  private running = false
  private currentTemplateId: string | null = null

  subscribe(listener: EventListener): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  private emit(event: AgentEvent & {
    agentStateUpdate?: { agentId: string; state: string; tokensUsed?: number }
    edgeActivation?: { edgeId: string; active: boolean }
  }) {
    this.events.push(event)
    for (const listener of this.listeners) {
      listener(event)
    }
  }

  startSimulation(templateId?: string) {
    // 이전 시뮬레이션 정리
    this.stopSimulation()
    this.events = []
    this.running = true
    this.currentTemplateId = templateId ?? null

    const template = templateId
      ? WORKFLOW_PRESETS.find((t) => t.id === templateId)
      : WORKFLOW_PRESETS[0]

    if (!template) return

    const sequence = createSimulationFromTemplate(template)
    for (const scheduled of sequence) {
      const timer = setTimeout(() => {
        const event: AgentEvent & {
          agentStateUpdate?: { agentId: string; state: string; tokensUsed?: number }
          edgeActivation?: { edgeId: string; active: boolean }
        } = {
          id: Math.random().toString(36).slice(2, 10),
          timestamp: Date.now(),
          type: scheduled.event.type,
          agentId: scheduled.event.agentId,
          agentName: scheduled.event.agentName,
          payload: scheduled.event.payload,
          agentStateUpdate: scheduled.agentStateUpdate as {
            agentId: string; state: string; tokensUsed?: number
          } | undefined,
          edgeActivation: scheduled.edgeActivation,
        }
        this.emit(event)
      }, scheduled.delayMs)
      this.simulationTimers.push(timer)
    }
  }

  stopSimulation() {
    this.running = false
    for (const timer of this.simulationTimers) {
      clearTimeout(timer)
    }
    this.simulationTimers = []
  }

  getEvents(): AgentEvent[] {
    return [...this.events]
  }

  isRunning(): boolean {
    return this.running
  }
}

let store: EventStore | null = null
export function getEventStore(): EventStore {
  if (!store) {
    store = new EventStore()
  }
  return store
}
