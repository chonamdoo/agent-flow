'use client'

import { useState, useEffect } from 'react'
import type { Platform } from '@/lib/types'

interface Project {
  id: string
  name: string
  path: string
  platform: Platform
  icon: string
}

interface PromptInputProps {
  projectId: string
  projectPath: string
  onSubmit: (prompt: string, projectId: string, projectPath: string) => void
  onBack: () => void
  isRunning: boolean
}

export default function PromptInput({ projectId, projectPath, onSubmit, onBack, isRunning }: PromptInputProps) {
  const [prompt, setPrompt] = useState('')
  const [projects, setProjects] = useState<Project[]>([])
  const [selectedProjectId, setSelectedProjectId] = useState(projectId)
  const [selectedProjectPath, setSelectedProjectPath] = useState(projectPath)

  // 프로젝트 목록 로드
  useEffect(() => {
    fetch('/api/projects')
      .then((res) => res.json())
      .then((data) => {
        setProjects(data)
        // 초기 프로젝트 경로 설정
        const proj = data.find((p: Project) => p.id === selectedProjectId)
        if (proj?.path) setSelectedProjectPath(proj.path)
      })
      .catch(() => {})
  }, [])

  const handleProjectChange = (newId: string) => {
    setSelectedProjectId(newId)
    const proj = projects.find((p) => p.id === newId)
    if (proj?.path) setSelectedProjectPath(proj.path)
  }

  const handleSubmit = () => {
    const trimmed = prompt.trim()
    if (!trimmed || isRunning) return
    onSubmit(trimmed, selectedProjectId, selectedProjectPath)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      handleSubmit()
    }
  }

  const selectedProject = projects.find((p) => p.id === selectedProjectId)

  return (
    <div className="flex min-h-screen items-center justify-center bg-[#09090b] p-4">
      <div className="w-full max-w-2xl">
        {/* 헤더 */}
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-bold text-white mb-2">Agent Flow</h1>
          <p className="text-sm text-zinc-500">
            아이디어를 입력하면 AI 에이전트 팀이 기획 → 디자인 → 구현 → 보안 → 검수를 자동 실행합니다.
          </p>
        </div>

        {/* 프로젝트 선택 */}
        <div className="mb-4 flex items-center justify-center gap-2">
          <div className="relative">
            <select
              value={selectedProjectId}
              onChange={(e) => handleProjectChange(e.target.value)}
              className="appearance-none rounded-full bg-zinc-900 border border-zinc-700 pl-3 pr-8 py-1.5 text-xs font-medium text-white focus:outline-none focus:border-emerald-500/50 cursor-pointer"
            >
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.icon} {p.name}
                </option>
              ))}
            </select>
            <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[10px] text-zinc-500">
              ▼
            </span>
          </div>
          {selectedProject && (
            <span className="text-[10px] text-zinc-600 max-w-48 truncate" title={selectedProjectPath}>
              {selectedProjectPath}
            </span>
          )}
          <button
            onClick={onBack}
            className="text-xs text-zinc-600 hover:text-zinc-400 transition-colors"
          >
            대시보드
          </button>
        </div>

        {/* 입력 영역 */}
        <div className="rounded-xl border border-zinc-800 bg-zinc-950 p-1">
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="아이디어를 입력하세요... (예: 거래 통계 대시보드에 월별 수익률 차트와 승률 히트맵을 추가해줘)"
            className="w-full resize-none rounded-lg bg-transparent px-4 py-3 text-sm text-white placeholder:text-zinc-600 focus:outline-none"
            rows={5}
            disabled={isRunning}
            autoFocus
          />

          <div className="flex items-center justify-between border-t border-zinc-800/50 px-4 py-2">
            <div className="flex items-center gap-3 text-[10px] text-zinc-600">
              <span>PM → Designer → Developer → Security → Reviewer</span>
            </div>

            <div className="flex items-center gap-2">
              <span className="text-[10px] text-zinc-600">
                {isRunning ? '실행 중...' : 'Ctrl+Enter'}
              </span>
              <button
                onClick={handleSubmit}
                disabled={!prompt.trim() || isRunning}
                className={`rounded-lg px-4 py-1.5 text-xs font-medium transition-all ${
                  prompt.trim() && !isRunning
                    ? 'bg-emerald-500 text-white hover:bg-emerald-400 shadow-lg shadow-emerald-500/20'
                    : 'bg-zinc-800 text-zinc-600 cursor-not-allowed'
                }`}
              >
                {isRunning ? (
                  <span className="flex items-center gap-1.5">
                    <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-400" />
                    실행 중
                  </span>
                ) : (
                  '하네스 실행'
                )}
              </button>
            </div>
          </div>
        </div>

        {/* 안내 */}
        <div className="mt-6 grid grid-cols-3 gap-3">
          <InfoCard
            title="비용"
            desc="$0 — Max 구독 범위 (CLI 모드)"
          />
          <InfoCard
            title="소요 시간"
            desc="에이전트 5개 순차 실행, 약 3~5분"
          />
          <InfoCard
            title="실행 환경"
            desc="로컬 Claude Code CLI 사용"
          />
        </div>
      </div>
    </div>
  )
}

function InfoCard({ title, desc }: { title: string; desc: string }) {
  return (
    <div className="rounded-lg border border-zinc-800/50 bg-zinc-900/50 px-3 py-2">
      <div className="text-[10px] font-medium text-zinc-500 mb-0.5">{title}</div>
      <div className="text-[11px] text-zinc-600">{desc}</div>
    </div>
  )
}
