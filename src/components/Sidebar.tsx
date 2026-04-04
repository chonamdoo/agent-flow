'use client'

import type { AgentNodeData, AgentEvent, AgentTeam } from '@/lib/types'
import { AGENT_ROLE_META, AGENT_STATE_COLORS } from '@/lib/types'
import StatusIndicator from './StatusIndicator'

interface SidebarProps {
  agents: AgentNodeData[]
  events: AgentEvent[]
  agentTeams: AgentTeam[]
  selectedAgentId: string | null
  onSelectAgent: (id: string | null) => void
}

export default function Sidebar({ agents, events, agentTeams, selectedAgentId, onSelectAgent }: SidebarProps) {
  // 최근 이벤트 (최신 순)
  const recentEvents = [...events].reverse().slice(0, 50)

  // 에이전트 그룹 분류
  const orchestrator = agents.find((a) => a.role === 'orchestrator')
  const subagents = agents.filter((a) => a.role !== 'orchestrator')

  return (
    <div className="flex h-full w-72 flex-col border-r border-zinc-800 bg-zinc-950">
      {/* 활동 로그 */}
      <div className="flex-1 overflow-hidden">
        <div className="border-b border-zinc-800 px-4 py-3">
          <div className="flex items-center gap-2">
            <TabButton active>전체</TabButton>
            <TabButton>에러</TabButton>
            <TabButton>HITL 큐</TabButton>
            <TabButton>도구</TabButton>
            <TabButton>에이전트 흐름</TabButton>
          </div>
        </div>

        {/* 이벤트 목록 */}
        <div className="flex-1 overflow-y-auto px-2 py-2" style={{ maxHeight: 'calc(50vh - 80px)' }}>
          {recentEvents.map((event) => (
            <EventItem key={event.id} event={event} />
          ))}
          {recentEvents.length === 0 && (
            <div className="px-2 py-8 text-center text-xs text-zinc-600">
              이벤트 대기 중...
            </div>
          )}
        </div>
      </div>

      {/* 에이전트 상태 */}
      <div className="border-t border-zinc-800">
        <div className="px-4 py-2">
          <h3 className="text-[10px] font-bold uppercase tracking-wider text-zinc-600">
            C-SUITE 상태
          </h3>
        </div>
        <div className="space-y-0.5 px-2 pb-2">
          {orchestrator && (
            <AgentStatusItem
              agent={orchestrator}
              isSelected={selectedAgentId === orchestrator.id}
              onSelect={onSelectAgent}
            />
          )}
          {subagents.map((agent) => (
            <AgentStatusItem
              key={agent.id}
              agent={agent}
              isSelected={selectedAgentId === agent.id}
              onSelect={onSelectAgent}
            />
          ))}
        </div>
      </div>

      {/* 에이전트 팀 */}
      <div className="border-t border-zinc-800">
        <div className="px-4 py-2">
          <h3 className="text-[10px] font-bold uppercase tracking-wider text-zinc-600">
            AGENT TEAMS
          </h3>
        </div>
        <div className="space-y-0.5 px-2 pb-3">
          {agentTeams.map((team) => {
            const teamAgents = agents.filter((a) => team.agentIds.includes(a.id))
            const hasRunning = teamAgents.some(
              (a) => a.state === 'running' || a.state === 'thinking' || a.state === 'tool_calling',
            )
            const allComplete = teamAgents.length > 0 && teamAgents.every((a) => a.state === 'completed')
            const statusLabel = hasRunning ? '실행 중' : allComplete ? '완료' : team.status === 'standby' ? 'standby' : '대기'
            const statusColor = hasRunning ? 'text-emerald-400' : allComplete ? 'text-blue-400' : 'text-zinc-600'

            return (
              <div
                key={team.id}
                className="flex items-center justify-between rounded-md px-2 py-1.5 text-xs text-zinc-400 hover:bg-zinc-900"
              >
                <div className="flex items-center gap-1.5">
                  <span>{team.name}</span>
                  <div className="flex -space-x-1">
                    {teamAgents.slice(0, 3).map((a) => (
                      <span
                        key={a.id}
                        className="inline-block h-3 w-3 rounded-full border border-zinc-950"
                        style={{ backgroundColor: a.color }}
                      />
                    ))}
                  </div>
                </div>
                <span className={`text-[10px] font-medium ${statusColor}`}>{statusLabel}</span>
              </div>
            )
          })}
          {agentTeams.length === 0 && (
            <div className="px-2 py-2 text-[11px] text-zinc-600">팀 없음</div>
          )}
        </div>
      </div>
    </div>
  )
}

function TabButton({
  children,
  active = false,
}: {
  children: React.ReactNode
  active?: boolean
}) {
  return (
    <button
      className={`rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors ${
        active
          ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
          : 'text-zinc-500 hover:text-zinc-300'
      }`}
    >
      {children}
    </button>
  )
}

function AgentStatusItem({
  agent,
  isSelected,
  onSelect,
}: {
  agent: AgentNodeData
  isSelected: boolean
  onSelect: (id: string | null) => void
}) {
  const meta = AGENT_ROLE_META[agent.role]

  return (
    <button
      onClick={() => onSelect(isSelected ? null : agent.id)}
      className={`flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left transition-colors ${
        isSelected
          ? 'bg-zinc-800 ring-1 ring-emerald-500/30'
          : 'hover:bg-zinc-900'
      }`}
    >
      <div className="flex items-center gap-2">
        <span
          className="inline-block h-2 w-2 rounded-full"
          style={{ backgroundColor: AGENT_STATE_COLORS[agent.state] }}
        />
        <span className="text-xs text-zinc-300">{meta.icon}</span>
        <span className="text-xs text-zinc-300">{agent.name}</span>
      </div>
      <span
        className="rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold"
        style={{
          backgroundColor: `${agent.color}15`,
          color: agent.color,
        }}
      >
        {agent.eventCount}
      </span>
    </button>
  )
}

function EventItem({ event }: { event: AgentEvent }) {
  const time = new Date(event.timestamp)
  const timeStr = `${String(time.getHours()).padStart(2, '0')}:${String(time.getMinutes()).padStart(2, '0')}:${String(time.getSeconds()).padStart(2, '0')}`

  const typeLabel: Record<string, string> = {
    agent_spawn: '서브에이전트 작업 완료',
    agent_complete: '응답 완료',
    agent_error: '에러 발생',
    tool_call_start: '도구 호출',
    tool_call_end: '도구 완료',
    message: '메시지',
    thinking: '사고 중',
    handoff: '핸드오프',
  }

  const statusColor: Record<string, string> = {
    agent_spawn: 'bg-emerald-500/10 text-emerald-400',
    agent_complete: 'bg-blue-500/10 text-blue-400',
    agent_error: 'bg-red-500/10 text-red-400',
    handoff: 'bg-purple-500/10 text-purple-400',
    tool_call_start: 'bg-amber-500/10 text-amber-400',
    tool_call_end: 'bg-amber-500/10 text-amber-400',
    thinking: 'bg-violet-500/10 text-violet-400',
    message: 'bg-zinc-500/10 text-zinc-400',
  }

  return (
    <div className="rounded-md border border-zinc-800/50 bg-zinc-900/50 px-3 py-2 mb-1.5">
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-1.5">
          <span className="text-xs font-medium text-white">{event.agentName}</span>
          <span
            className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${statusColor[event.type] ?? 'bg-zinc-800 text-zinc-400'}`}
          >
            {typeLabel[event.type] ?? event.type}
          </span>
        </div>
        <span className="font-mono text-[10px] text-zinc-600">{timeStr}</span>
      </div>
      {typeof event.payload.message === 'string' && (
        <p className="text-[11px] text-zinc-500 leading-relaxed">
          {event.payload.message}
        </p>
      )}
    </div>
  )
}
