'use client'

import { useState, useEffect } from 'react'

interface Project {
  id: string
  name: string
  icon: string
  platform: string
  lastRunAt?: number
  lastStatus?: 'completed' | 'failed'
}

interface Execution {
  id: string
  projectId: string
  userPrompt: string
  status: 'completed' | 'failed' | 'stopped'
  startedAt: number
  completedAt: number
  results: Array<{ role: string; status: string; durationMs: number }>
}

interface DashboardMainProps {
  projects: Project[]
  onSelectProject: (id: string) => void
}

const AGENT_ICONS: Record<string, { icon: string; color: string }> = {
  pm_planner: { icon: '📋', color: '#8b5cf6' },
  designer: { icon: '🎨', color: '#ec4899' },
  developer: { icon: '⚙️', color: '#34d399' },
  security_expert: { icon: '🔒', color: '#f59e0b' },
  reviewer: { icon: '📝', color: '#3b82f6' },
}

export default function DashboardMain({ projects, onSelectProject }: DashboardMainProps) {
  const [executions, setExecutions] = useState<Execution[]>([])

  useEffect(() => {
    fetch('/api/executions')
      .then((res) => res.json())
      .then(setExecutions)
      .catch(() => {})
  }, [])

  const totalRuns = executions.length
  const successRuns = executions.filter((e) => e.status === 'completed').length
  const failedRuns = executions.filter((e) => e.status === 'failed').length
  const totalAgentRuns = executions.reduce((sum, e) => sum + (e.results?.length ?? 0), 0)

  return (
    <div className="flex-1 overflow-y-auto">
      {/* 헤더 */}
      <div className="border-b border-zinc-800 px-4 md:px-6 py-4">
        <div className="flex items-center justify-between">
          <h1 className="text-xs font-bold uppercase tracking-wider text-zinc-500">Dashboard</h1>
          <span className="text-[10px] text-zinc-700">Agent Flow v0.2</span>
        </div>
      </div>

      <div className="px-4 md:px-6 py-5">
        {/* 통계 카드 — Paperclip 스타일 */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
          <StatCard
            value={projects.length}
            label="Projects"
            sub={`${projects.length}개 등록`}
            icon="◈"
            iconBg="bg-emerald-500/10"
            iconColor="text-emerald-400"
          />
          <StatCard
            value={totalRuns}
            label="Total Runs"
            sub={`${totalAgentRuns} 에이전트 실행`}
            icon="◎"
            iconBg="bg-blue-500/10"
            iconColor="text-blue-400"
          />
          <StatCard
            value={`$0.00`}
            label="Month Spend"
            sub="Max 구독 (무제한)"
            icon="$"
            iconBg="bg-amber-500/10"
            iconColor="text-amber-400"
            large
          />
          <StatCard
            value={failedRuns}
            label="Failed"
            sub={successRuns > 0 ? `성공률 ${Math.round((successRuns / totalRuns) * 100)}%` : '0 에러'}
            icon="✓"
            iconBg={failedRuns > 0 ? 'bg-red-500/10' : 'bg-zinc-800'}
            iconColor={failedRuns > 0 ? 'text-red-400' : 'text-zinc-500'}
          />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-5 gap-5">
          {/* 좌(3열): 프로젝트 */}
          <div className="lg:col-span-2">
            <h2 className="mb-3 text-[10px] font-bold uppercase tracking-wider text-zinc-600">
              Projects
            </h2>
            <div className="space-y-2">
              {projects.map((project) => {
                const projExecs = executions.filter((e) => e.projectId === project.id)
                const lastExec = projExecs[0]
                const projSuccess = projExecs.filter((e) => e.status === 'completed').length

                return (
                  <button
                    key={project.id}
                    onClick={() => onSelectProject(project.id)}
                    className="group flex w-full items-center gap-3 rounded-xl border border-zinc-800 bg-zinc-900/50 px-4 py-3.5 text-left hover:border-zinc-700 hover:bg-zinc-900 transition-all"
                  >
                    {/* 프로젝트 아이콘 */}
                    <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-zinc-800 text-lg">
                      {project.icon}
                    </div>

                    {/* 정보 */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-semibold text-white">{project.name}</span>
                        <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[9px] font-medium text-zinc-500">
                          {project.platform}
                        </span>
                      </div>
                      <div className="mt-1 flex items-center gap-3 text-[10px] text-zinc-600">
                        <span>{projExecs.length}회 실행</span>
                        {projSuccess > 0 && <span className="text-emerald-500">{projSuccess} 성공</span>}
                        {lastExec && (
                          <span className="flex items-center gap-1">
                            <span className={`h-1.5 w-1.5 rounded-full ${
                              lastExec.status === 'completed' ? 'bg-emerald-400' : 'bg-red-400'
                            }`} />
                            {getTimeAgo(lastExec.startedAt)}
                          </span>
                        )}
                      </div>
                    </div>

                    {/* 화살표 */}
                    <span className="text-zinc-700 group-hover:text-zinc-400 transition-colors">→</span>
                  </button>
                )
              })}

              {projects.length === 0 && (
                <div className="rounded-xl border border-dashed border-zinc-800 px-4 py-10 text-center">
                  <div className="text-2xl mb-2">📁</div>
                  <div className="text-xs text-zinc-600">등록된 프로젝트가 없습니다</div>
                </div>
              )}
            </div>
          </div>

          {/* 우(3열): 최근 실행 기록 */}
          <div className="lg:col-span-3">
            <h2 className="mb-3 text-[10px] font-bold uppercase tracking-wider text-zinc-600">
              Recent Activity
            </h2>
            <div className="space-y-1.5">
              {executions.length === 0 && (
                <div className="rounded-xl border border-dashed border-zinc-800 px-4 py-10 text-center">
                  <div className="text-2xl mb-2">⏳</div>
                  <div className="text-xs text-zinc-600">실행 기록이 없습니다</div>
                  <div className="mt-1 text-[10px] text-zinc-700">프로젝트를 선택하고 아이디어를 입력하세요</div>
                </div>
              )}
              {executions.slice(0, 10).map((exec) => {
                const project = projects.find((p) => p.id === exec.projectId)
                const duration = exec.completedAt
                  ? Math.round((exec.completedAt - exec.startedAt) / 1000)
                  : 0
                const agentResults = exec.results ?? []

                return (
                  <div
                    key={exec.id}
                    className="rounded-lg border border-zinc-800/50 bg-zinc-900/30 px-4 py-3 hover:bg-zinc-900/50 transition-colors"
                  >
                    {/* 상단: 프로젝트 + 상태 + 시간 */}
                    <div className="flex items-center justify-between mb-1.5">
                      <div className="flex items-center gap-2">
                        <span className="text-sm">{project?.icon ?? '📁'}</span>
                        <span className="text-xs font-medium text-white">{project?.name ?? exec.projectId}</span>
                        <StatusBadge status={exec.status} />
                      </div>
                      <div className="flex items-center gap-3 text-[10px] text-zinc-600">
                        {duration > 0 && (
                          <span className="font-mono">
                            {duration > 60 ? `${Math.floor(duration / 60)}m ${duration % 60}s` : `${duration}s`}
                          </span>
                        )}
                        <span>{getTimeAgo(exec.startedAt)}</span>
                      </div>
                    </div>

                    {/* 프롬프트 */}
                    <p className="text-[11px] text-zinc-500 leading-relaxed line-clamp-1 mb-2">
                      {exec.userPrompt}
                    </p>

                    {/* 에이전트 결과 아바타 */}
                    {agentResults.length > 0 && (
                      <div className="flex items-center gap-1">
                        {agentResults.map((r, i) => {
                          const agent = AGENT_ICONS[r.role]
                          if (!agent) return null
                          const isSuccess = r.status === 'success'
                          return (
                            <span
                              key={`${r.role}-${i}`}
                              className={`flex h-5 w-5 items-center justify-center rounded-full text-[9px] border ${
                                isSuccess
                                  ? 'border-emerald-500/30 bg-emerald-500/10'
                                  : 'border-red-500/30 bg-red-500/10'
                              }`}
                              title={`${r.role}: ${r.status}`}
                            >
                              {agent.icon}
                            </span>
                          )
                        })}
                        <span className="ml-1 text-[9px] text-zinc-600">
                          {agentResults.filter((r) => r.status === 'success').length}/{agentResults.length} pass
                        </span>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function StatCard({
  value,
  label,
  sub,
  icon,
  iconBg,
  iconColor,
  large,
}: {
  value: string | number
  label: string
  sub: string
  icon: string
  iconBg: string
  iconColor: string
  large?: boolean
}) {
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/50 px-4 py-4">
      <div className="flex items-start justify-between mb-2">
        <div>
          <div className={`font-bold text-white ${large ? 'text-xl md:text-2xl' : 'text-2xl'} font-mono`}>
            {value}
          </div>
          <div className="mt-0.5 text-xs text-zinc-500">{label}</div>
        </div>
        <span className={`flex h-8 w-8 items-center justify-center rounded-lg text-sm ${iconBg} ${iconColor}`}>
          {icon}
        </span>
      </div>
      <div className="text-[10px] text-zinc-600">{sub}</div>
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const styles = status === 'completed'
    ? 'bg-emerald-500/10 text-emerald-400'
    : status === 'failed'
      ? 'bg-red-500/10 text-red-400'
      : 'bg-zinc-500/10 text-zinc-400'
  const label = status === 'completed' ? '완료' : status === 'failed' ? '실패' : '중단'
  return <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${styles}`}>{label}</span>
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
