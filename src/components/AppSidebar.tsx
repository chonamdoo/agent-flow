'use client'

import { AGENT_ROLE_META, AGENT_STATE_COLORS } from '@/lib/types'
import type { AgentNodeData } from '@/lib/types'

interface Project {
  id: string
  name: string
  icon: string
  platform: string
  lastStatus?: 'completed' | 'failed'
}

type View = 'dashboard' | 'kanban' | 'flow'

interface AppSidebarProps {
  projects: Project[]
  selectedProjectId: string | null
  currentView: View
  onSelectDashboard: () => void
  onSelectProject: (id: string) => void
  agents: AgentNodeData[]
  isRunning: boolean
}

export default function AppSidebar({
  projects,
  selectedProjectId,
  currentView,
  onSelectDashboard,
  onSelectProject,
  agents,
  isRunning,
}: AppSidebarProps) {
  return (
    <div className="flex h-screen w-14 md:w-56 flex-col border-r border-zinc-800 bg-zinc-950 shrink-0 transition-all">
      {/* 로고 */}
      <div className="flex items-center gap-2 border-b border-zinc-800 px-2 md:px-4 py-3 justify-center md:justify-start">
        <span className="text-sm font-bold text-white hidden md:block">Agent Flow</span>
        <span className="text-sm font-bold text-white md:hidden">AF</span>
        {isRunning && (
          <span className="hidden md:flex items-center gap-1 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[10px] text-emerald-400">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
            live
          </span>
        )}
        {isRunning && (
          <span className="md:hidden inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-400" />
        )}
      </div>

      {/* 대시보드 버튼 */}
      <div className="px-3 pt-3">
        <button
          onClick={onSelectDashboard}
          className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors justify-center md:justify-start ${
            currentView === 'dashboard'
              ? 'bg-zinc-800 text-white'
              : 'text-zinc-400 hover:bg-zinc-900 hover:text-zinc-300'
          }`}
        >
          <span className="text-xs">⊞</span>
          <span className="text-xs font-medium hidden md:block">Dashboard</span>
        </button>
      </div>

      {/* 프로젝트 목록 */}
      <div className="px-3 pt-4 pb-2">
        <div className="mb-2 flex items-center justify-between px-1">
          <span className="text-[10px] font-bold uppercase tracking-wider text-zinc-600 hidden md:block">
            Projects
          </span>
          <span className="text-[10px] text-zinc-700 cursor-pointer hover:text-zinc-400 hidden md:block">+</span>
        </div>

        <div className="space-y-0.5">
          {projects.map((project) => {
            const isSelected = project.id === selectedProjectId && currentView !== 'dashboard'
            return (
              <button
                key={project.id}
                onClick={() => onSelectProject(project.id)}
                className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors justify-center md:justify-start ${
                  isSelected
                    ? 'bg-zinc-800 text-white'
                    : 'text-zinc-400 hover:bg-zinc-900 hover:text-zinc-300'
                }`}
                title={project.name}
              >
                <span className="text-sm">{project.icon}</span>
                <span className="text-xs font-medium truncate hidden md:block">{project.name}</span>
              </button>
            )
          })}
        </div>
      </div>

      {/* 에이전트 상태 */}
      <div className="mt-auto border-t border-zinc-800 px-3 py-3">
        <div className="mb-2 px-1">
          <span className="text-[10px] font-bold uppercase tracking-wider text-zinc-600 hidden md:block">
            Agents
          </span>
        </div>

        <div className="space-y-0.5">
          {agents
            .filter((a) => a.role !== 'orchestrator')
            .map((agent) => {
              const meta = AGENT_ROLE_META[agent.role]
              const stateColor = AGENT_STATE_COLORS[agent.state]
              const isActive = agent.state === 'running' || agent.state === 'thinking' || agent.state === 'tool_calling'

              return (
                <div
                  key={agent.id}
                  className="flex items-center gap-2 rounded-md px-2 py-1 text-xs text-zinc-500 justify-center md:justify-start"
                  title={agent.name}
                >
                  <span
                    className="inline-block h-1.5 w-1.5 rounded-full shrink-0"
                    style={{ backgroundColor: stateColor }}
                  />
                  <span className="text-[11px] shrink-0">{meta.icon}</span>
                  <span className={`truncate hidden md:block ${isActive ? 'text-white font-medium' : ''}`}>
                    {agent.name}
                  </span>
                  {isActive && (
                    <span className="ml-auto text-[9px] text-emerald-400 hidden md:block">live</span>
                  )}
                </div>
              )
            })}
        </div>
      </div>
    </div>
  )
}
