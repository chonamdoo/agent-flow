'use client'

import { useState, useRef } from 'react'
import type { AgentNodeData } from '@/lib/types'
import { AGENT_ROLE_META } from '@/lib/types'

interface KanbanCard {
  id: string
  prompt: string
  status: KanbanColumn
  projectId: string
  startedAt: number
  currentAgent?: string
  completedAt?: number
  qaScore?: string
  tags?: string[]
  completedAgents?: string[]  // 완료된 에이전트 role 목록
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
  onSubmit: (files?: File[]) => void
  isRunning: boolean
  onStop: () => void
  compact?: boolean // 분할 뷰에서 좁은 모드
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
  compact = false,
}: KanbanBoardProps) {
  const [isDragging, setIsDragging] = useState(false)
  const [attachedFiles, setAttachedFiles] = useState<File[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)

  const getColumnCards = (columnId: KanbanColumn) =>
    cards.filter((c) => c.status === columnId)

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault()
    setIsDragging(true)
  }
  const handleDragLeave = () => setIsDragging(false)
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setIsDragging(false)
    const files = Array.from(e.dataTransfer.files)
    if (files.length > 0) {
      setAttachedFiles((prev) => [...prev, ...files])
    }
  }
  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? [])
    if (files.length > 0) {
      setAttachedFiles((prev) => [...prev, ...files])
    }
  }
  const removeFile = (index: number) => {
    setAttachedFiles((prev) => prev.filter((_, i) => i !== index))
  }

  const handleSubmit = () => {
    if (!prompt.trim() || isRunning) return
    onSubmit(attachedFiles.length > 0 ? attachedFiles : undefined)
    setAttachedFiles([])
  }

  return (
    <div className="flex h-full flex-col">
      {/* 프로젝트 헤더 */}
      <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-2.5 shrink-0">
        <div className="flex items-center gap-3">
          <h1 className="text-sm font-bold text-white">{projectName}</h1>
          {isRunning && (
            <span className="flex items-center gap-1.5 rounded-full bg-emerald-500/10 border border-emerald-500/20 px-2.5 py-0.5 text-[10px] font-medium text-emerald-400">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
              실행 중
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

      {/* 프롬프트 입력 영역 — 확대 + 드래그앤드롭 */}
      <div
        className={`border-b border-zinc-800 px-4 py-3 shrink-0 transition-colors ${
          isDragging ? 'bg-emerald-500/5 border-emerald-500/30' : ''
        }`}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        <div className={`rounded-xl border bg-zinc-900/50 transition-colors ${
          isDragging ? 'border-emerald-500/40 shadow-lg shadow-emerald-500/5' : 'border-zinc-800'
        }`}>
          <textarea
            value={prompt}
            onChange={(e) => onPromptChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && prompt.trim() && !isRunning) handleSubmit()
            }}
            placeholder={isDragging
              ? '파일을 여기에 놓으세요...'
              : '추가할 기능을 설명하세요...\n이미지나 파일을 드래그하여 첨부할 수 있습니다.'
            }
            className="w-full resize-none rounded-t-xl bg-transparent px-4 py-3 text-sm text-white placeholder:text-zinc-600 focus:outline-none"
            rows={compact ? 3 : 4}
            disabled={isRunning}
          />

          {/* 첨부 파일 표시 */}
          {attachedFiles.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-4 pb-2">
              {attachedFiles.map((file, i) => (
                <span
                  key={`${file.name}-${i}`}
                  className="flex items-center gap-1 rounded-md bg-zinc-800 px-2 py-1 text-[10px] text-zinc-400"
                >
                  {file.type.startsWith('image/') ? '🖼' : '📎'}
                  <span className="max-w-24 truncate">{file.name}</span>
                  <button
                    onClick={() => removeFile(i)}
                    className="ml-0.5 text-zinc-600 hover:text-white transition-colors"
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}

          {/* 하단 바 */}
          <div className="flex items-center justify-between border-t border-zinc-800/50 px-4 py-2">
            <div className="flex items-center gap-2">
              <button
                onClick={() => fileInputRef.current?.click()}
                className="rounded-md px-2 py-1 text-[11px] text-zinc-600 hover:text-zinc-400 hover:bg-zinc-800 transition-colors"
                disabled={isRunning}
              >
                📎 파일 첨부
              </button>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept="image/*,.pdf,.md,.txt"
                onChange={handleFileSelect}
                className="hidden"
              />
              <span className="text-[10px] text-zinc-700">Ctrl+Enter로 실행</span>
            </div>

            <button
              onClick={handleSubmit}
              disabled={!prompt.trim() || isRunning}
              className={`rounded-lg px-4 py-1.5 text-xs font-medium transition-all ${
                prompt.trim() && !isRunning
                  ? 'bg-emerald-500 text-white hover:bg-emerald-400 shadow-lg shadow-emerald-500/20'
                  : 'bg-zinc-800 text-zinc-600 cursor-not-allowed'
              }`}
            >
              하네스 실행
            </button>
          </div>
        </div>
      </div>

      {/* 칸반 보드 */}
      <div className={`flex flex-1 gap-2 overflow-x-auto p-3 ${compact ? 'gap-1.5 p-2' : ''}`}>
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
            activeAgent.state === 'running' || activeAgent.state === 'thinking' || activeAgent.state === 'tool_calling'
          )

          return (
            <div
              key={column.id}
              className={`flex ${compact ? 'w-36' : 'w-48'} shrink-0 flex-col rounded-lg border bg-zinc-900/30 ${
                isActiveColumn ? 'border-emerald-500/30 shadow-lg shadow-emerald-500/5' : 'border-zinc-800/50'
              }`}
            >
              <div className="flex items-center justify-between px-2.5 py-2 border-b border-zinc-800/50">
                <div className="flex items-center gap-1.5">
                  <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: column.color }} />
                  <span className="text-[10px] font-semibold text-zinc-400">{column.label}</span>
                </div>
                <span className="rounded bg-zinc-800 px-1 py-0.5 text-[9px] font-mono text-zinc-500">{columnCards.length}</span>
              </div>

              <div className="flex-1 space-y-1.5 overflow-y-auto p-1.5">
                {columnCards.map((card) => (
                  <KanbanCardItem key={card.id} card={card} compact={compact} />
                ))}
                {columnCards.length === 0 && !isActiveColumn && (
                  <div className="py-3 text-center text-[9px] text-zinc-700">비어 있음</div>
                )}
                {isActiveColumn && activeAgent && (
                  <div className="rounded-md border border-emerald-500/20 bg-emerald-500/5 p-2">
                    <div className="flex items-center gap-1.5">
                      <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
                      <span className="text-[10px] font-medium text-emerald-400">{activeAgent.name}</span>
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

// 단계 순서와 진행률 계산
const STAGE_ORDER: KanbanColumn[] = ['backlog', 'planning', 'designing', 'developing', 'reviewing', 'done']
function getProgress(status: KanbanColumn): number {
  const idx = STAGE_ORDER.indexOf(status)
  return idx >= 0 ? Math.round((idx / (STAGE_ORDER.length - 1)) * 100) : 0
}

// 에이전트 role → 아바타 정보
const AGENT_AVATARS: Record<string, { icon: string; color: string }> = {
  pm_planner: { icon: '📋', color: '#8b5cf6' },
  designer: { icon: '🎨', color: '#ec4899' },
  developer: { icon: '⚙️', color: '#34d399' },
  security_expert: { icon: '🔒', color: '#f59e0b' },
  reviewer: { icon: '📝', color: '#3b82f6' },
}

function KanbanCardItem({ card, compact }: { card: KanbanCard; compact: boolean }) {
  const timeAgo = getTimeAgo(card.startedAt)
  const isDone = card.status === 'done'
  const isFailed = card.qaScore?.includes('REJECT')
  const progress = getProgress(card.status)
  const completedAgents = card.completedAgents ?? []
  const duration = card.completedAt
    ? Math.round((card.completedAt - card.startedAt) / 1000)
    : Math.round((Date.now() - card.startedAt) / 1000)

  return (
    <div className={`rounded-lg border p-2.5 transition-all ${
      isDone
        ? 'border-emerald-500/20 bg-emerald-500/5'
        : isFailed
          ? 'border-red-500/20 bg-red-500/5'
          : 'border-zinc-800 bg-zinc-900/50 hover:border-zinc-700'
    }`}>
      {/* 태그 */}
      {card.tags && card.tags.length > 0 && (
        <div className="flex flex-wrap gap-1 mb-1.5">
          {card.tags.map((tag) => (
            <span key={tag} className="rounded bg-zinc-800 px-1.5 py-0.5 text-[9px] font-medium text-zinc-500">
              {tag}
            </span>
          ))}
        </div>
      )}

      {/* 프롬프트 텍스트 */}
      <p className={`text-white leading-relaxed ${compact ? 'text-[10px] line-clamp-2' : 'text-[11px] line-clamp-3'}`}>
        {card.prompt}
      </p>

      {/* 진행률 바 */}
      {!isDone && progress > 0 && (
        <div className="mt-2 h-1 w-full rounded-full bg-zinc-800 overflow-hidden">
          <div
            className="h-full rounded-full bg-emerald-500 transition-all duration-500"
            style={{ width: `${progress}%` }}
          />
        </div>
      )}

      {/* 하단: 에이전트 아바타 + 메타 */}
      <div className="mt-2 flex items-center justify-between">
        <div className="flex items-center gap-1">
          {/* 완료된 에이전트 아바타들 */}
          <div className="flex -space-x-1">
            {completedAgents.slice(0, 5).map((role) => {
              const avatar = AGENT_AVATARS[role]
              return avatar ? (
                <span
                  key={role}
                  className="flex h-4 w-4 items-center justify-center rounded-full text-[8px] border border-zinc-900"
                  style={{ backgroundColor: `${avatar.color}20` }}
                  title={AGENT_ROLE_META[role as keyof typeof AGENT_ROLE_META]?.label ?? role}
                >
                  {avatar.icon}
                </span>
              ) : null
            })}
          </div>

          {/* 현재 작업 중인 에이전트 */}
          {card.currentAgent && !isDone && (
            <span className="flex items-center gap-0.5 text-[9px] text-emerald-400">
              <span className="inline-block h-1 w-1 animate-pulse rounded-full bg-emerald-400" />
              {AGENT_AVATARS[card.currentAgent]?.icon}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {/* QA 점수 */}
          {card.qaScore && (
            <span className={`rounded px-1.5 py-0.5 text-[9px] font-mono font-semibold ${
              card.qaScore.includes('PASS')
                ? 'bg-emerald-500/10 text-emerald-400'
                : card.qaScore.includes('REJECT')
                  ? 'bg-red-500/10 text-red-400'
                  : 'bg-zinc-800 text-zinc-400'
            }`}>
              {card.qaScore}
            </span>
          )}

          {/* 소요 시간 */}
          <span className="font-mono text-[9px] text-zinc-600">
            {duration > 60 ? `${Math.floor(duration / 60)}m` : `${duration}s`}
          </span>

          {/* 시간 */}
          <span className="text-[9px] text-zinc-700">{timeAgo}</span>
        </div>
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
  return `${Math.floor(hours / 24)}일 전`
}

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
