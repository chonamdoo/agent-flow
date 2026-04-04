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
  results: Array<{ role: string; status: string }>
}

interface DashboardMainProps {
  projects: Project[]
  onSelectProject: (id: string) => void
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

  return (
    <div className="flex-1 overflow-y-auto">
      {/* 헤더 */}
      <div className="border-b border-zinc-800 px-6 py-4">
        <h1 className="text-xs font-bold uppercase tracking-wider text-zinc-500">Dashboard</h1>
      </div>

      <div className="px-6 py-6">
        {/* 통계 카드 */}
        <div className="grid grid-cols-4 gap-4 mb-8">
          <StatCard value={projects.length} label="Projects" />
          <StatCard value={totalRuns} label="Total Runs" />
          <StatCard value={successRuns} label="Completed" accent="emerald" />
          <StatCard value={failedRuns} label="Failed" accent={failedRuns > 0 ? 'red' : undefined} />
        </div>

        <div className="grid grid-cols-2 gap-6">
          {/* 좌: 프로젝트 */}
          <div>
            <h2 className="mb-3 text-[10px] font-bold uppercase tracking-wider text-zinc-600">
              Projects
            </h2>
            <div className="space-y-1.5">
              {projects.map((project) => (
                <button
                  key={project.id}
                  onClick={() => onSelectProject(project.id)}
                  className="group flex w-full items-center justify-between rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3 text-left hover:border-zinc-700 transition-colors"
                >
                  <div className="flex items-center gap-3">
                    <span className="text-lg">{project.icon}</span>
                    <div>
                      <div className="text-sm font-medium text-white">{project.name}</div>
                      <div className="text-[10px] text-zinc-600">{project.platform}</div>
                    </div>
                  </div>
                  <span className="text-[11px] text-zinc-600 opacity-0 group-hover:opacity-100 transition-opacity">
                    열기 →
                  </span>
                </button>
              ))}
            </div>
          </div>

          {/* 우: 최근 실행 */}
          <div>
            <h2 className="mb-3 text-[10px] font-bold uppercase tracking-wider text-zinc-600">
              Recent Activity
            </h2>
            <div className="space-y-1.5">
              {executions.length === 0 && (
                <div className="rounded-lg border border-dashed border-zinc-800 px-4 py-8 text-center text-xs text-zinc-600">
                  실행 기록이 없습니다
                </div>
              )}
              {executions.slice(0, 8).map((exec) => {
                const project = projects.find((p) => p.id === exec.projectId)
                return (
                  <div
                    key={exec.id}
                    className="flex items-start justify-between rounded-lg border border-zinc-800/50 bg-zinc-900/30 px-3 py-2.5"
                  >
                    <div className="flex items-start gap-2 min-w-0">
                      <span className="mt-0.5 text-sm">{project?.icon ?? '📁'}</span>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="text-xs font-medium text-white">{project?.name ?? exec.projectId}</span>
                          <StatusBadge status={exec.status} />
                        </div>
                        <p className="mt-0.5 truncate text-[11px] text-zinc-500">{exec.userPrompt}</p>
                      </div>
                    </div>
                    <span className="shrink-0 text-[10px] text-zinc-600">{getTimeAgo(exec.startedAt)}</span>
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

function StatCard({ value, label, accent }: { value: number; label: string; accent?: string }) {
  const color = accent === 'emerald' ? 'text-emerald-400' : accent === 'red' ? 'text-red-400' : 'text-white'
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-4">
      <div className={`text-2xl font-bold ${color}`}>{value}</div>
      <div className="mt-1 text-xs text-zinc-500">{label}</div>
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
