'use client'

import { useState } from 'react'
import type { AgentNodeData } from '@/lib/types'
import { AGENT_STATE_COLORS } from '@/lib/types'

interface KanbanCard {
  id: string
  prompt: string
  status: KanbanColumn
  projectId: string
  startedAt: number
  currentAgent?: string
  completedAt?: number
  qaScore?: string
}

type KanbanColumn = 'backlog' | 'planning' | 'designing' | 'developing' | 'reviewing' | 'done'

const COLUMNS: Array<{ id: KanbanColumn; label: string; color: string }> = [
  { id: 'backlog', label: '아이디어', color: '#52525b' },
  { id: 'planning', label: '기획 중', color: '#8b5cf6' },
  { id: 'designing', label: '디자인', color: '#ec4899' },
  { id: 'developing', label: '개발 중', color: '#34d399' },
  { id: 'reviewing', label: '검수', color: '#3b82f6' },
  { id: 'done', label: '완료', color: '#22c55e' },
]

interface KanbanBoardProps {
  cards: KanbanCard[]
  agents: AgentNodeData[]
  projectName: string
  prompt: string
  onPromptChange: (value: string) => void
  onSubmit: () => void
  isRunning: boolean
  onStop: () => void
}

export default function KanbanBoard({
  cards,
  agents,
  projectName,
  prompt,
  onPromptChange,
  onSubmit,
  isRunning,
  onStop,
}: KanbanBoardProps) {
  const getColumnCards = (columnId: KanbanColumn) =>
    cards.filter((c) => c.status === columnId)

  return (
    <div className="flex h-full flex-col">
      {/* 프로젝트 헤더 + 인라인 프롬프트 */}
      <div className="border-b border-zinc-800 px-6 py-4">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-3">
            <h1 className="text-sm font-bold text-white">{projectName}</h1>
            {isRunning && (
              <span className="flex items-center gap-1.5 rounded-full bg-emerald-500/10 border border-emerald-500/20 px-2.5 py-0.5 text-[10px] font-medium text-emerald-400">
                <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
                하네스 실행 중
              </span>
            )}
          </div>
          {isRunning && (
            <button
              onClick={onStop}
              className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-1 text-[11px] font-medium text-red-400 hover:bg-red-500/20 transition-colors"
            >
              중지
            </button>
          )}
        </div>

        {/* 인라인 프롬프트 입력 */}
        <div className="flex gap-2">
          <input
            type="text"
            value={prompt}
            onChange={(e) => onPromptChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && prompt.trim() && !isRunning) onSubmit()
            }}
            placeholder="추가할 기능을 입력하세요... (Enter로 실행)"
            className="flex-1 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs text-white placeholder:text-zinc-600 focus:outline-none focus:border-zinc-700"
            disabled={isRunning}
          />
          <button
            onClick={onSubmit}
            disabled={!prompt.trim() || isRunning}
            className={`shrink-0 rounded-lg px-4 py-2 text-xs font-medium transition-all ${
              prompt.trim() && !isRunning
                ? 'bg-emerald-500 text-white hover:bg-emerald-400'
                : 'bg-zinc-800 text-zinc-600 cursor-not-allowed'
            }`}
          >
            하네스 실행
          </button>
        </div>
      </div>

      {/* 칸반 보드 */}
      <div className="flex flex-1 gap-3 overflow-x-auto p-4">
        {COLUMNS.map((column) => {
          const columnCards = getColumnCards(column.id)
          const activeAgent = agents.find(
            (a) =>
              (column.id === 'planning' && a.role === 'pm_planner') ||
              (column.id === 'designing' && a.role === 'designer') ||
              (column.id === 'developing' && a.role === 'developer') ||
              (column.id === 'reviewing' && (a.role === 'security_expert' || a.role === 'reviewer')),
          )
          const isActiveColumn = activeAgent && (
            activeAgent.state === 'running' ||
            activeAgent.state === 'thinking' ||
            activeAgent.state === 'tool_calling'
          )

          return (
            <div
              key={column.id}
              className={`flex w-56 shrink-0 flex-col rounded-lg border bg-zinc-900/30 ${
                isActiveColumn
                  ? 'border-emerald-500/30 shadow-lg shadow-emerald-500/5'
                  : 'border-zinc-800/50'
              }`}
            >
              {/* 칼럼 헤더 */}
              <div className="flex items-center justify-between px-3 py-2.5 border-b border-zinc-800/50">
                <div className="flex items-center gap-2">
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ backgroundColor: column.color }}
                  />
                  <span className="text-[11px] font-semibold text-zinc-400">{column.label}</span>
                </div>
                <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] font-mono text-zinc-500">
                  {columnCards.length}
                </span>
              </div>

              {/* 카드 목록 */}
              <div className="flex-1 space-y-2 overflow-y-auto p-2">
                {columnCards.map((card) => (
                  <KanbanCardItem key={card.id} card={card} />
                ))}

                {columnCards.length === 0 && !isActiveColumn && (
                  <div className="py-4 text-center text-[10px] text-zinc-700">
                    비어 있음
                  </div>
                )}

                {/* 실행 중인 칼럼에 진행 표시 */}
                {isActiveColumn && activeAgent && (
                  <div className="rounded-md border border-emerald-500/20 bg-emerald-500/5 p-2.5">
                    <div className="flex items-center gap-1.5 mb-1">
                      <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
                      <span className="text-[10px] font-medium text-emerald-400">
                        {activeAgent.name} 작업 중
                      </span>
                    </div>
                    <div className="text-[10px] text-zinc-500">
                      {activeAgent.state === 'thinking' ? '분석 중...' : '실행 중...'}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function KanbanCardItem({ card }: { card: KanbanCard }) {
  const timeAgo = getTimeAgo(card.startedAt)
  const isDone = card.status === 'done'

  return (
    <div
      className={`rounded-md border p-2.5 transition-colors cursor-default ${
        isDone
          ? 'border-emerald-500/20 bg-emerald-500/5'
          : 'border-zinc-800 bg-zinc-900/50 hover:border-zinc-700'
      }`}
    >
      <p className="text-[11px] text-white leading-relaxed line-clamp-2">
        {card.prompt}
      </p>
      <div className="mt-2 flex items-center justify-between">
        <span className="text-[9px] text-zinc-600">{timeAgo}</span>
        {card.qaScore && (
          <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[9px] font-mono text-zinc-400">
            {card.qaScore}
          </span>
        )}
      </div>
    </div>
  )
}

function getTimeAgo(timestamp: number): string {
  const diff = Date.now() - timestamp
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return '방금'
  if (minutes < 60) return `${minutes}분 전`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}시간 전`
  const days = Math.floor(hours / 24)
  return `${days}일 전`
}

// 에이전트 상태 → 칸반 칼럼 매핑
export function agentStateToColumn(agents: AgentNodeData[]): KanbanColumn {
  const pm = agents.find((a) => a.role === 'pm_planner')
  const designer = agents.find((a) => a.role === 'designer')
  const developer = agents.find((a) => a.role === 'developer')
  const security = agents.find((a) => a.role === 'security_expert')
  const reviewer = agents.find((a) => a.role === 'reviewer')

  if (reviewer?.state === 'completed') return 'done'
  if (reviewer?.state === 'running' || reviewer?.state === 'thinking' || security?.state === 'running' || security?.state === 'thinking') return 'reviewing'
  if (developer?.state === 'running' || developer?.state === 'thinking' || developer?.state === 'tool_calling') return 'developing'
  if (designer?.state === 'running' || designer?.state === 'thinking') return 'designing'
  if (pm?.state === 'running' || pm?.state === 'thinking') return 'planning'
  return 'backlog'
}
