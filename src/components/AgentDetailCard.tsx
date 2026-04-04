'use client'

import type { AgentNodeData, AgentEvent } from '@/lib/types'
import { AGENT_ROLE_META, AGENT_STATE_COLORS } from '@/lib/types'

interface AgentDetailCardProps {
  agent: AgentNodeData
  events: AgentEvent[]
  onClose: () => void
}

export default function AgentDetailCard({ agent, events, onClose }: AgentDetailCardProps) {
  const meta = AGENT_ROLE_META[agent.role]
  const agentEvents = events.filter((e) => e.agentId === agent.id)
  const stateColor = AGENT_STATE_COLORS[agent.state]

  const duration = agent.startedAt && agent.completedAt
    ? Math.round((agent.completedAt - agent.startedAt) / 1000)
    : agent.startedAt
      ? Math.round((Date.now() - agent.startedAt) / 1000)
      : 0

  return (
    <div className="absolute right-4 top-16 w-80 rounded-xl border border-zinc-800 bg-zinc-950/95 backdrop-blur-sm shadow-2xl">
      {/* 헤더 */}
      <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
        <div className="flex items-center gap-2">
          <span
            className="flex h-8 w-8 items-center justify-center rounded-full text-sm"
            style={{ backgroundColor: `${agent.color}20`, color: agent.color }}
          >
            {meta.icon}
          </span>
          <div>
            <div className="text-sm font-semibold text-white">{agent.name}</div>
            <div className="text-[10px] text-zinc-500">{agent.role}</div>
          </div>
        </div>
        <button
          onClick={onClose}
          className="flex h-6 w-6 items-center justify-center rounded text-zinc-500 hover:bg-zinc-800 hover:text-white transition-colors"
        >
          ×
        </button>
      </div>

      {/* 상태 정보 */}
      <div className="grid grid-cols-3 gap-3 border-b border-zinc-800 px-4 py-3">
        <InfoItem label="상태">
          <span className="flex items-center gap-1.5">
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ backgroundColor: stateColor }}
            />
            <span className="text-xs text-white">{agent.state}</span>
          </span>
        </InfoItem>
        <InfoItem label="토큰">
          <span className="font-mono text-xs text-white">
            {agent.tokensUsed > 0 ? `${(agent.tokensUsed / 1000).toFixed(1)}k` : '-'}
          </span>
        </InfoItem>
        <InfoItem label="시간">
          <span className="font-mono text-xs text-white">
            {duration > 0 ? `${duration}s` : '-'}
          </span>
        </InfoItem>
      </div>

      {/* 모델 정보 */}
      <div className="border-b border-zinc-800 px-4 py-3">
        <InfoItem label="모델">
          <span className="text-xs text-zinc-300">{agent.model ?? 'Opus 4.6'}</span>
        </InfoItem>
        {agent.output && (
          <div className="mt-2">
            <InfoItem label="출력">
              <span className="text-xs text-emerald-400">{agent.output}</span>
            </InfoItem>
          </div>
        )}
      </div>

      {/* 이벤트 히스토리 */}
      <div className="max-h-48 overflow-y-auto px-4 py-3">
        <h4 className="mb-2 text-[10px] font-bold uppercase tracking-wider text-zinc-600">
          이벤트 히스토리
        </h4>
        {agentEvents.length === 0 && (
          <p className="text-xs text-zinc-600">이벤트 없음</p>
        )}
        {agentEvents.map((event) => {
          const time = new Date(event.timestamp)
          const timeStr = `${String(time.getHours()).padStart(2, '0')}:${String(time.getMinutes()).padStart(2, '0')}:${String(time.getSeconds()).padStart(2, '0')}`
          return (
            <div
              key={event.id}
              className="flex items-start gap-2 border-l border-zinc-800 py-1.5 pl-3"
            >
              <span className="font-mono text-[10px] text-zinc-600 shrink-0">{timeStr}</span>
              <span className="text-[11px] text-zinc-400">
                {event.payload.message ? String(event.payload.message) : event.type}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function InfoItem({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div>
      <div className="text-[10px] text-zinc-600 mb-0.5">{label}</div>
      {children}
    </div>
  )
}
