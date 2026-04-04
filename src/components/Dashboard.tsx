'use client'

import { useState, useEffect } from 'react'
import type { Platform } from '@/lib/types'

interface Project {
  id: string
  name: string
  path: string
  platform: Platform
  icon: string
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
  totalCost: number
  results: Array<{
    role: string
    status: 'success' | 'error'
    durationMs: number
    outputPreview: string
  }>
}

interface DashboardProps {
  onRunHarness: (projectId: string, projectPath: string) => void
}

export default function Dashboard({ onRunHarness }: DashboardProps) {
  const [projects, setProjects] = useState<Project[]>([])
  const [executions, setExecutions] = useState<Execution[]>([])
  const [loading, setLoading] = useState(true)

  // 프로젝트 + 실행 기록 로드
  useEffect(() => {
    async function load() {
      try {
        const [projRes, execRes] = await Promise.all([
          fetch('/api/projects'),
          fetch('/api/executions'),
        ])
        if (projRes.ok) setProjects(await projRes.json())
        if (execRes.ok) setExecutions(await execRes.json())
      } catch {
        // 로드 실패 무시
      }
      setLoading(false)
    }
    load()
  }, [])

  // 통계
  const totalProjects = projects.length
  const runningCount = 0 // 현재 실행 중인 건 page.tsx에서 관리
  const recentExecutions = executions.slice(0, 10)
  const totalRuns = executions.length
  const successRuns = executions.filter((e) => e.status === 'completed').length

  // 최근 활동 (실행 기록에서 생성)
  const activities = recentExecutions.map((exec) => {
    const project = projects.find((p) => p.id === exec.projectId)
    const timeAgo = getTimeAgo(exec.completedAt || exec.startedAt)
    const agentCount = exec.results?.length ?? 0
    return {
      id: exec.id,
      project: project?.name ?? exec.projectId,
      icon: project?.icon ?? '📁',
      prompt: exec.userPrompt,
      status: exec.status,
      timeAgo,
      agentCount,
      cost: exec.totalCost,
    }
  })

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[#09090b]">
        <div className="text-sm text-zinc-500">로딩 중...</div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-[#09090b]">
      {/* 헤더 */}
      <div className="border-b border-zinc-800 px-6 py-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xs font-bold uppercase tracking-wider text-zinc-500">DASHBOARD</h1>
            <p className="mt-1 text-sm text-zinc-600">Agent Flow</p>
          </div>
        </div>
      </div>

      <div className="px-6 py-6">
        {/* 통계 카드 4열 */}
        <div className="grid grid-cols-4 gap-4 mb-8">
          <StatCard value={totalProjects} label="Projects" sub={`${totalProjects}개 등록`} />
          <StatCard value={totalRuns} label="Total Runs" sub={`${successRuns} 성공`} />
          <StatCard value="$0.00" label="Month Spend" sub="Max 구독 (무제한)" />
          <StatCard value={recentExecutions.filter((e) => e.status === 'failed').length} label="Failed Runs" sub="최근 실패" />
        </div>

        <div className="grid grid-cols-2 gap-6">
          {/* 좌: 프로젝트 목록 */}
          <div>
            <h2 className="mb-3 text-xs font-bold uppercase tracking-wider text-zinc-500">
              PROJECTS
            </h2>
            <div className="space-y-2">
              {projects.map((project) => {
                const lastExec = executions.find((e) => e.projectId === project.id)
                const timeAgo = project.lastRunAt ? getTimeAgo(project.lastRunAt) : null

                return (
                  <div
                    key={project.id}
                    className="group flex items-center justify-between rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3 hover:border-zinc-700 transition-colors"
                  >
                    <div className="flex items-center gap-3">
                      <span className="text-lg">{project.icon}</span>
                      <div>
                        <div className="text-sm font-medium text-white">{project.name}</div>
                        <div className="flex items-center gap-2 text-[10px] text-zinc-600">
                          <span>{project.platform}</span>
                          {timeAgo && (
                            <>
                              <span>·</span>
                              <span className="flex items-center gap-1">
                                <span
                                  className={`inline-block h-1.5 w-1.5 rounded-full ${
                                    project.lastStatus === 'completed' ? 'bg-emerald-400' : 'bg-red-400'
                                  }`}
                                />
                                {timeAgo}
                              </span>
                            </>
                          )}
                        </div>
                      </div>
                    </div>

                    <button
                      onClick={() => onRunHarness(project.id, project.path)}
                      className="rounded-md border border-emerald-500/20 bg-emerald-500/10 px-3 py-1 text-[11px] font-medium text-emerald-400 opacity-0 group-hover:opacity-100 hover:bg-emerald-500/20 transition-all"
                    >
                      하네스 실행
                    </button>
                  </div>
                )
              })}

              {projects.length === 0 && (
                <div className="rounded-lg border border-dashed border-zinc-800 px-4 py-8 text-center text-xs text-zinc-600">
                  등록된 프로젝트가 없습니다
                </div>
              )}
            </div>
          </div>

          {/* 우: 최근 실행 기록 */}
          <div>
            <h2 className="mb-3 text-xs font-bold uppercase tracking-wider text-zinc-500">
              RECENT ACTIVITY
            </h2>
            <div className="space-y-1.5">
              {activities.length === 0 && (
                <div className="rounded-lg border border-dashed border-zinc-800 px-4 py-8 text-center text-xs text-zinc-600">
                  실행 기록이 없습니다
                </div>
              )}
              {activities.map((activity) => (
                <div
                  key={activity.id}
                  className="flex items-start justify-between rounded-lg border border-zinc-800/50 bg-zinc-900/30 px-3 py-2.5"
                >
                  <div className="flex items-start gap-2.5 min-w-0">
                    <span className="mt-0.5 text-sm">{activity.icon}</span>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-medium text-white">{activity.project}</span>
                        <span
                          className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                            activity.status === 'completed'
                              ? 'bg-emerald-500/10 text-emerald-400'
                              : activity.status === 'failed'
                                ? 'bg-red-500/10 text-red-400'
                                : 'bg-zinc-500/10 text-zinc-400'
                          }`}
                        >
                          {activity.status === 'completed' ? '완료' : activity.status === 'failed' ? '실패' : '중단'}
                        </span>
                      </div>
                      <p className="mt-0.5 truncate text-[11px] text-zinc-500">
                        {activity.prompt}
                      </p>
                      <div className="mt-1 flex items-center gap-2 text-[10px] text-zinc-600">
                        <span>{activity.agentCount}개 에이전트</span>
                      </div>
                    </div>
                  </div>
                  <span className="shrink-0 text-[10px] text-zinc-600">{activity.timeAgo}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function StatCard({ value, label, sub }: { value: string | number; label: string; sub: string }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-4">
      <div className="text-2xl font-bold text-white">{value}</div>
      <div className="mt-1 text-xs text-zinc-500">{label}</div>
      <div className="text-[10px] text-zinc-600">{sub}</div>
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
