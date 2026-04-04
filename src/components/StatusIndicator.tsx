'use client'

import type { AgentState } from '@/lib/types'
import { AGENT_STATE_COLORS } from '@/lib/types'

const STATE_LABELS: Record<AgentState, string> = {
  idle: '대기',
  running: '실행 중',
  thinking: '사고 중',
  tool_calling: '도구 호출',
  completed: '완료',
  error: '에러',
  waiting: '대기 중',
}

interface StatusIndicatorProps {
  state: AgentState
  size?: 'sm' | 'md'
}

export default function StatusIndicator({ state, size = 'sm' }: StatusIndicatorProps) {
  const color = AGENT_STATE_COLORS[state]
  const isActive = state === 'running' || state === 'thinking' || state === 'tool_calling'
  const dotSize = size === 'sm' ? 'h-2 w-2' : 'h-2.5 w-2.5'

  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={`inline-block rounded-full ${dotSize} ${isActive ? 'animate-pulse' : ''}`}
        style={{ backgroundColor: color }}
      />
      <span className="text-xs text-zinc-500">{STATE_LABELS[state]}</span>
    </span>
  )
}
