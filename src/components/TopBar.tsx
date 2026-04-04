'use client'

import type { Session, AgentEvent } from '@/lib/types'
import { PLATFORM_META } from '@/lib/types'

interface TopBarProps {
  session: Session
  events: AgentEvent[]
}

export default function TopBar({ session, events }: TopBarProps) {
  const activeAgents = session.agents.filter(
    (a) => a.state === 'running' || a.state === 'thinking' || a.state === 'tool_calling',
  ).length
  const errorAgents = session.agents.filter((a) => a.state === 'error').length
  const elapsed = session.startedAt
    ? Math.floor((Date.now() - session.startedAt) / 1000)
    : 0
  const minutes = Math.floor(elapsed / 60)
  const seconds = elapsed % 60

  return (
    <div className="flex h-12 items-center justify-between border-b border-zinc-800 bg-zinc-950 px-4">
      {/* 좌: 프로젝트명 + 상태 */}
      <div className="flex items-center gap-4">
        <h1 className="text-sm font-semibold text-white">Agent Flow</h1>
        <div className="h-4 w-px bg-zinc-800" />
        <span className="text-xs text-zinc-400">
          {PLATFORM_META[session.platform].icon} {session.projectName}
        </span>
        <span
          className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
            session.status === 'active'
              ? 'bg-emerald-500/10 text-emerald-400'
              : session.status === 'completed'
                ? 'bg-zinc-700/50 text-zinc-400'
                : 'bg-red-500/10 text-red-400'
          }`}
        >
          {session.status === 'active' ? '실행 중' : session.status === 'completed' ? '완료' : '실패'}
        </span>
      </div>

      {/* 우: 카운터들 */}
      <div className="flex items-center gap-6">
        <div className="flex items-center gap-4 text-xs">
          <CounterBadge
            value={events.length}
            label="이벤트"
            color="text-emerald-400"
            bgColor="bg-emerald-500/10"
          />
          <CounterBadge
            value={activeAgents}
            label="활성"
            color="text-amber-400"
            bgColor="bg-amber-500/10"
          />
          <CounterBadge
            value={errorAgents}
            label="에러"
            color="text-red-400"
            bgColor="bg-red-500/10"
          />
        </div>

        <div className="h-4 w-px bg-zinc-800" />

        {/* 비용 */}
        <div className="text-right">
          <div className="text-[10px] text-zinc-600">비용 분석</div>
          <div className="font-mono text-xs font-semibold text-white">
            ${session.totalCost.toFixed(2)}
          </div>
        </div>

        {/* 경과 시간 */}
        <div className="text-right">
          <div className="text-[10px] text-zinc-600">경과</div>
          <div className="font-mono text-xs text-zinc-300">
            {String(minutes).padStart(2, '0')}:{String(seconds).padStart(2, '0')}
          </div>
        </div>
      </div>
    </div>
  )
}

function CounterBadge({
  value,
  label,
  color,
  bgColor,
}: {
  value: number
  label: string
  color: string
  bgColor: string
}) {
  return (
    <div className="flex items-center gap-1.5">
      <span className={`rounded px-1.5 py-0.5 font-mono font-bold ${color} ${bgColor}`}>
        {value}
      </span>
      <span className="text-zinc-500">{label}</span>
    </div>
  )
}
