'use client'

import { useState } from 'react'
import type { AgentNodeData, AgentEvent } from '@/lib/types'
import { AGENT_ROLE_META, AGENT_STATE_COLORS } from '@/lib/types'

interface AgentDetailCardProps {
  agent: AgentNodeData
  events: AgentEvent[]
  onClose: () => void
}

type DetailTab = 'output' | 'events'

const OUTPUT_LABELS: Record<string, string> = {
  pm_planner: 'SPEC.md',
  designer: 'DESIGN.md',
  developer: 'SELF_CHECK.md',
  security_expert: 'SECURITY_REPORT.md',
  reviewer: 'QA_REPORT.md',
}

export default function AgentDetailCard({ agent, events, onClose }: AgentDetailCardProps) {
  const [tab, setTab] = useState<DetailTab>('output')
  const meta = AGENT_ROLE_META[agent.role]
  const agentEvents = events.filter((e) => e.agentId === agent.id)
  const stateColor = AGENT_STATE_COLORS[agent.state]
  const isActive = agent.state === 'running' || agent.state === 'thinking' || agent.state === 'tool_calling'

  const duration = agent.startedAt && agent.completedAt
    ? Math.round((agent.completedAt - agent.startedAt) / 1000)
    : agent.startedAt
      ? Math.round((Date.now() - agent.startedAt) / 1000)
      : 0

  const hasOutput = agent.output && agent.output.length > 0
  const outputLabel = OUTPUT_LABELS[agent.role] ?? '출력'

  return (
    <div className="absolute right-4 top-4 bottom-4 w-[420px] flex flex-col rounded-xl border border-zinc-800 bg-zinc-950/95 backdrop-blur-sm shadow-2xl">
      {/* 헤더 */}
      <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3 shrink-0">
        <div className="flex items-center gap-2.5">
          <span
            className="flex h-9 w-9 items-center justify-center rounded-lg text-sm"
            style={{ backgroundColor: `${agent.color}15`, color: agent.color }}
          >
            {meta.icon}
          </span>
          <div>
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-white">{agent.name}</span>
              {isActive && (
                <span className="flex items-center gap-1 rounded-full bg-emerald-500/10 px-1.5 py-0.5 text-[9px] text-emerald-400">
                  <span className="inline-block h-1 w-1 animate-pulse rounded-full bg-emerald-400" />
                  live
                </span>
              )}
            </div>
            <div className="text-[10px] text-zinc-500">{agent.model ?? 'claude'}</div>
          </div>
        </div>
        <button
          onClick={onClose}
          className="flex h-6 w-6 items-center justify-center rounded text-zinc-500 hover:bg-zinc-800 hover:text-white transition-colors"
        >
          ×
        </button>
      </div>

      {/* 상태 정보 바 */}
      <div className="flex items-center gap-4 border-b border-zinc-800 px-4 py-2.5 shrink-0">
        <div className="flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: stateColor }} />
          <span className="text-[11px] text-zinc-400">{agent.state}</span>
        </div>
        <div className="h-3 w-px bg-zinc-800" />
        <span className="font-mono text-[11px] text-zinc-400">
          {agent.tokensUsed > 0 ? `${(agent.tokensUsed / 1000).toFixed(1)}k tok` : '- tok'}
        </span>
        <div className="h-3 w-px bg-zinc-800" />
        <span className="font-mono text-[11px] text-zinc-400">
          {duration > 0 ? (duration > 60 ? `${Math.floor(duration / 60)}m ${duration % 60}s` : `${duration}s`) : '-'}
        </span>
        <span className="ml-auto font-mono text-[10px] text-zinc-600">
          {agentEvents.length} events
        </span>
      </div>

      {/* 탭 전환 */}
      <div className="flex border-b border-zinc-800 shrink-0">
        <button
          onClick={() => setTab('output')}
          className={`flex-1 py-2 text-[11px] font-medium transition-colors ${
            tab === 'output'
              ? 'text-white border-b-2 border-emerald-500'
              : 'text-zinc-500 hover:text-zinc-300'
          }`}
        >
          {outputLabel}
          {isActive && tab === 'output' && (
            <span className="ml-1.5 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
          )}
        </button>
        <button
          onClick={() => setTab('events')}
          className={`flex-1 py-2 text-[11px] font-medium transition-colors ${
            tab === 'events'
              ? 'text-white border-b-2 border-emerald-500'
              : 'text-zinc-500 hover:text-zinc-300'
          }`}
        >
          이벤트 ({agentEvents.length})
        </button>
      </div>

      {/* 탭 내용 */}
      <div className="flex-1 overflow-y-auto min-h-0">
        {tab === 'output' && (
          <div className="p-4">
            {hasOutput ? (
              <div className="relative">
                {/* 스트리밍 인디케이터 */}
                {isActive && (
                  <div className="mb-3 flex items-center gap-2 rounded-lg bg-emerald-500/5 border border-emerald-500/20 px-3 py-2">
                    <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-400" />
                    <span className="text-[11px] text-emerald-400">실시간 스트리밍 중...</span>
                    <span className="ml-auto font-mono text-[10px] text-zinc-600">{agent.output?.length ?? 0} chars</span>
                  </div>
                )}

                {/* 출력 내용 — 마크다운 스타일 */}
                <div className="prose-sm">
                  <pre className="whitespace-pre-wrap text-[11px] text-zinc-300 leading-[1.7] font-mono break-words">
                    {agent.output}
                  </pre>
                </div>

                {/* 완료 상태 */}
                {agent.state === 'completed' && agent.output && (
                  <div className="mt-3 flex items-center justify-between rounded-lg bg-zinc-900 border border-zinc-800 px-3 py-2">
                    <span className="text-[10px] text-zinc-500">
                      {agent.output.length.toLocaleString()} chars
                    </span>
                    <button
                      onClick={() => {
                        if (agent.output) navigator.clipboard.writeText(agent.output)
                      }}
                      className="text-[10px] text-zinc-500 hover:text-white transition-colors"
                    >
                      복사
                    </button>
                  </div>
                )}
              </div>
            ) : (
              <div className="flex flex-col items-center justify-center py-12 text-zinc-600">
                {isActive ? (
                  <>
                    <span className="mb-2 inline-block h-4 w-4 animate-spin rounded-full border-2 border-zinc-700 border-t-emerald-400" />
                    <span className="text-[11px]">응답 대기 중...</span>
                  </>
                ) : (
                  <>
                    <span className="text-2xl mb-2">{meta.icon}</span>
                    <span className="text-[11px]">아직 출력이 없습니다</span>
                  </>
                )}
              </div>
            )}
          </div>
        )}

        {tab === 'events' && (
          <div className="p-4">
            {agentEvents.length === 0 ? (
              <p className="py-8 text-center text-xs text-zinc-600">이벤트 없음</p>
            ) : (
              <div className="space-y-0.5">
                {agentEvents.map((event) => {
                  const time = new Date(event.timestamp)
                  const timeStr = `${String(time.getHours()).padStart(2, '0')}:${String(time.getMinutes()).padStart(2, '0')}:${String(time.getSeconds()).padStart(2, '0')}`
                  const typeColor = EVENT_TYPE_COLORS[event.type] ?? 'bg-zinc-800 text-zinc-400'

                  return (
                    <div
                      key={event.id}
                      className="flex items-start gap-2 rounded-md px-2 py-1.5 hover:bg-zinc-900/50"
                    >
                      <span className="font-mono text-[10px] text-zinc-600 shrink-0 mt-0.5">{timeStr}</span>
                      <span className={`shrink-0 rounded px-1.5 py-0.5 text-[9px] font-medium ${typeColor}`}>
                        {EVENT_TYPE_LABELS[event.type] ?? event.type}
                      </span>
                      <span className="text-[11px] text-zinc-400 leading-relaxed">
                        {event.payload.message ? String(event.payload.message) : ''}
                      </span>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

const EVENT_TYPE_LABELS: Record<string, string> = {
  agent_spawn: '시작',
  agent_complete: '완료',
  agent_error: '에러',
  tool_call_start: '도구',
  tool_call_end: '도구 완료',
  message: '메시지',
  thinking: '사고',
  handoff: '전달',
}

const EVENT_TYPE_COLORS: Record<string, string> = {
  agent_spawn: 'bg-emerald-500/10 text-emerald-400',
  agent_complete: 'bg-blue-500/10 text-blue-400',
  agent_error: 'bg-red-500/10 text-red-400',
  handoff: 'bg-purple-500/10 text-purple-400',
  tool_call_start: 'bg-amber-500/10 text-amber-400',
  tool_call_end: 'bg-amber-500/10 text-amber-400',
  thinking: 'bg-violet-500/10 text-violet-400',
  message: 'bg-zinc-500/10 text-zinc-400',
}
